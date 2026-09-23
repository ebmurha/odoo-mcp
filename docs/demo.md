# Accounting Demo

This demonstration shows the workflow-oriented accounting surface without
generic model access. Use a dedicated non-production Odoo technical user and an
authorized company containing suitable synthetic records.

Start Local Development or a protected Dedicated Remote endpoint with the full
example permission map. In the MCP client, perform this sequence:

1. Call `get_erp_capabilities` and confirm the authorized company, Odoo version,
   selected transport, Accounting capability, and available tools.
2. Call `get_trial_balance` for a closed synthetic period and confirm opening,
   period, and closing totals reconcile.
3. Call `get_aged_receivables` for the same company and inspect bucket totals.
4. Call `flag_unmatched_statement_lines` for a synthetic bank period.
5. Pass selected line IDs to `reconcile_bank_statement_lines` with the default
   `dry_run: true`. Confirm that it proposes matches without finalizing Odoo
   reconciliation.
6. Preview `create_customer_invoice`, then preview `validate_invoice` and
   `register_payment` against an existing synthetic draft/invoice as applicable.
   Do not set `dry_run: false` during release qualification. Payment preview may
   create an ephemeral standard Odoo wizard but does not execute a payment.
7. Run `odoo-mcp-admin verify` against the stopped or drained local database and
   verify that the tenant audit chain and stored state are valid.

Record only pass/fail stages, structured error codes, version/transport, and
reconciled booleans. Do not capture credentials, names, company/account IDs,
amounts, raw Odoo payloads, database files, or artifact contents.

Expected failures are part of the demonstration: an unauthorized company,
missing capability, Odoo ACL denial, invalid period, malformed response, or
unreconciled partial source must return a structured failure and must not be
presented as empty or successful data.
