"""Compact public MCP response schemas."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from odoo_mcp.mcp.error_codes import ErrorCode, ErrorResponse


class CapabilityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    available: bool


class AuthorizedCompany(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    is_default: bool


class OdooRuntimeInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edition: str
    major_version: int
    transport: str


class CapabilitiesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    request_id: str
    odoo: OdooRuntimeInfo
    installed_modules: list[CapabilityItem]
    available_tools: list[str]
    authorized_companies: list[AuthorizedCompany]


class CapabilitiesToolResponse(BaseModel):
    """One object-shaped output schema for success and structured failure."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "failed"]
    request_id: str
    odoo: OdooRuntimeInfo | None = None
    installed_modules: list[CapabilityItem] | None = None
    available_tools: list[str] | None = None
    authorized_companies: list[AuthorizedCompany] | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    remediation_hint: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> CapabilitiesToolResponse:
        success_fields = (
            self.odoo,
            self.installed_modules,
            self.available_tools,
            self.authorized_companies,
        )
        error_fields = (self.error_code, self.error_message, self.remediation_hint)
        if self.status == "ok" and (
            any(item is None for item in success_fields) or any(error_fields)
        ):
            raise ValueError("invalid success response")
        if self.status == "failed" and (
            any(item is not None for item in success_fields)
            or any(item is None for item in error_fields)
        ):
            raise ValueError("invalid failure response")
        return self

    @model_serializer(mode="wrap")
    def omit_nulls(self, handler: object) -> dict[str, object]:
        serialized = handler(self)  # type: ignore[operator]
        return {key: value for key, value in serialized.items() if value is not None}

    @classmethod
    def from_success(cls, response: CapabilitiesResponse) -> CapabilitiesToolResponse:
        return cls(**response.model_dump())

    @classmethod
    def from_error(cls, response: ErrorResponse) -> CapabilitiesToolResponse:
        return cls(**response.model_dump())


class AccountingReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: int = Field(gt=0)
    limit: int = Field(default=100, ge=1, le=500)
    cursor: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class TrialBalanceInput(AccountingReadInput):
    period_start: date
    period_end: date
    account_ids: tuple[int, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def validate_period_and_accounts(self) -> TrialBalanceInput:
        if self.period_end < self.period_start:
            raise ValueError("period_end must not precede period_start")
        if any(identifier <= 0 for identifier in self.account_ids):
            raise ValueError("account_ids must contain positive integers")
        if len(set(self.account_ids)) != len(self.account_ids):
            raise ValueError("account_ids must not contain duplicates")
        return self


class AgingInput(AccountingReadInput):
    as_of_date: date
    partner_ids: tuple[int, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def validate_partners(self) -> AgingInput:
        if any(identifier <= 0 for identifier in self.partner_ids):
            raise ValueError("partner_ids must contain positive integers")
        if len(set(self.partner_ids)) != len(self.partner_ids):
            raise ValueError("partner_ids must not contain duplicates")
        return self


class CashbookInput(AccountingReadInput):
    period_start: date
    period_end: date
    journal_ids: tuple[int, ...] = Field(default=(), max_length=500)
    partner_ids: tuple[int, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def validate_filters(self) -> CashbookInput:
        if self.period_end < self.period_start:
            raise ValueError("period_end must not precede period_start")
        for name, identifiers in (
            ("journal_ids", self.journal_ids),
            ("partner_ids", self.partner_ids),
        ):
            if any(identifier <= 0 for identifier in identifiers):
                raise ValueError(f"{name} must contain positive integers")
            if len(set(identifiers)) != len(identifiers):
                raise ValueError(f"{name} must not contain duplicates")
        return self


class UnmatchedStatementLinesInput(AccountingReadInput):
    period_start: date
    period_end: date
    journal_id: int | None = Field(default=None, gt=0)
    match_confidence_threshold: Decimal = Field(
        default=Decimal("0.85"), ge=Decimal("0"), le=Decimal("1")
    )

    @model_validator(mode="after")
    def validate_period(self) -> UnmatchedStatementLinesInput:
        if self.period_end < self.period_start:
            raise ValueError("period_end must not precede period_start")
        return self


class ReconciliationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: int = Field(gt=0)
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    bank_journal_id: int = Field(gt=0)
    statement_line_ids: tuple[int, ...] = Field(min_length=1, max_length=500)
    match_confidence_threshold: Decimal = Field(
        default=Decimal("0.85"), ge=Decimal("0"), le=Decimal("1")
    )
    dry_run: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_statement_lines(self) -> ReconciliationInput:
        year, month = (int(part) for part in self.period.split("-", 1))
        try:
            date(year, month, 1)
        except ValueError as exc:
            raise ValueError("period must be a valid calendar month") from exc
        if any(identifier <= 0 for identifier in self.statement_line_ids):
            raise ValueError("statement_line_ids must contain positive integers")
        if len(set(self.statement_line_ids)) != len(self.statement_line_ids):
            raise ValueError("statement_line_ids must not contain duplicates")
        return self


class OpenDocumentsInput(AccountingReadInput):
    as_of_date: date
    partner_ids: tuple[int, ...] = Field(default=(), max_length=500)
    overdue_only: bool = False

    @model_validator(mode="after")
    def validate_partners(self) -> OpenDocumentsInput:
        if any(identifier <= 0 for identifier in self.partner_ids):
            raise ValueError("partner_ids must contain positive integers")
        if len(set(self.partner_ids)) != len(self.partner_ids):
            raise ValueError("partner_ids must not contain duplicates")
        return self


class InvoiceLineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=500)
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    account_id: int = Field(gt=0)
    product_id: int | None = Field(default=None, gt=0)
    analytic_account_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_description(self) -> InvoiceLineInput:
        if not self.description.strip():
            raise ValueError("description must not be blank")
        return self


class CreateInvoiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: int = Field(gt=0)
    partner_id: int = Field(gt=0)
    invoice_date: date
    lines: tuple[InvoiceLineInput, ...] = Field(min_length=1, max_length=500)
    currency_id: int | None = Field(default=None, gt=0)
    payment_term_id: int | None = Field(default=None, gt=0)
    analytic_account_id: int | None = Field(default=None, gt=0)
    vendor_reference: str | None = Field(default=None, max_length=500)
    dry_run: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_reference(self) -> CreateInvoiceInput:
        if self.vendor_reference is not None and not self.vendor_reference.strip():
            raise ValueError("vendor_reference must not be blank")
        return self


class ValidateInvoiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_id: int = Field(gt=0)
    company_id: int = Field(gt=0)
    dry_run: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class CreditNoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: int = Field(gt=0)
    original_move_id: int = Field(gt=0)
    credit_date: date
    reason: str = Field(min_length=1, max_length=500)
    dry_run: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_reason(self) -> CreditNoteInput:
        if not self.reason.strip():
            raise ValueError("reason must not be blank")
        return self


class RegisterPaymentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_id: int = Field(gt=0)
    company_id: int = Field(gt=0)
    payment_date: date
    amount: Decimal | None = Field(default=None, gt=0)
    journal_id: int | None = Field(default=None, gt=0)
    payment_method_line_id: int | None = Field(default=None, gt=0)
    dry_run: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_route(self) -> RegisterPaymentInput:
        if (self.journal_id is None) is not (self.payment_method_line_id is None):
            raise ValueError("journal_id and payment_method_line_id must be supplied together")
        return self


class JournalEntriesInput(AccountingReadInput):
    period_start: date
    period_end: date
    journal_ids: tuple[int, ...] = Field(default=(), max_length=500)
    states: tuple[Literal["draft", "posted"], ...] = Field(default=(), max_length=2)

    @model_validator(mode="after")
    def validate_filters(self) -> JournalEntriesInput:
        if self.period_end < self.period_start:
            raise ValueError("period_end must not precede period_start")
        if any(identifier <= 0 for identifier in self.journal_ids):
            raise ValueError("journal_ids must contain positive integers")
        if len(set(self.journal_ids)) != len(self.journal_ids):
            raise ValueError("journal_ids must not contain duplicates")
        if len(set(self.states)) != len(self.states):
            raise ValueError("states must not contain duplicates")
        return self


class JournalEntryLineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int = Field(gt=0)
    partner_id: int | None = Field(default=None, gt=0)
    description: str | None = Field(default=None, max_length=500)
    debit: Decimal = Field(ge=0)
    credit: Decimal = Field(ge=0)
    analytic_account_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_amounts(self) -> JournalEntryLineInput:
        if (self.debit > 0) == (self.credit > 0):
            raise ValueError("exactly one of debit or credit must be positive")
        if self.description is not None and not self.description.strip():
            raise ValueError("description must not be blank")
        return self


class CreateJournalEntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: int = Field(gt=0)
    journal_id: int = Field(gt=0)
    entry_date: date
    reference: str | None = Field(default=None, max_length=500)
    lines: tuple[JournalEntryLineInput, ...] = Field(min_length=2, max_length=500)
    dry_run: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_reference(self) -> CreateJournalEntryInput:
        if self.reference is not None and not self.reference.strip():
            raise ValueError("reference must not be blank")
        return self


class PostJournalEntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: int = Field(gt=0)
    move_id: int = Field(gt=0)
    dry_run: bool = True
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class TrialBalanceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int
    code: str
    name: str
    opening_balance: Decimal
    period_debit: Decimal
    period_credit: Decimal
    closing_balance: Decimal


class TrialBalanceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_count: int
    opening_balance: Decimal
    period_debit: Decimal
    period_credit: Decimal
    closing_balance: Decimal


class AgingBuckets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    not_yet_due: Decimal
    days_1_30: Decimal
    days_31_60: Decimal
    days_61_90: Decimal
    days_90_plus: Decimal


class AgingItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    partner_id: int
    partner_name: str
    residual_total: Decimal
    buckets: AgingBuckets
    oldest_due_date: date


class AgingSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    partner_count: int
    residual_total: Decimal
    buckets: AgingBuckets


class CashbookItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: int
    move_id: int
    journal_id: int
    journal_name: str
    date: date
    partner_id: int | None
    partner_name: str | None
    reference: str | None
    currency_id: int | None
    currency_name: str
    debit: Decimal
    credit: Decimal
    amount: Decimal


class CashbookSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_count: int
    opening_balance: Decimal
    total_debit: Decimal
    total_credit: Decimal
    closing_balance: Decimal


UnmatchedReason = Literal[
    "no_eligible_candidate",
    "below_confidence_threshold",
    "ambiguous_best_match",
    "candidate_conflict",
]


class UnmatchedStatementLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement_line_id: int
    date: date
    amount: Decimal
    currency_id: int | None
    currency_name: str
    partner_id: int | None
    partner_name: str | None
    reference: str | None
    best_rejected_score: Decimal | None
    reason_code: UnmatchedReason


class UnmatchedStatementLinesSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement_line_count: int
    unmatched_count: int
    no_candidate_count: int
    below_threshold_count: int
    ambiguous_count: int
    conflict_count: int


class ReconciliationMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement_line_id: int
    move_line_id: int
    amount: Decimal
    currency_id: int | None
    currency_name: str
    confidence: Decimal
    score_components: dict[str, Decimal]


class ReconciliationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement_line_count: int
    matched_count: int
    unmatched_count: int
    currency_id: int | None
    currency_name: str
    matched_amount: Decimal
    unmatched_amount: Decimal


class ReconciliationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_id: int
    period: str
    bank_journal_id: int
    matches: list[ReconciliationMatch]
    unmatched: list[UnmatchedStatementLineItem]
    summary: ReconciliationSummary
    risk_flags: list[str]
    artifact_markdown: str


class OpenDocumentItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    move_id: int
    partner_id: int
    partner_name: str
    number: str
    invoice_date: date
    due_date: date | None
    currency_id: int
    currency_name: str
    amount_total: Decimal
    residual_currency: Decimal
    residual_company: Decimal
    payment_state: str | None
    overdue_days: int


class CurrencyResidualTotal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency_id: int
    currency_name: str
    residual: Decimal


class OpenDocumentsSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_count: int
    currency_totals: list[CurrencyResidualTotal]
    company_currency_residual: Decimal


class OpenDocumentsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    request_id: str
    company_id: int
    as_of_date: date
    items: list[OpenDocumentItem]
    next_cursor: str | None
    summary: OpenDocumentsSummary
    artifact_markdown: str


class JournalEntryLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: int
    account_id: int
    account_name: str
    partner_id: int | None
    partner_name: str | None
    description: str | None
    debit: Decimal
    credit: Decimal
    analytic_ids: list[int]


class JournalEntryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    move_id: int
    name: str
    date: date
    journal_id: int
    journal_name: str
    state: Literal["draft", "posted"]
    reference: str | None
    currency_id: int
    currency_name: str
    total_debit: Decimal
    total_credit: Decimal
    lines: list[JournalEntryLineItem]
    lines_truncated: bool
    lines_cursor: str | None


class JournalEntriesSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_count: int
    draft_count: int
    posted_count: int


class JournalEntriesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    request_id: str
    company_id: int
    period_start: date
    period_end: date
    items: list[JournalEntryItem]
    next_cursor: str | None
    summary: JournalEntriesSummary
    artifact_markdown: str


class TrialBalanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    request_id: str
    company_id: int
    period_start: date
    period_end: date
    items: list[TrialBalanceItem]
    next_cursor: str | None
    summary: TrialBalanceSummary
    artifact_markdown: str


class AgingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    request_id: str
    company_id: int
    as_of_date: date
    items: list[AgingItem]
    next_cursor: str | None
    summary: AgingSummary
    artifact_markdown: str


class CashbookResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    request_id: str
    company_id: int
    period_start: date
    period_end: date
    items: list[CashbookItem]
    next_cursor: str | None
    summary: CashbookSummary
    artifact_markdown: str


class UnmatchedStatementLinesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    request_id: str
    company_id: int
    period_start: date
    period_end: date
    items: list[UnmatchedStatementLineItem]
    next_cursor: str | None
    summary: UnmatchedStatementLinesSummary
    artifact_markdown: str


class AccountingToolResponse(BaseModel):
    """One compact response shape for accounting report success or failure."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "failed"]
    request_id: str
    company_id: int | None = None
    period_start: date | None = None
    period_end: date | None = None
    as_of_date: date | None = None
    items: (
        list[
            TrialBalanceItem
            | AgingItem
            | CashbookItem
            | UnmatchedStatementLineItem
            | OpenDocumentItem
            | JournalEntryItem
        ]
        | None
    ) = None
    next_cursor: str | None = None
    summary: (
        TrialBalanceSummary
        | AgingSummary
        | CashbookSummary
        | UnmatchedStatementLinesSummary
        | OpenDocumentsSummary
        | JournalEntriesSummary
        | None
    ) = None
    artifact_markdown: str | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    remediation_hint: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> AccountingToolResponse:
        error_fields = (self.error_code, self.error_message, self.remediation_hint)
        if self.status == "ok" and (
            self.company_id is None
            or self.items is None
            or self.summary is None
            or self.artifact_markdown is None
            or any(error_fields)
        ):
            raise ValueError("invalid accounting success response")
        if self.status == "failed" and (
            self.company_id is not None
            or self.items is not None
            or self.summary is not None
            or self.artifact_markdown is not None
            or any(item is None for item in error_fields)
        ):
            raise ValueError("invalid accounting failure response")
        return self

    @model_serializer(mode="wrap")
    def omit_nulls(self, handler: object) -> dict[str, object]:
        serialized = handler(self)  # type: ignore[operator]
        return {key: value for key, value in serialized.items() if value is not None}

    @classmethod
    def from_success(
        cls,
        response: (
            TrialBalanceResponse
            | AgingResponse
            | CashbookResponse
            | UnmatchedStatementLinesResponse
            | OpenDocumentsResponse
            | JournalEntriesResponse
        ),
    ) -> AccountingToolResponse:
        return cls(**response.model_dump())

    @classmethod
    def from_error(cls, response: ErrorResponse) -> AccountingToolResponse:
        return cls(**response.model_dump())
