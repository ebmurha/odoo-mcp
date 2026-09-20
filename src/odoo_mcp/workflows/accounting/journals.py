"""Journal-entry listing, draft creation, and explicit posting workflows."""

from __future__ import annotations

import base64
import binascii
from decimal import ROUND_HALF_UP, Decimal

from odoo_mcp.adapters.accounting import (
    AccountMove,
    FilterClause,
    JournalEntry,
    JournalEntryDraft,
    JournalEntryDraftLine,
    PageRequest,
    ReadFilters,
    RelatedRecord,
)
from odoo_mcp.adapters.base import OdooAdapter
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import (
    CreateJournalEntryInput,
    JournalEntriesInput,
    JournalEntriesResponse,
    JournalEntriesSummary,
    JournalEntryItem,
    JournalEntryLineItem,
    PostJournalEntryInput,
)
from odoo_mcp.policy.write_safety import AppliedWrite, PreparedWrite

_ZERO = Decimal("0")
_LINE_PAGE_SIZE = 100
_SOURCE_PAGE_SIZE = 500
_MAX_SOURCE_ENTRIES = 100_000


def _error(code: ErrorCode, message: str, hint: str) -> OdooMcpError:
    return OdooMcpError(code, message, hint)


def _encode_cursor(kind: str, *values: int) -> str:
    raw = ":".join(("v1", kind, *(str(value) for value in values)))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[str, tuple[int, ...]]:
    if cursor is None:
        return "entries", (0,)
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        parts = base64.b64decode(padded, altchars=b"-_", validate=True).decode().split(":")
        values = tuple(int(value) for value in parts[2:])
    except (ValueError, UnicodeError, binascii.Error):
        raise _error(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor is invalid.",
            "Restart the journal listing without a cursor.",
        ) from None
    if (
        len(parts) < 3
        or parts[0] != "v1"
        or parts[1] not in {"entries", "lines"}
        or any(value < 0 for value in values)
        or (parts[1] == "entries" and len(values) != 1)
        or (parts[1] == "lines" and (len(values) != 2 or values[0] <= 0 or values[1] <= 0))
    ):
        raise _error(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor is invalid.",
            "Restart the journal listing without a cursor.",
        )
    return parts[1], values


def _analytic_ids(distribution: dict[str, Decimal]) -> list[int]:
    result: set[int] = set()
    try:
        for key in distribution:
            identifiers = [int(value) for value in key.split(",")]
            if not identifiers or any(value <= 0 for value in identifiers):
                raise ValueError
            result.update(identifiers)
    except ValueError:
        raise _error(
            ErrorCode.ODOO_API_ERROR,
            "Odoo returned an invalid analytic distribution.",
            "Check the journal entry analytic data and retry.",
        ) from None
    return sorted(result)


def _line_item(line: object) -> JournalEntryLineItem:
    from odoo_mcp.adapters.accounting import JournalEntryLine

    if not isinstance(line, JournalEntryLine):
        raise TypeError("invalid journal entry line")
    return JournalEntryLineItem(
        line_id=line.id,
        account_id=line.account.id,
        account_name=line.account.name,
        partner_id=line.partner.id if line.partner else None,
        partner_name=line.partner.name if line.partner else None,
        description=line.description,
        debit=line.debit,
        credit=line.credit,
        analytic_ids=_analytic_ids(line.analytic_distribution),
    )


def _entry_item(entry: JournalEntry, line_offset: int = 0) -> JournalEntryItem:
    lines = entry.lines[line_offset : line_offset + _LINE_PAGE_SIZE]
    next_offset = line_offset + len(lines)
    return JournalEntryItem(
        move_id=entry.id,
        name=entry.name,
        date=entry.date,
        journal_id=entry.journal.id,
        journal_name=entry.journal.name,
        state=entry.state,  # type: ignore[arg-type]
        reference=entry.reference,
        currency_id=entry.currency.id,
        currency_name=entry.currency.name,
        total_debit=sum((line.debit for line in entry.lines), _ZERO),
        total_credit=sum((line.credit for line in entry.lines), _ZERO),
        lines=[_line_item(line) for line in lines],
        lines_truncated=next_offset < len(entry.lines),
        lines_cursor=(
            _encode_cursor("lines", entry.id, next_offset)
            if next_offset < len(entry.lines)
            else None
        ),
    )


