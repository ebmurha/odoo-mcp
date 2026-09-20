from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from odoo_mcp.adapters.accounting import (
    AccountMoveLine,
    BankStatementLine,
    DatePeriod,
    Journal,
    PageRequest,
    ReadFilters,
    RecordPage,
    RelatedRecord,
)
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import (
    CashbookInput,
    ReconciliationInput,
    UnmatchedStatementLinesInput,
)
from odoo_mcp.workflows.accounting.cashbook import get_cashbook
from odoo_mcp.workflows.accounting.reconcile_bank import (
    build_reconciliation_proposal,
    flag_unmatched_statement_lines,
)


def _move_line(
    identifier: int,
    *,
    amount: str,
    line_date: date,
    partner_id: int | None = None,
    label: str | None = None,
    move_id: int | None = None,
    journal_id: int = 20,
) -> AccountMoveLine:
    value = Decimal(amount)
    return AccountMoveLine(
        id=identifier,
        move=RelatedRecord(id=move_id or 1000 + identifier, name=f"MVE/{identifier}"),
        account=RelatedRecord(id=400, name="Clearing"),
        journal=RelatedRecord(id=journal_id, name="General"),
        partner=(
            None
            if partner_id is None
            else RelatedRecord(id=partner_id, name=f"Partner {partner_id}")
        ),
        company_id=1,
        date=line_date,
        label=label,
        debit=max(value, Decimal("0")),
        credit=max(-value, Decimal("0")),
        balance=value,
        amount_currency=value,
        residual=value,
        residual_currency=value,
        reconciled=False,
        analytic_distribution={},
    )


def _statement(
    identifier: int,
    *,
    amount: str = "100",
    line_date: date = date(2026, 4, 10),
    partner_id: int | None = 7,
    reference: str | None = " Invoice  42 ",
    move_id: int | None = None,
) -> BankStatementLine:
    return BankStatementLine(
        id=identifier,
        date=line_date,
        payment_reference=reference,
        amount=Decimal(amount),
        partner=(
            None
            if partner_id is None
            else RelatedRecord(id=partner_id, name=f"Partner {partner_id}")
        ),
        journal=RelatedRecord(id=10, name="Bank"),
        company_id=1,
        reconciled=False,
        move=(None if move_id is None else RelatedRecord(id=move_id, name="Bank move")),
    )


