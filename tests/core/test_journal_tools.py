from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from mcp import Client

from odoo_mcp.adapters.accounting import (
    Account,
    Currency,
    Journal,
    JournalEntry,
    JournalEntryDraft,
    JournalEntryLine,
    PageRequest,
    ReadFilters,
    RecordPage,
    RelatedRecord,
)
from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.connections import ConnectionBinding
from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings
from odoo_mcp.mcp.server import create_mcp_server
from odoo_mcp.storage import Storage


@dataclass
class Resolver:
    binding: ConnectionBinding

    async def resolve(self) -> ConnectionBinding:
        return self.binding


@dataclass
class JournalState:
    state: str = "draft"
    create_count: int = 0
    post_count: int = 0


def _entry(state: str) -> JournalEntry:
    return JournalEntry(
        id=101,
        name="MISC/101",
        move_type="entry",
        state=state,
        date=date(2026, 9, 1),
        journal=RelatedRecord(id=5, name="Miscellaneous"),
        company_id=1,
        currency=RelatedRecord(id=1, name="KES"),
        reference="Synthetic",
        lines=(
            JournalEntryLine(
                id=1,
                account=RelatedRecord(id=10, name="Debit"),
                debit=Decimal("100"),
                credit=Decimal("0"),
            ),
            JournalEntryLine(
                id=2,
                account=RelatedRecord(id=20, name="Credit"),
                debit=Decimal("0"),
                credit=Decimal("100"),
            ),
        ),
    )


class JournalAdapter:
    def __init__(self, state: JournalState, *, returned_move_id: int = 101) -> None:
        self.state = state
        self.returned_move_id = returned_move_id

    async def get_capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            edition="enterprise", version=19, transport="json2", modules={"account": True}
        )

    async def get_companies(self) -> list[Company]:
        return [Company(id=1, name="Synthetic Company", currency=RelatedRecord(id=1, name="KES"))]

    async def get_currencies(self, *_args: object, **_kwargs: object) -> RecordPage[Currency]:
        return RecordPage(items=[Currency(id=1, name="KES", rounding=Decimal("0.01"))])

    async def get_journals(self, *_args: object, **_kwargs: object) -> RecordPage[Journal]:
        return RecordPage(
            items=[
                Journal(
                    id=5, name="Miscellaneous", code="MISC", journal_type="general", company_id=1
                )
            ]
        )

    async def get_account_accounts(
        self, company_id: int, filters: ReadFilters, *, page: PageRequest
    ) -> RecordPage[Account]:
        return RecordPage(
            items=[
                Account(
                    id=10,
                    code="1000",
                    name="Debit",
                    account_type="asset_current",
                    company_ids=(1,),
                    reconcile=False,
                ),
                Account(
                    id=20,
                    code="2000",
                    name="Credit",
                    account_type="liability_current",
                    company_ids=(1,),
                    reconcile=False,
                ),
            ]
        )

    async def create_journal_entry_draft(self, draft: JournalEntryDraft) -> JournalEntry:
        self.state.create_count += 1
        self.state.state = "draft"
        return _entry("draft")

    async def get_journal_entry(self, company_id: int, move_id: int) -> JournalEntry:
        return _entry(self.state.state).model_copy(update={"id": self.returned_move_id})

    async def post_journal_entry(self, company_id: int, move_id: int) -> JournalEntry:
        self.state.post_count += 1
        self.state.state = "posted"
        return _entry("posted").model_copy(update={"id": self.returned_move_id})

    async def close(self) -> None:
        return None


def _binding(connection: OdooConnectionSettings) -> ConnectionBinding:
    return ConnectionBinding(
        profile=DeploymentProfile.LOCAL,
        tenant_id="tenant-journal",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=frozenset({"accounting_propose"}),
        connection=connection,
    )


def _create_args(*, dry_run: bool, key: str | None = None) -> dict[str, object]:
    return {
        "company_id": 1,
        "journal_id": 5,
        "entry_date": "2026-09-01",
        "reference": "Synthetic",
        "lines": [
            {"account_id": 10, "debit": "100", "credit": "0"},
            {"account_id": 20, "debit": "0", "credit": "100"},
        ],
        "dry_run": dry_run,
        "idempotency_key": key,
    }


async def test_create_and_post_remain_separate_and_replay_safe(
    connection: OdooConnectionSettings, tmp_path
) -> None:
    state = JournalState()
    storage_path = tmp_path / "journals.sqlite3"

    async def factory(_connection: object) -> OdooAdapter:
        return JournalAdapter(state)

    server = create_mcp_server(
        Resolver(_binding(connection)), adapter_factory=factory, storage=Storage.open(storage_path)
    )
    async with Client(server) as client:
        preview = await client.call_tool("create_journal_entry", _create_args(dry_run=True))
        created = await client.call_tool(
            "create_journal_entry", _create_args(dry_run=False, key="create-101")
        )
        post_preview = await client.call_tool(
            "post_journal_entry", {"company_id": 1, "move_id": 101}
        )

    assert preview.structured_content["proposed_action"]["posts_entry"] is False
    assert created.structured_content["status"] == "succeeded"
    assert post_preview.structured_content["status"] == "preview"
    assert state.create_count == 1
    assert state.post_count == 0

    restarted = create_mcp_server(
        Resolver(_binding(connection)), adapter_factory=factory, storage=Storage.open(storage_path)
    )
    async with Client(restarted) as client:
        replay = await client.call_tool(
            "create_journal_entry", _create_args(dry_run=False, key="create-101")
        )
        posted = await client.call_tool(
            "post_journal_entry",
            {"company_id": 1, "move_id": 101, "dry_run": False, "idempotency_key": "post-101"},
        )

    assert replay.structured_content == created.structured_content
    assert posted.structured_content["status"] == "succeeded"
    assert state.create_count == 1
    assert state.post_count == 1


async def test_journal_execution_requires_an_idempotency_key(
    connection: OdooConnectionSettings, tmp_path
) -> None:
    state = JournalState()

    async def factory(_connection: object) -> OdooAdapter:
        return JournalAdapter(state)

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=Storage.open(tmp_path / "journal-key.sqlite3"),
    )
    async with Client(server) as client:
        result = await client.call_tool("create_journal_entry", _create_args(dry_run=False))

    assert result.structured_content["error_code"] == "EXECUTION_NOT_EXPLICIT"
    assert state.create_count == 0


async def test_post_rejects_substituted_move_before_mutation(
    connection: OdooConnectionSettings, tmp_path
) -> None:
    state = JournalState()

    async def factory(_connection: object) -> OdooAdapter:
        return JournalAdapter(state, returned_move_id=202)

    server = create_mcp_server(
        Resolver(_binding(connection)),
        adapter_factory=factory,
        storage=Storage.open(tmp_path / "journal-substitution.sqlite3"),
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "post_journal_entry",
            {
                "company_id": 1,
                "move_id": 101,
                "dry_run": False,
                "idempotency_key": "post-101",
            },
        )

    assert result.structured_content["error_code"] == "ODOO_API_ERROR"
    assert state.post_count == 0
