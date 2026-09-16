"""Tenant-scoped repositories for server-owned workflow state."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from datetime import timedelta
from typing import ClassVar

from odoo_mcp.storage.database import SQLiteDatabase
from odoo_mcp.storage.errors import (
    IdempotencyPayloadMismatch,
    IdempotencyTransitionError,
    ProposalTransitionError,
)
from odoo_mcp.storage.json_support import (
    canonical_json,
    parse_mapping,
    parse_string_tuple,
    parse_timestamp,
    timestamp,
    utc_now,
)
from odoo_mcp.storage.models import (
    ArtifactRecord,
    IdempotencyDecision,
    IdempotencyDisposition,
    IdempotencyState,
    ProposalRecord,
    ProposalState,
)


def _required(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} is required")


def _company(company_id: int) -> None:
    if company_id <= 0:
        raise ValueError("company_id must be positive")


def _proposal(row: sqlite3.Row) -> ProposalRecord:
    return ProposalRecord(
        id=int(row["id"]),
        request_id=str(row["request_id"]),
        tenant_id=str(row["tenant_id"]),
        company_id=int(row["company_id"]),
        tool_name=str(row["tool_name"]),
        module=str(row["module"]),
        proposal_type=str(row["proposal_type"]),
        payload=parse_mapping(str(row["payload_json"])),
        status=ProposalState(str(row["status"])),
        executed_at=(
            None if row["executed_at"] is None else parse_timestamp(str(row["executed_at"]))
        ),
        erp_record_refs=parse_string_tuple(str(row["erp_record_refs"])),
        created_at=parse_timestamp(str(row["created_at"])),
    )


class ProposalRepository:
    _TRANSITIONS: ClassVar[dict[ProposalState, frozenset[ProposalState]]] = {
        ProposalState.PROPOSED: frozenset({ProposalState.EXECUTING, ProposalState.EXPIRED}),
        ProposalState.EXECUTING: frozenset(
            {
                ProposalState.EXECUTED,
                ProposalState.FAILED,
                ProposalState.UNKNOWN,
            }
        ),
    }

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def create(
        self,
        request_id: str,
        tenant_id: str,
        company_id: int,
        tool_name: str,
        module: str,
        proposal_type: str,
        payload: Mapping[str, object],
    ) -> ProposalRecord:
        _required(request_id, "request_id")
        _required(tenant_id, "tenant_id")
        _company(company_id)
        _required(tool_name, "tool_name")
        _required(module, "module")
        _required(proposal_type, "proposal_type")
        created_at = timestamp()
        with self._database.transaction(write=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO proposals (
                    request_id, tenant_id, company_id, tool_name, module,
                    proposal_type, payload_json, status, executed_at,
                    erp_record_refs, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', NULL, '[]', ?)
                """,
                (
                    request_id,
                    tenant_id,
                    company_id,
                    tool_name,
                    module,
                    proposal_type,
                    canonical_json(payload),
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM proposals WHERE id = ? AND tenant_id = ?",
                (cursor.lastrowid, tenant_id),
            ).fetchone()
        if row is None:
            raise ProposalTransitionError("Proposal creation failed")
        return _proposal(row)

    def get(self, tenant_id: str, proposal_id: int) -> ProposalRecord | None:
        _required(tenant_id, "tenant_id")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE tenant_id = ? AND id = ?",
                (tenant_id, proposal_id),
            ).fetchone()
        return None if row is None else _proposal(row)

    def transition(
        self,
        tenant_id: str,
        proposal_id: int,
        expected: ProposalState,
        target: ProposalState,
        erp_record_refs: tuple[str, ...] = (),
    ) -> ProposalRecord:
        _required(tenant_id, "tenant_id")
        if target not in self._TRANSITIONS.get(expected, set()):
            raise ProposalTransitionError("The proposal state transition is not allowed")
        executed_at = timestamp() if target is ProposalState.EXECUTED else None
        with self._database.transaction(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE proposals
                SET status = ?, executed_at = ?, erp_record_refs = ?
                WHERE tenant_id = ? AND id = ? AND status = ?
                """,
                (
                    target.value,
                    executed_at,
                    canonical_json(erp_record_refs),
                    tenant_id,
                    proposal_id,
                    expected.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ProposalTransitionError("The proposal state changed concurrently")
            row = connection.execute(
                "SELECT * FROM proposals WHERE tenant_id = ? AND id = ?",
                (tenant_id, proposal_id),
            ).fetchone()
        if row is None:
            raise ProposalTransitionError("The proposal could not be resolved")
        return _proposal(row)


class ArtifactRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def create(
        self,
        request_id: str,
        tenant_id: str,
        company_id: int,
        tool_name: str,
        module: str,
        artifact_type: str,
        artifact_format: str,
        content: str,
    ) -> ArtifactRecord:
        _required(request_id, "request_id")
        _required(tenant_id, "tenant_id")
        _company(company_id)
        _required(tool_name, "tool_name")
        _required(module, "module")
        _required(artifact_type, "artifact_type")
        _required(artifact_format, "artifact_format")
        created_at = timestamp()
        with self._database.transaction(write=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO artifacts (
                    request_id, tenant_id, company_id, tool_name, module,
                    artifact_type, artifact_format, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    tenant_id,
                    company_id,
                    tool_name,
                    module,
                    artifact_type,
                    artifact_format,
                    content,
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM artifacts WHERE tenant_id = ? AND id = ?",
                (tenant_id, cursor.lastrowid),
            ).fetchone()
        if row is None:
            raise RuntimeError("Artifact creation failed")
        return self._from_row(row)

    def get(self, tenant_id: str, artifact_id: int) -> ArtifactRecord | None:
        _required(tenant_id, "tenant_id")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE tenant_id = ? AND id = ?",
                (tenant_id, artifact_id),
            ).fetchone()
        return None if row is None else self._from_row(row)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            id=int(row["id"]),
            request_id=str(row["request_id"]),
            tenant_id=str(row["tenant_id"]),
            company_id=int(row["company_id"]),
            tool_name=str(row["tool_name"]),
            module=str(row["module"]),
            artifact_type=str(row["artifact_type"]),
            artifact_format=str(row["artifact_format"]),
            content=str(row["content"]),
            created_at=parse_timestamp(str(row["created_at"])),
        )


class CapabilityCacheRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def put(
        self,
        tenant_id: str,
        connection_id: str,
        odoo_version: str,
        capabilities: Mapping[str, bool],
    ) -> None:
        _required(tenant_id, "tenant_id")
        _required(connection_id, "connection_id")
        _required(odoo_version, "odoo_version")
        if not all(isinstance(value, bool) for value in capabilities.values()):
            raise ValueError("Capability values must be booleans")
        with self._database.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO capabilities_cache (
                    tenant_id, connection_id, odoo_version, capabilities_json, probed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, connection_id, odoo_version) DO UPDATE SET
                    capabilities_json = excluded.capabilities_json,
                    probed_at = excluded.probed_at
                """,
                (
                    tenant_id,
                    connection_id,
                    odoo_version,
                    canonical_json(capabilities),
                    timestamp(),
                ),
            )

    def get(self, tenant_id: str, connection_id: str, odoo_version: str) -> dict[str, bool] | None:
        _required(tenant_id, "tenant_id")
        _required(connection_id, "connection_id")
        _required(odoo_version, "odoo_version")
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT capabilities_json FROM capabilities_cache
                WHERE tenant_id = ? AND connection_id = ? AND odoo_version = ?
                """,
                (tenant_id, connection_id, odoo_version),
            ).fetchone()
        if row is None:
            return None
        parsed = parse_mapping(str(row["capabilities_json"]))
        if not all(isinstance(value, bool) for value in parsed.values()):
            raise ValueError("Persisted capability state is invalid")
        return {key: bool(value) for key, value in parsed.items()}


class IdempotencyRepository:
    _TTL = timedelta(hours=24)

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    @staticmethod
    def _request_hash(company_id: int, request_payload: Mapping[str, object]) -> str:
        bound = {"company_id": company_id, "payload": request_payload}
        return hashlib.sha256(canonical_json(bound).encode()).hexdigest()

    def reserve(
        self,
        tenant_id: str,
        company_id: int,
        tool_name: str,
        idempotency_key: str,
        request_payload: Mapping[str, object],
        owner_request_id: str,
    ) -> IdempotencyDecision:
        with self._database.transaction(write=True) as connection:
            return self.reserve_in_transaction(
                connection,
                tenant_id,
                company_id,
                tool_name,
                idempotency_key,
                request_payload,
                owner_request_id,
            )

    def reserve_in_transaction(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        company_id: int,
        tool_name: str,
        idempotency_key: str,
        request_payload: Mapping[str, object],
        owner_request_id: str,
    ) -> IdempotencyDecision:
        _required(tenant_id, "tenant_id")
        _company(company_id)
        _required(tool_name, "tool_name")
        _required(idempotency_key, "idempotency_key")
        _required(owner_request_id, "owner_request_id")
        now = utc_now()
        now_text = timestamp(now)
        expires_at = now + self._TTL
        request_hash = self._request_hash(company_id, request_payload)
        row = connection.execute(
            """
            SELECT * FROM idempotency_keys
            WHERE tenant_id = ? AND tool_name = ? AND idempotency_key = ?
            """,
            (tenant_id, tool_name, idempotency_key),
        ).fetchone()
        existing_state: IdempotencyState | None = None
        existing_response: dict[str, object] | None = None
        if row is not None:
            existing_state = IdempotencyState(str(row["state"]))
            existing_response = (
                None if row["response_json"] is None else parse_mapping(str(row["response_json"]))
            )
            if (existing_state is IdempotencyState.IN_PROGRESS) != (existing_response is None):
                raise IdempotencyTransitionError("Stored idempotency replay state is invalid")
        if (
            row is not None
            and parse_timestamp(str(row["expires_at"])) <= now
            and existing_state in {IdempotencyState.SUCCEEDED, IdempotencyState.FAILED}
        ):
            connection.execute("DELETE FROM idempotency_keys WHERE id = ?", (row["id"],))
            row = None
        if row is None:
            connection.execute(
                """
                INSERT INTO idempotency_keys (
                    tenant_id, company_id, tool_name, idempotency_key,
                    request_hash, state, owner_request_id, response_json,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'in_progress', ?, NULL, ?, ?, ?)
                """,
                (
                    tenant_id,
                    company_id,
                    tool_name,
                    idempotency_key,
                    request_hash,
                    owner_request_id,
                    timestamp(expires_at),
                    now_text,
                    now_text,
                ),
            )
            return IdempotencyDecision(
                disposition=IdempotencyDisposition.RESERVED,
                state=IdempotencyState.IN_PROGRESS,
                owner_request_id=owner_request_id,
                response=None,
                expires_at=expires_at,
            )
        if str(row["request_hash"]) != request_hash:
            raise IdempotencyPayloadMismatch(
                "The idempotency key was already used for a different request"
            )
        if existing_state is None:
            raise IdempotencyTransitionError("Stored idempotency replay state is invalid")
        return IdempotencyDecision(
            disposition=(
                IdempotencyDisposition.IN_PROGRESS
                if existing_state is IdempotencyState.IN_PROGRESS
                else IdempotencyDisposition.REPLAY
            ),
            state=existing_state,
            owner_request_id=str(row["owner_request_id"]),
            response=existing_response,
            expires_at=parse_timestamp(str(row["expires_at"])),
        )

    def finish(
        self,
        tenant_id: str,
        company_id: int,
        tool_name: str,
        idempotency_key: str,
        owner_request_id: str,
        state: IdempotencyState = IdempotencyState.SUCCEEDED,
        response: Mapping[str, object] | None = None,
    ) -> None:
        with self._database.transaction(write=True) as connection:
            self.finish_in_transaction(
                connection,
                tenant_id,
                company_id,
                tool_name,
                idempotency_key,
                owner_request_id,
                state,
                response,
            )

    def finish_in_transaction(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        company_id: int,
        tool_name: str,
        idempotency_key: str,
        owner_request_id: str,
        state: IdempotencyState = IdempotencyState.SUCCEEDED,
        response: Mapping[str, object] | None = None,
    ) -> None:
        _required(tenant_id, "tenant_id")
        _company(company_id)
        _required(tool_name, "tool_name")
        _required(idempotency_key, "idempotency_key")
        _required(owner_request_id, "owner_request_id")
        if state is IdempotencyState.IN_PROGRESS:
            raise IdempotencyTransitionError("An idempotency outcome must be final")
        if response is None:
            raise IdempotencyTransitionError(
                "A final idempotency state requires a replayable response"
            )
        cursor = connection.execute(
            """
            UPDATE idempotency_keys
            SET state = ?, response_json = ?, updated_at = ?
            WHERE tenant_id = ? AND tool_name = ? AND idempotency_key = ?
              AND company_id = ? AND owner_request_id = ? AND state = 'in_progress'
            """,
            (
                state.value,
                canonical_json(response),
                timestamp(),
                tenant_id,
                tool_name,
                idempotency_key,
                company_id,
                owner_request_id,
            ),
        )
        if cursor.rowcount != 1:
            raise IdempotencyTransitionError(
                "The idempotency reservation is not owned or is already final"
            )
