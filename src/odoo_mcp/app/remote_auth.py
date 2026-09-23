"""Fail-closed authentication for the Dedicated Remote HTTP profile."""

from __future__ import annotations

import os
from typing import Any

import jwt
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from odoo_mcp.adapters.odoo.connections import (
    RemoteIdentity,
    reset_remote_identity,
    set_remote_identity,
)
from odoo_mcp.app.settings import SettingsError


class DedicatedAuthSettings(BaseModel):
    """Server-owned validation contract for deployment-issued JWTs."""

    model_config = ConfigDict(frozen=True)

    issuer: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    signing_key: SecretStr

    @field_validator("issuer", "audience")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("signing_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("must contain at least 32 characters")
        return value


def load_dedicated_auth_settings() -> DedicatedAuthSettings:
    """Load authentication settings without returning configured values."""

    try:
        return DedicatedAuthSettings(
            issuer=os.environ.get("ODOO_MCP_AUTH_ISSUER", ""),
            audience=os.environ.get("ODOO_MCP_AUTH_AUDIENCE", ""),
            signing_key=SecretStr(os.environ.get("ODOO_MCP_AUTH_SIGNING_KEY", "")),
        )
    except ValidationError:
        raise SettingsError("Missing or invalid Dedicated Remote authentication setting") from None


class DedicatedAuthMiddleware:
    """Authenticate `/mcp` bearer requests before protocol routing."""

    def __init__(self, app: ASGIApp, settings: DedicatedAuthSettings) -> None:
        self._app = app
        self._settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/mcp":
            await self._app(scope, receive, send)
            return
        authorization_headers = [
            value for key, value in scope.get("headers", ()) if key.lower() == b"authorization"
        ]
        if len(authorization_headers) != 1:
            await self._reject(scope, receive, send)
            return
        authorization = authorization_headers[0].decode("latin-1")
        scheme, separator, encoded = authorization.partition(" ")
        if separator != " " or scheme.casefold() != "bearer" or not encoded:
            await self._reject(scope, receive, send)
            return
        try:
            claims: dict[str, Any] = jwt.decode(
                encoded,
                self._settings.signing_key.get_secret_value(),
                algorithms=["HS256"],
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "client_id"]},
            )
            subject = claims.get("sub")
            client_id = claims.get("client_id")
            if not isinstance(subject, str) or not isinstance(client_id, str):
                raise jwt.InvalidTokenError
            identity = RemoteIdentity(subject=subject, client_id=client_id)
        except (jwt.PyJWTError, ValidationError):
            await self._reject(scope, receive, send)
            return
        token = set_remote_identity(identity)
        try:
            await self._app(scope, receive, send)
        finally:
            reset_remote_identity(token)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"error": "unauthorized"}, status_code=401)
        await response(scope, receive, send)


def protect_dedicated_app(app: ASGIApp, settings: DedicatedAuthSettings) -> ASGIApp:
    return DedicatedAuthMiddleware(app, settings)
