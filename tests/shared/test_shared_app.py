from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
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


def _settings(base_path: str = "") -> SharedHostedSettings:
    resource_path = base_path or "/mcp"
    return SharedHostedSettings.model_validate(
        {
            "issuer_url": f"https://testserver{base_path}",
            "public_mcp_url": f"https://testserver{resource_path}",
            "active_key_version": 1,
            "encryption_keys": {1: b"a" * 32},
            "storage_kind": "local",
        }
    )


@pytest.mark.parametrize(
    ("base_path", "resource_path"),
    [("", "/mcp"), ("/odoo", "/odoo")],
)
def test_complete_shared_hosted_flow_and_fail_closed_mcp(
    tmp_path, base_path: str, resource_path: str
) -> None:
    storage = Storage.open(
        tmp_path / "state.sqlite3",
        keyring=EncryptionKeyring(1, {1: b"a" * 32}),
    )
    app = create_shared_app(
        _settings(base_path),
        storage,
        validator=Validator(),
        adapter_factory=_adapter_factory,
        permissions=frozenset({"core_read", "accounting_read"}),
    )
    endpoint = lambda suffix: f"{base_path}{suffix}"  # noqa: E731
    resource_url = f"https://testserver{resource_path}"
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    mcp_headers = {"Accept": "application/json, text/event-stream"}

    with TestClient(app, base_url="https://testserver") as client:
        metadata = client.get(f"/.well-known/oauth-authorization-server{base_path}")
        resource = client.get(f"/.well-known/oauth-protected-resource{resource_path}")
        unauthorized = client.post(
            resource_path,
            headers=mcp_headers,
            json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
        )
        registration = client.post(
            endpoint("/register"),
            json={
                "client_name": "Synthetic client",
                "redirect_uris": ["https://client.invalid/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "core_read accounting_read",
            },
        )
        unsafe_registrations = [
            client.post(
                endpoint("/register"),
                json={
                    "client_name": "Unsafe client",
                    "redirect_uris": [redirect_uri],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "scope": "core_read",
                },
            )
            for redirect_uri in (
                "javascript:alert(1)",
                "https://client.invalid/callback#fragment",
            )
        ]
        native_registrations = [
            client.post(
                endpoint("/register"),
                json={
                    "client_name": "Native client",
                    "redirect_uris": [redirect_uri],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code"],
                    "response_types": ["code"],
                    "scope": "core_read",
                },
            )
            for redirect_uri in (
                "http://127.0.0.1:8123/callback",
                "com.example.app:/callback",
            )
        ]
        client_id = registration.json()["client_id"]
        wrong_redirect = client.get(
            endpoint("/authorize"),
            params={
                "client_id": client_id,
                "redirect_uri": "https://attacker.invalid/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "core_read accounting_read",
                "resource": resource_url,
            },
            follow_redirects=False,
        )
        plain_pkce = client.get(
            endpoint("/authorize"),
            params={
                "client_id": client_id,
                "redirect_uri": "https://client.invalid/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "plain",
                "scope": "core_read accounting_read",
                "resource": resource_url,
            },
            follow_redirects=False,
        )
        authorization = client.get(
            endpoint("/authorize"),
            params={
                "client_id": client_id,
                "redirect_uri": "https://client.invalid/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "core_read accounting_read",
                "resource": resource_url,
                "state": "client-state",
            },
            follow_redirects=False,
        )
        landing = client.get(authorization.headers["location"], follow_redirects=False)
        cookie_headers = landing.headers.get_list("set-cookie")
        enrollment = client.get(landing.headers["location"])
        csrf = client.cookies[CSRF_COOKIE]
        rejected_csrf = client.post(
            endpoint("/enroll/prepare"),
            data={
                "csrf": "wrong",
                "url": "https://odoo.invalid",
                "database": "synthetic-db",
                "username": "synthetic-user",
                "api_key": "synthetic-secret",
            },
        )
        prepared = client.post(
            endpoint("/enroll/prepare"),
            data={
                "csrf": csrf,
                "url": "https://odoo.invalid",
                "database": "synthetic-db",
                "username": "synthetic-user",
                "api_key": "synthetic-secret",
            },
        )
        rejected_consent = client.post(
            endpoint("/enroll/commit"),
            data={
                "csrf": csrf,
                "company_id": "1",
                "default_company_id": "1",
            },
            follow_redirects=False,
        )
        rejected_company = client.post(
            endpoint("/enroll/commit"),
            data={
                "csrf": csrf,
                "default_company_id": "99",
                "consent": "yes",
            },
            follow_redirects=False,
        )
        completed = client.post(
            endpoint("/enroll/commit"),
            data={
                "csrf": csrf,
                "default_company_id": "1",
                "consent": "yes",
            },
            follow_redirects=False,
        )
        code = parse_qs(urlsplit(completed.headers["location"]).query)["code"][0]
        tokens = client.post(
            endpoint("/token"),
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": "https://client.invalid/callback",
                "code_verifier": verifier,
                "resource": resource_url,
            },
        )
        access_token = tokens.json()["access_token"]
        authorized_headers = {**mcp_headers, "Authorization": f"Bearer {access_token}"}
        initialize = client.post(
            resource_path,
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
            resource_path,
            headers=authorized_headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_erp_capabilities", "arguments": {}},
            },
        )
        narrowed_tokens = client.post(
            endpoint("/token"),
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens.json()["refresh_token"],
                "scope": "core_read",
                "resource": resource_url,
            },
        )
        narrowed_headers = {
            **mcp_headers,
            "Authorization": f"Bearer {narrowed_tokens.json()['access_token']}",
        }
        denied_accounting = client.post(
            resource_path,
            headers=narrowed_headers,
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "get_trial_balance",
                    "arguments": {
                        "company_id": 1,
                        "period_start": "2026-01-01",
                        "period_end": "2026-01-31",
                    },
                },
            },
        )
        reset_context = client.post(
            resource_path,
            headers=mcp_headers,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )

        root_fallbacks = []
        if base_path:
            root_fallbacks = [
                client.get("/authorize"),
                client.post("/token"),
                client.get("/enroll"),
                client.post(
                    "/mcp",
                    headers=mcp_headers,
                    json={"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
                ),
            ]

    assert metadata.status_code == 200
    assert resource.status_code == 200
    assert unauthorized.status_code == 401
    assert registration.status_code == 201
    assert [response.status_code for response in unsafe_registrations] == [400, 400]
    assert [response.status_code for response in native_registrations] == [201, 201]
    assert wrong_redirect.status_code == 400
    assert plain_pkce.status_code == 400
    assert authorization.status_code == 302
    assert urlsplit(authorization.headers["location"]).path == endpoint("/enroll")
    assert landing.status_code == 303
    assert urlsplit(landing.headers["location"]).path == endpoint("/enroll")
    assert all(
        "Secure" in header
        and "HttpOnly" in header
        and "SameSite=lax" in header
        and f"Path={base_path or '/'}" in header
        for header in cookie_headers
    )
    assert enrollment.status_code == 200
    assert "Connect your Odoo workspace" in enrollment.text
    assert "<style>" in enrollment.text
    assert "style-src 'sha256-" in enrollment.headers["content-security-policy"]
    assert "'unsafe-inline'" not in enrollment.headers["content-security-policy"]
    assert f'action="{endpoint("/enroll/prepare")}"' in enrollment.text
    assert "synthetic-secret" not in enrollment.text
    assert rejected_csrf.status_code == 400
    assert rejected_csrf.headers["content-type"].startswith("text/html")
    assert "Your secure session is no longer valid" in rejected_csrf.text
    assert "synthetic-secret" not in rejected_csrf.text
    assert prepared.status_code == 200
    assert "Choose company access" in prepared.text
    assert "Default company (always authorized)" in prepared.text
    assert f'action="{endpoint("/enroll/commit")}"' in prepared.text
    assert "synthetic-secret" not in prepared.text
    assert rejected_consent.status_code == 400
    assert rejected_consent.headers["content-type"].startswith("text/html")
    assert "Approve the connector to continue" in rejected_consent.text
    assert rejected_company.status_code == 400
    assert rejected_company.headers["content-type"].startswith("text/html")
    assert "save that company authorization" in rejected_company.text
    assert completed.status_code == 303
    completed_redirect = urlsplit(completed.headers["location"])
    assert (completed_redirect.scheme, completed_redirect.netloc, completed_redirect.path) == (
        "https",
        "client.invalid",
        "/callback",
    )
    assert parse_qs(completed_redirect.query)["state"] == ["client-state"]
    assert tokens.status_code == 200
    assert initialize.status_code == 200
    assert called.status_code == 200
    assert called.json()["result"]["structuredContent"]["status"] == "ok"
    assert narrowed_tokens.status_code == 200
    assert narrowed_tokens.json()["scope"] == "core_read"
    assert denied_accounting.status_code == 200
    assert (
        denied_accounting.json()["result"]["structuredContent"]["error_code"] == "ODOO_AUTH_FAILED"
    )
    assert reset_context.status_code == 401
    assert [response.status_code for response in root_fallbacks] == [404] * len(root_fallbacks)


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


def test_path_scoped_shared_app_owns_only_the_configured_surface(tmp_path) -> None:
    settings = SharedHostedSettings.model_validate(
        {
            "issuer_url": "https://testserver/odoo",
            "public_mcp_url": "https://testserver/odoo",
            "active_key_version": 1,
            "encryption_keys": {1: b"a" * 32},
            "storage_kind": "local",
        }
    )
    storage = Storage.open(
        tmp_path / "path-state.sqlite3",
        keyring=EncryptionKeyring(1, {1: b"a" * 32}),
    )
    app = create_shared_app(
        settings,
        storage,
        validator=Validator(),
        adapter_factory=_adapter_factory,
        permissions=frozenset({"core_read"}),
    )

    with TestClient(app, base_url="https://testserver") as client:
        health = client.get("/odoo/healthz")
        root_health = client.get("/healthz")
        metadata = client.get("/.well-known/oauth-authorization-server/odoo")
        resource = client.get("/.well-known/oauth-protected-resource/odoo")
        root_registration = client.post("/register", json={})
        registration = client.post(
            "/odoo/register",
            json={
                "client_name": "Path client",
                "redirect_uris": ["https://client.invalid/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "scope": "core_read",
            },
        )

    assert health.status_code == 200
    assert root_health.status_code == 404
    assert root_registration.status_code == 404
    assert registration.status_code == 201
    assert metadata.status_code == 200
    assert metadata.json()["issuer"] == "https://testserver/odoo"
    assert metadata.json()["authorization_endpoint"] == "https://testserver/odoo/authorize"
    assert metadata.json()["token_endpoint"] == "https://testserver/odoo/token"
    assert metadata.json()["registration_endpoint"] == "https://testserver/odoo/register"
    assert metadata.json()["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata.json()["revocation_endpoint_auth_methods_supported"] == ["none"]
    assert "client_id_metadata_document_supported" not in metadata.json()
    assert resource.status_code == 200
    assert resource.json()["resource"] == "https://testserver/odoo"
