from __future__ import annotations

import pytest

from odoo_mcp.adapters.odoo.outbound_policy import SharedOutboundPolicy
from odoo_mcp.mcp.error_codes import OdooMcpError


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "192.0.2.1",
        "198.18.0.1",
        "::1",
        "fe80::1",
        "::ffff:127.0.0.1",
    ],
)
@pytest.mark.asyncio
async def test_forbidden_address_classes_fail_closed(address: str) -> None:
    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return (address,)

    with pytest.raises(OdooMcpError):
        await SharedOutboundPolicy(resolver).validate_url("https://odoo.invalid")


@pytest.mark.asyncio
async def test_mixed_dns_answers_and_excessive_results_fail_closed() -> None:
    async def mixed(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34", "127.0.0.1")

    async def excessive(_host: str, _port: int) -> tuple[str, ...]:
        return tuple(f"8.8.8.{value}" for value in range(1, 18))

    with pytest.raises(OdooMcpError):
        await SharedOutboundPolicy(mixed).validate_url("https://odoo.invalid")
    with pytest.raises(OdooMcpError):
        await SharedOutboundPolicy(excessive).validate_url("https://odoo.invalid")


@pytest.mark.parametrize(
    "url",
    [
        "http://odoo.invalid",
        "https://user:password@odoo.invalid",
        "https://odoo.invalid?next=https://private.invalid",
        "https://odoo.invalid/#fragment",
    ],
)
@pytest.mark.asyncio
async def test_unsafe_url_shapes_fail_closed(url: str) -> None:
    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    with pytest.raises(OdooMcpError):
        await SharedOutboundPolicy(resolver).validate_url(url)


@pytest.mark.asyncio
async def test_all_public_dns_answers_are_returned_for_pinned_connection() -> None:
    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34", "8.8.8.8")

    assert await SharedOutboundPolicy(resolver).validate_url("https://odoo.invalid") == (
        "93.184.216.34",
        "8.8.8.8",
    )
