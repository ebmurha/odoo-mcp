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
    Currency,
    DatePeriod,
    FilterClause,
    InvoiceDraft,
    InvoiceDraftLine,
    Journal,
    JournalEntryDraft,
    JournalEntryDraftLine,
    PageRequest,
    PartialReconciliation,
    Partner,
    PaymentPreviewRequest,
    PaymentRegistration,
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
        self.executions: list[dict[str, Any]] = []

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

    async def execute_method(
        self,
        model: str,
        method: str,
        *,
        ids: tuple[int, ...] = (),
        positional: list[Any] | None = None,
        named: dict[str, Any] | None = None,
        company_ids: tuple[int, ...],
    ) -> Any:
        self.executions.append(
            {
                "model": model,
                "method": method,
                "ids": ids,
                "positional": positional,
                "named": named,
                "company_ids": company_ids,
            }
        )
        if model == "account.move" and method == "create":
            return 901
        return True

    async def close(self) -> None:
        return None


class PaymentWizardTransport(FakeTransport):
    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__(rows)
        self.wizard_values: dict[int, dict[str, Any]] = {}
        self.next_wizard_id = 5000
        self.available_journal_ids = [10, 20]

    async def execute_method(
        self,
        model: str,
        method: str,
        *,
        ids: tuple[int, ...] = (),
        positional: list[Any] | None = None,
        named: dict[str, Any] | None = None,
        company_ids: tuple[int, ...],
    ) -> Any:
        self.executions.append(
            {
                "model": model,
                "method": method,
                "ids": ids,
                "positional": positional,
                "named": named,
                "company_ids": company_ids,
            }
        )
        assert model == "account.payment.register"
        if method == "create":
            self.next_wizard_id += 1
            assert named is not None
            self.wizard_values[self.next_wizard_id] = dict(named["vals_list"])
            return self.next_wizard_id
        if method == "action_create_payments":
            return True
        assert method == "read" and len(ids) == 1
        values = self.wizard_values[ids[0]]
        journal_id = values.get("journal_id", 10)
        method_id = values.get("payment_method_line_id", 100 if journal_id == 10 else 200)
        methods = [100, 101] if journal_id == 10 else [200]
        method_name = "Localized Manual" if method_id == 100 else f"Method {method_id}"
        method_code = "manual" if method_id == 100 else "electronic"
        return [
            {
                "id": ids[0],
                "available_journal_ids": self.available_journal_ids,
                "journal_id": [journal_id, f"Journal {journal_id}"],
                "available_payment_method_line_ids": methods,
                "payment_method_line_id": [method_id, method_name],
                "payment_method_code": method_code,
                "amount": values["amount"],
                "currency_id": [40, "USD"],
                "payment_date": values["payment_date"],
                "payment_type": "inbound",
                "partner_type": "customer",
                "company_id": [1, "Synthetic Company"],
                "can_edit_wizard": True,
            }
        ]


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


def _invoice_effect_row(identifier: int = 901) -> dict[str, Any]:
    return {
        "id": identifier,
        "name": "INV/901",
        "move_type": "out_invoice",
        "state": "draft",
        "company_id": [1, "Synthetic Company"],
        "partner_id": [20, "Synthetic Customer"],
        "invoice_date": "2026-09-20",
        "invoice_date_due": "2026-10-20",
        "journal_id": [30, "Sales"],
        "fiscal_position_id": [31, "Domestic"],
        "currency_id": [40, "USD"],
        "invoice_payment_term_id": [41, "30 Days"],
        "amount_untaxed": "20.00",
        "amount_tax": "3.20",
        "amount_total": "23.20",
        "amount_residual": "23.20",
        "payment_state": "not_paid",
    }


