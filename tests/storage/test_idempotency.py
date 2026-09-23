from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from odoo_mcp.storage import (
    AuditEvent,
    IdempotencyDisposition,
    IdempotencyPayloadMismatch,
    IdempotencyState,
    IdempotencyTransitionError,
    Storage,
)


def _write_event(request_id: str, final_status: str) -> AuditEvent:
    return AuditEvent(
        request_id=request_id,
        tenant_id="tenant-a",
        company_id=1,
        tool_name="post_entry",
        tool_version="1.0",
        module="accounting",
        authenticated_subject="subject-a",
        mcp_client="test-client",
        odoo_db_name="synthetic-db",
        odoo_user="synthetic-user",
        odoo_version="19",
        odoo_transport="json2",
        input_payload={"entry_id": 4},
        dry_run=False,
        proposed_action={"entry_id": 4},
        actual_result=None,
        affected_odoo_records=(),
        error_code=None,
        error_message=None,
        final_status=final_status,
    )


def test_idempotency_replay_mismatch_and_owner_binding_survive_restart(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    storage = Storage.open(path)
    first = storage.idempotency.reserve(
        tenant_id="tenant-a",
        company_id=1,
        tool_name="create_invoice",
        idempotency_key="key-1",
        request_payload={"amount": "10.00"},
        owner_request_id="req_1",
    )
    assert first.disposition is IdempotencyDisposition.RESERVED

    storage.idempotency.finish(
        tenant_id="tenant-a",
        company_id=1,
        tool_name="create_invoice",
        idempotency_key="key-1",
        owner_request_id="req_1",
        state=IdempotencyState.SUCCEEDED,
        response={"status": "succeeded", "id": 7},
    )

    restarted = Storage.open(path)
    replay = restarted.idempotency.reserve(
        tenant_id="tenant-a",
        company_id=1,
        tool_name="create_invoice",
        idempotency_key="key-1",
        request_payload={"amount": "10.00"},
        owner_request_id="req_2",
    )
    assert replay.disposition is IdempotencyDisposition.REPLAY
    assert replay.state is IdempotencyState.SUCCEEDED
    assert replay.response == {"id": 7, "status": "succeeded"}

    with pytest.raises(IdempotencyPayloadMismatch):
        restarted.idempotency.reserve(
            tenant_id="tenant-a",
            company_id=1,
            tool_name="create_invoice",
            idempotency_key="key-1",
            request_payload={"amount": "11.00"},
            owner_request_id="req_3",
        )
    with pytest.raises(IdempotencyPayloadMismatch):
        restarted.idempotency.reserve(
            tenant_id="tenant-a",
            company_id=2,
            tool_name="create_invoice",
            idempotency_key="key-1",
            request_payload={"amount": "10.00"},
            owner_request_id="req_4",
        )
    with pytest.raises(IdempotencyTransitionError):
        restarted.idempotency.finish(
            tenant_id="tenant-a",
            company_id=1,
            tool_name="create_invoice",
            idempotency_key="key-1",
            owner_request_id="req_wrong",
            state=IdempotencyState.FAILED,
            response={"status": "failed"},
        )


def test_concurrent_duplicate_has_one_owner(tmp_path) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")

    def reserve(index: int) -> IdempotencyDisposition:
        return storage.idempotency.reserve(
            tenant_id="tenant-a",
            company_id=1,
            tool_name="post_entry",
            idempotency_key="same-key",
            request_payload={"entry_id": 4},
            owner_request_id=f"req_{index}",
        ).disposition

    with ThreadPoolExecutor(max_workers=8) as pool:
        dispositions = list(pool.map(reserve, range(8)))

    assert dispositions.count(IdempotencyDisposition.RESERVED) == 1
    assert dispositions.count(IdempotencyDisposition.IN_PROGRESS) == 7


def test_unknown_outcome_is_replayed_not_reserved_again(tmp_path) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    storage.idempotency.reserve(
        tenant_id="tenant-a",
        company_id=1,
        tool_name="post_entry",
        idempotency_key="unknown-key",
        request_payload={"entry_id": 4},
        owner_request_id="req_1",
    )
    storage.idempotency.finish(
        tenant_id="tenant-a",
        company_id=1,
        tool_name="post_entry",
        idempotency_key="unknown-key",
        owner_request_id="req_1",
        state=IdempotencyState.UNKNOWN,
        response={"status": "unknown"},
    )

    replay = storage.idempotency.reserve(
        tenant_id="tenant-a",
        company_id=1,
        tool_name="post_entry",
        idempotency_key="unknown-key",
        request_payload={"entry_id": 4},
        owner_request_id="req_2",
    )
    assert replay.disposition is IdempotencyDisposition.REPLAY
    assert replay.state is IdempotencyState.UNKNOWN


def test_expired_unknown_and_in_progress_outcomes_still_block_reexecution(tmp_path) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    for key in ("in-progress", "unknown"):
        storage.idempotency.reserve("tenant-a", 1, "post_entry", key, {"entry_id": 4}, f"req_{key}")
    storage.idempotency.finish(
        "tenant-a",
        1,
        "post_entry",
        "unknown",
        "req_unknown",
        IdempotencyState.UNKNOWN,
        {"status": "unknown"},
    )
    expired = datetime(2000, 1, 1, tzinfo=UTC).isoformat()
    database = sqlite3.connect(storage.database.path)
    database.execute("UPDATE idempotency_keys SET expires_at = ?", (expired,))
    database.commit()
    database.close()

    in_progress = storage.idempotency.reserve(
        "tenant-a", 1, "post_entry", "in-progress", {"entry_id": 4}, "req_new"
    )
    unknown = storage.idempotency.reserve(
        "tenant-a", 1, "post_entry", "unknown", {"entry_id": 4}, "req_new"
    )
    assert in_progress.disposition is IdempotencyDisposition.IN_PROGRESS
    assert unknown.disposition is IdempotencyDisposition.REPLAY
    assert unknown.state is IdempotencyState.UNKNOWN


def test_write_attempt_and_outcome_are_atomic_with_their_audit_events(tmp_path) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    database = sqlite3.connect(storage.database.path)
    database.execute(
        """
        CREATE TRIGGER reject_attempt BEFORE INSERT ON audit_log
        WHEN NEW.final_status = 'attempted'
        BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END
        """
    )
    database.commit()
    database.close()

    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        storage.execution.begin(_write_event("req_1", "attempted"), "key-atomic", {"entry_id": 4})

    database = sqlite3.connect(storage.database.path)
    count = database.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0]
    database.execute("DROP TRIGGER reject_attempt")
    database.commit()
    database.close()
    assert count == 0

    decision = storage.execution.begin(
        _write_event("req_1", "attempted"), "key-atomic", {"entry_id": 4}
    )
    assert decision.disposition is IdempotencyDisposition.RESERVED
    outcome = _write_event("req_1", "unknown")
    storage.execution.finish(
        outcome,
        "key-atomic",
        IdempotencyState.UNKNOWN,
        {"status": "unknown"},
    )
    assert [record.final_status for record in storage.audit.list_for_tenant("tenant-a")] == [
        "attempted",
        "unknown",
    ]


