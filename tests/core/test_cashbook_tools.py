from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mcp import Client

from odoo_mcp.adapters.accounting import (
    AccountMoveLine,
    BankStatementLine,
    Currency,
    DatePeriod,
    Journal,
    PageRequest,
    ReadFilters,
    RecordPage,
    RelatedRecord,
)
from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.connections import ConnectionBinding
from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings
from odoo_mcp.mcp.server import create_mcp_server
from odoo_mcp.storage import Storage


@dataclass
class Resolver:
    binding: ConnectionBinding

    async def resolve(self) -> ConnectionBinding:
        return self.binding


class BankAdapter:
    def __init__(
        self,
        *,
        accountant_available: bool = True,
        candidate_amount: str = "-100",
    ) -> None:
        self.accountant_available = accountant_available
        self.candidate_amount = candidate_amount
        self.closed = False
        self.read_calls: list[str] = []

    async def get_companies(self) -> list[Company]:
        self.read_calls.append("companies")
        return [
            Company(
                id=1,
                name="Synthetic Company",
                currency=RelatedRecord(id=1, name="KES"),
            )
        ]

    async def get_capabilities(self) -> CapabilitySnapshot:
        self.read_calls.append("capabilities")
        return CapabilitySnapshot(
            edition="enterprise",
            version=19,
            transport="json2",
            modules={
                "base": True,
                "account": True,
                "account_accountant": self.accountant_available,
            },
        )

    async def get_journals(self, company_id: int, *, page: PageRequest) -> RecordPage[Journal]:
        self.read_calls.append("journals")
        return RecordPage(
            items=[
                Journal(
                    id=10,
                    name="Synthetic Bank",
                    code="BNK",
                    journal_type="bank",
                    company_id=company_id,
                )
            ]
        )

    async def get_currencies(
        self,
        company_id: int,
        currency_ids: tuple[int, ...],
        *,
        page: PageRequest,
    ) -> RecordPage[Currency]:
        self.read_calls.append("currencies")
        return RecordPage(
            items=[
                Currency(id=identifier, name="KES", rounding=Decimal("0.01"))
                for identifier in currency_ids
            ]
        )

    async def get_bank_statement_lines(
        self,
        company_id: int,
        period: DatePeriod,
        journal_id: int | None,
        *,
        page: PageRequest,
    ) -> RecordPage[BankStatementLine]:
        self.read_calls.append("statement_lines")
        return RecordPage(
            items=[
                BankStatementLine(
                    id=1001,
                    date=date(2026, 4, 10),
                    payment_reference="Invoice 42",
                    amount=Decimal("100"),
                    partner=RelatedRecord(id=7, name="Synthetic Partner"),
                    journal=RelatedRecord(id=10, name="Synthetic Bank"),
                    company_id=company_id,
                    reconciled=False,
                )
            ]
        )

    async def get_account_move_lines(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[AccountMoveLine]:
        self.read_calls.append("move_lines")
        value = Decimal(self.candidate_amount)
        journal_id = 10 if any(clause.field == "journal_id" for clause in filters.clauses) else 20
        return RecordPage(
            items=[
                AccountMoveLine(
                    id=2001,
                    move=RelatedRecord(id=3001, name="INV/42"),
                    account=RelatedRecord(id=400, name="Receivable"),
                    journal=RelatedRecord(
                        id=journal_id,
                        name="Synthetic Bank" if journal_id == 10 else "Sales",
                    ),
                    partner=RelatedRecord(id=7, name="Synthetic Partner"),
                    company_id=company_id,
                    currency=RelatedRecord(id=1, name="KES"),
                    date=date(2026, 4, 10),
                    label="invoice 42",
                    debit=max(value, Decimal("0")),
                    credit=max(-value, Decimal("0")),
                    balance=value,
                    amount_currency=value,
                    residual=value,
                    residual_currency=value,
                    reconciled=False,
                    analytic_distribution={},
                )
            ]
        )

    async def close(self) -> None:
        self.closed = True


def _binding(
    connection: OdooConnectionSettings,
    permissions: frozenset[str] = frozenset({"core_read", "accounting_read", "accounting_propose"}),
) -> ConnectionBinding:
    return ConnectionBinding(
        profile=DeploymentProfile.LOCAL,
        tenant_id="tenant-bank",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=permissions,
        connection=connection,
    )


async def test_cashbook_and_unmatched_reads_persist_artifacts_and_audits(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    storage = Storage.open(tmp_path / "cash-reads.sqlite3")
    adapters: list[BankAdapter] = []

    async def factory(_connection: object) -> OdooAdapter:
        adapter = BankAdapter(candidate_amount="-90")
        adapters.append(adapter)
        return adapter

    server = create_mcp_server(
        Resolver(_binding(connection)), adapter_factory=factory, storage=storage
    )
    async with Client(server) as client:
        cashbook = await client.call_tool(
            "get_cashbook",
            {
                "company_id": 1,
                "period_start": "2026-04-01",
                "period_end": "2026-04-30",
            },
        )
        unmatched = await client.call_tool(
            "flag_unmatched_statement_lines",
            {
                "company_id": 1,
                "period_start": "2026-04-01",
                "period_end": "2026-04-30",
            },
        )

    assert cashbook.structured_content is not None
    assert cashbook.structured_content["status"] == "ok"
    assert cashbook.structured_content["summary"]["transaction_count"] == 1
    assert unmatched.structured_content is not None
    assert unmatched.structured_content["items"][0]["reason_code"] == ("no_eligible_candidate")
    with storage.database.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 2
    assert [item.tool_name for item in storage.audit.list_for_tenant("tenant-bank")] == [
        "get_cashbook",
        "flag_unmatched_statement_lines",
    ]
    assert all(adapter.closed for adapter in adapters)


async def test_reconciliation_preview_is_read_only_and_does_not_store_proposal(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    storage = Storage.open(tmp_path / "preview.sqlite3")
    adapters: list[BankAdapter] = []

    async def factory(_connection: object) -> OdooAdapter:
        adapter = BankAdapter()
        adapters.append(adapter)
        return adapter

    server = create_mcp_server(
        Resolver(_binding(connection)), adapter_factory=factory, storage=storage
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "reconcile_bank_statement_lines",
            {
                "company_id": 1,
                "period": "2026-04",
                "bank_journal_id": 10,
                "statement_line_ids": [1001],
            },
        )

    assert result.structured_content is not None
    assert result.structured_content["status"] == "preview"
    assert result.structured_content["proposed_action"]["finalizes_odoo_reconciliation"] is False
    assert result.structured_content["material_effects"]["matches"][0]["confidence"] == "1.00"
    with storage.database.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    audit = storage.audit.list_for_tenant("tenant-bank")[0]
    assert audit.final_status == "previewed"
    assert audit.dry_run is True
    assert all(adapter.closed for adapter in adapters)


async def test_explicit_reconciliation_stores_once_and_replays_after_restart(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    path = tmp_path / "proposal.sqlite3"
    storage = Storage.open(path)

    async def factory(_connection: object) -> OdooAdapter:
        return BankAdapter()

    payload = {
        "company_id": 1,
        "period": "2026-04",
        "bank_journal_id": 10,
        "statement_line_ids": [1001],
        "dry_run": False,
        "idempotency_key": "reconcile-2026-04-1001",
    }
    server = create_mcp_server(
        Resolver(_binding(connection)), adapter_factory=factory, storage=storage
    )
    async with Client(server) as client:
        first = await client.call_tool("reconcile_bank_statement_lines", payload)

    reopened = Storage.open(path)
    restarted = create_mcp_server(
        Resolver(_binding(connection)), adapter_factory=factory, storage=reopened
    )
    async with Client(restarted) as client:
        replay = await client.call_tool("reconcile_bank_statement_lines", payload)

    assert first.structured_content is not None
    assert replay.structured_content is not None
    assert first.structured_content["status"] == "succeeded"
    assert replay.structured_content == first.structured_content
    proposal_id = first.structured_content["material_effects"]["proposal_id"]
    proposal = reopened.proposals.get("tenant-bank", proposal_id)
    assert proposal is not None
    assert proposal.company_id == 1
    assert proposal.payload["matches"][0]["move_line_id"] == 2001
    with reopened.database.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    assert [item.final_status for item in reopened.audit.list_for_tenant("tenant-bank")] == [
        "attempted",
        "succeeded",
        "replayed",
    ]


async def test_reconciliation_permission_and_capability_fail_before_workflow_reads(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    for name, permissions, available, expected in (
        ("denied", frozenset({"accounting_read"}), True, "ODOO_AUTH_FAILED"),
        (
            "capability",
            frozenset({"accounting_propose"}),
            False,
            "CAPABILITY_NOT_AVAILABLE",
        ),
    ):
        storage = Storage.open(tmp_path / f"{name}.sqlite3")
        adapters: list[BankAdapter] = []

        async def factory(
            _connection: object,
            selected: bool = available,
            captured: list[BankAdapter] = adapters,
        ) -> OdooAdapter:
            adapter = BankAdapter(accountant_available=selected)
            captured.append(adapter)
            return adapter

        server = create_mcp_server(
            Resolver(_binding(connection, permissions)),
            adapter_factory=factory,
            storage=storage,
        )
        async with Client(server) as client:
            result = await client.call_tool(
                "reconcile_bank_statement_lines",
                {
                    "company_id": 1,
                    "period": "2026-04",
                    "bank_journal_id": 10,
                    "statement_line_ids": [1001],
                },
            )

        assert result.structured_content is not None
        assert result.structured_content["error_code"] == expected
        assert all("statement_lines" not in adapter.read_calls for adapter in adapters)


async def test_changed_reconciliation_state_fails_without_storing_proposal(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    storage = Storage.open(tmp_path / "state-conflict.sqlite3")
    created = 0

    async def factory(_connection: object) -> OdooAdapter:
        nonlocal created
        created += 1
        return BankAdapter(candidate_amount="-100" if created < 3 else "-90")

    server = create_mcp_server(
        Resolver(_binding(connection)), adapter_factory=factory, storage=storage
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "reconcile_bank_statement_lines",
            {
                "company_id": 1,
                "period": "2026-04",
                "bank_journal_id": 10,
                "statement_line_ids": [1001],
                "dry_run": False,
                "idempotency_key": "state-conflict",
            },
        )

    assert result.structured_content is not None
    assert result.structured_content["error_code"] == "ODOO_STATE_CONFLICT"
    with storage.database.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 0
        state = database.execute("SELECT state FROM idempotency_keys").fetchone()[0]
    assert state == "failed"