async def test_draft_invoice_creation_uses_allowlisted_create_and_reads_back_effects(
    connection: OdooConnectionSettings,
) -> None:
    transport = FakeTransport(
        {
            "account.move": [_invoice_effect_row()],
            "account.move.line": [
                {
                    "id": 1001,
                    "move_id": [901, "INV/901"],
                    "company_id": [1, "Synthetic Company"],
                    "display_type": "product",
                    "name": "Consulting",
                    "quantity": "2",
                    "price_unit": "10",
                    "price_subtotal": "20",
                    "price_total": "23.20",
                    "tax_line_id": False,
                    "account_id": [70, "Revenue"],
                    "debit": "0",
                    "credit": "20",
                    "balance": "-20",
                    "currency_id": [40, "USD"],
                    "amount_currency": "-20",
                    "analytic_distribution": {"77": 100},
                    "date_maturity": False,
                    "amount_residual": "0",
                },
                {
                    "id": 1002,
                    "move_id": [901, "INV/901"],
                    "company_id": [1, "Synthetic Company"],
                    "display_type": "tax",
                    "name": "Tax",
                    "quantity": "0",
                    "price_unit": "0",
                    "price_subtotal": "0",
                    "price_total": "0",
                    "tax_line_id": [55, "VAT"],
                    "account_id": [71, "Tax"],
                    "debit": "0",
                    "credit": "3.20",
                    "balance": "-3.20",
                    "currency_id": [40, "USD"],
                    "amount_currency": "-3.20",
                    "analytic_distribution": {},
                    "date_maturity": False,
                    "amount_residual": "0",
                },
                {
                    "id": 1003,
                    "move_id": [901, "INV/901"],
                    "company_id": [1, "Synthetic Company"],
                    "display_type": "payment_term",
                    "name": "Due",
                    "quantity": "0",
                    "price_unit": "0",
                    "price_subtotal": "0",
                    "price_total": "0",
                    "tax_line_id": False,
                    "account_id": [72, "Receivable"],
                    "debit": "23.20",
                    "credit": "0",
                    "balance": "23.20",
                    "currency_id": [40, "USD"],
                    "amount_currency": "23.20",
                    "analytic_distribution": {},
                    "date_maturity": "2026-10-20",
                    "amount_residual": "23.20",
                },
            ],
        }
    )
    client = await _validated_client(connection, transport)

    effect = await client.create_draft_invoice(
        InvoiceDraft(
            company_id=1,
            move_type="out_invoice",
            partner_id=20,
            invoice_date=date(2026, 9, 20),
            lines=(
                InvoiceDraftLine(
                    description="Consulting",
                    quantity=Decimal("2"),
                    unit_price=Decimal("10"),
                    account_id=70,
                    analytic_account_id=77,
                ),
            ),
        )
    )

    assert [(call["model"], call["method"]) for call in transport.executions] == [
        ("account.move", "create")
    ]
    assert transport.executions[0]["company_ids"] == (1,)
    assert effect.state == "draft"
    assert effect.journal.id == 30
    assert effect.fiscal_position is not None and effect.fiscal_position.id == 31
    assert effect.taxes[0].amount == Decimal("3.20")
    assert effect.lines[0].total == Decimal("23.20")
    assert effect.payment_schedule[0].amount == Decimal("23.20")


