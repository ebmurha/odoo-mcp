from __future__ import annotations

import sqlite3

import pytest

from odoo_mcp.adapters.odoo.connections import (
    ConnectorAuthorization,
    SharedHostedConnectionResolver,
    reset_connector_authorization,
    set_connector_authorization,
)
from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings
from odoo_mcp.storage import ConnectionDecryptionError, EncryptionKeyring, Storage


def _authorization(tenant_id: str, connection_id: str) -> ConnectorAuthorization:
    return ConnectorAuthorization(
        tenant_id=tenant_id,
        connection_id=connection_id,
        authenticated_subject="subject-a",
        mcp_client="test-client",
        permissions=frozenset({"core_read"}),
    )


def _connection() -> OdooConnectionSettings:
    return OdooConnectionSettings(
        url="https://odoo.invalid",
        database="synthetic-db",
        username="synthetic-user",
        api_key="synthetic-secret",
        allowed_company_ids=(1, 2),
        default_company_id=1,
    )


@pytest.mark.asyncio
async def test_connections_are_encrypted_bound_and_redacted(tmp_path) -> None:
    keyring = EncryptionKeyring(active_version=1, keys={1: b"a" * 32})
    storage = Storage.open(tmp_path / "state.sqlite3", keyring=keyring)
    storage.connections.save(
        tenant_id="tenant-a",
        connection_id="connection-a",
        connection_label="Synthetic",
        connection=_connection(),
    )

    stored = (
        sqlite3.connect(tmp_path / "state.sqlite3")
        .execute("SELECT encrypted_api_key FROM erp_connections")
        .fetchone()[0]
    )
    assert "synthetic-secret" not in stored
    assert "synthetic-secret" not in (tmp_path / "state.sqlite3").read_bytes().decode(
        "utf-8", errors="ignore"
    )
    resolved = await storage.connections.resolve_authorized(
        _authorization("tenant-a", "connection-a")
    )
    assert resolved is not None
    assert resolved.api_key.get_secret_value() == "synthetic-secret"
    assert (
        await storage.connections.resolve_authorized(_authorization("tenant-b", "connection-a"))
        is None
    )
    assert (
        await storage.connections.resolve_authorized(_authorization("tenant-a", "connection-b"))
        is None
    )


@pytest.mark.asyncio
async def test_encrypted_repository_drives_the_shared_hosted_resolver(tmp_path) -> None:
    storage = Storage.open(
        tmp_path / "state.sqlite3",
        keyring=EncryptionKeyring(active_version=1, keys={1: b"a" * 32}),
    )
    storage.connections.save("tenant-a", "connection-a", "A", _connection())
    authorization = _authorization("tenant-a", "connection-a")
    token = set_connector_authorization(authorization)
    try:
        binding = await SharedHostedConnectionResolver(storage.connections).resolve()
    finally:
        reset_connector_authorization(token)

    assert binding.profile is DeploymentProfile.SHARED
    assert binding.tenant_id == "tenant-a"
    assert binding.connection.api_key.get_secret_value() == "synthetic-secret"


@pytest.mark.asyncio
async def test_ciphertext_cannot_move_between_tenants_or_connections(tmp_path) -> None:
    keyring = EncryptionKeyring(active_version=1, keys={1: b"a" * 32})
    storage = Storage.open(tmp_path / "state.sqlite3", keyring=keyring)
    storage.connections.save("tenant-a", "connection-a", "A", _connection())
    storage.connections.save("tenant-b", "connection-b", "B", _connection())
    database = sqlite3.connect(storage.database.path)
    database.execute(
        """
        UPDATE erp_connections
        SET encrypted_api_key = (
            SELECT encrypted_api_key FROM erp_connections
            WHERE tenant_id = 'tenant-a' AND id = 'connection-a'
        )
        WHERE tenant_id = 'tenant-b' AND id = 'connection-b'
        """
    )
    database.commit()
    database.close()

    with pytest.raises(ConnectionDecryptionError, match="could not be decrypted"):
        await storage.connections.resolve_authorized(_authorization("tenant-b", "connection-b"))


