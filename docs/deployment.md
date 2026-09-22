# Deployment

All deployment profiles run the same Python package, MCP server, registry,
workflows, Odoo adapter, and SQLite storage implementation.

## Local Development

Copy `.env.example` to the ignored `.env.local`, fill it locally, and run:

```console
uv sync --locked --all-extras
uv run odoo-mcp --profile local --config config/config.example.yaml
```

This profile uses stdio and is intended for a local MCP host. Process
environment settings override `.env.local`.

## Dedicated Remote

Dedicated Remote uses Streamable HTTP with one deployment-owned Odoo
connection. Supply settings through the process environment or a secret store;
this profile never loads `.env.local`.

The deployment's identity service must issue HS256 JWT bearer tokens containing
`iss`, `aud`, `exp`, `iat`, `sub`, and `client_id`. Configure the exact issuer,
audience, and a random signing key of at least 32 characters through
`ODOO_MCP_AUTH_ISSUER`, `ODOO_MCP_AUTH_AUDIENCE`, and
`ODOO_MCP_AUTH_SIGNING_KEY`. Keep the signing key in the deployment secret store
and rotate/revoke it through that issuer. Requests with missing or invalid
tokens are rejected before MCP routing. Tool permissions and allowed companies
come from server configuration, never token claims.

For Docker:

```console
docker compose build
docker compose up -d
```

To build and qualify the release image independently of Odoo, run:

```console
uv run python scripts/verify_docker.py
```

The check imports the installed package through the image's virtual environment
and verifies that the container runs as a non-root user.

The Compose template publishes only `127.0.0.1:8000`. Put a TLS-terminating
reverse proxy or private-network gateway in front of `/mcp`.
The package does not treat forwarding headers as identity. The unauthenticated
`/healthz` route is a liveness check and returns no dependency details.

For a direct Python service, install the wheel into `/opt/odoo-mcp/.venv`, copy
`deploy/odoo-mcp.service` to systemd, and place non-secret permissions at
`/etc/odoo-mcp/config.yaml`. Store credentials in the root-readable environment
file referenced by the unit, including the Dedicated JWT settings. Review paths and the service account before
enabling the unit.

## Shared Hosted

Shared Hosted is the package's portable open-enrollment service. Copy the
variable names from `.env.shared.example` into the operator secret/configuration
system, use an immutable image reference, and run:

```console
docker compose --env-file /secure/path/shared.env \
  -f deploy/shared-compose.yml up -d
```

`ODOO_MCP_SHARED_ISSUER_URL` is the canonical authorization-server origin and
`ODOO_MCP_SHARED_PUBLIC_MCP_URL` is the exact protected resource and public MCP
route. Both must be HTTPS. `ODOO_MCP_ENCRYPTION_KEYS` contains comma-separated
`version:URL-safe-base64` 32-byte keys and
`ODOO_MCP_ACTIVE_KEY_VERSION` selects the write key. Keep old versions available
until every connection has been rotated. Incomplete configuration fails at
startup without falling back to Dedicated credentials, static bearer tokens,
or anonymous access.

The service supplies OAuth discovery, public-client DCR, CIMD lookup,
authorization, token rotation, revocation, enrollment, consent, connector
replacement, protected-resource metadata, and MCP routing. It stores only token
hashes and encrypted Odoo API keys. A customer needs no separate product
account: successful Odoo verification, company selection, and explicit consent
create the connector grant. Confidential DCR clients are rejected; use a public
S256-PKCE client or validated HTTPS CIMD metadata.

Registered callbacks must be HTTPS web URLs, HTTP loopback URLs for native
clients (`localhost`, `127.0.0.1`, or `::1`), or reverse-domain private-use URI
schemes such as `com.example.app:/callback`. User information and fragments are
forbidden, and other HTTP or executable schemes are rejected. The same policy
applies to DCR and CIMD before metadata is stored.

Run exactly one writable application process against one durable, locally
mounted SQLite volume. Active-active replicas, multiple workers, and network or
shared filesystems are unsupported. A stateless proxy may scale independently.
The optional `deploy/reverse-proxy/nginx.conf` is routing guidance only; it owns
no identity or application state. Forwarded host, scheme, path, and identity
headers never change the configured issuer, resource, connector, company, or
permissions. The same image and application settings run with direct ingress
or behind a standards-compatible routing/TLS proxy.

Customer Odoo URLs must be public HTTPS destinations. Every outbound connection
revalidates all DNS answers, rejects mixed public/private answers and forbidden
IPv4/IPv6 ranges, pins the socket to a validated address while retaining the
original hostname for TLS, follows no redirects, and bounds DNS, connection,
read, response-size, and address work.

Before deployment, run the synthetic fixed-output composition verifier:

```console
uv run python scripts/verify_shared.py
```

It exercises discovery, public-client registration, S256 authorization,
enrollment, connector activation, authenticated MCP initialize/list/call,
revocation, restart, and encrypted-state verification without contacting Odoo.

## Secrets and persistent data

Never bake environment files, API keys, SQLite databases, or encryption keys
into an image. Mount `.odoo-mcp/state.sqlite3` on durable storage. For Shared
Hosted, back up the encrypted database and the operator key material separately.
See [`operations.md`](operations.md) before upgrades or recovery.

Production deployment, public endpoint publication, and client-directory
listing are operator release decisions and are not performed by this repository.
