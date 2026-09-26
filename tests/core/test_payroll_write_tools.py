from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from mcp import Client
from pydantic import ValidationError

from odoo_mcp.adapters.accounting import RelatedRecord
from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.connections import ConnectionBinding
from odoo_mcp.adapters.payroll import (
    DeletedPayslipInput,
    DraftPayslipInputCreate,
    DraftPayslipInputUpdate,
    PayrollContractFilters,
    PayrollContractSegment,
    PayrollEmployee,
    PayrollInputType,
    PayrollInputTypeFilters,
    PayrollPage,
    PayrollPageRequest,
    PayrollStructure,
    PayrollWriteRejected,
    Payslip,
    PayslipChildFilters,
    PayslipFilters,
    PayslipInput,
    PayslipLine,
    PayslipWorkedDay,
)
from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.payroll_write_schemas import (
    RecalculateDraftPayslip,
    SetDraftPayrollInput,
)
from odoo_mcp.mcp.server import create_mcp_server
from odoo_mcp.storage import Storage
from odoo_mcp.workflows.payroll.writes import (
    prepare_recalculate_draft_payslip,
    prepare_set_draft_payroll_input,
    validate_payroll_write_plan,
)

STAMP = datetime(2026, 9, 26, 8, tzinfo=UTC)


@dataclass
class Resolver:
    binding: ConnectionBinding

    async def resolve(self) -> ConnectionBinding:
        return self.binding


@dataclass
class PayrollWriteState:
    version: int = 19
    capability: bool = True
    attachment_type: bool = False
    permission_failure: bool = False
    unknown_failure: bool = False
    malformed_postcondition: bool = False
    block_mutation: bool = False
    next_input_id: int = 901
    create_count: int = 0
    update_count: int = 0
    delete_count: int = 0
    recalculate_count: int = 0
    mutation_started: asyncio.Event = field(default_factory=asyncio.Event)
    mutation_release: asyncio.Event = field(default_factory=asyncio.Event)
    inputs: list[PayslipInput] = field(default_factory=list)
    payslip: Payslip = field(init=False)
    structure: PayrollStructure = field(init=False)
    input_type: PayrollInputType = field(init=False)
    employee: PayrollEmployee = field(init=False)
    contract: PayrollContractSegment = field(init=False)

    def __post_init__(self) -> None:
        currency = RelatedRecord(id=10, name="USD")
        employee = RelatedRecord(id=501, name="Synthetic Employee")
        contract = RelatedRecord(id=301, name="Synthetic Contract")
        contract_source = "hr.contract" if self.version == 18 else "hr.version"
        source_state = "verify" if self.version == 18 else "draft"
        state = "waiting" if self.version == 18 else "draft"
        self.payslip = Payslip(
            id=101,
            name="Synthetic Payslip",
            reference="SYN/101",
            employee=employee,
            date_from=date(2026, 9, 1),
            date_to=date(2026, 9, 30),
            state=state,
            source_state=source_state,
            company_id=1,
            contract_segment=contract,
            contract_source=contract_source,
            structure=RelatedRecord(id=201, name="Monthly"),
            batch=None,
            credit_note=False,
            currency=currency,
            write_date=STAMP,
        )
        self.structure = PayrollStructure(
            id=201,
            input_line_type_ids=(401,),
            write_date=STAMP,
        )
        self.input_type = PayrollInputType(
            id=401,
            name="Synthetic Adjustment",
            code="SYN_ADJ",
            structure_ids=(201,),
            active=True,
            is_quantity=False,
            available_in_attachments=self.attachment_type,
            write_date=STAMP,
        )
        self.employee = PayrollEmployee(
            id=501,
            name="Synthetic Employee",
            active=True,
            company_id=1,
            write_date=STAMP,
        )
        self.contract = PayrollContractSegment(
            source_model=contract_source,
            id=301,
            employee=employee,
            company_id=1,
            active=True,
            source_status="open",
            revision_date=None,
            effective_start=date(2026, 1, 1),
            effective_end=date(2026, 12, 31),
            wage=Decimal("1000"),
            currency=currency,
            structure_type=None,
            resource_calendar=None,
            department=None,
            job=None,
            contract_type=None,
            write_date=STAMP,
        )


