from __future__ import annotations

import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from odoo_mcp.adapters.base import Company
from odoo_mcp.adapters.odoo.connections import ConnectorAuthorization
from odoo_mcp.adapters.odoo.enrollment import VerifiedEnrollment
from odoo_mcp.adapters.odoo.outbound_policy import SharedOutboundPolicy
from odoo_mcp.app.settings import OdooEnrollmentCredentials
from odoo_mcp.app.shared_auth import CimdFetcher, SharedOAuthProvider
from odoo_mcp.app.shared_lifecycle import SharedConnectorLifecycle
from odoo_mcp.storage import EncryptionKeyring, Storage


class Validator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def validate(self, _credentials: OdooEnrollmentCredentials) -> VerifiedEnrollment:
        if self.fail:
            raise ValueError("synthetic validation failure")
        return VerifiedEnrollment(
            version=19,
            transport="json2",
            companies=(Company(id=1, name="One"), Company(id=2, name="Two")),
        )


def _credentials() -> OdooEnrollmentCredentials:
    return OdooEnrollmentCredentials.model_validate(
        {
            "url": "https://odoo.invalid",
            "database": "synthetic-db",
            "username": "synthetic-user",
            "api_key": "synthetic-secret",
        }
    )


def _storage(tmp_path) -> Storage:
    return Storage.open(
        tmp_path / "state.sqlite3",
        keyring=EncryptionKeyring(1, {1: b"a" * 32}),
    )


def _provider(storage: Storage) -> SharedOAuthProvider:
    async def resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    return SharedOAuthProvider(
        storage.database,
        issuer_url="https://service.invalid",
        resource_url="https://service.invalid/mcp",
        cimd_fetcher=CimdFetcher(SharedOutboundPolicy(resolver)),
        audit=storage.audit,
    )


def _client(client_id: str = "client-a") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_name="Synthetic client",
        redirect_uris=[AnyUrl("https://client.invalid/callback")],
        token_endpoint_auth_method="none",
        scope="core_read accounting_read",
    )


async def _session(provider: SharedOAuthProvider, client: OAuthClientInformationFull) -> str:
    await provider.register_client(client)
    location = await provider.authorize(
        client,
        AuthorizationParams(
            state="client-state",
            scopes=["core_read", "accounting_read"],
            code_challenge="a" * 43,
            redirect_uri=AnyUrl("https://client.invalid/callback"),
            redirect_uri_provided_explicitly=True,
            resource="https://service.invalid/mcp",
        ),
    )
    return parse_qs(urlsplit(location).query)["session"][0]


@pytest.mark.asyncio
async def test_authorization_rejects_wrong_resource_and_scope(tmp_path) -> None:
    provider = _provider(_storage(tmp_path))
    client = _client()
    await provider.register_client(client)
    for resource, scopes in (
        ("https://wrong.invalid/mcp", ["core_read"]),
        ("https://service.invalid/mcp", ["unknown_scope"]),
    ):
        with pytest.raises(AuthorizeError):
            await provider.authorize(
                client,
                AuthorizationParams(
                    state=None,
                    scopes=scopes,
                    code_challenge="a" * 43,
                    redirect_uri=AnyUrl("https://client.invalid/callback"),
                    redirect_uri_provided_explicitly=True,
                    resource=resource,
                ),
            )


@pytest.mark.asyncio
async def test_invalid_preparation_persists_no_connector_or_secret(tmp_path) -> None:
    storage = _storage(tmp_path)
    lifecycle = SharedConnectorLifecycle(
        storage.database,
        storage.audit,
        storage.keyring,
        Validator(fail=True),
    )

    with pytest.raises(ValueError, match="synthetic validation failure"):
        await lifecycle.prepare_enrollment(_credentials())

    database = sqlite3.connect(storage.database.path)
    count = database.execute("SELECT count(*) FROM erp_connections").fetchone()[0]
    database.close()
    assert count == 0
    assert "synthetic-secret" not in storage.database.path.read_bytes().decode(
        "utf-8", errors="ignore"
    )


@pytest.mark.asyncio
async def test_pending_requires_discovered_selection_before_activation(tmp_path) -> None:
    storage = _storage(tmp_path)
    lifecycle = SharedConnectorLifecycle(
        storage.database, storage.audit, storage.keyring, Validator()
    )
    prepared = await lifecycle.prepare_enrollment(_credentials(), enrollment_handle="h" * 32)

    assert prepared.companies == (Company(id=1, name="One"), Company(id=2, name="Two"))
    assert (
        await storage.connections.resolve_authorized(
            ConnectorAuthorization(
                tenant_id=prepared.tenant_id,
                connection_id=prepared.connector_id,
                authenticated_subject="subject",
                mcp_client="client-a",
                permissions=frozenset({"core_read"}),
            )
        )
        is None
    )
    with pytest.raises(ValueError, match="not discovered"):
        lifecycle.commit_enrollment(prepared.enrollment_handle, (99,), 99)

    committed = lifecycle.commit_enrollment(prepared.enrollment_handle, (2,), 2)
    assert committed.allowed_company_ids == (2,)
    assert lifecycle.commit_enrollment(prepared.enrollment_handle, (2,), 2) == committed
    with pytest.raises(ValueError, match="already consumed"):
        lifecycle.commit_enrollment(prepared.enrollment_handle, (1,), 1)

    with pytest.raises(sqlite3.IntegrityError, match="incomplete"):
        with storage.database.transaction(write=True) as connection:
            connection.execute(
                "UPDATE erp_connections SET status = 'active' WHERE id = ?",
                (prepared.connector_id,),
            )


