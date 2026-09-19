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


class AccountingToolResponse(BaseModel):
    """One compact response shape for accounting report success or failure."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "failed"]
    request_id: str
    company_id: int | None = None
    period_start: date | None = None
    period_end: date | None = None
    as_of_date: date | None = None
    items: list[TrialBalanceItem | AgingItem] | None = None
    next_cursor: str | None = None
    summary: TrialBalanceSummary | AgingSummary | None = None
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
    def from_success(cls, response: TrialBalanceResponse | AgingResponse) -> AccountingToolResponse:
        return cls(**response.model_dump())

    @classmethod
    def from_error(cls, response: ErrorResponse) -> AccountingToolResponse:
        return cls(**response.model_dump())
