from __future__ import annotations

import json
import os
import runpy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from odoo_mcp.adapters.odoo.connections import ConnectorAuthorization
from odoo_mcp.app.settings import OdooConnectionSettings
from odoo_mcp.storage import (
    AuditEvent,
    EncryptionKeyring,
    IdempotencyDisposition,
    IdempotencyPayloadMismatch,
    IdempotencyState,
    MigrationError,
    ProposalState,
    Storage,
    StorageCorruptionError,
)
from odoo_mcp.storage.postgres_migrations import POSTGRES_BASELINE


def _database_url() -> str:
    value = os.environ.get("ODOO_MCP_TEST_POSTGRES_URL")
    if not value:
        pytest.skip("ODOO_MCP_TEST_POSTGRES_URL is not configured")
    return value


def _reset(url: str) -> None:
    with psycopg.connect(url) as connection:
        connection.execute(
            """
            TRUNCATE TABLE oauth_tokens, oauth_grants, oauth_authorization_sessions,
                oauth_clients, erp_connections, capabilities_cache, idempotency_keys,
                artifacts, proposals, audit_log RESTART IDENTITY CASCADE
            """
        )


def _event(request_id: str) -> AuditEvent:
    return AuditEvent(
        request_id=request_id,
        tenant_id="tenant-a",
        company_id=1,
        tool_name="synthetic_tool",
        tool_version="1.0",
        module="accounting",
        authenticated_subject="subject-a",
        mcp_client="test-client",
        odoo_db_name="synthetic-db",
        odoo_user="synthetic-user",
        odoo_version="19",
        odoo_transport="json2",
        input_payload={"value": 1},
        dry_run=False,
        proposed_action=None,
        actual_result={"status": "ok"},
        affected_odoo_records=(),
        error_code=None,
        error_message=None,
        final_status="succeeded",
    )


def _save_active_connector(storage: Storage) -> None:
    assert storage.keyring is not None
    settings = OdooConnectionSettings(
        url="https://odoo.invalid",
        database="synthetic-db",
        username="synthetic-user",
        api_key="synthetic-secret",
        allowed_company_ids=(1, 2),
        default_company_id=1,
    )
    encrypted, key_version = storage.keyring.encrypt(
        "tenant-a", "connection-a", settings.api_key.get_secret_value()
    )
    client_metadata = json.dumps(
        {
            "client_id": "test-client",
            "redirect_uris": ["https://client.invalid/callback"],
            "token_endpoint_auth_method": "none",
        }
    )
    with storage.database.transaction(write=True) as connection:
        connection.execute(
            """
            INSERT INTO oauth_clients (
                client_id, registration_method, metadata_json, client_secret_hash,
                metadata_expires_at, created_at, updated_at
            ) VALUES (
                'test-client', 'dcr', ?, NULL, NULL,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
            ) ON CONFLICT (client_id) DO NOTHING
            """,
            (client_metadata,),
        )
        connection.execute(
            """
            INSERT INTO erp_connections (
                id, tenant_id, connection_label, odoo_url, database_name, username,
                encrypted_api_key, discovered_company_ids_json,
                allowed_company_ids_json, default_company_id, detected_version,
                selected_transport, key_version, status, enrollment_handle_hash,
                consumed_enrollment_handle_hash, enrollment_expires_at, activated_at,
                revoked_at, created_at, updated_at
            ) VALUES (
                'connection-a', 'tenant-a', 'Synthetic', ?, ?, ?, ?, '[1,2]',
                '[1,2]', 1, '19', 'json2', ?, 'pending', NULL, ?,
                '2099-01-01T00:00:00Z', NULL, NULL,
                '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
            )
            """,
            (
                str(settings.url),
                settings.database,
                settings.username,
                encrypted,
                key_version,
                uuid4().hex * 2,
            ),
        )
        connection.execute(
            """
            INSERT INTO oauth_grants (
                id, connector_id, tenant_id, client_id, resource, scopes_json,
                status, created_at, revoked_at
            ) VALUES (?, 'connection-a', 'tenant-a', 'test-client',
                'https://service.invalid/mcp', '["core_read"]', 'active',
                '2026-01-01T00:00:00Z', NULL)
            """,
            (str(uuid4()),),
        )
        connection.execute(
            """
            UPDATE erp_connections SET status = 'active', activated_at = updated_at
            WHERE tenant_id = 'tenant-a' AND id = 'connection-a'
            """
        )


