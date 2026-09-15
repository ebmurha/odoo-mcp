from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from odoo_mcp.adapters.accounting import (
    Account,
    AccountMove,
    AccountMoveLine,
    AnalyticAccount,
    BankStatementLine,
    DatePeriod,
    FilterClause,
    Journal,
    PageRequest,
    PartialReconciliation,
    Partner,
    PaymentMethodLine,
    PaymentTerm,
    Product,
    ReadFilters,
)
from odoo_mcp.adapters.odoo.client import OdooClient
from odoo_mcp.app.settings import OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError


class FakeTransport:
    name = "json2"

    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def authenticate(self) -> None:
        return None

    async def probe_model(self, model: str, *, company_ids: tuple[int, ...]) -> bool:
        return True

    async def search_count(
        self,
        model: str,
        domain: list[Any],
        *,
        company_ids: tuple[int, ...],
    ) -> int:
        return len(self.rows.get(model, []))

    async def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        *,
        limit: int,
        offset: int = 0,
        order: str = "id",
        company_ids: tuple[int, ...],
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "model": model,
                "domain": domain,
                "fields": fields,
                "limit": limit,
                "offset": offset,
                "order": order,
                "company_ids": company_ids,
            }
        )
        rows = self.rows.get(model, [])
        cursor_id = next(
            (term[2] for term in domain if isinstance(term, list) and term[:2] == ["id", ">"]),
            0,
        )
        selected = [row for row in rows if row.get("id", 0) > cursor_id]
        return selected[offset : offset + limit]

    async def close(self) -> None:
        return None


def _move(identifier: int) -> dict[str, Any]:
    return {
        "id": identifier,
        "name": f"MVE/{identifier}",
        "move_type": "entry",
        "state": "posted",
        "date": "2026-09-01",
        "invoice_date": False,
        "invoice_date_due": False,
        "partner_id": [20, "Synthetic Partner"],
        "journal_id": [30, "Synthetic Journal"],
        "company_id": [1, "Synthetic Company"],
        "currency_id": [40, "Synthetic Currency"],
        "amount_total": "12.30",
        "amount_residual": 2.1,
        "payment_state": "partial",
        "ref": "Synthetic reference",
        "password": "raw-synthetic-secret",
    }


async def _validated_client(
    connection: OdooConnectionSettings,
    transport: FakeTransport,
) -> OdooClient:
    client = OdooClient(connection, 19, transport)
    transport.rows["res.company"] = [
        {"id": 1, "name": "Synthetic Company"},
        {"id": 2, "name": "Synthetic Company 2"},
    ]
    await client.get_companies()
    transport.calls.clear()
    return client


async def test_account_moves_are_typed_scoped_and_cursor_paginated(
    connection: OdooConnectionSettings,
) -> None:
    transport = FakeTransport({"account.move": [_move(1), _move(2)]})
    client = await _validated_client(connection, transport)

    first = await client.get_account_moves(
        1,
        ReadFilters(clauses=(FilterClause(field="state", operator="=", value="posted"),)),
        PageRequest(limit=1),
    )
    second = await client.get_account_moves(
        1,
        ReadFilters(),
        PageRequest(limit=1, cursor=first.next_cursor),
    )

    assert first.items == [
        AccountMove(
            id=1,
            name="MVE/1",
            move_type="entry",
            state="posted",
            date=date(2026, 9, 1),
            partner={"id": 20, "name": "Synthetic Partner"},
            journal={"id": 30, "name": "Synthetic Journal"},
            company_id=1,
            currency={"id": 40, "name": "Synthetic Currency"},
            amount_total=Decimal("12.30"),
            amount_residual=Decimal("2.1"),
            payment_state="partial",
            reference="Synthetic reference",
        )
    ]
    assert first.next_cursor is not None
    assert second.items[0].id == 2
    assert second.next_cursor is None
    assert ["company_id", "=", 1] in transport.calls[0]["domain"]
    assert transport.calls[0]["company_ids"] == (1,)
    assert transport.calls[0]["limit"] == 2
    assert ["id", ">", 1] in transport.calls[1]["domain"]
    assert "password" not in first.items[0].model_dump()


