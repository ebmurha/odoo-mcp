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

For Docker:

```console
docker compose build
docker compose up -d
```

The Compose template publishes only `127.0.0.1:8000`. Put a TLS-terminating,
authenticated reverse proxy or private-network gateway in front of `/mcp`.
The package does not treat forwarding headers as identity. The unauthenticated
`/healthz` route is a liveness check and returns no dependency details.

For a direct Python service, install the wheel into `/opt/odoo-mcp/.venv`, copy
`deploy/odoo-mcp.service` to systemd, and place non-secret permissions at
`/etc/odoo-mcp/config.yaml`. Store credentials in the root-readable environment
file referenced by the unit. Review paths and the service account before
enabling the unit.

## Shared Hosted

Shared Hosted is an integration profile, not a standalone public endpoint. The
hosting layer must authenticate the connector, create the trusted connector
authorization context, inject the encrypted connection repository and external
keyring, terminate TLS, and enforce network controls. Starting `--profile
shared` without that integration fails closed and exposes no Odoo connection.

Do not adapt the Dedicated Remote template into a multi-tenant service by
passing identity through unverified headers. See the package connection resolver
interfaces for the required trusted integration boundary.

## Secrets and persistent data

Never bake environment files, API keys, SQLite databases, or encryption keys
into an image. Mount `.odoo-mcp/state.sqlite3` on durable storage. For Shared
Hosted, back up the encrypted database and the operator key material separately.
See [`operations.md`](operations.md) before upgrades or recovery.

Production deployment, public endpoint publication, and client-directory
listing are operator release decisions and are not performed by this repository.
