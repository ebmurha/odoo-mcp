"""Cashbook reporting over posted cash and bank journal entries."""

from __future__ import annotations

import base64
import binascii
from decimal import Decimal

from odoo_mcp.adapters.accounting import (
    AccountMoveLine,
    FilterClause,
    Journal,
    PageRequest,
    ReadFilters,
)
from odoo_mcp.adapters.base import OdooAdapter
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import (
    CashbookInput,
    CashbookItem,
    CashbookResponse,
    CashbookSummary,
)

_SOURCE_PAGE_SIZE = 100
_MAX_SOURCE_RECORDS = 100_000
_ZERO = Decimal("0")


def _source_error(message: str) -> OdooMcpError:
    return OdooMcpError(
        ErrorCode.ODOO_API_ERROR,
        message,
        "Check Odoo record access and accounting data, then retry.",
    )


async def _journals(adapter: OdooAdapter, company_id: int) -> list[Journal]:
    result: list[Journal] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_ids: set[int] = set()
    while True:
        page = await adapter.get_journals(
            company_id,
            page=PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
        )
        if len(page.items) > _SOURCE_PAGE_SIZE:
            raise _source_error("Odoo returned an oversized cashbook journal page.")
        for item in page.items:
            if item.company_id != company_id or item.id in seen_ids:
                raise _source_error("Odoo returned inconsistent cashbook journal data.")
            seen_ids.add(item.id)
            result.append(item)
        if len(result) > _MAX_SOURCE_RECORDS:
            raise _source_error("The cashbook exceeds the safe processing bound.")
        if page.next_cursor is None:
            return result
        if page.next_cursor in seen_cursors:
            raise _source_error("Odoo returned an invalid cashbook journal page.")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor


async def _lines(
    adapter: OdooAdapter,
    company_id: int,
    filters: ReadFilters,
) -> list[AccountMoveLine]:
    result: list[AccountMoveLine] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_ids: set[int] = set()
    while True:
        page = await adapter.get_account_move_lines(
            company_id,
            filters,
            PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
        )
        if len(page.items) > _SOURCE_PAGE_SIZE:
            raise _source_error("Odoo returned an oversized cashbook line page.")
        for item in page.items:
            if (
                item.company_id != company_id
                or item.id in seen_ids
                or item.debit < _ZERO
                or item.credit < _ZERO
                or item.balance != item.debit - item.credit
            ):
                raise _source_error("Odoo returned inconsistent cashbook line data.")
            seen_ids.add(item.id)
            result.append(item)
        if len(result) > _MAX_SOURCE_RECORDS:
            raise _source_error("The cashbook exceeds the safe processing bound.")
        if page.next_cursor is None:
            return result
        if page.next_cursor in seen_cursors:
            raise _source_error("Odoo returned an invalid cashbook line page.")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        version, raw_offset = (
            base64.b64decode(padded, altchars=b"-_", validate=True).decode().split(":", 1)
        )
        offset = int(raw_offset)
    except (ValueError, UnicodeError, binascii.Error):
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor is invalid.",
            "Restart the cashbook without a cursor.",
        ) from None
    if version != "v1" or offset <= 0:
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor is invalid.",
            "Restart the cashbook without a cursor.",
        )
    return offset


def _next_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"v1:{offset}".encode()).decode().rstrip("=")


def _markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _artifact(
    request: CashbookInput,
    company_name: str,
    items: list[CashbookItem],
    summary: CashbookSummary,
    request_id: str,
    offset: int,
    has_more: bool,
) -> str:
    first = offset + 1 if items else 0
    last = offset + len(items) if items else 0
    rows = [
        "# Cashbook",
        "",
        f"- Period: {request.period_start.isoformat()} to {request.period_end.isoformat()}",
        f"- Company: {_markdown(company_name)} ({request.company_id})",
        f"- Audit reference: `{request_id}`",
        f"- Page rows: {first}-{last} of {summary.transaction_count}",
        "- Continuation: more rows are available through `next_cursor`"
        if has_more
        else "- Continuation: complete",
        "",
        "| Date | Journal | Reference | Partner | Debit | Credit | Amount |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    rows.extend(
        f"| {item.date.isoformat()} | {_markdown(item.journal_name)} | "
        f"{_markdown(item.reference or '')} | {_markdown(item.partner_name or '')} | "
        f"{item.debit} | {item.credit} | {item.amount} |"
        for item in items
    )
    rows.extend(
        [
            "",
            f"Whole-report totals: opening {summary.opening_balance}; "
            f"debit {summary.total_debit}; credit {summary.total_credit}; "
            f"closing {summary.closing_balance}.",
        ]
    )
    return "\n".join(rows)