async def test_payment_preview_uses_only_bounded_wizard_create_read_routes(
    connection: OdooConnectionSettings,
) -> None:
    invoice = _invoice_effect_row(101)
    invoice.update(
        {
            "name": "INV/101",
            "state": "posted",
            "amount_residual": "100",
            "amount_total": "100",
            "amount_untaxed": "100",
            "amount_tax": "0",
        }
    )
    transport = PaymentWizardTransport({"account.move": [invoice], "account.move.line": []})
    client = await _validated_client(connection, transport)

    preview = await client.get_payment_registration_preview(
        PaymentPreviewRequest(
            invoice_id=101,
            company_id=1,
            payment_date=date(2026, 9, 20),
            amount=Decimal("100"),
        )
    )

    assert [(route.journal.id, route.payment_method_line.id) for route in preview.routes] == [
        (10, 100),
        (10, 101),
        (20, 200),
    ]
    assert preview.routes[0].payment_method_line.name == "Localized Manual"
    assert preview.routes[0].external_effect_status == "not_initiated_by_odoo"
    assert preview.routes[1].external_effect_status == "unknown"
    assert preview.default_journal_id == 10
    assert preview.default_payment_method_line_id == 100
    assert [call["method"] for call in transport.executions] == [
        "create",
        "read",
        "create",
        "read",
        "create",
        "read",
        "create",
        "read",
    ]
    assert all(call["model"] == "account.payment.register" for call in transport.executions)
    expected_fields = [
        "available_journal_ids",
        "journal_id",
        "available_payment_method_line_ids",
        "payment_method_line_id",
        "payment_method_code",
        "amount",
        "currency_id",
        "payment_date",
        "payment_type",
        "partner_type",
        "company_id",
        "can_edit_wizard",
    ]
    assert all(
        call["named"]["fields"] == expected_fields
        for call in transport.executions
        if call["method"] == "read"
    )
    assert not {"search", "search_read", "write", "unlink", "action_create_payments"} & {
        call["method"] for call in transport.executions
    }


async def test_payment_execution_uses_fresh_wizard_read_before_standard_action(
    connection: OdooConnectionSettings,
) -> None:
    invoice = _invoice_effect_row(101)
    invoice.update(
        {
            "name": "INV/101",
            "state": "posted",
            "amount_residual": "100",
            "amount_total": "100",
            "amount_untaxed": "100",
            "amount_tax": "0",
        }
    )
    transport = PaymentWizardTransport({"account.move": [invoice], "account.move.line": []})
    client = await _validated_client(connection, transport)

    await client.register_payment(
        PaymentRegistration(
            invoice_id=101,
            company_id=1,
            payment_date=date(2026, 9, 20),
            amount=Decimal("100"),
            journal_id=10,
            payment_method_line_id=100,
            payment_method_code="manual",
            external_effect_status="not_initiated_by_odoo",
        )
    )

    assert [call["method"] for call in transport.executions] == [
        "create",
        "read",
        "action_create_payments",
    ]
    assert transport.executions[-1]["ids"] == transport.executions[-2]["ids"]


async def test_payment_preview_rejects_excessive_wizard_routes(
    connection: OdooConnectionSettings,
) -> None:
    invoice = _invoice_effect_row(101)
    invoice.update({"state": "posted", "amount_residual": "100"})
    transport = PaymentWizardTransport({"account.move": [invoice], "account.move.line": []})
    transport.available_journal_ids = list(range(1, 52))
    client = await _validated_client(connection, transport)

    with pytest.raises(OdooMcpError) as caught:
        await client.get_payment_registration_preview(
            PaymentPreviewRequest(
                invoice_id=101,
                company_id=1,
                payment_date=date(2026, 9, 20),
                amount=Decimal("100"),
            )
        )

    assert caught.value.code is ErrorCode.ODOO_API_ERROR
    assert [call["method"] for call in transport.executions] == ["create", "read"]


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


async def test_unnumbered_draft_journal_entry_uses_placeholder_name(
    connection: OdooConnectionSettings,
) -> None:
    row = _move(3)
    row.update({"name": False, "state": "draft"})
    client = await _validated_client(connection, FakeTransport({"account.move": [row]}))

    result = await client.get_account_moves(1, ReadFilters(), PageRequest(limit=1))

    assert result.items[0].name == "/"


async def test_exact_journal_entry_rejects_substituted_move(
    connection: OdooConnectionSettings,
) -> None:
    transport = FakeTransport({"account.move": [_move(202)], "account.move.line": []})
    client = await _validated_client(connection, transport)

    with pytest.raises(OdooMcpError) as raised:
        await client.get_journal_entry(1, 101)

    assert raised.value.code is ErrorCode.ODOO_API_ERROR


