"""Runnable provider-neutral Shared Hosted ASGI composition."""

from __future__ import annotations

import html
import secrets
import sqlite3
import sys
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import AuthSettings as McpAuthSettings
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from odoo_mcp.adapters.base import OdooAdapter
from odoo_mcp.adapters.odoo.client import OdooClient
from odoo_mcp.adapters.odoo.connections import SharedHostedConnectionResolver
from odoo_mcp.adapters.odoo.enrollment import OdooEnrollmentValidator
from odoo_mcp.adapters.odoo.outbound_policy import SharedOutboundPolicy, shared_http_transport
from odoo_mcp.app.settings import (
    OdooConnectionSettings,
    OdooEnrollmentCredentials,
    SharedHostedSettings,
)
from odoo_mcp.app.shared_auth import VALID_SCOPES, CimdFetcher, SharedOAuthProvider
from odoo_mcp.app.shared_lifecycle import EnrollmentValidator, SharedConnectorLifecycle
from odoo_mcp.mcp.error_codes import OdooMcpError
from odoo_mcp.mcp.server import AdapterFactory, create_mcp_server
from odoo_mcp.storage import EncryptionKeyring, Storage
from odoo_mcp.storage.errors import StorageError

SESSION_COOKIE = "__Host-odoo-mcp-session"
ENROLLMENT_COOKIE = "__Host-odoo-mcp-enrollment"
CSRF_COOKIE = "__Host-odoo-mcp-csrf"
MAX_FORM_BYTES = 32 * 1024


class SingleWriterLease:
    """Hold an OS-level lock for the lifetime of one writable application."""

    def __init__(self, storage_path: Path) -> None:
        self._path = storage_path.resolve().with_suffix(storage_path.suffix + ".writer.lock")
        self._file: Any | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - exercised by container qualification
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("Shared Hosted storage already has an active writer") from None
        self._file = handle

    def release(self) -> None:
        handle = self._file
        if handle is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised by container qualification
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._file = None


class SharedRateLimitMiddleware:
    """Small in-process abuse hook; infrastructure may enforce stricter limits."""

    def __init__(self, app: ASGIApp, *, requests: int = 60, window_seconds: int = 60) -> None:
        self._app = app
        self._requests = requests
        self._window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith(
            ("/authorize", "/register", "/token", "/revoke", "/enroll")
        ):
            client = scope.get("client")
            key = "unknown" if client is None else str(client[0])
            now = time.monotonic()
            events = self._events[key]
            while events and events[0] <= now - self._window:
                events.popleft()
            if len(events) >= self._requests:
                response = JSONResponse({"error": "rate_limited"}, status_code=429)
                await response(scope, receive, send)
                return
            events.append(now)
        await self._app(scope, receive, send)


async def _form(request: Request) -> dict[str, list[str]]:
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise ValueError("Request body is too large")
    try:
        return parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        raise ValueError("Request form is invalid") from None


def _one(form: dict[str, list[str]], name: str) -> str:
    values = form.get(name, ())
    if len(values) != 1:
        raise ValueError("Request form is invalid")
    return values[0]


