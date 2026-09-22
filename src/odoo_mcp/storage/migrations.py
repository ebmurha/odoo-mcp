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

IDEMPOTENCY_RESPONSE_INVARIANT = Migration(
    "0002_idempotency_response_invariant",
    (
        """
        CREATE TABLE _migration_0002_idempotency_validation (
            valid INTEGER NOT NULL CHECK (valid = 1)
        )
        """,
        """
        INSERT INTO _migration_0002_idempotency_validation (valid)
        SELECT CASE WHEN EXISTS (
            SELECT 1 FROM idempotency_keys
            WHERE (state = 'in_progress' AND response_json IS NOT NULL)
               OR (state != 'in_progress' AND response_json IS NULL)
        ) THEN 0 ELSE 1 END
        """,
        "DROP TABLE _migration_0002_idempotency_validation",
        """
        CREATE TRIGGER idempotency_response_invariant_insert
        BEFORE INSERT ON idempotency_keys
        WHEN (NEW.state = 'in_progress' AND NEW.response_json IS NOT NULL)
          OR (NEW.state != 'in_progress' AND NEW.response_json IS NULL)
        BEGIN
            SELECT RAISE(ABORT, 'idempotency response invariant failed');
        END
        """,
        """
        CREATE TRIGGER idempotency_response_invariant_update
        BEFORE UPDATE OF state, response_json ON idempotency_keys
        WHEN (NEW.state = 'in_progress' AND NEW.response_json IS NOT NULL)
          OR (NEW.state != 'in_progress' AND NEW.response_json IS NULL)
        BEGIN
            SELECT RAISE(ABORT, 'idempotency response invariant failed');
        END
        """,
    ),
)

SHARED_HOSTED_AUTHORITY = Migration(
    "0003_shared_hosted_authority",
    (
        "ALTER TABLE erp_connections RENAME TO _erp_connections_legacy",
        """
        CREATE TABLE erp_connections (
            id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            connection_label TEXT NOT NULL,
            odoo_url TEXT NOT NULL,
            database_name TEXT NOT NULL,
            username TEXT NOT NULL,
            encrypted_api_key TEXT NOT NULL,
            discovered_company_ids_json TEXT NOT NULL,
            allowed_company_ids_json TEXT,
            default_company_id INTEGER CHECK (default_company_id > 0),
            detected_version TEXT,
            selected_transport TEXT,
            key_version INTEGER NOT NULL CHECK (key_version > 0),
            status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'revoked', 'expired')),
            enrollment_handle_hash TEXT UNIQUE,
            consumed_enrollment_handle_hash TEXT UNIQUE,
            enrollment_expires_at TEXT,
            activated_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, id),
            CHECK (
                (status = 'active' AND allowed_company_ids_json IS NOT NULL
                    AND default_company_id IS NOT NULL AND enrollment_handle_hash IS NULL)
                OR status != 'active'
            )
        )
        """,
        """
        INSERT INTO erp_connections (
            id, tenant_id, connection_label, odoo_url, database_name, username,
            encrypted_api_key, discovered_company_ids_json, allowed_company_ids_json,
            default_company_id, detected_version, selected_transport, key_version,
            status, enrollment_handle_hash, consumed_enrollment_handle_hash,
            enrollment_expires_at, activated_at,
            revoked_at, created_at, updated_at
        )
        SELECT id, tenant_id, connection_label, odoo_url, database_name, username,
            encrypted_api_key, allowed_company_ids_json, allowed_company_ids_json,
            default_company_id, detected_version, selected_transport, key_version,
            'active', NULL, NULL, NULL, created_at, NULL, created_at, updated_at
        FROM _erp_connections_legacy
        """,
        "DROP TABLE _erp_connections_legacy",
        "CREATE INDEX erp_connections_status ON erp_connections (status, enrollment_expires_at)",
        """
        CREATE TABLE oauth_clients (
            client_id TEXT PRIMARY KEY,
            registration_method TEXT NOT NULL CHECK (registration_method IN ('dcr', 'cimd')),
            metadata_json TEXT NOT NULL,
            client_secret_hash TEXT,
            metadata_expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE oauth_authorization_sessions (
            state_hash TEXT PRIMARY KEY,
            client_id TEXT NOT NULL REFERENCES oauth_clients(client_id),
            connector_id TEXT,
            superseded_connector_id TEXT,
            redirect_uri TEXT NOT NULL,
            resource TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            code_challenge TEXT NOT NULL,
            client_state TEXT,
            authorization_code_hash TEXT UNIQUE,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'authorized', 'consumed', 'expired', 'denied')
            ),
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE oauth_grants (
            id TEXT PRIMARY KEY,
            connector_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            client_id TEXT NOT NULL REFERENCES oauth_clients(client_id),
            resource TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            FOREIGN KEY (tenant_id, connector_id) REFERENCES erp_connections(tenant_id, id)
        )
        """,
        """
        CREATE TABLE oauth_tokens (
            token_hash TEXT PRIMARY KEY,
            grant_id TEXT NOT NULL REFERENCES oauth_grants(id),
            token_type TEXT NOT NULL CHECK (token_type IN ('access', 'refresh')),
            expires_at TEXT NOT NULL,
            rotated_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX oauth_tokens_grant ON oauth_tokens (grant_id, token_type)",
        """
        CREATE TRIGGER erp_connection_legal_transition
        BEFORE UPDATE OF status ON erp_connections
        WHEN OLD.status != NEW.status AND NOT (
            (OLD.status = 'pending' AND NEW.status IN ('active', 'expired', 'revoked'))
            OR (OLD.status = 'active' AND NEW.status = 'revoked')
        )
        BEGIN
            SELECT RAISE(ABORT, 'illegal connector status transition');
        END
        """,
        """
        CREATE TRIGGER erp_connection_active_authority
        BEFORE UPDATE OF status ON erp_connections
        WHEN NEW.status = 'active' AND OLD.status != 'active' AND (
            NEW.allowed_company_ids_json IS NULL
            OR NEW.default_company_id IS NULL
            OR NEW.enrollment_handle_hash IS NOT NULL
            OR NOT EXISTS (
                SELECT 1 FROM oauth_grants
                WHERE connector_id = NEW.id AND tenant_id = NEW.tenant_id
                    AND status = 'active'
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'active connector authority is incomplete');
        END
        """,
        """
        CREATE TRIGGER oauth_session_legal_transition
        BEFORE UPDATE OF status ON oauth_authorization_sessions
        WHEN OLD.status != NEW.status AND NOT (
            (OLD.status = 'pending' AND NEW.status IN ('authorized', 'expired', 'denied'))
            OR (OLD.status = 'authorized' AND NEW.status = 'consumed')
        )
        BEGIN
            SELECT RAISE(ABORT, 'illegal authorization-session transition');
        END
        """,
        """
        CREATE TRIGGER oauth_grant_legal_transition
        BEFORE UPDATE OF status ON oauth_grants
        WHEN OLD.status != NEW.status
            AND NOT (OLD.status = 'active' AND NEW.status = 'revoked')
        BEGIN
            SELECT RAISE(ABORT, 'illegal grant status transition');
        END
        """,
    ),
)

MIGRATIONS = (INITIAL_SCHEMA, IDEMPOTENCY_RESPONSE_INVARIANT, SHARED_HOSTED_AUTHORITY)


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