class CashAdapter:
    def __init__(
        self,
        *,
        statements: list[BankStatementLine] | None = None,
        lines: list[AccountMoveLine] | None = None,
        journals: list[Journal] | None = None,
    ) -> None:
        self.statements = statements or []
        self.lines = lines or []
        self.journals = journals or [
            Journal(id=10, name="Bank", code="BNK", journal_type="bank", company_id=1),
            Journal(id=11, name="Cash", code="CSH", journal_type="cash", company_id=1),
            Journal(id=20, name="General", code="GEN", journal_type="general", company_id=1),
        ]
        self.move_line_filters: list[ReadFilters] = []

    async def get_journals(self, company_id: int, *, page: PageRequest) -> RecordPage[Journal]:
        assert company_id == 1
        return RecordPage(items=self.journals)

    async def get_account_move_lines(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[AccountMoveLine]:
        assert company_id == 1
        self.move_line_filters.append(filters)
        return RecordPage(items=self.lines)

    async def get_bank_statement_lines(
        self,
        company_id: int,
        period: DatePeriod,
        journal_id: int | None,
        *,
        page: PageRequest,
    ) -> RecordPage[BankStatementLine]:
        assert company_id == 1
        selected = [item for item in self.statements if journal_id in {None, item.journal.id}]
        return RecordPage(items=selected)


async def test_cashbook_uses_only_cash_bank_journals_and_reconciles_summary() -> None:
    adapter = CashAdapter(
        lines=[
            _move_line(1, amount="50", line_date=date(2026, 3, 31), journal_id=10),
            _move_line(2, amount="25", line_date=date(2026, 4, 2), journal_id=11),
            _move_line(3, amount="-10", line_date=date(2026, 4, 3), journal_id=10),
        ]
    )

    result = await get_cashbook(
        adapter,
        CashbookInput(
            company_id=1,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
        ),
        company_name="Synthetic Co",
        request_id="req_cash",
    )

    assert [item.line_id for item in result.items] == [2, 3]
    assert result.summary.opening_balance == Decimal("50")
    assert result.summary.total_debit == Decimal("25")
    assert result.summary.total_credit == Decimal("10")
    assert result.summary.closing_balance == Decimal("65")
    assert "Whole-report totals: opening 50; debit 25; credit 10; closing 65." in (
        result.artifact_markdown
    )
    clauses = adapter.move_line_filters[0].clauses
    assert any(clause.field == "move_id.state" and clause.value == "posted" for clause in clauses)
    assert any(clause.field == "journal_id" and clause.value == (10, 11) for clause in clauses)


async def test_reconciliation_scores_exact_match_and_never_calls_a_mutation() -> None:
    adapter = CashAdapter(
        statements=[_statement(1001)],
        lines=[
            _move_line(
                2001,
                amount="-100",
                line_date=date(2026, 4, 10),
                partner_id=7,
                label="invoice 42",
            )
        ],
    )

    result = await build_reconciliation_proposal(
        adapter,
        ReconciliationInput(
            company_id=1,
            period="2026-04",
            bank_journal_id=10,
            statement_line_ids=(1001,),
        ),
        company_name="Synthetic Co",
        request_id="req_match",
    )

    assert len(result.matches) == 1
    assert result.matches[0].statement_line_id == 1001
    assert result.matches[0].move_line_id == 2001
    assert result.matches[0].confidence == Decimal("1.00")
    assert result.matches[0].score_components == {
        "amount": Decimal("0.55"),
        "partner": Decimal("0.20"),
        "reference": Decimal("0.15"),
        "date_proximity": Decimal("0.10"),
    }
    assert result.unmatched == []
    assert result.summary.matched_amount == Decimal("100")


async def test_best_score_tie_is_ambiguous_and_flagged_unmatched() -> None:
    adapter = CashAdapter(
        statements=[_statement(1001)],
        lines=[
            _move_line(
                2001,
                amount="-100",
                line_date=date(2026, 4, 10),
                partner_id=7,
                label="invoice 42",
            ),
            _move_line(
                2002,
                amount="-100",
                line_date=date(2026, 4, 10),
                partner_id=7,
                label="INVOICE 42",
            ),
        ],
    )

    result = await flag_unmatched_statement_lines(
        adapter,
        UnmatchedStatementLinesInput(
            company_id=1,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
        ),
        company_name="Synthetic Co",
        request_id="req_tie",
    )

    assert len(result.items) == 1
    assert result.items[0].reason_code == "ambiguous_best_match"
    assert result.items[0].best_rejected_score == Decimal("1.00")
    assert result.summary.ambiguous_count == 1


async def test_threshold_rejects_but_does_not_relax_candidate_eligibility() -> None:
    adapter = CashAdapter(
        statements=[_statement(1001, partner_id=None, reference=None)],
        lines=[
            _move_line(
                2001,
                amount="-100",
                line_date=date(2026, 4, 10),
                partner_id=None,
                label=None,
            ),
            _move_line(2002, amount="-99.99", line_date=date(2026, 4, 10)),
        ],
    )

    result = await build_reconciliation_proposal(
        adapter,
        ReconciliationInput(
            company_id=1,
            period="2026-04",
            bank_journal_id=10,
            statement_line_ids=(1001,),
            match_confidence_threshold=Decimal("0.70"),
        ),
        company_name="Synthetic Co",
        request_id="req_threshold",
    )

    assert result.matches == []
    assert result.unmatched[0].reason_code == "below_confidence_threshold"
    assert result.unmatched[0].best_rejected_score == Decimal("0.65")


async def test_reconciliation_requires_the_bank_journal_currency() -> None:
    usd = RelatedRecord(id=2, name="USD")
    eur = RelatedRecord(id=3, name="EUR")
    adapter = CashAdapter(
        statements=[_statement(1001)],
        lines=[
            _move_line(
                2001,
                amount="-100",
                line_date=date(2026, 4, 10),
                partner_id=7,
                label="invoice 42",
            ).model_copy(update={"currency": eur}),
            _move_line(
                2002,
                amount="-100",
                line_date=date(2026, 4, 10),
                partner_id=7,
                label="invoice 42",
            ).model_copy(update={"currency": usd}),
        ],
        journals=[
            Journal(
                id=10,
                name="USD Bank",
                code="USD",
                journal_type="bank",
                company_id=1,
                currency=usd,
            )
        ],
    )

    result = await build_reconciliation_proposal(
        adapter,
        ReconciliationInput(
            company_id=1,
            period="2026-04",
            bank_journal_id=10,
            statement_line_ids=(1001,),
        ),
        company_name="Synthetic Co",
        request_id="req_currency",
    )

    assert result.matches[0].move_line_id == 2002
    assert result.matches[0].currency_id == 2
    assert result.summary.currency_name == "USD"


async def test_one_move_line_cannot_be_proposed_for_two_statement_lines() -> None:
    adapter = CashAdapter(
        statements=[_statement(1001), _statement(1002)],
        lines=[
            _move_line(
                2001,
                amount="-100",
                line_date=date(2026, 4, 10),
                partner_id=7,
                label="invoice 42",
            )
        ],
    )

    result = await build_reconciliation_proposal(
        adapter,
        ReconciliationInput(
            company_id=1,
            period="2026-04",
            bank_journal_id=10,
            statement_line_ids=(1001, 1002),
        ),
        company_name="Synthetic Co",
        request_id="req_collision",
    )

    assert result.matches == []
    assert {item.reason_code for item in result.unmatched} == {"candidate_conflict"}


async def test_unmatched_output_pages_without_hiding_whole_report_totals() -> None:
    adapter = CashAdapter(
        statements=[
            _statement(1001, amount="100"),
            _statement(1002, amount="25", line_date=date(2026, 4, 11)),
        ],
        lines=[],
    )

    result = await flag_unmatched_statement_lines(
        adapter,
        UnmatchedStatementLinesInput(
            company_id=1,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
            limit=1,
        ),
        company_name="Synthetic Co",
        request_id="req_page",
    )

    assert len(result.items) == 1
    assert result.next_cursor is not None
    assert result.summary.unmatched_count == 2
    assert "Page rows: 1-1 of 2" in result.artifact_markdown
    assert "Whole-report totals: reviewed 2; unmatched 2." in result.artifact_markdown


async def test_cross_company_candidate_fails_instead_of_becoming_a_match() -> None:
    adapter = CashAdapter(
        statements=[_statement(1001)],
        lines=[
            _move_line(
                2001,
                amount="-100",
                line_date=date(2026, 4, 10),
                partner_id=7,
                label="invoice 42",
            ).model_copy(update={"company_id": 2})
        ],
    )

    with pytest.raises(OdooMcpError) as caught:
        await build_reconciliation_proposal(
            adapter,
            ReconciliationInput(
                company_id=1,
                period="2026-04",
                bank_journal_id=10,
                statement_line_ids=(1001,),
            ),
            company_name="Synthetic Co",
            request_id="req_cross_company",
        )

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


async def test_repeated_upstream_cursor_fails_explicitly() -> None:
    class RepeatingCursorAdapter(CashAdapter):
        async def get_bank_statement_lines(
            self,
            company_id: int,
            period: DatePeriod,
            journal_id: int | None,
            *,
            page: PageRequest,
        ) -> RecordPage[BankStatementLine]:
            return RecordPage(items=[], next_cursor="same-page")

    with pytest.raises(OdooMcpError) as caught:
        await flag_unmatched_statement_lines(
            RepeatingCursorAdapter(),
            UnmatchedStatementLinesInput(
                company_id=1,
                period_start=date(2026, 4, 1),
                period_end=date(2026, 4, 30),
            ),
            company_name="Synthetic Co",
            request_id="req_partial",
        )

    assert caught.value.code is ErrorCode.ODOO_API_ERROR