async def test_exact_journal_entry_rejects_line_from_another_move(
    connection: OdooConnectionSettings,
) -> None:
    move = _move(101)
    move["state"] = "draft"
    line = {
        "id": 1001,
        "move_id": [202, "MVE/202"],
        "parent_state": "posted",
        "account_id": [10, "Debit"],
        "journal_id": [30, "Synthetic Journal"],
        "partner_id": False,
        "company_id": [1, "Synthetic Company"],
        "currency_id": [40, "Synthetic Currency"],
        "date": "2026-09-01",
        "date_maturity": False,
        "name": "Substituted line",
        "debit": "100",
        "credit": "0",
        "balance": "100",
        "amount_currency": "100",
        "amount_residual": "0",
        "amount_residual_currency": "0",
        "reconciled": False,
        "analytic_distribution": {},
    }
    client = await _validated_client(
        connection,
        FakeTransport({"account.move": [move], "account.move.line": [line]}),
    )

    with pytest.raises(OdooMcpError) as raised:
        await client.get_journal_entry(1, 101)

    assert raised.value.code is ErrorCode.ODOO_API_ERROR


async def test_manual_journal_draft_creation_is_separate_from_posting(
    connection: OdooConnectionSettings,
) -> None:
    move = _move(901)
    move["state"] = "draft"
    transport = FakeTransport(
        {
            "account.move": [move],
            "account.move.line": [
                {
                    "id": 1001,
                    "move_id": [901, "MVE/901"],
                    "parent_state": "draft",
                    "account_id": [10, "Debit"],
                    "journal_id": [30, "Synthetic Journal"],
                    "partner_id": [20, "Synthetic Partner"],
                    "company_id": [1, "Synthetic Company"],
                    "currency_id": [40, "Synthetic Currency"],
                    "date": "2026-09-01",
                    "date_maturity": False,
                    "name": "Debit line",
                    "debit": "100",
                    "credit": "0",
                    "balance": "100",
                    "amount_currency": "100",
                    "amount_residual": "0",
                    "amount_residual_currency": "0",
                    "reconciled": False,
                    "analytic_distribution": {"77": 100},
                }
            ],
        }
    )
    client = await _validated_client(connection, transport)

    effect = await client.create_journal_entry_draft(
        JournalEntryDraft(
            company_id=1,
            journal_id=30,
            entry_date=date(2026, 9, 1),
            reference="Synthetic",
            lines=(
                JournalEntryDraftLine(
                    account_id=10,
                    partner_id=20,
                    description="Debit line",
                    debit=Decimal("100"),
                    credit=Decimal("0"),
                    analytic_account_id=77,
                ),
                JournalEntryDraftLine(
                    account_id=11,
                    debit=Decimal("0"),
                    credit=Decimal("100"),
                ),
            ),
        )
    )

    assert effect.state == "draft"
    assert [call["method"] for call in transport.executions] == ["create"]
    values = transport.executions[0]["named"]["vals_list"]
    assert values["move_type"] == "entry"
    assert values["line_ids"][0][2]["analytic_distribution"] == {"77": 100}


async def test_post_adapter_rejects_non_draft_before_action(
    connection: OdooConnectionSettings,
) -> None:
    transport = FakeTransport({"account.move": [_move(901)], "account.move.line": []})
    client = await _validated_client(connection, transport)

    with pytest.raises(OdooMcpError) as raised:
        await client.post_journal_entry(1, 901)

    assert raised.value.code is ErrorCode.JOURNAL_ENTRY_NOT_DRAFT
    assert transport.executions == []