def _secure_cookie(response: Response, name: str, value: str) -> None:
    response.set_cookie(
        name,
        value,
        max_age=600,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _html_response(content: str) -> HTMLResponse:
    return HTMLResponse(
        content,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _csrf(request: Request, form: dict[str, list[str]]) -> None:
    expected = request.cookies.get(CSRF_COOKIE)
    supplied = _one(form, "csrf")
    if expected is None or not secrets.compare_digest(expected, supplied):
        raise ValueError("The authorization session is invalid")


def _credentials_form(csrf: str) -> str:
    escaped = html.escape(csrf, quote=True)
    return f"""<!doctype html><html><body><main>
<h1>Connect Odoo</h1>
<form method="post" action="/enroll/prepare" autocomplete="off">
<input type="hidden" name="csrf" value="{escaped}">
<label>Odoo URL <input name="url" type="url" required></label>
<label>Database <input name="database" required></label>
<label>Username <input name="username" required></label>
<label>API key <input name="api_key" type="password" required></label>
<button type="submit">Verify connection</button>
</form></main></body></html>"""


def _company_form(
    csrf: str,
    client_id: str,
    scopes: tuple[str, ...],
    companies: tuple[tuple[int, str], ...],
) -> str:
    options = "".join(
        f'<label><input type="checkbox" name="company_id" value="{identifier}">'
        f"{html.escape(name)}</label>"
        for identifier, name in companies
    )
    defaults = "".join(
        f'<option value="{identifier}">{html.escape(name)}</option>'
        for identifier, name in companies
    )
    return f"""<!doctype html><html><body><main>
<h1>Authorize Odoo MCP</h1>
<p>Client: {html.escape(client_id)}</p>
<p>Scopes: {html.escape(", ".join(scopes))}</p>
<form method="post" action="/enroll/commit">
<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">
{options}<label>Default company <select name="default_company_id">{defaults}</select></label>
<label><input type="checkbox" name="consent" value="yes" required>Approve this connector</label>
<button type="submit">Authorize</button>
</form></main></body></html>"""


def create_shared_app(
    settings: SharedHostedSettings,
    storage: Storage,
    *,
    validator: EnrollmentValidator | None = None,
    adapter_factory: AdapterFactory | None = None,
    outbound_policy: SharedOutboundPolicy | None = None,
    permissions: frozenset[str] = VALID_SCOPES,
) -> Starlette:
    """Compose OAuth, enrollment, and the unchanged MCP server in one process."""

    policy = outbound_policy or SharedOutboundPolicy()
    keyring = storage.keyring
    if keyring is None:
        raise ValueError("Shared Hosted requires an encryption keyring")
    lifecycle = SharedConnectorLifecycle(
        storage.database,
        storage.audit,
        keyring,
        validator or OdooEnrollmentValidator(policy),
    )
    lifecycle.expire_pending()
    provider = SharedOAuthProvider(
        storage.database,
        issuer_url=str(settings.issuer_url),
        resource_url=str(settings.public_mcp_url),
        cimd_fetcher=CimdFetcher(policy),
        valid_scopes=permissions,
        audit=storage.audit,
    )

    if adapter_factory is None:

        async def shared_adapter_factory(connection: object) -> OdooAdapter:
            if not isinstance(connection, OdooConnectionSettings):
                raise TypeError("Expected normalized Odoo connection settings")
            await policy.validate_url(str(connection.url))
            return await OdooClient.connect(
                connection, http_transport=shared_http_transport(policy)
            )

        selected_factory: AdapterFactory = shared_adapter_factory
    else:
        selected_factory = adapter_factory

    auth = McpAuthSettings(
        issuer_url=AnyHttpUrl(str(settings.issuer_url)),
        resource_server_url=AnyHttpUrl(str(settings.public_mcp_url)),
        required_scopes=["core_read"],
        validate_token_resource=True,
    )
    server = create_mcp_server(
        SharedHostedConnectionResolver(storage.connections),
        adapter_factory=selected_factory,
        storage=storage,
        auth=auth,
        token_verifier=provider,
    )
    public_mcp = urlsplit(str(settings.public_mcp_url))
    mcp_path = public_mcp.path or "/mcp"
    mcp_app = server.streamable_http_app(
        streamable_http_path=mcp_path,
        stateless_http=True,
        json_response=True,
        host=public_mcp.hostname or "127.0.0.1",
    )

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def enrollment(request: Request) -> Response:
        supplied = request.query_params.get("session")
        if supplied is not None:
            try:
                provider.session_summary(supplied)
            except ValueError:
                return JSONResponse({"error": "invalid_session"}, status_code=400)
            response = RedirectResponse("/enroll", status_code=303)
            _secure_cookie(response, SESSION_COOKIE, supplied)
            _secure_cookie(response, CSRF_COOKIE, _random_csrf())
            return response
        session = request.cookies.get(SESSION_COOKIE)
        csrf = request.cookies.get(CSRF_COOKIE)
        if session is None or csrf is None:
            return JSONResponse({"error": "invalid_session"}, status_code=400)
        try:
            summary = provider.session_summary(session)
        except ValueError:
            return JSONResponse({"error": "invalid_session"}, status_code=400)
        discovered = tuple(
            cast(int, value)
            for value in cast(tuple[object, ...], summary["discovered_company_ids"])
        )
        if discovered:
            companies = tuple((value, f"Company {value}") for value in discovered)
            content = _company_form(
                csrf,
                str(summary["client_id"]),
                tuple(str(value) for value in cast(tuple[object, ...], summary["scopes"])),
                companies,
            )
        else:
            content = _credentials_form(csrf)
        return _html_response(content)

    async def prepare(request: Request) -> Response:
        prepared_connector: tuple[str, str] | None = None
        try:
            form = await _form(request)
            _csrf(request, form)
            session = request.cookies[SESSION_COOKIE]
            provider.session_summary(session)
            credentials = OdooEnrollmentCredentials.model_validate(
                {
                    "url": _one(form, "url"),
                    "database": _one(form, "database"),
                    "username": _one(form, "username"),
                    "api_key": _one(form, "api_key"),
                }
            )
            enrollment_handle = f"enroll:{session}"
            prepared = await lifecycle.prepare_enrollment(
                credentials, enrollment_handle=enrollment_handle
            )
            prepared_connector = (prepared.tenant_id, prepared.connector_id)
            provider.bind_connector(session, prepared.connector_id)
            summary = provider.session_summary(session)
            companies = tuple((company.id, company.name) for company in prepared.companies)
            response = _html_response(
                _company_form(
                    request.cookies[CSRF_COOKIE],
                    str(summary["client_id"]),
                    tuple(str(value) for value in cast(tuple[object, ...], summary["scopes"])),
                    companies,
                )
            )
            _secure_cookie(response, ENROLLMENT_COOKIE, prepared.enrollment_handle)
            return response
        except (KeyError, ValueError, ValidationError, OdooMcpError, StorageError, sqlite3.Error):
            if prepared_connector is not None:
                lifecycle.discard_pending(*prepared_connector)
            return JSONResponse({"error": "enrollment_failed"}, status_code=400)

    async def commit(request: Request) -> Response:
        try:
            form = await _form(request)
            _csrf(request, form)
            session = request.cookies[SESSION_COOKIE]
            handle = f"enroll:{session}"
            cookie_handle = request.cookies.get(ENROLLMENT_COOKIE)
            if cookie_handle is not None and not secrets.compare_digest(cookie_handle, handle):
                raise ValueError("The enrollment handle is invalid")
            selected = tuple(int(value) for value in form.get("company_id", ()))
            default = int(_one(form, "default_company_id"))
            consent = _one(form, "consent") == "yes"
            if not consent:
                raise ValueError("Explicit consent is required")
            lifecycle.commit_enrollment(handle, selected, default)
            location = provider.activate_connector(session, consent=consent)
            response = RedirectResponse(location, status_code=303)
            for name in (SESSION_COOKIE, ENROLLMENT_COOKIE, CSRF_COOKIE):
                response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")
            return response
        except (KeyError, TypeError, ValueError, StorageError, sqlite3.Error):
            return JSONResponse({"error": "authorization_failed"}, status_code=400)

    async def revoke(request: Request) -> Response:
        try:
            form = await _form(request)
            provider.revoke_raw_token(_one(form, "client_id"), _one(form, "token"))
        except (ValueError, StorageError, sqlite3.Error):
            pass
        return Response(status_code=200, headers={"Cache-Control": "no-store"})

    async def replacement(request: Request) -> Response:
        authorization = request.headers.get("authorization", "")
        scheme, separator, raw_token = authorization.partition(" ")
        session = request.cookies.get(SESSION_COOKIE)
        if separator != " " or scheme.casefold() != "bearer" or session is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        trusted = provider.authorization_for_access_token(raw_token)
        if trusted is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            provider.authorize_replacement(session, trusted)
        except ValueError:
            return JSONResponse({"error": "invalid_session"}, status_code=400)
        return RedirectResponse("/enroll", status_code=303)

    auth_routes = create_auth_routes(
        provider,
        AnyHttpUrl(str(settings.issuer_url)),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=sorted(permissions),
            default_scopes=["core_read"],
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    authorization_handler = AuthorizationHandler(provider)

    async def authorize(request: Request) -> Response:
        if request.query_params.get("code_challenge_method") != "S256":
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "error_description": "S256 PKCE is required",
                },
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        return await authorization_handler.handle(request)

    auth_routes = [route for route in auth_routes if route.path != "/authorize"]

    @asynccontextmanager
    async def lifespan(_application: Starlette) -> AsyncIterator[None]:
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    routes = [
        Route("/healthz", health, methods=["GET"]),
        Route("/revoke", revoke, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET"]),
        *auth_routes,
        Route("/enroll", enrollment, methods=["GET"]),
        Route("/enroll/prepare", prepare, methods=["POST"]),
        Route("/enroll/commit", commit, methods=["POST"]),
        Route("/enroll/replacement", replacement, methods=["POST"]),
        Mount("/", app=mcp_app),
    ]
    return Starlette(routes=routes, lifespan=lifespan, middleware=[])


def _random_csrf() -> str:
    return secrets.token_urlsafe(32)


def open_shared_app(
    settings: SharedHostedSettings,
    storage_path: Path,
    *,
    validator: EnrollmentValidator | None = None,
    permissions: frozenset[str] = VALID_SCOPES,
) -> tuple[Starlette, SingleWriterLease]:
    lease = SingleWriterLease(storage_path)
    lease.acquire()
    try:
        keyring = EncryptionKeyring(settings.active_key_version, settings.encryption_keys)
        storage = Storage.open(storage_path, keyring=keyring)
        app = create_shared_app(
            settings,
            storage,
            validator=validator,
            permissions=permissions,
        )
        app.state.writer_lease = lease
        return app, lease
    except BaseException:
        lease.release()
        raise


def protect_shared_app(app: ASGIApp) -> ASGIApp:
    return SharedRateLimitMiddleware(app)
