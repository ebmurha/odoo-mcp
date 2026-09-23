"""Immutable PostgreSQL schema equivalent to the accepted SQLite state model."""

# SQL statements remain readable and reviewable as schema text.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from odoo_mcp.storage.database import DATABASE_ERRORS, PostgresDatabase
from odoo_mcp.storage.errors import MigrationError


@dataclass(frozen=True, slots=True)
class PostgresMigration:
    identifier: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        payload = "\n-- statement --\n".join(self.statements).encode()
        return hashlib.sha256(payload).hexdigest()


POSTGRES_BASELINE = PostgresMigration(
    "0001_postgresql_storage",
    (
        """
        CREATE TABLE audit_log (
            id BIGSERIAL PRIMARY KEY,
            request_id TEXT NOT NULL, tenant_id TEXT NOT NULL, company_id INTEGER,
            tool_name TEXT NOT NULL, tool_version TEXT NOT NULL, module TEXT NOT NULL,
            authenticated_subject TEXT NOT NULL, mcp_client TEXT NOT NULL,
            odoo_db_name TEXT, odoo_user TEXT, odoo_version TEXT, odoo_transport TEXT,
            input_payload TEXT NOT NULL, dry_run INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
            proposed_action TEXT, actual_result TEXT, affected_odoo_records TEXT NOT NULL,
            error_code TEXT, error_message TEXT, final_status TEXT NOT NULL,
            previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE (tenant_id, entry_hash)
        )
        """,
        "CREATE INDEX audit_log_tenant_order ON audit_log (tenant_id, id)",
        """
        CREATE TABLE proposals (
            id BIGSERIAL PRIMARY KEY, request_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            company_id INTEGER NOT NULL CHECK (company_id > 0), tool_name TEXT NOT NULL,
            module TEXT NOT NULL, proposal_type TEXT NOT NULL, payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('proposed', 'executing', 'executed', 'failed', 'unknown', 'expired')
            ),
            executed_at TEXT, erp_record_refs TEXT NOT NULL, created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX proposals_tenant_request ON proposals (tenant_id, request_id)",
        """
        CREATE TABLE artifacts (
            id BIGSERIAL PRIMARY KEY, request_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            company_id INTEGER NOT NULL CHECK (company_id > 0), tool_name TEXT NOT NULL,
            module TEXT NOT NULL, artifact_type TEXT NOT NULL, artifact_format TEXT NOT NULL,
            content TEXT NOT NULL, created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX artifacts_tenant_request ON artifacts (tenant_id, request_id)",
        """
        CREATE TABLE idempotency_keys (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL,
            company_id INTEGER NOT NULL CHECK (company_id > 0), tool_name TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('in_progress', 'succeeded', 'failed', 'unknown')),
            owner_request_id TEXT NOT NULL, response_json TEXT, expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE (tenant_id, tool_name, idempotency_key),
            CHECK ((state = 'in_progress') = (response_json IS NULL))
        )
        """,
        """
        CREATE TABLE capabilities_cache (
            id BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, connection_id TEXT NOT NULL,
            odoo_version TEXT NOT NULL, capabilities_json TEXT NOT NULL, probed_at TEXT NOT NULL,
            UNIQUE (tenant_id, connection_id, odoo_version)
        )
        """,
        """
        CREATE TABLE erp_connections (
            id TEXT NOT NULL, tenant_id TEXT NOT NULL, connection_label TEXT NOT NULL,
            odoo_url TEXT NOT NULL, database_name TEXT NOT NULL, username TEXT NOT NULL,
            encrypted_api_key TEXT NOT NULL, discovered_company_ids_json TEXT NOT NULL,
            allowed_company_ids_json TEXT, default_company_id INTEGER CHECK (default_company_id > 0),
            detected_version TEXT, selected_transport TEXT,
            key_version INTEGER NOT NULL CHECK (key_version > 0),
            status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'revoked', 'expired')),
            enrollment_handle_hash TEXT UNIQUE, consumed_enrollment_handle_hash TEXT UNIQUE,
            enrollment_expires_at TEXT, activated_at TEXT, revoked_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, id),
            CHECK ((status != 'active') OR (
                allowed_company_ids_json IS NOT NULL AND default_company_id IS NOT NULL
                AND enrollment_handle_hash IS NULL AND consumed_enrollment_handle_hash IS NOT NULL
            ))
        )
        """,
        "CREATE INDEX erp_connections_status ON erp_connections (status, enrollment_expires_at)",
        """
        CREATE TABLE oauth_clients (
            client_id TEXT PRIMARY KEY,
            registration_method TEXT NOT NULL CHECK (registration_method IN ('dcr', 'cimd')),
            metadata_json TEXT NOT NULL, client_secret_hash TEXT, metadata_expires_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE oauth_authorization_sessions (
            state_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES oauth_clients(client_id),
            connector_id TEXT, superseded_connector_id TEXT, redirect_uri TEXT NOT NULL,
            resource TEXT NOT NULL, scopes_json TEXT NOT NULL, code_challenge TEXT NOT NULL,
            client_state TEXT, authorization_code_hash TEXT UNIQUE,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'authorized', 'consumed', 'expired', 'denied')
            ),
            expires_at TEXT NOT NULL, consumed_at TEXT, created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE oauth_grants (
            id TEXT PRIMARY KEY, connector_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            client_id TEXT NOT NULL REFERENCES oauth_clients(client_id), resource TEXT NOT NULL,
            scopes_json TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
            created_at TEXT NOT NULL, revoked_at TEXT,
            FOREIGN KEY (tenant_id, connector_id) REFERENCES erp_connections(tenant_id, id)
        )
        """,
        """
        CREATE TABLE oauth_tokens (
            token_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES oauth_grants(id),
            token_type TEXT NOT NULL CHECK (token_type IN ('access', 'refresh')),
            scopes_json TEXT NOT NULL, expires_at TEXT NOT NULL, rotated_at TEXT,
            revoked_at TEXT, created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX oauth_tokens_grant ON oauth_tokens (grant_id, token_type)",
        """
        CREATE FUNCTION reject_audit_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'audit_log is append-only' USING ERRCODE = 'integrity_constraint_violation'; END
        $$
        """,
        "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log FOR EACH ROW EXECUTE FUNCTION reject_audit_change()",
        "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION reject_audit_change()",
        """
        CREATE FUNCTION enforce_connector_authority() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' AND NEW.status = 'active' THEN
                RAISE EXCEPTION 'active connector authority must be activated transactionally' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status <> NEW.status AND NOT (
                (OLD.status = 'pending' AND NEW.status IN ('active', 'expired', 'revoked'))
                OR (OLD.status = 'active' AND NEW.status = 'revoked')
            ) THEN
                RAISE EXCEPTION 'illegal connector status transition' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.status = 'active' AND (
                NEW.allowed_company_ids_json IS NULL OR NEW.default_company_id IS NULL
                OR NEW.enrollment_handle_hash IS NOT NULL
                OR NEW.consumed_enrollment_handle_hash IS NULL
                OR NOT EXISTS (
                    SELECT 1 FROM oauth_grants WHERE connector_id = NEW.id
                    AND tenant_id = NEW.tenant_id AND status = 'active'
                )
            ) THEN
                RAISE EXCEPTION 'active connector authority is incomplete' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$
        """,
        "CREATE TRIGGER erp_connection_authority BEFORE INSERT OR UPDATE ON erp_connections FOR EACH ROW EXECUTE FUNCTION enforce_connector_authority()",
        """
        CREATE FUNCTION enforce_oauth_session_transition() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status <> NEW.status AND NOT (
                (OLD.status = 'pending' AND NEW.status IN ('authorized', 'expired', 'denied'))
                OR (OLD.status = 'authorized' AND NEW.status = 'consumed')
            ) THEN RAISE EXCEPTION 'illegal authorization-session transition' USING ERRCODE = 'integrity_constraint_violation'; END IF;
            RETURN NEW;
        END $$
        """,
        "CREATE TRIGGER oauth_session_legal_transition BEFORE UPDATE OF status ON oauth_authorization_sessions FOR EACH ROW EXECUTE FUNCTION enforce_oauth_session_transition()",
        """
        CREATE FUNCTION enforce_oauth_grant() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status = 'active' AND EXISTS (SELECT 1 FROM erp_connections WHERE id = OLD.connector_id AND tenant_id = OLD.tenant_id AND status = 'active') THEN
                    RAISE EXCEPTION 'active connector grant cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.scopes_json <> NEW.scopes_json THEN RAISE EXCEPTION 'OAuth grant scopes are immutable' USING ERRCODE = 'integrity_constraint_violation'; END IF;
            IF OLD.status <> NEW.status AND NOT (OLD.status = 'active' AND NEW.status = 'revoked') THEN RAISE EXCEPTION 'illegal grant status transition' USING ERRCODE = 'integrity_constraint_violation'; END IF;
            IF OLD.status = 'active' AND NEW.status <> 'active' AND EXISTS (SELECT 1 FROM erp_connections WHERE id = OLD.connector_id AND tenant_id = OLD.tenant_id AND status = 'active') THEN
                RAISE EXCEPTION 'active connector grant must be revoked transactionally' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$
        """,
        "CREATE TRIGGER oauth_grant_update_authority BEFORE UPDATE ON oauth_grants FOR EACH ROW EXECUTE FUNCTION enforce_oauth_grant()",
        "CREATE TRIGGER oauth_grant_delete_authority BEFORE DELETE ON oauth_grants FOR EACH ROW EXECUTE FUNCTION enforce_oauth_grant()",
        """
        CREATE FUNCTION enforce_oauth_token_scopes() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE grant_scopes JSONB;
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.scopes_json <> NEW.scopes_json THEN RAISE EXCEPTION 'OAuth token scopes are immutable' USING ERRCODE = 'integrity_constraint_violation'; END IF;
            SELECT scopes_json::jsonb INTO grant_scopes FROM oauth_grants WHERE id = NEW.grant_id;
            IF jsonb_typeof(NEW.scopes_json::jsonb) <> 'array' OR jsonb_array_length(NEW.scopes_json::jsonb) = 0
               OR EXISTS (
                   SELECT 1 FROM jsonb_array_elements_text(NEW.scopes_json::jsonb) s
                   WHERE NOT jsonb_exists(grant_scopes, s)
               ) THEN
                RAISE EXCEPTION 'OAuth token scope invariant failed' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$
        """,
        "CREATE TRIGGER oauth_token_scope_invariant BEFORE INSERT OR UPDATE ON oauth_tokens FOR EACH ROW EXECUTE FUNCTION enforce_oauth_token_scopes()",
    ),
)

POSTGRES_MIGRATIONS = (POSTGRES_BASELINE,)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def apply_postgres_migrations(database: PostgresDatabase) -> None:
    identifiers = [migration.identifier for migration in POSTGRES_MIGRATIONS]
    expected = {migration.identifier: migration.checksum for migration in POSTGRES_MIGRATIONS}
    try:
        with database.migration_transaction() as connection:
            database.lock(connection, "odoo-mcp-schema-migrations")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    id TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                str(row["id"]): str(row["checksum"])
                for row in connection.execute(
                    "SELECT id, checksum FROM schema_migrations ORDER BY id"
                )
            }
            if list(applied) != identifiers[: len(applied)]:
                raise MigrationError("Applied migration history is not a known ordered prefix")
            if any(
                expected.get(identifier) != checksum for identifier, checksum in applied.items()
            ):
                raise MigrationError("An applied migration does not match its immutable checksum")
            for migration in POSTGRES_MIGRATIONS[len(applied) :]:
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (id, checksum, applied_at) VALUES (?, ?, ?)",
                    (migration.identifier, migration.checksum, _timestamp()),
                )
    except DATABASE_ERRORS as exc:
        raise MigrationError("PostgreSQL migration failed") from exc
