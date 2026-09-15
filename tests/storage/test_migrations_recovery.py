from __future__ import annotations

import sqlite3

import pytest

from odoo_mcp.storage import (
    EncryptionKeyring,
    Migration,
    MigrationError,
    Storage,
    StorageCorruptionError,
)
from odoo_mcp.storage.database import SQLiteDatabase
from odoo_mcp.storage.migrations import apply_migrations


def test_migration_failure_rolls_back_and_is_not_recorded(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "migration.sqlite3")
    migrations = (
        Migration("0001_ok", ("CREATE TABLE stable (id INTEGER PRIMARY KEY)",)),
        Migration(
            "0002_fails",
            (
                "CREATE TABLE transient (id INTEGER PRIMARY KEY)",
                "THIS IS NOT SQL",
            ),
        ),
    )

    with pytest.raises(MigrationError, match="0002_fails"):
        apply_migrations(database, migrations)

    connection = sqlite3.connect(database.path)
    applied = connection.execute("SELECT id FROM schema_migrations ORDER BY id").fetchall()
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    connection.close()
    assert applied == [("0001_ok",)]
    assert "stable" in tables
    assert "transient" not in tables


def test_applied_migration_checksum_is_immutable(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "migration.sqlite3")
    original = (Migration("0001_test", ("CREATE TABLE stable (id INTEGER)",)),)
    apply_migrations(database, original)

    changed = (Migration("0001_test", ("CREATE TABLE different (id INTEGER)",)),)
    with pytest.raises(MigrationError, match="immutable checksum"):
        apply_migrations(database, changed)


def test_backup_restore_validates_state_and_preserves_source(tmp_path) -> None:
    keyring = EncryptionKeyring(1, {1: b"a" * 32})
    source = Storage.open(tmp_path / "source.sqlite3", keyring=keyring)
    source.capabilities.put("tenant-a", "connection-a", "19", {"account": True})
    source.idempotency.reserve("tenant-a", 1, "tool", "key", {"value": 1}, "req_1")
    source.idempotency.finish("tenant-a", "tool", "key", "req_1", response={"status": "succeeded"})
    backup = tmp_path / "backup.sqlite3"
    source.backup(backup)
    before = backup.read_bytes()

    restored_path = tmp_path / "restored.sqlite3"
    restored = Storage.restore(backup, restored_path, keyring=keyring)
    assert backup.read_bytes() == before
    assert restored.capabilities.get("tenant-a", "connection-a", "19") == {"account": True}
    replay = restored.idempotency.reserve("tenant-a", 1, "tool", "key", {"value": 1}, "req_2")
    assert replay.response == {"status": "succeeded"}


def test_corrupt_restore_fails_closed_without_replacing_destination(tmp_path) -> None:
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    destination = tmp_path / "restored.sqlite3"

    with pytest.raises(StorageCorruptionError, match="Restore validation failed"):
        Storage.restore(corrupt, destination)

    assert corrupt.read_bytes() == b"not a sqlite database"
    assert not destination.exists()


def test_empty_sqlite_database_is_not_accepted_as_a_restore(tmp_path) -> None:
    empty = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty).close()

    with pytest.raises(StorageCorruptionError, match="Restore validation failed"):
        Storage.restore(empty, tmp_path / "restored.sqlite3")

    assert empty.exists()
    assert not (tmp_path / "restored.sqlite3").exists()


def test_logically_corrupt_server_state_is_rejected_on_restore(tmp_path) -> None:
    source = Storage.open(tmp_path / "source.sqlite3")
    source.proposals.create("req_1", "tenant-a", 1, "tool", "accounting", "test", {})
    backup = tmp_path / "backup.sqlite3"
    source.backup(backup)
    database = sqlite3.connect(backup)
    database.execute("UPDATE proposals SET payload_json = '{'")
    database.commit()
    database.close()

    with pytest.raises(StorageCorruptionError, match="Restore validation failed"):
        Storage.restore(backup, tmp_path / "restored.sqlite3")

    assert backup.exists()
    assert not (tmp_path / "restored.sqlite3").exists()


@pytest.mark.asyncio
async def test_restore_requires_the_separate_connection_key_material(tmp_path) -> None:
    from odoo_mcp.adapters.odoo.connections import ConnectorAuthorization
    from odoo_mcp.app.settings import OdooConnectionSettings

    keyring = EncryptionKeyring(1, {1: b"a" * 32})
    source = Storage.open(tmp_path / "source.sqlite3", keyring=keyring)
    source.connections.save(
        "tenant-a",
        "connection-a",
        "Synthetic",
        OdooConnectionSettings(
            url="https://odoo.invalid",
            database="synthetic-db",
            username="synthetic-user",
            api_key="synthetic-secret",
            allowed_company_ids=(1,),
            default_company_id=1,
        ),
    )
    backup = tmp_path / "backup.sqlite3"
    source.backup(backup)

    missing_key_destination = tmp_path / "missing-key.sqlite3"
    with pytest.raises(StorageCorruptionError, match="Restore validation failed"):
        Storage.restore(backup, missing_key_destination)
    assert not missing_key_destination.exists()

    restored = Storage.restore(
        backup,
        tmp_path / "restored.sqlite3",
        keyring=keyring,
    )
    resolved = await restored.connections.resolve_authorized(
        ConnectorAuthorization(
            tenant_id="tenant-a",
            connection_id="connection-a",
            authenticated_subject="subject-a",
            mcp_client="test-client",
            permissions=frozenset({"core_read"}),
        )
    )
    assert resolved is not None
    assert resolved.api_key.get_secret_value() == "synthetic-secret"
