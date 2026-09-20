from __future__ import annotations

from datetime import date
from decimal import Decimal

from odoo_mcp.adapters.accounting import (
    Account,
    AccountMove,
    AccountMoveLine,
    InvoiceEffect,
    PageRequest,
    PartialReconciliation,
    Partner,
    PaymentRoute,
    ReadFilters,
    RecordPage,
    RelatedRecord,
)
from odoo_mcp.mcp.schemas import (
    CreateInvoiceInput,
    InvoiceLineInput,
    OpenDocumentsInput,
    RegisterPaymentInput,
)
from odoo_mcp.workflows.accounting.invoicing import (
    DEFERRED_INVOICE_FIELDS,
    list_open_documents,
    prepare_invoice_draft,
    prepare_payment,
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

    async def get_invoice_effect(self, company_id: int, invoice_id: int) -> InvoiceEffect:
        return _effect()

    async def get_payment_routes(
        self, company_id: int, invoice_id: int
    ) -> tuple[PaymentRoute, ...]:
        return self.routes


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
            payment_type="inbound",
            may_initiate_external_effect=False,
        ),
        PaymentRoute(
            journal=RelatedRecord(id=2, name="Bank B"),
            payment_method_line=RelatedRecord(id=22, name="Electronic"),
            payment_type="inbound",
            may_initiate_external_effect=True,
        ),
    )
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