@pytest.mark.parametrize(
    ("method", "model", "scope_term"),
    [
        ("get_account_move_lines", "account.move.line", ["company_id", "=", 1]),
        (
            "get_partial_reconciliations",
            "account.partial.reconcile",
            ["debit_move_id.company_id", "=", 1],
        ),
        ("get_journals", "account.journal", ["company_id", "=", 1]),
        (
            "get_bank_statement_lines",
            "account.bank.statement.line",
            ["company_id", "=", 1],
        ),
        ("get_payment_terms", "account.payment.term", ["company_id", "in", [False, 1]]),
        (
            "get_payment_method_lines",
            "account.payment.method.line",
            ["journal_id.company_id", "=", 1],
        ),
        ("get_partners", "res.partner", ["company_id", "in", [False, 1]]),
        ("get_products", "product.product", ["company_id", "in", [False, 1]]),
        ("get_account_accounts", "account.account", ["company_ids", "in", [1]]),
        (
            "get_analytic_accounts",
            "account.analytic.account",
            ["company_id", "in", [False, 1]],
        ),
    ],
)
async def test_every_accounting_read_adds_an_explicit_company_scope(
    connection: OdooConnectionSettings,
    method: str,
    model: str,
    scope_term: list[Any],
) -> None:
    transport = FakeTransport({model: []})
    client = await _validated_client(connection, transport)
    page = PageRequest(limit=1)
    filters = ReadFilters()

    if method == "get_bank_statement_lines":
        await client.get_bank_statement_lines(
            1,
            DatePeriod(start=date(2026, 9, 1), end=date(2026, 9, 30)),
            journal_id=30,
            page=page,
        )
    elif method == "get_payment_method_lines":
        await client.get_payment_method_lines(1, (30,), page=page)
    elif method in {
        "get_account_move_lines",
        "get_partial_reconciliations",
    }:
        await getattr(client, method)(1, filters, page)
    elif method in {
        "get_partners",
        "get_products",
        "get_account_accounts",
        "get_analytic_accounts",
    }:
        await getattr(client, method)(1, filters, page=page)
    else:
        await getattr(client, method)(1, page=page)

    assert transport.calls[0]["model"] == model
    assert scope_term in transport.calls[0]["domain"]
    assert transport.calls[0]["company_ids"] == (1,)


async def test_unauthorized_company_and_company_filter_injection_fail_before_transport(
    connection: OdooConnectionSettings,
) -> None:
    transport = FakeTransport({"account.move": []})
    client = await _validated_client(connection, transport)

    with pytest.raises(OdooMcpError) as unauthorized:
        await client.get_account_moves(3, ReadFilters(), PageRequest())
    with pytest.raises(OdooMcpError) as injected:
        await client.get_account_moves(
            1,
            ReadFilters(clauses=(FilterClause(field="company_id", operator="=", value=2),)),
            PageRequest(),
        )

    assert unauthorized.value.code is ErrorCode.COMPANY_NOT_FOUND
    assert injected.value.code is ErrorCode.INVALID_INPUT
    assert transport.calls == []


@pytest.mark.parametrize("value", ["not-a-date", "NaN", [True, "Broken relation"]])
async def test_malformed_odoo_values_are_safe_failures(
    connection: OdooConnectionSettings,
    value: object,
) -> None:
    row = _move(1)
    if value == "not-a-date":
        row["date"] = value
    elif value == "NaN":
        row["amount_total"] = value
    else:
        row["journal_id"] = value
    transport = FakeTransport({"account.move": [row]})
    client = await _validated_client(connection, transport)

    with pytest.raises(OdooMcpError) as caught:
        await client.get_account_moves(1, ReadFilters(), PageRequest())

    assert caught.value.code is ErrorCode.ODOO_API_ERROR
    assert "not-a-date" not in str(caught.value)
    assert "NaN" not in str(caught.value)
    assert "Broken relation" not in str(caught.value)


async def test_invalid_cursor_is_rejected_before_transport(
    connection: OdooConnectionSettings,
) -> None:
    transport = FakeTransport({"account.move": []})
    client = await _validated_client(connection, transport)

    with pytest.raises(OdooMcpError) as caught:
        await client.get_account_moves(
            1,
            ReadFilters(),
            PageRequest(cursor="not-a-valid-cursor"),
        )

    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert transport.calls == []


async def test_denied_filter_field_is_rejected_before_transport(
    connection: OdooConnectionSettings,
) -> None:
    transport = FakeTransport({"account.move": []})
    client = await _validated_client(connection, transport)

    with pytest.raises(OdooMcpError) as caught:
        await client.get_account_moves(
            1,
            ReadFilters(
                clauses=(FilterClause(field="partner_id.api_key", operator="=", value="x"),)
            ),
            PageRequest(),
        )

    assert caught.value.code is ErrorCode.FIELD_DENIED
    assert transport.calls == []


async def test_transport_cannot_overrun_the_bounded_page(
    connection: OdooConnectionSettings,
) -> None:
    class OverrunningTransport(FakeTransport):
        async def search_read(
            self,
            model: str,
            domain: list[Any],
            fields: list[str],
            *,
            limit: int,
            offset: int = 0,
            order: str = "id",
            company_ids: tuple[int, ...],
        ) -> list[dict[str, Any]]:
            rows = await super().search_read(
                model,
                domain,
                fields,
                limit=limit,
                offset=offset,
                order=order,
                company_ids=company_ids,
            )
            return [*rows, _move(3)] if model == "account.move" else rows

    transport = OverrunningTransport({"account.move": [_move(1), _move(2)]})
    client = await _validated_client(connection, transport)

    with pytest.raises(OdooMcpError) as caught:
        await client.get_account_moves(1, ReadFilters(), PageRequest(limit=1))

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


