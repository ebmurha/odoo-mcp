"""MCP routing for controlled draft-only Payroll writes."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Lock
from typing import Annotated, Literal

from mcp.server import MCPServer
from pydantic import Field, ValidationError

from odoo_mcp.adapters.base import CapabilitySnapshot, OdooAdapter
from odoo_mcp.adapters.odoo.connections import ConnectionBinding, ConnectionResolver
from odoo_mcp.adapters.payroll import PayrollWriteRejected
from odoo_mcp.mcp.error_codes import ErrorCode, ErrorResponse, OdooMcpError
from odoo_mcp.mcp.payroll_write_schemas import (
    RecalculateDraftPayslip,
    RemoveDraftPayrollInput,
    SetDraftPayrollInput,
)
from odoo_mcp.mcp.registry import ToolDefinition, get_tool_definition
from odoo_mcp.mcp.request_ids import new_request_id
from odoo_mcp.policy.write_safety import (
    AppliedWrite,
    KnownWriteFailure,
    PreparedWrite,
    WriteCommand,
    WriteSafetyCoordinator,
    WriteSafetyResponse,
)
from odoo_mcp.storage import AuditEvent, Storage
from odoo_mcp.workflows.payroll.writes import (
    PayrollWriteOperation,
    PayrollWritePlan,
    execute_payroll_write_plan,
    payroll_write_audit_payload,
    payroll_write_request_payload,
    prepare_recalculate_draft_payslip,
    prepare_remove_draft_payroll_input,
    prepare_set_draft_payroll_input,
    validate_payroll_write_plan,
)

AdapterFactory = Callable[[object], Awaitable[OdooAdapter]]
PositiveIdentifier = Annotated[int, Field(gt=0, strict=True)]
OptionalIdentifier = Annotated[int | None, Field(gt=0, strict=True)]
Description = Annotated[str | None, Field(max_length=200)]
DecimalString = Annotated[str | None, Field(max_length=80)]
IdempotencyKey = Annotated[str | None, Field(max_length=128)]
WriteRequest = SetDraftPayrollInput | RemoveDraftPayrollInput | RecalculateDraftPayslip
PrepareOperation = Callable[[OdooAdapter, int], Awaitable[tuple[PayrollWritePlan, PreparedWrite]]]

LOGGER = logging.getLogger(__name__)

_RETAINED_ACTION_KEYS = (
    "operation",
    "payslip_id",
    "input_id",
    "before_source_state",
)
_RETAINED_EFFECT_KEYS = (
    "operation",
    "status",
    "completion",
    "payslip_id",
    "input_id",
    "result_input_id",
    "before_source_state",
    "after_source_state",
)


class _PayslipLockRegistry:
    """Non-queuing process lock for one tenant/company/payslip write scope."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._active: set[tuple[str, int, int]] = set()

    @contextmanager
    def hold(self, key: tuple[str, int, int]) -> Iterator[bool]:
        with self._guard:
            acquired = key not in self._active
            if acquired:
                self._active.add(key)
        try:
            yield acquired
        finally:
            if acquired:
                with self._guard:
                    self._active.discard(key)


_PAYSLIP_LOCKS = _PayslipLockRegistry()


async def _close_adapter(adapter: OdooAdapter) -> None:
    close = getattr(adapter, "close", None)
    if close is not None:
        result = close()
        if inspect.isawaitable(result):
            await result


def _safe_input(
    operation: PayrollWriteOperation,
    payslip_id: object,
    input_id: object,
    *,
    dry_run: bool,
    idempotency_state: str,
) -> dict[str, object]:
    return {
        "operation": operation,
        "payslip_id": payslip_id if isinstance(payslip_id, int) else None,
        "input_id": input_id if isinstance(input_id, int) else None,
        "dry_run": dry_run,
        "idempotency_state": idempotency_state,
    }


def _result_projection(
    operation: PayrollWriteOperation,
    payslip_id: object,
    input_id: object,
    response: WriteSafetyResponse,
) -> dict[str, object]:
    effects = response.material_effects
    return {
        "operation": operation,
        "status": response.status,
        "outcome": response.outcome,
        "payslip_id": payslip_id if isinstance(payslip_id, int) else None,
        "input_id": effects.get("input_id", input_id if isinstance(input_id, int) else None),
        "result_input_id": effects.get("result_input_id"),
        "before_source_state": effects.get("before_source_state"),
        "after_source_state": effects.get("after_source_state"),
        "error_code": None if response.error_code is None else response.error_code.value,
    }


