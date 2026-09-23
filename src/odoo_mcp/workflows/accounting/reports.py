"""Deterministic accounting reports over the typed adapter boundary."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, TypeVar

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
    BalanceSheetInput,
    BalanceSheetItem,
    BalanceSheetResponse,
    BalanceSheetSummary,
    ProfitAndLossInput,
    ProfitAndLossItem,
    ProfitAndLossResponse,
    ProfitAndLossSummary,
    TrialBalanceInput,
    TrialBalanceItem,
    TrialBalanceResponse,
    TrialBalanceSummary,
)

_SOURCE_PAGE_SIZE = 100
_MAX_SOURCE_RECORDS = 100_000
_ZERO = Decimal("0")
_ONE_HUNDRED = Decimal("100")
_INCOME_TYPES = frozenset({"income", "income_other"})
_EXPENSE_TYPES = frozenset({"expense", "expense_depreciation", "expense_direct_cost"})
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
    seen_ids: set[int] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None
    while True:
        if cursor is not None:
            if cursor in seen_cursors:
                raise _invalid_report_response()
            seen_cursors.add(cursor)
        page = await adapter.get_account_move_lines(
            company_id,
            filters,
            PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
        )
        if page.next_cursor is not None and not page.items:
            raise _invalid_report_response()
        if any(item.id in seen_ids for item in page.items):
            raise _invalid_report_response()
        seen_ids.update(item.id for item in page.items)
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
    seen_ids: set[int] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None
    while True:
        if cursor is not None:
            if cursor in seen_cursors:
                raise _invalid_report_response()
            seen_cursors.add(cursor)
        page = await adapter.get_partial_reconciliations(
            company_id,
            filters,
            PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
        )
        if page.next_cursor is not None and not page.items:
            raise _invalid_report_response()
        if any(item.id in seen_ids for item in page.items):
            raise _invalid_report_response()
        seen_ids.update(item.id for item in page.items)
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
    seen_ids: set[int] = set()
    for start in range(0, len(identifiers), _SOURCE_PAGE_SIZE):
        chunk = identifiers[start : start + _SOURCE_PAGE_SIZE]
        cursor: str | None = None
        filters = ReadFilters(clauses=(FilterClause(field="id", operator="in", value=chunk),))
        seen_cursors: set[str] = set()
        while True:
            if cursor is not None:
                if cursor in seen_cursors:
                    raise _invalid_report_response()
                seen_cursors.add(cursor)
            page = await adapter.get_account_accounts(
                company_id,
                filters,
                page=PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
            )
            if page.next_cursor is not None and not page.items:
                raise _invalid_report_response()
            if any(item.id in seen_ids for item in page.items):
                raise _invalid_report_response()
            seen_ids.update(item.id for item in page.items)
            result.extend(page.items)
            _bounded(len(result))
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    return result


async def _validate_analytic_accounts(
    adapter: OdooAdapter,
    company_id: int,
    identifiers: tuple[int, ...],
) -> None:
    if not identifiers:
        return
    found: set[int] = set()
    for start in range(0, len(identifiers), _SOURCE_PAGE_SIZE):
        chunk = identifiers[start : start + _SOURCE_PAGE_SIZE]
        cursor: str | None = None
        filters = ReadFilters(clauses=(FilterClause(field="id", operator="in", value=chunk),))
        seen_cursors: set[str] = set()
        while True:
            if cursor is not None:
                if cursor in seen_cursors:
                    raise _invalid_report_response()
                seen_cursors.add(cursor)
            page = await adapter.get_analytic_accounts(
                company_id,
                filters,
                page=PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
            )
            if page.next_cursor is not None and not page.items:
                raise _invalid_report_response()
            if any(item.id not in chunk or item.id in found for item in page.items):
                raise _invalid_report_response()
            found.update(item.id for item in page.items)
            _bounded(len(found))
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    if found != set(identifiers):
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "One or more analytic accounts are unavailable.",
            "Use exact analytic account IDs from the authorized company.",
        )


async def _currency_rounding(adapter: OdooAdapter, company_id: int, currency_id: int) -> Decimal:
    page = await adapter.get_currencies(
        company_id,
        (currency_id,),
        page=PageRequest(limit=1),
    )
    if len(page.items) != 1 or page.items[0].id != currency_id or page.next_cursor is not None:
        raise _invalid_report_response()
    return page.items[0].rounding


def _invalid_report_response() -> OdooMcpError:
    return OdooMcpError(
        ErrorCode.ODOO_API_ERROR,
        "Odoo returned invalid accounting report data.",
        "Check Odoo data integrity and record access, then retry.",
    )


def _rounded(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * increment


def _analytic_factor(distribution: dict[str, Decimal], requested: frozenset[int]) -> Decimal:
    if not requested:
        return Decimal("1")
    selected = _ZERO
    total = _ZERO
    try:
        for key, percentage in distribution.items():
            identifiers = {int(value) for value in key.split(",")}
            if not identifiers or any(identifier <= 0 for identifier in identifiers):
                raise ValueError
            if percentage < _ZERO or percentage > _ONE_HUNDRED:
                raise ValueError
            total += percentage
            if identifiers & requested:
                selected += percentage
    except (ValueError, ArithmeticError):
        raise _invalid_report_response() from None
    if total > _ONE_HUNDRED or selected > _ONE_HUNDRED:
        raise _invalid_report_response()
    return selected / _ONE_HUNDRED


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


def _account_map(accounts: list[Account], identifiers: set[int]) -> dict[int, Account]:
    result: dict[int, Account] = {}
    for account in accounts:
        if account.id not in identifiers or account.id in result:
            raise _invalid_report_response()
        result[account.id] = account
    if set(result) != identifiers:
        raise _invalid_report_response()
    return result


def _pnl_group(account_type: str) -> Literal["income", "expense"] | None:
    if account_type in _INCOME_TYPES:
        return "income"
    if account_type in _EXPENSE_TYPES:
        return "expense"
    if (
        account_type.startswith("asset_")
        or account_type.startswith("liability_")
        or account_type in {"equity", "equity_unaffected", "off_balance"}
    ):
        return None
    raise _invalid_report_response()


def _balance_group(account_type: str) -> Literal["asset", "liability", "equity"] | None:
    if account_type.startswith("asset_"):
        return "asset"
    if account_type.startswith("liability_"):
        return "liability"
    if account_type in {"equity", "equity_unaffected"}:
        return "equity"
    if (
        account_type in _INCOME_TYPES
        or account_type in _EXPENSE_TYPES
        or account_type == "off_balance"
    ):
        return None
    raise _invalid_report_response()


def _validate_line_amounts(lines: Iterable[AccountMoveLine], rounding: Decimal) -> None:
    if any(_rounded(line.balance - line.debit + line.credit, rounding) != _ZERO for line in lines):
        raise _invalid_report_response()


def _validate_statement_lines(
    lines: Iterable[AccountMoveLine], *, start: date | None, end: date
) -> None:
    if any(
        line.move_state != "posted" or line.date > end or (start is not None and line.date < start)
        for line in lines
    ):
        raise _invalid_report_response()


def _pnl_artifact(
    request: ProfitAndLossInput,
    company_name: str,
    items: Iterable[ProfitAndLossItem],
    summary: ProfitAndLossSummary,
    request_id: str,
    page_offset: int,
    page_count: int,
    has_more: bool,
) -> str:
    first_row = page_offset + 1 if page_count else 0
    last_row = page_offset + page_count if page_count else 0
    rows = [
        "# Profit and Loss",
        "",
        f"- Period: {request.period_start.isoformat()} to {request.period_end.isoformat()}",
        f"- Company: {_markdown_text(company_name)} ({request.company_id})",
        f"- Audit reference: `{request_id}`",
        f"- Page rows: {first_row}-{last_row} of {summary.account_count}",
        "- Continuation: more rows are available through `next_cursor`"
        if has_more
        else "- Continuation: complete",
        "",
        "| Group | Code | Account | Type | Debit | Credit | Signed balance |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    rows.extend(
        f"| {item.group} | {_markdown_text(item.code)} | {_markdown_text(item.name)} | "
        f"{_markdown_text(item.account_type)} | {item.debit} | {item.credit} | "
        f"{item.signed_balance} |"
        for item in items
    )
    rows.extend(
        [
            "",
            f"Whole-report totals: income {summary.income_balance}; "
            f"expenses {summary.expense_balance}; net profit or loss {summary.net_profit}.",
        ]
    )
    return "\n".join(rows)


async def get_profit_and_loss(
    adapter: OdooAdapter,
    request: ProfitAndLossInput,
    *,
    company_currency_id: int,
    company_name: str,
    request_id: str,
) -> ProfitAndLossResponse:
    await _validate_analytic_accounts(adapter, request.company_id, request.analytic_account_ids)
    rounding = await _currency_rounding(adapter, request.company_id, company_currency_id)
    lines = await _move_lines(
        adapter,
        request.company_id,
        ReadFilters(
            clauses=(
                FilterClause(field="move_id.state", operator="=", value="posted"),
                FilterClause(field="date", operator=">=", value=request.period_start),
                FilterClause(field="date", operator="<=", value=request.period_end),
                FilterClause(field="account_id", operator="!=", value=False),
            )
        ),
    )
    _validate_statement_lines(lines, start=request.period_start, end=request.period_end)
    _validate_line_amounts(lines, rounding)
    if _rounded(sum((line.balance for line in lines), _ZERO), rounding) != _ZERO:
        raise OdooMcpError(
            ErrorCode.ODOO_API_ERROR,
            "The full-company profit and loss source did not reconcile.",
            "Check Odoo record access and accounting data, then retry.",
        )
    identifiers = {line.account.id for line in lines}
    accounts = _account_map(
        await _accounts(adapter, request.company_id, tuple(sorted(identifiers))), identifiers
    )
    requested_analytics = frozenset(request.analytic_account_ids)
    values: dict[int, dict[str, Decimal]] = {}
    groups: dict[int, Literal["income", "expense"]] = {}
    for line in lines:
        account = accounts[line.account.id]
        group = _pnl_group(account.account_type)
        if group is None:
            continue
        factor = _analytic_factor(line.analytic_distribution, requested_analytics)
        if factor == _ZERO:
            continue
        value = values.setdefault(line.account.id, {"debit": _ZERO, "credit": _ZERO})
        value["debit"] += line.debit * factor
        value["credit"] += line.credit * factor
        groups[line.account.id] = group
    all_items: list[ProfitAndLossItem] = []
    for identifier, value in values.items():
        account = accounts[identifier]
        group = groups[identifier]
        debit = _rounded(value["debit"], rounding)
        credit = _rounded(value["credit"], rounding)
        signed_balance = credit - debit if group == "income" else debit - credit
        all_items.append(
            ProfitAndLossItem(
                account_id=identifier,
                code=account.code,
                name=account.name,
                account_type=account.account_type,
                group=group,
                debit=debit,
                credit=credit,
                signed_balance=signed_balance,
            )
        )
    group_order = {"income": 0, "expense": 1}
    all_items.sort(
        key=lambda item: (group_order[item.group], item.code.casefold(), item.account_id)
    )
    income_items = [item for item in all_items if item.group == "income"]
    expense_items = [item for item in all_items if item.group == "expense"]
    income_balance = sum((item.signed_balance for item in income_items), _ZERO)
    expense_balance = sum((item.signed_balance for item in expense_items), _ZERO)
    summary = ProfitAndLossSummary(
        account_count=len(all_items),
        income_debit=sum((item.debit for item in income_items), _ZERO),
        income_credit=sum((item.credit for item in income_items), _ZERO),
        income_balance=income_balance,
        expense_debit=sum((item.debit for item in expense_items), _ZERO),
        expense_credit=sum((item.credit for item in expense_items), _ZERO),
        expense_balance=expense_balance,
        net_profit=income_balance - expense_balance,
    )
    page_offset = _cursor_offset(request.cursor)
    selected, next_cursor = _page(all_items, request.limit, request.cursor)
    artifact = _pnl_artifact(
        request,
        company_name,
        selected,
        summary,
        request_id,
        page_offset,
        len(selected),
        next_cursor is not None,
    )
    return ProfitAndLossResponse(
        request_id=request_id,
        company_id=request.company_id,
        period_start=request.period_start,
        period_end=request.period_end,
        items=selected,
        next_cursor=next_cursor,
        summary=summary,
        artifact_markdown=artifact,
    )


def _balance_sheet_artifact(
    request: BalanceSheetInput,
    company_name: str,
    items: Iterable[BalanceSheetItem],
    summary: BalanceSheetSummary,
    request_id: str,
    page_offset: int,
    page_count: int,
    has_more: bool,
) -> str:
    first_row = page_offset + 1 if page_count else 0
    last_row = page_offset + page_count if page_count else 0
    rows = [
        "# Balance Sheet",
        "",
        f"- As of: {request.as_of_date.isoformat()}",
        f"- Company: {_markdown_text(company_name)} ({request.company_id})",
        f"- Audit reference: `{request_id}`",
        f"- Page rows: {first_row}-{last_row} of {summary.account_count}",
        "- Continuation: more rows are available through `next_cursor`"
        if has_more
        else "- Continuation: complete",
        "",
        "| Group | Code | Account | Type | Signed balance |",
        "|---|---|---|---|---:|",
    ]
    rows.extend(
        f"| {item.group} | {_markdown_text(item.code)} | {_markdown_text(item.name)} | "
        f"{_markdown_text(item.account_type)} | {item.signed_balance} |"
        for item in items
    )
    check = "balanced" if summary.is_balanced else f"difference {summary.balancing_difference}"
    rows.extend(
        [
            "",
            f"Whole-report totals: assets {summary.total_assets}; "
            f"liabilities {summary.total_liabilities}; equity {summary.total_equity} "
            f"(including unclosed earnings {summary.unclosed_earnings}).",
            f"Balancing check: {check}.",
        ]
    )
    return "\n".join(rows)


async def get_balance_sheet(
    adapter: OdooAdapter,
    request: BalanceSheetInput,
    *,
    company_currency_id: int,
    company_name: str,
    request_id: str,
) -> BalanceSheetResponse:
    await _validate_analytic_accounts(adapter, request.company_id, request.analytic_account_ids)
    rounding = await _currency_rounding(adapter, request.company_id, company_currency_id)
    lines = await _move_lines(
        adapter,
        request.company_id,
        ReadFilters(
            clauses=(
                FilterClause(field="move_id.state", operator="=", value="posted"),
                FilterClause(field="date", operator="<=", value=request.as_of_date),
                FilterClause(field="account_id", operator="!=", value=False),
            )
        ),
    )
    _validate_statement_lines(lines, start=None, end=request.as_of_date)
    _validate_line_amounts(lines, rounding)
    if _rounded(sum((line.balance for line in lines), _ZERO), rounding) != _ZERO:
        raise OdooMcpError(
            ErrorCode.ODOO_API_ERROR,
            "The full-company balance sheet source did not reconcile.",
            "Check Odoo record access and accounting data, then retry.",
        )
    identifiers = {line.account.id for line in lines}
    accounts = _account_map(
        await _accounts(adapter, request.company_id, tuple(sorted(identifiers))), identifiers
    )
    requested_analytics = frozenset(request.analytic_account_ids)
    balances: dict[int, Decimal] = {}
    for line in lines:
        factor = _analytic_factor(line.analytic_distribution, requested_analytics)
        if factor != _ZERO:
            balances[line.account.id] = balances.get(line.account.id, _ZERO) + line.balance * factor
    all_items: list[BalanceSheetItem] = []
    unclosed_earnings = _ZERO
    for identifier, raw_balance in balances.items():
        account = accounts[identifier]
        balance = _rounded(raw_balance, rounding)
        group = _balance_group(account.account_type)
        if group is None:
            if account.account_type in _INCOME_TYPES:
                unclosed_earnings += -balance
            elif account.account_type in _EXPENSE_TYPES:
                unclosed_earnings -= balance
            continue
        signed_balance = balance if group == "asset" else -balance
        all_items.append(
            BalanceSheetItem(
                account_id=identifier,
                code=account.code,
                name=account.name,
                account_type=account.account_type,
                group=group,
                signed_balance=signed_balance,
            )
        )
    group_order = {"asset": 0, "liability": 1, "equity": 2}
    all_items.sort(
        key=lambda item: (group_order[item.group], item.code.casefold(), item.account_id)
    )
    total_assets = sum((item.signed_balance for item in all_items if item.group == "asset"), _ZERO)
    total_liabilities = sum(
        (item.signed_balance for item in all_items if item.group == "liability"), _ZERO
    )
    equity_account_balance = sum(
        (item.signed_balance for item in all_items if item.group == "equity"), _ZERO
    )
    unclosed_earnings = _rounded(unclosed_earnings, rounding)
    total_equity = equity_account_balance + unclosed_earnings
    difference = _rounded(total_assets - total_liabilities - total_equity, rounding)
    summary = BalanceSheetSummary(
        account_count=len(all_items),
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        equity_account_balance=equity_account_balance,
        unclosed_earnings=unclosed_earnings,
        total_equity=total_equity,
        balancing_difference=difference,
        is_balanced=difference == _ZERO,
    )
    if not request.analytic_account_ids and not summary.is_balanced:
        raise OdooMcpError(
            ErrorCode.ODOO_API_ERROR,
            "The full-company balance sheet did not reconcile.",
            "Check Odoo account classifications and accounting data, then retry.",
        )
    page_offset = _cursor_offset(request.cursor)
    selected, next_cursor = _page(all_items, request.limit, request.cursor)
    artifact = _balance_sheet_artifact(
        request,
        company_name,
        selected,
        summary,
        request_id,
        page_offset,
        len(selected),
        next_cursor is not None,
    )
    return BalanceSheetResponse(
        request_id=request_id,
        company_id=request.company_id,
        as_of_date=request.as_of_date,
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