def test_page_size_is_bounded() -> None:
    with pytest.raises(ValidationError):
        PageRequest(limit=501)


async def test_every_accounting_record_shape_is_normalized(
    connection: OdooConnectionSettings,
) -> None:
    rows: dict[str, list[dict[str, Any]]] = {
        "account.move.line": [
            {
                "id": 1,
                "move_id": [2, "MVE/2"],
                "account_id": [3, "1000 Cash"],
                "journal_id": [4, "Bank"],
                "partner_id": False,
                "company_id": [1, "Synthetic Company"],
                "currency_id": [5, "USD"],
                "date": "2026-09-01",
                "date_maturity": False,
                "name": "Line",
                "debit": 10,
                "credit": 0,
                "balance": "10.00",
                "amount_currency": 10,
                "amount_residual": 2,
                "amount_residual_currency": 2,
                "reconciled": False,
                "analytic_distribution": {"6": 100},
            }
        ],
        "account.partial.reconcile": [
            {
                "id": 7,
                "debit_move_id": [1, "Debit"],
                "credit_move_id": [2, "Credit"],
                "amount": 5,
                "debit_amount_currency": 5,
                "credit_amount_currency": 5,
                "max_date": "2026-09-02",
            }
        ],
        "account.journal": [
            {
                "id": 8,
                "name": "Bank",
                "code": "BNK",
                "type": "bank",
                "company_id": [1, "Synthetic Company"],
                "currency_id": False,
            }
        ],
        "account.bank.statement.line": [
            {
                "id": 9,
                "date": "2026-09-03",
                "payment_ref": "Transfer",
                "amount": -5,
                "amount_currency": False,
                "foreign_currency_id": False,
                "partner_id": [10, "Partner"],
                "journal_id": [8, "Bank"],
                "company_id": [1, "Synthetic Company"],
                "is_reconciled": False,
                "move_id": False,
            }
        ],
        "account.payment.term": [{"id": 11, "name": "Immediate", "company_id": False}],
        "account.payment.method.line": [
            {
                "id": 12,
                "name": "Manual",
                "journal_id": [8, "Bank"],
                "payment_method_id": [13, "Manual"],
                "payment_type": "inbound",
            }
        ],
        "res.partner": [{"id": 14, "name": "Partner", "company_id": False}],
        "product.product": [
            {
                "id": 15,
                "name": "Service",
                "default_code": False,
                "company_id": False,
                "list_price": 20,
                "currency_id": [5, "USD"],
            }
        ],
        "account.account": [
            {
                "id": 16,
                "code": "1000",
                "name": "Cash",
                "account_type": "asset_cash",
                "company_ids": [1, 2],
                "currency_id": False,
                "reconcile": True,
            }
        ],
        "account.analytic.account": [
            {
                "id": 17,
                "name": "Programme",
                "code": "PRG",
                "company_id": [1, "Synthetic Company"],
                "currency_id": [5, "USD"],
            }
        ],
    }
    transport = FakeTransport(rows)
    client = await _validated_client(connection, transport)
    page = PageRequest(limit=1)

    results = [
        await client.get_account_move_lines(1, ReadFilters(), page),
        await client.get_partial_reconciliations(1, ReadFilters(), page),
        await client.get_journals(1, page=page),
        await client.get_bank_statement_lines(
            1,
            DatePeriod(start=date(2026, 9, 1), end=date(2026, 9, 30)),
            8,
            page=page,
        ),
        await client.get_payment_terms(1, page=page),
        await client.get_payment_method_lines(1, (8,), page=page),
        await client.get_partners(1, ReadFilters(), page=page),
        await client.get_products(1, ReadFilters(), page=page),
        await client.get_account_accounts(1, ReadFilters(), page=page),
        await client.get_analytic_accounts(1, ReadFilters(), page=page),
    ]

    expected_types = (
        AccountMoveLine,
        PartialReconciliation,
        Journal,
        BankStatementLine,
        PaymentTerm,
        PaymentMethodLine,
        Partner,
        Product,
        Account,
        AnalyticAccount,
    )
    assert all(
        isinstance(result.items[0], expected)
        for result, expected in zip(results, expected_types, strict=True)
    )
    assert results[0].items[0].balance == Decimal("10.00")
    assert results[0].items[0].analytic_distribution == {"6": Decimal("100")}
    assert results[8].items[0].company_ids == (1,)


async def test_cross_company_response_is_rejected(
    connection: OdooConnectionSettings,
) -> None:
    transport = FakeTransport(
        {
            "account.journal": [
                {
                    "id": 8,
                    "name": "Other company journal",
                    "code": "OTH",
                    "type": "general",
                    "company_id": [2, "Synthetic Company 2"],
                    "currency_id": False,
                }
            ]
        }
    )
    client = await _validated_client(connection, transport)

    with pytest.raises(OdooMcpError) as caught:
        await client.get_journals(1)

    assert caught.value.code is ErrorCode.ODOO_API_ERROR
    assert "Other company journal" not in str(caught.value)
