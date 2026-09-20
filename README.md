# Odoo MCP

`odoo-mcp` is a workflow-native MCP server for Odoo Enterprise. It exposes
read-only capability discovery, trial-balance reporting, aged receivables and
payables reporting, cashbook visibility, unmatched bank-line detection, and
proposal-only bank reconciliation.

The internal accounting adapter provides bounded, typed, company-scoped read
primitives for the reporting workflows. It enforces fixed model/action allowlists,
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
uv run odoo-mcp --profile local --config config/config.example.yaml
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

The server stores local durable state at `.odoo-mcp/state.sqlite3` by default.
Use `--storage <path>` to select a different SQLite file. Successful accounting
reports atomically persist their Markdown artifact and a compact audit outcome;
failed and denied report calls persist a secret-safe failure audit when an
isolation identity has been resolved.

## Write safety

`odoo_mcp.policy.WriteSafetyCoordinator` is the required boundary for
write-capable workflows. It enforces registry risk metadata, resolved identity,
permission, company, and capability gates before workflow preparation. Calls
default to preview-only behavior. Execution requires explicit `dry_run: false`
and a non-empty idempotency key, then reserves that key and appends the attempt
audit before fresh-state validation and the Odoo mutation.

Successful, rejected, conflicting, replayed, known-failed, and unknown outcomes
remain distinct and replay-safe across restart. An uncertain mutation is never
automatically retried; if outcome persistence fails after a possible mutation,
the in-progress reservation continues to block duplicate execution. Odoo
permission denials that prove no mutation occurred remain structured known
failures. Audit and response text suppress raw exception details.

Human confirmation belongs to the MCP client host. The server does not issue
approval tokens, provide an approval UI, or automatically turn a preview into
execution. Reconciliation calls default to preview. An explicit
`dry_run: false` call with an idempotency key stores a server-owned proposal and
Markdown artifact, but never finalizes reconciliation or changes a bank
statement line in Odoo.

## Configuration

The supported Odoo settings are documented in `.env.example`. Do not configure
an Odoo version: the adapter detects it and fails explicitly for unsupported or
malformed responses. `config/config.example.yaml` is the safe MCP permission-map
example and enables the current read tools. A tool is authorized only when it is
listed under its registry-defined
permission; unknown or mismatched entries prevent startup.

Use a dedicated non-production Odoo technical user with only the required
company and module access. Company IDs are an additional MCP authorization
boundary and never expand the technical user's Odoo permissions.

## Accounting workflows

- `get_trial_balance` returns posted opening balances, inclusive-period debit
  and credit movement, closing balances, totals, and a Markdown artifact.
- `get_aged_receivables` and `get_aged_payables` reconstruct posted residuals
  as of a date, including later partial reconciliations, and group them into
  not-yet-due, 1–30, 31–60, 61–90, and 90+ day buckets.
- `get_cashbook` returns posted move lines from cash and bank journals with
  opening balance, period debit and credit, and closing balance.
- `flag_unmatched_statement_lines` identifies unreconciled statement lines
  without one unique eligible match at or above the requested threshold.
- `reconcile_bank_statement_lines` scores exact one-to-one candidates by amount,
  partner, normalized reference, and date proximity. Ties and candidate reuse
  remain explicit unmatched results. The tool can persist a proposal locally;
  it does not perform Odoo reconciliation.
- `list_open_invoices` and `list_open_bills` reconstruct record- and
  company-currency residuals as of a date, including later partial
  reconciliations.
- `create_customer_invoice` and `create_supplier_bill` provide non-mutating,
  input-only previews by default. Explicit execution creates one unposted
  draft and reads back Odoo's effective accounting results.
- `create_credit_note` creates one linked full draft reversal. Before posting,
  `validate_invoice` reports locally determinable balance, currency, account,
  total, and tax blockers while explicitly deferring Odoo-only posting checks.
- `register_payment` delegates route discovery and execution to Odoo's standard
  payment-registration wizard. Preview may create bounded ephemeral wizard
  records but never executes a payment or alters accounting records. Non-manual
  or unidentified methods report their possible external effect as unknown.
  Execution requires an idempotency key and a freshly revalidated wizard route.

Report results are deterministically ordered and cursor-paginated with a default
limit of 100 and maximum of 500. Empty data is a successful empty report;
upstream denial, timeout, malformed data, or partial retrieval is a structured
failure rather than an empty result. Paginated Markdown artifacts label the row
range, continuation state, and whole-report totals explicitly.

## Verification

One command runs formatting checks, static analysis, tests, package builds, and
artifact inspection in temporary directories:

```powershell
uv run python scripts/verify.py
```

Automated tests use synthetic Odoo responses. They do not establish live Odoo
version compatibility, real permissions, installed modules, or deployment
networking.