class PayrollWriteAdapter:
    def __init__(self, state: PayrollWriteState) -> None:
        self.state = state

    async def get_companies(self) -> list[Company]:
        return [
            Company(
                id=1,
                name="Synthetic Company",
                currency=RelatedRecord(id=10, name="USD"),
            )
        ]

    async def get_capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            edition="enterprise",
            version=self.state.version,
            transport="json2" if self.state.version == 19 else "json-rpc",
            modules={"base": True, "hr_payroll": self.state.capability},
        )

    async def get_payslips(
        self,
        company_id: int,
        filters: PayslipFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[Payslip]:
        del page
        items = (
            [self.state.payslip]
            if company_id == 1 and self.state.payslip.id in filters.payslip_ids
            else []
        )
        return PayrollPage[Payslip](items=items, total_count=len(items))

    async def get_payslip_inputs(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayslipInput]:
        del page
        items = [
            item
            for item in self.state.inputs
            if company_id == 1
            and item.payslip.id in filters.payslip_ids
            and (not filters.record_ids or item.id in filters.record_ids)
        ]
        return PayrollPage[PayslipInput](items=items, total_count=len(items))

    async def get_payroll_employees(
        self,
        company_id: int,
        employee_ids: tuple[int, ...],
        page: PayrollPageRequest,
    ) -> PayrollPage[PayrollEmployee]:
        del page
        items = (
            [self.state.employee]
            if company_id == 1 and self.state.employee.id in employee_ids
            else []
        )
        return PayrollPage[PayrollEmployee](items=items, total_count=len(items))

    async def get_payroll_contract_segments(
        self,
        company_id: int,
        filters: PayrollContractFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayrollContractSegment]:
        del page
        items = (
            [self.state.contract]
            if company_id == 1 and self.state.contract.employee.id in filters.employee_ids
            else []
        )
        return PayrollPage[PayrollContractSegment](items=items, total_count=len(items))

    async def get_payroll_structure(self, company_id: int, structure_id: int) -> PayrollStructure:
        if company_id != 1 or structure_id != self.state.structure.id:
            raise AssertionError("unexpected structure")
        return self.state.structure

    async def get_payroll_input_types(
        self,
        company_id: int,
        filters: PayrollInputTypeFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayrollInputType]:
        del page
        eligible = (
            company_id == 1
            and filters.structure_id == self.state.structure.id
            and not self.state.input_type.available_in_attachments
            and self.state.input_type.active
            and self.state.input_type.id in self.state.structure.input_line_type_ids
            and (not filters.input_type_ids or self.state.input_type.id in filters.input_type_ids)
        )
        items = [self.state.input_type] if eligible else []
        return PayrollPage[PayrollInputType](items=items, total_count=len(items))

    async def get_payslip_lines(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayslipLine]:
        del company_id, filters, page
        return PayrollPage[PayslipLine](items=[], total_count=0)

    async def get_payslip_worked_days(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayslipWorkedDay]:
        del company_id, filters, page
        return PayrollPage[PayslipWorkedDay](items=[], total_count=0)

    async def _before_mutation(self) -> None:
        self.state.mutation_started.set()
        if self.state.block_mutation:
            await self.state.mutation_release.wait()
        if self.state.permission_failure:
            raise PayrollWriteRejected(
                OdooMcpError(
                    ErrorCode.ODOO_PERMISSION_DENIED,
                    "Odoo denied the requested Payroll write.",
                    "Grant the technical user the required Odoo access and retry.",
                )
            )
        if self.state.unknown_failure:
            raise RuntimeError("synthetic uncertain transport outcome")

    async def create_draft_payslip_input(
        self, company_id: int, payload: DraftPayslipInputCreate
    ) -> PayslipInput:
        assert company_id == 1
        await self._before_mutation()
        self.state.create_count += 1
        created = PayslipInput(
            id=self.state.next_input_id,
            name=payload.description,
            payslip=RelatedRecord(id=payload.payslip_id, name="Synthetic Payslip"),
            sequence=10,
            input_type=RelatedRecord(id=payload.input_type_id, name=self.state.input_type.name),
            code=self.state.input_type.code,
            amount=payload.amount,
            contract_segment=RelatedRecord(id=301, name="Synthetic Contract"),
            contract_source=self.state.payslip.contract_source,
            write_date=STAMP + timedelta(minutes=self.state.create_count),
        )
        self.state.next_input_id += 1
        self.state.inputs.append(created)
        if self.state.malformed_postcondition:
            return created.model_copy(update={"amount": created.amount + Decimal("1")})
        return created

    async def update_draft_payslip_input(
        self,
        company_id: int,
        input_id: int,
        payload: DraftPayslipInputUpdate,
    ) -> PayslipInput:
        assert company_id == 1
        await self._before_mutation()
        self.state.update_count += 1
        current = next(item for item in self.state.inputs if item.id == input_id)
        updated = current.model_copy(
            update={
                "name": payload.description or current.name,
                "amount": payload.amount if payload.amount is not None else current.amount,
                "write_date": current.write_date + timedelta(minutes=1),
            }
        )
        self.state.inputs = [updated if item.id == input_id else item for item in self.state.inputs]
        return updated

    async def delete_draft_payslip_input(
        self, company_id: int, input_id: int
    ) -> DeletedPayslipInput:
        assert company_id == 1
        await self._before_mutation()
        self.state.delete_count += 1
        self.state.inputs = [item for item in self.state.inputs if item.id != input_id]
        return DeletedPayslipInput(id=input_id, payslip_id=101)

    async def recompute_draft_payslip(self, company_id: int, payslip_id: int) -> Payslip:
        assert company_id == 1 and payslip_id == 101
        await self._before_mutation()
        self.state.recalculate_count += 1
        source_state = "verify" if self.state.version == 18 else "draft"
        normalized = "waiting" if self.state.version == 18 else "draft"
        self.state.payslip = self.state.payslip.model_copy(
            update={
                "source_state": source_state,
                "state": normalized,
                "write_date": self.state.payslip.write_date + timedelta(minutes=1),
            }
        )
        return self.state.payslip

    async def close(self) -> None:
        return None


def _binding(
    connection: OdooConnectionSettings,
    *,
    permissions: frozenset[str] = frozenset({"payroll_draft_write"}),
) -> ConnectionBinding:
    return ConnectionBinding(
        profile=DeploymentProfile.LOCAL,
        tenant_id="tenant-payroll-write",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=permissions,
        connection=connection,
    )


def _server(
    connection: OdooConnectionSettings,
    path: Path,
    state: PayrollWriteState,
    *,
    permissions: frozenset[str] = frozenset({"payroll_draft_write"}),
) -> tuple[object, Storage]:
    storage = Storage.open(path)

    async def factory(_connection: object) -> OdooAdapter:
        return PayrollWriteAdapter(state)  # type: ignore[return-value]

    server = create_mcp_server(
        Resolver(_binding(connection, permissions=permissions)),
        adapter_factory=factory,
        storage=storage,
    )
    return server, storage


def _set_arguments(
    *,
    dry_run: bool,
    key: str | None = None,
    input_id: int | None = None,
    input_type_id: int | None = 401,
    description: str | None = "Do not retain: secret adjustment",
    amount: str | None = "40.125",
) -> dict[str, object]:
    result: dict[str, object] = {
        "company_id": 1,
        "payslip_id": 101,
        "dry_run": dry_run,
        "idempotency_key": key,
    }
    if input_id is not None:
        result["input_id"] = input_id
    if input_type_id is not None:
        result["input_type_id"] = input_type_id
    if description is not None:
        result["description"] = description
    if amount is not None:
        result["amount"] = amount
    return result


@pytest.mark.parametrize("bad_amount", ["NaN", "Infinity", "1000000000001", "1.1234567"])
def test_set_schema_rejects_invalid_amounts_and_cross_operation_fields(
    bad_amount: str,
) -> None:
    with pytest.raises(ValidationError):
        SetDraftPayrollInput(
            company_id=1,
            payslip_id=101,
            input_type_id=401,
            description="Synthetic",
            amount=bad_amount,
        )
    with pytest.raises(ValidationError):
        SetDraftPayrollInput(
            company_id=1,
            payslip_id=101,
            input_id=901,
            input_type_id=401,
            description="Synthetic",
        )


async def test_preview_without_type_needs_input_and_never_mutates() -> None:
    state = PayrollWriteState()
    request = SetDraftPayrollInput(
        company_id=1,
        payslip_id=101,
        description=" Synthetic adjustment ",
        amount="0",
    )

    _, prepared = await prepare_set_draft_payroll_input(
        PayrollWriteAdapter(state),
        request,
        19,  # type: ignore[arg-type]
    )

    assert prepared.needs_input is True
    assert prepared.proposed_action["eligible_input_types"] == [
        {
            "id": 401,
            "name": "Synthetic Adjustment",
            "code": "SYN_ADJ",
            "is_quantity": False,
            "structure_scope": [201],
        }
    ]
    assert state.create_count == 0


@pytest.mark.parametrize(("version", "accepted"), [(18, True), (19, True)])
async def test_version_specific_editable_sources_and_structure_anchor_race(
    version: int, accepted: bool
) -> None:
    state = PayrollWriteState(version=version)
    request = SetDraftPayrollInput(
        company_id=1,
        payslip_id=101,
        input_type_id=401,
        description="Synthetic",
        amount="1",
    )
    plan, _ = await prepare_set_draft_payroll_input(
        PayrollWriteAdapter(state),
        request,
        version,  # type: ignore[arg-type]
    )
    assert accepted
    state.structure = state.structure.model_copy(
        update={"write_date": state.structure.write_date + timedelta(seconds=1)}
    )
    with pytest.raises(OdooMcpError) as caught:
        await validate_payroll_write_plan(
            PayrollWriteAdapter(state),
            plan,  # type: ignore[arg-type]
        )
    assert caught.value.code is ErrorCode.ODOO_STATE_CONFLICT


async def test_noneditable_and_attachment_owned_inputs_fail_closed() -> None:
    state = PayrollWriteState(attachment_type=True)
    request = SetDraftPayrollInput(
        company_id=1,
        payslip_id=101,
        input_type_id=401,
        description="Synthetic",
        amount="1",
    )
    with pytest.raises(OdooMcpError) as type_error:
        await prepare_set_draft_payroll_input(
            PayrollWriteAdapter(state),
            request,
            19,  # type: ignore[arg-type]
        )
    assert type_error.value.code is ErrorCode.PAYROLL_INPUT_TYPE_NOT_ALLOWED

    state = PayrollWriteState()
    state.payslip = state.payslip.model_copy(update={"source_state": "done", "state": "done"})
    with pytest.raises(OdooMcpError) as state_error:
        await prepare_recalculate_draft_payslip(
            PayrollWriteAdapter(state),
            RecalculateDraftPayslip(company_id=1, payslip_id=101),
            19,
        )
    assert state_error.value.code is ErrorCode.PAYROLL_STATE_NOT_EDITABLE


async def test_mcp_create_update_remove_and_recalculate_are_separate_and_replay_safe(
    connection: OdooConnectionSettings,
    tmp_path: Path,
) -> None:
    path = tmp_path / "payroll-writes.sqlite3"
    state = PayrollWriteState()
    server, storage = _server(connection, path, state)

    async with Client(server) as client:  # type: ignore[arg-type]
        preview = await client.call_tool(
            "set_draft_payroll_input",
            _set_arguments(dry_run=True, input_type_id=None),
        )
        created = await client.call_tool(
            "set_draft_payroll_input",
            _set_arguments(dry_run=False, key="secret-key-create"),
        )
        replay = await client.call_tool(
            "set_draft_payroll_input",
            _set_arguments(dry_run=False, key="secret-key-create"),
        )
        mismatch = await client.call_tool(
            "set_draft_payroll_input",
            _set_arguments(dry_run=False, key="secret-key-create", amount="41.125"),
        )
        updated = await client.call_tool(
            "set_draft_payroll_input",
            _set_arguments(
                dry_run=False,
                key="secret-key-update",
                input_id=901,
                input_type_id=None,
                description=None,
                amount="42.125",
            ),
        )
        removed = await client.call_tool(
            "remove_draft_payroll_input",
            {
                "company_id": 1,
                "payslip_id": 101,
                "input_id": 901,
                "dry_run": False,
                "idempotency_key": "secret-key-remove",
            },
        )
        recalculated = await client.call_tool(
            "recalculate_draft_payslip",
            {
                "company_id": 1,
                "payslip_id": 101,
                "dry_run": False,
                "idempotency_key": "secret-key-recalculate",
            },
        )

    assert preview.structured_content is not None
    assert preview.structured_content["status"] == "needs_input"
    assert created.structured_content is not None
    assert created.structured_content["status"] == "succeeded"
    assert created.structured_content["material_effects"]["resulting_input"]["amount"] == "40.125"
    assert replay.structured_content is not None
    assert replay.structured_content["status"] == "succeeded"
    assert "resulting_input" not in replay.structured_content["material_effects"]
    assert mismatch.structured_content is not None
    assert mismatch.structured_content["error_code"] == "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"
    assert updated.structured_content is not None
    assert updated.structured_content["material_effects"]["resulting_input"]["amount"] == "42.125"
    assert removed.structured_content is not None
    assert removed.structured_content["material_effects"]["completion"] == "deleted"
    assert recalculated.structured_content is not None
    assert recalculated.structured_content["material_effects"]["normalized_state"] == "draft"
    assert state.create_count == state.update_count == state.delete_count == 1
    assert state.recalculate_count == 1

    audits = storage.audit.list_for_tenant("tenant-payroll-write")
    assert audits
    assert all(event.module == "payroll" for event in audits)
    assert storage.audit.verify_chain("tenant-payroll-write").entry_count == len(audits)
    with storage.database.transaction() as database:
        counts = {
            "proposals": int(database.execute("SELECT count(*) FROM proposals").fetchone()[0]),
            "artifacts": int(database.execute("SELECT count(*) FROM artifacts").fetchone()[0]),
            "capabilities_cache": int(
                database.execute("SELECT count(*) FROM capabilities_cache").fetchone()[0]
            ),
            "idempotency_keys": int(
                database.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0]
            ),
        }
    assert counts == {
        "proposals": 0,
        "artifacts": 0,
        "capabilities_cache": 0,
        "idempotency_keys": 4,
    }
    retained = path.read_text(encoding="utf-8", errors="ignore")
    for forbidden in (
        "Do not retain: secret adjustment",
        "40.125",
        "42.125",
    ):
        assert forbidden not in retained
    audit_text = repr(audits)
    for idempotency_key in (
        "secret-key-create",
        "secret-key-update",
        "secret-key-remove",
        "secret-key-recalculate",
    ):
        assert idempotency_key not in audit_text


