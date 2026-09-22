from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient

from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.enrollment import VerifiedEnrollment
from odoo_mcp.app.settings import OdooEnrollmentCredentials, SharedHostedSettings
from odoo_mcp.app.shared import CSRF_COOKIE, SingleWriterLease, create_shared_app
from odoo_mcp.storage import EncryptionKeyring, Storage


class Validator:
    async def validate(self, _credentials: OdooEnrollmentCredentials) -> VerifiedEnrollment:
        return VerifiedEnrollment(
            version=19,
            transport="json2",
            companies=(Company(id=1, name="Synthetic Company"),),
        )


class Adapter:
    async def get_capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            edition="enterprise",
            version=19,
            transport="json2",
            modules={"base": True},
        )

    async def get_companies(self) -> list[Company]:
        return [Company(id=1, name="Synthetic Company")]


async def _adapter_factory(_connection: object) -> OdooAdapter:
    return Adapter()


def _settings() -> SharedHostedSettings:
    return SharedHostedSettings.model_validate(
        {
            "issuer_url": "https://testserver",
            "public_mcp_url": "https://testserver/mcp",
            "active_key_version": 1,
            "encryption_keys": {1: b"a" * 32},
            "storage_kind": "local",
        }
    )


def test_complete_shared_hosted_flow_and_fail_closed_mcp(tmp_path) -> None:
    storage = Storage.open(
        tmp_path / "state.sqlite3",
        keyring=EncryptionKeyring(1, {1: b"a" * 32}),
    )
    app = create_shared_app(
        _settings(),
        storage,
        validator=Validator(),
        adapter_factory=_adapter_factory,
        permissions=frozenset({"core_read"}),
    )
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    mcp_headers = {"Accept": "application/json, text/event-stream"}

    with TestClient(app, base_url="https://testserver") as client:
        metadata = client.get("/.well-known/oauth-authorization-server")
        resource = client.get("/.well-known/oauth-protected-resource/mcp")
        unauthorized = client.post(
            "/mcp",
            headers=mcp_headers,
            json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
        )
        registration = client.post(
            "/register",
            json={
                "client_name": "Synthetic client",
                "redirect_uris": ["https://client.invalid/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "core_read",
            },
        )
        client_id = registration.json()["client_id"]
        wrong_redirect = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://attacker.invalid/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "core_read",
                "resource": "https://testserver/mcp",
            },
            follow_redirects=False,
        )
        plain_pkce = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://client.invalid/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "plain",
                "scope": "core_read",
                "resource": "https://testserver/mcp",
            },
            follow_redirects=False,
        )
        authorization = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://client.invalid/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "core_read",
                "resource": "https://testserver/mcp",
                "state": "client-state",
            },
            follow_redirects=False,
        )
        landing = client.get(authorization.headers["location"], follow_redirects=False)
        cookie_headers = landing.headers.get_list("set-cookie")
        enrollment = client.get(landing.headers["location"])
        csrf = client.cookies[CSRF_COOKIE]
        rejected_csrf = client.post(
            "/enroll/prepare",
            data={
                "csrf": "wrong",
                "url": "https://odoo.invalid",
                "database": "synthetic-db",
                "username": "synthetic-user",
                "api_key": "synthetic-secret",
            },
        )
        prepared = client.post(
            "/enroll/prepare",
            data={
                "csrf": csrf,
                "url": "https://odoo.invalid",
                "database": "synthetic-db",
                "username": "synthetic-user",
                "api_key": "synthetic-secret",
            },
        )
        rejected_consent = client.post(
            "/enroll/commit",
            data={
                "csrf": csrf,
                "company_id": "1",
                "default_company_id": "1",
            },
            follow_redirects=False,
        )
        completed = client.post(
            "/enroll/commit",
            data={
                "csrf": csrf,
                "company_id": "1",
                "default_company_id": "1",
                "consent": "yes",
            },
            follow_redirects=False,
        )
        code = parse_qs(urlsplit(completed.headers["location"]).query)["code"][0]
        tokens = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": "https://client.invalid/callback",
                "code_verifier": verifier,
                "resource": "https://testserver/mcp",
            },
        )
        access_token = tokens.json()["access_token"]
        authorized_headers = {**mcp_headers, "Authorization": f"Bearer {access_token}"}
        initialize = client.post(
            "/mcp",
            headers=authorized_headers,
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
        called = client.post(
            "/mcp",
            headers=authorized_headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_erp_capabilities", "arguments": {}},
            },
        )
        reset_context = client.post(
            "/mcp",
            headers=mcp_headers,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )

    assert metadata.status_code == 200
    assert resource.status_code == 200
    assert unauthorized.status_code == 401
    assert registration.status_code == 201
    assert wrong_redirect.status_code == 400
    assert plain_pkce.status_code == 400
    assert authorization.status_code == 302
    assert landing.status_code == 303
    assert all(
        "Secure" in header
        and "HttpOnly" in header
        and "SameSite=lax" in header
        and "Path=/" in header
        for header in cookie_headers
    )
    assert enrollment.status_code == 200
    assert "synthetic-secret" not in enrollment.text
    assert rejected_csrf.status_code == 400
    assert prepared.status_code == 200
    assert "synthetic-secret" not in prepared.text
    assert rejected_consent.status_code == 400
    assert completed.status_code == 303
    assert tokens.status_code == 200
    assert initialize.status_code == 200
    assert called.status_code == 200
    assert called.json()["result"]["structuredContent"]["status"] == "ok"
    assert reset_context.status_code == 401


def test_single_writer_lease_rejects_a_second_application(tmp_path) -> None:
    first = SingleWriterLease(tmp_path / "state.sqlite3")
    second = SingleWriterLease(tmp_path / "state.sqlite3")
    first.acquire()
    try:
        try:
            second.acquire()
        except RuntimeError as exc:
            assert str(exc) == "Shared Hosted storage already has an active writer"
        else:  # pragma: no cover - would violate the deployment contract
            raise AssertionError("Second writer was accepted")
    finally:
        first.release()


def test_reverse_proxy_headers_cannot_change_canonical_authority(tmp_path) -> None:
    storage = Storage.open(
        tmp_path / "state.sqlite3",
        keyring=EncryptionKeyring(1, {1: b"a" * 32}),
    )
    app = create_shared_app(
        _settings(),
        storage,
        validator=Validator(),
        adapter_factory=_adapter_factory,
        permissions=frozenset({"core_read"}),
    )
    with TestClient(app, base_url="https://testserver") as client:
        direct = client.get("/.well-known/oauth-protected-resource/mcp")
        proxied = client.get(
            "/.well-known/oauth-protected-resource/mcp",
            headers={
                "X-Forwarded-Host": "attacker.invalid",
                "X-Forwarded-Proto": "http",
                "X-Connector-Id": "attacker",
            },
        )
        spoofed = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "X-Forwarded-User": "attacker",
                "X-Connector-Id": "attacker",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

    assert direct.json() == proxied.json()
    assert direct.json()["resource"] == "https://testserver/mcp"
    assert spoofed.status_code == 401
