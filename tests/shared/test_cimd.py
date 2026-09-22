from __future__ import annotations

import httpx
import pytest

from odoo_mcp.adapters.odoo.outbound_policy import SharedOutboundPolicy
from odoo_mcp.app.shared_auth import CimdFetcher


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