async def test_permission_capability_and_explicit_execution_gate_before_mutation(
    connection: OdooConnectionSettings,
    tmp_path: Path,
) -> None:
    state = PayrollWriteState()
    unauthorized, _ = _server(
        connection,
        tmp_path / "unauthorized.sqlite3",
        state,
        permissions=frozenset({"payroll_read"}),
    )
    async with Client(unauthorized) as client:  # type: ignore[arg-type]
        denied = await client.call_tool(
            "set_draft_payroll_input",
            _set_arguments(dry_run=False, key="denied"),
        )
    assert denied.structured_content is not None
    assert denied.structured_content["error_code"] == "ODOO_AUTH_FAILED"

    state.capability = False
    unavailable, _ = _server(connection, tmp_path / "unavailable.sqlite3", state)
    async with Client(unavailable) as client:  # type: ignore[arg-type]
        capability = await client.call_tool(
            "set_draft_payroll_input",
            _set_arguments(dry_run=False, key="capability"),
        )
    assert capability.structured_content is not None
    assert capability.structured_content["error_code"] == "CAPABILITY_NOT_AVAILABLE"

    state.capability = True
    explicit, _ = _server(connection, tmp_path / "explicit.sqlite3", state)
    async with Client(explicit) as client:  # type: ignore[arg-type]
        missing_key = await client.call_tool(
            "set_draft_payroll_input",
            _set_arguments(dry_run=False, key=None),
        )
    assert missing_key.structured_content is not None
    assert missing_key.structured_content["error_code"] == "EXECUTION_NOT_EXPLICIT"
    assert state.create_count == 0


