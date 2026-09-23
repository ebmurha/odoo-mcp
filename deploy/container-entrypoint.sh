#!/bin/sh
set -eu

if [ "$#" -gt 0 ]; then
  exec /app/.venv/bin/odoo-mcp "$@"
fi

exec /app/.venv/bin/odoo-mcp \
  --profile "${ODOO_MCP_PROFILE:-dedicated}" \
  --config /app/config/config.example.yaml \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --storage /var/lib/odoo-mcp/state.sqlite3
