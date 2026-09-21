FROM python:3.11.16-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system odoo-mcp \
    && useradd --system --gid odoo-mcp --home-dir /var/lib/odoo-mcp odoo-mcp \
    && mkdir -p /app /var/lib/odoo-mcp \
    && chown -R odoo-mcp:odoo-mcp /app /var/lib/odoo-mcp

WORKDIR /app
COPY --chown=odoo-mcp:odoo-mcp . /app
RUN python -m pip install --no-cache-dir .

USER odoo-mcp
EXPOSE 8000
ENTRYPOINT ["odoo-mcp"]
CMD ["--profile", "dedicated", "--config", "/app/config/config.example.yaml", "--host", "0.0.0.0", "--port", "8000", "--storage", "/var/lib/odoo-mcp/state.sqlite3"]
