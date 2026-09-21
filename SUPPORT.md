# Support

Before opening an issue, run:

```console
uv run python scripts/verify.py
odoo-mcp-admin verify --storage .odoo-mcp/state.sqlite3
```

Public issues may include the package version, Python version, deployment
profile, Odoo major version, selected transport, structured MCP error code, and
the smallest synthetic reproduction. Do not include `.env.local`, API keys,
database files, tenant or company data, raw Odoo responses, or full tracebacks
that may contain upstream data.

For Dedicated Remote authentication failures, report only whether the token was
missing or rejected and the configured package version. Never send the token,
signing key, or decoded claims.

Supported targets are Python 3.11 or newer, Odoo Enterprise 18 via JSON-RPC,
and Odoo Enterprise 19 via JSON-2 on Odoo.sh or self-hosted deployments. Odoo 19
is live-qualified; Odoo 18 is implemented and fixture-tested. Odoo Online,
Community edition, custom forks, PostgreSQL state storage, generic Odoo CRUD,
and final bank reconciliation are outside this release.

The source repository does not provide a public hosted endpoint, production
operations, or guaranteed response times.
