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
    payment_state: str | None = None
    reference: str | None = None


class AccountMoveLine(AdapterValue):
    id: int = Field(gt=0)
    move: RelatedRecord
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


class PaymentMethodLine(AdapterValue):
    id: int = Field(gt=0)
    name: str
    journal: RelatedRecord
    payment_method: RelatedRecord
    payment_type: str | None = None


class Partner(AdapterValue):
    id: int = Field(gt=0)
    name: str
    company_id: int | None = Field(default=None, gt=0)


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
