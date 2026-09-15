from __future__ import annotations

from dataclasses import dataclass

from mcp import Client

from odoo_mcp.adapters.base import OdooAdapter
from odoo_mcp.adapters.odoo.connections import ConnectionBinding
from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings
from odoo_mcp.mcp.server import create_mcp_server


@dataclass
class Resolver:
    binding: ConnectionBinding

    async def resolve(self) -> ConnectionBinding:
        return self.binding


async def test_unexpected_failure_is_structured_and_secret_safe(
    connection: OdooConnectionSettings,
) -> None:
    async def factory(_connection: object) -> OdooAdapter:
        raise RuntimeError("raw failure synthetic-secret")

    binding = ConnectionBinding(
        profile=DeploymentProfile.DEDICATED,
        tenant_id="tenant-dedicated",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=frozenset({"core_read"}),
        connection=connection,
    )
    server = create_mcp_server(Resolver(binding), adapter_factory=factory)
    async with Client(server) as client:
        result = await client.call_tool("get_erp_capabilities", {})

    assert result.structured_content is not None
    payload = result.structured_content
    assert payload["status"] == "failed"
    assert payload["error_code"] == "UNKNOWN_ERROR"
    assert payload["request_id"].startswith("req_")
    assert "synthetic-secret" not in str(payload)
