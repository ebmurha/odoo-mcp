"""Atomic artifact and audit persistence for read reports."""

from __future__ import annotations

from odoo_mcp.storage.audit import AuditRepository
from odoo_mcp.storage.database import SQLiteDatabase
from odoo_mcp.storage.models import ArtifactRecord, AuditEvent
from odoo_mcp.storage.repositories import ArtifactRepository


class ReportJournal:
    def __init__(
        self,
        database: SQLiteDatabase,
        artifacts: ArtifactRepository,
        audit: AuditRepository,
    ) -> None:
        self._database = database
        self._artifacts = artifacts
        self._audit = audit

    def record_success(
        self,
        event: AuditEvent,
        *,
        artifact_type: str,
        content: str,
    ) -> ArtifactRecord:
        if event.company_id is None:
            raise ValueError("A report requires company_id")
        with self._database.transaction(write=True) as connection:
            artifact = self._artifacts.create_in_transaction(
                connection,
                event.request_id,
                event.tenant_id,
                event.company_id,
                event.tool_name,
                event.module,
                artifact_type,
                "markdown",
                content,
            )
            self._audit.append_in_transaction(connection, event)
            return artifact
