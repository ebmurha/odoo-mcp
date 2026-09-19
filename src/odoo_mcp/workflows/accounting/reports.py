"""Deterministic accounting reports over the typed adapter boundary."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TypeVar

from odoo_mcp.adapters.accounting import (
    Account,
    AccountMoveLine,
    FilterClause,
    PageRequest,
    PartialReconciliation,
    ReadFilters,
)
from odoo_mcp.adapters.base import OdooAdapter
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import (
    AgingBuckets,
    AgingInput,
    AgingItem,
    AgingResponse,
    AgingSummary,
    TrialBalanceInput,
    TrialBalanceItem,
    TrialBalanceResponse,
    TrialBalanceSummary,
)

_SOURCE_PAGE_SIZE = 100
_MAX_SOURCE_RECORDS = 100_000
_ZERO = Decimal("0")
PageItemT = TypeVar("PageItemT")


@dataclass
class _AgingAccumulator:
    name: str
    total: Decimal = _ZERO
    buckets: dict[str, Decimal] = field(default_factory=dict)
    oldest: date | None = None


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True).decode()
        version, raw_offset = decoded.split(":", 1)
        offset = int(raw_offset)
    except (ValueError, UnicodeError, binascii.Error):
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor is invalid.",
            "Restart the report without a cursor.",
        ) from None
    if version != "v1" or offset <= 0:
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor is invalid.",
            "Restart the report without a cursor.",
        )
    return offset


def _next_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"v1:{offset}".encode()).decode().rstrip("=")


def _bounded(count: int) -> None:
    if count > _MAX_SOURCE_RECORDS:
        raise OdooMcpError(
            ErrorCode.ODOO_API_ERROR,
            "The accounting report exceeds the safe processing bound.",
            "Narrow the report filters or period and retry.",
        )


async def _move_lines(
    adapter: OdooAdapter,
    company_id: int,
    filters: ReadFilters,
) -> list[AccountMoveLine]:
    result: list[AccountMoveLine] = []
    cursor: str | None = None
    while True:
        page = await adapter.get_account_move_lines(
            company_id,
            filters,
            PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
        )
        result.extend(page.items)
        _bounded(len(result))
        if page.next_cursor is None:
            return result
        cursor = page.next_cursor


async def _partials(
    adapter: OdooAdapter,
    company_id: int,
    filters: ReadFilters,
) -> list[PartialReconciliation]:
    result: list[PartialReconciliation] = []
    cursor: str | None = None
    while True:
        page = await adapter.get_partial_reconciliations(
            company_id,
            filters,
            PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
        )
        result.extend(page.items)
        _bounded(len(result))
        if page.next_cursor is None:
            return result
        cursor = page.next_cursor


async def _accounts(
    adapter: OdooAdapter,
    company_id: int,
    identifiers: tuple[int, ...],
) -> list[Account]:
    if not identifiers:
        return []
    result: list[Account] = []
    for start in range(0, len(identifiers), _SOURCE_PAGE_SIZE):
        chunk = identifiers[start : start + _SOURCE_PAGE_SIZE]
        cursor: str | None = None
        filters = ReadFilters(clauses=(FilterClause(field="id", operator="in", value=chunk),))
        while True:
            page = await adapter.get_account_accounts(
                company_id,
                filters,
                page=PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
            )
            result.extend(page.items)
            _bounded(len(result))
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    return result


def _page(
    items: list[PageItemT], limit: int, cursor: str | None
) -> tuple[list[PageItemT], str | None]:
    offset = _cursor_offset(cursor)
    if offset > len(items):
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor is outside this report.",
            "Restart the report without a cursor.",
        )
    selected = items[offset : offset + limit]
    next_offset = offset + len(selected)
    return selected, _next_cursor(next_offset) if next_offset < len(items) else None


def _markdown_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _trial_artifact(
    request: TrialBalanceInput,
    company_name: str,
    items: Iterable[TrialBalanceItem],
    summary: TrialBalanceSummary,
    request_id: str,
    page_offset: int,
    page_count: int,
    has_more: bool,
) -> str:
    first_row = page_offset + 1 if page_count else 0
    last_row = page_offset + page_count if page_count else 0
    rows = [
        "# Trial Balance",
        "",
        f"- Period: {request.period_start.isoformat()} to {request.period_end.isoformat()}",
        f"- Company: {_markdown_text(company_name)} ({request.company_id})",
        f"- Audit reference: `{request_id}`",
        f"- Page rows: {first_row}-{last_row} of {summary.account_count}",
        "- Continuation: more rows are available through `next_cursor`"
        if has_more
        else "- Continuation: complete",
        "",
        "| Code | Account | Opening | Debit | Credit | Closing |",
        "|---|---|---:|---:|---:|---:|",
    ]
    rows.extend(
        f"| {_markdown_text(item.code)} | {_markdown_text(item.name)} | "
        f"{item.opening_balance} | {item.period_debit} | {item.period_credit} | "
        f"{item.closing_balance} |"
        for item in items
    )
    rows.extend(
        [
            "",
            f"Whole-report totals: opening {summary.opening_balance}; "
            f"debit {summary.period_debit}; "
            f"credit {summary.period_credit}; closing {summary.closing_balance}.",
        ]
    )
    return "\n".join(rows)


async def get_trial_balance(
    adapter: OdooAdapter,
    request: TrialBalanceInput,
    *,
    company_name: str,
    request_id: str,
) -> TrialBalanceResponse:
    clauses = [
        FilterClause(field="move_id.state", operator="=", value="posted"),
        FilterClause(field="date", operator="<=", value=request.period_end),
        FilterClause(field="account_id", operator="!=", value=False),
    ]
    if request.account_ids:
        clauses.append(FilterClause(field="account_id", operator="in", value=request.account_ids))
    lines = await _move_lines(adapter, request.company_id, ReadFilters(clauses=tuple(clauses)))
    account_ids = tuple(sorted(set(request.account_ids) | {line.account.id for line in lines}))
    accounts = await _accounts(adapter, request.company_id, account_ids)
    account_by_id = {account.id: account for account in accounts}
    if set(account_ids) != set(account_by_id):
        raise OdooMcpError(
            ErrorCode.ACCOUNT_NOT_FOUND,
            "One or more requested accounts are unavailable.",
            "Use account IDs available to the authorized company and retry.",
        )
    values = {
        identifier: {"opening": _ZERO, "debit": _ZERO, "credit": _ZERO}
        for identifier in account_ids
    }
    for line in lines:
        target = values[line.account.id]
        if line.date < request.period_start:
            target["opening"] += line.balance
        else:
            target["debit"] += line.debit
            target["credit"] += line.credit
    all_items = []
    for identifier in account_ids:
        account = account_by_id[identifier]
        value = values[identifier]
        all_items.append(
            TrialBalanceItem(
                account_id=identifier,
                code=account.code,
                name=account.name,
                opening_balance=value["opening"],
                period_debit=value["debit"],
                period_credit=value["credit"],
                closing_balance=value["opening"] + value["debit"] - value["credit"],
            )
        )
    all_items.sort(key=lambda item: (item.code.casefold(), item.account_id))
    summary = TrialBalanceSummary(
        account_count=len(all_items),
        opening_balance=sum((item.opening_balance for item in all_items), _ZERO),
        period_debit=sum((item.period_debit for item in all_items), _ZERO),
        period_credit=sum((item.period_credit for item in all_items), _ZERO),
        closing_balance=sum((item.closing_balance for item in all_items), _ZERO),
    )
    if not request.account_ids and (
        summary.opening_balance != _ZERO
        or summary.period_debit != summary.period_credit
        or summary.closing_balance != _ZERO
    ):
        raise OdooMcpError(
            ErrorCode.ODOO_API_ERROR,
            "The full-company trial balance did not reconcile.",
            "Check Odoo record access and accounting data, then retry.",
        )
    page_offset = _cursor_offset(request.cursor)
    selected, next_cursor = _page(all_items, request.limit, request.cursor)
    artifact = _trial_artifact(
        request,
        company_name,
        selected,
        summary,
        request_id,
        page_offset,
        len(selected),
        next_cursor is not None,
    )
    return TrialBalanceResponse(
        request_id=request_id,
        company_id=request.company_id,
        period_start=request.period_start,
        period_end=request.period_end,
        items=selected,
        next_cursor=next_cursor,
        summary=summary,
        artifact_markdown=artifact,
    )


def _empty_buckets() -> dict[str, Decimal]:
    return {
        "not_yet_due": _ZERO,
        "days_1_30": _ZERO,
        "days_31_60": _ZERO,
        "days_61_90": _ZERO,
        "days_90_plus": _ZERO,
    }


def _bucket(due_date: date, as_of_date: date) -> str:
    overdue_days = (as_of_date - due_date).days
    if overdue_days <= 0:
        return "not_yet_due"
    if overdue_days <= 30:
        return "days_1_30"
    if overdue_days <= 60:
        return "days_31_60"
    if overdue_days <= 90:
        return "days_61_90"
    return "days_90_plus"


def _aging_artifact(
    title: str,
    request: AgingInput,
    company_name: str,
    items: Iterable[AgingItem],
    summary: AgingSummary,
    request_id: str,
    page_offset: int,
    page_count: int,
    has_more: bool,
) -> str:
    first_row = page_offset + 1 if page_count else 0
    last_row = page_offset + page_count if page_count else 0
    rows = [
        f"# {title}",
        "",
        f"- As of: {request.as_of_date.isoformat()}",
        f"- Company: {_markdown_text(company_name)} ({request.company_id})",
        f"- Audit reference: `{request_id}`",
        f"- Page rows: {first_row}-{last_row} of {summary.partner_count}",
        "- Continuation: more rows are available through `next_cursor`"
        if has_more
        else "- Continuation: complete",
        "",
        "| Partner | Total | Not yet due | 1-30 | 31-60 | 61-90 | 90+ | Oldest due |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    rows.extend(
        f"| {_markdown_text(item.partner_name)} | {item.residual_total} | "
        f"{item.buckets.not_yet_due} | {item.buckets.days_1_30} | "
        f"{item.buckets.days_31_60} | {item.buckets.days_61_90} | "
        f"{item.buckets.days_90_plus} | {item.oldest_due_date.isoformat()} |"
        for item in items
    )
    rows.extend(["", f"Whole-report total residual: {summary.residual_total}."])
    return "\n".join(rows)


async def get_aged_balance(
    adapter: OdooAdapter,
    request: AgingInput,
    *,
    company_name: str,
    request_id: str,
    payable: bool,
) -> AgingResponse:
    account_type = "liability_payable" if payable else "asset_receivable"
    clauses = [
        FilterClause(field="move_id.state", operator="=", value="posted"),
        FilterClause(field="date", operator="<=", value=request.as_of_date),
        FilterClause(field="account_id.account_type", operator="=", value=account_type),
    ]
    if request.partner_ids:
        clauses.append(FilterClause(field="partner_id", operator="in", value=request.partner_ids))
    lines = await _move_lines(adapter, request.company_id, ReadFilters(clauses=tuple(clauses)))
    partials = await _partials(
        adapter,
        request.company_id,
        ReadFilters(
            clauses=(FilterClause(field="max_date", operator=">", value=request.as_of_date),)
        ),
    )
    residuals = {line.id: line.residual for line in lines}
    for partial in partials:
        if partial.debit_move_line.id in residuals:
            residuals[partial.debit_move_line.id] += partial.amount
        if partial.credit_move_line.id in residuals:
            residuals[partial.credit_move_line.id] -= partial.amount
    grouped: dict[int, _AgingAccumulator] = {}
    for line in lines:
        residual = residuals[line.id]
        if residual == _ZERO:
            continue
        if line.partner is None:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned an unassigned aging line.",
                "Assign a partner to the receivable or payable line and retry.",
            )
        due_date = line.maturity_date or line.date
        partner = grouped.setdefault(
            line.partner.id,
            _AgingAccumulator(
                name=line.partner.name,
                buckets=_empty_buckets(),
                oldest=due_date,
            ),
        )
        if partner.name != line.partner.name:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned inconsistent partner data.",
                "Check Odoo data integrity and retry.",
            )
        partner.total += residual
        bucket_name = _bucket(due_date, request.as_of_date)
        partner.buckets[bucket_name] += residual
        partner.oldest = due_date if partner.oldest is None else min(partner.oldest, due_date)
    all_items: list[AgingItem] = []
    for partner_id, value in grouped.items():
        if value.oldest is None:
            raise AssertionError("aging accumulator requires a due date")
        all_items.append(
            AgingItem(
                partner_id=partner_id,
                partner_name=value.name,
                residual_total=value.total,
                buckets=AgingBuckets(**value.buckets),
                oldest_due_date=value.oldest,
            )
        )
    all_items.sort(key=lambda item: (item.partner_name.casefold(), item.partner_id))
    total_buckets = _empty_buckets()
    for item in all_items:
        for name, amount in item.buckets.model_dump().items():
            total_buckets[name] += amount
    summary = AgingSummary(
        partner_count=len(all_items),
        residual_total=sum((item.residual_total for item in all_items), _ZERO),
        buckets=AgingBuckets(**total_buckets),
    )
    page_offset = _cursor_offset(request.cursor)
    selected, next_cursor = _page(all_items, request.limit, request.cursor)
    artifact = _aging_artifact(
        "Aged Payables" if payable else "Aged Receivables",
        request,
        company_name,
        selected,
        summary,
        request_id,
        page_offset,
        len(selected),
        next_cursor is not None,
    )
    return AgingResponse(
        request_id=request_id,
        company_id=request.company_id,
        as_of_date=request.as_of_date,
        items=selected,
        next_cursor=next_cursor,
        summary=summary,
        artifact_markdown=artifact,
    )
