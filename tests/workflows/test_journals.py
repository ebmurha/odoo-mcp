from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from odoo_mcp.adapters.accounting import (
    Account,
    AccountMove,
    AnalyticAccount,
    Currency,
    Journal,
    JournalEntry,
    JournalEntryLine,
    Partner,
    RecordPage,
    RelatedRecord,
)
from odoo_mcp.adapters.base import Company
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import (
    CreateJournalEntryInput,
    JournalEntriesInput,
    JournalEntryLineInput,
    PostJournalEntryInput,
)
from odoo_mcp.workflows.accounting.journals import (
    execute_journal_entry_post,
    list_journal_entries,
    prepare_journal_entry_draft,
    prepare_journal_entry_post,
)


def _line(identifier: int, debit: str, credit: str) -> JournalEntryLine:
    return JournalEntryLine(
        id=identifier,
        account=RelatedRecord(id=10 if debit != "0" else 20, name="Account"),
        partner=RelatedRecord(id=30, name="Partner"),
        description="Line",
        debit=Decimal(debit),
        credit=Decimal(credit),
        analytic_distribution={"40": Decimal("100")},
    )


def _entry(
    *, state: str = "draft", lines: tuple[JournalEntryLine, ...] | None = None
) -> JournalEntry:
    return JournalEntry(
        id=101,
        name="MISC/101",
        move_type="entry",
        state=state,
        date=date(2026, 9, 1),
        journal=RelatedRecord(id=5, name="Miscellaneous"),
        company_id=1,
        currency=RelatedRecord(id=1, name="KES"),
        reference="Synthetic entry",
        lines=lines or (_line(1, "100", "0"), _line(2, "0", "100")),
    )


class JournalAdapter:
    def __init__(self, entry: JournalEntry | None = None) -> None:
        self.entry = entry or _entry()

    async def get_companies(self) -> list[Company]:
        return [Company(id=1, name="Synthetic Company", currency=RelatedRecord(id=1, name="KES"))]

    async def get_currencies(self, *_args: object, **_kwargs: object) -> RecordPage[Currency]:
        return RecordPage(items=[Currency(id=1, name="KES", rounding=Decimal("0.01"))])

    async def get_journals(self, *_args: object, **_kwargs: object) -> RecordPage[Journal]:
        return RecordPage(
            items=[
                Journal(
                    id=5, name="Miscellaneous", code="MISC", journal_type="general", company_id=1
                )
            ]
        )

    async def get_account_accounts(self, *_args: object, **_kwargs: object) -> RecordPage[Account]:
        return RecordPage(
            items=[
                Account(
                    id=10,
                    code="1000",
                    name="Debit",
                    account_type="asset_current",
                    company_ids=(1,),
                    reconcile=False,
                ),
                Account(
                    id=20,
                    code="2000",
                    name="Credit",
                    account_type="liability_current",
                    company_ids=(1,),
                    reconcile=False,
                ),
            ]
        )

    async def get_partners(self, *_args: object, **_kwargs: object) -> RecordPage[Partner]:
        return RecordPage(items=[Partner(id=30, name="Partner")])

    async def get_analytic_accounts(
        self, *_args: object, **_kwargs: object
    ) -> RecordPage[AnalyticAccount]:
        return RecordPage(items=[AnalyticAccount(id=40, name="Grant")])

    async def get_account_moves(self, *_args: object, **_kwargs: object) -> RecordPage[AccountMove]:
        entry = self.entry
        return RecordPage(
            items=[
                AccountMove(
                    id=entry.id,
                    name=entry.name,
                    move_type=entry.move_type,
                    state=entry.state,
                    date=entry.date,
                    journal=entry.journal,
                    company_id=entry.company_id,
                    currency=entry.currency,
                    amount_total=Decimal("0"),
                    amount_residual=Decimal("0"),
                    reference=entry.reference,
                )
            ]
        )

    async def get_journal_entry(self, company_id: int, move_id: int) -> JournalEntry:
        assert company_id == 1 and move_id == 101
        return self.entry

    async def post_journal_entry(self, company_id: int, move_id: int) -> JournalEntry:
        assert company_id == 1 and move_id == 101
        return self.entry


def _create_request(debit: str = "100.004", credit: str = "100") -> CreateJournalEntryInput:
    return CreateJournalEntryInput(
        company_id=1,
        journal_id=5,
        entry_date=date(2026, 9, 1),
        reference=" Synthetic entry ",
        lines=(
            JournalEntryLineInput(
                account_id=10,
                partner_id=30,
                debit=Decimal(debit),
                credit=Decimal("0"),
                analytic_account_id=40,
            ),
            JournalEntryLineInput(account_id=20, debit=Decimal("0"), credit=Decimal(credit)),
        ),
    )


