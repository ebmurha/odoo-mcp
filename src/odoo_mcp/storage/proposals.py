"""Atomic persistence for proposal payloads and their Markdown artifacts."""

from __future__ import annotations

from collections.abc import Mapping

from odoo_mcp.storage.database import SQLiteDatabase
from odoo_mcp.storage.models import ProposalRecord
from odoo_mcp.storage.repositories import ArtifactRepository, ProposalRepository


class ProposalJournal:
    def __init__(
        self,
        database: SQLiteDatabase,
        proposals: ProposalRepository,
        artifacts: ArtifactRepository,
    ) -> None:
        self._database = database
        self._proposals = proposals
        self._artifacts = artifacts

    def record(
        self,
        *,
        request_id: str,
        tenant_id: str,
        company_id: int,
        tool_name: str,
        module: str,
        proposal_type: str,
        payload: Mapping[str, object],
        artifact_markdown: str,
    ) -> ProposalRecord:
        with self._database.transaction(write=True) as connection:
            proposal = self._proposals.create_in_transaction(
                connection,
                request_id,
                tenant_id,
                company_id,
                tool_name,
                module,
                proposal_type,
                payload,
            )
            self._artifacts.create_in_transaction(
                connection,
                request_id,
                tenant_id,
                company_id,
                tool_name,
                module,
                proposal_type,
                "markdown",
                artifact_markdown,
            )
            return proposal
