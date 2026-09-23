"""Deployment-profile connection resolution."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Protocol

from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import BaseModel, ConfigDict, Field

from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError


class ConnectorAuthorization(BaseModel):
    """Identity facts supplied by an already-authenticated connector boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1)
    connection_id: str = Field(min_length=1)
    authenticated_subject: str = Field(min_length=1)
    mcp_client: str = Field(min_length=1)
    permissions: frozenset[str]


class RemoteIdentity(BaseModel):
    """Validated immutable identity for one Dedicated Remote request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1)
    client_id: str = Field(min_length=1)


class ConnectionBinding(BaseModel):
    """Resolved isolation identity and normalized Odoo connection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: DeploymentProfile
    tenant_id: str = Field(min_length=1)
    authenticated_subject: str = Field(min_length=1)
    mcp_client: str = Field(min_length=1)
    permissions: frozenset[str]
    connection: OdooConnectionSettings


class ConnectionResolver(Protocol):
    async def resolve(self) -> ConnectionBinding: ...


class EncryptedConnectionRepository(Protocol):
    """Persistence boundary implemented by the storage step."""

    async def resolve_authorized(
        self, authorization: ConnectorAuthorization
    ) -> OdooConnectionSettings | None: ...


_connector_authorization: ContextVar[ConnectorAuthorization | None] = ContextVar(
    "odoo_mcp_connector_authorization",
    default=None,
)
_remote_identity: ContextVar[RemoteIdentity | None] = ContextVar(
    "odoo_mcp_remote_identity", default=None
)


def set_connector_authorization(
    value: ConnectorAuthorization,
) -> Token[ConnectorAuthorization | None]:
    return _connector_authorization.set(value)


def reset_connector_authorization(token: Token[ConnectorAuthorization | None]) -> None:
    _connector_authorization.reset(token)


def set_remote_identity(value: RemoteIdentity) -> Token[RemoteIdentity | None]:
    return _remote_identity.set(value)


def reset_remote_identity(token: Token[RemoteIdentity | None]) -> None:
    _remote_identity.reset(token)


class StaticConnectionResolver:
    """Resolver for Local Development and Dedicated Remote."""

    def __init__(self, binding: ConnectionBinding) -> None:
        if binding.profile is DeploymentProfile.SHARED:
            raise ValueError("Static connections cannot be used for Shared Hosted")
        self._binding = binding

    async def resolve(self) -> ConnectionBinding:
        return self._binding


class DedicatedConnectionResolver:
    """Bind a validated remote subject to one deployment-owned connection."""

    def __init__(
        self,
        *,
        tenant_id: str,
        permissions: frozenset[str],
        connection: OdooConnectionSettings,
    ) -> None:
        self._tenant_id = tenant_id
        self._permissions = permissions
        self._connection = connection

    async def resolve(self) -> ConnectionBinding:
        identity = _remote_identity.get()
        if identity is None:
            raise OdooMcpError(
                ErrorCode.ODOO_AUTH_FAILED,
                "An authenticated remote identity is required.",
                "Reconnect with a valid bearer token and retry.",
            )
        return ConnectionBinding(
            profile=DeploymentProfile.DEDICATED,
            tenant_id=self._tenant_id,
            authenticated_subject=identity.subject,
            mcp_client=identity.client_id,
            permissions=self._permissions,
            connection=self._connection,
        )


class SharedHostedConnectionResolver:
    """Fail-closed resolver for authenticated connector contexts."""

    def __init__(self, repository: EncryptedConnectionRepository) -> None:
        self._repository = repository

    async def resolve(self) -> ConnectionBinding:
        authorization = _connector_authorization.get()
        if authorization is None:
            access_token = get_access_token()
            claims = None if access_token is None else access_token.claims
            if access_token is not None and isinstance(claims, dict):
                try:
                    authorization = ConnectorAuthorization(
                        tenant_id=claims["tenant_id"],
                        connection_id=claims["connection_id"],
                        authenticated_subject=access_token.subject or "connector",
                        mcp_client=access_token.client_id,
                        permissions=claims["permissions"],
                    )
                except (KeyError, ValueError):
                    authorization = None
        if authorization is None:
            raise OdooMcpError(
                ErrorCode.ODOO_AUTH_FAILED,
                "An authenticated connector context is required.",
                "Reconnect the MCP connector and retry.",
            )
        connection = await self._repository.resolve_authorized(authorization)
        if connection is None:
            raise OdooMcpError(
                ErrorCode.ODOO_AUTH_FAILED,
                "The connector is not authorized for an Odoo connection.",
                "Reconnect the MCP connector or contact the service operator.",
            )
        return ConnectionBinding(
            profile=DeploymentProfile.SHARED,
            tenant_id=authorization.tenant_id,
            authenticated_subject=authorization.authenticated_subject,
            mcp_client=authorization.mcp_client,
            permissions=authorization.permissions,
            connection=connection,
        )