async def _moves(adapter: OdooAdapter, request: JournalEntriesInput) -> list[AccountMove]:
    clauses = [
        FilterClause(field="move_type", operator="=", value="entry"),
        FilterClause(field="date", operator=">=", value=request.period_start),
        FilterClause(field="date", operator="<=", value=request.period_end),
    ]
    if request.journal_ids:
        clauses.append(FilterClause(field="journal_id", operator="in", value=request.journal_ids))
    clauses.append(
        FilterClause(
            field="state",
            operator="in",
            value=request.states or ("draft", "posted"),
        )
    )
    result: list[AccountMove] = []
    cursor: str | None = None
    while True:
        page = await adapter.get_account_moves(
            request.company_id,
            ReadFilters(clauses=tuple(clauses)),
            PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
        )
        result.extend(page.items)
        if len(result) > _MAX_SOURCE_ENTRIES:
            raise _error(
                ErrorCode.ODOO_API_ERROR,
                "The journal listing exceeds the safe processing bound.",
                "Narrow the period or filters and retry.",
            )
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    result.sort(key=lambda move: (move.date, move.id))
    return result


def _artifact(
    request: JournalEntriesInput,
    company_name: str,
    items: list[JournalEntryItem],
    summary: JournalEntriesSummary,
    request_id: str,
) -> str:
    safe_company = company_name.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    rows = [
        "# Journal Entries",
        "",
        f"- Period: {request.period_start.isoformat()} to {request.period_end.isoformat()}",
        f"- Company: {safe_company} ({request.company_id})",
        f"- Audit reference: `{request_id}`",
        f"- Whole-report entries: {summary.entry_count}",
        "",
        "| Date | Entry | Journal | State | Debit | Credit |",
        "|---|---|---|---|---:|---:|",
    ]
    for item in items:
        safe_name = item.name.replace("|", "\\|")
        safe_journal = item.journal_name.replace("|", "\\|")
        rows.append(
            f"| {item.date.isoformat()} | {safe_name} | {safe_journal} | {item.state} | "
            f"{item.total_debit} | {item.total_credit} |"
        )
    return "\n".join(rows)


async def list_journal_entries(
    adapter: OdooAdapter,
    request: JournalEntriesInput,
    *,
    company_name: str,
    request_id: str,
) -> JournalEntriesResponse:
    moves = await _moves(adapter, request)
    summary = JournalEntriesSummary(
        entry_count=len(moves),
        draft_count=sum(move.state == "draft" for move in moves),
        posted_count=sum(move.state == "posted" for move in moves),
    )
    kind, values = _decode_cursor(request.cursor)
    if kind == "lines":
        move_id, offset = values
        if move_id not in {move.id for move in moves}:
            raise _error(
                ErrorCode.INVALID_INPUT,
                "The line cursor is outside this journal listing.",
                "Restart the journal listing without a cursor.",
            )
        entries = [await adapter.get_journal_entry(request.company_id, move_id)]
        if offset >= len(entries[0].lines):
            raise _error(
                ErrorCode.INVALID_INPUT,
                "The line cursor is outside this journal entry.",
                "Restart the journal listing without a cursor.",
            )
        items = [_entry_item(entries[0], offset)]
        next_cursor = None
    else:
        offset = values[0]
        if offset > len(moves):
            raise _error(
                ErrorCode.INVALID_INPUT,
                "The pagination cursor is outside this journal listing.",
                "Restart the journal listing without a cursor.",
            )
        selected = moves[offset : offset + request.limit]
        entries = [
            await adapter.get_journal_entry(request.company_id, move.id) for move in selected
        ]
        items = [_entry_item(entry) for entry in entries]
        next_offset = offset + len(selected)
        next_cursor = _encode_cursor("entries", next_offset) if next_offset < len(moves) else None
    return JournalEntriesResponse(
        request_id=request_id,
        company_id=request.company_id,
        period_start=request.period_start,
        period_end=request.period_end,
        items=items,
        next_cursor=next_cursor,
        summary=summary,
        artifact_markdown=_artifact(request, company_name, items, summary, request_id),
    )


