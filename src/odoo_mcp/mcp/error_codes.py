"""Structured public errors; raw Odoo failures never cross this boundary."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from odoo_mcp.mcp.request_ids import new_request_id


class ErrorCode(StrEnum):
    ODOO_AUTH_FAILED = "ODOO_AUTH_FAILED"
    ODOO_API_ERROR = "ODOO_API_ERROR"
    ODOO_VERSION_UNSUPPORTED = "ODOO_VERSION_UNSUPPORTED"
    ODOO_TRANSPORT_NEGOTIATION_FAILED = "ODOO_TRANSPORT_NEGOTIATION_FAILED"
    ODOO_PERMISSION_DENIED = "ODOO_PERMISSION_DENIED"
    COMPANY_NOT_FOUND = "COMPANY_NOT_FOUND"
    CAPABILITY_NOT_AVAILABLE = "CAPABILITY_NOT_AVAILABLE"
    EXECUTION_NOT_EXPLICIT = "EXECUTION_NOT_EXPLICIT"
    ODOO_STATE_CONFLICT = "ODOO_STATE_CONFLICT"
    CONCURRENT_OPERATION_IN_PROGRESS = "CONCURRENT_OPERATION_IN_PROGRESS"
    IDEMPOTENCY_KEY_PAYLOAD_MISMATCH = "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"
    JOURNAL_NOT_FOUND = "JOURNAL_NOT_FOUND"
    STATEMENT_LINE_NOT_FOUND = "STATEMENT_LINE_NOT_FOUND"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    JOURNAL_ENTRY_NOT_DRAFT = "JOURNAL_ENTRY_NOT_DRAFT"
    JOURNAL_ENTRY_UNBALANCED = "JOURNAL_ENTRY_UNBALANCED"
    PAYROLL_PERIOD_NOT_FOUND = "PAYROLL_PERIOD_NOT_FOUND"
    INSUFFICIENT_COMPARISON_DATA = "INSUFFICIENT_COMPARISON_DATA"
    PAYROLL_BATCH_NOT_FOUND = "PAYROLL_BATCH_NOT_FOUND"
    PAYSLIP_NOT_FOUND = "PAYSLIP_NOT_FOUND"
    PAYROLL_STRUCTURE_NOT_FOUND = "PAYROLL_STRUCTURE_NOT_FOUND"
    CONTRACT_DATA_MISSING = "CONTRACT_DATA_MISSING"
    EMPLOYEE_NOT_FOUND = "EMPLOYEE_NOT_FOUND"
    PAYROLL_SOURCE_INCONSISTENT = "PAYROLL_SOURCE_INCONSISTENT"
    PAYROLL_RESULT_TOO_LARGE = "PAYROLL_RESULT_TOO_LARGE"
    PAYROLL_INPUT_NOT_FOUND = "PAYROLL_INPUT_NOT_FOUND"
    PAYROLL_INPUT_TYPE_NOT_ALLOWED = "PAYROLL_INPUT_TYPE_NOT_ALLOWED"
    PAYROLL_STATE_NOT_EDITABLE = "PAYROLL_STATE_NOT_EDITABLE"
    PAYROLL_RECALCULATION_FAILED = "PAYROLL_RECALCULATION_FAILED"
    INVALID_INPUT = "INVALID_INPUT"
    MODEL_NOT_ALLOWED = "MODEL_NOT_ALLOWED"
    FIELD_DENIED = "FIELD_DENIED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "failed"
    error_code: ErrorCode
    error_message: str
    remediation_hint: str
    request_id: str


class OdooMcpError(RuntimeError):
    """Internal exception carrying only a safe public description."""

    def __init__(self, code: ErrorCode, message: str, remediation_hint: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.remediation_hint = remediation_hint

    def as_response(self, request_id: str | None = None) -> ErrorResponse:
        return ErrorResponse(
            error_code=self.code,
            error_message=self.safe_message,
            remediation_hint=self.remediation_hint,
            request_id=request_id or new_request_id(),
        )
