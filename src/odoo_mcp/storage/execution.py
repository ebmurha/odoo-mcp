"""Atomic persistence boundary around a future Odoo write call."""

from __future__ import annotations

from collections.abc import Mapping

from odoo_mcp.storage.audit import AuditRepository
from odoo_mcp.storage.database import SQLiteDatabase
from odoo_mcp.storage.models import (
    AuditEvent,
    IdempotencyDecision,
    IdempotencyDisposition,
    IdempotencyState,
)
from odoo_mcp.storage.repositories import IdempotencyRepository


class ExecutionJournal:
    """Commit idempotency state and its audit event in the same transaction."""

    def __init__(
        self,
        database: SQLiteDatabase,
        idempotency: IdempotencyRepository,
        audit: AuditRepository,
    ) -> None:
        self._database = database
        self._idempotency = idempotency
        self._audit = audit

    def begin(
        self,
        event: AuditEvent,
        idempotency_key: str,
        request_payload: Mapping[str, object],
    ) -> IdempotencyDecision:
        if event.company_id is None:
            raise ValueError("A write attempt requires company_id")
        with self._database.transaction(write=True) as connection:
            decision = self._idempotency.reserve_in_transaction(
                connection,
                event.tenant_id,
                event.company_id,
                event.tool_name,
                idempotency_key,
                request_payload,
                event.request_id,
            )
            if decision.disposition is IdempotencyDisposition.RESERVED:
                self._audit.append_in_transaction(connection, event)
        return decision

    def finish(
        self,
        event: AuditEvent,
        idempotency_key: str,
        state: IdempotencyState,
        response: Mapping[str, object] | None,
    ) -> None:
        if event.company_id is None:
            raise ValueError("A write outcome requires company_id")
        with self._database.transaction(write=True) as connection:
            self._idempotency.finish_in_transaction(
                connection,
                event.tenant_id,
                event.company_id,
                event.tool_name,
                idempotency_key,
                event.request_id,
                state,
                response,
            )
            self._audit.append_in_transaction(connection, event)
