"""Typed workflow-facing Payroll adapter values."""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from odoo_mcp.adapters.accounting import RelatedRecord
from odoo_mcp.mcp.error_codes import OdooMcpError

PayrollState = Literal["draft", "waiting", "done", "paid", "cancelled"]
PayrollBatchState = Literal["draft", "ready", "done", "paid", "cancelled"]
PayrollWorkEntryState = Literal["draft", "conflict", "validated", "cancelled"]
_PositiveIdentifier = Annotated[StrictInt, Field(gt=0)]
_NonNegativeInteger = Annotated[StrictInt, Field(ge=0)]
_PageLimit = Annotated[StrictInt, Field(ge=1, le=200)]


class PayrollValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PayrollPeriod(PayrollValue):
    start: Date
    end: Date

    @model_validator(mode="after")
    def validate_period(self) -> PayrollPeriod:
        if self.end < self.start or (self.end - self.start).days > 365:
            raise ValueError("payroll period must contain 1 to 366 calendar dates")
        return self


class PayrollDiscoveryWindow(PayrollValue):
    start: Date
    end: Date

    @model_validator(mode="after")
    def validate_window(self) -> PayrollDiscoveryWindow:
        if self.start.year > Date.max.year - 5:
            latest_end = Date.max
        else:
            try:
                latest_end = self.start.replace(year=self.start.year + 5)
            except ValueError:
                latest_end = self.start.replace(year=self.start.year + 5, day=28)
        if self.end < self.start or self.end > latest_end:
            raise ValueError("payroll discovery window must not exceed five years")
        return self


class PayrollPageRequest(PayrollValue):
    limit: _PageLimit = 50
    cursor: str | None = Field(default=None, max_length=128)


DEFAULT_PAYROLL_PAGE_REQUEST = PayrollPageRequest()


ItemT = TypeVar("ItemT", bound=PayrollValue)


class PayrollPage(PayrollValue, Generic[ItemT]):
    items: list[ItemT]
    total_count: _NonNegativeInteger
    next_cursor: str | None = None


def _validate_ids(values: tuple[int, ...], *, required: bool = False) -> tuple[int, ...]:
    if required and not values:
        raise ValueError("at least one identifier is required")
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in values):
        raise ValueError("identifiers must be positive integers")
    if len(values) != len(set(values)):
        raise ValueError("identifiers must be unique")
    return values


class PayrollBatchFilters(PayrollValue):
    batch_ids: tuple[_PositiveIdentifier, ...] = Field(default=(), max_length=200)
    window: PayrollDiscoveryWindow | None = None
    states: tuple[PayrollBatchState, ...] = ()

    @model_validator(mode="after")
    def validate_filters(self) -> PayrollBatchFilters:
        _validate_ids(self.batch_ids)
        if not self.batch_ids and self.window is None:
            raise ValueError("batch IDs or a discovery window are required")
        if len(self.states) != len(set(self.states)):
            raise ValueError("states must be unique")
        return self


class PayslipFilters(PayrollValue):
    payslip_ids: tuple[_PositiveIdentifier, ...] = Field(default=(), max_length=200)
    batch_id: _PositiveIdentifier | None = None
    period: PayrollPeriod | None = None
    window: PayrollDiscoveryWindow | None = None
    employee_ids: tuple[_PositiveIdentifier, ...] = Field(default=(), max_length=100)
    states: tuple[PayrollState, ...] = ()

    @model_validator(mode="after")
    def validate_filters(self) -> PayslipFilters:
        _validate_ids(self.payslip_ids)
        _validate_ids(self.employee_ids)
        if self.period is not None and self.window is not None:
            raise ValueError("period and window are mutually exclusive")
        if (
            not self.payslip_ids
            and self.batch_id is None
            and self.period is None
            and self.window is None
        ):
            raise ValueError("a bounded payslip scope is required")
        if len(self.states) != len(set(self.states)):
            raise ValueError("states must be unique")
        return self


class PayslipChildFilters(PayrollValue):
    payslip_ids: tuple[_PositiveIdentifier, ...] = Field(min_length=1, max_length=200)
    record_ids: tuple[_PositiveIdentifier, ...] = Field(default=(), max_length=200)

    @model_validator(mode="after")
    def validate_filters(self) -> PayslipChildFilters:
        _validate_ids(self.payslip_ids, required=True)
        _validate_ids(self.record_ids)
        return self


class PayrollInputTypeFilters(PayrollValue):
    structure_id: _PositiveIdentifier
    input_type_ids: tuple[_PositiveIdentifier, ...] = Field(default=(), max_length=200)

    @model_validator(mode="after")
    def validate_filters(self) -> PayrollInputTypeFilters:
        _validate_ids(self.input_type_ids)
        return self


class PayrollContractFilters(PayrollValue):
    employee_ids: tuple[_PositiveIdentifier, ...] = Field(min_length=1, max_length=100)
    period: PayrollPeriod

    @model_validator(mode="after")
    def validate_filters(self) -> PayrollContractFilters:
        _validate_ids(self.employee_ids, required=True)
        return self


class PayrollWorkEntryFilters(PayrollValue):
    employee_ids: tuple[_PositiveIdentifier, ...] = Field(min_length=1, max_length=100)
    period: PayrollPeriod
    states: tuple[PayrollWorkEntryState, ...] = ()

    @model_validator(mode="after")
    def validate_filters(self) -> PayrollWorkEntryFilters:
        _validate_ids(self.employee_ids, required=True)
        if len(self.states) != len(set(self.states)):
            raise ValueError("states must be unique")
        return self


