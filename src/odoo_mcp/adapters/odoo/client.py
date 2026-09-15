"""Concrete Odoo adapter composed from a detected version transport."""

from __future__ import annotations

from typing import cast

import httpx

from odoo_mcp.adapters.base import CapabilitySnapshot, Company
from odoo_mcp.adapters.odoo.capabilities import detect_capabilities
from odoo_mcp.adapters.odoo.transports.base import OdooTransport
from odoo_mcp.adapters.odoo.transports.json2 import Json2Transport
from odoo_mcp.adapters.odoo.transports.json_rpc import JsonRpcTransport
from odoo_mcp.adapters.odoo.versioning import detect_major_version
from odoo_mcp.app.settings import OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError


class OdooClient:
    """The only component that selects and calls an Odoo API transport."""

    def __init__(
        self,
        connection: OdooConnectionSettings,
        version: int,
        transport: OdooTransport,
    ) -> None:
        self._connection = connection
        self._version = version
        self._transport = transport
        self._validated_company_ids: tuple[int, ...] | None = None

    @classmethod
    async def connect(
        cls,
        connection: OdooConnectionSettings,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> OdooClient:
        base_url = str(connection.url).rstrip("/")
        client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
            transport=http_transport,
        )
        try:
            version = await detect_major_version(connection, client)
            selected: OdooTransport
            if version == 18:
                selected = JsonRpcTransport(connection, client)
            else:
                selected = Json2Transport(connection, client)
            await selected.authenticate()
        except Exception:
            await client.aclose()
            raise
        return cls(connection, version, selected)

    async def get_capabilities(self) -> CapabilitySnapshot:
        if self._validated_company_ids is None:
            raise OdooMcpError(
                ErrorCode.ODOO_AUTH_FAILED,
                "Allowed companies must be validated before capability discovery.",
                "Validate the configured company access and retry.",
            )
        modules = await detect_capabilities(
            self._transport,
            company_ids=self._validated_company_ids,
        )
        if not modules.get("base"):
            raise OdooMcpError(
                ErrorCode.ODOO_AUTH_FAILED,
                "The technical user cannot access authorized companies.",
                "Grant least-privilege access to the allowed companies and retry.",
            )
        return CapabilitySnapshot(
            edition="enterprise",
            version=self._version,
            transport=self._transport.name,
            modules=modules,
        )

    async def get_companies(self) -> list[Company]:
        self._validated_company_ids = None
        allowed = self._connection.allowed_company_ids
        rows = await self._transport.search_read(
            "res.company",
            [["id", "in", list(allowed)]],
            ["id", "name"],
            limit=len(allowed),
        )
        companies: list[Company] = []
        for row in rows:
            identifier = row.get("id")
            name = row.get("name")
            if (
                isinstance(identifier, int)
                and not isinstance(identifier, bool)
                and isinstance(name, str)
            ):
                companies.append(Company(id=identifier, name=name))
        found = {company.id for company in companies}
        if found != set(allowed):
            raise OdooMcpError(
                ErrorCode.COMPANY_NOT_FOUND,
                "One or more allowed companies are unavailable to the technical user.",
                "Check allowed company IDs and Odoo company access, then retry.",
            )
        self._validated_company_ids = allowed
        return sorted(companies, key=lambda company: company.id)

    async def close(self) -> None:
        await self._transport.close()


def as_odoo_client(adapter: object) -> OdooClient:
    """Narrow helper used only by lifecycle-aware routing."""

    return cast(OdooClient, adapter)
