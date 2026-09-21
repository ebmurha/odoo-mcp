# Operations

## Diagnostics and monitoring

Remote deployments expose `GET /healthz`. It proves only that the application
process can serve HTTP; it deliberately does not probe Odoo or storage and must
not be used as a readiness or authorization signal.

Verify durable state offline or before admitting traffic:

```console
odoo-mcp-admin verify --storage /var/lib/odoo-mcp/state.sqlite3
```

The command checks migrations, SQLite integrity, tenant audit chains,
idempotency replay state, capabilities, proposals, artifacts, and encrypted
connections. It prints a fixed success or failure message and suppresses paths
and exception details. Shared Hosted operators must run the equivalent
`Storage.verify()` call with their external encryption keyring injected.

Send process logs to the deployment's normal stderr collector. Alert on process
exit, repeated structured MCP errors, audit-chain verification failure, disk
capacity, and failed backups. Do not log request payloads, raw Odoo responses,
credentials, or decrypted connection records.

## Backup and restore

Stop or drain the service for the simplest recovery procedure. Create a
database-consistent snapshot without overwriting an existing file:

```console
odoo-mcp-admin backup \
  --storage /var/lib/odoo-mcp/state.sqlite3 \
  --destination /var/backups/odoo-mcp/state-YYYYMMDD.sqlite3
```

Restore always targets a new path and validates before acceptance:

```console
odoo-mcp-admin restore \
  --source /var/backups/odoo-mcp/state-YYYYMMDD.sqlite3 \
  --storage /var/lib/odoo-mcp/restored.sqlite3
```

For Shared Hosted, preserve the matching key versions in a separate secret
backup and call `Storage.restore(..., keyring=keyring)`. A restore without the
required key fails closed. Keep the original database and backup until MCP
startup, audit verification, capability discovery, and one idempotent replay
have been checked against the restored destination.

## Upgrade

1. Drain traffic and finish or investigate every in-progress/unknown execution.
2. Record the installed version and immutable image or wheel digest.
3. Back up state and external Shared Hosted key material separately.
4. Install the exact new version in a new environment or deploy a new image.
5. Run `odoo-mcp-admin verify` against a copied database; startup then applies
   any ordered migrations.
6. Start on loopback, check `/healthz`, list MCP tools, run capability discovery,
   and verify the tenant audit chain before admitting traffic.

Never test an upgrade against the only copy of production state.

## Rollback

Code rollback and data rollback are separate. If an upgrade applied a migration,
do not point older code at the upgraded database. Stop the new version and start
the previous immutable package or image with a validated pre-upgrade database
restored to a new path. Verify it before switching traffic. Preserve the failed
upgrade state for investigation; do not delete or rewrite audit records.

## Incident handling

- Unknown write outcome: keep the idempotency reservation blocked and reconcile
  the outcome in Odoo before any explicit recovery.
- Audit or database corruption: stop serving, preserve all files, and restore a
  validated snapshot to a new destination.
- Lost Shared Hosted key: restore the separately protected matching key version;
  ciphertext cannot be bypassed or reset safely.
- Odoo permission denial: correct the technical user's Odoo ACL or record rule;
  do not broaden MCP allowlists as a workaround.
- Credential exposure: revoke and replace it in the operator secret store, then
  inspect logs and audit metadata for unintended disclosure.
