from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from odoo_mcp.storage import AuditEvent, AuditIntegrityError, Storage


def _event(request_id: str, tenant_id: str = "tenant-a") -> AuditEvent:
    return AuditEvent(
        request_id=request_id,
        tenant_id=tenant_id,
        company_id=1,
        tool_name="synthetic_tool",
        tool_version="1.0",
        module="accounting",
        authenticated_subject="subject-a",
        mcp_client="test-client",
        odoo_db_name="synthetic-db",
        odoo_user="synthetic-user",
        odoo_version="19",
        odoo_transport="json2",
        input_payload={"company_id": 1, "api_key": "must-not-be-stored"},
        dry_run=True,
        proposed_action={"kind": "read"},
        actual_result={"count": 1},
        affected_odoo_records=["account.move:1"],
        error_code=None,
        error_message=None,
        final_status="succeeded",
    )


def test_audit_chain_is_tenant_scoped_append_only_and_secret_safe(tmp_path) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")

    first = storage.audit.append(_event("req_1"))
    second = storage.audit.append(_event("req_2"))
    other = storage.audit.append(_event("req_3", tenant_id="tenant-b"))

    assert first.previous_hash == "0" * 64
    assert second.previous_hash == first.entry_hash
    assert other.previous_hash == "0" * 64
    assert storage.audit.verify_chain("tenant-a").entry_count == 2
    assert [row.request_id for row in storage.audit.list_for_tenant("tenant-a")] == [
        "req_1",
        "req_2",
    ]
    assert "must-not-be-stored" not in (tmp_path / "state.sqlite3").read_bytes().decode(
        "utf-8", errors="ignore"
    )

    connection = sqlite3.connect(storage.database.path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE audit_log SET final_status = 'failed' WHERE id = ?", (first.id,))
    connection.close()


def test_audit_verification_detects_tampering(tmp_path) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    record = storage.audit.append(_event("req_1"))
    connection = sqlite3.connect(storage.database.path)
    connection.execute("DROP TRIGGER audit_log_no_update")
    connection.execute("UPDATE audit_log SET final_status = 'failed' WHERE id = ?", (record.id,))
    connection.commit()
    connection.close()

    with pytest.raises(AuditIntegrityError, match="verification failed"):
        storage.audit.verify_chain("tenant-a")


def test_concurrent_audit_appends_form_one_valid_chain(tmp_path) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda value: storage.audit.append(_event(f"req_{value}")), range(12)))

    assert storage.audit.verify_chain("tenant-a").entry_count == 12
