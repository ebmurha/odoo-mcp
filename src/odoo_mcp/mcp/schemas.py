"""Compact public MCP response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_serializer, model_validator

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
