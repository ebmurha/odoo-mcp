"""Storage composition, backup, and verified restore."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

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
        self.audit = AuditRepository(database)
        self.proposals = ProposalRepository(database)
        self.artifacts = ArtifactRepository(database)
        self.idempotency = IdempotencyRepository(database)
        self.execution = ExecutionJournal(database, self.idempotency, self.audit)
        self.capabilities = CapabilityCacheRepository(database)
        self.connections = SQLiteEncryptedConnectionRepository(database, self.audit, keyring)

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
        for tenant_id in self.audit.tenant_ids():
            self.audit.verify_chain(tenant_id)
        for row in idempotency_rows:
            request_hash = str(row["request_hash"])
            if len(request_hash) != 64 or any(
                char not in "0123456789abcdef" for char in request_hash
            ):
                raise StorageCorruptionError("Stored idempotency state is invalid")
            IdempotencyState(str(row["state"]))
            if row["response_json"] is not None:
                parse_mapping(str(row["response_json"]))
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
        self.connections.verify_all()

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