async def _currency_rounding(
    adapter: OdooAdapter, company_id: int
) -> tuple[RelatedRecord, Decimal]:
    companies = await adapter.get_companies()
    company = next((value for value in companies if value.id == company_id), None)
    if company is None or company.currency is None:
        raise _error(
            ErrorCode.ODOO_API_ERROR,
            "Odoo returned incomplete company currency data.",
            "Configure the company currency and retry.",
        )
    page = await adapter.get_currencies(
        company_id, (company.currency.id,), page=PageRequest(limit=1)
    )
    if len(page.items) != 1 or page.items[0].id != company.currency.id:
        raise _error(
            ErrorCode.ODOO_API_ERROR,
            "Odoo returned incomplete currency precision data.",
            "Correct the company currency and retry.",
        )
    return company.currency, page.items[0].rounding


def _rounded(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * increment


async def prepare_journal_entry_draft(
    adapter: OdooAdapter, request: CreateJournalEntryInput
) -> tuple[JournalEntryDraft, PreparedWrite]:
    journals = []
    cursor: str | None = None
    while True:
        page = await adapter.get_journals(
            request.company_id, page=PageRequest(limit=500, cursor=cursor)
        )
        journals.extend(page.items)
        if len(journals) > 10_000:
            raise _error(
                ErrorCode.ODOO_API_ERROR,
                "The journal selection exceeds the safe processing bound.",
                "Reduce accessible journals or contact the service operator.",
            )
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    journal = next((value for value in journals if value.id == request.journal_id), None)
    if journal is None:
        raise _error(
            ErrorCode.JOURNAL_NOT_FOUND,
            "The requested journal is unavailable.",
            "Use an exact journal ID from the authorized company.",
        )
    account_ids = tuple(sorted({line.account_id for line in request.lines}))
    accounts = await adapter.get_account_accounts(
        request.company_id,
        ReadFilters(clauses=(FilterClause(field="id", operator="in", value=account_ids),)),
        page=PageRequest(limit=len(account_ids)),
    )
    if {value.id for value in accounts.items} != set(account_ids):
        raise _error(
            ErrorCode.ACCOUNT_NOT_FOUND,
            "One or more journal accounts are unavailable.",
            "Use exact account IDs from the authorized company.",
        )
    partner_ids = tuple(sorted({line.partner_id for line in request.lines if line.partner_id}))
    if partner_ids:
        partners = await adapter.get_partners(
            request.company_id,
            ReadFilters(clauses=(FilterClause(field="id", operator="in", value=partner_ids),)),
            page=PageRequest(limit=len(partner_ids)),
        )
        if {value.id for value in partners.items} != set(partner_ids):
            raise _error(
                ErrorCode.INVALID_INPUT,
                "One or more journal partners are unavailable.",
                "Use exact partner IDs from the authorized company.",
            )
    analytic_ids = tuple(
        sorted({line.analytic_account_id for line in request.lines if line.analytic_account_id})
    )
    if analytic_ids:
        analytics = await adapter.get_analytic_accounts(
            request.company_id,
            ReadFilters(clauses=(FilterClause(field="id", operator="in", value=analytic_ids),)),
            page=PageRequest(limit=len(analytic_ids)),
        )
        if {value.id for value in analytics.items} != set(analytic_ids):
            raise _error(
                ErrorCode.INVALID_INPUT,
                "One or more analytic accounts are unavailable.",
                "Use exact analytic account IDs from the authorized company.",
            )
    currency, increment = await _currency_rounding(adapter, request.company_id)
    total_debit = sum((line.debit for line in request.lines), _ZERO)
    total_credit = sum((line.credit for line in request.lines), _ZERO)
    if _rounded(total_debit, increment) != _rounded(total_credit, increment):
        raise _error(
            ErrorCode.JOURNAL_ENTRY_UNBALANCED,
            "The journal entry is not balanced at company-currency precision.",
            "Make total debits equal total credits and retry.",
        )
    draft = JournalEntryDraft(
        company_id=request.company_id,
        journal_id=request.journal_id,
        entry_date=request.entry_date,
        reference=request.reference.strip() if request.reference else None,
        lines=tuple(
            JournalEntryDraftLine(
                account_id=line.account_id,
                partner_id=line.partner_id,
                description=line.description.strip() if line.description else None,
                debit=line.debit,
                credit=line.credit,
                analytic_account_id=line.analytic_account_id,
            )
            for line in request.lines
        ),
    )
    effects = {
        "draft": draft.model_dump(mode="json"),
        "journal": journal.model_dump(mode="json"),
        "currency": currency.model_dump(mode="json"),
        "total_debit": str(total_debit),
        "total_credit": str(total_credit),
        "currency_rounding": str(increment),
        "balanced": True,
    }
    return draft, PreparedWrite(
        proposed_action={"action": "create_draft_manual_journal_entry", "posts_entry": False},
        material_effects=effects,
    )


def _entry_effect(entry: JournalEntry) -> dict[str, object]:
    item = _entry_item(entry)
    item = item.model_copy(
        update={
            "lines": [_line_item(line) for line in entry.lines],
            "lines_truncated": False,
            "lines_cursor": None,
        }
    )
    return item.model_dump(mode="json")


async def execute_journal_entry_draft(
    adapter: OdooAdapter, draft: JournalEntryDraft
) -> AppliedWrite:
    entry = await adapter.create_journal_entry_draft(draft)
    return AppliedWrite(
        material_effects=_entry_effect(entry), record_refs=(f"account.move:{entry.id}",)
    )


async def prepare_journal_entry_post(
    adapter: OdooAdapter, request: PostJournalEntryInput
) -> tuple[JournalEntry, PreparedWrite]:
    entry = await adapter.get_journal_entry(request.company_id, request.move_id)
    if entry.move_type != "entry" or entry.state != "draft":
        raise _error(
            ErrorCode.JOURNAL_ENTRY_NOT_DRAFT,
            "The journal entry is not an eligible draft manual entry.",
            "Use an existing draft manual journal entry from the authorized company.",
        )
    _currency, increment = await _currency_rounding(adapter, request.company_id)
    debit = sum((line.debit for line in entry.lines), _ZERO)
    credit = sum((line.credit for line in entry.lines), _ZERO)
    findings: list[str] = []
    if len(entry.lines) < 2:
        findings.append("TOO_FEW_LINES")
    if any(
        line.debit < 0 or line.credit < 0 or (line.debit > 0) == (line.credit > 0)
        for line in entry.lines
    ):
        findings.append("LINE_AMOUNT_INVALID")
    if _rounded(debit, increment) != _rounded(credit, increment):
        findings.append("JOURNAL_ENTRY_UNBALANCED")
    return entry, PreparedWrite(
        proposed_action={"action": "post_existing_draft_manual_entry", "move_id": entry.id},
        material_effects={
            "entry": _entry_effect(entry),
            "blocking_findings": findings,
        },
        needs_input=bool(findings),
    )


async def execute_journal_entry_post(
    adapter: OdooAdapter, company_id: int, move_id: int
) -> AppliedWrite:
    entry = await adapter.post_journal_entry(company_id, move_id)
    return AppliedWrite(
        material_effects=_entry_effect(entry), record_refs=(f"account.move:{entry.id}",)
    )
