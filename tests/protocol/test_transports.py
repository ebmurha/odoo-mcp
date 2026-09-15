from __future__ import annotations

import sys
from dataclasses import dataclass

import pytest
from mcp import Client, StdioServerParameters
from starlette.testclient import TestClient

from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.connections import ConnectionBinding
from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings
from odoo_mcp.mcp.server import create_mcp_server


@dataclass
class Resolver:
    binding: ConnectionBinding

    async def resolve(self) -> ConnectionBinding:
        return self.binding


class FakeAdapter:
    async def get_capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            edition="enterprise",
            version=18,
            transport="json_rpc",
            modules={"base": True},
        )

    async def get_companies(self) -> list[Company]:
        return [Company(id=1, name="Synthetic Company")]


async def _factory(_connection: object) -> OdooAdapter:
    return FakeAdapter()


def test_streamable_http_lists_the_shared_registry(
    connection: OdooConnectionSettings,
) -> None:
    binding = ConnectionBinding(
        profile=DeploymentProfile.DEDICATED,
        tenant_id="tenant-http",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=frozenset({"core_read"}),
        connection=connection,
    )
    server = create_mcp_server(Resolver(binding), adapter_factory=_factory)
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host="127.0.0.1",
    )
    headers = {"Accept": "application/json, text/event-stream"}

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        initialize = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic-client", "version": "1"},
                },
            },
        )
        listed = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        called = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_erp_capabilities", "arguments": {}},
            },
        )

    assert initialize.status_code == 200
    assert listed.status_code == 200
    assert [tool["name"] for tool in listed.json()["result"]["tools"]] == ["get_erp_capabilities"]
    assert called.status_code == 200
    assert called.json()["result"]["structuredContent"]["status"] == "ok"


@pytest.mark.asyncio
async def test_stdio_profile_lists_the_shared_registry() -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odoo_mcp", "--profile", "local", "--transport", "stdio"],
        env={
            "ODOO_MCP_ODOO_URL": "https://odoo.invalid",
            "ODOO_MCP_ODOO_DATABASE": "synthetic-db",
            "ODOO_MCP_ODOO_USERNAME": "synthetic-user",
            "ODOO_MCP_ODOO_API_KEY": "synthetic-secret",
            "ODOO_MCP_ALLOWED_COMPANY_IDS": "1",
            "ODOO_MCP_DEFAULT_COMPANY_ID": "1",
        },
    )

    async with Client(parameters) as client:
        listed = await client.list_tools()

    assert [tool.name for tool in listed.tools] == ["get_erp_capabilities"]
