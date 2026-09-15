"""Typed records for server-owned durable state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ProposalState(StrEnum):
    PROPOSED = "proposed"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    EXPIRED = "expired"


class IdempotencyState(StrEnum):
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class IdempotencyDisposition(StrEnum):
    RESERVED = "reserved"
    IN_PROGRESS = "in_progress"
    REPLAY = "replay"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    request_id: str
    tenant_id: str
    company_id: int | None
    tool_name: str
    tool_version: str
    module: str
    authenticated_subject: str
    mcp_client: str
    odoo_db_name: str | None
    odoo_user: str | None
    odoo_version: str | None
    odoo_transport: str | None
    input_payload: Mapping[str, object]
    dry_run: bool
    proposed_action: Mapping[str, object] | None
    actual_result: Mapping[str, object] | None
    affected_odoo_records: Sequence[str]
    error_code: str | None
    error_message: str | None
    final_status: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AuditRecord(AuditEvent):
    id: int = 0
    previous_hash: str = ""
    entry_hash: str = ""


@dataclass(frozen=True, slots=True)
class AuditVerification:
    tenant_id: str
    entry_count: int
    last_hash: str


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    id: int
    request_id: str
    tenant_id: str
    company_id: int
    tool_name: str
    module: str
    proposal_type: str
    payload: Mapping[str, object]
    status: ProposalState
    executed_at: datetime | None
    erp_record_refs: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: int
    request_id: str
    tenant_id: str
    company_id: int
    tool_name: str
    module: str
    artifact_type: str
    artifact_format: str
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IdempotencyDecision:
    disposition: IdempotencyDisposition
    state: IdempotencyState
    owner_request_id: str
    response: Mapping[str, object] | None
    expires_at: datetime
