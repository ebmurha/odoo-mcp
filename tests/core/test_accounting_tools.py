from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mcp import Client

from odoo_mcp.adapters.accounting import (
    Account,
    AccountMoveLine,
    AnalyticAccount,
    Currency,
    PageRequest,
    PartialReconciliation,
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


class AccountingAdapter:
    def __init__(
        self,
        *,
        account_available: bool = True,
        fail_lines: bool = False,
        partial_ledger: bool = False,
    ) -> None:
        self.account_available = account_available
        self.fail_lines = fail_lines
        self.partial_ledger = partial_ledger
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

    async def get_account_move_lines(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[AccountMoveLine]:
        if self.fail_lines:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "The accounting read timed out.",
                "Retry the request.",
            )
        items = [
            AccountMoveLine(
                id=1,
                move=RelatedRecord(id=2, name="MVE/1"),
                move_state="posted",
                account=RelatedRecord(id=3, name="Cash"),
                journal=RelatedRecord(id=4, name="General"),
                company_id=company_id,
                date=date(2026, 1, 15),
                debit=Decimal("10"),
                credit=Decimal("0"),
                balance=Decimal("10"),
                amount_currency=Decimal("10"),
                residual=Decimal("0"),
                residual_currency=Decimal("0"),
                reconciled=True,
                analytic_distribution={},
            ),
            AccountMoveLine(
                id=5,
                move=RelatedRecord(id=6, name="MVE/1"),
                move_state="posted",
                account=RelatedRecord(id=7, name="Revenue"),
                journal=RelatedRecord(id=4, name="General"),
                company_id=company_id,
                date=date(2026, 1, 15),
                debit=Decimal("0"),
                credit=Decimal("10"),
                balance=Decimal("-10"),
                amount_currency=Decimal("-10"),
                residual=Decimal("0"),
                residual_currency=Decimal("0"),
                reconciled=True,
                analytic_distribution={},
            ),
        ]
        if self.partial_ledger:
            items = [items[1]]
        return RecordPage(items=items)

    async def get_account_accounts(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest,
    ) -> RecordPage[Account]:
        return RecordPage(
            items=[
                Account(
                    id=3,
                    code="1000",
                    name="Cash",
                    account_type="asset_cash",
                    company_ids=(company_id,),
                    reconcile=False,
                ),
                Account(
                    id=7,
                    code="4000",
                    name="Revenue",
                    account_type="income",
                    company_ids=(company_id,),
                    reconcile=False,
                ),
            ]
        )

    async def get_partial_reconciliations(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[PartialReconciliation]:
        return RecordPage(items=[])

    async def get_analytic_accounts(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest,
    ) -> RecordPage[AnalyticAccount]:
        return RecordPage(items=[])

    async def get_currencies(
        self,
        company_id: int,
        currency_ids: tuple[int, ...],
        *,
        page: PageRequest,
    ) -> RecordPage[Currency]:
        return RecordPage(items=[Currency(id=1, name="KES", rounding=Decimal("0.01"))])

    async def close(self) -> None:
        self.closed = True


def _binding(
    connection: OdooConnectionSettings,
    permissions: frozenset[str] = frozenset({"core_read", "accounting_read"}),
) -> ConnectionBinding:
    return ConnectionBinding(
        profile=DeploymentProfile.LOCAL,
        tenant_id="tenant-accounting",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=permissions,
        connection=connection,
    )


async def test_trial_balance_runs_full_path_and_persists_artifact_and_audit(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    storage = Storage.open(tmp_path / "reports.sqlite3")
    adapter = AccountingAdapter()

    async def factory(_connection: object) -> OdooAdapter:
        return adapter

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "get_trial_balance",
            {
                "period_start": "2026-01-01",
                "period_end": "2026-03-31",
                "company_id": 1,
            },
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "ok"
    assert result.structured_content["items"][0]["closing_balance"] == "10"
    audits = storage.audit.list_for_tenant("tenant-accounting")
    assert len(audits) == 1
    assert audits[0].tool_name == "get_trial_balance"
    assert audits[0].final_status == "succeeded"
    assert audits[0].actual_result is not None
    assert "items" not in audits[0].actual_result
    with storage.database.transaction() as database:
        artifacts = database.execute("SELECT * FROM artifacts").fetchall()
    assert len(artifacts) == 1
    assert "# Trial Balance" in str(artifacts[0]["content"])
    assert adapter.closed is True
    assert storage.audit.verify_chain("tenant-accounting").entry_count == 1


async def test_financial_statements_run_full_path_and_persist_artifacts(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    storage = Storage.open(tmp_path / "financial-statements.sqlite3")
    adapter = AccountingAdapter()

    async def factory(_connection: object) -> OdooAdapter:
        return adapter

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        profit_and_loss = await client.call_tool(
            "get_profit_and_loss",
            {
                "period_start": "2026-01-01",
                "period_end": "2026-03-31",
                "company_id": 1,
            },
        )
        balance_sheet = await client.call_tool(
            "get_balance_sheet",
            {"as_of_date": "2026-03-31", "company_id": 1},
        )

    assert profit_and_loss.structured_content is not None
    assert profit_and_loss.structured_content["summary"]["net_profit"] == "10.00"
    assert balance_sheet.structured_content is not None
    assert balance_sheet.structured_content["summary"]["total_assets"] == "10.00"
    assert balance_sheet.structured_content["summary"]["total_equity"] == "10.00"
    assert balance_sheet.structured_content["summary"]["is_balanced"] is True
    audits = storage.audit.list_for_tenant("tenant-accounting")
    assert [audit.tool_name for audit in audits] == ["get_profit_and_loss", "get_balance_sheet"]
    assert all(audit.final_status == "succeeded" for audit in audits)
    with storage.database.transaction() as database:
        artifacts = database.execute("SELECT artifact_type, content FROM artifacts").fetchall()
    assert [row["artifact_type"] for row in artifacts] == [
        "get_profit_and_loss",
        "get_balance_sheet",
    ]
    assert "# Profit and Loss" in str(artifacts[0]["content"])
    assert "# Balance Sheet" in str(artifacts[1]["content"])
    assert storage.audit.verify_chain("tenant-accounting").entry_count == 2


async def test_permission_denial_is_audited_before_adapter_creation(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    storage = Storage.open(tmp_path / "denied.sqlite3")
    called = False

    async def factory(_connection: object) -> OdooAdapter:
        nonlocal called
        called = True
        return AccountingAdapter()

    server = create_mcp_server(
        Resolver(_binding(connection, frozenset({"core_read"}))),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "get_aged_receivables",
            {"as_of_date": "2026-03-31", "company_id": 1},
        )

    assert called is False
    assert result.structured_content is not None
    assert result.structured_content["error_code"] == "ODOO_AUTH_FAILED"
    audit = storage.audit.list_for_tenant("tenant-accounting")[0]
    assert audit.error_code == "ODOO_AUTH_FAILED"
    assert audit.error_message == "Operation failed with ODOO_AUTH_FAILED."


async def test_empty_aging_result_persists_success_audit_and_artifact(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    storage = Storage.open(tmp_path / "empty.sqlite3")

    async def factory(_connection: object) -> OdooAdapter:
        return AccountingAdapter()

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "get_aged_receivables",
            {"as_of_date": "2026-03-31", "company_id": 1},
        )

    assert result.structured_content is not None
    assert result.structured_content["status"] == "ok"
    assert result.structured_content["items"] == []
    assert storage.audit.list_for_tenant("tenant-accounting")[0].final_status == "succeeded"
    with storage.database.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1


async def test_missing_capability_and_upstream_failure_never_create_artifacts(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    for name, adapter, expected in (
        ("missing", AccountingAdapter(account_available=False), "CAPABILITY_NOT_AVAILABLE"),
        ("timeout", AccountingAdapter(fail_lines=True), "ODOO_API_ERROR"),
    ):
        storage = Storage.open(tmp_path / f"{name}.sqlite3")

        async def factory(
            _connection: object,
            selected: AccountingAdapter = adapter,
        ) -> OdooAdapter:
            return selected

        server = create_mcp_server(
            Resolver(_binding(connection)),
            adapter_factory=factory,
            storage=storage,
        )
        async with Client(server) as client:
            result = await client.call_tool(
                "get_aged_payables",
                {"as_of_date": "2026-03-31", "company_id": 1},
            )

        assert result.structured_content is not None
        assert result.structured_content["status"] == "failed"
        assert result.structured_content["error_code"] == expected
        with storage.database.transaction() as database:
            assert database.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert storage.audit.list_for_tenant("tenant-accounting")[0].final_status == "failed"
        assert adapter.closed is True


async def test_unauthorized_company_fails_before_adapter_creation(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    storage = Storage.open(tmp_path / "company.sqlite3")
    called = False

    async def factory(_connection: object) -> OdooAdapter:
        nonlocal called
        called = True
        return AccountingAdapter()

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "get_trial_balance",
            {
                "period_start": "2026-01-01",
                "period_end": "2026-03-31",
                "company_id": 3,
            },
        )

    assert called is False
    assert result.structured_content is not None
    assert result.structured_content["error_code"] == "COMPANY_NOT_FOUND"


async def test_partial_upstream_page_is_failed_and_not_persisted_as_a_report(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    class PartialAdapter(AccountingAdapter):
        calls = 0

        async def get_account_move_lines(
            self, company_id: int, filters: ReadFilters, page: PageRequest
        ) -> RecordPage[AccountMoveLine]:
            self.calls += 1
            if self.calls == 1:
                first = await super().get_account_move_lines(company_id, filters, page)
                return first.model_copy(update={"next_cursor": "synthetic-next"})
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "The next accounting page was unavailable.",
                "Retry the report.",
            )

    storage = Storage.open(tmp_path / "partial.sqlite3")
    adapter = PartialAdapter()

    async def factory(_connection: object) -> OdooAdapter:
        return adapter

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "get_trial_balance",
            {
                "period_start": "2026-01-01",
                "period_end": "2026-03-31",
                "company_id": 1,
            },
        )

    assert result.structured_content is not None
    assert result.structured_content["error_code"] == "ODOO_API_ERROR"
    with storage.database.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    assert storage.audit.list_for_tenant("tenant-accounting")[0].final_status == "failed"


async def test_invalid_report_period_returns_structured_input_error(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    storage = Storage.open(tmp_path / "invalid.sqlite3")
    called = False

    async def factory(_connection: object) -> OdooAdapter:
        nonlocal called
        called = True
        return AccountingAdapter()

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "get_trial_balance",
            {
                "period_start": "2026-04-01",
                "period_end": "2026-03-31",
                "company_id": 1,
            },
        )

    assert called is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "failed"
    assert result.structured_content["error_code"] == "INVALID_INPUT"
    assert storage.audit.list_for_tenant("tenant-accounting")[0].error_code == "INVALID_INPUT"


async def test_financial_statement_invalid_input_and_timeout_are_audited(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    invalid_storage = Storage.open(tmp_path / "invalid-pnl.sqlite3")
    called = False

    async def unused_factory(_connection: object) -> OdooAdapter:
        nonlocal called
        called = True
        return AccountingAdapter()

    invalid_server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=unused_factory,
        storage=invalid_storage,
    )
    async with Client(invalid_server) as client:
        invalid = await client.call_tool(
            "get_profit_and_loss",
            {
                "period_start": "2026-04-01",
                "period_end": "2026-03-31",
                "company_id": 1,
            },
        )

    assert called is False
    assert invalid.structured_content is not None
    assert invalid.structured_content["error_code"] == "INVALID_INPUT"
    assert (
        invalid_storage.audit.list_for_tenant("tenant-accounting")[0].error_code == "INVALID_INPUT"
    )

    timeout_storage = Storage.open(tmp_path / "timeout-balance-sheet.sqlite3")
    adapter = AccountingAdapter(fail_lines=True)

    async def failing_factory(_connection: object) -> OdooAdapter:
        return adapter

    timeout_server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=failing_factory,
        storage=timeout_storage,
    )
    async with Client(timeout_server) as client:
        timeout = await client.call_tool(
            "get_balance_sheet",
            {"as_of_date": "2026-03-31", "company_id": 1},
        )

    assert timeout.structured_content is not None
    assert timeout.structured_content["error_code"] == "ODOO_API_ERROR"
    with timeout_storage.database.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    assert timeout_storage.audit.list_for_tenant("tenant-accounting")[0].final_status == "failed"


async def test_unreconciled_profit_and_loss_source_fails_without_artifact(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    storage = Storage.open(tmp_path / "partial-profit-and-loss.sqlite3")
    adapter = AccountingAdapter(partial_ledger=True)

    async def factory(_connection: object) -> OdooAdapter:
        return adapter

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "get_profit_and_loss",
            {
                "period_start": "2026-01-01",
                "period_end": "2026-03-31",
                "company_id": 1,
            },
        )

    assert result.structured_content is not None
    assert result.structured_content["error_code"] == "ODOO_API_ERROR"
    with storage.database.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
    audit = storage.audit.list_for_tenant("tenant-accounting")[0]
    assert audit.tool_name == "get_profit_and_loss"
    assert audit.final_status == "failed"


async def test_invalid_aging_filter_is_audited_without_adapter_creation(
    connection: OdooConnectionSettings,
    tmp_path,
) -> None:
    storage = Storage.open(tmp_path / "invalid-aging.sqlite3")
    called = False

    async def factory(_connection: object) -> OdooAdapter:
        nonlocal called
        called = True
        return AccountingAdapter()

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "get_aged_receivables",
            {
                "as_of_date": "2026-03-31",
                "company_id": 1,
                "partner_ids": [-1],
            },
        )

    assert called is False
    assert result.structured_content is not None
    assert result.structured_content["error_code"] == "INVALID_INPUT"
    audit = storage.audit.list_for_tenant("tenant-accounting")[0]
    assert audit.tool_name == "get_aged_receivables"
    assert audit.company_id == 1
    assert audit.error_code == "INVALID_INPUT"
