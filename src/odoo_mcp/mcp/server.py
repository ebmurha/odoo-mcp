"""MCP validation and routing over the shared deterministic registry."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, TypeAlias

from mcp import types
from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from odoo_mcp.adapters.accounting import (
    InvoiceDraft,
    InvoiceEffect,
    JournalEntry,
    JournalEntryDraft,
    PaymentRegistration,
    RelatedRecord,
)
from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.client import OdooClient
from odoo_mcp.adapters.odoo.connections import ConnectionBinding, ConnectionResolver
from odoo_mcp.mcp.error_codes import ErrorCode, ErrorResponse, OdooMcpError
from odoo_mcp.mcp.payroll import register_payroll_tools
from odoo_mcp.mcp.registry import TOOL_REGISTRY, ToolDefinition, get_tool_definition
from odoo_mcp.mcp.request_ids import new_request_id
from odoo_mcp.mcp.schemas import (
    AccountingReadInput,
    AccountingToolResponse,
    AgingInput,
    AgingResponse,
    BalanceSheetInput,
    BalanceSheetResponse,
    CapabilitiesToolResponse,
    CashbookInput,
    CashbookResponse,
    CreateInvoiceInput,
    CreateJournalEntryInput,
    CreditNoteInput,
    CurrencyRateHistoryInput,
    CurrencyRateHistoryResponse,
    InvoiceLineInput,
    JournalEntriesInput,
    JournalEntriesResponse,
    JournalEntryLineInput,
    OpenDocumentsInput,
    OpenDocumentsResponse,
    PostJournalEntryInput,
    ProfitAndLossInput,
    ProfitAndLossResponse,
    ReconciliationInput,
    ReconciliationProposal,
    RegisterPaymentInput,
    TrialBalanceInput,
    TrialBalanceResponse,
    UnmatchedStatementLinesInput,
    UnmatchedStatementLinesResponse,
    ValidateInvoiceInput,
)
from odoo_mcp.policy.write_safety import (
    AppliedWrite,
    KnownWriteFailure,
    PreparedWrite,
    WriteCommand,
    WriteSafetyCoordinator,
    WriteSafetyResponse,
)
from odoo_mcp.storage import AuditEvent, Storage
from odoo_mcp.workflows.accounting.cashbook import get_cashbook
from odoo_mcp.workflows.accounting.currency_rates import get_currency_rate_history
from odoo_mcp.workflows.accounting.invoicing import (
    execute_invoice_draft,
    list_open_documents,
    prepare_credit_note,
    prepare_invoice_draft,
    prepare_payment,
    prepare_validation,
)
from odoo_mcp.workflows.accounting.journals import (
    execute_journal_entry_draft,
    execute_journal_entry_post,
    list_journal_entries,
    prepare_journal_entry_draft,
    prepare_journal_entry_post,
)
from odoo_mcp.workflows.accounting.reconcile_bank import (
    build_reconciliation_proposal,
    flag_unmatched_statement_lines,
)
from odoo_mcp.workflows.accounting.reports import (
    get_aged_balance,
    get_balance_sheet,
    get_profit_and_loss,
    get_trial_balance,
)
from odoo_mcp.workflows.core.capabilities import get_erp_capabilities

SERVER_ICON_URL = "https://raw.githubusercontent.com/ebmurha/odoo-mcp/main/assets/odoo-mcp-logo.png"
AdapterFactory = Callable[[object], Awaitable[OdooAdapter]]
ReportResponse: TypeAlias = (
    TrialBalanceResponse
    | ProfitAndLossResponse
    | BalanceSheetResponse
    | AgingResponse
    | CashbookResponse
    | UnmatchedStatementLinesResponse
    | OpenDocumentsResponse
    | JournalEntriesResponse
    | CurrencyRateHistoryResponse
)
ReportOperation = Callable[[OdooAdapter, Company, str], Awaitable[ReportResponse]]
LOGGER = logging.getLogger(__name__)
PositiveCompanyId: TypeAlias = Annotated[int, Field(gt=0)]
ReportLimit: TypeAlias = Annotated[int, Field(ge=1, le=500)]
ReportCursor: TypeAlias = Annotated[str | None, Field(max_length=128)]
IdempotencyKey: TypeAlias = Annotated[str | None, Field(min_length=1, max_length=128)]
PositiveIdentifier: TypeAlias = Annotated[int, Field(gt=0)]
ConfidenceThreshold: TypeAlias = Annotated[Decimal, Field(ge=0, le=1)]
StatementLineIds: TypeAlias = Annotated[tuple[int, ...], Field(min_length=1, max_length=500)]
PeriodMonth: TypeAlias = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


async def _default_adapter_factory(connection: object) -> OdooAdapter:
    from odoo_mcp.app.settings import OdooConnectionSettings

    if not isinstance(connection, OdooConnectionSettings):
        raise TypeError("Expected normalized Odoo connection settings")
    return await OdooClient.connect(connection)


def _require_company_currency(company: Company) -> RelatedRecord:
    if company.currency is None:
        raise OdooMcpError(
            ErrorCode.ODOO_API_ERROR,
            "Odoo did not return the authorized company's currency.",
            "Check Odoo compatibility and company access, then retry.",
        )
    return company.currency


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
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer:
    """Build the one server used by every deployment profile."""

    server = MCPServer(
        "odoo-mcp",
        title="Odoo MCP",
        icons=[
            types.Icon(
                src=SERVER_ICON_URL,
                mime_type="image/png",
                sizes=["256x256"],
            )
        ],
        auth=auth,
        token_verifier=token_verifier,
    )

    @server.custom_route(  # type: ignore[untyped-decorator]
        "/healthz", methods=["GET"], include_in_schema=False
    )
    async def health_check(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

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

    async def invalid_accounting_report(
        report_definition: ToolDefinition,
        company_id: int,
        input_payload: Mapping[str, object],
    ) -> AccountingToolResponse:
        request_id = new_request_id()
        binding: ConnectionBinding | None = None
        try:
            binding = await resolver.resolve()
            if report_definition.required_permission not in binding.permissions:
                raise OdooMcpError(
                    ErrorCode.ODOO_AUTH_FAILED,
                    "The resolved MCP identity is not authorized for this accounting report.",
                    "Reconnect with accounting read permission and retry.",
                )
            if company_id not in binding.connection.allowed_company_ids:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is not authorized for this connection.",
                    "Use an authorized company ID and retry.",
                )
            error = ErrorResponse(
                error_code=ErrorCode.INVALID_INPUT,
                error_message="The accounting report input is invalid.",
                remediation_hint="Correct the dates, identifiers, bounds, or cursor and retry.",
                request_id=request_id,
            )
        except OdooMcpError as exc:
            error = exc.as_response(request_id)
        except Exception:
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
                        company_id,
                        input_payload,
                        request_id,
                        None,
                        final_status="failed",
                        error=error,
                    )
                )
            except Exception:
                LOGGER.warning("Accounting report audit persistence failed; details suppressed.")
        return AccountingToolResponse.from_error(error)

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
            result = await operation(adapter, company, request_id)
            await _close_adapter(adapter)
            adapter = None
            if storage is None:
                raise RuntimeError("Accounting report storage is unavailable")
            response = AccountingToolResponse.from_success(result)
            storage.reports.record_success(
                _audit_event(
                    binding,
                    report_definition,
                    request.company_id,
                    request.model_dump(mode="json"),
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
                        request.company_id,
                        request.model_dump(mode="json"),
                        request_id,
                        snapshot,
                        final_status="failed",
                        error=error,
                    )
                )
            except Exception:
                LOGGER.warning("Accounting report audit persistence failed; details suppressed.")
        return AccountingToolResponse.from_error(error)

    async def invalid_accounting_write(
        definition: ToolDefinition,
        company_id: int,
        dry_run: bool,
        input_payload: Mapping[str, object],
    ) -> WriteSafetyResponse:
        request_id = new_request_id()
        binding: ConnectionBinding | None = None
        try:
            binding = await resolver.resolve()
            if definition.required_permission not in binding.permissions:
                raise OdooMcpError(
                    ErrorCode.ODOO_AUTH_FAILED,
                    "The resolved MCP identity is not authorized for this accounting write.",
                    "Reconnect with accounting proposal permission and retry.",
                )
            if company_id not in binding.connection.allowed_company_ids:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is not authorized for this connection.",
                    "Use an authorized company ID and retry.",
                )
            error = ErrorResponse(
                error_code=ErrorCode.INVALID_INPUT,
                error_message="The accounting write input is invalid.",
                remediation_hint="Correct the identifiers, amounts, dates, or controls and retry.",
                request_id=request_id,
            )
        except OdooMcpError as exc:
            error = exc.as_response(request_id)
        except Exception:
            error = ErrorResponse(
                error_code=ErrorCode.UNKNOWN_ERROR,
                error_message="The accounting write failed unexpectedly.",
                remediation_hint="Retry the request or contact the service operator.",
                request_id=request_id,
            )
        response = WriteSafetyResponse(
            status="failed",
            outcome="not_attempted",
            request_id=request_id,
            company_id=company_id,
            error_code=error.error_code,
            error_message=error.error_message,
            remediation_hint=error.remediation_hint,
        )
        if binding is not None and storage is not None:
            try:
                storage.audit.append(
                    _audit_event(
                        binding,
                        definition,
                        company_id,
                        input_payload,
                        request_id,
                        None,
                        final_status="failed",
                        actual_result=response.model_dump(mode="json"),
                        error=error,
                        dry_run=dry_run,
                    )
                )
            except Exception:
                LOGGER.warning("Accounting write audit persistence failed; details suppressed.")
        return response

    async def accounting_write(
        definition: ToolDefinition,
        command: WriteCommand,
        prepare_operation: Callable[[OdooAdapter], Awaitable[tuple[object, PreparedWrite]]],
        validate_operation: Callable[[OdooAdapter, object], Awaitable[None]],
        execute_operation: Callable[[OdooAdapter, object], Awaitable[AppliedWrite]],
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
                    "The resolved MCP identity is not authorized for this accounting write.",
                    "Reconnect with accounting proposal permission and retry.",
                )
            if command.company_id not in binding.connection.allowed_company_ids:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is not authorized for this connection.",
                    "Use an authorized company ID and retry.",
                )
            initial_adapter = await adapter_factory(binding.connection)
            companies = await initial_adapter.get_companies()
            if command.company_id not in {item.id for item in companies}:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is unavailable to the technical user.",
                    "Check company access and retry.",
                )
            snapshot = await initial_adapter.get_capabilities()
            await _close_adapter(initial_adapter)
            initial_adapter = None
            if storage is None:
                raise RuntimeError("Accounting write storage is unavailable")
        except OdooMcpError as exc:
            error = exc.as_response(request_id)
        except Exception:
            error = ErrorResponse(
                error_code=ErrorCode.UNKNOWN_ERROR,
                error_message="The accounting write failed unexpectedly.",
                remediation_hint="Retry the request or contact the service operator.",
                request_id=request_id,
            )
        else:
            state: object | None = None

            async def prepare() -> PreparedWrite:
                nonlocal state
                adapter = await adapter_factory(binding.connection)
                try:
                    companies = await adapter.get_companies()
                    if command.company_id not in {item.id for item in companies}:
                        raise OdooMcpError(
                            ErrorCode.COMPANY_NOT_FOUND,
                            "The requested company is unavailable to the technical user.",
                            "Check company access and retry.",
                        )
                    state, prepared = await prepare_operation(adapter)
                    return prepared
                finally:
                    await _close_adapter(adapter)

            async def validate_current_state() -> None:
                if state is None:
                    raise RuntimeError("Accounting write was not prepared")
                adapter = await adapter_factory(binding.connection)
                try:
                    companies = await adapter.get_companies()
                    if command.company_id not in {item.id for item in companies}:
                        raise OdooMcpError(
                            ErrorCode.COMPANY_NOT_FOUND,
                            "The requested company is unavailable to the technical user.",
                            "Check company access and retry.",
                        )
                    await validate_operation(adapter, state)
                finally:
                    await _close_adapter(adapter)

            async def execute() -> AppliedWrite:
                if state is None:
                    raise RuntimeError("Accounting write was not prepared")
                adapter = await adapter_factory(binding.connection)
                try:
                    companies = await adapter.get_companies()
                    if command.company_id not in {item.id for item in companies}:
                        raise OdooMcpError(
                            ErrorCode.COMPANY_NOT_FOUND,
                            "The requested company is unavailable to the technical user.",
                            "Check company access and retry.",
                        )
                    return await execute_operation(adapter, state)
                except OdooMcpError as exc:
                    if exc.code is ErrorCode.ODOO_PERMISSION_DENIED:
                        raise KnownWriteFailure(exc) from None
                    raise
                finally:
                    await _close_adapter(adapter)

            return await WriteSafetyCoordinator(storage).run(
                binding=binding,
                definition=definition,
                module="accounting",
                snapshot=snapshot,
                command=command,
                prepare=prepare,
                validate_current_state=validate_current_state,
                execute=execute,
                request_id=request_id,
            )

        if initial_adapter is not None:
            try:
                await _close_adapter(initial_adapter)
            except Exception:
                LOGGER.warning("Odoo adapter cleanup failed; details were suppressed.")
        response = WriteSafetyResponse(
            status="failed",
            outcome="not_attempted",
            request_id=request_id,
            company_id=command.company_id,
            error_code=error.error_code,
            error_message=error.error_message,
            remediation_hint=error.remediation_hint,
        )
        if binding is not None and storage is not None:
            try:
                storage.audit.append(
                    _audit_event(
                        binding,
                        definition,
                        command.company_id,
                        command.request_payload,
                        request_id,
                        snapshot,
                        final_status="failed",
                        actual_result=response.model_dump(mode="json"),
                        error=error,
                        dry_run=command.dry_run,
                    )
                )
            except Exception:
                LOGGER.warning("Accounting write audit persistence failed; details suppressed.")
        return response

    currency_rate_definition = get_tool_definition("get_currency_rate_history")

    async def currency_rate_history_tool(
        company_id: PositiveCompanyId,
        currency_id: PositiveIdentifier,
        period_start: date,
        period_end: date,
        limit: ReportLimit = 100,
        cursor: ReportCursor = None,
    ) -> AccountingToolResponse:
        try:
            request = CurrencyRateHistoryInput(
                company_id=company_id,
                currency_id=currency_id,
                period_start=period_start,
                period_end=period_end,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError:
            return await invalid_accounting_report(
                currency_rate_definition,
                company_id,
                {
                    "company_id": company_id,
                    "currency_id": currency_id,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "limit": limit,
                    "cursor": cursor,
                },
            )

        async def run(adapter: OdooAdapter, company: Company, request_id: str) -> ReportResponse:
            return await get_currency_rate_history(
                adapter, request, company=company, request_id=request_id
            )

        return await accounting_report(currency_rate_definition, request, run)

    server.add_tool(
        currency_rate_history_tool,
        name=currency_rate_definition.name,
        title=currency_rate_definition.title,
        description=currency_rate_definition.description,
        annotations=currency_rate_definition.annotations,
        meta=currency_rate_definition.protocol_meta(),
        structured_output=True,
    )

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
            return await invalid_accounting_report(
                trial_definition,
                company_id,
                {
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "company_id": company_id,
                    "account_ids": list(account_ids),
                    "idempotency_key": idempotency_key,
                    "limit": limit,
                    "cursor": cursor,
                },
            )

        async def run(adapter: OdooAdapter, company: Company, request_id: str) -> ReportResponse:
            return await get_trial_balance(
                adapter,
                request,
                company_name=company.name,
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

    profit_and_loss_definition = get_tool_definition("get_profit_and_loss")

    async def profit_and_loss_tool(
        period_start: date,
        period_end: date,
        company_id: PositiveCompanyId,
        analytic_account_ids: tuple[int, ...] = (),
        idempotency_key: IdempotencyKey = None,
        limit: ReportLimit = 100,
        cursor: ReportCursor = None,
    ) -> AccountingToolResponse:
        try:
            request = ProfitAndLossInput(
                period_start=period_start,
                period_end=period_end,
                company_id=company_id,
                analytic_account_ids=analytic_account_ids,
                idempotency_key=idempotency_key,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError:
            return await invalid_accounting_report(
                profit_and_loss_definition,
                company_id,
                {
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "company_id": company_id,
                    "analytic_account_ids": list(analytic_account_ids),
                    "idempotency_key": idempotency_key,
                    "limit": limit,
                    "cursor": cursor,
                },
            )

        async def run(adapter: OdooAdapter, company: Company, request_id: str) -> ReportResponse:
            return await get_profit_and_loss(
                adapter,
                request,
                company_currency_id=_require_company_currency(company).id,
                company_name=company.name,
                request_id=request_id,
            )

        return await accounting_report(profit_and_loss_definition, request, run)

    server.add_tool(
        profit_and_loss_tool,
        name=profit_and_loss_definition.name,
        title=profit_and_loss_definition.title,
        description=profit_and_loss_definition.description,
        annotations=profit_and_loss_definition.annotations,
        meta=profit_and_loss_definition.protocol_meta(),
        structured_output=True,
    )

    balance_sheet_definition = get_tool_definition("get_balance_sheet")

    async def balance_sheet_tool(
        as_of_date: date,
        company_id: PositiveCompanyId,
        analytic_account_ids: tuple[int, ...] = (),
        idempotency_key: IdempotencyKey = None,
        limit: ReportLimit = 100,
        cursor: ReportCursor = None,
    ) -> AccountingToolResponse:
        try:
            request = BalanceSheetInput(
                as_of_date=as_of_date,
                company_id=company_id,
                analytic_account_ids=analytic_account_ids,
                idempotency_key=idempotency_key,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError:
            return await invalid_accounting_report(
                balance_sheet_definition,
                company_id,
                {
                    "as_of_date": as_of_date.isoformat(),
                    "company_id": company_id,
                    "analytic_account_ids": list(analytic_account_ids),
                    "idempotency_key": idempotency_key,
                    "limit": limit,
                    "cursor": cursor,
                },
            )

        async def run(adapter: OdooAdapter, company: Company, request_id: str) -> ReportResponse:
            return await get_balance_sheet(
                adapter,
                request,
                company_currency_id=_require_company_currency(company).id,
                company_name=company.name,
                request_id=request_id,
            )

        return await accounting_report(balance_sheet_definition, request, run)

    server.add_tool(
        balance_sheet_tool,
        name=balance_sheet_definition.name,
        title=balance_sheet_definition.title,
        description=balance_sheet_definition.description,
        annotations=balance_sheet_definition.annotations,
        meta=balance_sheet_definition.protocol_meta(),
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
                return await invalid_accounting_report(
                    aging_definition,
                    company_id,
                    {
                        "as_of_date": as_of_date.isoformat(),
                        "company_id": company_id,
                        "partner_ids": list(partner_ids),
                        "idempotency_key": idempotency_key,
                        "limit": limit,
                        "cursor": cursor,
                    },
                )

            async def run(
                adapter: OdooAdapter,
                company: Company,
                request_id: str,
            ) -> ReportResponse:
                return await get_aged_balance(
                    adapter,
                    request,
                    company_name=company.name,
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

    cashbook_definition = get_tool_definition("get_cashbook")

    async def cashbook_tool(
        period_start: date,
        period_end: date,
        company_id: PositiveCompanyId,
        journal_ids: tuple[int, ...] = (),
        partner_ids: tuple[int, ...] = (),
        limit: ReportLimit = 100,
        cursor: ReportCursor = None,
    ) -> AccountingToolResponse:
        try:
            request = CashbookInput(
                period_start=period_start,
                period_end=period_end,
                company_id=company_id,
                journal_ids=journal_ids,
                partner_ids=partner_ids,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError:
            return await invalid_accounting_report(
                cashbook_definition,
                company_id,
                {
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "company_id": company_id,
                    "journal_ids": list(journal_ids),
                    "partner_ids": list(partner_ids),
                    "limit": limit,
                    "cursor": cursor,
                },
            )

        async def run(
            adapter: OdooAdapter,
            company: Company,
            request_id: str,
        ) -> ReportResponse:
            return await get_cashbook(
                adapter,
                request,
                company_name=company.name,
                request_id=request_id,
            )

        return await accounting_report(cashbook_definition, request, run)

    server.add_tool(
        cashbook_tool,
        name=cashbook_definition.name,
        title=cashbook_definition.title,
        description=cashbook_definition.description,
        annotations=cashbook_definition.annotations,
        meta=cashbook_definition.protocol_meta(),
        structured_output=True,
    )

    unmatched_definition = get_tool_definition("flag_unmatched_statement_lines")

    async def unmatched_statement_lines_tool(
        period_start: date,
        period_end: date,
        company_id: PositiveCompanyId,
        journal_id: PositiveIdentifier | None = None,
        match_confidence_threshold: ConfidenceThreshold = Decimal("0.85"),
        limit: ReportLimit = 100,
        cursor: ReportCursor = None,
    ) -> AccountingToolResponse:
        try:
            request = UnmatchedStatementLinesInput(
                period_start=period_start,
                period_end=period_end,
                company_id=company_id,
                journal_id=journal_id,
                match_confidence_threshold=match_confidence_threshold,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError:
            return await invalid_accounting_report(
                unmatched_definition,
                company_id,
                {
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "company_id": company_id,
                    "journal_id": journal_id,
                    "match_confidence_threshold": str(match_confidence_threshold),
                    "limit": limit,
                    "cursor": cursor,
                },
            )

        async def run(
            adapter: OdooAdapter,
            company: Company,
            request_id: str,
        ) -> ReportResponse:
            return await flag_unmatched_statement_lines(
                adapter,
                request,
                company_currency=_require_company_currency(company),
                company_name=company.name,
                request_id=request_id,
            )

        return await accounting_report(unmatched_definition, request, run)

    server.add_tool(
        unmatched_statement_lines_tool,
        name=unmatched_definition.name,
        title=unmatched_definition.title,
        description=unmatched_definition.description,
        annotations=unmatched_definition.annotations,
        meta=unmatched_definition.protocol_meta(),
        structured_output=True,
    )

    reconciliation_definition = get_tool_definition("reconcile_bank_statement_lines")

    async def reconciliation_failure(
        *,
        company_id: int,
        dry_run: bool,
        input_payload: Mapping[str, object],
        error: ErrorResponse,
        binding: ConnectionBinding | None,
        snapshot: CapabilitySnapshot | None = None,
    ) -> WriteSafetyResponse:
        response = WriteSafetyResponse(
            status="failed",
            outcome="not_attempted",
            request_id=error.request_id,
            company_id=company_id,
            error_code=error.error_code,
            error_message=error.error_message,
            remediation_hint=error.remediation_hint,
        )
        if binding is not None and storage is not None:
            try:
                storage.audit.append(
                    _audit_event(
                        binding,
                        reconciliation_definition,
                        company_id,
                        input_payload,
                        error.request_id,
                        snapshot,
                        final_status="failed",
                        actual_result=response.model_dump(mode="json"),
                        error=error,
                        dry_run=dry_run,
                    )
                )
            except Exception:
                LOGGER.warning("Reconciliation audit persistence failed; details suppressed.")
        return response

    async def reconcile_bank_statement_lines_tool(
        period: PeriodMonth,
        company_id: PositiveCompanyId,
        bank_journal_id: PositiveIdentifier,
        statement_line_ids: StatementLineIds,
        match_confidence_threshold: ConfidenceThreshold = Decimal("0.85"),
        dry_run: bool = True,
        idempotency_key: IdempotencyKey = None,
    ) -> WriteSafetyResponse:
        request_id = new_request_id()
        raw_payload: dict[str, object] = {
            "period": period,
            "company_id": company_id,
            "bank_journal_id": bank_journal_id,
            "statement_line_ids": list(statement_line_ids),
            "match_confidence_threshold": str(match_confidence_threshold),
            "dry_run": dry_run,
            "idempotency_key": idempotency_key,
        }
        binding: ConnectionBinding | None = None
        snapshot: CapabilitySnapshot | None = None
        try:
            request = ReconciliationInput(
                period=period,
                company_id=company_id,
                bank_journal_id=bank_journal_id,
                statement_line_ids=statement_line_ids,
                match_confidence_threshold=match_confidence_threshold,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
            )
        except ValidationError:
            try:
                binding = await resolver.resolve()
                if reconciliation_definition.required_permission not in binding.permissions:
                    raise OdooMcpError(
                        ErrorCode.ODOO_AUTH_FAILED,
                        "The resolved MCP identity is not authorized for reconciliation proposals.",
                        "Reconnect with accounting proposal permission and retry.",
                    )
                if company_id not in binding.connection.allowed_company_ids:
                    raise OdooMcpError(
                        ErrorCode.COMPANY_NOT_FOUND,
                        "The requested company is not authorized for this connection.",
                        "Use an authorized company ID and retry.",
                    )
                error = ErrorResponse(
                    error_code=ErrorCode.INVALID_INPUT,
                    error_message="The reconciliation proposal input is invalid.",
                    remediation_hint="Correct the period, identifiers, threshold, or controls.",
                    request_id=request_id,
                )
            except OdooMcpError as exc:
                error = exc.as_response(request_id)
            except Exception:
                error = ErrorResponse(
                    error_code=ErrorCode.UNKNOWN_ERROR,
                    error_message="The reconciliation proposal failed unexpectedly.",
                    remediation_hint="Retry the request or contact the service operator.",
                    request_id=request_id,
                )
            return await reconciliation_failure(
                company_id=company_id,
                dry_run=dry_run,
                input_payload=raw_payload,
                error=error,
                binding=binding,
            )

        initial_adapter: OdooAdapter | None = None
        try:
            binding = await resolver.resolve()
            if reconciliation_definition.required_permission not in binding.permissions:
                raise OdooMcpError(
                    ErrorCode.ODOO_AUTH_FAILED,
                    "The resolved MCP identity is not authorized for reconciliation proposals.",
                    "Reconnect with accounting proposal permission and retry.",
                )
            if request.company_id not in binding.connection.allowed_company_ids:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is not authorized for this connection.",
                    "Use an authorized company ID and retry.",
                )
            initial_adapter = await adapter_factory(binding.connection)
            companies = await initial_adapter.get_companies()
            company = next((item for item in companies if item.id == request.company_id), None)
            if company is None:
                raise OdooMcpError(
                    ErrorCode.COMPANY_NOT_FOUND,
                    "The requested company is unavailable to the technical user.",
                    "Check company access and retry.",
                )
            snapshot = await initial_adapter.get_capabilities()
            await _close_adapter(initial_adapter)
            initial_adapter = None
            if storage is None:
                raise RuntimeError("Reconciliation proposal storage is unavailable")
        except OdooMcpError as exc:
            error = exc.as_response(request_id)
        except Exception:
            error = ErrorResponse(
                error_code=ErrorCode.UNKNOWN_ERROR,
                error_message="The reconciliation proposal failed unexpectedly.",
                remediation_hint="Retry the request or contact the service operator.",
                request_id=request_id,
            )
        else:
            proposal: ReconciliationProposal | None = None

            async def load_proposal() -> ReconciliationProposal:
                adapter = await adapter_factory(binding.connection)
                try:
                    return await build_reconciliation_proposal(
                        adapter,
                        request,
                        company_currency=_require_company_currency(company),
                        company_name=company.name,
                        request_id=request_id,
                    )
                finally:
                    await _close_adapter(adapter)

            async def prepare() -> PreparedWrite:
                nonlocal proposal
                proposal = await load_proposal()
                payload = proposal.model_dump(mode="json", exclude={"artifact_markdown"})
                return PreparedWrite(
                    proposed_action={
                        "action": "persist_reconciliation_proposal",
                        "bank_journal_id": request.bank_journal_id,
                        "statement_line_ids": list(request.statement_line_ids),
                        "finalizes_odoo_reconciliation": False,
                    },
                    material_effects=payload,
                    artifact_markdown=proposal.artifact_markdown,
                )

            async def validate_current_state() -> None:
                nonlocal proposal
                refreshed = await load_proposal()
                if proposal is None or refreshed.model_dump() != proposal.model_dump():
                    raise OdooMcpError(
                        ErrorCode.ODOO_STATE_CONFLICT,
                        "The reconciliation candidates changed before proposal persistence.",
                        "Refresh the proposal and retry with a new idempotency key.",
                    )
                proposal = refreshed

            async def persist_proposal() -> AppliedWrite:
                if proposal is None:
                    raise RuntimeError("Reconciliation proposal was not prepared")
                payload = proposal.model_dump(mode="json", exclude={"artifact_markdown"})
                record = storage.proposal_journal.record(
                    request_id=request_id,
                    tenant_id=binding.tenant_id,
                    company_id=request.company_id,
                    tool_name=reconciliation_definition.name,
                    module="accounting",
                    proposal_type="bank_reconciliation",
                    payload=payload,
                    artifact_markdown=proposal.artifact_markdown,
                )
                return AppliedWrite(
                    material_effects={**payload, "proposal_id": record.id},
                    artifact_markdown=proposal.artifact_markdown,
                )

            command = WriteCommand(
                company_id=request.company_id,
                request_payload={
                    "period": request.period,
                    "bank_journal_id": request.bank_journal_id,
                    "statement_line_ids": list(request.statement_line_ids),
                    "match_confidence_threshold": str(request.match_confidence_threshold),
                },
                dry_run=request.dry_run,
                idempotency_key=request.idempotency_key,
            )
            return await WriteSafetyCoordinator(storage).run(
                binding=binding,
                definition=reconciliation_definition,
                module="accounting",
                snapshot=snapshot,
                command=command,
                prepare=prepare,
                validate_current_state=validate_current_state,
                execute=persist_proposal,
                request_id=request_id,
            )

        if initial_adapter is not None:
            try:
                await _close_adapter(initial_adapter)
            except Exception:
                LOGGER.warning("Odoo adapter cleanup failed; details were suppressed.")
                error = ErrorResponse(
                    error_code=ErrorCode.UNKNOWN_ERROR,
                    error_message="The reconciliation proposal failed unexpectedly.",
                    remediation_hint="Retry the request or contact the service operator.",
                    request_id=request_id,
                )
        return await reconciliation_failure(
            company_id=request.company_id,
            dry_run=request.dry_run,
            input_payload=raw_payload,
            error=error,
            binding=binding,
            snapshot=snapshot,
        )

    server.add_tool(
        reconcile_bank_statement_lines_tool,
        name=reconciliation_definition.name,
        title=reconciliation_definition.title,
        description=reconciliation_definition.description,
        annotations=reconciliation_definition.annotations,
        meta=reconciliation_definition.protocol_meta(),
        structured_output=True,
    )

    def add_open_documents_tool(name: str, *, bills: bool) -> None:
        definition = get_tool_definition(name)

        async def tool(
            as_of_date: date,
            company_id: PositiveCompanyId,
            partner_ids: tuple[int, ...] = (),
            overdue_only: bool = False,
            limit: ReportLimit = 100,
            cursor: ReportCursor = None,
        ) -> AccountingToolResponse:
            try:
                request = OpenDocumentsInput(
                    as_of_date=as_of_date,
                    company_id=company_id,
                    partner_ids=partner_ids,
                    overdue_only=overdue_only,
                    limit=limit,
                    cursor=cursor,
                )
            except ValidationError:
                return await invalid_accounting_report(
                    definition,
                    company_id,
                    {
                        "as_of_date": as_of_date.isoformat(),
                        "company_id": company_id,
                        "partner_ids": list(partner_ids),
                        "overdue_only": overdue_only,
                        "limit": limit,
                        "cursor": cursor,
                    },
                )

            async def run(
                adapter: OdooAdapter, company: Company, request_id: str
            ) -> ReportResponse:
                return await list_open_documents(
                    adapter,
                    request,
                    bills=bills,
                    company_name=company.name,
                    request_id=request_id,
                )

            return await accounting_report(definition, request, run)

        server.add_tool(
            tool,
            name=definition.name,
            title=definition.title,
            description=definition.description,
            annotations=definition.annotations,
            meta=definition.protocol_meta(),
            structured_output=True,
        )

    add_open_documents_tool("list_open_invoices", bills=False)
    add_open_documents_tool("list_open_bills", bills=True)

    def add_create_invoice_tool(name: str, *, supplier: bool) -> None:
        definition = get_tool_definition(name)

        async def invoke(
            company_id: PositiveCompanyId,
            partner_id: PositiveIdentifier,
            invoice_date: date,
            lines: tuple[InvoiceLineInput, ...],
            currency_id: PositiveIdentifier | None,
            payment_term_id: PositiveIdentifier | None,
            analytic_account_id: PositiveIdentifier | None,
            vendor_reference: str | None,
            dry_run: bool,
            idempotency_key: IdempotencyKey,
        ) -> WriteSafetyResponse:
            raw_payload: dict[str, object] = {
                "partner_id": partner_id,
                "invoice_date": invoice_date.isoformat(),
                "lines": [item.model_dump(mode="json") for item in lines],
                "currency_id": currency_id,
                "payment_term_id": payment_term_id,
                "analytic_account_id": analytic_account_id,
                "vendor_reference": vendor_reference,
                "idempotency_key": idempotency_key,
            }
            try:
                request = CreateInvoiceInput(
                    company_id=company_id,
                    partner_id=partner_id,
                    invoice_date=invoice_date,
                    lines=lines,
                    currency_id=currency_id,
                    payment_term_id=payment_term_id,
                    analytic_account_id=analytic_account_id,
                    vendor_reference=vendor_reference,
                    dry_run=dry_run,
                    idempotency_key=idempotency_key,
                )
            except ValidationError:
                return await invalid_accounting_write(definition, company_id, dry_run, raw_payload)
            payload = request.model_dump(
                mode="json", exclude={"company_id", "dry_run", "idempotency_key"}
            )

            async def prepare_op(
                adapter: OdooAdapter,
            ) -> tuple[object, PreparedWrite]:
                return await prepare_invoice_draft(adapter, request, supplier=supplier)

            async def validate_op(adapter: OdooAdapter, state: object) -> None:
                if not isinstance(state, InvoiceDraft):
                    raise RuntimeError("Invalid prepared invoice state")
                refreshed, _ = await prepare_invoice_draft(adapter, request, supplier=supplier)
                if refreshed != state:
                    raise OdooMcpError(
                        ErrorCode.ODOO_STATE_CONFLICT,
                        "An invoice reference changed before draft creation.",
                        "Refresh the preview and retry with a new idempotency key.",
                    )

            async def execute_op(adapter: OdooAdapter, state: object) -> AppliedWrite:
                if not isinstance(state, InvoiceDraft):
                    raise RuntimeError("Invalid prepared invoice state")
                return await execute_invoice_draft(adapter, state)

            return await accounting_write(
                definition,
                WriteCommand(
                    company_id=request.company_id,
                    request_payload=payload,
                    dry_run=request.dry_run,
                    idempotency_key=request.idempotency_key,
                ),
                prepare_op,
                validate_op,
                execute_op,
            )

        if supplier:

            async def supplier_tool(
                company_id: PositiveCompanyId,
                partner_id: PositiveIdentifier,
                invoice_date: date,
                lines: tuple[InvoiceLineInput, ...],
                currency_id: PositiveIdentifier | None = None,
                payment_term_id: PositiveIdentifier | None = None,
                analytic_account_id: PositiveIdentifier | None = None,
                vendor_reference: str | None = None,
                dry_run: bool = True,
                idempotency_key: IdempotencyKey = None,
            ) -> WriteSafetyResponse:
                return await invoke(
                    company_id,
                    partner_id,
                    invoice_date,
                    lines,
                    currency_id,
                    payment_term_id,
                    analytic_account_id,
                    vendor_reference,
                    dry_run,
                    idempotency_key,
                )

            registered_tool: Callable[..., Awaitable[WriteSafetyResponse]] = supplier_tool

        else:

            async def customer_tool(
                company_id: PositiveCompanyId,
                partner_id: PositiveIdentifier,
                invoice_date: date,
                lines: tuple[InvoiceLineInput, ...],
                currency_id: PositiveIdentifier | None = None,
                payment_term_id: PositiveIdentifier | None = None,
                analytic_account_id: PositiveIdentifier | None = None,
                dry_run: bool = True,
                idempotency_key: IdempotencyKey = None,
            ) -> WriteSafetyResponse:
                return await invoke(
                    company_id,
                    partner_id,
                    invoice_date,
                    lines,
                    currency_id,
                    payment_term_id,
                    analytic_account_id,
                    None,
                    dry_run,
                    idempotency_key,
                )

            registered_tool = customer_tool

        server.add_tool(
            registered_tool,
            name=definition.name,
            title=definition.title,
            description=definition.description,
            annotations=definition.annotations,
            meta=definition.protocol_meta(),
            structured_output=True,
        )

    add_create_invoice_tool("create_customer_invoice", supplier=False)
    add_create_invoice_tool("create_supplier_bill", supplier=True)

    credit_definition = get_tool_definition("create_credit_note")

    async def create_credit_note_tool(
        company_id: PositiveCompanyId,
        original_move_id: PositiveIdentifier,
        credit_date: date,
        reason: str,
        dry_run: bool = True,
        idempotency_key: IdempotencyKey = None,
    ) -> WriteSafetyResponse:
        raw_payload: dict[str, object] = {
            "original_move_id": original_move_id,
            "credit_date": credit_date.isoformat(),
            "reason": reason,
            "idempotency_key": idempotency_key,
        }
        try:
            request = CreditNoteInput(
                company_id=company_id,
                original_move_id=original_move_id,
                credit_date=credit_date,
                reason=reason,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
            )
        except ValidationError:
            return await invalid_accounting_write(
                credit_definition, company_id, dry_run, raw_payload
            )
        payload = request.model_dump(
            mode="json", exclude={"company_id", "dry_run", "idempotency_key"}
        )

        async def prepare_op(adapter: OdooAdapter) -> tuple[object, PreparedWrite]:
            return await prepare_credit_note(adapter, request)

        async def validate_op(adapter: OdooAdapter, state: object) -> None:
            if not isinstance(state, InvoiceEffect):
                raise RuntimeError("Invalid prepared credit-note state")
            refreshed, _ = await prepare_credit_note(adapter, request)
            if refreshed != state:
                raise OdooMcpError(
                    ErrorCode.ODOO_STATE_CONFLICT,
                    "The original invoice changed before credit-note creation.",
                    "Refresh and retry with a new idempotency key.",
                )

        async def execute_op(adapter: OdooAdapter, state: object) -> AppliedWrite:
            if not isinstance(state, InvoiceEffect):
                raise RuntimeError("Invalid prepared credit-note state")
            effect = await adapter.create_credit_note(
                request.company_id,
                request.original_move_id,
                request.credit_date,
                request.reason.strip(),
            )
            return AppliedWrite(
                material_effects=effect.model_dump(mode="json", exclude={"validation_lines"}),
                record_refs=(f"account.move:{effect.id}",),
            )

        return await accounting_write(
            credit_definition,
            WriteCommand(
                company_id=request.company_id,
                request_payload=payload,
                dry_run=request.dry_run,
                idempotency_key=request.idempotency_key,
            ),
            prepare_op,
            validate_op,
            execute_op,
        )

    server.add_tool(
        create_credit_note_tool,
        name=credit_definition.name,
        title=credit_definition.title,
        description=credit_definition.description,
        annotations=credit_definition.annotations,
        meta=credit_definition.protocol_meta(),
        structured_output=True,
    )

    validation_definition = get_tool_definition("validate_invoice")

    async def validate_invoice_tool(
        invoice_id: PositiveIdentifier,
        company_id: PositiveCompanyId,
        dry_run: bool = True,
        idempotency_key: IdempotencyKey = None,
    ) -> WriteSafetyResponse:
        request = ValidateInvoiceInput(
            invoice_id=invoice_id,
            company_id=company_id,
            dry_run=dry_run,
            idempotency_key=idempotency_key,
        )

        async def prepare_op(adapter: OdooAdapter) -> tuple[object, PreparedWrite]:
            return await prepare_validation(adapter, request)

        async def validate_op(adapter: OdooAdapter, state: object) -> None:
            if not isinstance(state, InvoiceEffect):
                raise RuntimeError("Invalid prepared validation state")
            refreshed, prepared = await prepare_validation(adapter, request)
            if refreshed != state or prepared.needs_input:
                raise OdooMcpError(
                    ErrorCode.ODOO_STATE_CONFLICT,
                    "The draft changed before posting.",
                    "Refresh and retry with a new idempotency key.",
                )

        async def execute_op(adapter: OdooAdapter, state: object) -> AppliedWrite:
            if not isinstance(state, InvoiceEffect):
                raise RuntimeError("Invalid prepared validation state")
            effect = await adapter.post_invoice(request.company_id, request.invoice_id)
            return AppliedWrite(
                material_effects=effect.model_dump(mode="json", exclude={"validation_lines"}),
                record_refs=(f"account.move:{effect.id}",),
            )

        return await accounting_write(
            validation_definition,
            WriteCommand(
                company_id=request.company_id,
                request_payload={"invoice_id": request.invoice_id},
                dry_run=request.dry_run,
                idempotency_key=request.idempotency_key,
            ),
            prepare_op,
            validate_op,
            execute_op,
        )

    server.add_tool(
        validate_invoice_tool,
        name=validation_definition.name,
        title=validation_definition.title,
        description=validation_definition.description,
        annotations=validation_definition.annotations,
        meta=validation_definition.protocol_meta(),
        structured_output=True,
    )

    payment_definition = get_tool_definition("register_payment")

    async def register_payment_tool(
        invoice_id: PositiveIdentifier,
        company_id: PositiveCompanyId,
        payment_date: date,
        amount: Decimal | None = None,
        journal_id: PositiveIdentifier | None = None,
        payment_method_line_id: PositiveIdentifier | None = None,
        dry_run: bool = True,
        idempotency_key: IdempotencyKey = None,
    ) -> WriteSafetyResponse:
        raw_payload: dict[str, object] = {
            "invoice_id": invoice_id,
            "payment_date": payment_date.isoformat(),
            "amount": str(amount) if amount is not None else None,
            "journal_id": journal_id,
            "payment_method_line_id": payment_method_line_id,
            "idempotency_key": idempotency_key,
        }
        try:
            request = RegisterPaymentInput(
                invoice_id=invoice_id,
                company_id=company_id,
                payment_date=payment_date,
                amount=amount,
                journal_id=journal_id,
                payment_method_line_id=payment_method_line_id,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
            )
        except ValidationError:
            return await invalid_accounting_write(
                payment_definition, company_id, dry_run, raw_payload
            )

        async def prepare_op(adapter: OdooAdapter) -> tuple[object, PreparedWrite]:
            effect, registration, prepared = await prepare_payment(adapter, request)
            return (effect, registration), prepared

        async def validate_op(adapter: OdooAdapter, state: object) -> None:
            if not isinstance(state, tuple) or len(state) != 2:
                raise RuntimeError("Invalid prepared payment state")
            effect, registration, _ = await prepare_payment(adapter, request)
            if (effect, registration) != state:
                raise OdooMcpError(
                    ErrorCode.ODOO_STATE_CONFLICT,
                    "The invoice or payment route changed before registration.",
                    "Refresh and retry with a new idempotency key.",
                )

        async def execute_op(adapter: OdooAdapter, state: object) -> AppliedWrite:
            if not isinstance(state, tuple) or len(state) != 2:
                raise RuntimeError("Invalid prepared payment state")
            registration = state[1]
            if not isinstance(registration, PaymentRegistration):
                raise RuntimeError("Payment route was not resolved")
            effect = await adapter.register_payment(registration)
            return AppliedWrite(
                material_effects=effect.model_dump(mode="json", exclude={"validation_lines"}),
                record_refs=(f"account.move:{effect.id}",),
            )

        return await accounting_write(
            payment_definition,
            WriteCommand(
                company_id=request.company_id,
                request_payload=request.model_dump(
                    mode="json", exclude={"company_id", "dry_run", "idempotency_key"}
                ),
                dry_run=request.dry_run,
                idempotency_key=request.idempotency_key,
            ),
            prepare_op,
            validate_op,
            execute_op,
        )

    server.add_tool(
        register_payment_tool,
        name=payment_definition.name,
        title=payment_definition.title,
        description=payment_definition.description,
        annotations=payment_definition.annotations,
        meta=payment_definition.protocol_meta(),
        structured_output=True,
    )

    journal_list_definition = get_tool_definition("list_journal_entries")

    async def list_journal_entries_tool(
        period_start: date,
        period_end: date,
        company_id: PositiveCompanyId,
        journal_ids: tuple[int, ...] = (),
        states: tuple[Literal["draft", "posted"], ...] = (),
        limit: ReportLimit = 100,
        cursor: ReportCursor = None,
    ) -> AccountingToolResponse:
        try:
            request = JournalEntriesInput(
                period_start=period_start,
                period_end=period_end,
                company_id=company_id,
                journal_ids=journal_ids,
                states=states,
                limit=limit,
                cursor=cursor,
            )
        except ValidationError:
            return await invalid_accounting_report(
                journal_list_definition,
                company_id,
                {
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "journal_ids": list(journal_ids),
                    "states": list(states),
                    "limit": limit,
                    "cursor": cursor,
                },
            )

        async def run(adapter: OdooAdapter, company: Company, request_id: str) -> ReportResponse:
            return await list_journal_entries(
                adapter, request, company_name=company.name, request_id=request_id
            )

        return await accounting_report(journal_list_definition, request, run)

    server.add_tool(
        list_journal_entries_tool,
        name=journal_list_definition.name,
        title=journal_list_definition.title,
        description=journal_list_definition.description,
        annotations=journal_list_definition.annotations,
        meta=journal_list_definition.protocol_meta(),
        structured_output=True,
    )

    create_journal_definition = get_tool_definition("create_journal_entry")

    async def create_journal_entry_tool(
        company_id: PositiveCompanyId,
        journal_id: PositiveIdentifier,
        entry_date: date,
        lines: tuple[JournalEntryLineInput, ...],
        reference: str | None = None,
        dry_run: bool = True,
        idempotency_key: IdempotencyKey = None,
    ) -> WriteSafetyResponse:
        raw_payload: dict[str, object] = {
            "journal_id": journal_id,
            "entry_date": entry_date.isoformat(),
            "reference": reference,
            "lines": [line.model_dump(mode="json") for line in lines],
            "idempotency_key": idempotency_key,
        }
        try:
            request = CreateJournalEntryInput(
                company_id=company_id,
                journal_id=journal_id,
                entry_date=entry_date,
                reference=reference,
                lines=lines,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
            )
        except ValidationError:
            return await invalid_accounting_write(
                create_journal_definition, company_id, dry_run, raw_payload
            )

        async def prepare_op(adapter: OdooAdapter) -> tuple[object, PreparedWrite]:
            return await prepare_journal_entry_draft(adapter, request)

        async def validate_op(adapter: OdooAdapter, state: object) -> None:
            if not isinstance(state, JournalEntryDraft):
                raise RuntimeError("Invalid prepared journal-entry state")
            refreshed, _ = await prepare_journal_entry_draft(adapter, request)
            if refreshed != state:
                raise OdooMcpError(
                    ErrorCode.ODOO_STATE_CONFLICT,
                    "A journal entry reference changed before draft creation.",
                    "Refresh and retry with a new idempotency key.",
                )

        async def execute_op(adapter: OdooAdapter, state: object) -> AppliedWrite:
            if not isinstance(state, JournalEntryDraft):
                raise RuntimeError("Invalid prepared journal-entry state")
            return await execute_journal_entry_draft(adapter, state)

        return await accounting_write(
            create_journal_definition,
            WriteCommand(
                company_id=request.company_id,
                request_payload=request.model_dump(
                    mode="json", exclude={"company_id", "dry_run", "idempotency_key"}
                ),
                dry_run=request.dry_run,
                idempotency_key=request.idempotency_key,
            ),
            prepare_op,
            validate_op,
            execute_op,
        )

    server.add_tool(
        create_journal_entry_tool,
        name=create_journal_definition.name,
        title=create_journal_definition.title,
        description=create_journal_definition.description,
        annotations=create_journal_definition.annotations,
        meta=create_journal_definition.protocol_meta(),
        structured_output=True,
    )

    post_journal_definition = get_tool_definition("post_journal_entry")

    async def post_journal_entry_tool(
        company_id: PositiveCompanyId,
        move_id: PositiveIdentifier,
        dry_run: bool = True,
        idempotency_key: IdempotencyKey = None,
    ) -> WriteSafetyResponse:
        request = PostJournalEntryInput(
            company_id=company_id,
            move_id=move_id,
            dry_run=dry_run,
            idempotency_key=idempotency_key,
        )

        async def prepare_op(adapter: OdooAdapter) -> tuple[object, PreparedWrite]:
            return await prepare_journal_entry_post(adapter, request)

        async def validate_op(adapter: OdooAdapter, state: object) -> None:
            if not isinstance(state, JournalEntry):
                raise RuntimeError("Invalid prepared journal-post state")
            refreshed, prepared = await prepare_journal_entry_post(adapter, request)
            if refreshed != state or prepared.needs_input:
                raise OdooMcpError(
                    ErrorCode.ODOO_STATE_CONFLICT,
                    "The draft journal entry changed before posting.",
                    "Refresh and retry with a new idempotency key.",
                )

        async def execute_op(adapter: OdooAdapter, state: object) -> AppliedWrite:
            if not isinstance(state, JournalEntry):
                raise RuntimeError("Invalid prepared journal-post state")
            if state.id != request.move_id:
                raise OdooMcpError(
                    ErrorCode.ODOO_STATE_CONFLICT,
                    "The draft journal entry identity changed before posting.",
                    "Refresh and retry with a new idempotency key.",
                )
            return await execute_journal_entry_post(adapter, request.company_id, request.move_id)

        return await accounting_write(
            post_journal_definition,
            WriteCommand(
                company_id=request.company_id,
                request_payload={"move_id": request.move_id},
                dry_run=request.dry_run,
                idempotency_key=request.idempotency_key,
            ),
            prepare_op,
            validate_op,
            execute_op,
        )

    server.add_tool(
        post_journal_entry_tool,
        name=post_journal_definition.name,
        title=post_journal_definition.title,
        description=post_journal_definition.description,
        annotations=post_journal_definition.annotations,
        meta=post_journal_definition.protocol_meta(),
        structured_output=True,
    )
    register_payroll_tools(
        server,
        resolver,
        adapter_factory=adapter_factory,
        storage=storage,
    )
    return server


def _audit_event(
    binding: ConnectionBinding,
    definition: ToolDefinition,
    company_id: int,
    input_payload: Mapping[str, object],
    request_id: str,
    snapshot: CapabilitySnapshot | None,
    *,
    final_status: str,
    actual_result: dict[str, object] | None = None,
    error: ErrorResponse | None = None,
    dry_run: bool = False,
) -> AuditEvent:
    return AuditEvent(
        request_id=request_id,
        tenant_id=binding.tenant_id,
        company_id=company_id,
        tool_name=definition.name,
        tool_version=definition.version,
        module="accounting",
        authenticated_subject=binding.authenticated_subject,
        mcp_client=binding.mcp_client,
        odoo_db_name=binding.connection.database,
        odoo_user=binding.connection.username,
        odoo_version=None if snapshot is None else str(snapshot.version),
        odoo_transport=None if snapshot is None else snapshot.transport,
        input_payload=input_payload,
        dry_run=dry_run,
        proposed_action=None,
        actual_result=actual_result,
        affected_odoo_records=(),
        error_code=None if error is None else error.error_code.value,
        error_message=None if error is None else error.error_message,
        final_status=final_status,
    )
