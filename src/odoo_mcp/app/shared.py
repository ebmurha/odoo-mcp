"""Runnable provider-neutral Shared Hosted ASGI composition."""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import secrets
import sys
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import build_metadata, create_auth_routes
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
    StorageKind,
)
from odoo_mcp.app.shared_auth import VALID_SCOPES, CimdFetcher, SharedOAuthProvider
from odoo_mcp.app.shared_lifecycle import EnrollmentValidator, SharedConnectorLifecycle
from odoo_mcp.mcp.error_codes import OdooMcpError
from odoo_mcp.mcp.server import AdapterFactory, create_mcp_server
from odoo_mcp.storage import EncryptionKeyring, Storage
from odoo_mcp.storage.database import DATABASE_ERRORS
from odoo_mcp.storage.errors import StorageError

SESSION_COOKIE = "__Secure-odoo-mcp-session"
ENROLLMENT_COOKIE = "__Secure-odoo-mcp-enrollment"
LOGGER = logging.getLogger(__name__)

ENROLLMENT_CSS = """
:root {
  color-scheme: light;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", sans-serif;
  color: #271b2d;
  background: #f6f2f8;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 32px 18px;
  background:
    radial-gradient(circle at top left, #f0dff4 0, transparent 34rem),
    #f6f2f8;
}
main {
  width: min(100%, 640px);
  overflow: hidden;
  background: #fff;
  border: 1px solid #e8ddea;
  border-radius: 20px;
  box-shadow: 0 24px 70px rgb(66 36 75 / 12%);
}
.masthead { padding: 28px 32px 0; }
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #5f2867;
  font-size: 15px;
  font-weight: 750;
  letter-spacing: .01em;
}
.brand-mark {
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border-radius: 10px;
  color: #fff;
  background: linear-gradient(145deg, #71347b, #a34da4);
  box-shadow: 0 6px 16px rgb(113 52 123 / 24%);
}
.steps {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 24px;
}
.step { height: 4px; border-radius: 999px; background: #eadfeb; }
.step.active { background: linear-gradient(90deg, #73347c, #b24c9b); }
.content { padding: 28px 32px 32px; }
.eyebrow {
  margin: 0 0 8px;
  color: #7b4a82;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: .12em;
  text-transform: uppercase;
}
h1 { margin: 0; font-size: clamp(26px, 5vw, 34px); line-height: 1.15; }
.intro { margin: 12px 0 26px; color: #6c6070; line-height: 1.6; }
.field { display: grid; gap: 7px; margin-top: 18px; }
.field-label, legend { font-size: 14px; font-weight: 750; }
.hint { color: #7b707f; font-size: 12px; line-height: 1.45; }
input, select {
  width: 100%;
  min-height: 46px;
  padding: 10px 12px;
  color: #271b2d;
  background: #fff;
  border: 1px solid #cfc1d2;
  border-radius: 10px;
  font: inherit;
}
input:focus, select:focus {
  outline: 3px solid rgb(154 72 157 / 16%);
  border-color: #8a3f92;
}
fieldset { min-width: 0; margin: 22px 0 0; padding: 0; border: 0; }
legend { margin-bottom: 6px; }
.choices { display: grid; gap: 10px; margin-top: 12px; }
.choice {
  display: flex;
  align-items: flex-start;
  gap: 11px;
  padding: 13px 14px;
  border: 1px solid #ded2e0;
  border-radius: 12px;
  background: #fcfafc;
}
.choice input { width: 18px; min-height: 18px; margin: 2px 0 0; accent-color: #7b3484; }
.choice strong, .choice small { display: block; }
.choice small { margin-top: 3px; color: #7b707f; }
.meta {
  display: grid;
  gap: 8px;
  margin: 20px 0 0;
  padding: 14px;
  border-radius: 12px;
  background: #f7f2f8;
  color: #5f5263;
  font-size: 13px;
  overflow-wrap: anywhere;
}
.meta code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
.notice {
  margin: 0 0 22px;
  padding: 13px 14px;
  border-left: 4px solid #a33d62;
  border-radius: 8px;
  color: #70253f;
  background: #fff1f5;
  line-height: 1.5;
}
.consent { margin-top: 22px; }
.actions { display: flex; align-items: center; gap: 14px; margin-top: 26px; }
button, .button {
  min-height: 46px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 11px 18px;
  border: 0;
  border-radius: 11px;
  color: #fff;
  background: linear-gradient(135deg, #71347b, #9e438e);
  box-shadow: 0 8px 20px rgb(113 52 123 / 20%);
  font: inherit;
  font-weight: 750;
  text-decoration: none;
  cursor: pointer;
}
button:disabled {
  cursor: wait;
  opacity: .78;
}
button[aria-busy="true"]::before {
  width: 14px;
  height: 14px;
  margin-right: 9px;
  border: 2px solid rgb(255 255 255 / 45%);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
button:hover, .button:hover { background: #642d6d; }
.privacy { margin: 22px 0 0; color: #817486; font-size: 12px; line-height: 1.5; }
@media (max-width: 520px) {
  body { padding: 0; place-items: stretch; }
  main { min-height: 100vh; border: 0; border-radius: 0; }
  .masthead { padding: 24px 22px 0; }
  .content { padding: 26px 22px; }
  .actions, button, .button { width: 100%; }
}
""".strip()
ENROLLMENT_STYLE_HASH = base64.b64encode(
    hashlib.sha256(ENROLLMENT_CSS.encode()).digest()
).decode()
ENROLLMENT_SCRIPT = """
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (form.dataset.submitting === "true") {
    event.preventDefault();
    return;
  }
  form.dataset.submitting = "true";
  const button = form.querySelector('button[type="submit"]');
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = button.dataset.pendingLabel || "Working...";
  }
});
""".strip()
ENROLLMENT_SCRIPT_HASH = base64.b64encode(
    hashlib.sha256(ENROLLMENT_SCRIPT.encode()).digest()
).decode()
CSRF_COOKIE = "__Secure-odoo-mcp-csrf"
MAX_FORM_BYTES = 32 * 1024
SHARED_DATABASE_ERRORS = (StorageError, *DATABASE_ERRORS)


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

    def __init__(
        self,
        app: ASGIApp,
        *,
        base_path: str = "",
        requests: int = 60,
        window_seconds: int = 60,
    ) -> None:
        self._app = app
        self._requests = requests
        self._window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._paths = tuple(
            f"{base_path}{path}"
            for path in ("/authorize", "/register", "/token", "/revoke", "/enroll")
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith(self._paths):
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


def _secure_cookie(response: Response, name: str, value: str, path: str) -> None:
    response.set_cookie(
        name,
        value,
        max_age=600,
        secure=True,
        httponly=True,
        samesite="lax",
        path=path,
    )


def _callback_csp_source(redirect_uri: str) -> str:
    parsed = urlsplit(redirect_uri)
    if parsed.scheme in {"https", "http"} and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    if parsed.scheme and not parsed.netloc:
        return f"{parsed.scheme}:"
    raise ValueError("The authorization redirect URI is invalid")


def _html_response(
    content: str,
    *,
    status_code: int = 200,
    redirect_uri: str | None = None,
) -> HTMLResponse:
    form_action = "'self'"
    if redirect_uri is not None:
        form_action += f" {_callback_csp_source(redirect_uri)}"
    return HTMLResponse(
        content,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; "
                f"style-src 'sha256-{ENROLLMENT_STYLE_HASH}'; "
                f"script-src 'sha256-{ENROLLMENT_SCRIPT_HASH}'; "
                f"form-action {form_action}; base-uri 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _page(
    *,
    step: int,
    eyebrow: str,
    title: str,
    introduction: str,
    body: str,
    error: str | None = None,
) -> str:
    error_html = f'<div class="notice" role="alert">{html.escape(error)}</div>' if error else ""
    second_step = " active" if step == 2 else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · Odoo MCP</title>
<style>{ENROLLMENT_CSS}</style>
</head>
<body>
<main>
<div class="masthead">
<div class="brand"><span class="brand-mark" aria-hidden="true">O</span> Odoo MCP</div>
<div class="steps" aria-label="Enrollment progress">
<span class="step active"></span><span class="step{second_step}"></span>
</div>
</div>
<div class="content">
<p class="eyebrow">{html.escape(eyebrow)}</p>
<h1>{html.escape(title)}</h1>
<p class="intro">{html.escape(introduction)}</p>
{error_html}
{body}
</div>
</main>
<script>{ENROLLMENT_SCRIPT}</script>
</body>
</html>"""


def _csrf(request: Request, form: dict[str, list[str]]) -> None:
    expected = request.cookies.get(CSRF_COOKIE)
    supplied = _one(form, "csrf")
    if expected is None or not secrets.compare_digest(expected, supplied):
        raise ValueError("The authorization session is invalid")


def _credentials_form(csrf: str, base_path: str, *, error: str | None = None) -> str:
    escaped = html.escape(csrf, quote=True)
    action = html.escape(f"{base_path}/enroll/prepare", quote=True)
    body = f"""
<form method="post" action="{action}" autocomplete="off">
<input type="hidden" name="csrf" value="{escaped}">
<label class="field" for="url">
<span class="field-label">Odoo URL</span>
<input id="url" name="url" type="url" placeholder="https://your-company.odoo.com"
  autocomplete="url" required>
</label>
<label class="field" for="database">
<span class="field-label">Database</span>
<input id="database" name="database" autocomplete="organization" required>
</label>
<label class="field" for="username">
<span class="field-label">Username</span>
<input id="username" name="username" autocomplete="username" required>
</label>
<label class="field" for="api-key">
<span class="field-label">API key</span>
<input id="api-key" name="api_key" type="password" autocomplete="current-password" required>
<span class="hint">Use an Odoo API key, not your account password.</span>
</label>
<div class="actions"><button type="submit" data-pending-label="Verifying...">
Verify and continue</button></div>
</form>
<p class="privacy">Your credentials are verified directly against Odoo and stored encrypted
while you review company access.</p>"""
    return _page(
        step=1,
        eyebrow="Step 1 of 2",
        title="Connect your Odoo workspace",
        introduction="Enter the Odoo connection this assistant is allowed to use.",
        body=body,
        error=error,
    )


def _company_form(
    csrf: str,
    client_id: str,
    scopes: tuple[str, ...],
    companies: tuple[tuple[int, str], ...],
    base_path: str,
    *,
    error: str | None = None,
) -> str:
    choices = "".join(
        f'<label class="choice"><input type="checkbox" name="company_id" '
        f'value="{identifier}"{" checked" if index == 0 else ""}>'
        f"<span><strong>{html.escape(name)}</strong>"
        f"<small>Company ID {identifier}</small></span></label>"
        for index, (identifier, name) in enumerate(companies)
    )
    defaults = "".join(
        f'<option value="{identifier}">{html.escape(name)}</option>'
        for identifier, name in companies
    )
    action = html.escape(f"{base_path}/enroll/commit", quote=True)
    body = f"""
<form method="post" action="{action}">
<input type="hidden" name="csrf" value="{html.escape(csrf, quote=True)}">
<fieldset>
<legend>Company access</legend>
<span class="hint">Select every company the assistant may access.</span>
<div class="choices">{choices}</div>
</fieldset>
<label class="field" for="default-company">
<span class="field-label">Default company (always authorized)</span>
<select id="default-company" name="default_company_id" required>{defaults}</select>
<span class="hint">Used when a request does not specify another authorized company.</span>
</label>
<div class="meta">
<span><strong>Client</strong> <code>{html.escape(client_id)}</code></span>
<span><strong>Permissions</strong> {html.escape(", ".join(scopes))}</span>
</div>
<label class="choice consent">
<input type="checkbox" name="consent" value="yes" required>
<span><strong>Approve this connector</strong>
<small>Allow this client to use the companies and permissions shown above.</small></span>
</label>
<div class="actions"><button type="submit" data-pending-label="Authorizing...">
Authorize connector</button></div>
</form>
<p class="privacy">You can revoke this connector later. Odoo access remains limited by the
permissions of the Odoo user and API key supplied in step 1.</p>"""
    return _page(
        step=2,
        eyebrow="Step 2 of 2",
        title="Choose company access",
        introduction="Review the client, permissions, and companies before authorizing.",
        body=body,
        error=error,
    )


def _authorization_error(base_path: str, message: str, reference: str) -> str:
    body = f"""
<div class="actions">
<a class="button" href="{html.escape(f'{base_path}/enroll', quote=True)}">
Return to company selection
</a>
</div>
<p class="privacy">Reference: <code>{html.escape(reference)}</code></p>"""
    return _page(
        step=2,
        eyebrow="Authorization not completed",
        title="We couldn't authorize this connector",
        introduction="Nothing was activated. Review the message below and try again.",
        body=body,
        error=message,
    )


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
    base_path = settings.base_path
    cookie_path = base_path or "/"
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
            response = RedirectResponse(f"{base_path}/enroll", status_code=303)
            _secure_cookie(response, SESSION_COOKIE, supplied, cookie_path)
            _secure_cookie(response, CSRF_COOKIE, _random_csrf(), cookie_path)
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
                base_path,
            )
        else:
            content = _credentials_form(csrf, base_path)
        redirect_uri = str(summary["redirect_uri"]) if discovered else None
        return _html_response(content, redirect_uri=redirect_uri)

    async def prepare(request: Request) -> Response:
        prepared_connector: tuple[str, str] | None = None
        failure_stage = "request"
        try:
            form = await _form(request)
            _csrf(request, form)
            failure_stage = "session"
            session = request.cookies[SESSION_COOKIE]
            provider.session_summary(session)
            failure_stage = "verification"
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
                    base_path,
                ),
                redirect_uri=str(summary["redirect_uri"]),
            )
            _secure_cookie(response, ENROLLMENT_COOKIE, prepared.enrollment_handle, cookie_path)
            return response
        except (
            KeyError,
            ValueError,
            ValidationError,
            OdooMcpError,
            *SHARED_DATABASE_ERRORS,
        ) as exc:
            if prepared_connector is not None:
                lifecycle.discard_pending(*prepared_connector)
            reference = secrets.token_hex(4)
            LOGGER.warning(
                "Enrollment preparation failed [reference=%s, stage=%s, error_type=%s]",
                reference,
                failure_stage,
                type(exc).__name__,
            )
            if failure_stage in {"request", "session"}:
                message = (
                    "Your secure session is no longer valid. Return to your assistant and "
                    "start the connection again."
                )
            else:
                message = (
                    "We couldn't verify those Odoo details. Check the URL, database, username, "
                    "and API key, then try again."
                )
            csrf = request.cookies.get(CSRF_COOKIE, "")
            return _html_response(
                _credentials_form(csrf, base_path, error=f"{message} Reference: {reference}"),
                status_code=400,
            )

    async def commit(request: Request) -> Response:
        failure_stage = "request"
        try:
            form = await _form(request)
            _csrf(request, form)
            failure_stage = "session"
            session = request.cookies[SESSION_COOKIE]
            handle = f"enroll:{session}"
            cookie_handle = request.cookies.get(ENROLLMENT_COOKIE)
            if cookie_handle is not None and not secrets.compare_digest(cookie_handle, handle):
                raise ValueError("The enrollment handle is invalid")
            failure_stage = "selection"
            default = int(_one(form, "default_company_id"))
            selected = tuple(
                dict.fromkeys(
                    [*(int(value) for value in form.get("company_id", ())), default]
                )
            )
            failure_stage = "consent"
            consent = _one(form, "consent") == "yes"
            if not consent:
                raise ValueError("Explicit consent is required")
            failure_stage = "commit"
            lifecycle.commit_enrollment(handle, selected, default)
            failure_stage = "activation"
            location = provider.activate_connector(session, consent=consent)
            response = RedirectResponse(location, status_code=303)
            for name in (SESSION_COOKIE, ENROLLMENT_COOKIE, CSRF_COOKIE):
                response.delete_cookie(
                    name, path=cookie_path, secure=True, httponly=True, samesite="lax"
                )
            return response
        except (KeyError, TypeError, ValueError, *SHARED_DATABASE_ERRORS) as exc:
            reference = secrets.token_hex(4)
            LOGGER.warning(
                "Enrollment authorization failed [reference=%s, stage=%s, error_type=%s]",
                reference,
                failure_stage,
                type(exc).__name__,
            )
            if failure_stage == "consent":
                message = "Approve the connector to continue. No authorization was created."
            elif failure_stage == "selection":
                message = "Choose a valid default company, then try again."
            elif failure_stage in {"request", "session", "activation"}:
                message = (
                    "Your authorization session is invalid or expired. Return to your assistant "
                    "and start the connection again."
                )
            else:
                message = (
                    "We couldn't save that company authorization. Return to company selection "
                    "and try again."
                )
            status_code = 503 if isinstance(exc, SHARED_DATABASE_ERRORS) else 400
            return _html_response(
                _authorization_error(base_path, message, reference),
                status_code=status_code,
            )

    async def revoke(request: Request) -> Response:
        try:
            form = await _form(request)
            provider.revoke_raw_token(_one(form, "client_id"), _one(form, "token"))
        except (ValueError, *SHARED_DATABASE_ERRORS):
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
        return RedirectResponse(f"{base_path}/enroll", status_code=303)

    issuer_url = AnyHttpUrl(str(settings.issuer_url))
    registration_options = ClientRegistrationOptions(
        enabled=True,
        valid_scopes=sorted(permissions),
        default_scopes=["core_read"],
    )
    revocation_options = RevocationOptions(enabled=True)
    auth_routes = create_auth_routes(
        provider,
        issuer_url,
        client_registration_options=registration_options,
        revocation_options=revocation_options,
    )
    oauth_metadata = build_metadata(
        issuer_url,
        None,
        registration_options,
        revocation_options,
    )
    oauth_metadata.token_endpoint_auth_methods_supported = ["none"]
    oauth_metadata.revocation_endpoint_auth_methods_supported = ["none"]
    metadata_handler = MetadataHandler(oauth_metadata)
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
    scoped_auth_routes: list[Route] = []
    for route in auth_routes:
        path = route.path
        endpoint = route.endpoint
        if path == "/.well-known/oauth-authorization-server":
            endpoint = metadata_handler.handle
            if base_path:
                path = f"{path}{base_path}"
        elif not path.startswith("/.well-known/"):
            path = f"{base_path}{path}"
        scoped_auth_routes.append(
            Route(
                path,
                endpoint=endpoint,
                methods=route.methods,
                name=route.name,
                include_in_schema=route.include_in_schema,
            )
        )

    mcp_app.router.routes[:] = [
        route for route in mcp_app.router.routes if getattr(route, "path", None) != "/healthz"
    ]

    @asynccontextmanager
    async def lifespan(_application: Starlette) -> AsyncIterator[None]:
        try:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        finally:
            storage.close()

    routes = [
        Route(f"{base_path}/healthz", health, methods=["GET"]),
        Route(f"{base_path}/revoke", revoke, methods=["POST"]),
        Route(f"{base_path}/authorize", authorize, methods=["GET"]),
        *scoped_auth_routes,
        Route(f"{base_path}/enroll", enrollment, methods=["GET"]),
        Route(f"{base_path}/enroll/prepare", prepare, methods=["POST"]),
        Route(f"{base_path}/enroll/commit", commit, methods=["POST"]),
        Route(f"{base_path}/enroll/replacement", replacement, methods=["POST"]),
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
) -> tuple[Starlette, SingleWriterLease | _NoopLease]:
    lease: SingleWriterLease | _NoopLease
    if settings.storage_kind is StorageKind.LOCAL:
        lease = SingleWriterLease(storage_path)
        lease.acquire()
    else:
        lease = _NoopLease()
    try:
        keyring = EncryptionKeyring(settings.active_key_version, settings.encryption_keys)
        if settings.storage_kind is StorageKind.POSTGRESQL:
            assert settings.database_url is not None
            assert settings.database_migration_url is not None
            storage = Storage.open_postgres(
                settings.database_url.get_secret_value(),
                settings.database_migration_url.get_secret_value(),
                keyring=keyring,
            )
        else:
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


class _NoopLease:
    def release(self) -> None:
        """PostgreSQL coordinates writers through database transactions."""


def protect_shared_app(app: ASGIApp, *, base_path: str = "") -> ASGIApp:
    return SharedRateLimitMiddleware(app, base_path=base_path)
