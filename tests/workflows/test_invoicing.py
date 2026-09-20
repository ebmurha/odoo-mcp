from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from odoo_mcp.adapters.accounting import (
    Account,
    AccountMove,
    AccountMoveLine,
    Currency,
    InvoiceEffect,
    InvoiceValidationLine,
    PageRequest,
    PartialReconciliation,
    Partner,
    PaymentRegistrationPreview,
    PaymentRoute,
    ReadFilters,
    RecordPage,
    RelatedRecord,
)
from odoo_mcp.adapters.base import Company
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import (
    CreateInvoiceInput,
    InvoiceLineInput,
    OpenDocumentsInput,
    RegisterPaymentInput,
    ValidateInvoiceInput,
)
from odoo_mcp.workflows.accounting.invoicing import (
    DEFERRED_INVOICE_FIELDS,
    list_open_documents,
    prepare_invoice_draft,
    prepare_payment,
    prepare_validation,
)


def _move() -> AccountMove:
    return AccountMove(
        id=101,
        name="INV/101",
        move_type="out_invoice",
        state="posted",
        date=date(2026, 1, 10),
        invoice_date=date(2026, 1, 10),
        invoice_date_due=date(2026, 1, 31),
        partner=RelatedRecord(id=20, name="Synthetic Customer"),
        journal=RelatedRecord(id=30, name="Sales"),
        company_id=1,
        currency=RelatedRecord(id=40, name="USD"),
        amount_total=Decimal("100"),
        amount_residual=Decimal("0"),
        payment_state="paid",
    )


def _receivable_line() -> AccountMoveLine:
    return AccountMoveLine(
        id=501,
        move=RelatedRecord(id=101, name="INV/101"),
        account=RelatedRecord(id=60, name="Receivable"),
        journal=RelatedRecord(id=30, name="Sales"),
        partner=RelatedRecord(id=20, name="Synthetic Customer"),
        company_id=1,
        currency=RelatedRecord(id=40, name="USD"),
        date=date(2026, 1, 10),
        maturity_date=date(2026, 1, 31),
        debit=Decimal("80"),
        credit=Decimal("0"),
        balance=Decimal("80"),
        amount_currency=Decimal("100"),
        residual=Decimal("0"),
        residual_currency=Decimal("0"),
        reconciled=True,
        analytic_distribution={},
    )


def _effect() -> InvoiceEffect:
    return InvoiceEffect(
        id=101,
        name="INV/101",
        move_type="out_invoice",
        state="posted",
        company_id=1,
        partner=RelatedRecord(id=20, name="Synthetic Customer"),
        invoice_date=date(2026, 1, 10),
        due_date=date(2026, 1, 31),
        journal=RelatedRecord(id=30, name="Sales"),
        currency=RelatedRecord(id=40, name="USD"),
        amount_untaxed=Decimal("90"),
        amount_tax=Decimal("10"),
        amount_total=Decimal("100"),
        amount_residual=Decimal("100"),
        payment_state="not_paid",
    )