def _audit_event(
    binding: ConnectionBinding,
    definition: ToolDefinition,
    company_id: int,
    operation: PayrollWriteOperation,
    payslip_id: object,
    input_id: object,
    request_id: str,
    *,
    dry_run: bool,
    final_status: str,
    response: WriteSafetyResponse,
    snapshot: CapabilitySnapshot | None = None,
) -> AuditEvent:
    idempotency_state = (
        "not_reserved"
        if dry_run or response.outcome == "not_attempted"
        else "unknown"
        if response.outcome == "unknown"
        else "succeeded"
        if response.status == "succeeded"
        else "failed"
    )
    return AuditEvent(
        request_id=request_id,
        tenant_id=binding.tenant_id,
        company_id=company_id if isinstance(company_id, int) and company_id > 0 else None,
        tool_name=definition.name,
        tool_version=definition.version,
        module="payroll",
        authenticated_subject=binding.authenticated_subject,
        mcp_client=binding.mcp_client,
        odoo_db_name=binding.connection.database,
        odoo_user=binding.connection.username,
        odoo_version=None if snapshot is None else str(snapshot.version),
        odoo_transport=None if snapshot is None else snapshot.transport,
        input_payload=_safe_input(
            operation,
            payslip_id,
            input_id,
            dry_run=dry_run,
            idempotency_state=idempotency_state,
        ),
        dry_run=dry_run,
        proposed_action=None,
        actual_result=_result_projection(operation, payslip_id, input_id, response),
        affected_odoo_records=(),
        error_code=None if response.error_code is None else response.error_code.value,
        error_message=response.error_message,
        final_status=final_status,
    )


def _failure(error: ErrorResponse, company_id: int) -> WriteSafetyResponse:
    return WriteSafetyResponse(
        status="failed",
        outcome="not_attempted",
        request_id=error.request_id,
        company_id=company_id,
        error_code=error.error_code,
        error_message=error.error_message,
        remediation_hint=error.remediation_hint,
    )


def _invalid_error(request_id: str) -> ErrorResponse:
    return ErrorResponse(
        error_code=ErrorCode.INVALID_INPUT,
        error_message="The draft Payroll write input is invalid.",
        remediation_hint="Correct the identifiers, values, or execution controls and retry.",
        request_id=request_id,
    )


