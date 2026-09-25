"""Validation, authorization, routing, and metadata-only audit for Payroll reads."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime
from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field, ValidationError

from odoo_mcp.adapters.base import CapabilitySnapshot, OdooAdapter
from odoo_mcp.adapters.odoo.connections import ConnectionBinding, ConnectionResolver
from odoo_mcp.adapters.payroll import PayrollState, PayrollWorkEntryState
from odoo_mcp.mcp.error_codes import ErrorCode, ErrorResponse, OdooMcpError
from odoo_mcp.mcp.payroll_schemas import (
    DEFAULT_PAYROLL_STATES,
    DEFAULT_WORK_ENTRY_STATES,
    GetAttendanceSummaryInput,
    GetAttendanceSummaryResponse,
    GetEmployeePayrollContextInput,
    GetPayrollBatchInput,
    GetPayrollBatchResponse,
    GetPayslipInput,
    ListPayrollPeriodsInput,
    ListPayrollPeriodsResponse,
    ListPayslipsInput,
    ListPayslipsResponse,
    ListSalaryRulesInput,
    ListSalaryRulesResponse,
    PayrollCursor,
    PayrollEvidenceResponse,
    PayrollLimit,
    PayrollToolResponse,
    PositiveIdentifier,
)
from odoo_mcp.mcp.registry import ToolDefinition, get_tool_definition
from odoo_mcp.mcp.request_ids import new_request_id
from odoo_mcp.storage import AuditEvent, Storage
from odoo_mcp.workflows.payroll.evidence import (
    get_attendance_summary,
    get_employee_payroll_context,
    get_payroll_batch,
    get_payslip,
    list_payroll_periods,
    list_payslips,
    list_salary_rules,
)

AdapterFactory = Callable[[object], Awaitable[OdooAdapter]]
PayrollOperation = Callable[[OdooAdapter, str, datetime], Awaitable[PayrollEvidenceResponse]]
EmployeeIds = Annotated[tuple[int, ...], Field(max_length=100)]
RequiredEmployeeIds = Annotated[tuple[int, ...], Field(min_length=1, max_length=100)]

LOGGER = logging.getLogger(__name__)


async def _close_adapter(adapter: OdooAdapter) -> None:
    close = getattr(adapter, "close", None)
    if close is not None:
        result = close()
        if inspect.isawaitable(result):
            await result


def _safe_input(
    query_kind: str,
    *,
    states: tuple[str, ...] = (),
    employee_filter_count: int = 0,
    batch_filter_count: int = 0,
    payslip_filter_count: int = 0,
    period_filter_count: int = 0,
    continuation_requested: bool = False,
) -> dict[str, object]:
    return {
        "query_kind": query_kind,
        "normalized_states": list(states),
        "employee_filter_count": employee_filter_count,
        "batch_filter_count": batch_filter_count,
        "payslip_filter_count": payslip_filter_count,
        "period_filter_count": period_filter_count,
        "comparison_requested": False,
        "history_requested": False,
        "continuation_requested": continuation_requested,
    }


def _audit_input(definition: ToolDefinition, request: object) -> dict[str, object]:
    states = tuple(str(value) for value in getattr(request, "states", ()))
    employee_ids = tuple(getattr(request, "employee_ids", ()))
    return _safe_input(
        definition.name,
        states=states,
        employee_filter_count=(
            1 if isinstance(request, GetEmployeePayrollContextInput) else len(employee_ids)
        ),
        batch_filter_count=(
            1
            if isinstance(request, GetPayrollBatchInput)
            or getattr(request, "batch_id", None) is not None
            else 0
        ),
        payslip_filter_count=1 if isinstance(request, GetPayslipInput) else 0,
        period_filter_count=(
            1
            if isinstance(
                request,
                (
                    ListPayrollPeriodsInput,
                    GetEmployeePayrollContextInput,
                    GetAttendanceSummaryInput,
                ),
            )
            or getattr(request, "period_start", None) is not None
            else 0
        ),
        continuation_requested=getattr(request, "cursor", None) is not None,
    )


def _result_projection(
    response: PayrollEvidenceResponse | None,
    *,
    final_status: str,
    error: ErrorResponse | None = None,
) -> dict[str, object]:
    if response is None:
        item_count = 0
        has_more = False
        limitations: list[str] = []
    else:
        if isinstance(
            response,
            (
                ListPayrollPeriodsResponse,
                GetPayrollBatchResponse,
                ListPayslipsResponse,
                ListSalaryRulesResponse,
                GetAttendanceSummaryResponse,
            ),
        ):
            item_count = len(response.items)
            has_more = response.next_cursor is not None
        else:
            item_count = 1
            has_more = False
        limitations = list(response.limitations)
    return {
        "final_status": final_status,
        "item_count": item_count,
        "has_more": has_more,
        "limitation_codes": limitations,
        "error_code": None if error is None else error.error_code.value,
    }


def _audit_event(
    binding: ConnectionBinding,
    definition: ToolDefinition,
    company_id: int,
    input_payload: Mapping[str, object],
    request_id: str,
    snapshot: CapabilitySnapshot | None,
    *,
    final_status: str,
    response: PayrollEvidenceResponse | None = None,
    error: ErrorResponse | None = None,
) -> AuditEvent:
    return AuditEvent(
        request_id=request_id,
        tenant_id=binding.tenant_id,
        company_id=company_id,
        tool_name=definition.name,
        tool_version=definition.version,
        module="payroll",
        authenticated_subject=binding.authenticated_subject,
        mcp_client=binding.mcp_client,
        odoo_db_name=binding.connection.database,
        odoo_user=binding.connection.username,
        odoo_version=None if snapshot is None else str(snapshot.version),
        odoo_transport=None if snapshot is None else snapshot.transport,
        input_payload=input_payload,
        dry_run=False,
        proposed_action=None,
        actual_result=_result_projection(
            response,
            final_status=final_status,
            error=error,
        ),
        affected_odoo_records=(),
        error_code=None if error is None else error.error_code.value,
        error_message=None if error is None else error.error_message,
        final_status=final_status,
    )


def register_payroll_tools(
    server: MCPServer,
    resolver: ConnectionResolver,
    *,
    adapter_factory: AdapterFactory,
    storage: Storage | None,
) -> None:
    """Register the seven read-only Payroll evidence tools."""

    async def invalid_request(
        definition: ToolDefinition,
        company_id: int,
        audit_input: Mapping[str, object],
    ) -> PayrollToolResponse:
        request_id = new_request_id()
        binding: ConnectionBinding | None = None
        try:
            binding = await resolver.resolve()
            if definition.required_permission not in binding.permissions:
                raise OdooMcpError(
                    ErrorCode.ODOO_AUTH_FAILED,
                    "The resolved MCP identity is not authorized for Payroll evidence.",
                    "Reconnect with Payroll read permission and retry.",
                )
            if company_id not in binding.connection.allowed_company_ids:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is not authorized for this connection.",
                    "Use an authorized company ID and retry.",
                )
            error = ErrorResponse(
                error_code=ErrorCode.INVALID_INPUT,
                error_message="The Payroll evidence request is invalid.",
                remediation_hint="Correct the dates, states, identifiers, bounds, or cursor.",
                request_id=request_id,
            )
        except OdooMcpError as exc:
            error = exc.as_response(request_id)
        except Exception:
            error = ErrorResponse(
                error_code=ErrorCode.UNKNOWN_ERROR,
                error_message="The Payroll evidence request failed unexpectedly.",
                remediation_hint="Retry the request or contact the service operator.",
                request_id=request_id,
            )
        if binding is not None and storage is not None:
            try:
                storage.audit.append(
                    _audit_event(
                        binding,
                        definition,
                        company_id,
                        audit_input,
                        request_id,
                        None,
                        final_status="failed",
                        error=error,
                    )
                )
            except Exception:
                LOGGER.warning("Payroll audit persistence failed; details were suppressed.")
        return PayrollToolResponse.from_error(error)

    async def run(
        definition: ToolDefinition,
        company_id: int,
        audit_input: Mapping[str, object],
        operation: PayrollOperation,
    ) -> PayrollToolResponse:
        request_id = new_request_id()
        binding: ConnectionBinding | None = None
        adapter: OdooAdapter | None = None
        snapshot: CapabilitySnapshot | None = None
        try:
            binding = await resolver.resolve()
            if definition.required_permission not in binding.permissions:
                raise OdooMcpError(
                    ErrorCode.ODOO_AUTH_FAILED,
                    "The resolved MCP identity is not authorized for Payroll evidence.",
                    "Reconnect with Payroll read permission and retry.",
                )
            if company_id not in binding.connection.allowed_company_ids:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is not authorized for this connection.",
                    "Use an authorized company ID and retry.",
                )
            adapter = await adapter_factory(binding.connection)
            companies = await adapter.get_companies()
            company = next((value for value in companies if value.id == company_id), None)
            if company is None:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is unavailable to the technical user.",
                    "Check company access and retry.",
                )
            snapshot = await adapter.get_capabilities()
            capability = definition.required_capability
            if capability is not None and not snapshot.modules.get(capability, False):
                raise OdooMcpError(
                    ErrorCode.CAPABILITY_NOT_AVAILABLE,
                    "The Payroll application is unavailable to the technical user.",
                    "Install Payroll or grant the required least-privilege access.",
                )
            result = await operation(adapter, request_id, datetime.now(UTC))
            await _close_adapter(adapter)
            adapter = None
            if storage is None:
                raise RuntimeError("Payroll audit storage is unavailable")
            response = PayrollToolResponse.from_success(result)
            storage.audit.append(
                _audit_event(
                    binding,
                    definition,
                    company_id,
                    audit_input,
                    request_id,
                    snapshot,
                    final_status="succeeded",
                    response=result,
                )
            )
            return response
        except asyncio.CancelledError:
            error = ErrorResponse(
                error_code=ErrorCode.UNKNOWN_ERROR,
                error_message="The Payroll evidence request was cancelled.",
                remediation_hint="Retry the request against fresh Odoo Payroll state.",
                request_id=request_id,
            )
            if adapter is not None:
                try:
                    await asyncio.shield(_close_adapter(adapter))
                except Exception:
                    LOGGER.warning("Odoo adapter cleanup failed; details were suppressed.")
            if binding is not None and storage is not None:
                try:
                    storage.audit.append(
                        _audit_event(
                            binding,
                            definition,
                            company_id,
                            audit_input,
                            request_id,
                            snapshot,
                            final_status="failed",
                            error=error,
                        )
                    )
                except Exception:
                    LOGGER.warning("Payroll audit persistence failed; details were suppressed.")
            raise
        except OdooMcpError as exc:
            error = exc.as_response(request_id)
        except Exception:
            error = ErrorResponse(
                error_code=ErrorCode.UNKNOWN_ERROR,
                error_message="The Payroll evidence request failed unexpectedly.",
                remediation_hint="Retry the request or contact the service operator.",
                request_id=request_id,
            )
        if adapter is not None:
            try:
                await _close_adapter(adapter)
            except Exception:
                LOGGER.warning("Odoo adapter cleanup failed; details were suppressed.")
                error = ErrorResponse(
                    error_code=ErrorCode.UNKNOWN_ERROR,
                    error_message="The Payroll evidence request failed unexpectedly.",
                    remediation_hint="Retry the request or contact the service operator.",
                    request_id=request_id,
                )
        if binding is not None and storage is not None:
            try:
                storage.audit.append(
                    _audit_event(
                        binding,
                        definition,
                        company_id,
                        audit_input,
                        request_id,
                        snapshot,
                        final_status="failed",
                        error=error,
                    )
                )
            except Exception:
                LOGGER.warning("Payroll audit persistence failed; details were suppressed.")
        return PayrollToolResponse.from_error(error)

    periods_definition = get_tool_definition("list_payroll_periods")

    async def list_payroll_periods_tool(
        window_start: date,
        window_end: date,
        company_id: PositiveIdentifier,
        states: tuple[PayrollState, ...] = DEFAULT_PAYROLL_STATES,
        limit: PayrollLimit = 50,
        cursor: PayrollCursor = None,
    ) -> PayrollToolResponse:
        safe = _safe_input(
            periods_definition.name,
            states=tuple(states),
            period_filter_count=1,
            continuation_requested=cursor is not None,
        )
        try:
            request = ListPayrollPeriodsInput(
                company_id=company_id,
                window_start=window_start,
                window_end=window_end,
                states=states,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError:
            return await invalid_request(periods_definition, company_id, safe)

        async def operation(
            adapter: OdooAdapter, request_id: str, observed_at: datetime
        ) -> PayrollEvidenceResponse:
            return await list_payroll_periods(
                adapter,
                request,
                request_id=request_id,
                observed_at=observed_at,
            )

        return await run(
            periods_definition,
            request.company_id,
            _audit_input(periods_definition, request),
            operation,
        )

    server.add_tool(
        list_payroll_periods_tool,
        name=periods_definition.name,
        title=periods_definition.title,
        description=periods_definition.description,
        annotations=periods_definition.annotations,
        meta=periods_definition.protocol_meta(),
        structured_output=True,
    )

    batch_definition = get_tool_definition("get_payroll_batch")

    async def get_payroll_batch_tool(
        batch_id: PositiveIdentifier,
        company_id: PositiveIdentifier,
        states: tuple[PayrollState, ...] = DEFAULT_PAYROLL_STATES,
        limit: PayrollLimit = 50,
        cursor: PayrollCursor = None,
    ) -> PayrollToolResponse:
        safe = _safe_input(
            batch_definition.name,
            states=tuple(states),
            batch_filter_count=1,
            continuation_requested=cursor is not None,
        )
        try:
            request = GetPayrollBatchInput(
                company_id=company_id,
                batch_id=batch_id,
                states=states,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError:
            return await invalid_request(batch_definition, company_id, safe)

        async def operation(
            adapter: OdooAdapter, request_id: str, observed_at: datetime
        ) -> PayrollEvidenceResponse:
            return await get_payroll_batch(
                adapter,
                request,
                request_id=request_id,
                observed_at=observed_at,
            )

        return await run(
            batch_definition,
            request.company_id,
            _audit_input(batch_definition, request),
            operation,
        )

    server.add_tool(
        get_payroll_batch_tool,
        name=batch_definition.name,
        title=batch_definition.title,
        description=batch_definition.description,
        annotations=batch_definition.annotations,
        meta=batch_definition.protocol_meta(),
        structured_output=True,
    )

    payslips_definition = get_tool_definition("list_payslips")

    async def list_payslips_tool(
        company_id: PositiveIdentifier,
        batch_id: PositiveIdentifier | None = None,
        period_start: date | None = None,
        period_end: date | None = None,
        employee_ids: EmployeeIds = (),
        states: tuple[PayrollState, ...] = DEFAULT_PAYROLL_STATES,
        limit: PayrollLimit = 50,
        cursor: PayrollCursor = None,
    ) -> PayrollToolResponse:
        safe = _safe_input(
            payslips_definition.name,
            states=tuple(states),
            employee_filter_count=len(employee_ids),
            batch_filter_count=int(batch_id is not None),
            period_filter_count=int(period_start is not None or period_end is not None),
            continuation_requested=cursor is not None,
        )
        try:
            request = ListPayslipsInput(
                company_id=company_id,
                batch_id=batch_id,
                period_start=period_start,
                period_end=period_end,
                employee_ids=employee_ids,
                states=states,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError:
            return await invalid_request(payslips_definition, company_id, safe)

        async def operation(
            adapter: OdooAdapter, request_id: str, observed_at: datetime
        ) -> PayrollEvidenceResponse:
            return await list_payslips(
                adapter,
                request,
                request_id=request_id,
                observed_at=observed_at,
            )

        return await run(
            payslips_definition,
            request.company_id,
            _audit_input(payslips_definition, request),
            operation,
        )

    server.add_tool(
        list_payslips_tool,
        name=payslips_definition.name,
        title=payslips_definition.title,
        description=payslips_definition.description,
        annotations=payslips_definition.annotations,
        meta=payslips_definition.protocol_meta(),
        structured_output=True,
    )

    payslip_definition = get_tool_definition("get_payslip")

    async def get_payslip_tool(
        payslip_id: PositiveIdentifier,
        company_id: PositiveIdentifier,
    ) -> PayrollToolResponse:
        safe = _safe_input(payslip_definition.name, payslip_filter_count=1)
        try:
            request = GetPayslipInput(company_id=company_id, payslip_id=payslip_id)
        except ValidationError:
            return await invalid_request(payslip_definition, company_id, safe)

        async def operation(
            adapter: OdooAdapter, request_id: str, observed_at: datetime
        ) -> PayrollEvidenceResponse:
            return await get_payslip(
                adapter,
                request,
                request_id=request_id,
                observed_at=observed_at,
            )

        return await run(
            payslip_definition,
            request.company_id,
            _audit_input(payslip_definition, request),
            operation,
        )

    server.add_tool(
        get_payslip_tool,
        name=payslip_definition.name,
        title=payslip_definition.title,
        description=payslip_definition.description,
        annotations=payslip_definition.annotations,
        meta=payslip_definition.protocol_meta(),
        structured_output=True,
    )

    employee_definition = get_tool_definition("get_employee_payroll_context")

    async def get_employee_payroll_context_tool(
        employee_id: PositiveIdentifier,
        period_start: date,
        period_end: date,
        company_id: PositiveIdentifier,
    ) -> PayrollToolResponse:
        safe = _safe_input(
            employee_definition.name,
            employee_filter_count=1,
            period_filter_count=1,
        )
        try:
            request = GetEmployeePayrollContextInput(
                company_id=company_id,
                employee_id=employee_id,
                period_start=period_start,
                period_end=period_end,
            )
        except ValidationError:
            return await invalid_request(employee_definition, company_id, safe)

        async def operation(
            adapter: OdooAdapter, request_id: str, observed_at: datetime
        ) -> PayrollEvidenceResponse:
            return await get_employee_payroll_context(
                adapter,
                request,
                request_id=request_id,
                observed_at=observed_at,
            )

        return await run(
            employee_definition,
            request.company_id,
            _audit_input(employee_definition, request),
            operation,
        )

    server.add_tool(
        get_employee_payroll_context_tool,
        name=employee_definition.name,
        title=employee_definition.title,
        description=employee_definition.description,
        annotations=employee_definition.annotations,
        meta=employee_definition.protocol_meta(),
        structured_output=True,
    )

    rules_definition = get_tool_definition("list_salary_rules")

    async def list_salary_rules_tool(
        company_id: PositiveIdentifier,
        batch_id: PositiveIdentifier | None = None,
        period_start: date | None = None,
        period_end: date | None = None,
        employee_ids: EmployeeIds = (),
        states: tuple[PayrollState, ...] = DEFAULT_PAYROLL_STATES,
        limit: PayrollLimit = 50,
        cursor: PayrollCursor = None,
    ) -> PayrollToolResponse:
        safe = _safe_input(
            rules_definition.name,
            states=tuple(states),
            employee_filter_count=len(employee_ids),
            batch_filter_count=int(batch_id is not None),
            period_filter_count=int(period_start is not None or period_end is not None),
            continuation_requested=cursor is not None,
        )
        try:
            request = ListSalaryRulesInput(
                company_id=company_id,
                batch_id=batch_id,
                period_start=period_start,
                period_end=period_end,
                employee_ids=employee_ids,
                states=states,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError:
            return await invalid_request(rules_definition, company_id, safe)

        async def operation(
            adapter: OdooAdapter, request_id: str, observed_at: datetime
        ) -> PayrollEvidenceResponse:
            return await list_salary_rules(
                adapter,
                request,
                request_id=request_id,
                observed_at=observed_at,
            )

        return await run(
            rules_definition,
            request.company_id,
            _audit_input(rules_definition, request),
            operation,
        )

    server.add_tool(
        list_salary_rules_tool,
        name=rules_definition.name,
        title=rules_definition.title,
        description=rules_definition.description,
        annotations=rules_definition.annotations,
        meta=rules_definition.protocol_meta(),
        structured_output=True,
    )

    attendance_definition = get_tool_definition("get_attendance_summary")

    async def get_attendance_summary_tool(
        employee_ids: RequiredEmployeeIds,
        period_start: date,
        period_end: date,
        company_id: PositiveIdentifier,
        states: tuple[PayrollWorkEntryState, ...] = DEFAULT_WORK_ENTRY_STATES,
        limit: PayrollLimit = 50,
        cursor: PayrollCursor = None,
    ) -> PayrollToolResponse:
        safe = _safe_input(
            attendance_definition.name,
            states=tuple(states),
            employee_filter_count=len(employee_ids),
            period_filter_count=1,
            continuation_requested=cursor is not None,
        )
        try:
            request = GetAttendanceSummaryInput(
                company_id=company_id,
                employee_ids=employee_ids,
                period_start=period_start,
                period_end=period_end,
                states=states,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError:
            return await invalid_request(attendance_definition, company_id, safe)

        async def operation(
            adapter: OdooAdapter, request_id: str, observed_at: datetime
        ) -> PayrollEvidenceResponse:
            return await get_attendance_summary(
                adapter,
                request,
                request_id=request_id,
                observed_at=observed_at,
            )

        return await run(
            attendance_definition,
            request.company_id,
            _audit_input(attendance_definition, request),
            operation,
        )

    server.add_tool(
        get_attendance_summary_tool,
        name=attendance_definition.name,
        title=attendance_definition.title,
        description=attendance_definition.description,
        annotations=attendance_definition.annotations,
        meta=attendance_definition.protocol_meta(),
        structured_output=True,
    )
