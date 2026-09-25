from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from mcp import Client

from odoo_mcp.adapters.accounting import RelatedRecord
from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.connections import ConnectionBinding
from odoo_mcp.adapters.payroll import PayrollPage, PayrollPageRequest, Payslip, PayslipFilters
from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.payroll import _audit_input
from odoo_mcp.mcp.payroll_schemas import (
    GetAttendanceSummaryInput,
    GetEmployeePayrollContextInput,
    GetPayrollBatchInput,
    GetPayslipInput,
    ListPayrollPeriodsInput,
    ListPayslipsInput,
    ListSalaryRulesInput,
)
from odoo_mcp.mcp.registry import get_tool_definition
from odoo_mcp.mcp.server import create_mcp_server
from odoo_mcp.storage import Storage


@dataclass
class Resolver:
    binding: ConnectionBinding

    async def resolve(self) -> ConnectionBinding:
        return self.binding


class PayrollToolAdapter:
    def __init__(self, *, capability: bool = True, fail: bool = False) -> None:
        self.capability = capability
        self.fail = fail
        self.closed = False
        self.reads = 0

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
            version=19,
            transport="json2",
            modules={"base": True, "hr_payroll": self.capability},
        )

    async def get_payslips(
        self,
        company_id: int,
        filters: PayslipFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[Payslip]:
        del filters, page
        self.reads += 1
        if self.fail:
            raise OdooMcpError(
                ErrorCode.ODOO_PERMISSION_DENIED,
                "Odoo denied access to the requested operation.",
                "Grant the technical user the required Odoo access and retry.",
            )
        return PayrollPage[Payslip](
            items=[
                Payslip(
                    id=301,
                    name="Synthetic Payslip",
                    reference="PS-301",
                    employee=RelatedRecord(id=101, name="Synthetic Employee"),
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                    state="done",
                    source_state="validated",
                    company_id=company_id,
                    contract_segment=RelatedRecord(id=201, name="Synthetic Contract"),
                    contract_source="hr.version",
                    structure=RelatedRecord(id=60, name="Monthly"),
                    batch=RelatedRecord(id=50, name="Synthetic Batch"),
                    credit_note=False,
                    currency=RelatedRecord(id=10, name="USD"),
                    write_date=datetime(2026, 9, 25, 10, tzinfo=UTC),
                )
            ],
            total_count=1,
            next_cursor=None,
        )

    async def close(self) -> None:
        self.closed = True


def _binding(
    connection: OdooConnectionSettings,
    *,
    permissions: frozenset[str] = frozenset({"payroll_read"}),
) -> ConnectionBinding:
    return ConnectionBinding(
        profile=DeploymentProfile.LOCAL,
        tenant_id="tenant-payroll",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=permissions,
        connection=connection,
    )


async def test_payroll_tool_records_only_metadata_and_creates_no_other_state(
    connection: OdooConnectionSettings,
    tmp_path: Path,
) -> None:
    path = tmp_path / "payroll.sqlite3"
    storage = Storage.open(path)
    adapter = PayrollToolAdapter()

    async def factory(_connection: object) -> OdooAdapter:
        return adapter  # type: ignore[return-value]

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "list_payroll_periods",
            {
                "company_id": 1,
                "window_start": "2026-01-01",
                "window_end": "2026-01-31",
            },
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "ok"
    assert result.structured_content["items"][0]["non_cancelled_payslip_count"] == 1
    assert "artifact_markdown" not in result.structured_content
    assert adapter.closed is True

    audits = storage.audit.list_for_tenant("tenant-payroll")
    assert len(audits) == 1
    audit = audits[0]
    assert audit.module == "payroll"
    assert audit.input_payload == {
        "batch_filter_count": 0,
        "comparison_requested": False,
        "continuation_requested": False,
        "employee_filter_count": 0,
        "history_requested": False,
        "normalized_states": ["waiting", "done", "paid"],
        "payslip_filter_count": 0,
        "period_filter_count": 1,
        "query_kind": "list_payroll_periods",
    }
    assert audit.actual_result == {
        "error_code": None,
        "final_status": "succeeded",
        "has_more": False,
        "item_count": 1,
        "limitation_codes": [],
    }
    assert audit.affected_odoo_records == ()
    assert audit.proposed_action is None
    assert storage.audit.verify_chain("tenant-payroll").entry_count == 1
    serialized = json.dumps(
        {"input": audit.input_payload, "result": audit.actual_result},
        sort_keys=True,
    )
    for forbidden in (
        "2026-01-01",
        "2026-01-31",
        "301",
        "101",
        "Synthetic Employee",
        "Synthetic Payslip",
    ):
        assert forbidden not in serialized

    with sqlite3.connect(path) as database:
        counts = {
            table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "proposals",
                "artifacts",
                "idempotency_keys",
                "capabilities_cache",
            )
        }
    assert counts == {
        "proposals": 0,
        "artifacts": 0,
        "idempotency_keys": 0,
        "capabilities_cache": 0,
    }


async def test_invalid_payroll_input_is_structured_audited_and_skips_adapter(
    connection: OdooConnectionSettings,
    tmp_path: Path,
) -> None:
    storage = Storage.open(tmp_path / "invalid.sqlite3")
    called = False

    async def factory(_connection: object) -> OdooAdapter:
        nonlocal called
        called = True
        return PayrollToolAdapter()  # type: ignore[return-value]

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        result = await client.call_tool("list_payslips", {"company_id": 1})

    assert called is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "failed"
    assert result.structured_content["error_code"] == "INVALID_INPUT"
    audit = storage.audit.list_for_tenant("tenant-payroll")[0]
    assert audit.error_code == "INVALID_INPUT"
    assert audit.input_payload["batch_filter_count"] == 0
    assert audit.input_payload["period_filter_count"] == 0