class PayrollBatch(PayrollValue):
    id: _PositiveIdentifier
    name: str
    date_start: Date
    date_end: Date
    state: PayrollBatchState
    source_state: str
    company_id: _PositiveIdentifier
    write_date: datetime


class Payslip(PayrollValue):
    id: _PositiveIdentifier
    name: str
    reference: str
    employee: RelatedRecord
    date_from: Date
    date_to: Date
    state: PayrollState
    source_state: str
    company_id: _PositiveIdentifier
    contract_segment: RelatedRecord
    contract_source: Literal["hr.contract", "hr.version"]
    structure: RelatedRecord
    batch: RelatedRecord | None = None
    credit_note: bool
    currency: RelatedRecord
    write_date: datetime


class PayslipLine(PayrollValue):
    id: _PositiveIdentifier
    payslip: RelatedRecord
    salary_rule: RelatedRecord
    employee: RelatedRecord
    contract_segment: RelatedRecord
    contract_source: Literal["hr.contract", "hr.version"]
    name: str
    code: str
    category: RelatedRecord
    sequence: _NonNegativeInteger
    quantity: Decimal
    rate: Decimal
    amount: Decimal
    total: Decimal
    currency: RelatedRecord
    write_date: datetime


class PayslipWorkedDay(PayrollValue):
    id: _PositiveIdentifier
    payslip: RelatedRecord
    contract_segment: RelatedRecord
    contract_source: Literal["hr.contract", "hr.version"]
    work_entry_type: RelatedRecord
    name: str
    code: str
    number_of_days: Decimal
    number_of_hours: Decimal
    amount: Decimal
    currency: RelatedRecord
    write_date: datetime


class PayslipInput(PayrollValue):
    id: _PositiveIdentifier
    name: str
    payslip: RelatedRecord
    sequence: _NonNegativeInteger
    input_type: RelatedRecord
    code: str
    amount: Decimal
    contract_segment: RelatedRecord
    contract_source: Literal["hr.contract", "hr.version"]
    write_date: datetime


class PayrollInputType(PayrollValue):
    id: _PositiveIdentifier
    name: str
    code: str
    structure_ids: tuple[_PositiveIdentifier, ...]
    active: bool
    is_quantity: bool
    available_in_attachments: bool
    write_date: datetime


class PayrollStructure(PayrollValue):
    id: _PositiveIdentifier
    input_line_type_ids: tuple[_PositiveIdentifier, ...]
    write_date: datetime


class PayrollEmployee(PayrollValue):
    id: _PositiveIdentifier
    name: str
    active: bool
    company_id: _PositiveIdentifier
    write_date: datetime


class PayrollContractSegment(PayrollValue):
    source_model: Literal["hr.contract", "hr.version"]
    id: _PositiveIdentifier
    employee: RelatedRecord
    company_id: _PositiveIdentifier
    active: bool
    source_status: Literal["draft", "open", "close", "cancel", "unavailable"]
    revision_date: Date | None = None
    effective_start: Date
    effective_end: Date
    wage: Decimal
    currency: RelatedRecord
    structure_type: RelatedRecord | None = None
    resource_calendar: RelatedRecord | None = None
    department: RelatedRecord | None = None
    job: RelatedRecord | None = None
    contract_type: RelatedRecord | None = None
    write_date: datetime


class PayrollWorkEntry(PayrollValue):
    id: _PositiveIdentifier
    employee: RelatedRecord
    company_id: _PositiveIdentifier
    contract_segment: RelatedRecord | None = None
    contract_source: Literal["hr.version"] | None = None
    date_start: Date
    date_end: Date
    duration: Decimal
    work_entry_type: RelatedRecord
    code: str
    state: PayrollWorkEntryState
    conflict: bool
    write_date: datetime


def _validate_amount(value: Decimal | None, *, required: bool) -> Decimal | None:
    if value is None:
        if required:
            raise ValueError("amount is required")
        return None
    if not value.is_finite() or abs(value) > Decimal("1000000000000"):
        raise ValueError("amount is outside the supported bound")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -6:
        raise ValueError("amount must have at most six decimal places")
    return value


class DraftPayslipInputCreate(PayrollValue):
    payslip_id: _PositiveIdentifier
    input_type_id: _PositiveIdentifier
    description: str
    amount: Decimal

    @field_validator("description")
    @classmethod
    def trim_description(cls, value: str) -> str:
        result = value.strip()
        if not result or len(result) > 200:
            raise ValueError("description must contain 1 to 200 characters")
        return result

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        result = _validate_amount(value, required=True)
        assert result is not None
        return result


class DraftPayslipInputUpdate(PayrollValue):
    payslip_id: _PositiveIdentifier
    description: str | None = None
    amount: Decimal | None = None

    @field_validator("description")
    @classmethod
    def trim_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        result = value.strip()
        if not result or len(result) > 200:
            raise ValueError("description must contain 1 to 200 characters")
        return result

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal | None) -> Decimal | None:
        return _validate_amount(value, required=False)

    @model_validator(mode="after")
    def validate_change(self) -> DraftPayslipInputUpdate:
        if self.description is None and self.amount is None:
            raise ValueError("description or amount is required")
        return self


class DeletedPayslipInput(PayrollValue):
    id: _PositiveIdentifier
    payslip_id: _PositiveIdentifier
    deleted: Literal[True] = True


class PayrollWriteRejected(RuntimeError):
    """Odoo authoritatively rejected the dispatched Payroll mutation."""

    def __init__(self, error: OdooMcpError) -> None:
        super().__init__(error.safe_message)
        self.error = error