async def test_create_preview_validates_references_and_currency_precision() -> None:
    draft, preview = await prepare_journal_entry_draft(JournalAdapter(), _create_request())

    assert draft.reference == "Synthetic entry"
    assert preview.proposed_action["posts_entry"] is False
    assert preview.material_effects["balanced"] is True
    assert preview.material_effects["total_debit"] == "100.004"


async def test_create_rejects_unbalanced_entry() -> None:
    with pytest.raises(OdooMcpError) as raised:
        await prepare_journal_entry_draft(JournalAdapter(), _create_request("100.02", "100"))

    assert raised.value.code is ErrorCode.JOURNAL_ENTRY_UNBALANCED


@pytest.mark.parametrize(
    ("method_name", "expected_code"),
    [
        ("get_journals", ErrorCode.JOURNAL_NOT_FOUND),
        ("get_account_accounts", ErrorCode.ACCOUNT_NOT_FOUND),
        ("get_partners", ErrorCode.INVALID_INPUT),
        ("get_analytic_accounts", ErrorCode.INVALID_INPUT),
        ("get_currencies", ErrorCode.ODOO_API_ERROR),
    ],
)
async def test_create_rejects_unavailable_scoped_references(
    method_name: str, expected_code: ErrorCode
) -> None:
    adapter = JournalAdapter()

    async def unavailable(*_args: object, **_kwargs: object) -> RecordPage[object]:
        return RecordPage(items=[])

    setattr(adapter, method_name, unavailable)
    with pytest.raises(OdooMcpError) as raised:
        await prepare_journal_entry_draft(adapter, _create_request())

    assert raised.value.code is expected_code


def test_line_requires_exactly_one_positive_side() -> None:
    with pytest.raises(ValidationError):
        JournalEntryLineInput(account_id=10, debit=Decimal("1"), credit=Decimal("1"))


async def test_listing_bounds_lines_and_continues_without_losing_entry() -> None:
    lines = tuple(
        _line(index, "1" if index % 2 else "0", "0" if index % 2 else "1")
        for index in range(1, 102)
    )
    adapter = JournalAdapter(_entry(lines=lines))
    request = JournalEntriesInput(
        company_id=1, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30)
    )

    first = await list_journal_entries(
        adapter, request, company_name="Synthetic Company", request_id="req-1"
    )
    assert len(first.items[0].lines) == 100
    assert first.items[0].lines_truncated is True
    assert first.items[0].lines_cursor is not None

    continued = await list_journal_entries(
        adapter,
        request.model_copy(update={"cursor": first.items[0].lines_cursor}),
        company_name="Synthetic Company",
        request_id="req-2",
    )
    assert [line.line_id for line in continued.items[0].lines] == [101]
    assert continued.items[0].lines_truncated is False


async def test_post_preview_reports_blocking_unbalanced_finding() -> None:
    entry = _entry(lines=(_line(1, "100.02", "0"), _line(2, "0", "100")))
    _state, preview = await prepare_journal_entry_post(
        JournalAdapter(entry), PostJournalEntryInput(company_id=1, move_id=101)
    )

    assert preview.needs_input is True
    assert preview.material_effects["blocking_findings"] == ["JOURNAL_ENTRY_UNBALANCED"]


async def test_post_preview_rejects_substituted_move_identity() -> None:
    entry = _entry().model_copy(update={"id": 202})

    with pytest.raises(OdooMcpError) as raised:
        await prepare_journal_entry_post(
            JournalAdapter(entry),
            PostJournalEntryInput(company_id=1, move_id=101),
        )

    assert raised.value.code is ErrorCode.ODOO_API_ERROR


async def test_post_execution_rejects_substituted_read_back_identity() -> None:
    entry = _entry(state="posted").model_copy(update={"id": 202})

    with pytest.raises(OdooMcpError) as raised:
        await execute_journal_entry_post(JournalAdapter(entry), 1, 101)

    assert raised.value.code is ErrorCode.ODOO_API_ERROR


async def test_post_rejects_non_draft_manual_entry() -> None:
    with pytest.raises(OdooMcpError) as raised:
        await prepare_journal_entry_post(
            JournalAdapter(_entry(state="posted")),
            PostJournalEntryInput(company_id=1, move_id=101),
        )

    assert raised.value.code is ErrorCode.JOURNAL_ENTRY_NOT_DRAFT