async def test_completed_write_replays_after_storage_and_server_restart(
    connection: OdooConnectionSettings,
    tmp_path: Path,
) -> None:
    path = tmp_path / "restart.sqlite3"
    state = PayrollWriteState()
    first_server, first_storage = _server(connection, path, state)
    arguments = _set_arguments(dry_run=False, key="restart-create")
    async with Client(first_server) as client:  # type: ignore[arg-type]
        first = await client.call_tool("set_draft_payroll_input", arguments)
    first_storage.close()

    restarted_server, restarted_storage = _server(connection, path, state)
    async with Client(restarted_server) as client:  # type: ignore[arg-type]
        replay = await client.call_tool("set_draft_payroll_input", arguments)
    restarted_storage.close()

    assert first.structured_content is not None
    assert first.structured_content["status"] == "succeeded"
    assert replay.structured_content is not None
    assert replay.structured_content["status"] == "succeeded"
    assert replay.structured_content["material_effects"] == {
        "operation": "create",
        "payslip_id": 101,
        "input_id": 901,
        "result_input_id": 901,
        "before_source_state": "draft",
        "after_source_state": "draft",
    }
    assert state.create_count == 1


@pytest.mark.parametrize(
    ("failure", "expected_outcome", "expected_state"),
    [
        ("permission", "known", "failed"),
        ("unknown", "unknown", "unknown"),
        ("postcondition", "unknown", "unknown"),
    ],
)
async def test_acl_transport_and_postcondition_failures_are_finalized_safely(
    connection: OdooConnectionSettings,
    tmp_path: Path,
    failure: str,
    expected_outcome: str,
    expected_state: str,
) -> None:
    state = PayrollWriteState(
        permission_failure=failure == "permission",
        unknown_failure=failure == "unknown",
        malformed_postcondition=failure == "postcondition",
    )
    server, storage = _server(connection, tmp_path / f"{failure}.sqlite3", state)
    arguments = _set_arguments(dry_run=False, key=f"key-{failure}")
    async with Client(server) as client:  # type: ignore[arg-type]
        result = await client.call_tool("set_draft_payroll_input", arguments)
        replay = await client.call_tool("set_draft_payroll_input", arguments)

    assert result.structured_content is not None
    assert result.structured_content["outcome"] == expected_outcome
    assert replay.structured_content is not None
    assert replay.structured_content["outcome"] == expected_outcome
    decision = storage.idempotency.reserve(
        "tenant-payroll-write",
        1,
        "set_draft_payroll_input",
        f"key-{failure}",
        {
            "payslip_id": 101,
            "input_type_id": 401,
            "description": "Do not retain: secret adjustment",
            "amount": "40.125",
        },
        "inspection",
    )
    assert decision.state.value == expected_state