class InvoiceAdapter:
    def __init__(self) -> None:
        self.mutations = 0
        self.routes: tuple[PaymentRoute, ...] = ()
        self.default_journal_id: int | None = None
        self.default_payment_method_line_id: int | None = None

    async def get_account_moves(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[AccountMove]:
        return RecordPage(items=[_move()])

    async def get_account_move_lines(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[AccountMoveLine]:
        return RecordPage(items=[_receivable_line()])

    async def get_partial_reconciliations(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[PartialReconciliation]:
        return RecordPage(
            items=[
                PartialReconciliation(
                    id=701,
                    debit_move_line=RelatedRecord(id=501, name="Receivable"),
                    credit_move_line=RelatedRecord(id=502, name="Payment"),
                    amount=Decimal("80"),
                    debit_amount_currency=Decimal("100"),
                    credit_amount_currency=Decimal("80"),
                    max_date=date(2026, 2, 10),
                )
            ]
        )

    async def get_partners(
        self, company_id: int, filters: ReadFilters, *, page: PageRequest
    ) -> RecordPage[Partner]:
        return RecordPage(items=[Partner(id=20, name="Synthetic Customer", customer_rank=1)])

    async def get_products(self, *args: object, **kwargs: object) -> RecordPage[object]:
        return RecordPage(items=[])

    async def get_account_accounts(
        self, company_id: int, filters: ReadFilters, *, page: PageRequest
    ) -> RecordPage[Account]:
        return RecordPage(
            items=[
                Account(
                    id=70,
                    code="4000",
                    name="Revenue",
                    account_type="income",
                    company_ids=(1,),
                    reconcile=False,
                )
            ]
        )

    async def get_currencies(
        self,
        company_id: int,
        currency_ids: tuple[int, ...],
        *,
        page: PageRequest,
    ) -> RecordPage[Currency]:
        return RecordPage(items=[])

    async def get_invoice_effect(self, company_id: int, invoice_id: int) -> InvoiceEffect:
        return _effect()

    async def get_payment_registration_preview(self, request: object) -> PaymentRegistrationPreview:
        return PaymentRegistrationPreview(
            invoice_id=101,
            company_id=1,
            payment_date=date(2026, 3, 1),
            amount=Decimal("100"),
            currency=RelatedRecord(id=40, name="USD"),
            payment_type="inbound",
            partner_type="customer",
            can_edit_wizard=True,
            routes=self.routes,
            default_journal_id=self.default_journal_id,
            default_payment_method_line_id=self.default_payment_method_line_id,
        )


async def test_open_invoice_reconstructs_historical_multicurrency_residual() -> None:
    response = await list_open_documents(
        InvoiceAdapter(),
        OpenDocumentsInput(company_id=1, as_of_date=date(2026, 1, 31)),
        bills=False,
        company_name="Synthetic Company",
        request_id="req-open",
    )

    assert response.summary.document_count == 1
    assert response.items[0].residual_currency == Decimal("100")
    assert response.items[0].residual_company == Decimal("80")
    assert response.items[0].overdue_days == 0
    assert response.summary.currency_totals[0].residual == Decimal("100")


async def test_invoice_preview_is_input_only_and_non_mutating() -> None:
    adapter = InvoiceAdapter()
    request = CreateInvoiceInput(
        company_id=1,
        partner_id=20,
        invoice_date=date(2026, 3, 1),
        lines=(
            InvoiceLineInput(
                description="  Consulting  ",
                quantity=Decimal("2"),
                unit_price=Decimal("12.345"),
                account_id=70,
            ),
        ),
    )

    draft, preview = await prepare_invoice_draft(adapter, request, supplier=False)

    assert adapter.mutations == 0
    assert draft.lines[0].description == "Consulting"
    assert preview.material_effects["deferred_fields"] == list(DEFERRED_INVOICE_FIELDS)
    known = preview.material_effects["known_effects"]
    assert isinstance(known, dict)
    assert known["calculation_status"] == "deferred_to_execution"
    assert known["input_subtotals"] == ["24.690"]
    assert known["input_subtotal_total"] == "24.690"
    assert "requested_currency_id" not in known
    assert not {"journal_id", "taxes", "total"} & set(known)


async def test_payment_requires_exact_route_when_odoo_has_multiple_choices() -> None:
    adapter = InvoiceAdapter()
    adapter.routes = (
        PaymentRoute(
            journal=RelatedRecord(id=1, name="Bank A"),
            payment_method_line=RelatedRecord(id=11, name="Manual"),
            payment_method_code="manual",
            payment_type="inbound",
            external_effect_status="not_initiated_by_odoo",
        ),
        PaymentRoute(
            journal=RelatedRecord(id=2, name="Bank B"),
            payment_method_line=RelatedRecord(id=22, name="Electronic"),
            payment_method_code="custom",
            payment_type="inbound",
            external_effect_status="unknown",
        ),
    )
    adapter.default_journal_id = 1
    adapter.default_payment_method_line_id = 11
    request = RegisterPaymentInput(
        invoice_id=101,
        company_id=1,
        payment_date=date(2026, 3, 1),
    )

    _effect_value, registration, preview = await prepare_payment(adapter, request)

    assert registration is None
    assert preview.needs_input is True
    assert preview.material_effects["reason_code"] == "PAYMENT_ROUTE_SELECTION_REQUIRED"
    assert len(preview.material_effects["valid_choices"]) == 2
    assert preview.material_effects["default_journal"] == {"id": 1, "name": "Bank A"}
    assert preview.material_effects["default_payment_method_line"] == {
        "id": 11,
        "name": "Manual",
    }
    assert preview.material_effects["external_effect_status"] == "not_initiated_by_odoo"
    assert adapter.mutations == 0


async def test_invoice_preview_rejects_unavailable_currency_reference() -> None:
    request = CreateInvoiceInput(
        company_id=1,
        partner_id=20,
        invoice_date=date(2026, 3, 1),
        currency_id=999,
        lines=(
            InvoiceLineInput(
                description="Consulting",
                quantity=Decimal("1"),
                unit_price=Decimal("10"),
                account_id=70,
            ),
        ),
    )

    with pytest.raises(OdooMcpError) as raised:
        await prepare_invoice_draft(InvoiceAdapter(), request, supplier=False)

    assert raised.value.code is ErrorCode.INVALID_INPUT


def test_invoice_lines_reject_nonpositive_quantity_and_negative_amount() -> None:
    with pytest.raises(ValidationError):
        InvoiceLineInput(
            description="Invalid",
            quantity=Decimal("0"),
            unit_price=Decimal("-1"),
            account_id=70,
        )


class ValidationAdapter:
    def __init__(self, effect: InvoiceEffect) -> None:
        self.effect = effect

    async def get_invoice_effect(self, company_id: int, invoice_id: int) -> InvoiceEffect:
        return self.effect

    async def get_companies(self) -> list[Company]:
        return [
            Company(
                id=1,
                name="Synthetic Company",
                currency=RelatedRecord(id=1, name="KES"),
            )
        ]

    async def get_currencies(
        self,
        company_id: int,
        currency_ids: tuple[int, ...],
        *,
        page: PageRequest,
    ) -> RecordPage[Currency]:
        return RecordPage(
            items=[
                Currency(
                    id=identifier,
                    name="KES" if identifier == 1 else "USD",
                    rounding=Decimal("0.01"),
                )
                for identifier in currency_ids
            ]
        )


def _validation_line(
    identifier: int,
    *,
    display_type: str,
    account: bool = True,
    debit: str = "0",
    credit: str = "0",
    amount_currency: str = "0",
    currency_id: int | None = 40,
    subtotal: str = "0",
    total: str = "0",
) -> InvoiceValidationLine:
    return InvoiceValidationLine(
        id=identifier,
        display_type=display_type,
        account=RelatedRecord(id=70 + identifier, name="Account") if account else None,
        debit=Decimal(debit),
        credit=Decimal(credit),
        balance=Decimal(debit) - Decimal(credit),
        currency=(RelatedRecord(id=currency_id, name="USD") if currency_id is not None else None),
        amount_currency=Decimal(amount_currency),
        subtotal=Decimal(subtotal),
        total=Decimal(total),
    )


def _validation_effect(
    lines: tuple[InvoiceValidationLine, ...],
    *,
    untaxed: str = "100",
    tax: str = "20",
    total: str = "120",
) -> InvoiceEffect:
    return _effect().model_copy(
        update={
            "state": "draft",
            "amount_untaxed": Decimal(untaxed),
            "amount_tax": Decimal(tax),
            "amount_total": Decimal(total),
            "validation_lines": lines,
        }
    )


@pytest.mark.parametrize(
    ("effect", "expected"),
    [
        (
            _validation_effect(
                (_validation_line(1, display_type="line_section", account=False),),
                untaxed="0",
                tax="0",
                total="0",
            ),
            "NO_POSTABLE_LINES",
        ),
        (
            _validation_effect(
                (
                    _validation_line(
                        1,
                        display_type="product",
                        account=False,
                        credit="100",
                        amount_currency="-100",
                        subtotal="100",
                        total="100",
                    ),
                    _validation_line(2, display_type="payment_term", debit="100"),
                ),
                tax="0",
                total="100",
            ),
            "LINE_ACCOUNT_MISSING",
        ),
        (
            _validation_effect(
                (
                    _validation_line(
                        1, display_type="product", credit="99", subtotal="100", total="120"
                    ),
                    _validation_line(2, display_type="payment_term", debit="120"),
                )
            ),
            "ENTRY_UNBALANCED",
        ),
        (
            _validation_effect(
                (
                    _validation_line(
                        1,
                        display_type="product",
                        credit="100",
                        amount_currency="-100",
                        currency_id=41,
                        subtotal="100",
                        total="120",
                    ),
                    _validation_line(2, display_type="tax", credit="20"),
                    _validation_line(3, display_type="payment_term", debit="120"),
                )
            ),
            "CURRENCY_INCONSISTENT",
        ),
        (
            _validation_effect(
                (
                    _validation_line(
                        1, display_type="product", credit="100", subtotal="90", total="110"
                    ),
                    _validation_line(2, display_type="tax", credit="20"),
                    _validation_line(3, display_type="payment_term", debit="120"),
                )
            ),
            "TOTAL_ARITHMETIC_MISMATCH",
        ),
        (
            _validation_effect(
                (
                    _validation_line(
                        1, display_type="product", credit="100", subtotal="100", total="120"
                    ),
                    _validation_line(2, display_type="tax", credit="20"),
                    _validation_line(3, display_type="payment_term", debit="120"),
                ),
                tax="10",
            ),
            "TAX_ARITHMETIC_MISMATCH",
        ),
    ],
)
async def test_validation_preview_reports_each_local_blocker(
    effect: InvoiceEffect, expected: str
) -> None:
    _state, prepared = await prepare_validation(
        ValidationAdapter(effect),
        ValidateInvoiceInput(invoice_id=101, company_id=1),
    )

    assert expected in prepared.material_effects["blocking_validation_findings"]
    assert prepared.material_effects["validation_status"] == "blocked"
    assert prepared.needs_input is True


async def test_validation_preview_is_locally_clear_but_defers_odoo_constraints() -> None:
    effect = _validation_effect(
        (
            _validation_line(1, display_type="product", credit="100", subtotal="100", total="120"),
            _validation_line(2, display_type="tax", credit="20"),
            _validation_line(3, display_type="payment_term", debit="120"),
        )
    )

    _state, prepared = await prepare_validation(
        ValidationAdapter(effect),
        ValidateInvoiceInput(invoice_id=101, company_id=1),
    )

    assert prepared.material_effects["blocking_validation_findings"] == []
    assert prepared.material_effects["validation_status"] == "locally_clear_with_deferred_checks"
    assert prepared.material_effects["deferred_checks"] == ["odoo_posting_constraints"]
    assert prepared.needs_input is False


async def test_validation_balance_uses_company_currency_precision() -> None:
    effect = _validation_effect(
        (
            _validation_line(1, display_type="product", credit="100", subtotal="100", total="120"),
            _validation_line(2, display_type="tax", credit="20"),
            _validation_line(3, display_type="payment_term", debit="120.004"),
        )
    )

    _state, prepared = await prepare_validation(
        ValidationAdapter(effect),
        ValidateInvoiceInput(invoice_id=101, company_id=1),
    )

    assert "ENTRY_UNBALANCED" not in prepared.material_effects["blocking_validation_findings"]
