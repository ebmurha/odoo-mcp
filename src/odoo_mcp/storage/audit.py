"""Append-only, tenant-scoped SHA-256 audit chains."""

from __future__ import annotations

import hashlib

from odoo_mcp.mcp.error_codes import ErrorCode
from odoo_mcp.storage.database import Connection, Database, Row
from odoo_mcp.storage.errors import AuditIntegrityError
from odoo_mcp.storage.json_support import (
    canonical_json,
    parse_mapping,
    parse_string_tuple,
    parse_timestamp,
    redact_sensitive,
    timestamp,
)
from odoo_mcp.storage.models import AuditEvent, AuditRecord, AuditVerification

GENESIS_HASH = "0" * 64


def _safe_error_fields(
    error_code: str | None,
    error_message: str | None,
) -> tuple[str | None, str | None]:
    if error_code is None and error_message is None:
        return None, None
    try:
        safe_code = ErrorCode(error_code) if error_code is not None else ErrorCode.UNKNOWN_ERROR
    except ValueError:
        safe_code = ErrorCode.UNKNOWN_ERROR
    return safe_code.value, f"Operation failed with {safe_code.value}."


def _hash_payload(values: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json(values).encode()).hexdigest()


def _event_values(
    event: AuditEvent,
    previous_hash: str,
    created_at: str,
    *,
    normalize_error: bool = True,
) -> dict[str, object]:
    error_code, error_message = (
        _safe_error_fields(event.error_code, event.error_message)
        if normalize_error
        else (event.error_code, event.error_message)
    )
    return {
        "request_id": event.request_id,
        "tenant_id": event.tenant_id,
        "company_id": event.company_id,
        "tool_name": event.tool_name,
        "tool_version": event.tool_version,
        "module": event.module,
        "authenticated_subject": event.authenticated_subject,
        "mcp_client": event.mcp_client,
        "odoo_db_name": event.odoo_db_name,
        "odoo_user": event.odoo_user,
        "odoo_version": event.odoo_version,
        "odoo_transport": event.odoo_transport,
        "input_payload": redact_sensitive(event.input_payload),
        "dry_run": event.dry_run,
        "proposed_action": redact_sensitive(event.proposed_action),
        "actual_result": redact_sensitive(event.actual_result),
        "affected_odoo_records": list(event.affected_odoo_records),
        "error_code": error_code,
        "error_message": error_message,
        "final_status": event.final_status,
        "previous_hash": previous_hash,
        "created_at": created_at,
    }


def _row_to_record(row: Row) -> AuditRecord:
    proposed = None if row["proposed_action"] is None else parse_mapping(row["proposed_action"])
    actual = None if row["actual_result"] is None else parse_mapping(row["actual_result"])
    return AuditRecord(
        request_id=str(row["request_id"]),
        tenant_id=str(row["tenant_id"]),
        company_id=None if row["company_id"] is None else int(row["company_id"]),
        tool_name=str(row["tool_name"]),
        tool_version=str(row["tool_version"]),
        module=str(row["module"]),
        authenticated_subject=str(row["authenticated_subject"]),
        mcp_client=str(row["mcp_client"]),
        odoo_db_name=None if row["odoo_db_name"] is None else str(row["odoo_db_name"]),
        odoo_user=None if row["odoo_user"] is None else str(row["odoo_user"]),
        odoo_version=None if row["odoo_version"] is None else str(row["odoo_version"]),
        odoo_transport=(None if row["odoo_transport"] is None else str(row["odoo_transport"])),
        input_payload=parse_mapping(str(row["input_payload"])),
        dry_run=bool(row["dry_run"]),
        proposed_action=proposed,
        actual_result=actual,
        affected_odoo_records=parse_string_tuple(str(row["affected_odoo_records"])),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        error_message=None if row["error_message"] is None else str(row["error_message"]),
        final_status=str(row["final_status"]),
        created_at=parse_timestamp(str(row["created_at"])),
        id=int(row["id"]),
        previous_hash=str(row["previous_hash"]),
        entry_hash=str(row["entry_hash"]),
    )


class AuditRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def append(self, event: AuditEvent) -> AuditRecord:
        with self._database.transaction(write=True) as connection:
            return self.append_in_transaction(connection, event)

    def append_in_transaction(
        self,
        connection: Connection,
        event: AuditEvent,
    ) -> AuditRecord:
        required_values = (
            event.request_id,
            event.tenant_id,
            event.tool_name,
            event.tool_version,
            event.module,
            event.authenticated_subject,
            event.mcp_client,
            event.final_status,
        )
        if any(not value.strip() for value in required_values):
            raise ValueError("Audit identity and status fields are required")
        if event.company_id is not None and event.company_id <= 0:
            raise ValueError("Audit company_id must be positive")
        self._database.lock(connection, f"audit:{event.tenant_id}")
        row = connection.execute(
            "SELECT entry_hash FROM audit_log WHERE tenant_id = ? ORDER BY id DESC LIMIT 1",
            (event.tenant_id,),
        ).fetchone()
        previous_hash = GENESIS_HASH if row is None else str(row["entry_hash"])
        created_at = timestamp(event.created_at)
        values = _event_values(event, previous_hash, created_at)
        entry_hash = _hash_payload(values)
        cursor = connection.execute(
            """
            INSERT INTO audit_log (
                request_id, tenant_id, company_id, tool_name, tool_version, module,
                authenticated_subject, mcp_client, odoo_db_name, odoo_user,
                odoo_version, odoo_transport, input_payload, dry_run,
                proposed_action, actual_result, affected_odoo_records, error_code,
                error_message, final_status, previous_hash, entry_hash, created_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                event.request_id,
                event.tenant_id,
                event.company_id,
                event.tool_name,
                event.tool_version,
                event.module,
                event.authenticated_subject,
                event.mcp_client,
                event.odoo_db_name,
                event.odoo_user,
                event.odoo_version,
                event.odoo_transport,
                canonical_json(values["input_payload"]),
                int(event.dry_run),
                None
                if values["proposed_action"] is None
                else canonical_json(values["proposed_action"]),
                None
                if values["actual_result"] is None
                else canonical_json(values["actual_result"]),
                canonical_json(values["affected_odoo_records"]),
                values["error_code"],
                values["error_message"],
                event.final_status,
                previous_hash,
                entry_hash,
                created_at,
            ),
        )
        record_id = cursor.lastrowid
        if record_id is None:
            raise AuditIntegrityError("Audit append failed")
        row = connection.execute("SELECT * FROM audit_log WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise AuditIntegrityError("Audit append failed")
        return _row_to_record(row)

    def list_for_tenant(self, tenant_id: str) -> list[AuditRecord]:
        if not tenant_id.strip():
            raise ValueError("Audit tenant identifier is required")
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_log WHERE tenant_id = ? ORDER BY id", (tenant_id,)
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def tenant_ids(self) -> tuple[str, ...]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT DISTINCT tenant_id FROM audit_log ORDER BY tenant_id"
            ).fetchall()
        return tuple(str(row["tenant_id"]) for row in rows)

    def verify_chain(self, tenant_id: str) -> AuditVerification:
        if not tenant_id.strip():
            raise ValueError("Audit tenant identifier is required")
        records = self.list_for_tenant(tenant_id)
        expected_previous = GENESIS_HASH
        for record in records:
            created_at = timestamp(record.created_at)
            values = _event_values(
                record,
                record.previous_hash,
                created_at,
                normalize_error=False,
            )
            if (
                record.previous_hash != expected_previous
                or _hash_payload(values) != record.entry_hash
            ):
                raise AuditIntegrityError("Audit chain verification failed")
            expected_previous = record.entry_hash
        return AuditVerification(
            tenant_id=tenant_id,
            entry_count=len(records),
            last_hash=expected_previous,
        )
