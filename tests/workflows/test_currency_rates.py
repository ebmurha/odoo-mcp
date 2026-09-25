from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from odoo_mcp.adapters.accounting import (
    Currency,
    CurrencyRate,
    CurrencyRatePage,
    PageRequest,
    RecordPage,
    RelatedRecord,
)
from odoo_mcp.adapters.base import Company
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import CurrencyRateHistoryInput
from odoo_mcp.workflows.accounting.currency_rates import get_currency_rate_history


class RateAdapter:
    def __init__(self, rates: list[CurrencyRate]) -> None:
        self.rates = rates
        self.rate_calls = 0

    async def get_currencies(
        self, company_id: int, currency_ids: tuple[int, ...], *, page: PageRequest
    ) -> RecordPage[Currency]:
        return RecordPage(
            items=[
                Currency(
                    id=currency_ids[0],
                    name="KES" if currency_ids[0] == 1 else "USD",
                    rounding=Decimal("0.01"),
                )
            ]
        )

    async def get_currency_rates(
        self,
        company_id: int,
        currency_id: int,
        through_date: date,
        *,
        page: PageRequest,
    ) -> CurrencyRatePage:
        self.rate_calls += 1
        return CurrencyRatePage(
            company_currency=RelatedRecord(id=1, name="KES"),
            root_company_id=10,
            items=self.rates,
        )


def _company() -> Company:
    return Company(
        id=2,
        name="Branch",
        currency=RelatedRecord(id=1, name="KES"),
        root_id=10,
    )


def _request(**changes: object) -> CurrencyRateHistoryInput:
    values: dict[str, object] = {
        "company_id": 2,
        "currency_id": 3,
        "period_start": date(2026, 1, 1),
        "period_end": date(2026, 1, 31),
        "limit": 100,
    }
    values.update(changes)
    return CurrencyRateHistoryInput(**values)


def _rate(identifier: int, on: date, company_id: int | None, company_rate: str) -> CurrencyRate:
    return CurrencyRate(
        id=identifier,
        effective_date=on,
        currency_id=3,
        company_id=company_id,
        company_rate=Decimal(company_rate),
        inverse_company_rate=Decimal("0.01"),
    )


async def test_company_rate_wins_over_shared_rate_on_same_date() -> None:
    adapter = RateAdapter(
        [
            _rate(1, date(2025, 12, 31), None, "120"),
            _rate(2, date(2026, 1, 10), None, "125"),
            _rate(3, date(2026, 1, 10), 10, "130"),
        ]
    )

    response = await get_currency_rate_history(
        adapter,
        _request(),
        company=_company(),
        request_id="req-1",  # type: ignore[arg-type]
    )

    assert response.summary.history_status == "available"
    assert response.effective_at_start is not None
    assert response.effective_at_start.source_rate_id == 1
    assert [item.source_rate_id for item in response.items] == [3]
    assert response.items[0].source_scope == "company"


async def test_missing_prior_evidence_never_uses_a_future_rate() -> None:
    adapter = RateAdapter([_rate(4, date(2026, 1, 2), None, "125")])

    response = await get_currency_rate_history(
        adapter,
        _request(),
        company=_company(),
        request_id="req-2",  # type: ignore[arg-type]
    )

    assert response.summary.history_status == "missing"
    assert response.effective_at_start is None
    assert [item.source_rate_id for item in response.items] == [4]


async def test_company_currency_is_identity_without_fabricated_rate() -> None:
    adapter = RateAdapter([])

    response = await get_currency_rate_history(
        adapter,
        _request(currency_id=1),
        company=_company(),  # type: ignore[arg-type]
        request_id="req-3",
    )

    assert response.summary.history_status == "company_currency_identity"
    assert response.effective_at_start is not None
    assert response.effective_at_start.source_rate_id is None
    assert response.effective_at_start.currency_units_per_company_unit == Decimal("1")
    assert response.items == []
    assert adapter.rate_calls == 0


async def test_cursor_is_bound_to_the_original_request() -> None:
    adapter = RateAdapter(
        [
            _rate(1, date(2026, 1, 1), None, "120"),
            _rate(2, date(2026, 1, 2), None, "121"),
        ]
    )
    first = await get_currency_rate_history(
        adapter,
        _request(limit=1),
        company=_company(),  # type: ignore[arg-type]
        request_id="req-4",
    )

    with pytest.raises(OdooMcpError) as caught:
        await get_currency_rate_history(
            adapter,
            _request(limit=1, period_end=date(2026, 2, 1), cursor=first.next_cursor),
            company=_company(),  # type: ignore[arg-type]
            request_id="req-5",
        )

    assert caught.value.code is ErrorCode.INVALID_INPUT


def test_period_is_bounded_to_366_dates() -> None:
    with pytest.raises(ValueError):
        _request(period_end=date(2027, 1, 2))
