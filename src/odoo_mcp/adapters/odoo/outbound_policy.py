"""Shared Hosted public-destination validation and DNS-pinned transport."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import cast
from urllib.parse import urlsplit

import httpcore
import httpx

from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError

AddressResolver = Callable[[str, int], Awaitable[tuple[str, ...]]]
MAX_RESPONSE_BYTES = 10 * 1024 * 1024


def _safe_network_error() -> OdooMcpError:
    return OdooMcpError(
        ErrorCode.ODOO_API_ERROR,
        "The destination is not permitted for Shared Hosted connections.",
        "Use a public HTTPS Odoo endpoint and retry.",
    )


def _public_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise _safe_network_error() from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if (
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
    ):
        raise _safe_network_error()
    return str(address)


async def _system_resolver(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    try:
        answers = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            timeout=3.0,
        )
    except (TimeoutError, OSError):
        raise _safe_network_error() from None
    return tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))


class SharedOutboundPolicy:
    """Validate every connection attempt and reject mixed or private DNS answers."""

    def __init__(
        self,
        resolver: AddressResolver = _system_resolver,
        *,
        max_addresses: int = 16,
    ) -> None:
        self._resolver = resolver
        self._max_addresses = max_addresses

    @staticmethod
    def parse_url(raw_url: str) -> tuple[str, int]:
        try:
            parsed = urlsplit(raw_url)
            port = parsed.port or 443
        except ValueError:
            raise _safe_network_error() from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise _safe_network_error()
        return parsed.hostname, port

    async def resolve_public(self, host: str, port: int) -> tuple[str, ...]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            answers = await self._resolver(host, port)
        else:
            answers = (str(literal),)
        if not answers or len(answers) > self._max_addresses:
            raise _safe_network_error()
        return tuple(_public_address(address) for address in answers)

    async def validate_url(self, raw_url: str) -> tuple[str, ...]:
        host, port = self.parse_url(raw_url)
        return await self.resolve_public(host, port)


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, policy: SharedOutboundPolicy) -> None:
        self._policy = policy
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 -- httpcore protocol
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._policy.resolve_public(host, port)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # pragma: no cover - backend-specific exception family
                last_error = exc
        raise _safe_network_error() from last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 -- httpcore protocol
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise _safe_network_error()

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class RebindingSafeTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport whose socket uses only a freshly validated DNS answer."""

    def __init__(self, policy: SharedOutboundPolicy) -> None:
        super().__init__(retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=_PinnedNetworkBackend(policy),
            max_connections=20,
            max_keepalive_connections=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_BoundedStream(cast(httpx.AsyncByteStream, response.stream), MAX_RESPONSE_BYTES),
            extensions=response.extensions,
        )


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, limit: int) -> None:
        self._stream = stream
        self._limit = limit

    async def __aiter__(self) -> AsyncIterator[bytes]:
        seen = 0
        async for chunk in self._stream:
            seen += len(chunk)
            if seen > self._limit:
                raise httpx.ReadError("Shared Hosted response exceeded the allowed size")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


def shared_http_transport(policy: SharedOutboundPolicy) -> httpx.AsyncBaseTransport:
    return RebindingSafeTransport(policy)