async def test_payslip_lock_rejects_concurrent_different_keys(
    connection: OdooConnectionSettings,
    tmp_path: Path,
) -> None:
    state = PayrollWriteState(block_mutation=True)
    server, _ = _server(connection, tmp_path / "concurrent.sqlite3", state)
    async with Client(server) as first_client, Client(server) as second_client:  # type: ignore[arg-type]
        first = asyncio.create_task(
            first_client.call_tool(
                "set_draft_payroll_input",
                _set_arguments(dry_run=False, key="concurrent-first"),
            )
        )
        await asyncio.wait_for(state.mutation_started.wait(), timeout=2)
        second = await second_client.call_tool(
            "set_draft_payroll_input",
            _set_arguments(dry_run=False, key="concurrent-second"),
        )
        state.mutation_release.set()
        completed = await first

    assert second.structured_content is not None
    assert second.structured_content["error_code"] == "CONCURRENT_OPERATION_IN_PROGRESS"
    assert completed.structured_content is not None
    assert completed.structured_content["status"] == "succeeded"
    assert state.create_count == 1


async def test_cancellation_after_dispatch_finalizes_unknown_and_releases_lock(
    connection: OdooConnectionSettings,
    tmp_path: Path,
) -> None:
    state = PayrollWriteState(block_mutation=True)
    server, storage = _server(connection, tmp_path / "cancelled.sqlite3", state)
    arguments = _set_arguments(dry_run=False, key="cancelled-create")
    async with Client(server) as client:  # type: ignore[arg-type]
        task = asyncio.create_task(client.call_tool("set_draft_payroll_input", arguments))
        await asyncio.wait_for(state.mutation_started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        replay = await client.call_tool("set_draft_payroll_input", arguments)

    assert replay.structured_content is not None
    assert replay.structured_content["status"] == "failed"
    assert replay.structured_content["outcome"] == "unknown"
    assert replay.structured_content["error_code"] == "UNKNOWN_ERROR"
    assert state.create_count == 0
    decision = storage.idempotency.reserve(
        "tenant-payroll-write",
        1,
        "set_draft_payroll_input",
        "cancelled-create",
        {
            "payslip_id": 101,
            "input_type_id": 401,
            "description": "Do not retain: secret adjustment",
            "amount": "40.125",
        },
        "inspection",
    )
    assert decision.state.value == "unknown"