async def get_cashbook(
    adapter: OdooAdapter,
    request: CashbookInput,
    *,
    company_name: str,
    request_id: str,
) -> CashbookResponse:
    available = await _journals(adapter, request.company_id)
    cash_journals = {item.id: item for item in available if item.journal_type in {"bank", "cash"}}
    if request.journal_ids:
        missing = set(request.journal_ids) - set(cash_journals)
        if missing:
            raise OdooMcpError(
                ErrorCode.JOURNAL_NOT_FOUND,
                "One or more requested cash or bank journals are unavailable.",
                "Use cash or bank journal IDs available to the authorized company.",
            )
        journal_ids = request.journal_ids
    else:
        journal_ids = tuple(sorted(cash_journals))

    lines: list[AccountMoveLine] = []
    if journal_ids:
        clauses = [
            FilterClause(field="move_id.state", operator="=", value="posted"),
            FilterClause(field="date", operator="<=", value=request.period_end),
            FilterClause(field="journal_id", operator="in", value=journal_ids),
            FilterClause(field="account_id.account_type", operator="=", value="asset_cash"),
        ]
        if request.partner_ids:
            clauses.append(
                FilterClause(field="partner_id", operator="in", value=request.partner_ids)
            )
        lines = await _lines(adapter, request.company_id, ReadFilters(clauses=tuple(clauses)))

    selected_journals = set(journal_ids)
    filtered = [
        line
        for line in lines
        if line.journal.id in selected_journals
        and line.date <= request.period_end
        and (not request.partner_ids or (line.partner and line.partner.id in request.partner_ids))
    ]
    opening = sum(
        (line.balance for line in filtered if line.date < request.period_start),
        _ZERO,
    )
    period_lines = [line for line in filtered if line.date >= request.period_start]
    all_items = [
        CashbookItem(
            line_id=line.id,
            move_id=line.move.id,
            journal_id=line.journal.id,
            journal_name=cash_journals[line.journal.id].name,
            date=line.date,
            partner_id=None if line.partner is None else line.partner.id,
            partner_name=None if line.partner is None else line.partner.name,
            reference=line.label,
            currency_id=None,
            currency_name="Company currency",
            debit=line.debit,
            credit=line.credit,
            amount=line.balance,
        )
        for line in period_lines
    ]
    all_items.sort(key=lambda item: (item.date, item.move_id, item.line_id))
    debit = sum((item.debit for item in all_items), _ZERO)
    credit = sum((item.credit for item in all_items), _ZERO)
    summary = CashbookSummary(
        transaction_count=len(all_items),
        opening_balance=opening,
        total_debit=debit,
        total_credit=credit,
        closing_balance=opening + debit - credit,
    )
    offset = _cursor_offset(request.cursor)
    if offset > len(all_items):
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor is outside this cashbook.",
            "Restart the cashbook without a cursor.",
        )
    items = all_items[offset : offset + request.limit]
    next_offset = offset + len(items)
    next_cursor = _next_cursor(next_offset) if next_offset < len(all_items) else None
    return CashbookResponse(
        request_id=request_id,
        company_id=request.company_id,
        period_start=request.period_start,
        period_end=request.period_end,
        items=items,
        next_cursor=next_cursor,
        summary=summary,
        artifact_markdown=_artifact(
            request,
            company_name,
            items,
            summary,
            request_id,
            offset,
            next_cursor is not None,
        ),
    )
