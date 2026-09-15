from __future__ import annotations

import pytest
from mcp import Client

from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.connections import (
    ConnectorAuthorization,
    SharedHostedConnectionResolver,
    reset_connector_authorization,
    set_connector_authorization,
)
from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.server import create_mcp_server


class Repository:
    def __init__(self, connection: OdooConnectionSettings | None) -> None:
        self.connection = connection
        self.seen: ConnectorAuthorization | None = None

    async def resolve_authorized(
        self, authorization: ConnectorAuthorization
    ) -> OdooConnectionSettings | None:
        self.seen = authorization
        return self.connection


async def test_shared_hosted_binds_authorized_connector_to_one_connection(
    connection: OdooConnectionSettings,
) -> None:
    repository = Repository(connection)
    resolver = SharedHostedConnectionResolver(repository)
    authorization = ConnectorAuthorization(
        tenant_id="tenant-a",
        connection_id="connection-a",
        authenticated_subject="subject-a",
        mcp_client="synthetic-client",
        permissions=frozenset({"core_read"}),
    )
    token = set_connector_authorization(authorization)
    try:
        binding = await resolver.resolve()
    finally:
        reset_connector_authorization(token)

    assert repository.seen == authorization
    assert binding.profile is DeploymentProfile.SHARED
    assert binding.tenant_id == "tenant-a"
    assert binding.connection is connection


async def test_shared_hosted_fails_closed_without_connector_context(
    connection: OdooConnectionSettings,
) -> None:
    resolver = SharedHostedConnectionResolver(Repository(connection))

    with pytest.raises(OdooMcpError) as caught:
        await resolver.resolve()

    assert caught.value.code is ErrorCode.ODOO_AUTH_FAILED


async def test_shared_hosted_connector_context_reaches_discovery(
    connection: OdooConnectionSettings,
) -> None:
    class FakeAdapter:
        async def get_capabilities(self) -> CapabilitySnapshot:
            return CapabilitySnapshot(
                edition="enterprise",
                version=19,
                transport="json2",
                modules={"base": True},
            )

        async def get_companies(self) -> list[Company]:
            return [Company(id=1, name="Synthetic Company")]

    async def factory(_connection: object) -> OdooAdapter:
        return FakeAdapter()

    authorization = ConnectorAuthorization(
        tenant_id="tenant-a",
        connection_id="connection-a",
        authenticated_subject="subject-a",
        mcp_client="synthetic-client",
        permissions=frozenset({"core_read"}),
    )
    resolver = SharedHostedConnectionResolver(Repository(connection))
    server = create_mcp_server(resolver, adapter_factory=factory)
    token = set_connector_authorization(authorization)
    try:
        async with Client(server) as client:
            result = await client.call_tool("get_erp_capabilities", {})
    finally:
        reset_connector_authorization(token)

    assert result.structured_content is not None
    assert result.structured_content["status"] == "ok"
    assert result.structured_content["authorized_companies"] == [
        {"id": 1, "name": "Synthetic Company", "is_default": True}
    ]
