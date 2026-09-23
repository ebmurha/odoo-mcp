from __future__ import annotations

import httpx
import pytest

from odoo_mcp.adapters.odoo.outbound_policy import SharedOutboundPolicy
from odoo_mcp.app.shared_auth import CimdFetcher, SharedOAuthProvider
from odoo_mcp.storage.database import SQLiteDatabase
from odoo_mcp.storage.migrations import (
    IDEMPOTENCY_RESPONSE_INVARIANT,
    INITIAL_SCHEMA,
    SHARED_HOSTED_AUTHORITY,
    apply_migrations,
)


async def _public(_host: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


@pytest.mark.asyncio
async def test_cimd_requires_exact_client_id_and_redirects() -> None:
    client_id = "https://client.invalid/metadata.json"

    def exact(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "client_id": str(request.url),
                "client_name": "Synthetic",
                "redirect_uris": ["https://client.invalid/callback"],
                "token_endpoint_auth_method": "none",
                "scope": "core_read",
            },
        )

    fetcher = CimdFetcher(SharedOutboundPolicy(_public), transport=httpx.MockTransport(exact))
    metadata = await fetcher.fetch(client_id)
    assert metadata is not None
    assert metadata.client_id == client_id

    def mismatch(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "client_id": "https://other.invalid/metadata.json",
                "redirect_uris": ["https://client.invalid/callback"],
                "token_endpoint_auth_method": "none",
            },
        )

    assert (
        await CimdFetcher(
            SharedOutboundPolicy(_public), transport=httpx.MockTransport(mismatch)
        ).fetch(client_id)
        is None
    )


@pytest.mark.asyncio
async def test_cimd_rejects_redirects_oversize_and_forbidden_destination() -> None:
    client_id = "https://client.invalid/metadata.json"

    def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://other.invalid"})

    def oversized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (64 * 1024 + 1))

    assert (
        await CimdFetcher(
            SharedOutboundPolicy(_public), transport=httpx.MockTransport(redirect)
        ).fetch(client_id)
        is None
    )
    assert (
        await CimdFetcher(
            SharedOutboundPolicy(_public), transport=httpx.MockTransport(oversized)
        ).fetch(client_id)
        is None
    )
    assert (
        await CimdFetcher(
            SharedOutboundPolicy(_public), transport=httpx.MockTransport(redirect)
        ).fetch("https://127.0.0.1/metadata.json")
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirect_uri",
    ("javascript:alert(1)", "https://client.invalid/callback#fragment"),
)
async def test_cimd_rejects_unsafe_redirect_uris(redirect_uri: str) -> None:
    client_id = "https://client.invalid/metadata.json"

    def unsafe(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "client_id": client_id,
                "redirect_uris": [redirect_uri],
                "token_endpoint_auth_method": "none",
            },
            request=request,
        )

    fetcher = CimdFetcher(SharedOutboundPolicy(_public), transport=httpx.MockTransport(unsafe))
    assert await fetcher.fetch(client_id) is None


@pytest.mark.asyncio
async def test_upgrade_rejects_unsafe_persisted_dcr_and_cimd_clients(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "legacy.sqlite3")
    apply_migrations(
        database,
        (INITIAL_SCHEMA, IDEMPOTENCY_RESPONSE_INVARIANT, SHARED_HOSTED_AUTHORITY),
    )
    with database.transaction(write=True) as connection:
        connection.executemany(
            """
            INSERT INTO oauth_clients (
                client_id, registration_method, metadata_json, client_secret_hash,
                metadata_expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                (
                    "legacy-dcr",
                    "dcr",
                    '{"client_id":"legacy-dcr","redirect_uris":["javascript:alert(1)"],"token_endpoint_auth_method":"none"}',
                    None,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
                (
                    "legacy-malformed",
                    "dcr",
                    "{",
                    None,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
                (
                    "https://legacy.invalid/client.json",
                    "cimd",
                    '{"client_id":"https://legacy.invalid/client.json","redirect_uris":["https://client.invalid/callback#fragment"],"token_endpoint_auth_method":"none"}',
                    "2099-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            ),
        )
    apply_migrations(database)

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    provider = SharedOAuthProvider(
        database,
        issuer_url="https://service.invalid",
        resource_url="https://service.invalid/mcp",
        cimd_fetcher=CimdFetcher(
            SharedOutboundPolicy(_public), transport=httpx.MockTransport(unavailable)
        ),
    )

    assert await provider.get_client("legacy-dcr") is None
    assert await provider.get_client("legacy-malformed") is None
    assert await provider.get_client("https://legacy.invalid/client.json") is None
    with database.transaction() as connection:
        expiries = connection.execute(
            "SELECT metadata_expires_at FROM oauth_clients ORDER BY client_id"
        ).fetchall()
    assert all(row["metadata_expires_at"] is not None for row in expiries)
