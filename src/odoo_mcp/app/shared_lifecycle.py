"""Transactional Shared Hosted connector enrollment and lifecycle."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError

from odoo_mcp.adapters.base import Company
from odoo_mcp.adapters.odoo.enrollment import VerifiedEnrollment
from odoo_mcp.app.settings import OdooConnectionSettings, OdooEnrollmentCredentials
from odoo_mcp.mcp.request_ids import new_request_id
from odoo_mcp.storage.audit import AuditRepository
from odoo_mcp.storage.connections import EncryptionKeyring
from odoo_mcp.storage.database import SQLiteDatabase
from odoo_mcp.storage.json_support import canonical_json, parse_timestamp
from odoo_mcp.storage.models import AuditEvent

SESSION_TTL = timedelta(minutes=10)


class EnrollmentValidator(Protocol):
    async def validate(self, credentials: OdooEnrollmentCredentials) -> VerifiedEnrollment: ...


@dataclass(frozen=True, slots=True)
class PreparedEnrollment:
    enrollment_handle: str
    connector_id: str
    tenant_id: str
    expires_at: datetime
    companies: tuple[Company, ...]


@dataclass(frozen=True, slots=True)
class CommittedEnrollment:
    connector_id: str
    tenant_id: str
    allowed_company_ids: tuple[int, ...]
    default_company_id: int


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SharedConnectorLifecycle:
    """Keep incomplete authority pending and make every transition transactional."""

    def __init__(
        self,
        database: SQLiteDatabase,
        audit: AuditRepository,
        keyring: EncryptionKeyring,
        validator: EnrollmentValidator,
        *,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._database = database
        self._audit = audit
        self._keyring = keyring
        self._validator = validator
        self._now = now

    async def prepare_enrollment(
        self,
        credentials: OdooEnrollmentCredentials,
        *,
        enrollment_handle: str | None = None,
    ) -> PreparedEnrollment:
        handle = enrollment_handle or secrets.token_urlsafe(32)
        if len(handle) < 22:
            raise ValueError("Enrollment handles must contain at least 128 bits of entropy")
        handle_hash = _hash(handle)
        verified = await self._validator.validate(credentials)
        with self._database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM erp_connections WHERE enrollment_handle_hash = ?",
                (handle_hash,),
            ).fetchone()
        if existing is not None:
            return self._restore_prepared(existing, credentials, handle, verified)

        connector_id = str(uuid4())
        tenant_id = f"connector:{connector_id}"
        encrypted, key_version = self._keyring.encrypt(
            tenant_id, connector_id, credentials.api_key.get_secret_value()
        )
        now = self._now()
        expires = now + SESSION_TTL
        company_ids = tuple(company.id for company in verified.companies)
        with self._database.transaction(write=True) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO erp_connections (
                        id, tenant_id, connection_label, odoo_url, database_name,
                        username, encrypted_api_key, discovered_company_ids_json,
                        allowed_company_ids_json, default_company_id, detected_version,
                        selected_transport, key_version, status, enrollment_handle_hash,
                        consumed_enrollment_handle_hash, enrollment_expires_at,
                        activated_at, revoked_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?,
                        'pending', ?, NULL, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        connector_id,
                        tenant_id,
                        "Odoo connection",
                        str(credentials.url),
                        credentials.database,
                        credentials.username,
                        encrypted,
                        canonical_json(company_ids),
                        str(verified.version),
                        verified.transport,
                        key_version,
                        handle_hash,
                        expires.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError:
                row = connection.execute(
                    "SELECT * FROM erp_connections WHERE enrollment_handle_hash = ?",
                    (handle_hash,),
                ).fetchone()
                if row is None:
                    raise
                return self._restore_prepared(row, credentials, handle, verified)
            self._append_audit(connection, tenant_id, "prepare_enrollment", connector_id)
        return PreparedEnrollment(handle, connector_id, tenant_id, expires, verified.companies)

    def _restore_prepared(
        self,
        row: sqlite3.Row,
        credentials: OdooEnrollmentCredentials,
        handle: str,
        verified: VerifiedEnrollment,
    ) -> PreparedEnrollment:
        if str(row["status"]) != "pending" or self._is_expired(row):
            raise ValueError("The enrollment handle is no longer active")
        plaintext = self._keyring.decrypt(
            str(row["tenant_id"]),
            str(row["id"]),
            str(row["encrypted_api_key"]),
            int(row["key_version"]),
        )
        if (
            str(row["odoo_url"]).rstrip("/") != str(credentials.url).rstrip("/")
            or str(row["database_name"]) != credentials.database
            or str(row["username"]) != credentials.username
            or not secrets.compare_digest(plaintext, credentials.api_key.get_secret_value())
            or str(row["detected_version"]) != str(verified.version)
            or str(row["selected_transport"]) != verified.transport
            or self._ids(row) != tuple(company.id for company in verified.companies)
        ):
            raise ValueError("The enrollment handle was already used for another request")
        return PreparedEnrollment(
            handle,
            str(row["id"]),
            str(row["tenant_id"]),
            parse_timestamp(str(row["enrollment_expires_at"])),
            verified.companies,
        )

    def commit_enrollment(
        self,
        enrollment_handle: str,
        allowed_company_ids: Sequence[int],
        default_company_id: int,
    ) -> CommittedEnrollment:
        handle_hash = _hash(enrollment_handle)
        selected = tuple(dict.fromkeys(allowed_company_ids))
        if (
            not selected
            or any(value <= 0 for value in selected)
            or default_company_id not in selected
        ):
            raise ValueError("A valid non-empty company selection is required")
        with self._database.transaction(write=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM erp_connections
                WHERE enrollment_handle_hash = ? OR consumed_enrollment_handle_hash = ?
                """,
                (handle_hash, handle_hash),
            ).fetchone()
            if row is None or str(row["status"]) != "pending" or self._is_expired(row):
                raise ValueError("The enrollment handle is invalid or expired")
            discovered = self._ids(row)
            if not set(selected).issubset(discovered):
                raise ValueError("The company selection was not discovered during enrollment")
            if row["consumed_enrollment_handle_hash"] is not None:
                prior = self._allowed_ids(row)
                if prior != selected or int(row["default_company_id"]) != default_company_id:
                    raise ValueError("The enrollment handle was already consumed")
            else:
                connection.execute(
                    """
                    UPDATE erp_connections
                    SET allowed_company_ids_json = ?, default_company_id = ?,
                        enrollment_handle_hash = NULL,
                        consumed_enrollment_handle_hash = ?, updated_at = ?
                    WHERE tenant_id = ? AND id = ? AND status = 'pending'
                    """,
                    (
                        canonical_json(selected),
                        default_company_id,
                        handle_hash,
                        self._now().isoformat(),
                        str(row["tenant_id"]),
                        str(row["id"]),
                    ),
                )
                self._append_audit(
                    connection, str(row["tenant_id"]), "commit_enrollment", str(row["id"])
                )
        return CommittedEnrollment(
            str(row["id"]), str(row["tenant_id"]), selected, default_company_id
        )

    def expire_pending(self) -> int:
        now = self._now().isoformat()
        with self._database.transaction(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE erp_connections
                SET status = 'expired', enrollment_handle_hash = NULL, updated_at = ?
                WHERE status = 'pending' AND enrollment_expires_at <= ?
                """,
                (now, now),
            )
            return cursor.rowcount

    def discard_pending(self, tenant_id: str, connector_id: str) -> None:
        """Remove a never-authorized connector after application binding fails."""

        with self._database.transaction(write=True) as connection:
            connection.execute(
                """
                DELETE FROM erp_connections
                WHERE tenant_id = ? AND id = ? AND status = 'pending'
                  AND NOT EXISTS (
                      SELECT 1 FROM oauth_grants
                      WHERE tenant_id = ? AND connector_id = ?
                  )
                """,
                (tenant_id, connector_id, tenant_id, connector_id),
            )

    def _is_expired(self, row: sqlite3.Row) -> bool:
        value = row["enrollment_expires_at"]
        return value is None or parse_timestamp(str(value)) <= self._now()

    @staticmethod
    def _ids(row: sqlite3.Row) -> tuple[int, ...]:
        import json

        values = json.loads(str(row["discovered_company_ids_json"]))
        if not isinstance(values, list) or not all(
            type(value) is int and value > 0 for value in values
        ):
            raise ValueError("Stored discovered companies are invalid")
        return tuple(values)

    @staticmethod
    def _allowed_ids(row: sqlite3.Row) -> tuple[int, ...]:
        import json

        values = json.loads(str(row["allowed_company_ids_json"]))
        if not isinstance(values, list) or not all(
            type(value) is int and value > 0 for value in values
        ):
            raise ValueError("Stored allowed companies are invalid")
        return tuple(values)

    def complete_settings(self, tenant_id: str, connector_id: str) -> OdooConnectionSettings:
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM erp_connections
                WHERE tenant_id = ? AND id = ? AND status = 'active'
                """,
                (tenant_id, connector_id),
            ).fetchone()
        if row is None:
            raise ValueError("The connector is not active")
        secret = self._keyring.decrypt(
            tenant_id,
            connector_id,
            str(row["encrypted_api_key"]),
            int(row["key_version"]),
        )
        try:
            return OdooConnectionSettings.model_validate(
                {
                    "url": str(row["odoo_url"]),
                    "database": str(row["database_name"]),
                    "username": str(row["username"]),
                    "api_key": secret,
                    "allowed_company_ids": self._allowed_ids(row),
                    "default_company_id": int(row["default_company_id"]),
                }
            )
        except (TypeError, ValueError, ValidationError):
            raise ValueError("The connector settings are incomplete") from None

    def _append_audit(
        self, connection: sqlite3.Connection, tenant_id: str, operation: str, connector_id: str
    ) -> None:
        self._audit.append_in_transaction(
            connection,
            AuditEvent(
                request_id=new_request_id(),
                tenant_id=tenant_id,
                company_id=None,
                tool_name=operation,
                tool_version="1.0",
                module="core",
                authenticated_subject=tenant_id,
                mcp_client="shared-hosted",
                odoo_db_name=None,
                odoo_user=None,
                odoo_version=None,
                odoo_transport=None,
                input_payload={"connector_id": connector_id},
                dry_run=False,
                proposed_action=None,
                actual_result={"status": "succeeded"},
                affected_odoo_records=(),
                error_code=None,
                error_message=None,
                final_status="succeeded",
            ),
        )