def register_payroll_write_tools(
    server: MCPServer,
    resolver: ConnectionResolver,
    *,
    adapter_factory: AdapterFactory,
    storage: Storage | None,
) -> None:
    """Register the three controlled, draft-only Payroll write tools."""

    async def invalid_request(
        definition: ToolDefinition,
        operation: PayrollWriteOperation,
        company_id: int,
        payslip_id: object,
        input_id: object,
        dry_run: bool,
    ) -> WriteSafetyResponse:
        request_id = new_request_id()
        binding: ConnectionBinding | None = None
        try:
            binding = await resolver.resolve()
            if definition.required_permission not in binding.permissions:
                raise OdooMcpError(
                    ErrorCode.ODOO_AUTH_FAILED,
                    "The resolved MCP identity is not authorized for this Payroll write.",
                    "Reconnect with the Payroll draft-write permission and retry.",
                )
            if company_id not in binding.connection.allowed_company_ids:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is not authorized for this connection.",
                    "Use an authorized company ID and retry.",
                )
            error = _invalid_error(request_id)
        except OdooMcpError as exc:
            error = exc.as_response(request_id)
        except Exception:
            error = ErrorResponse(
                error_code=ErrorCode.UNKNOWN_ERROR,
                error_message="The draft Payroll write failed unexpectedly.",
                remediation_hint="Retry the request or contact the service operator.",
                request_id=request_id,
            )
        response = _failure(error, company_id)
        if binding is not None and storage is not None:
            try:
                storage.audit.append(
                    _audit_event(
                        binding,
                        definition,
                        company_id,
                        operation,
                        payslip_id,
                        input_id,
                        request_id,
                        dry_run=dry_run,
                        final_status="failed",
                        response=response,
                    )
                )
            except Exception:
                LOGGER.warning("Payroll write audit persistence failed; details were suppressed.")
        return response

    async def run_write(
        definition: ToolDefinition,
        operation: PayrollWriteOperation,
        request: WriteRequest,
        prepare_operation: PrepareOperation,
    ) -> WriteSafetyResponse:
        request_id = new_request_id()
        binding: ConnectionBinding | None = None
        snapshot: CapabilitySnapshot | None = None
        initial_adapter: OdooAdapter | None = None
        try:
            binding = await resolver.resolve()
            if definition.required_permission not in binding.permissions:
                raise OdooMcpError(
                    ErrorCode.ODOO_AUTH_FAILED,
                    "The resolved MCP identity is not authorized for this Payroll write.",
                    "Reconnect with the Payroll draft-write permission and retry.",
                )
            if request.company_id not in binding.connection.allowed_company_ids:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is not authorized for this connection.",
                    "Use an authorized company ID and retry.",
                )
            initial_adapter = await adapter_factory(binding.connection)
            companies = await initial_adapter.get_companies()
            if request.company_id not in {item.id for item in companies}:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is unavailable to the technical user.",
                    "Check company access and retry.",
                )
            snapshot = await initial_adapter.get_capabilities()
            await _close_adapter(initial_adapter)
            initial_adapter = None
            if storage is None:
                raise RuntimeError("Payroll write storage is unavailable")
        except OdooMcpError as exc:
            error = exc.as_response(request_id)
        except Exception:
            error = ErrorResponse(
                error_code=ErrorCode.UNKNOWN_ERROR,
                error_message="The draft Payroll write failed unexpectedly.",
                remediation_hint="Retry the request or contact the service operator.",
                request_id=request_id,
            )
        else:
            plan: PayrollWritePlan | None = None

            async def prepare() -> PreparedWrite:
                nonlocal plan
                adapter = await adapter_factory(binding.connection)
                try:
                    companies = await adapter.get_companies()
                    if request.company_id not in {item.id for item in companies}:
                        raise OdooMcpError(
                            ErrorCode.COMPANY_NOT_FOUND,
                            "The requested company is unavailable to the technical user.",
                            "Check company access and retry.",
                        )
                    plan, prepared = await prepare_operation(adapter, snapshot.version)
                    return prepared
                finally:
                    await _close_adapter(adapter)

            async def validate_current_state() -> None:
                if plan is None:
                    raise RuntimeError("Payroll write was not prepared")
                adapter = await adapter_factory(binding.connection)
                try:
                    companies = await adapter.get_companies()
                    if request.company_id not in {item.id for item in companies}:
                        raise OdooMcpError(
                            ErrorCode.COMPANY_NOT_FOUND,
                            "The requested company is unavailable to the technical user.",
                            "Check company access and retry.",
                        )
                    await validate_payroll_write_plan(adapter, plan)
                finally:
                    await _close_adapter(adapter)

            async def execute() -> AppliedWrite:
                if plan is None:
                    raise RuntimeError("Payroll write was not prepared")
                adapter = await adapter_factory(binding.connection)
                try:
                    companies = await adapter.get_companies()
                    if request.company_id not in {item.id for item in companies}:
                        raise OdooMcpError(
                            ErrorCode.COMPANY_NOT_FOUND,
                            "The requested company is unavailable to the technical user.",
                            "Check company access and retry.",
                        )
                    return await execute_payroll_write_plan(
                        adapter,
                        plan,
                        request_id=request_id,
                        observed_at=datetime.now(UTC),
                    )
                except PayrollWriteRejected as exc:
                    raise KnownWriteFailure(exc.error) from None
                finally:
                    await _close_adapter(adapter)

            command = WriteCommand(
                company_id=request.company_id,
                request_payload=payroll_write_request_payload(request),
                dry_run=request.dry_run,
                idempotency_key=request.idempotency_key,
                audit_payload=payroll_write_audit_payload(operation, request),
                retained_proposed_action_keys=_RETAINED_ACTION_KEYS,
                retained_material_effect_keys=_RETAINED_EFFECT_KEYS,
                compact_persistence=True,
            )
            coordinator = WriteSafetyCoordinator(storage)
            if request.dry_run:
                return await coordinator.run(
                    binding=binding,
                    definition=definition,
                    module="payroll",
                    snapshot=snapshot,
                    command=command,
                    prepare=prepare,
                    validate_current_state=validate_current_state,
                    execute=execute,
                    request_id=request_id,
                )
            lock_key = (binding.tenant_id, request.company_id, request.payslip_id)
            with _PAYSLIP_LOCKS.hold(lock_key) as acquired:
                preflight_error = (
                    None
                    if acquired
                    else OdooMcpError(
                        ErrorCode.CONCURRENT_OPERATION_IN_PROGRESS,
                        "Another Payroll write is already active for this payslip.",
                        "Wait for recovery or retry after the active operation completes.",
                    )
                )
                return await coordinator.run(
                    binding=binding,
                    definition=definition,
                    module="payroll",
                    snapshot=snapshot,
                    command=command,
                    prepare=prepare,
                    validate_current_state=validate_current_state,
                    execute=execute,
                    request_id=request_id,
                    preflight_error=preflight_error,
                )

        if initial_adapter is not None:
            try:
                await _close_adapter(initial_adapter)
            except Exception:
                LOGGER.warning("Odoo adapter cleanup failed; details were suppressed.")
                error = ErrorResponse(
                    error_code=ErrorCode.UNKNOWN_ERROR,
                    error_message="The draft Payroll write failed unexpectedly.",
                    remediation_hint="Retry the request or contact the service operator.",
                    request_id=request_id,
                )
        response = _failure(error, request.company_id)
        if binding is not None and storage is not None:
            try:
                storage.audit.append(
                    _audit_event(
                        binding,
                        definition,
                        request.company_id,
                        operation,
                        request.payslip_id,
                        getattr(request, "input_id", None),
                        request_id,
                        dry_run=request.dry_run,
                        final_status="failed",
                        response=response,
                        snapshot=snapshot,
                    )
                )
            except Exception:
                LOGGER.warning("Payroll write audit persistence failed; details were suppressed.")
        return response

    set_definition = get_tool_definition("set_draft_payroll_input")

    async def set_draft_payroll_input_tool(
        company_id: PositiveIdentifier,
        payslip_id: PositiveIdentifier,
        input_id: OptionalIdentifier = None,
        input_type_id: OptionalIdentifier = None,
        description: Description = None,
        amount: DecimalString = None,
        dry_run: bool = True,
        idempotency_key: IdempotencyKey = None,
    ) -> WriteSafetyResponse:
        operation: Literal["create", "update"] = "create" if input_id is None else "update"
        try:
            request = SetDraftPayrollInput.model_validate(
                {
                    "company_id": company_id,
                    "payslip_id": payslip_id,
                    "input_id": input_id,
                    "input_type_id": input_type_id,
                    "description": description,
                    "amount": amount,
                    "dry_run": dry_run,
                    "idempotency_key": idempotency_key,
                }
            )
        except ValidationError:
            return await invalid_request(
                set_definition,
                operation,
                company_id,
                payslip_id,
                input_id,
                dry_run,
            )

        async def prepare_operation(
            adapter: OdooAdapter, version: int
        ) -> tuple[PayrollWritePlan, PreparedWrite]:
            return await prepare_set_draft_payroll_input(adapter, request, version)

        return await run_write(set_definition, operation, request, prepare_operation)

    server.add_tool(
        set_draft_payroll_input_tool,
        name=set_definition.name,
        title=set_definition.title,
        description=set_definition.description,
        annotations=set_definition.annotations,
        meta=set_definition.protocol_meta(),
        structured_output=True,
    )

    remove_definition = get_tool_definition("remove_draft_payroll_input")

    async def remove_draft_payroll_input_tool(
        company_id: PositiveIdentifier,
        payslip_id: PositiveIdentifier,
        input_id: PositiveIdentifier,
        dry_run: bool = True,
        idempotency_key: IdempotencyKey = None,
    ) -> WriteSafetyResponse:
        try:
            request = RemoveDraftPayrollInput(
                company_id=company_id,
                payslip_id=payslip_id,
                input_id=input_id,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
            )
        except ValidationError:
            return await invalid_request(
                remove_definition,
                "remove",
                company_id,
                payslip_id,
                input_id,
                dry_run,
            )

        async def prepare_operation(
            adapter: OdooAdapter, version: int
        ) -> tuple[PayrollWritePlan, PreparedWrite]:
            return await prepare_remove_draft_payroll_input(adapter, request, version)

        return await run_write(remove_definition, "remove", request, prepare_operation)

    server.add_tool(
        remove_draft_payroll_input_tool,
        name=remove_definition.name,
        title=remove_definition.title,
        description=remove_definition.description,
        annotations=remove_definition.annotations,
        meta=remove_definition.protocol_meta(),
        structured_output=True,
    )

    recalculate_definition = get_tool_definition("recalculate_draft_payslip")

    async def recalculate_draft_payslip_tool(
        company_id: PositiveIdentifier,
        payslip_id: PositiveIdentifier,
        dry_run: bool = True,
        idempotency_key: IdempotencyKey = None,
    ) -> WriteSafetyResponse:
        try:
            request = RecalculateDraftPayslip(
                company_id=company_id,
                payslip_id=payslip_id,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
            )
        except ValidationError:
            return await invalid_request(
                recalculate_definition,
                "recalculate",
                company_id,
                payslip_id,
                None,
                dry_run,
            )

        async def prepare_operation(
            adapter: OdooAdapter, version: int
        ) -> tuple[PayrollWritePlan, PreparedWrite]:
            return await prepare_recalculate_draft_payslip(adapter, request, version)

        return await run_write(
            recalculate_definition,
            "recalculate",
            request,
            prepare_operation,
        )

    server.add_tool(
        recalculate_draft_payslip_tool,
        name=recalculate_definition.name,
        title=recalculate_definition.title,
        description=recalculate_definition.description,
        annotations=recalculate_definition.annotations,
        meta=recalculate_definition.protocol_meta(),
        structured_output=True,
    )
