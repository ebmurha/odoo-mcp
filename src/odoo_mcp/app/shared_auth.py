"""Durable hosting-neutral OAuth provider for Shared Hosted."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    IdentityAssertionParams,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from odoo_mcp.adapters.odoo.connections import ConnectorAuthorization
from odoo_mcp.adapters.odoo.outbound_policy import SharedOutboundPolicy, shared_http_transport
from odoo_mcp.mcp.error_codes import OdooMcpError
from odoo_mcp.mcp.registry import TOOL_REGISTRY
from odoo_mcp.mcp.request_ids import new_request_id
from odoo_mcp.storage.audit import AuditRepository
from odoo_mcp.storage.database import Connection, Database, Row
from odoo_mcp.storage.json_support import canonical_json, parse_timestamp, timestamp
from odoo_mcp.storage.models import AuditEvent

AUTHORIZATION_TTL = timedelta(minutes=10)
AUTHORIZATION_CODE_TTL = timedelta(minutes=5)
ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=30)
MAX_CIMD_BYTES = 64 * 1024
VALID_SCOPES = frozenset(tool.required_permission for tool in TOOL_REGISTRY)
LOOPBACK_REDIRECT_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _random_value() -> str:
    return secrets.token_urlsafe(32)


def _redirect(url: str, **parameters: str | None) -> str:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, value) for key, value in parameters.items() if value is not None)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _redirect_uris_are_safe(redirect_uris: list[AnyUrl]) -> bool:
    for redirect_uri in redirect_uris:
        parsed = urlsplit(str(redirect_uri))
        if parsed.fragment or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.scheme == "https":
            if parsed.hostname is None:
                return False
        elif parsed.scheme == "http":
            if parsed.hostname not in LOOPBACK_REDIRECT_HOSTS:
                return False
        elif "." not in parsed.scheme or parsed.netloc or not parsed.path:
            return False
    return True


def _client_metadata_is_safe(metadata: OAuthClientInformationFull) -> bool:
    redirect_uris = metadata.redirect_uris
    return (
        redirect_uris is not None
        and bool(redirect_uris)
        and _redirect_uris_are_safe(redirect_uris)
        and metadata.token_endpoint_auth_method in {None, "none"}
    )


class CimdFetcher:
    """Fetch bounded HTTPS client metadata through the Shared outbound policy."""

    def __init__(
        self,
        policy: SharedOutboundPolicy,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._policy = policy
        self._transport = transport

    async def fetch(self, client_id: str) -> OAuthClientInformationFull | None:
        try:
            await self._policy.validate_url(client_id)
            async with httpx.AsyncClient(
                transport=self._transport or shared_http_transport(self._policy),
                timeout=httpx.Timeout(5.0),
                follow_redirects=False,
            ) as client:
                async with client.stream("GET", client_id) as response:
                    if response.status_code != 200:
                        return None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_CIMD_BYTES:
                            return None
            raw = json.loads(body)
            if not isinstance(raw, dict) or raw.get("client_id") != client_id:
                return None
            metadata = OAuthClientInformationFull.model_validate(raw)
            if not _client_metadata_is_safe(metadata):
                return None
            return metadata.model_copy(update={"token_endpoint_auth_method": "none"})
        except (ValueError, httpx.HTTPError, OdooMcpError):
            return None


class SharedOAuthProvider:
    """SDK provider and resource-server verifier backed only by token hashes."""

    def __init__(
        self,
        database: Database,
        *,
        issuer_url: str,
        resource_url: str,
        cimd_fetcher: CimdFetcher,
        valid_scopes: frozenset[str] = VALID_SCOPES,
        audit: AuditRepository | None = None,
    ) -> None:
        self._database = database
        self._issuer_url = issuer_url.rstrip("/")
        self._resource_url = resource_url
        self._cimd_fetcher = cimd_fetcher
        self._valid_scopes = valid_scopes
        self._audit = audit

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        if row is not None:
            expiry = row["metadata_expires_at"]
            if expiry is None or parse_timestamp(str(expiry)) > _now():
                try:
                    metadata = OAuthClientInformationFull.model_validate_json(
                        str(row["metadata_json"])
                    )
                except ValueError:
                    metadata = None
                if metadata is not None and _client_metadata_is_safe(metadata):
                    return metadata
                invalidated_at = timestamp()
                with self._database.transaction(write=True) as connection:
                    connection.execute(
                        """
                        UPDATE oauth_clients
                        SET metadata_expires_at = ?, updated_at = ?
                        WHERE client_id = ?
                        """,
                        (invalidated_at, invalidated_at, client_id),
                    )
            if str(row["registration_method"]) != "cimd":
                return None
        if not client_id.startswith("https://"):
            return None
        metadata = await self._cimd_fetcher.fetch(client_id)
        if metadata is None:
            return None
        now = _now()
        with self._database.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO oauth_clients (
                    client_id, registration_method, metadata_json, client_secret_hash,
                    metadata_expires_at, created_at, updated_at
                ) VALUES (?, 'cimd', ?, NULL, ?, ?, ?)
                ON CONFLICT (client_id) DO UPDATE SET
                    registration_method = 'cimd', metadata_json = excluded.metadata_json,
                    client_secret_hash = NULL,
                    metadata_expires_at = excluded.metadata_expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    client_id,
                    metadata.model_dump_json(exclude={"client_secret"}),
                    (now + timedelta(hours=1)).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return metadata

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if (
            client_info.client_secret is not None
            or client_info.token_endpoint_auth_method != "none"
        ):
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="Shared Hosted accepts public PKCE clients only",
            )
        if not _client_metadata_is_safe(client_info):
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="Every redirect URI must satisfy the callback policy",
            )
        now = timestamp()
        with self._database.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO oauth_clients (
                    client_id, registration_method, metadata_json, client_secret_hash,
                    metadata_expires_at, created_at, updated_at
                ) VALUES (?, 'dcr', ?, NULL, NULL, ?, ?)
                """,
                (
                    client_info.client_id,
                    client_info.model_dump_json(exclude={"client_secret"}),
                    now,
                    now,
                ),
            )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource != self._resource_url:
            raise AuthorizeError(
                error="invalid_target", error_description="The protected resource is invalid"
            )
        scopes = tuple(params.scopes or ())
        if not scopes or not set(scopes).issubset(self._valid_scopes):
            raise AuthorizeError(error="invalid_scope", error_description="The scope is invalid")
        if len(params.code_challenge) < 43:
            raise AuthorizeError(error="invalid_request", error_description="S256 PKCE is required")
        session = _random_value()
        now = _now()
        with self._database.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO oauth_authorization_sessions (
                    state_hash, client_id, connector_id, superseded_connector_id,
                    redirect_uri, resource, scopes_json, code_challenge, client_state,
                    authorization_code_hash, status, expires_at, consumed_at, created_at
                ) VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, NULL, 'pending', ?, NULL, ?)
                """,
                (
                    _hash(session),
                    client.client_id,
                    str(params.redirect_uri),
                    self._resource_url,
                    canonical_json(scopes),
                    params.code_challenge,
                    params.state,
                    (now + AUTHORIZATION_TTL).isoformat(),
                    now.isoformat(),
                ),
            )
        return f"{self._issuer_url}/enroll?session={session}"

    def bind_connector(self, session: str, connector_id: str) -> None:
        with self._database.transaction(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE oauth_authorization_sessions
                SET connector_id = ?
                WHERE state_hash = ? AND status = 'pending' AND connector_id IS NULL
                    AND expires_at > ?
                """,
                (connector_id, _hash(session), timestamp()),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT connector_id FROM oauth_authorization_sessions WHERE state_hash = ?",
                    (_hash(session),),
                ).fetchone()
                if row is None or str(row["connector_id"]) != connector_id:
                    raise ValueError("The authorization session is invalid or expired")

    def session_summary(self, session: str) -> dict[str, object]:
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT s.*, e.discovered_company_ids_json,
                    e.allowed_company_ids_json, e.default_company_id
                FROM oauth_authorization_sessions s
                LEFT JOIN erp_connections e ON e.id = s.connector_id
                WHERE s.state_hash = ? AND s.status = 'pending' AND s.expires_at > ?
                """,
                (_hash(session), timestamp()),
            ).fetchone()
        if row is None:
            raise ValueError("The authorization session is invalid or expired")
        return {
            "client_id": str(row["client_id"]),
            "scopes": tuple(json.loads(str(row["scopes_json"]))),
            "connector_id": None if row["connector_id"] is None else str(row["connector_id"]),
            "discovered_company_ids": (
                ()
                if row["discovered_company_ids_json"] is None
                else tuple(json.loads(str(row["discovered_company_ids_json"])))
            ),
            "allowed_company_ids": (
                ()
                if row["allowed_company_ids_json"] is None
                else tuple(json.loads(str(row["allowed_company_ids_json"])))
            ),
            "default_company_id": row["default_company_id"],
        }

    def authorize_replacement(self, session: str, authorization: ConnectorAuthorization) -> None:
        with self._database.transaction(write=True) as connection:
            active = connection.execute(
                """
                SELECT 1 FROM oauth_grants g
                JOIN erp_connections e
                  ON e.tenant_id = g.tenant_id AND e.id = g.connector_id
                WHERE g.connector_id = ? AND g.tenant_id = ? AND g.status = 'active'
                  AND g.client_id = ? AND e.status = 'active'
                """,
                (
                    authorization.connection_id,
                    authorization.tenant_id,
                    authorization.mcp_client,
                ),
            ).fetchone()
            if active is None:
                raise ValueError("The replacement authority is inactive")
            cursor = connection.execute(
                """
                UPDATE oauth_authorization_sessions
                SET superseded_connector_id = ?
                WHERE state_hash = ? AND client_id = ? AND status = 'pending'
                    AND expires_at > ?
                """,
                (
                    authorization.connection_id,
                    _hash(session),
                    authorization.mcp_client,
                    timestamp(),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("The replacement session is invalid")

    def activate_connector(self, session: str, *, consent: bool) -> str:
        if not consent:
            raise ValueError("Explicit consent is required")
        code = _random_value()
        now = _now()
        with self._database.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM oauth_authorization_sessions WHERE state_hash = ?",
                (_hash(session),),
            ).fetchone()
            if (
                row is None
                or str(row["status"]) != "pending"
                or parse_timestamp(str(row["expires_at"])) <= now
                or row["connector_id"] is None
            ):
                raise ValueError("The authorization session is invalid or expired")
            connector_id = str(row["connector_id"])
            connector = connection.execute(
                """
                SELECT * FROM erp_connections
                WHERE id = ? AND status = 'pending'
                  AND allowed_company_ids_json IS NOT NULL
                  AND default_company_id IS NOT NULL
                """,
                (connector_id,),
            ).fetchone()
            if connector is None:
                raise ValueError("The connector enrollment is incomplete")
            if parse_timestamp(str(connector["enrollment_expires_at"])) <= now:
                raise ValueError("The connector enrollment is expired")
            tenant_id = str(connector["tenant_id"])
            grant_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO oauth_grants (
                    id, connector_id, tenant_id, client_id, resource, scopes_json,
                    status, created_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, NULL)
                """,
                (
                    grant_id,
                    connector_id,
                    tenant_id,
                    str(row["client_id"]),
                    str(row["resource"]),
                    str(row["scopes_json"]),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE erp_connections
                SET status = 'active', enrollment_handle_hash = NULL,
                    activated_at = ?, updated_at = ?
                WHERE tenant_id = ? AND id = ? AND status = 'pending'
                """,
                (now.isoformat(), now.isoformat(), tenant_id, connector_id),
            )
            self._append_audit(connection, tenant_id, "activate_connector", connector_id)
            superseded = row["superseded_connector_id"]
            if superseded is not None:
                old = connection.execute(
                    "SELECT tenant_id FROM erp_connections WHERE id = ?",
                    (str(superseded),),
                ).fetchone()
                self._revoke_connector_in_transaction(connection, str(superseded), now.isoformat())
                if old is not None:
                    self._append_audit(
                        connection,
                        str(old["tenant_id"]),
                        "revoke_connector",
                        str(superseded),
                    )
            connection.execute(
                """
                UPDATE oauth_authorization_sessions
                SET authorization_code_hash = ?, status = 'authorized'
                WHERE state_hash = ? AND status = 'pending'
                """,
                (_hash(code), _hash(session)),
            )
        return _redirect(
            str(row["redirect_uri"]), code=code, state=cast(str | None, row["client_state"])
        )

    def revoke_connector(self, authorization: ConnectorAuthorization) -> None:
        with self._database.transaction(write=True) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM oauth_grants
                WHERE connector_id = ? AND tenant_id = ? AND client_id = ?
                """,
                (
                    authorization.connection_id,
                    authorization.tenant_id,
                    authorization.mcp_client,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("The connector revocation authority is invalid")
            self._revoke_connector_in_transaction(
                connection, authorization.connection_id, timestamp()
            )
            self._append_audit(
                connection,
                authorization.tenant_id,
                "revoke_connector",
                authorization.connection_id,
            )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM oauth_authorization_sessions
                WHERE authorization_code_hash = ? AND client_id = ? AND status = 'authorized'
                """,
                (_hash(authorization_code), client.client_id),
            ).fetchone()
        if row is None or parse_timestamp(str(row["expires_at"])) <= _now():
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=list(json.loads(str(row["scopes_json"]))),
            expires_at=(_now() + AUTHORIZATION_CODE_TTL).timestamp(),
            client_id=client.client_id,
            code_challenge=str(row["code_challenge"]),
            redirect_uri=AnyUrl(str(row["redirect_uri"])),
            redirect_uri_provided_explicitly=True,
            resource=str(row["resource"]),
            subject=str(row["connector_id"]),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        issued: OAuthToken | None = None
        with self._database.transaction(write=True) as connection:
            row = connection.execute(
                """
                SELECT s.*, g.id AS grant_id FROM oauth_authorization_sessions s
                JOIN oauth_grants g ON g.connector_id = s.connector_id
                    AND g.client_id = s.client_id AND g.status = 'active'
                WHERE s.authorization_code_hash = ? AND s.client_id = ?
                    AND s.status = 'authorized'
                """,
                (_hash(authorization_code.code), client.client_id),
            ).fetchone()
            if row is not None:
                connection.execute(
                    """
                    UPDATE oauth_authorization_sessions
                    SET status = 'consumed', consumed_at = ?
                    WHERE state_hash = ? AND status = 'authorized'
                    """,
                    (timestamp(), str(row["state_hash"])),
                )
                issued = self._issue_tokens(
                    connection, str(row["grant_id"]), authorization_code.scopes
                )
        if issued is None:
            raise TokenError(error="invalid_grant", error_description="Invalid code")
        return issued

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self._load_token(refresh_token, "refresh", client.client_id)
        if row is None:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=list(json.loads(str(row["scopes_json"]))),
            expires_at=int(parse_timestamp(str(row["expires_at"])).timestamp()),
            resource=str(row["resource"]),
            subject=str(row["connector_id"]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        requested = scopes or refresh_token.scopes
        if not set(requested).issubset(refresh_token.scopes):
            raise TokenError(error="invalid_scope", error_description="Invalid scope")
        invalid_grant = False
        invalid_scope = False
        issued: OAuthToken | None = None
        with self._database.transaction(write=True) as connection:
            row = self._token_row(connection, refresh_token.token, "refresh", client.client_id)
            if row is None:
                invalid_grant = True
            elif not set(requested).issubset(set(json.loads(str(row["scopes_json"])))):
                invalid_scope = True
            else:
                now = timestamp()
                connection.execute(
                    "UPDATE oauth_tokens SET rotated_at = ? WHERE token_hash = ?",
                    (now, _hash(refresh_token.token)),
                )
                connection.execute(
                    """
                    UPDATE oauth_tokens SET revoked_at = ?
                    WHERE grant_id = ? AND token_type = 'access' AND revoked_at IS NULL
                    """,
                    (now, str(row["grant_id"])),
                )
                issued = self._issue_tokens(connection, str(row["grant_id"]), requested)
        if invalid_grant:
            raise TokenError(error="invalid_grant", error_description="Invalid refresh token")
        if invalid_scope:
            raise TokenError(error="invalid_scope", error_description="Invalid scope")
        if issued is None:  # pragma: no cover - defensive transaction invariant
            raise RuntimeError("Token rotation produced no result")
        return issued

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._load_token(token, "access", None)
        if row is None:
            return None
        scopes = list(json.loads(str(row["scopes_json"])))
        return AccessToken(
            token=token,
            client_id=str(row["client_id"]),
            scopes=scopes,
            expires_at=int(parse_timestamp(str(row["expires_at"])).timestamp()),
            resource=str(row["resource"]),
            subject=str(row["connector_id"]),
            claims={
                "tenant_id": str(row["tenant_id"]),
                "connection_id": str(row["connector_id"]),
                "permissions": scopes,
            },
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.load_access_token(token)

    async def exchange_identity_assertion(
        self,
        client: OAuthClientInformationFull,
        params: IdentityAssertionParams,
    ) -> OAuthToken:
        raise TokenError(
            error="unsupported_grant_type",
            error_description="Identity assertions are not supported",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._database.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT grant_id FROM oauth_tokens WHERE token_hash = ?", (_hash(token.token),)
            ).fetchone()
            if row is None:
                return
            grant = connection.execute(
                "SELECT connector_id, tenant_id FROM oauth_grants WHERE id = ?",
                (str(row["grant_id"]),),
            ).fetchone()
            if grant is not None:
                self._revoke_connector_in_transaction(
                    connection, str(grant["connector_id"]), timestamp()
                )
                self._append_audit(
                    connection,
                    str(grant["tenant_id"]),
                    "revoke_connector",
                    str(grant["connector_id"]),
                )

    def revoke_raw_token(self, client_id: str, token: str) -> None:
        row = self._load_token(token, "access", client_id)
        if row is None:
            row = self._load_token(token, "refresh", client_id)
        if row is None:
            return
        authorization = ConnectorAuthorization(
            tenant_id=str(row["tenant_id"]),
            connection_id=str(row["connector_id"]),
            authenticated_subject=str(row["connector_id"]),
            mcp_client=client_id,
            permissions=frozenset(),
        )
        self.revoke_connector(authorization)

    def authorization_for_access_token(self, token: str) -> ConnectorAuthorization | None:
        row = self._load_token(token, "access", None)
        if row is None:
            return None
        scopes = frozenset(json.loads(str(row["scopes_json"])))
        return ConnectorAuthorization(
            tenant_id=str(row["tenant_id"]),
            connection_id=str(row["connector_id"]),
            authenticated_subject=str(row["connector_id"]),
            mcp_client=str(row["client_id"]),
            permissions=scopes.intersection(self._valid_scopes),
        )

    def _issue_tokens(self, connection: Connection, grant_id: str, scopes: list[str]) -> OAuthToken:
        access = _random_value()
        refresh = _random_value()
        now = _now()
        connection.executemany(
            """
            INSERT INTO oauth_tokens (
                token_hash, grant_id, token_type, scopes_json, expires_at,
                rotated_at, revoked_at, created_at
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                (
                    _hash(access),
                    grant_id,
                    "access",
                    canonical_json(scopes),
                    (now + ACCESS_TOKEN_TTL).isoformat(),
                    now.isoformat(),
                ),
                (
                    _hash(refresh),
                    grant_id,
                    "refresh",
                    canonical_json(scopes),
                    (now + REFRESH_TOKEN_TTL).isoformat(),
                    now.isoformat(),
                ),
            ),
        )
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
            scope=" ".join(scopes),
        )

    def _load_token(self, token: str, token_type: str, client_id: str | None) -> Row | None:
        with self._database.transaction() as connection:
            return self._token_row(connection, token, token_type, client_id)

    @staticmethod
    def _token_row(
        connection: Connection,
        token: str,
        token_type: str,
        client_id: str | None,
    ) -> Row | None:
        row = connection.execute(
            """
            SELECT t.*, g.client_id, g.connector_id, g.tenant_id, g.resource
            FROM oauth_tokens t
            JOIN oauth_grants g ON g.id = t.grant_id
            JOIN erp_connections e
              ON e.tenant_id = g.tenant_id AND e.id = g.connector_id
            WHERE t.token_hash = ? AND t.token_type = ?
              AND t.revoked_at IS NULL AND t.rotated_at IS NULL
              AND t.expires_at > ? AND g.status = 'active' AND e.status = 'active'
            """,
            (_hash(token), token_type, timestamp()),
        ).fetchone()
        if row is not None and client_id is not None and str(row["client_id"]) != client_id:
            return None
        return cast(Row | None, row)

    @staticmethod
    def _revoke_connector_in_transaction(
        connection: Connection, connector_id: str, now: str
    ) -> None:
        grants = connection.execute(
            "SELECT id FROM oauth_grants WHERE connector_id = ? AND status = 'active'",
            (connector_id,),
        ).fetchall()
        grant_ids = tuple(str(row["id"]) for row in grants)
        connection.execute(
            """
            UPDATE erp_connections SET status = 'revoked', revoked_at = ?, updated_at = ?
            WHERE id = ? AND status IN ('pending', 'active')
            """,
            (now, now, connector_id),
        )
        connection.execute(
            """
            UPDATE oauth_grants SET status = 'revoked', revoked_at = ?
            WHERE connector_id = ? AND status = 'active'
            """,
            (now, connector_id),
        )
        for grant_id in grant_ids:
            connection.execute(
                """
                UPDATE oauth_tokens SET revoked_at = ?
                WHERE grant_id = ? AND revoked_at IS NULL
                """,
                (now, grant_id),
            )

    def _append_audit(
        self,
        connection: Connection,
        tenant_id: str,
        operation: str,
        connector_id: str,
    ) -> None:
        if self._audit is None:
            return
        self._audit.append_in_transaction(
            connection,
            AuditEvent(
                request_id=new_request_id(),
                tenant_id=tenant_id,
                company_id=None,
                tool_name=operation,
                tool_version="1.0",
                module="core",
                authenticated_subject=connector_id,
                mcp_client="shared-hosted",
                odoo_db_name=None,
                odoo_user=None,
                odoo_version=None,
                odoo_transport=None,
                input_payload={"connector_id": connector_id},
                dry_run=False,
                proposed_action=None,
                actual_result={"status": "succeeded"},
                affected_odoo_records=(),
                error_code=None,
                error_message=None,
                final_status="succeeded",
            ),
        )
