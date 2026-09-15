"""ERP capability discovery workflow."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from odoo_mcp.adapters.base import OdooAdapter
from odoo_mcp.mcp.schemas import (
    AuthorizedCompany,
    CapabilitiesResponse,
    CapabilityItem,
    OdooRuntimeInfo,
)


@dataclass(frozen=True)
class ToolAvailability:
    name: str
    required_permission: str
    required_capability: str | None


async def get_erp_capabilities(
    adapter: OdooAdapter,
    *,
    permissions: frozenset[str],
    default_company_id: int,
    tools: tuple[ToolAvailability, ...],
    request_id: str | None = None,
) -> CapabilitiesResponse:
    """Return deterministic, authorized discovery data."""

    snapshot = await adapter.get_capabilities()
    companies = await adapter.get_companies()
    available_tools = sorted(
        tool.name
        for tool in tools
        if tool.required_permission in permissions
        and (
            tool.required_capability is None
            or snapshot.modules.get(tool.required_capability, False)
        )
    )
    return CapabilitiesResponse(
        request_id=request_id or f"req_{uuid4().hex}",
        odoo=OdooRuntimeInfo(
            edition=snapshot.edition,
            major_version=snapshot.version,
            transport=snapshot.transport,
        ),
        installed_modules=[
            CapabilityItem(name=name, available=available)
            for name, available in sorted(snapshot.modules.items())
        ],
        available_tools=available_tools,
        authorized_companies=[
            AuthorizedCompany(
                id=company.id,
                name=company.name,
                is_default=company.id == default_company_id,
            )
            for company in companies
        ],
    )
