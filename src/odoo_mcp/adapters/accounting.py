"""Typed workflow-facing accounting adapter values."""

from __future__ import annotations

from datetime import date as Date
from decimal import Decimal
from typing import Generic, Literal, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

FilterOperator = Literal[
    "=",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "in",
    "not in",
    "like",
    "ilike",
    "not like",
    "not ilike",
]
FilterScalar: TypeAlias = str | int | float | bool | Date | Decimal | None
FilterValue: TypeAlias = FilterScalar | tuple[FilterScalar, ...]


class AdapterValue(BaseModel):
    """Immutable value returned across the workflow-facing adapter boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class FilterClause(AdapterValue):
    field: str = Field(pattern=r"^[a-z_][a-z0-9_.]*$")
    operator: FilterOperator
    value: FilterValue


class ReadFilters(AdapterValue):
    clauses: tuple[FilterClause, ...] = ()


class PageRequest(AdapterValue):
    limit: int = Field(default=100, ge=1, le=500)
    cursor: str | None = Field(default=None, max_length=128)


DEFAULT_PAGE_REQUEST = PageRequest()


class DatePeriod(AdapterValue):
    start: Date
    end: Date

    @model_validator(mode="after")
    def validate_order(self) -> DatePeriod:
        if self.end < self.start:
            raise ValueError("period end must not precede period start")
        return self


class RelatedRecord(AdapterValue):
    id: int = Field(gt=0)
    name: str


class Currency(AdapterValue):
    id: int = Field(gt=0)
    name: str
    rounding: Decimal = Field(gt=0)


ItemT = TypeVar("ItemT", bound=AdapterValue)


class RecordPage(AdapterValue, Generic[ItemT]):
    items: list[ItemT]
    next_cursor: str | None = None


class AccountMove(AdapterValue):
    id: int = Field(gt=0)
    name: str
    move_type: str
    state: str
    date: Date
    invoice_date: Date | None = None
    invoice_date_due: Date | None = None
    partner: RelatedRecord | None = None
    journal: RelatedRecord
    company_id: int = Field(gt=0)
    currency: RelatedRecord
    amount_total: Decimal
    amount_residual: Decimal
    amount_residual_company: Decimal | None = None
    payment_state: str | None = None
    reference: str | None = None


class AccountMoveLine(AdapterValue):
    id: int = Field(gt=0)
    move: RelatedRecord
    move_state: Literal["draft", "posted"]
    account: RelatedRecord
    journal: RelatedRecord
    partner: RelatedRecord | None = None
    company_id: int = Field(gt=0)
    currency: RelatedRecord | None = None
    date: Date
    maturity_date: Date | None = None
    label: str | None = None
    debit: Decimal
    credit: Decimal
    balance: Decimal
    amount_currency: Decimal
    residual: Decimal
    residual_currency: Decimal
    reconciled: bool
    analytic_distribution: dict[str, Decimal]


class PartialReconciliation(AdapterValue):
    id: int = Field(gt=0)
    debit_move_line: RelatedRecord
    credit_move_line: RelatedRecord
    amount: Decimal
    debit_amount_currency: Decimal
    credit_amount_currency: Decimal
    max_date: Date


class Journal(AdapterValue):
    id: int = Field(gt=0)
    name: str
    code: str
    journal_type: str
    company_id: int = Field(gt=0)
    currency: RelatedRecord | None = None


class BankStatementLine(AdapterValue):
    id: int = Field(gt=0)
    date: Date
    payment_reference: str | None = None
    amount: Decimal
    amount_currency: Decimal | None = None
    foreign_currency: RelatedRecord | None = None
    partner: RelatedRecord | None = None
    journal: RelatedRecord
    company_id: int = Field(gt=0)
    reconciled: bool
    move: RelatedRecord | None = None


class PaymentTerm(AdapterValue):
    id: int = Field(gt=0)
    name: str
    company_id: int | None = Field(default=None, gt=0)


class Partner(AdapterValue):
    id: int = Field(gt=0)
    name: str
    company_id: int | None = Field(default=None, gt=0)
    customer_rank: int = Field(default=0, ge=0)
    supplier_rank: int = Field(default=0, ge=0)


class Product(AdapterValue):
    id: int = Field(gt=0)
    name: str
    default_code: str | None = None
    company_id: int | None = Field(default=None, gt=0)
    list_price: Decimal
    currency: RelatedRecord | None = None


class Account(AdapterValue):
    id: int = Field(gt=0)
    code: str
    name: str
    account_type: str
    company_ids: tuple[int, ...]
    currency: RelatedRecord | None = None
    reconcile: bool


class AnalyticAccount(AdapterValue):
    id: int = Field(gt=0)
    name: str
    code: str | None = None
    company_id: int | None = Field(default=None, gt=0)
    currency: RelatedRecord | None = None


class InvoiceTax(AdapterValue):
    id: int = Field(gt=0)
    name: str
    amount: Decimal


class InvoiceLineEffect(AdapterValue):
    id: int = Field(gt=0)
    description: str
    quantity: Decimal
    unit_price: Decimal
    subtotal: Decimal
    total: Decimal
    taxes: tuple[InvoiceTax, ...] = ()
    analytic_distribution: dict[str, Decimal] = Field(default_factory=dict)


class InvoiceValidationLine(AdapterValue):
    id: int = Field(gt=0)
    display_type: str | None = None
    account: RelatedRecord | None = None
    debit: Decimal
    credit: Decimal
    balance: Decimal
    currency: RelatedRecord | None = None
    amount_currency: Decimal
    tax_line: RelatedRecord | None = None
    subtotal: Decimal
    total: Decimal


class PaymentScheduleLine(AdapterValue):
    due_date: Date
    amount: Decimal


class InvoiceEffect(AdapterValue):
    id: int = Field(gt=0)
    name: str
    move_type: str
    state: str
    company_id: int = Field(gt=0)
    partner: RelatedRecord
    invoice_date: Date
    due_date: Date | None = None
    journal: RelatedRecord
    fiscal_position: RelatedRecord | None = None
    currency: RelatedRecord
    payment_term: RelatedRecord | None = None
    amount_untaxed: Decimal
    amount_tax: Decimal
    amount_total: Decimal
    amount_residual: Decimal
    payment_state: str | None = None
    taxes: tuple[InvoiceTax, ...] = ()
    lines: tuple[InvoiceLineEffect, ...] = ()
    payment_schedule: tuple[PaymentScheduleLine, ...] = ()
    validation_lines: tuple[InvoiceValidationLine, ...] = ()


class InvoiceDraftLine(AdapterValue):
    description: str
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    account_id: int = Field(gt=0)
    product_id: int | None = Field(default=None, gt=0)
    analytic_account_id: int | None = Field(default=None, gt=0)


class InvoiceDraft(AdapterValue):
    company_id: int = Field(gt=0)
    move_type: Literal["out_invoice", "in_invoice"]
    partner_id: int = Field(gt=0)
    invoice_date: Date
    lines: tuple[InvoiceDraftLine, ...] = Field(min_length=1, max_length=500)
    currency_id: int | None = Field(default=None, gt=0)
    payment_term_id: int | None = Field(default=None, gt=0)
    analytic_account_id: int | None = Field(default=None, gt=0)
    vendor_reference: str | None = None


class JournalEntryDraftLine(AdapterValue):
    account_id: int = Field(gt=0)
    partner_id: int | None = Field(default=None, gt=0)
    description: str | None = None
    debit: Decimal = Field(ge=0)
    credit: Decimal = Field(ge=0)
    analytic_account_id: int | None = Field(default=None, gt=0)


class JournalEntryDraft(AdapterValue):
    company_id: int = Field(gt=0)
    journal_id: int = Field(gt=0)
    entry_date: Date
    reference: str | None = None
    lines: tuple[JournalEntryDraftLine, ...] = Field(min_length=2, max_length=500)


class JournalEntryLine(AdapterValue):
    id: int = Field(gt=0)
    account: RelatedRecord
    partner: RelatedRecord | None = None
    description: str | None = None
    debit: Decimal
    credit: Decimal
    analytic_distribution: dict[str, Decimal] = Field(default_factory=dict)


class JournalEntry(AdapterValue):
    id: int = Field(gt=0)
    name: str
    move_type: str
    state: str
    date: Date
    journal: RelatedRecord
    company_id: int = Field(gt=0)
    currency: RelatedRecord
    reference: str | None = None
    lines: tuple[JournalEntryLine, ...]


class PaymentRoute(AdapterValue):
    journal: RelatedRecord
    payment_method_line: RelatedRecord
    payment_method_code: str | None = None
    payment_type: str
    external_effect_status: Literal["not_initiated_by_odoo", "unknown"]


class PaymentPreviewRequest(AdapterValue):
    invoice_id: int = Field(gt=0)
    company_id: int = Field(gt=0)
    payment_date: Date
    amount: Decimal = Field(gt=0)


class PaymentRegistrationPreview(AdapterValue):
    invoice_id: int = Field(gt=0)
    company_id: int = Field(gt=0)
    payment_date: Date
    amount: Decimal = Field(gt=0)
    currency: RelatedRecord
    payment_type: str
    partner_type: str
    can_edit_wizard: bool
    routes: tuple[PaymentRoute, ...]
    default_journal_id: int | None = Field(default=None, gt=0)
    default_payment_method_line_id: int | None = Field(default=None, gt=0)


class PaymentRegistration(AdapterValue):
    invoice_id: int = Field(gt=0)
    company_id: int = Field(gt=0)
    payment_date: Date
    amount: Decimal = Field(gt=0)
    journal_id: int = Field(gt=0)
    payment_method_line_id: int = Field(gt=0)
    payment_method_code: str | None = None
    external_effect_status: Literal["not_initiated_by_odoo", "unknown"]
