# Security Policy

## Supported version

Security fixes are provided for the latest released `0.1.x` version. Upgrade to
the latest patch release before reporting a problem.

## Reporting a vulnerability

Do not include credentials, database files, Odoo records, or exploit details in
a public issue. Use GitHub's private security-advisory flow for this repository.
Include the affected version, impact, and a minimal synthetic reproduction.

## Deployment boundary

- Use a dedicated non-admin Odoo technical user with access only to required
  companies and modules. Odoo ACLs and record rules remain authoritative.
- Keep Odoo credentials, Shared Hosted encryption keys, and remote-connector
  credentials in an operator-controlled secret store. Never bake them into an
  image, commit them, or place them in command arguments.
- Terminate TLS and authenticate remote MCP clients before supplying a trusted
  connector identity. Forwarded identity headers are not trusted by this
  package.
- Bind the included Dedicated Remote template to loopback and place it behind a
  hardened reverse proxy or private network boundary.
- Back up SQLite data and Shared Hosted key material separately. A database
  containing encrypted connections is not recoverable without the matching
  external keys.

The `/healthz` route is unauthenticated and reports liveness only. It contains
no configuration, dependency, tenant, company, or Odoo status.
