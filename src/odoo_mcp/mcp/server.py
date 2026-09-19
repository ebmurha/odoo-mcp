"""MCP validation and routing over the shared deterministic registry."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Annotated, TypeAlias

from mcp.server import MCPServer
from pydantic import Field, ValidationError

from odoo_mcp.adapters.base import CapabilitySnapshot, OdooAdapter
from odoo_mcp.adapters.odoo.client import OdooClient
from odoo_mcp.adapters.odoo.connections import ConnectionBinding, ConnectionResolver
from odoo_mcp.mcp.error_codes import ErrorCode, ErrorResponse, OdooMcpError
from odoo_mcp.mcp.registry import TOOL_REGISTRY, ToolDefinition, get_tool_definition
from odoo_mcp.mcp.request_ids import new_request_id
from odoo_mcp.mcp.schemas import (
    AccountingReadInput,
    AccountingToolResponse,
    AgingInput,
    AgingResponse,
    CapabilitiesToolResponse,
    TrialBalanceInput,
    TrialBalanceResponse,
)
from odoo_mcp.storage import AuditEvent, Storage
from odoo_mcp.workflows.accounting.reports import get_aged_balance, get_trial_balance
from odoo_mcp.workflows.core.capabilities import get_erp_capabilities

AdapterFactory = Callable[[object], Awaitable[OdooAdapter]]
ReportResponse: TypeAlias = TrialBalanceResponse | AgingResponse
ReportOperation = Callable[[OdooAdapter, str, str], Awaitable[ReportResponse]]
LOGGER = logging.getLogger(__name__)
PositiveCompanyId: TypeAlias = Annotated[int, Field(gt=0)]
ReportLimit: TypeAlias = Annotated[int, Field(ge=1, le=500)]
ReportCursor: TypeAlias = Annotated[str | None, Field(max_length=128)]
IdempotencyKey: TypeAlias = Annotated[str | None, Field(min_length=1, max_length=128)]


async def _default_adapter_factory(connection: object) -> OdooAdapter:
    from odoo_mcp.app.settings import OdooConnectionSettings

    if not isinstance(connection, OdooConnectionSettings):
        raise TypeError("Expected normalized Odoo connection settings")
    return await OdooClient.connect(connection)


async def _close_adapter(adapter: OdooAdapter) -> None:
    close = getattr(adapter, "close", None)
    if close is not None:
        result = close()
        if inspect.isawaitable(result):
            await result


def create_mcp_server(
    resolver: ConnectionResolver,
    *,
    adapter_factory: AdapterFactory = _default_adapter_factory,
    storage: Storage | None = None,
) -> MCPServer:
    """Build the one server used by every deployment profile."""

    server = MCPServer("odoo-mcp")
    definition = get_tool_definition("get_erp_capabilities")

    async def capabilities_tool() -> CapabilitiesToolResponse:
        request_id = new_request_id()
        adapter: OdooAdapter | None = None
        try:
            binding = await resolver.resolve()
            if definition.required_permission not in binding.permissions:
                raise OdooMcpError(
                    ErrorCode.ODOO_AUTH_FAILED,
                    "The resolved MCP identity is not authorized for ERP discovery.",
                    "Reconnect with core discovery permission and retry.",
                )
            adapter = await adapter_factory(binding.connection)
            response = CapabilitiesToolResponse.from_success(
                await get_erp_capabilities(
                    adapter,
                    permissions=binding.permissions,
                    default_company_id=binding.connection.default_company_id,
                    tools=tuple(tool.availability() for tool in TOOL_REGISTRY),
                    request_id=request_id,
                )
            )
        except OdooMcpError as exc:
            response = CapabilitiesToolResponse.from_error(exc.as_response(request_id))
        except Exception:
            response = CapabilitiesToolResponse.from_error(
                ErrorResponse(
                    error_code=ErrorCode.UNKNOWN_ERROR,
                    error_message="The capability request failed unexpectedly.",
                    remediation_hint="Retry the request or contact the service operator.",
                    request_id=request_id,
                )
            )
        if adapter is not None:
            try:
                await _close_adapter(adapter)
            except Exception:
                LOGGER.warning("Odoo adapter cleanup failed; details were suppressed.")
                if response.status == "ok":
                    response = CapabilitiesToolResponse.from_error(
                        ErrorResponse(
                            error_code=ErrorCode.UNKNOWN_ERROR,
                            error_message="The capability request failed unexpectedly.",
                            remediation_hint=("Retry the request or contact the service operator."),
                            request_id=request_id,
                        )
                    )
        return response

    server.add_tool(
        capabilities_tool,
        name=definition.name,
        title=definition.title,
        description=definition.description,
        annotations=definition.annotations,
        meta=definition.protocol_meta(),
        structured_output=True,
    )

    async def accounting_report(
        report_definition: ToolDefinition,
        request: AccountingReadInput,
        operation: ReportOperation,
    ) -> AccountingToolResponse:
        request_id = new_request_id()
        adapter: OdooAdapter | None = None
        binding: ConnectionBinding | None = None
        snapshot: CapabilitySnapshot | None = None
        try:
            binding = await resolver.resolve()
            if report_definition.required_permission not in binding.permissions:
                raise OdooMcpError(
                    ErrorCode.ODOO_AUTH_FAILED,
                    "The resolved MCP identity is not authorized for this accounting report.",
                    "Reconnect with accounting read permission and retry.",
                )
            if request.company_id not in binding.connection.allowed_company_ids:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is not authorized for this connection.",
                    "Use an authorized company ID and retry.",
                )
            adapter = await adapter_factory(binding.connection)
            companies = await adapter.get_companies()
            company = next(
                (item for item in companies if item.id == request.company_id),
                None,
            )
            if company is None:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is unavailable to the technical user.",
                    "Check company access and retry.",
                )
            snapshot = await adapter.get_capabilities()
            capability = report_definition.required_capability
            if capability is not None and not snapshot.modules.get(capability, False):
                raise OdooMcpError(
                    ErrorCode.CAPABILITY_NOT_AVAILABLE,
                    "The Accounting application is unavailable to the technical user.",
                    "Install Accounting or grant the required least-privilege access.",
                )
            result = await operation(adapter, company.name, request_id)
            await _close_adapter(adapter)
            adapter = None
            if storage is None:
                raise RuntimeError("Accounting report storage is unavailable")
            response = AccountingToolResponse.from_success(result)
            storage.reports.record_success(
                _audit_event(
                    binding,
                    report_definition,
                    request,
                    request_id,
                    snapshot,
                    final_status="succeeded",
                    actual_result={
                        "status": "ok",
                        "item_count": len(result.items),
                        "has_more": result.next_cursor is not None,
                        "summary": result.summary.model_dump(mode="json"),
                    },
                ),
                artifact_type=report_definition.name,
                content=result.artifact_markdown,
            )
            return response
        except OdooMcpError as exc:
            error = exc.as_response(request_id)
        except Exception:
            error = ErrorResponse(
                error_code=ErrorCode.UNKNOWN_ERROR,
                error_message="The accounting report failed unexpectedly.",
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
                    error_message="The accounting report failed unexpectedly.",
                    remediation_hint="Retry the request or contact the service operator.",
                    request_id=request_id,
                )
        if binding is not None and storage is not None:
            try:
                storage.audit.append(
                    _audit_event(
                        binding,
                        report_definition,
                        request,
                        request_id,
                        snapshot,
                        final_status="failed",
                        error=error,
                    )
                )
            except Exception:
                LOGGER.warning("Accounting report audit persistence failed; details suppressed.")
        return AccountingToolResponse.from_error(error)

    trial_definition = get_tool_definition("get_trial_balance")

    async def trial_balance_tool(
        period_start: date,
        period_end: date,
        company_id: PositiveCompanyId,
        account_ids: tuple[int, ...] = (),
        idempotency_key: IdempotencyKey = None,
        limit: ReportLimit = 100,
        cursor: ReportCursor = None,
    ) -> AccountingToolResponse:
        try:
            request = TrialBalanceInput(
                period_start=period_start,
                period_end=period_end,
                company_id=company_id,
                account_ids=account_ids,
                idempotency_key=idempotency_key,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError:
            return _invalid_input_response()

        async def run(adapter: OdooAdapter, company_name: str, request_id: str) -> ReportResponse:
            return await get_trial_balance(
                adapter,
                request,
                company_name=company_name,
                request_id=request_id,
            )

        return await accounting_report(trial_definition, request, run)

    server.add_tool(
        trial_balance_tool,
        name=trial_definition.name,
        title=trial_definition.title,
        description=trial_definition.description,
        annotations=trial_definition.annotations,
        meta=trial_definition.protocol_meta(),
        structured_output=True,
    )

    def add_aging_tool(name: str, *, payable: bool) -> None:
        aging_definition = get_tool_definition(name)

        async def aging_tool(
            as_of_date: date,
            company_id: PositiveCompanyId,
            partner_ids: tuple[int, ...] = (),
            idempotency_key: IdempotencyKey = None,
            limit: ReportLimit = 100,
            cursor: ReportCursor = None,
        ) -> AccountingToolResponse:
            try:
                request = AgingInput(
                    as_of_date=as_of_date,
                    company_id=company_id,
                    partner_ids=partner_ids,
                    idempotency_key=idempotency_key,
                    limit=limit,
                    cursor=cursor,
                )
            except ValidationError:
                return _invalid_input_response()

            async def run(
                adapter: OdooAdapter,
                company_name: str,
                request_id: str,
            ) -> ReportResponse:
                return await get_aged_balance(
                    adapter,
                    request,
                    company_name=company_name,
                    request_id=request_id,
                    payable=payable,
                )

            return await accounting_report(aging_definition, request, run)

        server.add_tool(
            aging_tool,
            name=aging_definition.name,
            title=aging_definition.title,
            description=aging_definition.description,
            annotations=aging_definition.annotations,
            meta=aging_definition.protocol_meta(),
            structured_output=True,
        )

    add_aging_tool("get_aged_receivables", payable=False)
    add_aging_tool("get_aged_payables", payable=True)
    return server


def _audit_event(
    binding: ConnectionBinding,
    definition: ToolDefinition,
    request: AccountingReadInput,
    request_id: str,
    snapshot: CapabilitySnapshot | None,
    *,
    final_status: str,
    actual_result: dict[str, object] | None = None,
    error: ErrorResponse | None = None,
) -> AuditEvent:
    return AuditEvent(
        request_id=request_id,
        tenant_id=binding.tenant_id,
        company_id=request.company_id,
        tool_name=definition.name,
        tool_version=definition.version,
        module="accounting",
        authenticated_subject=binding.authenticated_subject,
        mcp_client=binding.mcp_client,
        odoo_db_name=binding.connection.database,
        odoo_user=binding.connection.username,
        odoo_version=None if snapshot is None else str(snapshot.version),
        odoo_transport=None if snapshot is None else snapshot.transport,
        input_payload=request.model_dump(mode="json"),
        dry_run=False,
        proposed_action=None,
        actual_result=actual_result,
        affected_odoo_records=(),
        error_code=None if error is None else error.error_code.value,
        error_message=None if error is None else error.error_message,
        final_status=final_status,
    )


def _invalid_input_response() -> AccountingToolResponse:
    return AccountingToolResponse.from_error(
        ErrorResponse(
            error_code=ErrorCode.INVALID_INPUT,
            error_message="The accounting report input is invalid.",
            remediation_hint="Correct the dates, identifiers, bounds, or cursor and retry.",
            request_id=new_request_id(),
        )
    )
