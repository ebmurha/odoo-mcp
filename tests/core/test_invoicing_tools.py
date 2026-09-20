from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mcp import Client

from odoo_mcp.adapters.accounting import (
    Account,
    InvoiceDraft,
    InvoiceEffect,
    PageRequest,
    Partner,
    ReadFilters,
    RecordPage,
    RelatedRecord,
)
from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.connections import ConnectionBinding
from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.server import create_mcp_server
from odoo_mcp.storage import Storage


@dataclass
class Resolver:
    binding: ConnectionBinding

    async def resolve(self) -> ConnectionBinding:
        return self.binding


@dataclass
class InvoiceState:
    create_count: int = 0
    failure: str | None = None


def _effect() -> InvoiceEffect:
    return InvoiceEffect(
        id=901,
        name="INV/901",
        move_type="out_invoice",
        state="draft",
        company_id=1,
        partner=RelatedRecord(id=20, name="Synthetic Customer"),
        invoice_date=date(2026, 9, 20),
        due_date=date(2026, 10, 20),
        journal=RelatedRecord(id=30, name="Sales"),
        fiscal_position=RelatedRecord(id=31, name="Domestic"),
        currency=RelatedRecord(id=40, name="USD"),
        amount_untaxed=Decimal("20"),
        amount_tax=Decimal("3.2"),
        amount_total=Decimal("23.2"),
        amount_residual=Decimal("23.2"),
        payment_state="not_paid",
    )


class InvoiceAdapter:
    def __init__(self, state: InvoiceState, *, account_available: bool = True) -> None:
        self.state = state
        self.account_available = account_available
        self.closed = False

    async def get_companies(self) -> list[Company]:
        return [
            Company(
                id=1,
                name="Synthetic Company",
                currency=RelatedRecord(id=1, name="KES"),
            )
        ]

    async def get_capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            edition="enterprise",
            version=19,
            transport="json2",
            modules={"base": True, "account": self.account_available},
        )

    async def get_partners(
        self, company_id: int, filters: ReadFilters, *, page: PageRequest
    ) -> RecordPage[Partner]:
        return RecordPage(items=[Partner(id=20, name="Synthetic Customer", customer_rank=1)])

    async def get_account_accounts(
        self, company_id: int, filters: ReadFilters, *, page: PageRequest
    ) -> RecordPage[Account]:
        return RecordPage(
            items=[
                Account(
                    id=70,
                    code="4000",
                    name="Revenue",
                    account_type="income",
                    company_ids=(1,),
                    reconcile=False,
                )
            ]
        )

    async def create_draft_invoice(self, draft: InvoiceDraft) -> InvoiceEffect:
        self.state.create_count += 1
        if self.state.failure == "permission":
            raise OdooMcpError(
                ErrorCode.ODOO_PERMISSION_DENIED,
                "Odoo denied the requested operation.",
                "Correct Odoo access and retry.",
            )
        if self.state.failure == "unknown":
            raise RuntimeError("synthetic uncertain transport outcome")
        return _effect()

    async def close(self) -> None:
        self.closed = True


def _binding(
    connection: OdooConnectionSettings,
    permissions: frozenset[str] = frozenset({"accounting_propose"}),
) -> ConnectionBinding:
    return ConnectionBinding(
        profile=DeploymentProfile.LOCAL,
        tenant_id="tenant-invoice",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=permissions,
        connection=connection,
    )


def _arguments(*, dry_run: bool, key: str | None = None) -> dict[str, object]:
    return {
        "company_id": 1,
        "partner_id": 20,
        "invoice_date": "2026-09-20",
        "lines": [
            {
                "description": "Consulting",
                "quantity": "2",
                "unit_price": "10",
                "account_id": 70,
            }
        ],
        "dry_run": dry_run,
        "idempotency_key": key,
    }


async def test_invoice_preview_execute_and_restart_replay_are_safe(
    connection: OdooConnectionSettings, tmp_path
) -> None:
    path = tmp_path / "invoice.sqlite3"
    storage = Storage.open(path)
    state = InvoiceState()
    adapters: list[InvoiceAdapter] = []

    async def factory(_connection: object) -> OdooAdapter:
        adapter = InvoiceAdapter(state)
        adapters.append(adapter)
        return adapter

    server = create_mcp_server(
        Resolver(_binding(connection)), adapter_factory=factory, storage=storage
    )
    async with Client(server) as client:
        preview = await client.call_tool("create_customer_invoice", _arguments(dry_run=True))
        executed = await client.call_tool(
            "create_customer_invoice", _arguments(dry_run=False, key="invoice-901")
        )

    assert preview.structured_content is not None
    assert preview.structured_content["status"] == "preview"
    assert preview.structured_content["material_effects"]["deferred_fields"] == [
        "journal_id",
        "fiscal_position_id",
        "currency_id",
        "taxes",
        "total",
    ]
    assert executed.structured_content is not None
    assert executed.structured_content["status"] == "succeeded"
    assert executed.structured_content["material_effects"]["journal"]["id"] == 30
    assert state.create_count == 1

    reopened = Storage.open(path)
    restarted = create_mcp_server(
        Resolver(_binding(connection)), adapter_factory=factory, storage=reopened
    )
    async with Client(restarted) as client:
        replay = await client.call_tool(
            "create_customer_invoice", _arguments(dry_run=False, key="invoice-901")
        )

    assert replay.structured_content is not None
    assert replay.structured_content == executed.structured_content
    assert state.create_count == 1
    assert all(adapter.closed for adapter in adapters)


async def test_invoice_capability_denial_prevents_reference_reads_and_mutation(
    connection: OdooConnectionSettings, tmp_path
) -> None:
    state = InvoiceState()
    adapters: list[InvoiceAdapter] = []

    async def factory(_connection: object) -> OdooAdapter:
        adapter = InvoiceAdapter(state, account_available=False)
        adapters.append(adapter)
        return adapter

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=Storage.open(tmp_path / "denied.sqlite3"),
    )
    async with Client(server) as client:
        result = await client.call_tool("create_customer_invoice", _arguments(dry_run=True))

    assert result.structured_content is not None
    assert result.structured_content["status"] == "failed"
    assert result.structured_content["error_code"] == "CAPABILITY_NOT_AVAILABLE"
    assert state.create_count == 0


async def test_invoice_execution_distinguishes_known_denial_from_unknown_outcome(
    connection: OdooConnectionSettings, tmp_path
) -> None:
    for failure, expected_outcome in (("permission", "known"), ("unknown", "unknown")):
        state = InvoiceState(failure=failure)

        async def factory(_connection: object, current_state: InvoiceState = state) -> OdooAdapter:
            return InvoiceAdapter(current_state)

        storage = Storage.open(tmp_path / f"{failure}.sqlite3")
        server = create_mcp_server(
            Resolver(_binding(connection)), adapter_factory=factory, storage=storage
        )
        async with Client(server) as client:
            result = await client.call_tool(
                "create_customer_invoice",
                _arguments(dry_run=False, key=f"invoice-{failure}"),
            )

        assert result.structured_content is not None
        assert result.structured_content["status"] == "failed"
        assert result.structured_content["outcome"] == expected_outcome
        assert state.create_count == 1
        assert [item.final_status for item in storage.audit.list_for_tenant("tenant-invoice")] == [
            "attempted",
            "failed" if failure == "permission" else "unknown",
        ]
