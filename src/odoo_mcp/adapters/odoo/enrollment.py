"""Odoo validation used before Shared Hosted company selection."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from odoo_mcp.adapters.base import Company
from odoo_mcp.adapters.odoo.outbound_policy import SharedOutboundPolicy, shared_http_transport
from odoo_mcp.adapters.odoo.transports.json2 import Json2Transport
from odoo_mcp.adapters.odoo.transports.json_rpc import JsonRpcTransport
from odoo_mcp.adapters.odoo.versioning import detect_major_version
from odoo_mcp.app.settings import OdooEnrollmentCredentials
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError


@dataclass(frozen=True, slots=True)
class VerifiedEnrollment:
    version: int
    transport: str
    companies: tuple[Company, ...]


class OdooEnrollmentValidator:
    """Authenticate and discover companies without constructing final settings."""

    def __init__(self, policy: SharedOutboundPolicy, *, company_limit: int = 200) -> None:
        self._policy = policy
        self._company_limit = company_limit

    async def validate(self, credentials: OdooEnrollmentCredentials) -> VerifiedEnrollment:
        await self._policy.validate_url(str(credentials.url))
        client = httpx.AsyncClient(
            base_url=str(credentials.url).rstrip("/"),
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            transport=shared_http_transport(self._policy),
        )
        try:
            version = await detect_major_version(credentials, client)
            transport = (
                JsonRpcTransport(credentials, client)
                if version == 18
                else Json2Transport(credentials, client)
            )
            await transport.authenticate()
            rows = await transport.discover_companies(limit=self._company_limit + 1)
        finally:
            await client.aclose()
        if len(rows) > self._company_limit:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned too many companies for enrollment.",
                "Reduce the technical user's company access and retry.",
            )
        companies: list[Company] = []
        for row in rows:
            identifier = row.get("id")
            name = row.get("name")
            if (
                not isinstance(identifier, int)
                or isinstance(identifier, bool)
                or identifier <= 0
                or not isinstance(name, str)
                or not name.strip()
            ):
                raise OdooMcpError(
                    ErrorCode.ODOO_API_ERROR,
                    "Odoo returned an invalid company response.",
                    "Check Odoo compatibility and retry.",
                )
            companies.append(Company(id=identifier, name=name.strip()))
        if not companies or len({company.id for company in companies}) != len(companies):
            raise OdooMcpError(
                ErrorCode.ODOO_AUTH_FAILED,
                "No unambiguous authorized company was discovered.",
                "Grant access to at least one Odoo company and retry.",
            )
        return VerifiedEnrollment(
            version=version,
            transport=transport.name,
            companies=tuple(sorted(companies, key=lambda company: company.id)),
        )
