"""Storage composition, backup, and verified restore."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

from mcp.shared.auth import OAuthClientInformationFull

from odoo_mcp.storage.audit import AuditRepository
from odoo_mcp.storage.connections import EncryptionKeyring, SQLiteEncryptedConnectionRepository
from odoo_mcp.storage.database import SQLiteDatabase
from odoo_mcp.storage.errors import StorageCorruptionError, StorageError
from odoo_mcp.storage.execution import ExecutionJournal
from odoo_mcp.storage.json_support import (
    parse_mapping,
    parse_string_tuple,
    parse_timestamp,
)
from odoo_mcp.storage.migrations import apply_migrations
from odoo_mcp.storage.models import IdempotencyState, ProposalState
from odoo_mcp.storage.proposals import ProposalJournal
from odoo_mcp.storage.reporting import ReportJournal
from odoo_mcp.storage.repositories import (
    ArtifactRepository,
    CapabilityCacheRepository,
    IdempotencyRepository,
    ProposalRepository,
)


class Storage:
    """Initialized SQLite repositories sharing one transaction boundary."""

    def __init__(self, database: SQLiteDatabase, keyring: EncryptionKeyring | None) -> None:
        self.database = database
        self.keyring = keyring
        self.audit = AuditRepository(database)
        self.proposals = ProposalRepository(database)
        self.artifacts = ArtifactRepository(database)
        self.idempotency = IdempotencyRepository(database)
        self.execution = ExecutionJournal(database, self.idempotency, self.audit)
        self.capabilities = CapabilityCacheRepository(database)
        self.connections = SQLiteEncryptedConnectionRepository(database, self.audit, keyring)
        self.reports = ReportJournal(database, self.artifacts, self.audit)
        self.proposal_journal = ProposalJournal(database, self.proposals, self.artifacts)

    @classmethod
    def open(cls, path: Path, *, keyring: EncryptionKeyring | None = None) -> Storage:
        database = SQLiteDatabase(path)
        apply_migrations(database)
        return cls(database, keyring)

    def backup(self, destination: Path) -> None:
        destination = destination.resolve()
        if destination.exists():
            raise FileExistsError("Backup destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self.database.connect()
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        except BaseException:
            target.close()
            source.close()
            destination.unlink(missing_ok=True)
            raise
        else:
            target.close()
            source.close()

    def _verify_integrity(self) -> None:
        with self.database.transaction() as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if result is None or str(result[0]) != "ok":
                raise StorageCorruptionError("Storage integrity verification failed")
            idempotency_rows = connection.execute(
                """
                SELECT request_hash, state, response_json, expires_at, created_at, updated_at
                FROM idempotency_keys
                """
            ).fetchall()
            capability_rows = connection.execute(
                "SELECT capabilities_json FROM capabilities_cache"
            ).fetchall()
            proposal_rows = connection.execute(
                """
                SELECT payload_json, status, executed_at, erp_record_refs, created_at
                FROM proposals
                """
            ).fetchall()
            artifact_rows = connection.execute("SELECT created_at FROM artifacts").fetchall()
            connector_rows = connection.execute(
                """
                SELECT e.*,
                    (SELECT count(*) FROM oauth_grants g
                     WHERE g.tenant_id = e.tenant_id AND g.connector_id = e.id
                       AND g.status = 'active') AS active_grants
                FROM erp_connections e
                """
            ).fetchall()
            oauth_client_rows = connection.execute(
                "SELECT metadata_json, metadata_expires_at FROM oauth_clients"
            ).fetchall()
            oauth_session_rows = connection.execute(
                """
                SELECT state_hash, scopes_json, status, expires_at, consumed_at,
                    authorization_code_hash
                FROM oauth_authorization_sessions
                """
            ).fetchall()
            oauth_grant_rows = connection.execute(
                "SELECT scopes_json, status, created_at, revoked_at FROM oauth_grants"
            ).fetchall()
            oauth_token_rows = connection.execute(
                """
                SELECT token_hash, token_type, expires_at, rotated_at, revoked_at, created_at
                FROM oauth_tokens
                """
            ).fetchall()
        for tenant_id in self.audit.tenant_ids():
            self.audit.verify_chain(tenant_id)
        for row in idempotency_rows:
            request_hash = str(row["request_hash"])
            if len(request_hash) != 64 or any(
                char not in "0123456789abcdef" for char in request_hash
            ):
                raise StorageCorruptionError("Stored idempotency state is invalid")
            state = IdempotencyState(str(row["state"]))
            response = (
                None if row["response_json"] is None else parse_mapping(str(row["response_json"]))
            )
            if (state is IdempotencyState.IN_PROGRESS) != (response is None):
                raise StorageCorruptionError("Stored idempotency replay state is invalid")
            parse_timestamp(str(row["expires_at"]))
            parse_timestamp(str(row["created_at"]))
            parse_timestamp(str(row["updated_at"]))
        for row in capability_rows:
            parsed = parse_mapping(str(row["capabilities_json"]))
            if not all(isinstance(value, bool) for value in parsed.values()):
                raise StorageCorruptionError("Stored capability state is invalid")
        for row in proposal_rows:
            parse_mapping(str(row["payload_json"]))
            ProposalState(str(row["status"]))
            if row["executed_at"] is not None:
                parse_timestamp(str(row["executed_at"]))
            parse_string_tuple(str(row["erp_record_refs"]))
            parse_timestamp(str(row["created_at"]))
        for row in artifact_rows:
            parse_timestamp(str(row["created_at"]))
        for row in connector_rows:
            status = str(row["status"])
            discovered = json.loads(str(row["discovered_company_ids_json"]))
            if (
                status not in {"pending", "active", "revoked", "expired"}
                or not isinstance(discovered, list)
                or not discovered
                or not all(type(value) is int and value > 0 for value in discovered)
            ):
                raise StorageCorruptionError("Stored connector lifecycle state is invalid")
            if row["enrollment_expires_at"] is not None:
                parse_timestamp(str(row["enrollment_expires_at"]))
            if row["activated_at"] is not None:
                parse_timestamp(str(row["activated_at"]))
            if row["revoked_at"] is not None:
                parse_timestamp(str(row["revoked_at"]))
            if status == "active":
                allowed = json.loads(str(row["allowed_company_ids_json"]))
                if (
                    not isinstance(allowed, list)
                    or not allowed
                    or not set(allowed).issubset(discovered)
                    or row["default_company_id"] not in allowed
                    or int(row["active_grants"]) < 1
                ):
                    raise StorageCorruptionError("Stored connector authority is invalid")
        for row in oauth_client_rows:
            try:
                OAuthClientInformationFull.model_validate_json(str(row["metadata_json"]))
            except ValueError as exc:
                raise StorageCorruptionError("Stored OAuth client state is invalid") from exc
            if row["metadata_expires_at"] is not None:
                parse_timestamp(str(row["metadata_expires_at"]))
        for row in oauth_session_rows:
            self._verify_hash(str(row["state_hash"]), "authorization session")
            parse_string_tuple(str(row["scopes_json"]))
            parse_timestamp(str(row["expires_at"]))
            if row["consumed_at"] is not None:
                parse_timestamp(str(row["consumed_at"]))
            if row["authorization_code_hash"] is not None:
                self._verify_hash(str(row["authorization_code_hash"]), "authorization code")
        for row in oauth_grant_rows:
            parse_string_tuple(str(row["scopes_json"]))
            parse_timestamp(str(row["created_at"]))
            if row["revoked_at"] is not None:
                parse_timestamp(str(row["revoked_at"]))
        for row in oauth_token_rows:
            self._verify_hash(str(row["token_hash"]), "OAuth token")
            parse_timestamp(str(row["expires_at"]))
            parse_timestamp(str(row["created_at"]))
            if row["rotated_at"] is not None:
                parse_timestamp(str(row["rotated_at"]))
            if row["revoked_at"] is not None:
                parse_timestamp(str(row["revoked_at"]))
        self.connections.verify_all()

    @staticmethod
    def _verify_hash(value: str, label: str) -> None:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise StorageCorruptionError(f"Stored {label} state is invalid")

    def verify(self) -> None:
        """Verify migrations and all persisted recovery invariants."""

        apply_migrations(self.database)
        self._verify_integrity()

    @staticmethod
    def _verify_restore_source(path: Path) -> None:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            has_migrations = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'schema_migrations'
                """
            ).fetchone()
            if result is None or str(result[0]) != "ok" or has_migrations is None:
                raise StorageCorruptionError("Restore source is not a valid storage database")
        finally:
            connection.close()

    @classmethod
    def restore(
        cls,
        source: Path,
        destination: Path,
        *,
        keyring: EncryptionKeyring | None = None,
    ) -> Storage:
        source = source.resolve()
        destination = destination.resolve()
        if not source.is_file():
            raise FileNotFoundError("Restore source does not exist")
        if destination.exists():
            raise FileExistsError("Restore destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.restore")
        try:
            shutil.copy2(source, temporary)
            cls._verify_restore_source(temporary)
            candidate = cls.open(temporary, keyring=keyring)
            candidate._verify_integrity()
            temporary.replace(destination)
            return cls.open(destination, keyring=keyring)
        except (OSError, sqlite3.Error, StorageError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            raise StorageCorruptionError("Restore validation failed") from exc