async def test_post_adapter_rejects_substituted_post_action_record(
    connection: OdooConnectionSettings,
) -> None:
    draft = _move(101)
    draft["state"] = "draft"

    class SubstitutingPostTransport(FakeTransport):
        async def execute_method(
            self,
            model: str,
            method: str,
            *,
            ids: tuple[int, ...] = (),
            positional: list[Any] | None = None,
            named: dict[str, Any] | None = None,
            company_ids: tuple[int, ...],
        ) -> Any:
            result = await super().execute_method(
                model,
                method,
                ids=ids,
                positional=positional,
                named=named,
                company_ids=company_ids,
            )
            if model == "account.move" and method == "action_post":
                substituted = _move(202)
                substituted["state"] = "posted"
                self.rows["account.move"] = [substituted]
            return result

    transport = SubstitutingPostTransport({"account.move": [draft], "account.move.line": []})
    client = await _validated_client(connection, transport)

    with pytest.raises(OdooMcpError) as raised:
        await client.post_journal_entry(1, 101)

    assert raised.value.code is ErrorCode.ODOO_API_ERROR
    assert transport.executions[0]["ids"] == (101,)


async def test_currencies_return_odoo_rounding_in_authorized_company_context(
    connection: OdooConnectionSettings,
) -> None:
    transport = FakeTransport(
        {
            "res.currency": [
                {"id": 40, "name": "Synthetic Currency", "rounding": 0.01},
            ]
        }
    )
    client = await _validated_client(connection, transport)

    result = await client.get_currencies(1, (40,), page=PageRequest(limit=1))

    assert result.items == [Currency(id=40, name="Synthetic Currency", rounding=Decimal("0.01"))]
    assert transport.calls[0] == {
        "model": "res.currency",
        "domain": [["id", "in", [40]]],
        "fields": ["id", "name", "rounding"],
        "limit": 2,
        "offset": 0,
        "order": "id asc",
        "company_ids": (1,),
    }


@pytest.mark.parametrize("rounding", [0, -0.01, False, "not-a-number"])
async def test_currency_rounding_must_be_positive_and_numeric(
    connection: OdooConnectionSettings,
    rounding: object,
) -> None:
    transport = FakeTransport(
        {"res.currency": [{"id": 40, "name": "Synthetic Currency", "rounding": rounding}]}
    )
    client = await _validated_client(connection, transport)

    with pytest.raises(OdooMcpError) as caught:
        await client.get_currencies(1, (40,), page=PageRequest(limit=1))

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


async def test_currency_response_must_match_requested_ids(
    connection: OdooConnectionSettings,
) -> None:
    transport = FakeTransport(
        {"res.currency": [{"id": 41, "name": "Unexpected Currency", "rounding": 0.01}]}
    )
    client = await _validated_client(connection, transport)

    with pytest.raises(OdooMcpError) as caught:
        await client.get_currencies(1, (40,), page=PageRequest(limit=1))

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


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
                "parent_state": "posted",
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
            },
            {"id": 2, "company_id": [1, "Synthetic Company"]},
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
    assert results[7].items[0].company_ids == (1,)


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


async def test_cross_company_partial_reconciliation_credit_is_rejected(
    connection: OdooConnectionSettings,
) -> None:
    transport = FakeTransport(
        {
            "account.partial.reconcile": [
                {
                    "id": 1,
                    "debit_move_id": [10, "Company 1 debit"],
                    "credit_move_id": [20, "Other-company credit"],
                    "amount": 5,
                    "debit_amount_currency": 5,
                    "credit_amount_currency": 5,
                    "max_date": "2026-09-02",
                }
            ],
            "account.move.line": [
                {"id": 10, "company_id": [1, "Synthetic Company"]},
                {"id": 20, "company_id": [2, "Synthetic Company 2"]},
            ],
        }
    )
    client = await _validated_client(connection, transport)

    with pytest.raises(OdooMcpError) as caught:
        await client.get_partial_reconciliations(
            1,
            ReadFilters(),
            PageRequest(limit=1),
        )

    assert caught.value.code is ErrorCode.ODOO_API_ERROR
    assert "Other-company credit" not in str(caught.value)
    assert ["debit_move_id.company_id", "=", 1] in transport.calls[0]["domain"]
    assert ["credit_move_id.company_id", "=", 1] in transport.calls[0]["domain"]
