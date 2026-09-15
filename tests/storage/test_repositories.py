from __future__ import annotations

import sqlite3

import pytest

from odoo_mcp.storage import ProposalState, ProposalTransitionError, Storage, new_request_id


def test_proposals_artifacts_and_capabilities_are_tenant_and_company_scoped(tmp_path) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    proposal = storage.proposals.create(
        request_id="req_1",
        tenant_id="tenant-a",
        company_id=3,
        tool_name="reconcile",
        module="accounting",
        proposal_type="match",
        payload={"line_id": 7},
    )
    artifact = storage.artifacts.create(
        request_id="req_1",
        tenant_id="tenant-a",
        company_id=3,
        tool_name="reconcile",
        module="accounting",
        artifact_type="proposal",
        artifact_format="markdown",
        content="# Synthetic proposal",
    )
    storage.capabilities.put(
        tenant_id="tenant-a",
        connection_id="connection-a",
        odoo_version="19",
        capabilities={"base": True, "account": True},
    )

    assert storage.proposals.get("tenant-a", proposal.id) == proposal
    assert storage.proposals.get("tenant-b", proposal.id) is None
    assert storage.artifacts.get("tenant-a", artifact.id) == artifact
    assert storage.artifacts.get("tenant-b", artifact.id) is None
    assert storage.capabilities.get("tenant-a", "connection-a", "19") == {
        "account": True,
        "base": True,
    }
    assert storage.capabilities.get("tenant-a", "connection-b", "19") is None
    assert storage.capabilities.get("tenant-a", "connection-a", "18") is None
    assert storage.capabilities.get("tenant-b", "connection-a", "19") is None


def test_proposal_state_machine_rejects_invalid_and_concurrent_transitions(tmp_path) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    proposal = storage.proposals.create(
        request_id="req_1",
        tenant_id="tenant-a",
        company_id=3,
        tool_name="reconcile",
        module="accounting",
        proposal_type="match",
        payload={"line_id": 7},
    )
    executing = storage.proposals.transition(
        tenant_id="tenant-a",
        proposal_id=proposal.id,
        expected=ProposalState.PROPOSED,
        target=ProposalState.EXECUTING,
    )
    assert executing.status is ProposalState.EXECUTING

    with pytest.raises(ProposalTransitionError):
        storage.proposals.transition(
            tenant_id="tenant-a",
            proposal_id=proposal.id,
            expected=ProposalState.PROPOSED,
            target=ProposalState.EXPIRED,
        )
    with pytest.raises(ProposalTransitionError):
        storage.proposals.transition(
            tenant_id="tenant-a",
            proposal_id=proposal.id,
            expected=ProposalState.EXECUTING,
            target=ProposalState.EXPIRED,
        )


def test_request_ids_are_opaque_and_unique() -> None:
    first = new_request_id()
    second = new_request_id()
    assert first.startswith("req_")
    assert second.startswith("req_")
    assert first != second


def test_repositories_reject_unscoped_records(tmp_path) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    with pytest.raises(ValueError, match="tenant_id"):
        storage.capabilities.put("", "connection-a", "19", {"base": True})
    with pytest.raises(ValueError, match="company_id"):
        storage.proposals.create("req_1", "tenant-a", 0, "tool", "accounting", "proposal", {})


def test_schema_contains_only_server_owned_state(tmp_path) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    connection = sqlite3.connect(storage.database.path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    connection.close()
    assert tables == {
        "artifacts",
        "audit_log",
        "capabilities_cache",
        "erp_connections",
        "idempotency_keys",
        "proposals",
        "schema_migrations",
    }
