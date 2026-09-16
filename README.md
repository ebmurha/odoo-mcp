# Odoo MCP

`odoo-mcp` is a workflow-native MCP server for Odoo Enterprise. The current
foundation release exposes one read-only discovery tool,
`get_erp_capabilities`. Accounting workflows are not implemented yet.

The internal accounting adapter provides bounded, typed, company-scoped read
primitives for later workflow tools. It enforces fixed model/action allowlists,
strips denied fields, normalizes dates, decimals, relations, and cursor pages,
and translates Odoo authentication, permission, and transport failures into
safe errors. It does not expose generic CRUD or an Odoo configuration surface.

Supported connection targets are Odoo.sh and self-hosted Odoo Enterprise:

- Odoo 18 through external JSON-RPC
- Odoo 19 through JSON-2

Other Odoo versions, Odoo Online, Community edition, and custom forks are not
supported. This package is not an Odoo module and does not expose generic model
CRUD.

## Local Development

Requires Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```powershell
Copy-Item .env.example .env.local
# Replace the placeholders in .env.local, then:
uv sync --locked --all-extras
uv run odoo-mcp --profile local
```

Local Development uses stdio. Process-environment settings take precedence over
`.env.local`. Never commit `.env.local`.

## Remote profiles

Dedicated Remote and Shared Hosted use the same server, registry, workflow, and
Odoo adapter as Local Development, exposed through Streamable HTTP:

```powershell
uv run odoo-mcp --profile dedicated --host 127.0.0.1 --port 8000
uv run odoo-mcp --profile shared --host 127.0.0.1 --port 8000
```

Dedicated Remote reads its single Odoo connection from the process environment;
it never loads `.env.local`. Shared Hosted requires an authenticated connector
context and an encrypted connection repository supplied by the hosting layer.
The package supplies a SQLite repository for that integration, but the hosting
layer must inject its 256-bit encryption keys from a separate operator-controlled
secret store. Missing keys, invalid ciphertext, or an unauthorized tenant and
connection binding fail closed. TLS, public ingress, connector identity issuance,
and production deployment remain operator responsibilities.

## Durable state

`odoo_mcp.storage.Storage` provides ordered SQLite migrations and tenant-scoped
repositories for audit records, proposals, artifacts, idempotency reservations,
capability snapshots, and encrypted Shared Hosted connections. Audit rows are
append-only and SHA-256 hash-chained per tenant. Idempotency reservations bind
the tenant, company, tool, key, and request payload for 24-hour replay, while
in-progress and unknown outcomes remain blocked for explicit recovery. Final
outcomes must retain a replayable response and match the reserving company.
Audit failure text is derived from registered error codes; free-form upstream
error text is not persisted.

SQLite backups use a consistent snapshot. Restore writes to a new destination
and is accepted only after database integrity, migrations, tenant audit chains,
idempotency state, capability data, and encrypted connections verify. Encryption
keys must be backed up and restored separately. PostgreSQL is not supported or
claimed by this release.

## Configuration

The supported Odoo settings are documented in `.env.example`. Do not configure
an Odoo version: the adapter detects it and fails explicitly for unsupported or
malformed responses. `config/config.example.yaml` is the safe MCP permission-map
example. A tool is authorized only when it is listed under its registry-defined
permission; unknown or mismatched entries prevent startup.

Use a dedicated non-production Odoo technical user with only the required
company and module access. Company IDs are an additional MCP authorization
boundary and never expand the technical user's Odoo permissions.

## Verification

One command runs formatting checks, static analysis, tests, package builds, and
artifact inspection in temporary directories:

```powershell
uv run python scripts/verify.py
```

Automated tests use synthetic Odoo responses. They do not establish live Odoo
version compatibility, real permissions, installed modules, or deployment
networking.