def test_write_outcome_cannot_change_the_reserved_company(tmp_path) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    storage.execution.begin(_write_event("req_1", "attempted"), "key-company", {"entry_id": 4})

    with pytest.raises(IdempotencyTransitionError):
        storage.execution.finish(
            replace(_write_event("req_1", "succeeded"), company_id=2),
            "key-company",
            IdempotencyState.SUCCEEDED,
            {"status": "succeeded"},
        )

    database = sqlite3.connect(storage.database.path)
    idempotency_row = database.execute(
        "SELECT company_id, state FROM idempotency_keys WHERE idempotency_key = ?",
        ("key-company",),
    ).fetchone()
    audit_rows = database.execute(
        "SELECT company_id, final_status FROM audit_log ORDER BY id"
    ).fetchall()
    database.close()
    assert idempotency_row == (1, "in_progress")
    assert audit_rows == [(1, "attempted")]


def test_final_idempotency_state_requires_a_replayable_response(tmp_path) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    storage.idempotency.reserve(
        "tenant-a", 1, "post_entry", "key-response", {"entry_id": 4}, "req_1"
    )

    with pytest.raises(IdempotencyTransitionError, match="replayable response"):
        storage.idempotency.finish(
            "tenant-a",
            1,
            "post_entry",
            "key-response",
            "req_1",
            IdempotencyState.SUCCEEDED,
            None,
        )