@pytest.mark.asyncio
async def test_key_rotation_is_atomic_idempotent_and_audited(tmp_path) -> None:
    old = EncryptionKeyring(active_version=1, keys={1: b"a" * 32})
    path = tmp_path / "state.sqlite3"
    storage = Storage.open(path, keyring=old)
    storage.connections.save("tenant-a", "connection-a", "A", _connection())

    rotated = Storage.open(
        path,
        keyring=EncryptionKeyring(active_version=2, keys={1: b"a" * 32, 2: b"b" * 32}),
    )
    assert (
        rotated.connections.rotate(
            tenant_id="tenant-a",
            connection_id="connection-a",
            request_id="req_rotate",
            authenticated_subject="operator",
            mcp_client="maintenance",
        )
        is True
    )
    assert (
        rotated.connections.rotate(
            tenant_id="tenant-a",
            connection_id="connection-a",
            request_id="req_rotate_again",
            authenticated_subject="operator",
            mcp_client="maintenance",
        )
        is False
    )
    assert (
        await rotated.connections.resolve_authorized(_authorization("tenant-a", "connection-a"))
    ) is not None
    events = rotated.audit.list_for_tenant("tenant-a")
    assert len(events) == 1
    assert events[0].tool_name == "rotate_erp_connection_key"
    assert events[0].actual_result == {"connection_id": "connection-a", "key_version": 2}

    without_old_key = Storage.open(path, keyring=EncryptionKeyring(2, {2: b"b" * 32}))
    assert (
        await without_old_key.connections.resolve_authorized(
            _authorization("tenant-a", "connection-a")
        )
    ) is not None


@pytest.mark.asyncio
async def test_rotation_rolls_back_when_its_audit_event_cannot_persist(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    original = Storage.open(path, keyring=EncryptionKeyring(1, {1: b"a" * 32}))
    original.connections.save("tenant-a", "connection-a", "A", _connection())
    rotating = Storage.open(
        path,
        keyring=EncryptionKeyring(2, {1: b"a" * 32, 2: b"b" * 32}),
    )
    database = sqlite3.connect(path)
    database.execute(
        """
        CREATE TRIGGER reject_rotation BEFORE INSERT ON audit_log
        WHEN NEW.tool_name = 'rotate_erp_connection_key'
        BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END
        """
    )
    database.commit()
    database.close()

    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        rotating.connections.rotate(
            "tenant-a", "connection-a", "req_rotate", "operator", "maintenance"
        )

    database = sqlite3.connect(path)
    row = database.execute(
        "SELECT key_version FROM erp_connections WHERE tenant_id = ? AND id = ?",
        ("tenant-a", "connection-a"),
    ).fetchone()
    database.close()
    assert row == (1,)
    resolved = await original.connections.resolve_authorized(
        _authorization("tenant-a", "connection-a")
    )
    assert resolved is not None
    assert resolved.api_key.get_secret_value() == "synthetic-secret"


def test_tenant_rotation_is_resumable_after_a_partial_failure(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    original = Storage.open(path, keyring=EncryptionKeyring(1, {1: b"a" * 32}))
    original.connections.save("tenant-a", "connection-a", "A", _connection())
    original.connections.save("tenant-a", "connection-b", "B", _connection())
    rotating = Storage.open(
        path,
        keyring=EncryptionKeyring(2, {1: b"a" * 32, 2: b"b" * 32}),
    )
    database = sqlite3.connect(path)
    database.execute(
        """
        CREATE TRIGGER reject_second_rotation BEFORE INSERT ON audit_log
        WHEN NEW.input_payload LIKE '%connection-b%'
        BEGIN SELECT RAISE(ABORT, 'synthetic partial failure'); END
        """
    )
    database.commit()
    database.close()

    with pytest.raises(sqlite3.IntegrityError, match="synthetic partial failure"):
        rotating.connections.rotate_tenant("tenant-a", "operator", "maintenance")

    database = sqlite3.connect(path)
    versions = database.execute(
        "SELECT id, key_version FROM erp_connections ORDER BY id"
    ).fetchall()
    database.execute("DROP TRIGGER reject_second_rotation")
    database.commit()
    database.close()
    assert versions == [("connection-a", 2), ("connection-b", 1)]

    assert rotating.connections.rotate_tenant("tenant-a", "operator", "maintenance") == 1
    assert rotating.connections.rotate_tenant("tenant-a", "operator", "maintenance") == 0
    assert rotating.audit.verify_chain("tenant-a").entry_count == 2
