# Contributing

Use Python 3.11 or newer and install the locked development environment:

```console
uv sync --locked --all-extras
uv run python scripts/verify.py
```

Keep changes focused, add synthetic tests for behavioral changes, and update
public documentation when a public contract changes. Never commit environment
files, credentials, Odoo exports, client data, SQLite state, or raw upstream
errors. New Odoo access must remain behind the typed adapter, explicit model and
action allowlists, and field denylist. Workflows must not call Odoo transports
directly.

Open pull requests against the repository's default branch and require a review
before merge. Security reports belong in the private security-advisory flow,
not public issues.
