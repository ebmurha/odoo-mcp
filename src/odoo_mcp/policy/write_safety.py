"""Reusable fail-closed policy boundary for write-capable workflows."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from odoo_mcp.adapters.base import CapabilitySnapshot
from odoo_mcp.adapters.odoo.connections import ConnectionBinding
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.registry import ToolDefinition
from odoo_mcp.mcp.request_ids import new_request_id
from odoo_mcp.storage import AuditEvent, Storage
from odoo_mcp.storage.errors import IdempotencyPayloadMismatch
from odoo_mcp.storage.models import IdempotencyDisposition, IdempotencyState

LOGGER = logging.getLogger(__name__)
WriteStatus = Literal["preview", "succeeded", "needs_input", "failed"]
WriteOutcome = Literal["not_attempted", "known", "unknown"]


@dataclass(frozen=True, slots=True)
class WriteCommand:
    """Server-normalized controls and business payload for one write call."""

    company_id: int
    request_payload: Mapping[str, object]
    dry_run: bool = True
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedWrite:
    """Validated proposed action produced after policy gates pass."""

    proposed_action: Mapping[str, object]
    material_effects: Mapping[str, object]
    artifact_markdown: str | None = None
    needs_input: bool = False


@dataclass(frozen=True, slots=True)
class AppliedWrite:
    """Known Odoo result returned only after the mutation completes."""

    material_effects: Mapping[str, object]
    record_refs: tuple[str, ...] = field(default_factory=tuple)
    artifact_markdown: str | None = None


class UnknownWriteOutcome(RuntimeError):
    """The adapter cannot prove whether the attempted Odoo mutation committed."""


class KnownWriteFailure(RuntimeError):
    """The adapter proved that Odoo rejected the mutation without applying it."""

    def __init__(self, error: OdooMcpError) -> None:
        super().__init__(error.safe_message)
        self.error = error


class WriteSafetyResponse(BaseModel):
    """Common compact result returned by the reusable write-safety boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: WriteStatus
    outcome: WriteOutcome
    request_id: str
    company_id: int
    proposed_action: dict[str, object] | None = None
    material_effects: dict[str, object] = Field(default_factory=dict)
    record_refs: tuple[str, ...] = ()
    artifact_markdown: str | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    remediation_hint: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> WriteSafetyResponse:
        errors = (self.error_code, self.error_message, self.remediation_hint)
        if self.status == "failed" and any(value is None for value in errors):
            raise ValueError("failed write responses require structured error fields")
        if self.status != "failed" and any(value is not None for value in errors):
            raise ValueError("successful write responses cannot contain error fields")
        if self.outcome == "unknown" and self.status != "failed":
            raise ValueError("unknown write outcomes must fail explicitly")
        return self


PrepareWrite = Callable[[], Awaitable[PreparedWrite]]
ValidateCurrentState = Callable[[], Awaitable[None]]
ExecuteWrite = Callable[[], Awaitable[AppliedWrite]]


def validate_write_tool_definition(definition: ToolDefinition) -> None:
    """Reject registry metadata that would misrepresent a write-capable tool."""

    annotations = definition.annotations
    if definition.risk_level == "read":
        raise ValueError("Write safety requires a write-capable risk level")
    if (
        annotations.read_only_hint is not False
        or annotations.idempotent_hint is not True
        or annotations.open_world_hint is not True
    ):
        raise ValueError("Write-capable tool annotations do not match the safety contract")


