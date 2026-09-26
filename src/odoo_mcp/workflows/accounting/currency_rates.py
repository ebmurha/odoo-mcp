"""Company-scoped Odoo currency-rate history."""

from __future__ import annotations

import base64
import binascii
from datetime import date
from decimal import Decimal
from typing import Literal

from odoo_mcp.adapters.accounting import CurrencyRate, PageRequest
from odoo_mcp.adapters.base import Company, OdooAdapter
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import (
    CurrencyRateDescriptor,
    CurrencyRateHistoryInput,
    CurrencyRateHistoryResponse,
    CurrencyRateHistorySummary,
    CurrencyRateItem,
    EffectiveCurrencyRate,
)

_SOURCE_PAGE_SIZE = 500
_MAX_SOURCE_RECORDS = 100_000


def _invalid_data() -> OdooMcpError:
    return OdooMcpError(
        ErrorCode.ODOO_API_ERROR,
        "Odoo returned invalid currency-rate history data.",
        "Check Odoo currency-rate integrity and record access, then retry.",
    )


def _cursor(request: CurrencyRateHistoryInput, offset: int) -> str:
    raw = (
        f"v1:{request.company_id}:{request.currency_id}:"
        f"{request.period_start}:{request.period_end}:{offset}"
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _offset(request: CurrencyRateHistoryInput) -> int:
    if request.cursor is None:
        return 0
    try:
        padded = request.cursor + "=" * (-len(request.cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode()
        version, company, currency, start, end, offset = raw.split(":")
        parsed = int(offset)
    except (ValueError, UnicodeError, binascii.Error):
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor is invalid.",
            "Restart the history request without a cursor.",
        ) from None
    if (
        version != "v1"
        or company != str(request.company_id)
        or currency != str(request.currency_id)
        or start != str(request.period_start)
        or end != str(request.period_end)
        or parsed <= 0
    ):
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor does not match this history request.",
            "Restart the history request without a cursor.",
        )
    return parsed


async def _rates(
    adapter: OdooAdapter, request: CurrencyRateHistoryInput, company: Company
) -> list[CurrencyRate]:
    result: list[CurrencyRate] = []
    seen_ids: set[int] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None
    while True:
        page = await adapter.get_currency_rates(
            request.company_id,
            request.currency_id,
            request.period_end,
            page=PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
        )
        if (
            company.currency is None
            or page.company_currency.id != company.currency.id
            or company.root_id is None
            or page.root_company_id != company.root_id
            or (page.next_cursor is not None and not page.items)
            or any(item.id in seen_ids for item in page.items)
        ):
            raise _invalid_data()
        seen_ids.update(item.id for item in page.items)
        result.extend(page.items)
        if len(result) > _MAX_SOURCE_RECORDS:
            raise _invalid_data()
        if page.next_cursor is None:
            return result
        if page.next_cursor in seen_cursors:
            raise _invalid_data()
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor


def _point(rate: CurrencyRate, root_id: int) -> CurrencyRateItem:
    if rate.company_id not in {None, root_id}:
        raise _invalid_data()
    return CurrencyRateItem(
        source_rate_id=rate.id,
        source_scope="company" if rate.company_id == root_id else "shared",
        effective_date=rate.effective_date,
        currency_units_per_company_unit=rate.company_rate,
        company_units_per_currency_unit=rate.inverse_company_rate,
    )


def _artifact(
    request: CurrencyRateHistoryInput,
    company: Company,
    requested: CurrencyRateDescriptor,
    effective: EffectiveCurrencyRate | None,
    items: list[CurrencyRateItem],
    summary: CurrencyRateHistorySummary,
    request_id: str,
) -> str:
    company_currency = company.currency
    if company_currency is None:
        raise _invalid_data()
    rows = [
        "# Currency Rate History",
        "",
        f"- Company: {company.name} ({company.id})",
        f"- Company currency: {company_currency.name} ({company_currency.id})",
        f"- Requested currency: {requested.name} ({requested.id})",
        f"- Period: {request.period_start} to {request.period_end}",
        f"- History status: {summary.history_status}",
        f"- Audit reference: `{request_id}`",
        "",
        "| Effective date | Scope | Source rate ID | Currency units per company unit | "
        "Company units per currency unit |",
        "|---|---|---:|---:|---:|",
    ]
    if effective is not None:
        rows.append(
            f"| {effective.effective_date} | {effective.source_scope} | "
            f"{effective.source_rate_id or ''} | {effective.currency_units_per_company_unit} | "
            f"{effective.company_units_per_currency_unit} |"
        )
    rows.extend(
        f"| {item.effective_date} | {item.source_scope} | {item.source_rate_id} | "
        f"{item.currency_units_per_company_unit} | {item.company_units_per_currency_unit} |"
        for item in items
    )
    return "\n".join(rows) + "\n"


async def get_currency_rate_history(
    adapter: OdooAdapter,
    request: CurrencyRateHistoryInput,
    *,
    company: Company,
    request_id: str,
) -> CurrencyRateHistoryResponse:
    if company.currency is None or company.root_id is None:
        raise _invalid_data()
    currencies = await adapter.get_currencies(
        request.company_id, (request.currency_id,), page=PageRequest(limit=1)
    )
    if (
        len(currencies.items) != 1
        or currencies.items[0].id != request.currency_id
        or currencies.next_cursor is not None
    ):
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The requested currency is unavailable.",
            "Use an exact currency ID visible to the authorized company.",
        )
    requested = CurrencyRateDescriptor(id=currencies.items[0].id, name=currencies.items[0].name)
    if request.currency_id == company.currency.id and requested.name != company.currency.name:
        raise _invalid_data()
    offset = _offset(request)
    effective: EffectiveCurrencyRate | None
    all_items: list[CurrencyRateItem]
    history_status: Literal["available", "missing", "company_currency_identity"]
    if request.currency_id == company.currency.id:
        effective = EffectiveCurrencyRate(
            source_rate_id=None,
            source_scope="identity",
            effective_date=request.period_start,
            currency_units_per_company_unit=Decimal("1"),
            company_units_per_currency_unit=Decimal("1"),
        )
        all_items = []
        history_status = "company_currency_identity"
    else:
        raw = await _rates(adapter, request, company)
        by_date: dict[date, CurrencyRate] = {}
        for rate in raw:
            current = by_date.get(rate.effective_date)
            rank = (rate.company_id == company.root_id, rate.id)
            if current is None or rank > (current.company_id == company.root_id, current.id):
                by_date[rate.effective_date] = rate
        ordered = sorted(
            by_date.values(), key=lambda item: (item.effective_date, item.id), reverse=True
        )
        prior = next(
            (item for item in ordered if item.effective_date <= request.period_start), None
        )
        effective = (
            EffectiveCurrencyRate(**_point(prior, company.root_id).model_dump())
            if prior is not None
            else None
        )
        all_items = [
            _point(item, company.root_id)
            for item in ordered
            if request.period_start <= item.effective_date <= request.period_end
        ]
        history_status = "available" if effective is not None else "missing"
    if offset > len(all_items):
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor is outside this history.",
            "Restart the history request without a cursor.",
        )
    items = all_items[offset : offset + request.limit]
    next_offset = offset + len(items)
    next_cursor = _cursor(request, next_offset) if next_offset < len(all_items) else None
    summary = CurrencyRateHistorySummary(
        history_status=history_status,
        returned_count=len(items),
        has_more=next_cursor is not None,
    )
    return CurrencyRateHistoryResponse(
        request_id=request_id,
        company_id=request.company_id,
        company_currency=CurrencyRateDescriptor(id=company.currency.id, name=company.currency.name),
        requested_currency=requested,
        period_start=request.period_start,
        period_end=request.period_end,
        effective_at_start=effective,
        items=items,
        next_cursor=next_cursor,
        summary=summary,
        artifact_markdown=_artifact(
            request, company, requested, effective, items, summary, request_id
        ),
    )
