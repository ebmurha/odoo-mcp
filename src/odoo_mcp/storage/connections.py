"""Authenticated encryption and Shared Hosted ERP connection persistence."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from typing import cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from odoo_mcp.adapters.odoo.connections import ConnectorAuthorization
from odoo_mcp.app.settings import OdooConnectionSettings
from odoo_mcp.mcp.request_ids import new_request_id
from odoo_mcp.storage.audit import AuditRepository
from odoo_mcp.storage.database import SQLiteDatabase
from odoo_mcp.storage.errors import ConnectionDecryptionError
from odoo_mcp.storage.json_support import canonical_json, timestamp
from odoo_mcp.storage.models import AuditEvent


class EncryptionKeyring:
    """In-memory keyring populated by an operator-controlled secret source."""

    def __init__(self, active_version: int, keys: dict[int, bytes]) -> None:
        if active_version <= 0 or active_version not in keys:
            raise ValueError("The active encryption key version is unavailable")
        if any(version <= 0 or len(key) != 32 for version, key in keys.items()):
            raise ValueError("Encryption keys must be versioned 256-bit values")
        self._active_version = active_version
        self._keys = dict(keys)

    @property
    def active_version(self) -> int:
        return self._active_version

    @staticmethod
    def _associated_data(tenant_id: str, connection_id: str, key_version: int) -> bytes:
        return canonical_json(
            {
                "connection_id": connection_id,
                "key_version": key_version,
                "tenant_id": tenant_id,
            }
        ).encode()

    def encrypt(self, tenant_id: str, connection_id: str, plaintext: str) -> tuple[str, int]:
        nonce = os.urandom(12)
        associated_data = self._associated_data(tenant_id, connection_id, self._active_version)
        ciphertext = AESGCM(self._keys[self._active_version]).encrypt(
            nonce, plaintext.encode(), associated_data
        )
        return base64.urlsafe_b64encode(nonce + ciphertext).decode(), self._active_version

    def decrypt(
        self,
        tenant_id: str,
        connection_id: str,
        ciphertext: str,
        key_version: int,
    ) -> str:
        try:
            key = self._keys[key_version]
            combined = base64.b64decode(ciphertext.encode(), altchars=b"-_", validate=True)
            if len(combined) < 29:
                raise ValueError
            plaintext = AESGCM(key).decrypt(
                combined[:12],
                combined[12:],
                self._associated_data(tenant_id, connection_id, key_version),
            )
            return plaintext.decode()
        except (KeyError, ValueError, UnicodeDecodeError, InvalidTag) as exc:
            raise ConnectionDecryptionError(
                "The encrypted Odoo connection could not be decrypted"
            ) from exc


class SQLiteEncryptedConnectionRepository:
    """Tenant-bound connection storage implementing the resolver protocol."""

    def __init__(
        self,
        database: SQLiteDatabase,
        audit: AuditRepository,
        keyring: EncryptionKeyring | None,
    ) -> None:
        self._database = database
        self._audit = audit
        self._keyring = keyring

    def _require_keyring(self) -> EncryptionKeyring:
        if self._keyring is None:
            raise ConnectionDecryptionError("Connection encryption is not configured")
        return self._keyring

    def save(
        self,
        tenant_id: str,
        connection_id: str,
        connection_label: str,
        connection: OdooConnectionSettings,
        detected_version: str | None = None,
        selected_transport: str | None = None,
    ) -> None:
        if not tenant_id.strip() or not connection_id.strip() or not connection_label.strip():
            raise ValueError("Connection identifiers and label are required")
        keyring = self._require_keyring()
        encrypted, key_version = keyring.encrypt(
            tenant_id, connection_id, connection.api_key.get_secret_value()
        )
        now = timestamp()
        with self._database.transaction(write=True) as database_connection:
            database_connection.execute(
                """
                INSERT INTO erp_connections (
                    id, tenant_id, connection_label, odoo_url, database_name,
                    username, encrypted_api_key, allowed_company_ids_json,
                    default_company_id, detected_version, selected_transport,
                    key_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, id) DO UPDATE SET
                    connection_label = excluded.connection_label,
                    odoo_url = excluded.odoo_url,
                    database_name = excluded.database_name,
                    username = excluded.username,
                    encrypted_api_key = excluded.encrypted_api_key,
                    allowed_company_ids_json = excluded.allowed_company_ids_json,
                    default_company_id = excluded.default_company_id,
                    detected_version = excluded.detected_version,
                    selected_transport = excluded.selected_transport,
                    key_version = excluded.key_version,
                    updated_at = excluded.updated_at
                """,
                (
                    connection_id,
                    tenant_id,
                    connection_label,
                    str(connection.url),
                    connection.database,
                    connection.username,
                    encrypted,
                    canonical_json(connection.allowed_company_ids),
                    connection.default_company_id,
                    detected_version,
                    selected_transport,
                    key_version,
                    now,
                    now,
                ),
            )

    @staticmethod
    def _allowed_companies(value: str) -> tuple[int, ...]:
        parsed = json.loads(value)
        if (
            not isinstance(parsed, list)
            or not parsed
            or not all(isinstance(item, int) and not isinstance(item, bool) for item in parsed)
        ):
            raise ConnectionDecryptionError("The encrypted Odoo connection is invalid")
        return tuple(cast(list[int], parsed))

    def _resolve_row(self, row: sqlite3.Row) -> OdooConnectionSettings:
        tenant_id = str(row["tenant_id"])
        connection_id = str(row["id"])
        api_key = self._require_keyring().decrypt(
            tenant_id,
            connection_id,
            str(row["encrypted_api_key"]),
            int(row["key_version"]),
        )
        try:
            return OdooConnectionSettings.model_validate(
                {
                    "url": str(row["odoo_url"]),
                    "database": str(row["database_name"]),
                    "username": str(row["username"]),
                    "api_key": api_key,
                    "allowed_company_ids": self._allowed_companies(
                        str(row["allowed_company_ids_json"])
                    ),
                    "default_company_id": int(row["default_company_id"]),
                }
            )
        except (TypeError, ValueError) as exc:
            raise ConnectionDecryptionError("The encrypted Odoo connection is invalid") from exc

    async def resolve_authorized(
        self, authorization: ConnectorAuthorization
    ) -> OdooConnectionSettings | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM erp_connections WHERE tenant_id = ? AND id = ?",
                (authorization.tenant_id, authorization.connection_id),
            ).fetchone()
        return None if row is None else self._resolve_row(row)

    def rotate(
        self,
        tenant_id: str,
        connection_id: str,
        request_id: str,
        authenticated_subject: str,
        mcp_client: str,
    ) -> bool:
        if not all(
            value.strip()
            for value in (
                tenant_id,
                connection_id,
                request_id,
                authenticated_subject,
                mcp_client,
            )
        ):
            raise ValueError("Rotation identity fields are required")
        keyring = self._require_keyring()
        with self._database.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM erp_connections WHERE tenant_id = ? AND id = ?",
                (tenant_id, connection_id),
            ).fetchone()
            if row is None:
                raise ConnectionDecryptionError("The encrypted Odoo connection was not found")
            old_version = int(row["key_version"])
            if old_version == keyring.active_version:
                return False
            plaintext = keyring.decrypt(
                tenant_id,
                connection_id,
                str(row["encrypted_api_key"]),
                old_version,
            )
            encrypted, new_version = keyring.encrypt(tenant_id, connection_id, plaintext)
            cursor = connection.execute(
                """
                UPDATE erp_connections
                SET encrypted_api_key = ?, key_version = ?, updated_at = ?
                WHERE tenant_id = ? AND id = ? AND key_version = ?
                """,
                (
                    encrypted,
                    new_version,
                    timestamp(),
                    tenant_id,
                    connection_id,
                    old_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConnectionDecryptionError("The encryption key changed concurrently")
            self._audit.append_in_transaction(
                connection,
                AuditEvent(
                    request_id=request_id,
                    tenant_id=tenant_id,
                    company_id=None,
                    tool_name="rotate_erp_connection_key",
                    tool_version="1.0",
                    module="core",
                    authenticated_subject=authenticated_subject,
                    mcp_client=mcp_client,
                    odoo_db_name=str(row["database_name"]),
                    odoo_user=str(row["username"]),
                    odoo_version=(
                        None if row["detected_version"] is None else str(row["detected_version"])
                    ),
                    odoo_transport=(
                        None
                        if row["selected_transport"] is None
                        else str(row["selected_transport"])
                    ),
                    input_payload={"connection_id": connection_id},
                    dry_run=False,
                    proposed_action={"key_version": new_version},
                    actual_result={
                        "connection_id": connection_id,
                        "key_version": new_version,
                    },
                    affected_odoo_records=(),
                    error_code=None,
                    error_message=None,
                    final_status="succeeded",
                ),
            )
        return True

    def verify_all(self) -> None:
        with self._database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM erp_connections ORDER BY tenant_id, id"
            ).fetchall()
        for row in rows:
            self._resolve_row(row)

    def rotate_tenant(
        self,
        tenant_id: str,
        authenticated_subject: str,
        mcp_client: str,
    ) -> int:
        """Rotate each stale record atomically; reruns skip completed records."""

        if not all(value.strip() for value in (tenant_id, authenticated_subject, mcp_client)):
            raise ValueError("Rotation identity fields are required")
        active_version = self._require_keyring().active_version
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id FROM erp_connections
                WHERE tenant_id = ? AND key_version != ?
                ORDER BY id
                """,
                (tenant_id, active_version),
            ).fetchall()
        rotated = 0
        for row in rows:
            if self.rotate(
                tenant_id,
                str(row["id"]),
                new_request_id(),
                authenticated_subject,
                mcp_client,
            ):
                rotated += 1
        return rotated
