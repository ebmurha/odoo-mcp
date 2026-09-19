from __future__ import annotations

from datetime import date
from decimal import Decimal

from odoo_mcp.adapters.accounting import (
    Account,
    AccountMoveLine,
    PageRequest,
    PartialReconciliation,
    ReadFilters,
    RecordPage,
    RelatedRecord,
)
from odoo_mcp.mcp.schemas import AgingInput, TrialBalanceInput
from odoo_mcp.workflows.accounting.reports import get_aged_balance, get_trial_balance


def _line(
    identifier: int,
    *,
    account_id: int,
    account_name: str,
    line_date: date,
    debit: str = "0",
    credit: str = "0",
    residual: str = "0",
    partner_id: int | None = None,
    partner_name: str = "Synthetic Partner",
    maturity_date: date | None = None,
) -> AccountMoveLine:
    return AccountMoveLine(
        id=identifier,
        move=RelatedRecord(id=100 + identifier, name=f"MVE/{identifier}"),
        account=RelatedRecord(id=account_id, name=account_name),
        journal=RelatedRecord(id=10, name="General"),
        partner=(None if partner_id is None else RelatedRecord(id=partner_id, name=partner_name)),
        company_id=1,
        date=line_date,
        maturity_date=maturity_date,
        debit=Decimal(debit),
        credit=Decimal(credit),
        balance=Decimal(debit) - Decimal(credit),
        amount_currency=Decimal(debit) - Decimal(credit),
        residual=Decimal(residual),
        residual_currency=Decimal(residual),
        reconciled=Decimal(residual) == 0,
        analytic_distribution={},
    )


class ReportAdapter:
    def __init__(
        self,
        *,
        lines: list[AccountMoveLine],
        accounts: list[Account] | None = None,
        partials: list[PartialReconciliation] | None = None,
    ) -> None:
        self.lines = lines
        self.accounts = accounts or []
        self.partials = partials or []
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
        self.filters.append(filters)
        requested = next(
            (
                set(clause.value)
                for clause in filters.clauses
                if clause.field == "id" and isinstance(clause.value, tuple)
            ),
            set(),
        )
        return RecordPage(items=[item for item in self.accounts if item.id in requested])

    async def get_partial_reconciliations(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[PartialReconciliation]:
        assert company_id == 1
        self.filters.append(filters)
        return RecordPage(items=self.partials)


async def test_trial_balance_reconciles_opening_movement_totals_and_pages() -> None:
    adapter = ReportAdapter(
        lines=[
            _line(
                1,
                account_id=10,
                account_name="Cash",
                line_date=date(2025, 12, 31),
                debit="100",
            ),
            _line(
                2,
                account_id=10,
                account_name="Cash",
                line_date=date(2026, 1, 15),
                debit="25.50",
            ),
            _line(
                3,
                account_id=20,
                account_name="Revenue",
                line_date=date(2026, 2, 1),
                credit="25.50",
            ),
        ],
        accounts=[
            Account(
                id=10,
                code="1000",
                name="Cash",
                account_type="asset_cash",
                company_ids=(1,),
                reconcile=False,
            ),
            Account(
                id=20,
                code="4000",
                name="Revenue",
                account_type="income",
                company_ids=(1,),
                reconcile=False,
            ),
        ],
    )
    request = TrialBalanceInput(
        company_id=1,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 3, 31),
        limit=1,
    )

    first = await get_trial_balance(
        adapter, request, company_name="Synthetic Co", request_id="req_trial"
    )
    second = await get_trial_balance(
        adapter,
        request.model_copy(update={"cursor": first.next_cursor}),
        company_name="Synthetic Co",
        request_id="req_trial_2",
    )

    assert [item.account_id for item in first.items] == [10]
    assert [item.account_id for item in second.items] == [20]
    assert first.items[0].opening_balance == Decimal("100")
    assert first.items[0].period_debit == Decimal("25.50")
    assert first.summary.period_debit == first.summary.period_credit == Decimal("25.50")
    assert first.summary.closing_balance == Decimal("100.00")
    assert first.next_cursor is not None
    assert second.next_cursor is None
    assert "Audit reference: `req_trial`" in first.artifact_markdown
    assert {clause.field for clause in adapter.filters[0].clauses} >= {
        "move_id.state",
        "date",
        "account_id",
    }
    assert any(
        clause.field == "account_id" and clause.operator == "!=" and clause.value is False
        for clause in adapter.filters[0].clauses
    )