def test_postgres_serializes_startup_migrations_and_rejects_checksum_drift() -> None:
    url = _database_url()
    with psycopg.connect(url) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")

    def open_and_close(_index: int) -> None:
        storage = Storage.open_postgres(url, url)
        storage.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(open_and_close, range(2)))

    with psycopg.connect(url) as connection:
        applied = connection.execute("SELECT id, checksum FROM schema_migrations").fetchone()
        assert applied == (POSTGRES_BASELINE.identifier, POSTGRES_BASELINE.checksum)
        connection.execute(
            "UPDATE schema_migrations SET checksum = 'invalid' WHERE id = %s",
            (POSTGRES_BASELINE.identifier,),
        )

    try:
        with pytest.raises(MigrationError, match="immutable checksum"):
            Storage.open_postgres(url, url)
    finally:
        with psycopg.connect(url) as connection:
            connection.execute(
                "UPDATE schema_migrations SET checksum = %s WHERE id = %s",
                (POSTGRES_BASELINE.checksum, POSTGRES_BASELINE.identifier),
            )


def test_postgres_preserves_repository_transactions_and_invariants() -> None:
    url = _database_url()
    storage = Storage.open_postgres(url, url)
    _reset(url)
    try:
        proposal = storage.proposals.create(
            "req_1", "tenant-a", 1, "tool", "accounting", "synthetic", {"value": 1}
        )
        artifact = storage.artifacts.create(
            "req_1", "tenant-a", 1, "tool", "accounting", "report", "markdown", "ok"
        )
        assert storage.proposals.get("tenant-a", proposal.id) == proposal
        assert storage.artifacts.get("tenant-a", artifact.id) == artifact
        assert storage.proposals.get("tenant-b", proposal.id) is None
        assert storage.artifacts.get("tenant-b", artifact.id) is None
        assert (
            storage.proposals.transition(
                "tenant-a", proposal.id, ProposalState.PROPOSED, ProposalState.EXECUTING
            ).status
            is ProposalState.EXECUTING
        )

        first = storage.idempotency.reserve(
            "tenant-a", 1, "tool", "same-key", {"value": 1}, "req_owner"
        )
        assert first.disposition is IdempotencyDisposition.RESERVED
        storage.idempotency.finish(
            "tenant-a",
            1,
            "tool",
            "same-key",
            "req_owner",
            IdempotencyState.SUCCEEDED,
            {"status": "ok"},
        )
        assert (
            storage.idempotency.reserve(
                "tenant-a", 1, "tool", "same-key", {"value": 1}, "req_replay"
            ).disposition
            is IdempotencyDisposition.REPLAY
        )
        with pytest.raises(IdempotencyPayloadMismatch):
            storage.idempotency.reserve(
                "tenant-a", 1, "tool", "same-key", {"value": 2}, "req_mismatch"
            )

        unknown = storage.idempotency.reserve(
            "tenant-a", 1, "tool", "unknown-key", {"value": 3}, "req_unknown"
        )
        assert unknown.disposition is IdempotencyDisposition.RESERVED
        storage.idempotency.finish(
            "tenant-a",
            1,
            "tool",
            "unknown-key",
            "req_unknown",
            IdempotencyState.UNKNOWN,
            {"status": "unknown"},
        )
        unknown_replay = storage.idempotency.reserve(
            "tenant-a", 1, "tool", "unknown-key", {"value": 3}, "req_retry"
        )
        assert unknown_replay.disposition is IdempotencyDisposition.REPLAY
        assert unknown_replay.state is IdempotencyState.UNKNOWN

        with ThreadPoolExecutor(max_workers=5) as pool:
            dispositions = list(
                pool.map(
                    lambda index: (
                        storage.idempotency.reserve(
                            "tenant-a",
                            1,
                            "concurrent-tool",
                            "concurrent-key",
                            {"value": 1},
                            f"req_{index}",
                        ).disposition
                    ),
                    range(5),
                )
            )
        assert dispositions.count(IdempotencyDisposition.RESERVED) == 1
        assert dispositions.count(IdempotencyDisposition.IN_PROGRESS) == 4

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda index: storage.audit.append(_event(f"audit_{index}")), range(5)))
        storage.audit.verify_chain("tenant-a")

        with pytest.raises(psycopg.IntegrityError, match="activated transactionally"):
            with storage.database.transaction(write=True) as connection:
                connection.execute(
                    """
                    INSERT INTO erp_connections (
                        id, tenant_id, connection_label, odoo_url, database_name,
                        username, encrypted_api_key, discovered_company_ids_json,
                        allowed_company_ids_json, default_company_id, key_version,
                        status, consumed_enrollment_handle_hash, created_at, updated_at
                    ) VALUES (
                        'connection-a', 'tenant-a', 'A', 'https://odoo.invalid',
                        'synthetic', 'synthetic', 'ciphertext', '[1]', '[1]', 1, 1,
                        'active', 'consumed', '2026-01-01T00:00:00Z',
                        '2026-01-01T00:00:00Z'
                    )
                    """
                )

        storage.verify()
        with storage.database.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO idempotency_keys (
                    tenant_id, company_id, tool_name, idempotency_key,
                    request_hash, state, owner_request_id, response_json,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "tenant-a",
                    1,
                    "corruption-test",
                    "corruption-key",
                    "not-a-hash",
                    "in_progress",
                    "req_corrupt",
                    None,
                    "2030-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
        with pytest.raises(StorageCorruptionError, match="idempotency state"):
            storage.verify()
        with storage.database.transaction(write=True) as connection:
            connection.execute(
                "DELETE FROM idempotency_keys WHERE idempotency_key = ?",
                ("corruption-key",),
            )
        storage.verify()
    finally:
        storage.close()

    reopened = Storage.open_postgres(url, url)
    try:
        assert reopened.proposals.get("tenant-a", proposal.id) is not None
        reopened.verify()
    finally:
        reopened.close()


def test_postgres_runs_the_accepted_shared_oauth_mcp_flow(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _database_url()
    bootstrap = Storage.open_postgres(url, url)
    bootstrap.close()
    _reset(url)

    def open_postgres(_cls: type[Storage], _path: object, *, keyring: object = None) -> Storage:
        return Storage.open_postgres(url, url, keyring=keyring)  # type: ignore[arg-type]

    monkeypatch.setattr(Storage, "open", classmethod(open_postgres))
    shared_tests = runpy.run_path(str(Path(__file__).parents[1] / "shared" / "test_shared_app.py"))
    shared_tests["test_complete_shared_hosted_flow_and_fail_closed_mcp"](tmp_path)


@pytest.mark.asyncio
async def test_postgres_runs_the_accepted_encryption_key_rotation() -> None:
    url = _database_url()
    old = Storage.open_postgres(
        url, url, keyring=EncryptionKeyring(active_version=1, keys={1: b"a" * 32})
    )
    _reset(url)
    _save_active_connector(old)
    rotated = Storage.open_postgres(
        url,
        url,
        keyring=EncryptionKeyring(active_version=2, keys={1: b"a" * 32, 2: b"b" * 32}),
    )
    try:
        assert rotated.connections.rotate(
            "tenant-a", "connection-a", "req_rotate", "operator", "maintenance"
        )
        assert not rotated.connections.rotate(
            "tenant-a", "connection-a", "req_rotate_again", "operator", "maintenance"
        )
        authorization = ConnectorAuthorization(
            tenant_id="tenant-a",
            connection_id="connection-a",
            authenticated_subject="operator",
            mcp_client="test-client",
            permissions=frozenset({"core_read"}),
        )
        resolved = await rotated.connections.resolve_authorized(authorization)
        assert resolved is not None
        assert resolved.api_key.get_secret_value() == "synthetic-secret"
        events = rotated.audit.list_for_tenant("tenant-a")
        assert len(events) == 1
        assert events[0].actual_result == {"connection_id": "connection-a", "key_version": 2}
    finally:
        rotated.close()
        old.close()

    without_old_key = Storage.open_postgres(
        url, url, keyring=EncryptionKeyring(active_version=2, keys={2: b"b" * 32})
    )
    try:
        assert await without_old_key.connections.resolve_authorized(authorization) is not None
    finally:
        without_old_key.close()