class WriteSafetyCoordinator:
    """Order authorization, preview, replay protection, validation, write, and audit."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    async def run(
        self,
        *,
        binding: ConnectionBinding,
        definition: ToolDefinition,
        module: str,
        snapshot: CapabilitySnapshot,
        command: WriteCommand,
        prepare: PrepareWrite,
        validate_current_state: ValidateCurrentState,
        execute: ExecuteWrite,
        request_id: str | None = None,
    ) -> WriteSafetyResponse:
        validate_write_tool_definition(definition)
        selected_request_id = request_id or new_request_id()

        gate_error = self._gate_error(binding, definition, snapshot, command)
        if gate_error is not None:
            response = self._failure(
                gate_error,
                selected_request_id,
                command.company_id,
                outcome="not_attempted",
            )
            return self._append_or_fail(
                self._event(
                    binding,
                    definition,
                    module,
                    snapshot,
                    command,
                    selected_request_id,
                    final_status="rejected",
                    response=response,
                ),
                response,
            )

        if command.dry_run:
            try:
                prepared = await prepare()
            except OdooMcpError as exc:
                return self._record_pre_write_failure(
                    binding,
                    definition,
                    module,
                    snapshot,
                    command,
                    selected_request_id,
                    exc,
                )
            except Exception:
                return self._record_pre_write_failure(
                    binding,
                    definition,
                    module,
                    snapshot,
                    command,
                    selected_request_id,
                    self._unexpected_pre_write_error(),
                )
            response = WriteSafetyResponse(
                status="needs_input" if prepared.needs_input else "preview",
                outcome="not_attempted",
                request_id=selected_request_id,
                company_id=command.company_id,
                proposed_action=dict(prepared.proposed_action),
                material_effects=dict(prepared.material_effects),
                artifact_markdown=prepared.artifact_markdown,
            )
            return self._append_or_fail(
                self._event(
                    binding,
                    definition,
                    module,
                    snapshot,
                    command,
                    selected_request_id,
                    final_status="needs_input" if prepared.needs_input else "previewed",
                    response=response,
                ),
                response,
            )

        idempotency_key = command.idempotency_key
        if idempotency_key is None or not idempotency_key.strip():
            response = self._failure(
                OdooMcpError(
                    ErrorCode.EXECUTION_NOT_EXPLICIT,
                    "Execution requires an explicit non-empty idempotency key.",
                    "Submit dry_run false with a stable idempotency key.",
                ),
                selected_request_id,
                command.company_id,
                outcome="not_attempted",
            )
            return self._append_or_fail(
                self._event(
                    binding,
                    definition,
                    module,
                    snapshot,
                    command,
                    selected_request_id,
                    final_status="rejected",
                    response=response,
                ),
                response,
            )

        attempt = self._event(
            binding,
            definition,
            module,
            snapshot,
            command,
            selected_request_id,
            final_status="attempted",
            proposed_action={
                "tool": definition.name,
                "risk_level": definition.risk_level,
            },
        )
        try:
            decision = self._storage.execution.begin(
                attempt,
                idempotency_key,
                command.request_payload,
            )
        except IdempotencyPayloadMismatch:
            response = self._failure(
                OdooMcpError(
                    ErrorCode.IDEMPOTENCY_KEY_PAYLOAD_MISMATCH,
                    "The idempotency key was already used for a different request.",
                    "Use the original payload or submit a new idempotency key.",
                ),
                selected_request_id,
                command.company_id,
                outcome="not_attempted",
            )
            return self._append_or_fail(
                self._event(
                    binding,
                    definition,
                    module,
                    snapshot,
                    command,
                    selected_request_id,
                    final_status="conflicted",
                    response=response,
                ),
                response,
            )
        except Exception:
            return self._failure(
                self._unexpected_pre_write_error(),
                selected_request_id,
                command.company_id,
                outcome="not_attempted",
            )

        if decision.disposition is IdempotencyDisposition.IN_PROGRESS:
            response = self._failure(
                OdooMcpError(
                    ErrorCode.CONCURRENT_OPERATION_IN_PROGRESS,
                    "An identical write operation is already in progress.",
                    "Wait for recovery or replay the same request later.",
                ),
                selected_request_id,
                command.company_id,
                outcome="not_attempted",
            )
            return self._append_or_fail(
                self._event(
                    binding,
                    definition,
                    module,
                    snapshot,
                    command,
                    selected_request_id,
                    final_status="conflicted",
                    response=response,
                ),
                response,
            )
        if decision.disposition is IdempotencyDisposition.REPLAY:
            try:
                response = WriteSafetyResponse.model_validate(decision.response)
            except Exception:
                return self._failure(
                    self._unexpected_pre_write_error(),
                    selected_request_id,
                    command.company_id,
                    outcome="not_attempted",
                )
            return self._append_or_fail(
                self._event(
                    binding,
                    definition,
                    module,
                    snapshot,
                    command,
                    selected_request_id,
                    final_status="replayed",
                    response=response,
                ),
                response,
            )

        try:
            prepared = await prepare()
        except OdooMcpError as exc:
            return self._finish_pre_write_failure(
                binding,
                definition,
                module,
                snapshot,
                command,
                selected_request_id,
                idempotency_key,
                exc,
            )
        except Exception:
            return self._finish_pre_write_failure(
                binding,
                definition,
                module,
                snapshot,
                command,
                selected_request_id,
                idempotency_key,
                self._unexpected_pre_write_error(),
            )

        if prepared.needs_input:
            response = WriteSafetyResponse(
                status="needs_input",
                outcome="not_attempted",
                request_id=selected_request_id,
                company_id=command.company_id,
                proposed_action=dict(prepared.proposed_action),
                material_effects=dict(prepared.material_effects),
                artifact_markdown=prepared.artifact_markdown,
            )
            try:
                self._finish_after_attempt(
                    binding,
                    definition,
                    module,
                    snapshot,
                    command,
                    selected_request_id,
                    idempotency_key,
                    IdempotencyState.FAILED,
                    "needs_input",
                    response,
                    prepared,
                )
            except Exception:
                LOGGER.warning("Needs-input outcome persistence failed; details suppressed.")
            return response

        try:
            await validate_current_state()
        except OdooMcpError as exc:
            return self._finish_pre_write_failure(
                binding,
                definition,
                module,
                snapshot,
                command,
                selected_request_id,
                idempotency_key,
                exc,
                prepared,
            )
        except Exception:
            return self._finish_pre_write_failure(
                binding,
                definition,
                module,
                snapshot,
                command,
                selected_request_id,
                idempotency_key,
                self._unexpected_pre_write_error(),
                prepared,
            )

        try:
            applied = await execute()
        except KnownWriteFailure as exc:
            return self._finish_known_write_failure(
                binding,
                definition,
                module,
                snapshot,
                command,
                selected_request_id,
                idempotency_key,
                exc.error,
                prepared,
            )
        except Exception:
            response = self._failure(
                OdooMcpError(
                    ErrorCode.UNKNOWN_ERROR,
                    "The Odoo write outcome is unknown and requires recovery.",
                    "Do not retry with a new key; inspect Odoo and recover this request.",
                ),
                selected_request_id,
                command.company_id,
                outcome="unknown",
                prepared=prepared,
            )
            try:
                self._finish_after_attempt(
                    binding,
                    definition,
                    module,
                    snapshot,
                    command,
                    selected_request_id,
                    idempotency_key,
                    IdempotencyState.UNKNOWN,
                    "unknown",
                    response,
                    prepared,
                )
            except Exception:
                LOGGER.warning("Unknown write outcome persistence failed; details suppressed.")
            return response

        response = WriteSafetyResponse(
            status="succeeded",
            outcome="known",
            request_id=selected_request_id,
            company_id=command.company_id,
            proposed_action=dict(prepared.proposed_action),
            material_effects=dict(applied.material_effects),
            record_refs=applied.record_refs,
            artifact_markdown=applied.artifact_markdown or prepared.artifact_markdown,
        )
        try:
            self._storage.execution.finish(
                self._event(
                    binding,
                    definition,
                    module,
                    snapshot,
                    command,
                    selected_request_id,
                    final_status="succeeded",
                    response=response,
                    proposed_action=prepared.proposed_action,
                    affected_records=applied.record_refs,
                ),
                idempotency_key,
                IdempotencyState.SUCCEEDED,
                response.model_dump(mode="json"),
            )
        except Exception:
            LOGGER.warning("Write outcome persistence failed; details were suppressed.")
            return self._failure(
                OdooMcpError(
                    ErrorCode.UNKNOWN_ERROR,
                    "The Odoo write outcome is unknown and requires recovery.",
                    "Do not retry with a new key; inspect Odoo and recover this request.",
                ),
                selected_request_id,
                command.company_id,
                outcome="unknown",
                prepared=prepared,
            )
        return response

    def _finish_known_write_failure(
        self,
        binding: ConnectionBinding,
        definition: ToolDefinition,
        module: str,
        snapshot: CapabilitySnapshot,
        command: WriteCommand,
        request_id: str,
        idempotency_key: str,
        error: OdooMcpError,
        prepared: PreparedWrite,
    ) -> WriteSafetyResponse:
        response = self._failure(
            error,
            request_id,
            command.company_id,
            outcome="known",
            prepared=prepared,
        )
        try:
            self._finish_after_attempt(
                binding,
                definition,
                module,
                snapshot,
                command,
                request_id,
                idempotency_key,
                IdempotencyState.FAILED,
                "failed",
                response,
                prepared,
            )
        except Exception:
            LOGGER.warning("Known write failure persistence failed; details suppressed.")
        return response

    @staticmethod
    def _gate_error(
        binding: ConnectionBinding,
        definition: ToolDefinition,
        snapshot: CapabilitySnapshot,
        command: WriteCommand,
    ) -> OdooMcpError | None:
        if command.company_id <= 0:
            return OdooMcpError(
                ErrorCode.INVALID_INPUT,
                "The write request company ID is invalid.",
                "Use a positive authorized company ID.",
            )
        if definition.required_permission not in binding.permissions:
            return OdooMcpError(
                ErrorCode.ODOO_AUTH_FAILED,
                "The resolved MCP identity is not authorized for this write.",
                "Reconnect with the required write permission and retry.",
            )
        if command.company_id not in binding.connection.allowed_company_ids:
            return OdooMcpError(
                ErrorCode.COMPANY_NOT_FOUND,
                "The requested company is not authorized for this connection.",
                "Use an authorized company ID and retry.",
            )
        capability = definition.required_capability
        if capability is not None and not snapshot.modules.get(capability, False):
            return OdooMcpError(
                ErrorCode.CAPABILITY_NOT_AVAILABLE,
                "The required Odoo capability is unavailable.",
                "Enable the required module access or use an available tool.",
            )
        return None

    def _record_pre_write_failure(
        self,
        binding: ConnectionBinding,
        definition: ToolDefinition,
        module: str,
        snapshot: CapabilitySnapshot,
        command: WriteCommand,
        request_id: str,
        error: OdooMcpError,
    ) -> WriteSafetyResponse:
        response = self._failure(
            error,
            request_id,
            command.company_id,
            outcome="not_attempted",
        )
        return self._append_or_fail(
            self._event(
                binding,
                definition,
                module,
                snapshot,
                command,
                request_id,
                final_status="failed",
                response=response,
            ),
            response,
        )

    def _finish_pre_write_failure(
        self,
        binding: ConnectionBinding,
        definition: ToolDefinition,
        module: str,
        snapshot: CapabilitySnapshot,
        command: WriteCommand,
        request_id: str,
        idempotency_key: str,
        error: OdooMcpError,
        prepared: PreparedWrite | None = None,
    ) -> WriteSafetyResponse:
        response = self._failure(
            error,
            request_id,
            command.company_id,
            outcome="not_attempted",
            prepared=prepared,
        )
        try:
            self._finish_after_attempt(
                binding,
                definition,
                module,
                snapshot,
                command,
                request_id,
                idempotency_key,
                IdempotencyState.FAILED,
                "failed",
                response,
                prepared,
            )
        except Exception:
            LOGGER.warning("Pre-write failure persistence failed; details were suppressed.")
        return response

    def _finish_after_attempt(
        self,
        binding: ConnectionBinding,
        definition: ToolDefinition,
        module: str,
        snapshot: CapabilitySnapshot,
        command: WriteCommand,
        request_id: str,
        idempotency_key: str,
        state: IdempotencyState,
        final_status: str,
        response: WriteSafetyResponse,
        prepared: PreparedWrite | None,
    ) -> None:
        self._storage.execution.finish(
            self._event(
                binding,
                definition,
                module,
                snapshot,
                command,
                request_id,
                final_status=final_status,
                response=response,
                proposed_action=None if prepared is None else prepared.proposed_action,
            ),
            idempotency_key,
            state,
            response.model_dump(mode="json"),
        )

    def _append_or_fail(
        self,
        event: AuditEvent,
        response: WriteSafetyResponse,
    ) -> WriteSafetyResponse:
        try:
            self._storage.audit.append(event)
        except Exception:
            LOGGER.warning("Write audit persistence failed; details were suppressed.")
            return self._failure(
                self._unexpected_pre_write_error(),
                response.request_id,
                response.company_id,
                outcome="not_attempted",
            )
        return response

    @staticmethod
    def _failure(
        error: OdooMcpError,
        request_id: str,
        company_id: int,
        *,
        outcome: WriteOutcome,
        prepared: PreparedWrite | None = None,
    ) -> WriteSafetyResponse:
        return WriteSafetyResponse(
            status="failed",
            outcome=outcome,
            request_id=request_id,
            company_id=company_id,
            proposed_action=(None if prepared is None else dict(prepared.proposed_action)),
            material_effects=({} if prepared is None else dict(prepared.material_effects)),
            artifact_markdown=None if prepared is None else prepared.artifact_markdown,
            error_code=error.code,
            error_message=error.safe_message,
            remediation_hint=error.remediation_hint,
        )

    @staticmethod
    def _unexpected_pre_write_error() -> OdooMcpError:
        return OdooMcpError(
            ErrorCode.UNKNOWN_ERROR,
            "The write request failed before Odoo was changed.",
            "Correct the request or contact the service operator before retrying.",
        )

    @staticmethod
    def _event(
        binding: ConnectionBinding,
        definition: ToolDefinition,
        module: str,
        snapshot: CapabilitySnapshot,
        command: WriteCommand,
        request_id: str,
        *,
        final_status: str,
        response: WriteSafetyResponse | None = None,
        proposed_action: Mapping[str, object] | None = None,
        affected_records: tuple[str, ...] = (),
    ) -> AuditEvent:
        error = response if response is not None and response.status == "failed" else None
        return AuditEvent(
            request_id=request_id,
            tenant_id=binding.tenant_id,
            company_id=command.company_id if command.company_id > 0 else None,
            tool_name=definition.name,
            tool_version=definition.version,
            module=module,
            authenticated_subject=binding.authenticated_subject,
            mcp_client=binding.mcp_client,
            odoo_db_name=binding.connection.database,
            odoo_user=binding.connection.username,
            odoo_version=str(snapshot.version),
            odoo_transport=snapshot.transport,
            input_payload={
                "company_id": command.company_id,
                "dry_run": command.dry_run,
                "idempotency_key": command.idempotency_key,
                **command.request_payload,
            },
            dry_run=command.dry_run,
            proposed_action=proposed_action,
            actual_result=None if response is None else response.model_dump(mode="json"),
            affected_odoo_records=affected_records,
            error_code=None
            if error is None or error.error_code is None
            else error.error_code.value,
            error_message=None if error is None else error.error_message,
            final_status=final_status,
        )