async def test_aging_reconstructs_historical_residual_and_calendar_buckets() -> None:
    adapter = ReportAdapter(
        lines=[
            _line(
                1,
                account_id=30,
                account_name="Receivable",
                line_date=date(2026, 1, 1),
                debit="100",
                residual="40",
                partner_id=7,
                maturity_date=date(2026, 2, 15),
            ),
            _line(
                2,
                account_id=30,
                account_name="Receivable",
                line_date=date(2026, 2, 20),
                debit="50",
                residual="50",
                partner_id=7,
                maturity_date=date(2026, 3, 15),
            ),
        ],
        partials=[
            PartialReconciliation(
                id=1,
                debit_move_line=RelatedRecord(id=1, name="Receivable line"),
                credit_move_line=RelatedRecord(id=99, name="Payment line"),
                amount=Decimal("60"),
                debit_amount_currency=Decimal("60"),
                credit_amount_currency=Decimal("60"),
                max_date=date(2026, 3, 5),
            )
        ],
    )

    result = await get_aged_balance(
        adapter,
        AgingInput(company_id=1, as_of_date=date(2026, 3, 1)),
        company_name="Synthetic Co",
        request_id="req_aging",
        payable=False,
    )

    assert len(result.items) == 1
    item = result.items[0]
    assert item.residual_total == Decimal("150")
    assert item.buckets.days_1_30 == Decimal("100")
    assert item.buckets.not_yet_due == Decimal("50")
    assert item.oldest_due_date == date(2026, 2, 15)
    assert result.summary.residual_total == Decimal("150")
    assert any(
        clause.field == "account_id.account_type" and clause.value == "asset_receivable"
        for clause in adapter.filters[0].clauses
    )
    assert any(
        clause.field == "max_date" and clause.operator == ">"
        for clause in adapter.filters[1].clauses
    )


async def test_empty_aging_is_successful_and_compact() -> None:
    result = await get_aged_balance(
        ReportAdapter(lines=[]),
        AgingInput(company_id=1, as_of_date=date(2026, 3, 1)),
        company_name="Synthetic Co",
        request_id="req_empty",
        payable=True,
    )

    assert result.items == []
    assert result.summary.partner_count == 0
    assert result.summary.residual_total == 0
    assert "Aged Payables" in result.artifact_markdown


async def test_payables_preserve_odoo_company_currency_sign() -> None:
    adapter = ReportAdapter(
        lines=[
            _line(
                8,
                account_id=40,
                account_name="Payable",
                line_date=date(2025, 10, 1),
                credit="50",
                residual="-20",
                partner_id=9,
                partner_name="Synthetic Supplier",
                maturity_date=date(2025, 11, 1),
            )
        ],
        partials=[
            PartialReconciliation(
                id=2,
                debit_move_line=RelatedRecord(id=88, name="Payment line"),
                credit_move_line=RelatedRecord(id=8, name="Payable line"),
                amount=Decimal("30"),
                debit_amount_currency=Decimal("30"),
                credit_amount_currency=Decimal("30"),
                max_date=date(2026, 4, 1),
            )
        ],
    )

    result = await get_aged_balance(
        adapter,
        AgingInput(company_id=1, as_of_date=date(2026, 3, 1)),
        company_name="Synthetic Co",
        request_id="req_payables",
        payable=True,
    )

    assert result.items[0].residual_total == Decimal("-50")
    assert result.items[0].buckets.days_90_plus == Decimal("-50")


async def test_large_trial_balance_is_paginated_without_truncating_totals() -> None:
    accounts = [
        Account(
            id=identifier,
            code=f"{identifier:04d}",
            name=f"Synthetic account {identifier}",
            account_type="asset_current",
            company_ids=(1,),
            reconcile=False,
        )
        for identifier in range(1, 502)
    ]
    lines = [
        _line(
            identifier,
            account_id=identifier,
            account_name=f"Synthetic account {identifier}",
            line_date=date(2026, 1, 1),
            debit="1",
        )
        for identifier in range(1, 502)
    ]

    result = await get_trial_balance(
        ReportAdapter(lines=lines, accounts=accounts),
        TrialBalanceInput(
            company_id=1,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            limit=500,
        ),
        company_name="Synthetic Co",
        request_id="req_large",
    )

    assert len(result.items) == 500
    assert result.next_cursor is not None
    assert result.summary.account_count == 501
    assert result.summary.period_debit == Decimal("501")
