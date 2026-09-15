from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest
from mcp import Client

from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.connections import ConnectionBinding
from odoo_mcp.app.settings import (
    DeploymentProfile,
    OdooConnectionSettings,
    load_permission_config,
)
from odoo_mcp.mcp.registry import TOOL_REGISTRY
from odoo_mcp.mcp.server import create_mcp_server

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Resolver:
    binding: ConnectionBinding

    async def resolve(self) -> ConnectionBinding:
        return self.binding


class FakeAdapter:
    def __init__(self) -> None:
        self.closed = False

    async def get_capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            edition="enterprise",
            version=19,
            transport="json2",
            modules={"base": True, "account": True},
        )

    async def get_companies(self) -> list[Company]:
        return [Company(id=1, name="Synthetic Company")]

    async def close(self) -> None:
        self.closed = True


def _binding(
    profile: DeploymentProfile,
    connection: OdooConnectionSettings,
    permissions: frozenset[str] = frozenset({"core_read"}),
) -> ConnectionBinding:
    return ConnectionBinding(
        profile=profile,
        tenant_id=f"tenant-{profile.value}",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=permissions,
        connection=connection,
    )


async def test_all_profiles_expose_identical_registry_and_discovery(
    connection: OdooConnectionSettings,
) -> None:
    contracts: list[list[dict[str, object]]] = []
    outputs: list[dict[str, object]] = []
    adapters: list[FakeAdapter] = []

    for profile in DeploymentProfile:

        async def factory(_connection: object) -> OdooAdapter:
            adapter = FakeAdapter()
            adapters.append(adapter)
            return adapter

        server = create_mcp_server(Resolver(_binding(profile, connection)), adapter_factory=factory)
        async with Client(server) as client:
            listed = await client.list_tools()
            contracts.append([tool.model_dump(by_alias=True) for tool in listed.tools])
            result = await client.call_tool("get_erp_capabilities", {})
            assert result.is_error is False
            assert result.structured_content is not None
            outputs.append(result.structured_content)

    assert contracts[0] == contracts[1] == contracts[2]
    assert [tool["name"] for tool in contracts[0]] == ["get_erp_capabilities"]
    for output in outputs:
        output.pop("request_id")
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0]["available_tools"] == ["get_erp_capabilities"]
    assert outputs[0]["authorized_companies"] == [
        {"id": 1, "name": "Synthetic Company", "is_default": True}
    ]
    assert all(adapter.closed for adapter in adapters)


async def test_registry_metadata_and_schemas_match_the_contract(
    connection: OdooConnectionSettings,
) -> None:
    server = create_mcp_server(Resolver(_binding(DeploymentProfile.LOCAL, connection)))
    tools = await server.list_tools()

    assert len(TOOL_REGISTRY) == 1
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "get_erp_capabilities"
    assert tool.input_schema["type"] == "object"
    assert tool.input_schema.get("properties") == {}
    assert tool.output_schema is not None
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is True
    assert tool.meta == {
        "toolVersion": "1.0.0",
        "riskLevel": "read",
        "requiredPermission": "core_read",
        "requiredCapability": None,
    }


async def test_permission_denial_happens_before_adapter_creation(
    connection: OdooConnectionSettings,
) -> None:
    called = False

    async def factory(_connection: object) -> OdooAdapter:
        nonlocal called
        called = True
        return FakeAdapter()

    server = create_mcp_server(
        Resolver(_binding(DeploymentProfile.SHARED, connection, frozenset())),
        adapter_factory=factory,
    )
    async with Client(server) as client:
        result = await client.call_tool("get_erp_capabilities", {})

    assert called is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "failed"
    assert result.structured_content["error_code"] == "ODOO_AUTH_FAILED"


async def test_adapter_close_failure_is_structured_and_secret_safe(
    connection: OdooConnectionSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "raw-close-synthetic-secret"

    class CloseFailureAdapter(FakeAdapter):
        async def close(self) -> None:
            raise RuntimeError(marker)

    async def factory(_connection: object) -> OdooAdapter:
        return CloseFailureAdapter()

    server = create_mcp_server(
        Resolver(_binding(DeploymentProfile.LOCAL, connection)),
        adapter_factory=factory,
    )
    with caplog.at_level(logging.DEBUG):
        async with Client(server) as client:
            result = await client.call_tool("get_erp_capabilities", {})

    assert result.structured_content is not None
    assert result.structured_content["status"] == "failed"
    assert result.structured_content["error_code"] == "UNKNOWN_ERROR"
    assert marker not in str(result.structured_content)
    assert marker not in caplog.text
    assert "Traceback" not in caplog.text


def test_public_permission_example_matches_registry() -> None:
    config = load_permission_config(ROOT / "config" / "config.example.yaml")
    mapped = {permission: set(tool_names) for permission, tool_names in config.permissions.items()}

    assert mapped == {"core_read": {"get_erp_capabilities"}}
