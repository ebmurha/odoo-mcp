from __future__ import annotations

import pytest

from odoo_mcp.storage import AuditEvent, Storage


def test_report_artifact_rolls_back_when_audit_append_fails(tmp_path) -> None:
    storage = Storage.open(tmp_path / "reporting.sqlite3")
    invalid_event = AuditEvent(
        request_id="req_report",
        tenant_id="tenant-a",
        company_id=1,
        tool_name="get_trial_balance",
        tool_version="1.0.0",
        module="accounting",
        authenticated_subject="",
        mcp_client="synthetic-client",
        odoo_db_name="synthetic-db",
        odoo_user="synthetic-user",
        odoo_version="19",
        odoo_transport="json2",
        input_payload={"company_id": 1},
        dry_run=False,
        proposed_action=None,
        actual_result={"status": "ok"},
        affected_odoo_records=(),
        error_code=None,
        error_message=None,
        final_status="succeeded",
    )

    with pytest.raises(ValueError, match="identity"):
        storage.reports.record_success(
            invalid_event,
            artifact_type="get_trial_balance",
            content="# Trial Balance",
        )

    with storage.database.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