@pytest.mark.parametrize(
    ("permissions", "company_id", "capability", "expected", "adapter_created"),
    [
        (frozenset(), 1, True, "ODOO_AUTH_FAILED", False),
        (frozenset({"payroll_read"}), 3, True, "COMPANY_NOT_FOUND", False),
        (frozenset({"payroll_read"}), 1, False, "CAPABILITY_NOT_AVAILABLE", True),
    ],
)
async def test_payroll_authorization_company_and_capability_gates_precede_reads(
    connection: OdooConnectionSettings,
    tmp_path: Path,
    permissions: frozenset[str],
    company_id: int,
    capability: bool,
    expected: str,
    adapter_created: bool,
) -> None:
    storage = Storage.open(tmp_path / f"{expected}.sqlite3")
    adapter = PayrollToolAdapter(capability=capability)
    created = False

    async def factory(_connection: object) -> OdooAdapter:
        nonlocal created
        created = True
        return adapter  # type: ignore[return-value]

    server = create_mcp_server(
        Resolver(_binding(connection, permissions=permissions)),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "list_payroll_periods",
            {
                "company_id": company_id,
                "window_start": "2026-01-01",
                "window_end": "2026-01-31",
            },
        )

    assert result.structured_content is not None
    assert result.structured_content["error_code"] == expected
    assert created is adapter_created
    assert adapter.reads == 0
    assert storage.audit.list_for_tenant("tenant-payroll")[0].error_code == expected


async def test_odoo_acl_denial_is_safe_audited_and_contains_no_payroll_payload(
    connection: OdooConnectionSettings,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = Storage.open(tmp_path / "acl.sqlite3")
    adapter = PayrollToolAdapter(fail=True)

    async def factory(_connection: object) -> OdooAdapter:
        return adapter  # type: ignore[return-value]

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=storage,
    )
    with caplog.at_level("DEBUG"):
        async with Client(server) as client:
            result = await client.call_tool(
                "list_payroll_periods",
                {
                    "company_id": 1,
                    "window_start": "2026-01-01",
                    "window_end": "2026-01-31",
                },
            )

    assert result.structured_content is not None
    assert result.structured_content["error_code"] == "ODOO_PERMISSION_DENIED"
    assert "Synthetic Employee" not in str(result.structured_content)
    assert "Synthetic Employee" not in caplog.text
    audit = storage.audit.list_for_tenant("tenant-payroll")[0]
    assert audit.actual_result == {
        "error_code": "ODOO_PERMISSION_DENIED",
        "final_status": "failed",
        "has_more": False,
        "item_count": 0,
        "limitation_codes": [],
    }
    assert audit.error_message == "Operation failed with ODOO_PERMISSION_DENIED."


async def test_capability_discovery_reports_only_registered_payroll_tools(
    connection: OdooConnectionSettings,
) -> None:
    adapter = PayrollToolAdapter()

    async def factory(_connection: object) -> OdooAdapter:
        return adapter  # type: ignore[return-value]

    server = create_mcp_server(
        Resolver(
            _binding(
                connection,
                permissions=frozenset({"core_read", "payroll_read"}),
            )
        ),
        adapter_factory=factory,
    )
    async with Client(server) as client:
        result = await client.call_tool("get_erp_capabilities", {})

    assert result.structured_content is not None
    assert result.structured_content["available_tools"] == sorted(
        [
            "get_erp_capabilities",
            "list_payroll_periods",
            "get_payroll_batch",
            "list_payslips",
            "get_payslip",
            "get_employee_payroll_context",
            "list_salary_rules",
            "get_attendance_summary",
        ]
    )


def test_every_payroll_read_uses_identifier_free_audit_input_projection() -> None:
    requests = {
        "list_payroll_periods": ListPayrollPeriodsInput(
            company_id=1,
            window_start=date(2042, 2, 1),
            window_end=date(2042, 2, 28),
            cursor="private-period-cursor",
        ),
        "get_payroll_batch": GetPayrollBatchInput(
            company_id=1,
            batch_id=987_654,
            cursor="private-batch-cursor",
        ),
        "list_payslips": ListPayslipsInput(
            company_id=1,
            batch_id=987_654,
            period_start=date(2042, 2, 1),
            period_end=date(2042, 2, 28),
            employee_ids=(456_789,),
            cursor="private-payslip-cursor",
        ),
        "get_payslip": GetPayslipInput(company_id=1, payslip_id=345_678),
        "get_employee_payroll_context": GetEmployeePayrollContextInput(
            company_id=1,
            employee_id=456_789,
            period_start=date(2042, 2, 1),
            period_end=date(2042, 2, 28),
        ),
        "list_salary_rules": ListSalaryRulesInput(
            company_id=1,
            batch_id=987_654,
            employee_ids=(456_789,),
            cursor="private-rule-cursor",
        ),
        "get_attendance_summary": GetAttendanceSummaryInput(
            company_id=1,
            employee_ids=(456_789,),
            period_start=date(2042, 2, 1),
            period_end=date(2042, 2, 28),
            cursor="private-work-entry-cursor",
        ),
    }

    for name, request in requests.items():
        projection = _audit_input(get_tool_definition(name), request)
        encoded = json.dumps(projection, sort_keys=True)
        assert projection["query_kind"] == name
        assert projection["comparison_requested"] is False
        assert projection["history_requested"] is False
        for forbidden in (
            "2042-02-01",
            "2042-02-28",
            "987654",
            "456789",
            "345678",
            "private-",
        ):
            assert forbidden not in encoded
