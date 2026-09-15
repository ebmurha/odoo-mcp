"""Immutable, checksummed, ordered SQLite migrations."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from odoo_mcp.storage.database import SQLiteDatabase
from odoo_mcp.storage.errors import MigrationError


@dataclass(frozen=True, slots=True)
class Migration:
    identifier: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        payload = "\n-- statement --\n".join(self.statements).encode()
        return hashlib.sha256(payload).hexdigest()


INITIAL_SCHEMA = Migration(
    "0001_initial_storage",
    (
        """
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            company_id INTEGER,
            tool_name TEXT NOT NULL,
            tool_version TEXT NOT NULL,
            module TEXT NOT NULL,
            authenticated_subject TEXT NOT NULL,
            mcp_client TEXT NOT NULL,
            odoo_db_name TEXT,
            odoo_user TEXT,
            odoo_version TEXT,
            odoo_transport TEXT,
            input_payload TEXT NOT NULL,
            dry_run INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
            proposed_action TEXT,
            actual_result TEXT,
            affected_odoo_records TEXT NOT NULL,
            error_code TEXT,
            error_message TEXT,
            final_status TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (tenant_id, entry_hash)
        )
        """,
        "CREATE INDEX audit_log_tenant_order ON audit_log (tenant_id, id)",
        """
        CREATE TRIGGER audit_log_no_update
        BEFORE UPDATE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'audit_log is append-only');
        END
        """,
        """
        CREATE TRIGGER audit_log_no_delete
        BEFORE DELETE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'audit_log is append-only');
        END
        """,
        """
        CREATE TABLE proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            company_id INTEGER NOT NULL CHECK (company_id > 0),
            tool_name TEXT NOT NULL,
            module TEXT NOT NULL,
            proposal_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('proposed', 'executing', 'executed', 'failed', 'unknown', 'expired')
            ),
            executed_at TEXT,
            erp_record_refs TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX proposals_tenant_request ON proposals (tenant_id, request_id)",
        """
        CREATE TABLE artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            company_id INTEGER NOT NULL CHECK (company_id > 0),
            tool_name TEXT NOT NULL,
            module TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            artifact_format TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX artifacts_tenant_request ON artifacts (tenant_id, request_id)",
        """
        CREATE TABLE idempotency_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            company_id INTEGER NOT NULL CHECK (company_id > 0),
            tool_name TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('in_progress', 'succeeded', 'failed', 'unknown')),
            owner_request_id TEXT NOT NULL,
            response_json TEXT,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (tenant_id, tool_name, idempotency_key)
        )
        """,
        """
        CREATE TABLE capabilities_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            connection_id TEXT NOT NULL,
            odoo_version TEXT NOT NULL,
            capabilities_json TEXT NOT NULL,
            probed_at TEXT NOT NULL,
            UNIQUE (tenant_id, connection_id, odoo_version)
        )
        """,
        """
        CREATE TABLE erp_connections (
            id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            connection_label TEXT NOT NULL,
            odoo_url TEXT NOT NULL,
            database_name TEXT NOT NULL,
            username TEXT NOT NULL,
            encrypted_api_key TEXT NOT NULL,
            allowed_company_ids_json TEXT NOT NULL,
            default_company_id INTEGER NOT NULL CHECK (default_company_id > 0),
            detected_version TEXT,
            selected_transport TEXT,
            key_version INTEGER NOT NULL CHECK (key_version > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, id)
        )
        """,
    ),
)

MIGRATIONS = (INITIAL_SCHEMA,)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def apply_migrations(
    database: SQLiteDatabase,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> None:
    """Verify migration history and apply each missing migration atomically."""

    identifiers = [migration.identifier for migration in migrations]
    if len(set(identifiers)) != len(identifiers) or identifiers != sorted(identifiers):
        raise MigrationError("Migration identifiers must be unique and ordered")

    with database.transaction(write=True) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )

    with database.transaction() as connection:
        applied = {
            str(row["id"]): str(row["checksum"])
            for row in connection.execute("SELECT id, checksum FROM schema_migrations ORDER BY id")
        }
    if list(applied) != identifiers[: len(applied)]:
        raise MigrationError("Applied migration history is not a known ordered prefix")
    expected = {migration.identifier: migration.checksum for migration in migrations}
    if any(expected.get(identifier) != checksum for identifier, checksum in applied.items()):
        raise MigrationError("An applied migration does not match its immutable checksum")

    for migration in migrations[len(applied) :]:
        try:
            with database.transaction(write=True) as connection:
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (id, checksum, applied_at) VALUES (?, ?, ?)",
                    (migration.identifier, migration.checksum, _timestamp()),
                )
        except sqlite3.Error as exc:
            raise MigrationError(f"Migration {migration.identifier} failed") from exc
