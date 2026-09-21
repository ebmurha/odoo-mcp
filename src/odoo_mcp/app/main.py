"""Console entry point for stdio and Streamable HTTP deployment profiles."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import NoReturn

import uvicorn
from starlette.types import ASGIApp

from odoo_mcp.adapters.odoo.connections import (
    ConnectionBinding,
    ConnectionResolver,
    ConnectorAuthorization,
    DedicatedConnectionResolver,
    EncryptedConnectionRepository,
    SharedHostedConnectionResolver,
    StaticConnectionResolver,
)
from odoo_mcp.app.remote_auth import load_dedicated_auth_settings, protect_dedicated_app
from odoo_mcp.app.settings import (
    DeploymentProfile,
    OdooConnectionSettings,
    PermissionConfig,
    SettingsError,
    load_permission_config,
    load_settings,
)
from odoo_mcp.mcp.registry import TOOL_REGISTRY
from odoo_mcp.mcp.server import create_mcp_server
from odoo_mcp.storage import Storage


class _UnavailableSharedRepository:
    async def resolve_authorized(
        self,
        authorization: ConnectorAuthorization,
    ) -> OdooConnectionSettings | None:
        return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="odoo-mcp")
    parser.add_argument(
        "--profile",
        choices=[profile.value for profile in DeploymentProfile],
        default=DeploymentProfile.LOCAL.value,
    )
    parser.add_argument("--transport", choices=["stdio", "streamable-http"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--storage", type=Path, default=Path(".odoo-mcp/state.sqlite3"))
    return parser


def _fail(parser: argparse.ArgumentParser, message: str) -> NoReturn:
    parser.error(message)


def _permissions(config: PermissionConfig | None) -> frozenset[str]:
    if config is None:
        return frozenset({"core_read"})

    definitions = {tool.name: tool for tool in TOOL_REGISTRY}
    known_permissions = {tool.required_permission for tool in TOOL_REGISTRY}
    for permission, tool_names in config.permissions.items():
        if permission not in known_permissions:
            raise SettingsError("Permission configuration does not match the tool registry")
        for tool_name in tool_names:
            definition = definitions.get(tool_name)
            if definition is None or definition.required_permission != permission:
                raise SettingsError("Permission configuration does not match the tool registry")

    return frozenset(
        definition.required_permission
        for definition in TOOL_REGISTRY
        if definition.name in config.permissions.get(definition.required_permission, ())
    )


def build_resolver(
    profile: DeploymentProfile,
    *,
    config: PermissionConfig | None = None,
    shared_repository: EncryptedConnectionRepository | None = None,
) -> ConnectionResolver:
    """Create the profile-specific resolver behind one normalized contract."""

    settings = load_settings(profile)
    if profile is DeploymentProfile.SHARED:
        repository = shared_repository or _UnavailableSharedRepository()
        return SharedHostedConnectionResolver(repository)
    if settings.connection is None:
        raise SettingsError("The deployment profile has no Odoo connection")
    if profile is DeploymentProfile.DEDICATED:
        return DedicatedConnectionResolver(
            tenant_id="deployment:dedicated",
            permissions=_permissions(config),
            connection=settings.connection,
        )
    return StaticConnectionResolver(
        ConnectionBinding(
            profile=profile,
            tenant_id=f"deployment:{profile.value}",
            authenticated_subject=f"{profile.value}-process",
            mcp_client="deployment",
            permissions=_permissions(config),
            connection=settings.connection,
        )
    )


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    profile = DeploymentProfile(args.profile)
    transport = args.transport or (
        "stdio" if profile is DeploymentProfile.LOCAL else "streamable-http"
    )
    if profile is DeploymentProfile.LOCAL and transport != "stdio":
        _fail(parser, "Local Development requires stdio")
    if profile is not DeploymentProfile.LOCAL and transport != "streamable-http":
        _fail(parser, "Remote profiles require Streamable HTTP")
    try:
        config = load_permission_config(args.config) if args.config else None
        resolver = build_resolver(profile, config=config)
    except SettingsError as exc:
        _fail(parser, str(exc))
    server = create_mcp_server(resolver, storage=Storage.open(args.storage))
    if transport == "stdio":
        server.run(transport="stdio")
    else:
        app: ASGIApp = server.streamable_http_app(
            host=args.host,
            stateless_http=True,
            json_response=True,
        )
        if profile is DeploymentProfile.DEDICATED:
            try:
                app = protect_dedicated_app(app, load_dedicated_auth_settings())
            except SettingsError as exc:
                _fail(parser, str(exc))
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
