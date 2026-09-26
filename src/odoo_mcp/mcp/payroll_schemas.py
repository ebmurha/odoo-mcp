"""Compact public schemas for bounded Payroll evidence tools."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from itertools import pairwise
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_serializer,
    model_validator,
)

from odoo_mcp.adapters.payroll import PayrollBatchState, PayrollState, PayrollWorkEntryState
from odoo_mcp.mcp.error_codes import ErrorCode, ErrorResponse

PositiveIdentifier: TypeAlias = Annotated[StrictInt, Field(gt=0)]
PayrollLimit: TypeAlias = Annotated[StrictInt, Field(ge=1, le=200)]
PayrollCursor: TypeAlias = Annotated[str | None, Field(max_length=128)]

DEFAULT_PAYROLL_STATES: tuple[PayrollState, ...] = ("waiting", "done", "paid")
ALL_PAYROLL_STATES: tuple[PayrollState, ...] = (
    "draft",
    "waiting",
    "done",
    "paid",
    "cancelled",
)
DEFAULT_WORK_ENTRY_STATES: tuple[PayrollWorkEntryState, ...] = (
    "draft",
    "conflict",
    "validated",
)
ThresholdProfile = Literal["strict", "standard", "relaxed"]


class PayrollSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _validate_unique(values: tuple[object, ...], name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicates")


def _validate_period(start: date, end: date) -> None:
    if end < start or (end - start).days > 365:
        raise ValueError("a payroll period must contain 1 to 366 calendar dates")


def _validate_discovery_window(start: date, end: date) -> None:
    if start.year > date.max.year - 5:
        latest = date.max
    else:
        try:
            latest = start.replace(year=start.year + 5)
        except ValueError:
            latest = start.replace(year=start.year + 5, day=28)
    if end < start or end > latest:
        raise ValueError("a payroll discovery window must not exceed five years")


class PayrollListInput(PayrollSchema):
    company_id: PositiveIdentifier
    limit: PayrollLimit = 50
    cursor: PayrollCursor = None


class ListPayrollPeriodsInput(PayrollListInput):
    window_start: date
    window_end: date
    states: tuple[PayrollState, ...] = DEFAULT_PAYROLL_STATES

    @field_validator("states", mode="before")
    @classmethod
    def normalize_default_states(cls, value: object) -> object:
        return DEFAULT_PAYROLL_STATES if value in (None, (), []) else value

    @model_validator(mode="after")
    def validate_filters(self) -> ListPayrollPeriodsInput:
        _validate_discovery_window(self.window_start, self.window_end)
        _validate_unique(self.states, "states")
        return self


class GetPayrollBatchInput(PayrollListInput):
    batch_id: PositiveIdentifier
    states: tuple[PayrollState, ...] = DEFAULT_PAYROLL_STATES

    @field_validator("states", mode="before")
    @classmethod
    def normalize_default_states(cls, value: object) -> object:
        return DEFAULT_PAYROLL_STATES if value in (None, (), []) else value

    @model_validator(mode="after")
    def validate_states(self) -> GetPayrollBatchInput:
        _validate_unique(self.states, "states")
        return self


class ListPayslipsInput(PayrollListInput):
    batch_id: PositiveIdentifier | None = None
    period_start: date | None = None
    period_end: date | None = None
    employee_ids: tuple[PositiveIdentifier, ...] = Field(default=(), max_length=100)
    states: tuple[PayrollState, ...] = DEFAULT_PAYROLL_STATES

    @field_validator("states", mode="before")
    @classmethod
    def normalize_default_states(cls, value: object) -> object:
        return DEFAULT_PAYROLL_STATES if value in (None, (), []) else value

    @model_validator(mode="after")
    def validate_filters(self) -> ListPayslipsInput:
        if (self.period_start is None) != (self.period_end is None):
            raise ValueError("period_start and period_end must be supplied together")
        if self.batch_id is None and self.period_start is None:
            raise ValueError("batch_id, an exact period, or both are required")
        if self.period_start is not None and self.period_end is not None:
            _validate_period(self.period_start, self.period_end)
        _validate_unique(self.employee_ids, "employee_ids")
        _validate_unique(self.states, "states")
        return self


class GetPayslipInput(PayrollSchema):
    company_id: PositiveIdentifier
    payslip_id: PositiveIdentifier


class GetEmployeePayrollContextInput(PayrollSchema):
    company_id: PositiveIdentifier
    employee_id: PositiveIdentifier
    period_start: date
    period_end: date

    @model_validator(mode="after")
    def validate_period(self) -> GetEmployeePayrollContextInput:
        _validate_period(self.period_start, self.period_end)
        return self


class ListSalaryRulesInput(PayrollListInput):
    batch_id: PositiveIdentifier | None = None
    period_start: date | None = None
    period_end: date | None = None
    employee_ids: tuple[PositiveIdentifier, ...] = Field(default=(), max_length=100)
    states: tuple[PayrollState, ...] = DEFAULT_PAYROLL_STATES

    @field_validator("states", mode="before")
    @classmethod
    def normalize_default_states(cls, value: object) -> object:
        return DEFAULT_PAYROLL_STATES if value in (None, (), []) else value

    @model_validator(mode="after")
    def validate_filters(self) -> ListSalaryRulesInput:
        if (self.period_start is None) != (self.period_end is None):
            raise ValueError("period_start and period_end must be supplied together")
        has_batch = self.batch_id is not None
        has_period = self.period_start is not None
        if has_batch == has_period:
            raise ValueError("use exactly one of batch_id or an exact period")
        if self.period_start is not None and self.period_end is not None:
            _validate_period(self.period_start, self.period_end)
        _validate_unique(self.employee_ids, "employee_ids")
        _validate_unique(self.states, "states")
        return self


class GetAttendanceSummaryInput(PayrollListInput):
    period_start: date
    period_end: date
    employee_ids: tuple[PositiveIdentifier, ...] = Field(min_length=1, max_length=100)
    states: tuple[PayrollWorkEntryState, ...] = DEFAULT_WORK_ENTRY_STATES

    @field_validator("states", mode="before")
    @classmethod
    def normalize_default_states(cls, value: object) -> object:
        return DEFAULT_WORK_ENTRY_STATES if value in (None, (), []) else value

    @model_validator(mode="after")
    def validate_filters(self) -> GetAttendanceSummaryInput:
        _validate_period(self.period_start, self.period_end)
        _validate_unique(self.employee_ids, "employee_ids")
        _validate_unique(self.states, "states")
        return self


class PayrollPeriodRange(PayrollSchema):
    period_start: date
    period_end: date

    @model_validator(mode="after")
    def validate_period(self) -> PayrollPeriodRange:
        _validate_period(self.period_start, self.period_end)
        return self


def _validate_comparison_periods(
    baseline: PayrollPeriodRange,
    target: PayrollPeriodRange,
) -> None:
    if baseline.period_end >= target.period_start:
        raise ValueError("baseline_period must end before target_period starts")


class ComparePayrollPeriodsInput(PayrollSchema):
    company_id: PositiveIdentifier
    baseline_period: PayrollPeriodRange
    target_period: PayrollPeriodRange
    employee_ids: tuple[PositiveIdentifier, ...] = Field(default=(), max_length=100)
    states: tuple[PayrollState, ...] = DEFAULT_PAYROLL_STATES

    @field_validator("states", mode="before")
    @classmethod
    def normalize_default_states(cls, value: object) -> object:
        return DEFAULT_PAYROLL_STATES if value in (None, (), []) else value

    @model_validator(mode="after")
    def validate_filters(self) -> ComparePayrollPeriodsInput:
        _validate_comparison_periods(self.baseline_period, self.target_period)
        _validate_unique(self.employee_ids, "employee_ids")
        _validate_unique(self.states, "states")
        return self


class AnalyzeEmployeePayrollChangeInput(PayrollSchema):
    company_id: PositiveIdentifier
    employee_id: PositiveIdentifier
    baseline_period: PayrollPeriodRange
    target_period: PayrollPeriodRange

    @model_validator(mode="after")
    def validate_periods(self) -> AnalyzeEmployeePayrollChangeInput:
        _validate_comparison_periods(self.baseline_period, self.target_period)
        return self


class DetectPayrollAnomaliesInput(ComparePayrollPeriodsInput):
    history_periods: tuple[PayrollPeriodRange, ...] = Field(default=(), max_length=12)
    threshold_profile: ThresholdProfile = "standard"

    @model_validator(mode="after")
    def validate_history(self) -> DetectPayrollAnomaliesInput:
        ranges = sorted(
            (
                period.period_start,
                period.period_end,
            )
            for period in self.history_periods
        )
        if len(ranges) != len(set(ranges)):
            raise ValueError("history_periods must not contain duplicates")
        if any(period_end >= self.baseline_period.period_start for _, period_end in ranges):
            raise ValueError("history_periods must end before baseline_period starts")
        for previous, current in pairwise(ranges):
            if previous[1] >= current[0]:
                raise ValueError("history_periods must not overlap")
        return self


class PreparePayrollApprovalPackInput(PayrollSchema):
    company_id: PositiveIdentifier
    target_period: PayrollPeriodRange
    baseline_period: PayrollPeriodRange | None = None
    employee_ids: tuple[PositiveIdentifier, ...] = Field(default=(), max_length=100)
    states: tuple[PayrollState, ...] = DEFAULT_PAYROLL_STATES
    threshold_profile: ThresholdProfile = "standard"

    @field_validator("states", mode="before")
    @classmethod
    def normalize_default_states(cls, value: object) -> object:
        return DEFAULT_PAYROLL_STATES if value in (None, (), []) else value

    @model_validator(mode="after")
    def validate_filters(self) -> PreparePayrollApprovalPackInput:
        if self.baseline_period is not None:
            _validate_comparison_periods(self.baseline_period, self.target_period)
        _validate_unique(self.employee_ids, "employee_ids")
        _validate_unique(self.states, "states")
        return self


class ExplainPayslipInput(PayrollSchema):
    company_id: PositiveIdentifier
    payslip_id: PositiveIdentifier


class PayrollNamedReference(PayrollSchema):
    id: PositiveIdentifier
    name: str


class PayrollSourceReference(PayrollSchema):
    source_model: str
    source_ids: tuple[PositiveIdentifier, ...] = Field(min_length=1)


class PayrollStateCount(PayrollSchema):
    state: PayrollState
    count: int = Field(ge=0)


class PayrollCurrencyReference(PayrollNamedReference):
    pass


class PayrollPeriodItem(PayrollSchema):
    period_start: date
    period_end: date
    observed_payslip_count: int = Field(ge=0)
    non_cancelled_payslip_count: int = Field(ge=0)
    batch_ids: tuple[PositiveIdentifier, ...]
    currencies: list[PayrollCurrencyReference]
    state_counts: list[PayrollStateCount]


class PayrollPeriodsSummary(PayrollSchema):
    period_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    non_cancelled_payslip_count: int = Field(ge=0)
    has_more: bool


class CompactPayslip(PayrollSchema):
    payslip_id: PositiveIdentifier
    reference: str
    employee: PayrollNamedReference
    period_start: date
    period_end: date
    state: PayrollState
    batch: PayrollNamedReference | None
    contract_source: Literal["hr.contract", "hr.version"]
    contract: PayrollNamedReference
    structure: PayrollNamedReference
    credit_note: bool
    currency: PayrollCurrencyReference


class PayslipListSummary(PayrollSchema):
    payslip_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    state_counts: list[PayrollStateCount]
    has_more: bool


class PayrollBatchDetails(PayrollSchema):
    batch_id: PositiveIdentifier
    name: str
    period_start: date
    period_end: date
    state: PayrollBatchState


class ObservedRuleTotal(PayrollSchema):
    currency: PayrollCurrencyReference
    salary_rule: PayrollNamedReference
    code: str
    total: Decimal


class ObservedCategoryTotal(PayrollSchema):
    currency: PayrollCurrencyReference
    category: PayrollNamedReference
    total: Decimal


class PayrollBatchSummary(PayrollSchema):
    non_cancelled_payslip_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    employee_count: int = Field(ge=0)
    state_counts: list[PayrollStateCount]
    rule_totals: list[ObservedRuleTotal]
    category_totals: list[ObservedCategoryTotal]
    has_more: bool


class PayrollSalaryLineItem(PayrollSchema):
    source_line_id: PositiveIdentifier
    salary_rule: PayrollNamedReference
    category: PayrollNamedReference
    name: str
    code: str
    sequence: int = Field(ge=0)
    quantity: Decimal
    rate: Decimal
    amount: Decimal
    total: Decimal
    currency: PayrollCurrencyReference


class PayrollWorkedDayItem(PayrollSchema):
    source_line_id: PositiveIdentifier
    contract_source: Literal["hr.contract", "hr.version"]
    contract: PayrollNamedReference
    work_entry_type: PayrollNamedReference
    name: str
    code: str
    days: Decimal
    hours: Decimal


class PayrollInputItem(PayrollSchema):
    source_input_id: PositiveIdentifier
    input_type: PayrollNamedReference
    contract_source: Literal["hr.contract", "hr.version"]
    contract: PayrollNamedReference
    name: str
    code: str
    amount: Decimal | None
    quantity: Decimal | None
    currency: PayrollCurrencyReference
    sequence: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_value(self) -> PayrollInputItem:
        if (self.amount is None) == (self.quantity is None):
            raise ValueError("exactly one input value representation is required")
        return self


class PayrollEmployeeDetails(PayrollSchema):
    employee_id: PositiveIdentifier
    name: str
    active: bool


class PayrollContractSegmentItem(PayrollSchema):
    source_model: Literal["hr.contract", "hr.version"]
    source_id: PositiveIdentifier
    effective_start: date
    effective_end: date
    revision_date: date | None
    active: bool
    source_status: Literal["draft", "open", "close", "cancel", "unavailable"]
    wage: Decimal
    currency: PayrollCurrencyReference
    structure_type: PayrollNamedReference | None
    working_schedule: PayrollNamedReference | None
    department: PayrollNamedReference | None
    job: PayrollNamedReference | None
    contract_type: PayrollNamedReference | None


class SalaryRuleSnapshot(PayrollSchema):
    salary_rule: PayrollNamedReference
    code: str
    category: PayrollNamedReference
    sequence: int = Field(ge=0)
    affected_payslip_count: int = Field(ge=0)
    currencies: list[PayrollCurrencyReference]


class SalaryRulesSummary(PayrollSchema):
    rule_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    affected_payslip_count: int = Field(ge=0)
    has_more: bool


class AttendanceSummaryItem(PayrollSchema):
    employee: PayrollNamedReference
    work_entry_type: PayrollNamedReference
    code: str
    state: PayrollWorkEntryState
    entry_count: int = Field(ge=0)
    hours: Decimal
    earliest_source_date: date
    latest_source_date: date
    conflict_count: int = Field(ge=0)


class AttendanceSummary(PayrollSchema):
    selected_employee_count: int = Field(ge=0)
    employees_with_entries: int = Field(ge=0)
    entry_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    hours: Decimal
    conflict_count: int = Field(ge=0)
    has_more: bool


PayrollChangeKind = Literal[
    "unchanged",
    "increased",
    "decreased",
    "added",
    "removed",
    "unavailable",
]
PayrollFindingCode = Literal[
    "new_employee",
    "missing_employee",
    "employee_count_change",
    "contract_change",
    "rule_line_added",
    "rule_line_removed",
    "currency_change",
    "monetary_deviation",
    "hours_deviation",
    "work_entry_conflict",
    "statistical_deviation",
]
PayrollFindingSeverity = Literal["info", "review", "critical"]


class PayrollCountComparison(PayrollSchema):
    baseline: int = Field(ge=0)
    target: int = Field(ge=0)
    absolute_delta: int
    percentage_delta: Decimal | None
    change: PayrollChangeKind


class PayrollDecimalComparison(PayrollSchema):
    baseline: Decimal
    target: Decimal
    absolute_delta: Decimal
    percentage_delta: Decimal | None
    change: PayrollChangeKind


class PayrollEmployeeSetChanges(PayrollSchema):
    new_employees: list[PayrollNamedReference]
    missing_employees: list[PayrollNamedReference]
    common_employees: list[PayrollNamedReference]


class PayrollPayslipTotalComparison(PayrollSchema):
    employee: PayrollNamedReference
    currency: PayrollCurrencyReference
    baseline_payslip_ids: tuple[PositiveIdentifier, ...]
    target_payslip_ids: tuple[PositiveIdentifier, ...]
    observed_line_total: PayrollDecimalComparison
    source_refs: list[PayrollSourceReference]


class PayrollRuleTotalComparison(PayrollSchema):
    currency: PayrollCurrencyReference
    salary_rule: PayrollNamedReference
    code: str
    category: PayrollNamedReference
    total: PayrollDecimalComparison
    source_refs: list[PayrollSourceReference]


class PayrollCategoryTotalComparison(PayrollSchema):
    currency: PayrollCurrencyReference
    category: PayrollNamedReference
    total: PayrollDecimalComparison
    source_refs: list[PayrollSourceReference]


class PayrollRecognizedCurrencyTotal(PayrollSchema):
    currency: PayrollCurrencyReference
    total: Decimal
    source_refs: list[PayrollSourceReference]


class PayrollRecognizedPeriodValue(PayrollSchema):
    status: Literal["available", "unavailable"]
    values: list[PayrollRecognizedCurrencyTotal]
    reason: str | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> PayrollRecognizedPeriodValue:
        if self.status == "available" and (not self.values or self.reason is not None):
            raise ValueError("available recognized totals require values only")
        if self.status == "unavailable" and (self.values or self.reason is None):
            raise ValueError("unavailable recognized totals require a reason only")
        return self


class PayrollRecognizedRuleComparison(PayrollSchema):
    code: Literal["BASIC", "GROSS", "NET"]
    recognition_basis: Literal["exact_rule_code"] = "exact_rule_code"
    baseline: PayrollRecognizedPeriodValue
    target: PayrollRecognizedPeriodValue


class PayrollRecognizedRuleValue(PayrollSchema):
    code: Literal["BASIC", "GROSS", "NET"]
    recognition_basis: Literal["exact_rule_code"] = "exact_rule_code"
    value: PayrollRecognizedPeriodValue


class PayrollUnavailableMetric(PayrollSchema):
    status: Literal["unavailable"] = "unavailable"
    reason: str


class PayrollApprovalPackVariance(PayrollSchema):
    status: Literal["available", "not_requested"]
    baseline_period: PayrollPeriodRange | None = None
    headcount: PayrollCountComparison | None = None
    employee_changes: PayrollEmployeeSetChanges | None = None
    payslip_totals: list[PayrollPayslipTotalComparison] | None = None
    rule_totals: list[PayrollRuleTotalComparison] | None = None
    category_totals: list[PayrollCategoryTotalComparison] | None = None
    recognized_rule_totals: list[PayrollRecognizedRuleComparison] | None = None
    changed_employees: list[PayrollNamedReference] | None = None

    @model_validator(mode="after")
    def validate_status(self) -> PayrollApprovalPackVariance:
        details = (
            self.baseline_period,
            self.headcount,
            self.employee_changes,
            self.payslip_totals,
            self.rule_totals,
            self.category_totals,
            self.recognized_rule_totals,
            self.changed_employees,
        )
        if self.status == "available" and any(value is None for value in details):
            raise ValueError("available variance requires all comparison fields")
        if self.status == "not_requested" and any(value is not None for value in details):
            raise ValueError("not-requested variance cannot contain comparison fields")
        return self


class PayrollReviewAction(PayrollSchema):
    action: str
    source_refs: list[PayrollSourceReference]


class PayrollEvidenceChange(PayrollSchema):
    fact: str
    baseline_value: str | None
    target_value: str | None
    source_refs: list[PayrollSourceReference]


class PayrollContractFactChange(PayrollEvidenceChange):
    employee: PayrollNamedReference


class PayrollLineChange(PayrollSchema):
    employee: PayrollNamedReference
    currency: PayrollCurrencyReference | None
    salary_rule: PayrollNamedReference
    code: str
    category: PayrollNamedReference
    baseline_total: Decimal | None
    target_total: Decimal | None
    absolute_delta: Decimal | None
    percentage_delta: Decimal | None
    change: PayrollChangeKind
    source_refs: list[PayrollSourceReference]
    limitations: list[str]


class PayrollWorkEntryHoursComparison(PayrollSchema):
    employee: PayrollNamedReference
    work_entry_type: PayrollNamedReference
    code: str
    hours: PayrollDecimalComparison
    conflict_count: int = Field(ge=0)
    source_refs: list[PayrollSourceReference]


class PayrollFinding(PayrollSchema):
    finding_code: PayrollFindingCode
    severity: PayrollFindingSeverity
    employee: PayrollNamedReference | None
    baseline_period: PayrollPeriodRange | None
    target_period: PayrollPeriodRange
    currency: PayrollCurrencyReference | None
    salary_rule: PayrollNamedReference | None
    code: str | None
    baseline_value: str | None
    target_value: str | None
    absolute_delta: Decimal | None
    percentage_delta: Decimal | None
    observed_delta: Decimal | None
    calculation: str
    rule: str
    threshold: Decimal | None
    threshold_profile: ThresholdProfile | None
    source_refs: list[PayrollSourceReference]
    evidence: list[PayrollEvidenceChange]
    correlations: list[PayrollEvidenceChange]
    limitations: list[str]


class PayrollExplanationGroup(PayrollSchema):
    category: PayrollNamedReference
    currency: PayrollCurrencyReference
    observed_total: Decimal
    lines: list[PayrollSalaryLineItem]


class PayrollLineArithmetic(PayrollSchema):
    source_line_id: PositiveIdentifier
    quantity: Decimal
    rate: Decimal
    amount: Decimal
    odoo_returned_total: Decimal
    description: str


class PayrollSuccess(PayrollSchema):
    status: Literal["ok"] = "ok"
    request_id: str
    company_id: PositiveIdentifier
    observed_at: datetime
    source_refs: list[PayrollSourceReference]
    limitations: list[str]


class ComparePayrollPeriodsResponse(PayrollSuccess):
    baseline_period: PayrollPeriodRange
    target_period: PayrollPeriodRange
    headcount: PayrollCountComparison
    employee_changes: PayrollEmployeeSetChanges
    payslip_totals: list[PayrollPayslipTotalComparison]
    rule_totals: list[PayrollRuleTotalComparison]
    category_totals: list[PayrollCategoryTotalComparison]
    recognized_rule_totals: list[PayrollRecognizedRuleComparison]
    employer_cost: PayrollUnavailableMetric
    contract_changes: list[PayrollContractFactChange]
    work_entry_hours: list[PayrollWorkEntryHoursComparison]
    changed_employees: list[PayrollNamedReference]
    findings: list[PayrollFinding]


class AnalyzeEmployeePayrollChangeResponse(PayrollSuccess):
    employee: PayrollNamedReference
    baseline_period: PayrollPeriodRange
    target_period: PayrollPeriodRange
    baseline_payslips: list[CompactPayslip]
    target_payslips: list[CompactPayslip]
    line_changes: list[PayrollLineChange]
    contract_changes: list[PayrollContractFactChange]
    work_entry_hours: list[PayrollWorkEntryHoursComparison]
    demonstrated_contributors: list[PayrollEvidenceChange]
    correlations: list[PayrollEvidenceChange]
    unresolved_causes: list[str]
    findings: list[PayrollFinding]


class DetectPayrollAnomaliesResponse(PayrollSuccess):
    baseline_period: PayrollPeriodRange
    target_period: PayrollPeriodRange
    threshold_profile: ThresholdProfile
    evaluated_employee_count: int = Field(ge=0)
    statistical_period_count: int = Field(ge=0)
    findings: list[PayrollFinding]
    rendered_markdown: str


class PreparePayrollApprovalPackResponse(PayrollSuccess):
    company: PayrollNamedReference
    target_period: PayrollPeriodRange
    threshold_profile: ThresholdProfile
    headcount: int = Field(ge=0)
    rule_totals: list[ObservedRuleTotal]
    category_totals: list[ObservedCategoryTotal]
    recognized_rule_totals: list[PayrollRecognizedRuleValue]
    employer_cost: PayrollUnavailableMetric
    variance: PayrollApprovalPackVariance
    anomalies: list[PayrollFinding]
    exceptions: list[PayrollFinding]
    unresolved_issues: list[str]
    recommended_review_actions: list[PayrollReviewAction]
    sign_off_checklist: list[str]
    rendered_markdown: str


class ExplainPayslipResponse(PayrollSuccess):
    payslip: CompactPayslip
    line_groups: list[PayrollExplanationGroup]
    line_arithmetic: list[PayrollLineArithmetic]
    worked_days: list[PayrollWorkedDayItem]
    inputs: list[PayrollInputItem]
    contract_segments: list[PayrollContractSegmentItem]
    recognized_rule_totals: list[PayrollRecognizedRuleValue]
    employer_cost: PayrollUnavailableMetric
    missing_evidence: list[str]


class ListPayrollPeriodsResponse(PayrollSuccess):
    items: list[PayrollPeriodItem]
    next_cursor: str | None
    summary: PayrollPeriodsSummary


class GetPayrollBatchResponse(PayrollSuccess):
    batch: PayrollBatchDetails
    items: list[CompactPayslip]
    next_cursor: str | None
    summary: PayrollBatchSummary


class ListPayslipsResponse(PayrollSuccess):
    items: list[CompactPayslip]
    next_cursor: str | None
    summary: PayslipListSummary


class GetPayslipResponse(PayrollSuccess):
    payslip: CompactPayslip
    salary_lines: list[PayrollSalaryLineItem]
    worked_days: list[PayrollWorkedDayItem]
    inputs: list[PayrollInputItem]


class GetEmployeePayrollContextResponse(PayrollSuccess):
    employee: PayrollEmployeeDetails
    contract_segments: list[PayrollContractSegmentItem]


class ListSalaryRulesResponse(PayrollSuccess):
    items: list[SalaryRuleSnapshot]
    next_cursor: str | None
    summary: SalaryRulesSummary


class GetAttendanceSummaryResponse(PayrollSuccess):
    items: list[AttendanceSummaryItem]
    next_cursor: str | None
    summary: AttendanceSummary


PayrollEvidenceResponse: TypeAlias = (
    ListPayrollPeriodsResponse
    | GetPayrollBatchResponse
    | ListPayslipsResponse
    | GetPayslipResponse
    | GetEmployeePayrollContextResponse
    | ListSalaryRulesResponse
    | GetAttendanceSummaryResponse
    | ComparePayrollPeriodsResponse
    | AnalyzeEmployeePayrollChangeResponse
    | DetectPayrollAnomaliesResponse
    | PreparePayrollApprovalPackResponse
    | ExplainPayslipResponse
)


class PayrollToolResponse(PayrollSchema):
    """One object-shaped output schema for Payroll success or failure."""

    status: Literal["ok", "failed"]
    request_id: str
    company_id: PositiveIdentifier | None = None
    observed_at: datetime | None = None
    source_refs: list[PayrollSourceReference] | None = None
    limitations: list[str] | None = None
    items: (
        list[PayrollPeriodItem | CompactPayslip | SalaryRuleSnapshot | AttendanceSummaryItem] | None
    ) = None
    next_cursor: str | None = None
    summary: (
        PayrollPeriodsSummary
        | PayrollBatchSummary
        | PayslipListSummary
        | SalaryRulesSummary
        | AttendanceSummary
        | None
    ) = None
    batch: PayrollBatchDetails | None = None
    payslip: CompactPayslip | None = None
    salary_lines: list[PayrollSalaryLineItem] | None = None
    worked_days: list[PayrollWorkedDayItem] | None = None
    inputs: list[PayrollInputItem] | None = None
    employee: PayrollEmployeeDetails | PayrollNamedReference | None = None
    contract_segments: list[PayrollContractSegmentItem] | None = None
    baseline_period: PayrollPeriodRange | None = None
    target_period: PayrollPeriodRange | None = None
    headcount: PayrollCountComparison | int | None = None
    employee_changes: PayrollEmployeeSetChanges | None = None
    payslip_totals: list[PayrollPayslipTotalComparison] | None = None
    rule_totals: list[PayrollRuleTotalComparison] | list[ObservedRuleTotal] | None = None
    category_totals: list[PayrollCategoryTotalComparison] | list[ObservedCategoryTotal] | None = (
        None
    )
    recognized_rule_totals: (
        list[PayrollRecognizedRuleComparison] | list[PayrollRecognizedRuleValue] | None
    ) = None
    employer_cost: PayrollUnavailableMetric | None = None
    contract_changes: list[PayrollContractFactChange] | None = None
    work_entry_hours: list[PayrollWorkEntryHoursComparison] | None = None
    changed_employees: list[PayrollNamedReference] | None = None
    findings: list[PayrollFinding] | None = None
    company: PayrollNamedReference | None = None
    variance: PayrollApprovalPackVariance | None = None
    anomalies: list[PayrollFinding] | None = None
    exceptions: list[PayrollFinding] | None = None
    unresolved_issues: list[str] | None = None
    recommended_review_actions: list[PayrollReviewAction] | None = None
    sign_off_checklist: list[str] | None = None
    baseline_payslips: list[CompactPayslip] | None = None
    target_payslips: list[CompactPayslip] | None = None
    line_changes: list[PayrollLineChange] | None = None
    demonstrated_contributors: list[PayrollEvidenceChange] | None = None
    correlations: list[PayrollEvidenceChange] | None = None
    unresolved_causes: list[str] | None = None
    threshold_profile: ThresholdProfile | None = None
    evaluated_employee_count: int | None = None
    statistical_period_count: int | None = None
    rendered_markdown: str | None = None
    line_groups: list[PayrollExplanationGroup] | None = None
    line_arithmetic: list[PayrollLineArithmetic] | None = None
    missing_evidence: list[str] | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    remediation_hint: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> PayrollToolResponse:
        common = (self.company_id, self.observed_at, self.source_refs, self.limitations)
        errors = (self.error_code, self.error_message, self.remediation_hint)
        if self.status == "ok" and (any(value is None for value in common) or any(errors)):
            raise ValueError("invalid Payroll success response")
        if self.status == "failed" and (
            any(value is not None for value in common) or any(value is None for value in errors)
        ):
            raise ValueError("invalid Payroll failure response")
        return self

    @model_serializer(mode="wrap")
    def omit_nulls(self, handler: object) -> dict[str, object]:
        serialized = handler(self)  # type: ignore[operator]
        return {key: value for key, value in serialized.items() if value is not None}

    @classmethod
    def from_success(cls, response: PayrollEvidenceResponse) -> PayrollToolResponse:
        return cls.model_validate(response.model_dump())

    @classmethod
    def from_error(cls, response: ErrorResponse) -> PayrollToolResponse:
        return cls.model_validate(response.model_dump())
