"""MCP validation and routing over the shared deterministic registry."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from decimal import Decimal
from typing import Annotated, TypeAlias

from mcp.server import MCPServer
from pydantic import Field, ValidationError

from odoo_mcp.adapters.accounting import RelatedRecord
from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
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
    CashbookInput,
    CashbookResponse,
    ReconciliationInput,
    ReconciliationProposal,
    TrialBalanceInput,
    TrialBalanceResponse,
    UnmatchedStatementLinesInput,
    UnmatchedStatementLinesResponse,
)
from odoo_mcp.policy.write_safety import (
    AppliedWrite,
    PreparedWrite,
    WriteCommand,
    WriteSafetyCoordinator,
    WriteSafetyResponse,
)
from odoo_mcp.storage import AuditEvent, Storage
from odoo_mcp.workflows.accounting.cashbook import get_cashbook
from odoo_mcp.workflows.accounting.reconcile_bank import (
    build_reconciliation_proposal,
    flag_unmatched_statement_lines,
)
from odoo_mcp.workflows.accounting.reports import get_aged_balance, get_trial_balance
from odoo_mcp.workflows.core.capabilities import get_erp_capabilities

AdapterFactory = Callable[[object], Awaitable[OdooAdapter]]
ReportResponse: TypeAlias = (
    TrialBalanceResponse | AgingResponse | CashbookResponse | UnmatchedStatementLinesResponse
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