@pytest.mark.asyncio
async def test_grant_token_refresh_and_revocation_are_connector_bound(tmp_path) -> None:
    storage = _storage(tmp_path)
    lifecycle = SharedConnectorLifecycle(
        storage.database, storage.audit, storage.keyring, Validator()
    )
    provider = _provider(storage)
    client = _client()
    session = await _session(provider, client)
    prepared = await lifecycle.prepare_enrollment(_credentials(), enrollment_handle="h" * 32)
    provider.bind_connector(session, prepared.connector_id)
    lifecycle.commit_enrollment(prepared.enrollment_handle, (1, 2), 1)
    callback = provider.activate_connector(session, consent=True)
    code = parse_qs(urlsplit(callback).query)["code"][0]
    loaded_code = await provider.load_authorization_code(client, code)
    assert loaded_code is not None
    token = await provider.exchange_authorization_code(client, loaded_code)

    access = await provider.verify_token(token.access_token)
    assert access is not None
    assert access.claims == {
        "tenant_id": prepared.tenant_id,
        "connection_id": prepared.connector_id,
        "permissions": ["core_read", "accounting_read"],
    }
    assert token.access_token not in storage.database.path.read_bytes().decode(
        "utf-8", errors="ignore"
    )
    assert token.refresh_token is not None
    refresh = await provider.load_refresh_token(client, token.refresh_token)
    assert refresh is not None
    rotated = await provider.exchange_refresh_token(client, refresh, ["core_read"])
    assert await provider.load_refresh_token(client, token.refresh_token) is None
    assert await provider.verify_token(rotated.access_token) is not None

    trusted = provider.authorization_for_access_token(rotated.access_token)
    assert trusted is not None

    backup = tmp_path / "backup.sqlite3"
    storage.backup(backup)
    restored = Storage.restore(
        backup,
        tmp_path / "restored.sqlite3",
        keyring=EncryptionKeyring(1, {1: b"a" * 32}),
    )
    restored_provider = _provider(restored)
    assert await restored_provider.verify_token(rotated.access_token) is not None
    assert await restored.connections.resolve_authorized(trusted) is not None

    provider.revoke_connector(trusted)
    assert await provider.verify_token(rotated.access_token) is None
    assert await storage.connections.resolve_authorized(trusted) is None
    assert [event.tool_name for event in storage.audit.list_for_tenant(prepared.tenant_id)] == [
        "prepare_enrollment",
        "commit_enrollment",
        "activate_connector",
        "revoke_connector",
    ]


@pytest.mark.asyncio
async def test_replacement_atomically_revokes_superseded_authority(tmp_path) -> None:
    storage = _storage(tmp_path)
    lifecycle = SharedConnectorLifecycle(
        storage.database, storage.audit, storage.keyring, Validator()
    )
    provider = _provider(storage)
    client = _client()
    first_session = await _session(provider, client)
    first = await lifecycle.prepare_enrollment(_credentials(), enrollment_handle="a" * 32)
    provider.bind_connector(first_session, first.connector_id)
    lifecycle.commit_enrollment(first.enrollment_handle, (1,), 1)
    first_code = parse_qs(urlsplit(provider.activate_connector(first_session, consent=True)).query)[
        "code"
    ][0]
    loaded = await provider.load_authorization_code(client, first_code)
    assert loaded is not None
    first_tokens = await provider.exchange_authorization_code(client, loaded)
    authority = provider.authorization_for_access_token(first_tokens.access_token)
    assert authority is not None

    second_session = await provider.authorize(
        client,
        AuthorizationParams(
            state=None,
            scopes=["core_read"],
            code_challenge="b" * 43,
            redirect_uri=AnyUrl("https://client.invalid/callback"),
            redirect_uri_provided_explicitly=True,
            resource="https://service.invalid/mcp",
        ),
    )
    second_session = parse_qs(urlsplit(second_session).query)["session"][0]
    provider.authorize_replacement(second_session, authority)
    second = await lifecycle.prepare_enrollment(_credentials(), enrollment_handle="b" * 32)
    provider.bind_connector(second_session, second.connector_id)
    lifecycle.commit_enrollment(second.enrollment_handle, (2,), 2)
    provider.activate_connector(second_session, consent=True)

    assert await provider.verify_token(first_tokens.access_token) is None
    with storage.database.transaction() as connection:
        states = {
            str(row["id"]): str(row["status"])
            for row in connection.execute("SELECT id, status FROM erp_connections")
        }
    assert states == {first.connector_id: "revoked", second.connector_id: "active"}
