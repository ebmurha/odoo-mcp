from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from odoo_mcp.adapters.accounting import (
    Account,
    AccountMoveLine,
    AnalyticAccount,
    Currency,
    PageRequest,
    ReadFilters,
    RecordPage,
    RelatedRecord,
)
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import BalanceSheetInput, ProfitAndLossInput
from odoo_mcp.workflows.accounting.reports import get_balance_sheet, get_profit_and_loss


def _account(identifier: int, code: str, name: str, account_type: str) -> Account:
    return Account(
        id=identifier,
        code=code,
        name=name,
        account_type=account_type,
        company_ids=(1,),
        reconcile=False,
    )


def _line(
    identifier: int,
    account: Account,
    *,
    debit: str = "0",
    credit: str = "0",
    line_date: date = date(2026, 3, 31),
    move_state: str = "posted",
    analytics: dict[str, Decimal] | None = None,
) -> AccountMoveLine:
    return AccountMoveLine(
        id=identifier,
        move=RelatedRecord(id=100 + identifier, name=f"MVE/{identifier}"),
        move_state=move_state,
        account=RelatedRecord(id=account.id, name=account.name),
        journal=RelatedRecord(id=10, name="General"),
        company_id=1,
        date=line_date,
        debit=Decimal(debit),
        credit=Decimal(credit),
        balance=Decimal(debit) - Decimal(credit),
        amount_currency=Decimal(debit) - Decimal(credit),
        residual=Decimal("0"),
        residual_currency=Decimal("0"),
        reconciled=True,
        analytic_distribution=analytics or {},
    )


