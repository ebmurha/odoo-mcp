"""MCP validation and routing over the shared deterministic registry."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from mcp.server import MCPServer

from odoo_mcp.adapters.base import OdooAdapter
from odoo_mcp.adapters.odoo.client import OdooClient
from odoo_mcp.adapters.odoo.connections import ConnectionResolver
from odoo_mcp.mcp.error_codes import ErrorCode, ErrorResponse, OdooMcpError
from odoo_mcp.mcp.registry import TOOL_REGISTRY
from odoo_mcp.mcp.schemas import CapabilitiesToolResponse
from odoo_mcp.workflows.core.capabilities import get_erp_capabilities

AdapterFactory = Callable[[object], Awaitable[OdooAdapter]]
LOGGER = logging.getLogger(__name__)


async def _default_adapter_factory(connection: object) -> OdooAdapter:
    from odoo_mcp.app.settings import OdooConnectionSettings

    if not isinstance(connection, OdooConnectionSettings):
        raise TypeError("Expected normalized Odoo connection settings")
    return await OdooClient.connect(connection)


async def _close_adapter(adapter: OdooAdapter) -> None:
    close = getattr(adapter, "close", None)
    if close is not None:
        result = close()
        if inspect.isawaitable(result):
            await result


def create_mcp_server(
    resolver: ConnectionResolver,
    *,
    adapter_factory: AdapterFactory = _default_adapter_factory,
) -> MCPServer:
    """Build the one server used by every deployment profile."""

    server = MCPServer("odoo-mcp")
    definition = TOOL_REGISTRY[0]

    async def capabilities_tool() -> CapabilitiesToolResponse:
        request_id = f"req_{uuid4().hex}"
        adapter: OdooAdapter | None = None
        try:
            binding = await resolver.resolve()
            if definition.required_permission not in binding.permissions:
                raise OdooMcpError(
                    ErrorCode.ODOO_AUTH_FAILED,
                    "The resolved MCP identity is not authorized for ERP discovery.",
                    "Reconnect with core discovery permission and retry.",
                )
            adapter = await adapter_factory(binding.connection)
            response = CapabilitiesToolResponse.from_success(
                await get_erp_capabilities(
                    adapter,
                    permissions=binding.permissions,
                    default_company_id=binding.connection.default_company_id,
                    tools=tuple(tool.availability() for tool in TOOL_REGISTRY),
                    request_id=request_id,
                )
            )
        except OdooMcpError as exc:
            response = CapabilitiesToolResponse.from_error(exc.as_response(request_id))
        except Exception:
            response = CapabilitiesToolResponse.from_error(
                ErrorResponse(
                    error_code=ErrorCode.UNKNOWN_ERROR,
                    error_message="The capability request failed unexpectedly.",
                    remediation_hint="Retry the request or contact the service operator.",
                    request_id=request_id,
                )
            )
        if adapter is not None:
            try:
                await _close_adapter(adapter)
            except Exception:
                LOGGER.warning("Odoo adapter cleanup failed; details were suppressed.")
                if response.status == "ok":
                    response = CapabilitiesToolResponse.from_error(
                        ErrorResponse(
                            error_code=ErrorCode.UNKNOWN_ERROR,
                            error_message="The capability request failed unexpectedly.",
                            remediation_hint=("Retry the request or contact the service operator."),
                            request_id=request_id,
                        )
                    )
        return response

    server.add_tool(
        capabilities_tool,
        name=definition.name,
        title=definition.title,
        description=definition.description,
        annotations=definition.annotations,
        meta=definition.protocol_meta(),
        structured_output=True,
    )
    return server
