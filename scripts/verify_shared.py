"""Fixed-output synthetic qualification for the Shared Hosted composition."""

from __future__ import annotations

import base64
import hashlib
import logging
import tempfile
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient

from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.enrollment import VerifiedEnrollment
from odoo_mcp.app.settings import OdooEnrollmentCredentials, SharedHostedSettings
from odoo_mcp.app.shared import CSRF_COOKIE, create_shared_app
from odoo_mcp.storage import EncryptionKeyring, Storage


class _Validator:
    async def validate(self, _credentials: OdooEnrollmentCredentials) -> VerifiedEnrollment:
        return VerifiedEnrollment(
            version=19,
            transport="json2",
            companies=(Company(id=1, name="Synthetic Company"),),
        )


class _Adapter:
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
    return cast(OdooAdapter, _Adapter())


def _qualify(path: Path) -> None:
    settings = SharedHostedSettings.model_validate(
        {
            "issuer_url": "https://shared.invalid",
            "public_mcp_url": "https://shared.invalid/mcp",
            "active_key_version": 1,
            "encryption_keys": {1: b"q" * 32},
            "storage_kind": "local",
        }
    )
    storage = Storage.open(path, keyring=EncryptionKeyring(1, {1: b"q" * 32}))
    app = create_shared_app(
        settings,
        storage,
        validator=_Validator(),
        adapter_factory=_adapter_factory,
        permissions=frozenset({"core_read"}),
    )
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    challenge = challenge.rstrip("=")
    with TestClient(app, base_url="https://shared.invalid") as client:
        registration = client.post(
            "/register",
            json={
                "client_name": "Qualification client",
                "redirect_uris": ["https://client.invalid/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "core_read",
            },
        )
        registration.raise_for_status()
        client_id = registration.json()["client_id"]
        authorization = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://client.invalid/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "core_read",
                "resource": "https://shared.invalid/mcp",
            },
            follow_redirects=False,
        )
        enrollment = client.get(authorization.headers["location"], follow_redirects=True)
        enrollment.raise_for_status()
        csrf = client.cookies[CSRF_COOKIE]
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
        prepared.raise_for_status()
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
        if completed.status_code != 303:
            raise RuntimeError("Authorization did not complete")
        code = parse_qs(urlsplit(completed.headers["location"]).query)["code"][0]
        tokens = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": "https://client.invalid/callback",
                "code_verifier": verifier,
                "resource": "https://shared.invalid/mcp",
            },
        )
        tokens.raise_for_status()
        token = tokens.json()["access_token"]
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        initialize = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "qualification", "version": "1"},
                },
            },
        )
        listed = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        called = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_erp_capabilities", "arguments": {}},
            },
        )
        if not all(response.status_code == 200 for response in (initialize, listed, called)):
            raise RuntimeError("Authenticated MCP qualification failed")

        replacement_authorization = client.get(
            "/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://client.invalid/callback",
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "core_read",
                "resource": "https://shared.invalid/mcp",
            },
            follow_redirects=False,
        )
        replacement_page = client.get(
            replacement_authorization.headers["location"], follow_redirects=True
        )
        replacement_page.raise_for_status()
        replacement = client.post(
            "/enroll/replacement",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=False,
        )
        if replacement.status_code != 303:
            raise RuntimeError("Replacement authorization failed")
        replacement_csrf = client.cookies[CSRF_COOKIE]
        replacement_prepared = client.post(
            "/enroll/prepare",
            data={
                "csrf": replacement_csrf,
                "url": "https://odoo.invalid",
                "database": "synthetic-db",
                "username": "synthetic-user",
                "api_key": "synthetic-replacement-secret",
            },
        )
        replacement_prepared.raise_for_status()
        replacement_completed = client.post(
            "/enroll/commit",
            data={
                "csrf": replacement_csrf,
                "company_id": "1",
                "default_company_id": "1",
                "consent": "yes",
            },
            follow_redirects=False,
        )
        replacement_code = parse_qs(urlsplit(replacement_completed.headers["location"]).query)[
            "code"
        ][0]
        replacement_tokens = client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": replacement_code,
                "redirect_uri": "https://client.invalid/callback",
                "code_verifier": verifier,
                "resource": "https://shared.invalid/mcp",
            },
        )
        replacement_tokens.raise_for_status()
        replacement_token = replacement_tokens.json()["access_token"]
        rejected_old = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
        )
        if rejected_old.status_code != 401:
            raise RuntimeError("Superseded connector remained authorized")
        revoked = client.post(
            "/revoke",
            data={
                "client_id": client_id,
                "client_secret": "",
                "token": replacement_token,
            },
        )
        revoked.raise_for_status()
        rejected = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {replacement_token}",
            },
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
        )
        if rejected.status_code != 401:
            raise RuntimeError("Revoked connector remained authorized")
    Storage.open(path, keyring=EncryptionKeyring(1, {1: b"q" * 32})).verify()


def main() -> None:
    logging.disable(logging.CRITICAL)
    try:
        with tempfile.TemporaryDirectory(prefix="odoo-mcp-shared-") as directory:
            _qualify(Path(directory) / "state.sqlite3")
    except Exception:
        print("Shared Hosted qualification failed safely.")
        raise SystemExit(1) from None
    print("Shared Hosted qualification passed.")


if __name__ == "__main__":
    main()