class StatementAdapter:
    def __init__(
        self,
        *,
        lines: list[AccountMoveLine],
        accounts: list[Account],
        analytics: tuple[int, ...] = (77,),
    ) -> None:
        self.lines = lines
        self.accounts = accounts
        self.analytics = analytics
        self.filters: list[ReadFilters] = []

    async def get_account_move_lines(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[AccountMoveLine]:
        assert company_id == 1
        self.filters.append(filters)
        return RecordPage(items=self.lines)

    async def get_account_accounts(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest,
    ) -> RecordPage[Account]:
        assert company_id == 1
        requested = next(
            set(clause.value)
            for clause in filters.clauses
            if clause.field == "id" and isinstance(clause.value, tuple)
        )
        return RecordPage(items=[account for account in self.accounts if account.id in requested])

    async def get_analytic_accounts(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest,
    ) -> RecordPage[AnalyticAccount]:
        assert company_id == 1
        requested = next(
            set(clause.value)
            for clause in filters.clauses
            if clause.field == "id" and isinstance(clause.value, tuple)
        )
        return RecordPage(
            items=[
                AnalyticAccount(id=identifier, name=f"Analytic {identifier}", company_id=1)
                for identifier in self.analytics
                if identifier in requested
            ]
        )

    async def get_currencies(
        self,
        company_id: int,
        currency_ids: tuple[int, ...],
        *,
        page: PageRequest,
    ) -> RecordPage[Currency]:
        assert company_id == 1 and currency_ids == (1,)
        return RecordPage(items=[Currency(id=1, name="KES", rounding=Decimal("0.01"))])


async def test_profit_and_loss_classifies_odoo_types_and_reconciles_totals() -> None:
    revenue = _account(40, "4000", "Revenue", "income")
    other_income = _account(41, "4100", "Other income", "income_other")
    expense = _account(50, "5000", "Expense", "expense")
    depreciation = _account(51, "5100", "Depreciation", "expense_depreciation")
    cash = _account(10, "1000", "Cash", "asset_cash")
    adapter = StatementAdapter(
        accounts=[revenue, other_income, expense, depreciation, cash],
        lines=[
            _line(1, revenue, credit="100"),
            _line(2, other_income, credit="10"),
            _line(3, expense, debit="30"),
            _line(4, depreciation, debit="5"),
            _line(5, cash, debit="75"),
        ],
    )

    result = await get_profit_and_loss(
        adapter,
        ProfitAndLossInput(
            company_id=1,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
            limit=2,
        ),
        company_currency_id=1,
        company_name="Synthetic Co",
        request_id="req-pnl",
    )

    assert [item.account_id for item in result.items] == [40, 41]
    assert result.next_cursor is not None
    assert result.summary.account_count == 4
    assert result.summary.income_balance == Decimal("110")
    assert result.summary.expense_balance == Decimal("35")
    assert result.summary.net_profit == Decimal("75")
    assert "Page rows: 1-2 of 4" in result.artifact_markdown
    assert "Whole-report totals:" in result.artifact_markdown
    source_filters = adapter.filters[0].clauses
    assert any(
        clause.field == "move_id.state" and clause.value == "posted" for clause in source_filters
    )
    assert any(clause.field == "date" and clause.operator == ">=" for clause in source_filters)
    assert any(clause.field == "date" and clause.operator == "<=" for clause in source_filters)


@pytest.mark.parametrize(
    "line_date",
    [date(2025, 12, 31), date(2026, 4, 1)],
)
async def test_profit_and_loss_rejects_line_outside_requested_period(line_date: date) -> None:
    revenue = _account(40, "4000", "Revenue", "income")
    cash = _account(10, "1000", "Cash", "asset_cash")
    adapter = StatementAdapter(
        accounts=[revenue, cash],
        lines=[
            _line(1, revenue, credit="100", line_date=line_date),
            _line(2, cash, debit="100", line_date=line_date),
        ],
    )

    with pytest.raises(OdooMcpError) as caught:
        await get_profit_and_loss(
            adapter,
            ProfitAndLossInput(
                company_id=1,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
            ),
            company_currency_id=1,
            company_name="Synthetic Co",
            request_id="req-out-of-period",
        )

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


async def test_profit_and_loss_rejects_unreconciled_source_ledger() -> None:
    revenue = _account(40, "4000", "Revenue", "income")
    adapter = StatementAdapter(
        accounts=[revenue],
        lines=[_line(1, revenue, credit="100")],
    )

    with pytest.raises(OdooMcpError) as caught:
        await get_profit_and_loss(
            adapter,
            ProfitAndLossInput(
                company_id=1,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
            ),
            company_currency_id=1,
            company_name="Synthetic Co",
            request_id="req-unreconciled-pnl",
        )

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


async def test_profit_and_loss_applies_exact_analytic_filter() -> None:
    revenue = _account(40, "4000", "Revenue", "income")
    cash = _account(10, "1000", "Cash", "asset_cash")
    adapter = StatementAdapter(
        accounts=[revenue, cash],
        lines=[
            _line(1, revenue, credit="100", analytics={"77,88": Decimal("50")}),
            _line(2, revenue, credit="25", analytics={"99": Decimal("100")}),
            _line(3, cash, debit="100", analytics={"77,88": Decimal("50")}),
            _line(4, cash, debit="25", analytics={"99": Decimal("100")}),
        ],
    )

    result = await get_profit_and_loss(
        adapter,
        ProfitAndLossInput(
            company_id=1,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
            analytic_account_ids=(77,),
        ),
        company_currency_id=1,
        company_name="Synthetic Co",
        request_id="req-analytic",
    )

    assert result.summary.income_credit == Decimal("50")
    assert result.summary.net_profit == Decimal("50")


async def test_unknown_analytic_filter_fails_before_reporting() -> None:
    adapter = StatementAdapter(lines=[], accounts=[], analytics=())

    with pytest.raises(OdooMcpError) as caught:
        await get_profit_and_loss(
            adapter,
            ProfitAndLossInput(
                company_id=1,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
                analytic_account_ids=(77,),
            ),
            company_currency_id=1,
            company_name="Synthetic Co",
            request_id="req-missing-analytic",
        )

    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert adapter.filters == []


async def test_balance_sheet_includes_unclosed_earnings_and_balances() -> None:
    cash = _account(10, "1000", "Cash", "asset_cash")
    payable = _account(20, "2000", "Payable", "liability_payable")
    equity = _account(30, "3000", "Capital", "equity")
    revenue = _account(40, "4000", "Revenue", "income")
    expense = _account(50, "5000", "Expense", "expense")
    adapter = StatementAdapter(
        accounts=[cash, payable, equity, revenue, expense],
        lines=[
            _line(1, cash, debit="100"),
            _line(2, payable, credit="40"),
            _line(3, equity, credit="20"),
            _line(4, revenue, credit="50"),
            _line(5, expense, debit="10"),
        ],
    )

    result = await get_balance_sheet(
        adapter,
        BalanceSheetInput(company_id=1, as_of_date=date(2026, 3, 31)),
        company_currency_id=1,
        company_name="Synthetic Co",
        request_id="req-bs",
    )

    assert [item.group for item in result.items] == ["asset", "liability", "equity"]
    assert result.summary.total_assets == Decimal("100")
    assert result.summary.total_liabilities == Decimal("40")
    assert result.summary.equity_account_balance == Decimal("20")
    assert result.summary.unclosed_earnings == Decimal("40")
    assert result.summary.total_equity == Decimal("60")
    assert result.summary.balancing_difference == Decimal("0")
    assert result.summary.is_balanced is True
    assert "Balancing check: balanced" in result.artifact_markdown


async def test_unfiltered_unbalanced_balance_sheet_fails_explicitly() -> None:
    cash = _account(10, "1000", "Cash", "asset_cash")
    adapter = StatementAdapter(
        accounts=[cash],
        lines=[_line(1, cash, debit="100.02")],
    )

    with pytest.raises(OdooMcpError) as caught:
        await get_balance_sheet(
            adapter,
            BalanceSheetInput(company_id=1, as_of_date=date(2026, 3, 31)),
            company_currency_id=1,
            company_name="Synthetic Co",
            request_id="req-unbalanced",
        )

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


async def test_balance_sheet_rejects_line_after_as_of_date() -> None:
    cash = _account(10, "1000", "Cash", "asset_cash")
    equity = _account(30, "3000", "Capital", "equity")
    adapter = StatementAdapter(
        accounts=[cash, equity],
        lines=[
            _line(1, cash, debit="100", line_date=date(2026, 4, 1)),
            _line(2, equity, credit="100", line_date=date(2026, 4, 1)),
        ],
    )

    with pytest.raises(OdooMcpError) as caught:
        await get_balance_sheet(
            adapter,
            BalanceSheetInput(company_id=1, as_of_date=date(2026, 3, 31)),
            company_currency_id=1,
            company_name="Synthetic Co",
            request_id="req-future-balance",
        )

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


@pytest.mark.parametrize("statement", ["profit_and_loss", "balance_sheet"])
async def test_financial_statements_reject_non_posted_lines(statement: str) -> None:
    cash = _account(10, "1000", "Cash", "asset_cash")
    revenue = _account(40, "4000", "Revenue", "income")
    adapter = StatementAdapter(
        accounts=[cash, revenue],
        lines=[
            _line(1, cash, debit="100", move_state="draft"),
            _line(2, revenue, credit="100", move_state="draft"),
        ],
    )

    with pytest.raises(OdooMcpError) as caught:
        if statement == "profit_and_loss":
            await get_profit_and_loss(
                adapter,
                ProfitAndLossInput(
                    company_id=1,
                    period_start=date(2026, 1, 1),
                    period_end=date(2026, 3, 31),
                ),
                company_currency_id=1,
                company_name="Synthetic Co",
                request_id="req-draft-pnl",
            )
        else:
            await get_balance_sheet(
                adapter,
                BalanceSheetInput(company_id=1, as_of_date=date(2026, 3, 31)),
                company_currency_id=1,
                company_name="Synthetic Co",
                request_id="req-draft-bs",
            )

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


async def test_empty_statements_are_successful_and_compact() -> None:
    adapter = StatementAdapter(lines=[], accounts=[])

    pnl = await get_profit_and_loss(
        adapter,
        ProfitAndLossInput(
            company_id=1,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
        ),
        company_currency_id=1,
        company_name="Synthetic Co",
        request_id="req-empty-pnl",
    )
    balance_sheet = await get_balance_sheet(
        adapter,
        BalanceSheetInput(company_id=1, as_of_date=date(2026, 3, 31)),
        company_currency_id=1,
        company_name="Synthetic Co",
        request_id="req-empty-bs",
    )

    assert pnl.items == []
    assert pnl.summary.net_profit == 0
    assert balance_sheet.items == []
    assert balance_sheet.summary.is_balanced is True
    assert "Page rows: 0-0 of 0" in pnl.artifact_markdown
    assert "Page rows: 0-0 of 0" in balance_sheet.artifact_markdown


async def test_repeated_upstream_page_fails_instead_of_double_counting() -> None:
    revenue = _account(40, "4000", "Revenue", "income")

    class RepeatingAdapter(StatementAdapter):
        async def get_account_move_lines(
            self, company_id: int, filters: ReadFilters, page: PageRequest
        ) -> RecordPage[AccountMoveLine]:
            result = await super().get_account_move_lines(company_id, filters, page)
            return result.model_copy(update={"next_cursor": "repeat"})

    adapter = RepeatingAdapter(
        accounts=[revenue],
        lines=[_line(1, revenue, credit="10")],
    )

    with pytest.raises(OdooMcpError) as caught:
        await get_profit_and_loss(
            adapter,
            ProfitAndLossInput(
                company_id=1,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
            ),
            company_currency_id=1,
            company_name="Synthetic Co",
            request_id="req-repeated-page",
        )

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


def test_statement_inputs_reject_invalid_period_and_analytic_ids() -> None:
    with pytest.raises(ValidationError):
        ProfitAndLossInput(
            company_id=1,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 3, 31),
        )
    with pytest.raises(ValidationError):
        BalanceSheetInput(
            company_id=1,
            as_of_date=date(2026, 3, 31),
            analytic_account_ids=(77, 77),
        )
