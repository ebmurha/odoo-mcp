FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv
FROM python:3.11.16-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system odoo-mcp \
    && useradd --system --gid odoo-mcp --home-dir /var/lib/odoo-mcp odoo-mcp \
    && mkdir -p /app /var/lib/odoo-mcp \
    && chown -R odoo-mcp:odoo-mcp /app /var/lib/odoo-mcp

WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY --chown=odoo-mcp:odoo-mcp uv.lock pyproject.toml README.md LICENSE NOTICE /app/
RUN uv sync --frozen --no-dev --no-install-project
COPY --chown=odoo-mcp:odoo-mcp . /app
RUN uv sync --frozen --no-dev

USER odoo-mcp
EXPOSE 8000
ENTRYPOINT ["/app/.venv/bin/odoo-mcp"]
CMD ["--profile", "dedicated", "--config", "/app/config/config.example.yaml", "--host", "0.0.0.0", "--port", "8000", "--storage", "/var/lib/odoo-mcp/state.sqlite3"]
