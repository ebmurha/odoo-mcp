from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from mcp.types import ToolAnnotations

from odoo_mcp.adapters.base import CapabilitySnapshot
from odoo_mcp.adapters.odoo.connections import ConnectionBinding
from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.registry import ToolDefinition
from odoo_mcp.policy.write_safety import (
    AppliedWrite,
    KnownWriteFailure,
    PreparedWrite,
    UnknownWriteOutcome,
    WriteCommand,
    WriteSafetyCoordinator,
    validate_write_tool_definition,
)
from odoo_mcp.storage import Storage


def _definition() -> ToolDefinition:
    return ToolDefinition(
        name="synthetic_write",
        version="1.0.0",
        title="Synthetic write",
        description="Exercise the reusable write-safety boundary.",
        risk_level="draft_write",
        required_permission="accounting_propose",
        required_capability="account",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )


def _binding(
    connection: OdooConnectionSettings,
    *,
    tenant_id: str = "tenant-a",
    permissions: frozenset[str] = frozenset({"accounting_propose"}),
) -> ConnectionBinding:
    return ConnectionBinding(
        profile=DeploymentProfile.LOCAL,
        tenant_id=tenant_id,
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=permissions,
        connection=connection,
    )


def _snapshot(*, account: bool = True) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        edition="enterprise",
        version=19,
        transport="json2",
        modules={"base": True, "account": account},
    )


async def _prepared() -> PreparedWrite:
    return PreparedWrite(
        proposed_action={"operation": "create_draft"},
        material_effects={"amount": "10.00"},
    )


async def _valid_state() -> None:
    return None


async def _applied() -> AppliedWrite:
    return AppliedWrite(
        material_effects={"amount": "10.00"},
        record_refs=("account.move:41",),
    )


async def _run(
    coordinator: WriteSafetyCoordinator,
    binding: ConnectionBinding,
    command: WriteCommand,
    *,
    snapshot: CapabilitySnapshot | None = None,
    prepare: Callable[[], Awaitable[PreparedWrite]] = _prepared,
    validate_current_state: Callable[[], Awaitable[None]] = _valid_state,
    execute: Callable[[], Awaitable[AppliedWrite]] = _applied,
):
    return await coordinator.run(
        binding=binding,
        definition=_definition(),
        module="accounting",
        snapshot=snapshot or _snapshot(),
        command=command,
        prepare=prepare,
        validate_current_state=validate_current_state,
        execute=execute,
    )


def test_write_metadata_must_match_write_safety_contract() -> None:
    validate_write_tool_definition(_definition())
    with pytest.raises(ValueError, match="annotations"):
        ToolDefinition(
            **{
                **_definition().__dict__,
                "annotations": ToolAnnotations(
                    read_only_hint=True,
                    destructive_hint=False,
                    idempotent_hint=False,
                    open_world_hint=True,
                ),
            }
        )


@pytest.mark.parametrize("destructive_hint", [False, None])
def test_confirm_write_requires_destructive_hint_true(
    destructive_hint: bool | None,
) -> None:
    with pytest.raises(ValueError, match="annotations"):
        ToolDefinition(
            name="synthetic_confirm",
            version="1.0.0",
            title="Synthetic confirm",
            description="Confirm a synthetic record.",
            risk_level="confirm_write",
            required_permission="accounting_propose",
            required_capability="account",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=destructive_hint,
                idempotent_hint=True,
                open_world_hint=True,
            ),
        )

    bypassed = _definition()
    object.__setattr__(bypassed, "risk_level", "confirm_write")
    with pytest.raises(ValueError, match="annotations"):
        validate_write_tool_definition(bypassed)


def test_confirm_write_accepts_explicit_destructive_hint_true() -> None:
    definition = ToolDefinition(
        name="synthetic_confirm",
        version="1.0.0",
        title="Synthetic confirm",
        description="Confirm a synthetic record.",
        risk_level="confirm_write",
        required_permission="accounting_propose",
        required_capability="account",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )

    validate_write_tool_definition(definition)


async def test_dry_run_defaults_to_preview_and_never_checks_or_executes(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    coordinator = WriteSafetyCoordinator(storage)
    calls: list[str] = []

    async def prepare() -> PreparedWrite:
        calls.append("prepare")
        return await _prepared()

    async def state() -> None:
        calls.append("state")

    async def execute() -> AppliedWrite:
        calls.append("execute")
        return await _applied()

    result = await _run(
        coordinator,
        _binding(connection),
        WriteCommand(company_id=1, request_payload={"amount": "10.00"}),
        prepare=prepare,
        validate_current_state=state,
        execute=execute,
    )

    assert result.status == "preview"
    assert result.record_refs == ()
    assert calls == ["prepare"]
    records = storage.audit.list_for_tenant("tenant-a")
    assert [(record.dry_run, record.final_status) for record in records] == [(True, "previewed")]
    with storage.database.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0


async def test_malformed_preview_preparation_is_structured_and_audited(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    state_checked = False
    executed = False

    async def prepare() -> object:
        return object()

    async def state() -> None:
        nonlocal state_checked
        state_checked = True

    async def execute() -> AppliedWrite:
        nonlocal executed
        executed = True
        return await _applied()

    result = await _run(
        WriteSafetyCoordinator(storage),
        _binding(connection),
        WriteCommand(company_id=1, request_payload={"amount": "10.00"}),
        prepare=prepare,  # type: ignore[arg-type]
        validate_current_state=state,
        execute=execute,
    )

    assert result.status == "failed"
    assert result.outcome == "not_attempted"
    assert result.error_code is ErrorCode.UNKNOWN_ERROR
    assert state_checked is False
    assert executed is False
    records = storage.audit.list_for_tenant("tenant-a")
    assert [record.final_status for record in records] == ["failed"]
    storage.audit.verify_chain("tenant-a")


@pytest.mark.parametrize(
    "malformed",
    [
        PreparedWrite(
            proposed_action=object(),  # type: ignore[arg-type]
            material_effects={},
        ),
        PreparedWrite(
            proposed_action={},
            material_effects={"invalid": object()},
        ),
        PreparedWrite(
            proposed_action={},
            material_effects={},
            artifact_markdown=42,  # type: ignore[arg-type]
        ),
        PreparedWrite(
            proposed_action={},
            material_effects={},
            needs_input="yes",  # type: ignore[arg-type]
        ),
    ],
)
async def test_malformed_prepared_fields_fail_inside_preview_boundary(
    tmp_path,
    connection: OdooConnectionSettings,
    malformed: PreparedWrite,
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")

    async def prepare() -> PreparedWrite:
        return malformed

    result = await _run(
        WriteSafetyCoordinator(storage),
        _binding(connection),
        WriteCommand(company_id=1, request_payload={"amount": "10.00"}),
        prepare=prepare,
    )

    assert result.status == "failed"
    assert result.error_code is ErrorCode.UNKNOWN_ERROR
    assert [record.final_status for record in storage.audit.list_for_tenant("tenant-a")] == [
        "failed"
    ]


async def test_preview_preparation_cancellation_is_audited_then_propagated(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")

    async def prepare() -> PreparedWrite:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _run(
            WriteSafetyCoordinator(storage),
            _binding(connection),
            WriteCommand(company_id=1, request_payload={"amount": "10.00"}),
            prepare=prepare,
        )

    records = storage.audit.list_for_tenant("tenant-a")
    assert [record.final_status for record in records] == ["failed"]
    assert records[0].dry_run is True
    assert records[0].error_code == ErrorCode.UNKNOWN_ERROR.value
    with storage.database.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0
    storage.audit.verify_chain("tenant-a")


@pytest.mark.parametrize(
    ("binding_permissions", "company_id", "account", "expected_code"),
    [
        (frozenset(), 1, True, ErrorCode.ODOO_AUTH_FAILED),
        (frozenset({"accounting_propose"}), 99, True, ErrorCode.COMPANY_NOT_FOUND),
        (
            frozenset({"accounting_propose"}),
            1,
            False,
            ErrorCode.CAPABILITY_NOT_AVAILABLE,
        ),
    ],
)
async def test_all_authorization_gates_precede_workflow(
    tmp_path,
    connection: OdooConnectionSettings,
    binding_permissions: frozenset[str],
    company_id: int,
    account: bool,
    expected_code: ErrorCode,
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    called = False

    async def prepare() -> PreparedWrite:
        nonlocal called
        called = True
        return await _prepared()

    result = await _run(
        WriteSafetyCoordinator(storage),
        _binding(connection, permissions=binding_permissions),
        WriteCommand(company_id=company_id, request_payload={"amount": "10.00"}),
        snapshot=_snapshot(account=account),
        prepare=prepare,
    )

    assert called is False
    assert result.status == "failed"
    assert result.error_code is expected_code
    assert storage.audit.list_for_tenant("tenant-a")[-1].final_status == "rejected"


@pytest.mark.parametrize(
    ("reserved_key", "spoofed_value", "authoritative_value"),
    [
        ("company_id", 999, 1),
        ("dry_run", False, True),
        ("idempotency_key", "spoofed", None),
    ],
)
async def test_business_payload_cannot_overwrite_authoritative_audit_controls(
    tmp_path,
    connection: OdooConnectionSettings,
    reserved_key: str,
    spoofed_value: object,
    authoritative_value: object,
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    called = False

    async def prepare() -> PreparedWrite:
        nonlocal called
        called = True
        return await _prepared()

    result = await _run(
        WriteSafetyCoordinator(storage),
        _binding(connection),
        WriteCommand(
            company_id=1,
            request_payload={reserved_key: spoofed_value, "amount": "10.00"},
        ),
        prepare=prepare,
    )

    assert called is False
    assert result.error_code is ErrorCode.INVALID_INPUT
    record = storage.audit.list_for_tenant("tenant-a")[-1]
    assert record.final_status == "rejected"
    assert record.input_payload[reserved_key] == authoritative_value
    assert record.company_id == 1
    assert record.dry_run is True


async def test_execution_requires_explicit_false_and_non_empty_key(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    called = False

    async def prepare() -> PreparedWrite:
        nonlocal called
        called = True
        return await _prepared()

    result = await _run(
        WriteSafetyCoordinator(storage),
        _binding(connection),
        WriteCommand(company_id=1, dry_run=False, request_payload={"amount": "10.00"}),
        prepare=prepare,
    )

    assert called is False
    assert result.error_code is ErrorCode.EXECUTION_NOT_EXPLICIT
    assert storage.audit.list_for_tenant("tenant-a")[-1].final_status == "rejected"


async def test_malformed_execution_preparation_finalizes_failed_and_replays(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    coordinator = WriteSafetyCoordinator(storage)
    preparations = 0
    state_checked = False
    executed = False

    async def prepare() -> object:
        nonlocal preparations
        preparations += 1
        return object()

    async def state() -> None:
        nonlocal state_checked
        state_checked = True

    async def execute() -> AppliedWrite:
        nonlocal executed
        executed = True
        return await _applied()

    command = WriteCommand(
        company_id=1,
        dry_run=False,
        idempotency_key="malformed-prepare",
        request_payload={"amount": "10.00"},
    )
    first = await _run(
        coordinator,
        _binding(connection),
        command,
        prepare=prepare,  # type: ignore[arg-type]
        validate_current_state=state,
        execute=execute,
    )
    replay = await _run(
        coordinator,
        _binding(connection),
        command,
        prepare=prepare,  # type: ignore[arg-type]
        validate_current_state=state,
        execute=execute,
    )

    assert first.status == "failed"
    assert first.outcome == "not_attempted"
    assert first.error_code is ErrorCode.UNKNOWN_ERROR
    assert replay == first
    assert preparations == 1
    assert state_checked is False
    assert executed is False
    records = storage.audit.list_for_tenant("tenant-a")
    assert [record.final_status for record in records] == [
        "attempted",
        "failed",
        "replayed",
    ]
    with storage.database.transaction() as database:
        row = database.execute(
            "SELECT state, response_json FROM idempotency_keys WHERE idempotency_key = ?",
            ("malformed-prepare",),
        ).fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert '"outcome":"not_attempted"' in row[1]
    storage.audit.verify_chain("tenant-a")


@pytest.mark.parametrize("cancel_phase", ["prepare", "state"])
async def test_pre_mutation_cancellation_finalizes_failed_then_replays(
    tmp_path,
    connection: OdooConnectionSettings,
    cancel_phase: str,
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    coordinator = WriteSafetyCoordinator(storage)
    preparations = 0
    state_checks = 0
    executions = 0

    async def prepare() -> PreparedWrite:
        nonlocal preparations
        preparations += 1
        if cancel_phase == "prepare":
            raise asyncio.CancelledError
        return await _prepared()

    async def state() -> None:
        nonlocal state_checks
        state_checks += 1
        if cancel_phase == "state":
            raise asyncio.CancelledError

    async def execute() -> AppliedWrite:
        nonlocal executions
        executions += 1
        return await _applied()

    command = WriteCommand(
        company_id=1,
        dry_run=False,
        idempotency_key=f"cancel-{cancel_phase}",
        request_payload={"amount": "10.00"},
    )
    with pytest.raises(asyncio.CancelledError):
        await _run(
            coordinator,
            _binding(connection),
            command,
            prepare=prepare,
            validate_current_state=state,
            execute=execute,
        )

    with storage.database.transaction() as database:
        row = database.execute(
            "SELECT state, response_json FROM idempotency_keys WHERE idempotency_key = ?",
            (f"cancel-{cancel_phase}",),
        ).fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert '"outcome":"not_attempted"' in row[1]
    assert [record.final_status for record in storage.audit.list_for_tenant("tenant-a")] == [
        "attempted",
        "failed",
    ]

    replay = await _run(
        coordinator,
        _binding(connection),
        command,
        prepare=prepare,
        validate_current_state=state,
        execute=execute,
    )

    assert replay.status == "failed"
    assert replay.outcome == "not_attempted"
    assert replay.error_code is ErrorCode.UNKNOWN_ERROR
    assert preparations == 1
    assert state_checks == (1 if cancel_phase == "state" else 0)
    assert executions == 0
    assert [record.final_status for record in storage.audit.list_for_tenant("tenant-a")] == [
        "attempted",
        "failed",
        "replayed",
    ]
    storage.audit.verify_chain("tenant-a")


async def test_success_reserves_and_audits_before_fresh_state_and_write(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    calls: list[str] = []

    async def prepare() -> PreparedWrite:
        calls.append("prepare")
        return await _prepared()

    async def state() -> None:
        calls.append("state")
        records = storage.audit.list_for_tenant("tenant-a")
        assert [record.final_status for record in records] == ["attempted"]

    async def execute() -> AppliedWrite:
        calls.append("execute")
        return await _applied()

    result = await _run(
        WriteSafetyCoordinator(storage),
        _binding(connection),
        WriteCommand(
            company_id=1,
            dry_run=False,
            idempotency_key="write-1",
            request_payload={"amount": "10.00"},
        ),
        prepare=prepare,
        validate_current_state=state,
        execute=execute,
    )

    assert result.status == "succeeded"
    assert result.record_refs == ("account.move:41",)
    assert calls == ["prepare", "state", "execute"]
    records = storage.audit.list_for_tenant("tenant-a")
    assert [record.final_status for record in records] == ["attempted", "succeeded"]
    storage.audit.verify_chain("tenant-a")


async def test_stale_state_fails_without_mutation_and_replays(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    coordinator = WriteSafetyCoordinator(storage)
    executed = False

    async def stale() -> None:
        raise OdooMcpError(
            ErrorCode.ODOO_STATE_CONFLICT,
            "The Odoo record changed before execution.",
            "Refresh the record and submit a new request.",
        )

    async def execute() -> AppliedWrite:
        nonlocal executed
        executed = True
        return await _applied()

    command = WriteCommand(
        company_id=1,
        dry_run=False,
        idempotency_key="stale-1",
        request_payload={"record_id": 41, "expected_state": "draft"},
    )
    first = await _run(
        coordinator,
        _binding(connection),
        command,
        validate_current_state=stale,
        execute=execute,
    )
    replay = await _run(coordinator, _binding(connection), command)

    assert executed is False
    assert first.error_code is ErrorCode.ODOO_STATE_CONFLICT
    assert replay == first
    assert [record.final_status for record in storage.audit.list_for_tenant("tenant-a")] == [
        "attempted",
        "failed",
        "replayed",
    ]


async def test_needs_input_is_bounded_non_mutating_and_replayable(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    coordinator = WriteSafetyCoordinator(storage)
    executed = False

    async def prepare() -> PreparedWrite:
        return PreparedWrite(
            proposed_action={"operation": "register_payment"},
            material_effects={"choices": [{"journal_id": 3}, {"journal_id": 4}]},
            needs_input=True,
        )

    async def execute() -> AppliedWrite:
        nonlocal executed
        executed = True
        return await _applied()

    command = WriteCommand(
        company_id=1,
        dry_run=False,
        idempotency_key="needs-input-1",
        request_payload={"amount": "10.00"},
    )
    first = await _run(
        coordinator,
        _binding(connection),
        command,
        prepare=prepare,
        execute=execute,
    )
    replay = await _run(coordinator, _binding(connection), command, execute=execute)

    assert executed is False
    assert first.status == "needs_input"
    assert replay == first
    assert [record.final_status for record in storage.audit.list_for_tenant("tenant-a")] == [
        "attempted",
        "needs_input",
        "replayed",
    ]


async def test_proven_odoo_denial_is_known_failed_and_replayable(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    coordinator = WriteSafetyCoordinator(storage)
    executions = 0

    async def execute() -> AppliedWrite:
        nonlocal executions
        executions += 1
        raise KnownWriteFailure(
            OdooMcpError(
                ErrorCode.ODOO_PERMISSION_DENIED,
                "Odoo denied the requested operation.",
                "Grant the technical user the required least-privilege access.",
            )
        )

    command = WriteCommand(
        company_id=1,
        dry_run=False,
        idempotency_key="denied-1",
        request_payload={"amount": "10.00"},
    )
    first = await _run(coordinator, _binding(connection), command, execute=execute)
    replay = await _run(coordinator, _binding(connection), command, execute=execute)

    assert executions == 1
    assert first.outcome == "known"
    assert first.error_code is ErrorCode.ODOO_PERMISSION_DENIED
    assert replay == first


async def test_altered_replay_and_in_progress_duplicate_fail_closed(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    coordinator = WriteSafetyCoordinator(storage)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def execute() -> AppliedWrite:
        entered.set()
        await release.wait()
        return await _applied()

    command = WriteCommand(
        company_id=1,
        dry_run=False,
        idempotency_key="concurrent-1",
        request_payload={"amount": "10.00"},
    )
    owner = asyncio.create_task(_run(coordinator, _binding(connection), command, execute=execute))
    await entered.wait()
    duplicate = await _run(coordinator, _binding(connection), command)
    altered = await _run(
        coordinator,
        _binding(connection),
        WriteCommand(
            company_id=1,
            dry_run=False,
            idempotency_key="concurrent-1",
            request_payload={"amount": "11.00"},
        ),
    )
    release.set()
    succeeded = await owner

    assert duplicate.error_code is ErrorCode.CONCURRENT_OPERATION_IN_PROGRESS
    assert altered.error_code is ErrorCode.IDEMPOTENCY_KEY_PAYLOAD_MISMATCH
    assert succeeded.status == "succeeded"


@pytest.mark.parametrize("failure", [UnknownWriteOutcome(), RuntimeError("synthetic-secret")])
async def test_mutation_uncertainty_is_explicit_and_never_retried(
    tmp_path,
    connection: OdooConnectionSettings,
    failure: Exception,
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    coordinator = WriteSafetyCoordinator(storage)
    executions = 0

    async def execute() -> AppliedWrite:
        nonlocal executions
        executions += 1
        raise failure

    command = WriteCommand(
        company_id=1,
        dry_run=False,
        idempotency_key="unknown-1",
        request_payload={"amount": "10.00"},
    )
    first = await _run(coordinator, _binding(connection), command, execute=execute)
    replay = await _run(coordinator, _binding(connection), command, execute=execute)

    assert executions == 1
    assert first.status == "failed"
    assert first.outcome == "unknown"
    assert first.error_code is ErrorCode.UNKNOWN_ERROR
    assert "synthetic-secret" not in str(first.model_dump())
    assert replay == first
    assert storage.audit.list_for_tenant("tenant-a")[1].final_status == "unknown"


async def test_malformed_post_mutation_result_is_finalized_unknown(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    committed: list[str] = []

    async def execute() -> object:
        committed.append("account.move:41")
        return object()

    result = await _run(
        WriteSafetyCoordinator(storage),
        _binding(connection),
        WriteCommand(
            company_id=1,
            dry_run=False,
            idempotency_key="malformed-result",
            request_payload={"amount": "10.00"},
        ),
        execute=execute,  # type: ignore[arg-type]
    )

    assert committed == ["account.move:41"]
    assert result.outcome == "unknown"
    assert result.error_code is ErrorCode.UNKNOWN_ERROR
    records = storage.audit.list_for_tenant("tenant-a")
    assert [record.final_status for record in records] == ["attempted", "unknown"]
    with storage.database.transaction() as database:
        row = database.execute(
            "SELECT state, response_json FROM idempotency_keys WHERE idempotency_key = ?",
            ("malformed-result",),
        ).fetchone()
    assert row is not None
    assert row[0] == "unknown"
    assert '"outcome":"unknown"' in row[1]


async def test_cancellation_after_possible_mutation_finalizes_unknown_then_propagates(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    committed: list[str] = []

    async def execute() -> AppliedWrite:
        committed.append("account.move:41")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _run(
            WriteSafetyCoordinator(storage),
            _binding(connection),
            WriteCommand(
                company_id=1,
                dry_run=False,
                idempotency_key="cancelled-result",
                request_payload={"amount": "10.00"},
            ),
            execute=execute,
        )

    assert committed == ["account.move:41"]
    records = storage.audit.list_for_tenant("tenant-a")
    assert [record.final_status for record in records] == ["attempted", "unknown"]
    with storage.database.transaction() as database:
        row = database.execute(
            "SELECT state, response_json FROM idempotency_keys WHERE idempotency_key = ?",
            ("cancelled-result",),
        ).fetchone()
    assert row is not None
    assert row[0] == "unknown"
    assert '"outcome":"unknown"' in row[1]


async def test_audit_failure_before_write_prevents_mutation(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    with storage.database.transaction(write=True) as database:
        database.execute(
            """
            CREATE TRIGGER reject_attempt BEFORE INSERT ON audit_log
            WHEN NEW.final_status = 'attempted'
            BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END
            """
        )
    executed = False

    async def execute() -> AppliedWrite:
        nonlocal executed
        executed = True
        return await _applied()

    result = await _run(
        WriteSafetyCoordinator(storage),
        _binding(connection),
        WriteCommand(
            company_id=1,
            dry_run=False,
            idempotency_key="audit-before",
            request_payload={"amount": "10.00"},
        ),
        execute=execute,
    )

    assert executed is False
    assert result.status == "failed"
    assert result.outcome == "not_attempted"
    with storage.database.transaction() as database:
        assert database.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 0


async def test_audit_failure_after_write_returns_unknown_and_blocks_retry(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    with storage.database.transaction(write=True) as database:
        database.execute(
            """
            CREATE TRIGGER reject_success BEFORE INSERT ON audit_log
            WHEN NEW.final_status = 'succeeded'
            BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END
            """
        )
    executions = 0

    async def execute() -> AppliedWrite:
        nonlocal executions
        executions += 1
        return await _applied()

    command = WriteCommand(
        company_id=1,
        dry_run=False,
        idempotency_key="audit-after",
        request_payload={"amount": "10.00"},
    )
    first = await _run(
        WriteSafetyCoordinator(storage),
        _binding(connection),
        command,
        execute=execute,
    )
    second = await _run(
        WriteSafetyCoordinator(storage),
        _binding(connection),
        command,
        execute=execute,
    )

    assert executions == 1
    assert first.outcome == "unknown"
    assert second.error_code is ErrorCode.CONCURRENT_OPERATION_IN_PROGRESS
    assert [record.final_status for record in storage.audit.list_for_tenant("tenant-a")] == [
        "attempted",
        "conflicted",
    ]


async def test_unknown_outcome_persistence_failure_stays_safe_and_blocks_retry(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    storage = Storage.open(tmp_path / "state.sqlite3")
    with storage.database.transaction(write=True) as database:
        database.execute(
            """
            CREATE TRIGGER reject_unknown BEFORE INSERT ON audit_log
            WHEN NEW.final_status = 'unknown'
            BEGIN SELECT RAISE(ABORT, 'synthetic-secret'); END
            """
        )
    executions = 0

    async def execute() -> AppliedWrite:
        nonlocal executions
        executions += 1
        raise UnknownWriteOutcome()

    command = WriteCommand(
        company_id=1,
        dry_run=False,
        idempotency_key="unknown-persistence",
        request_payload={"amount": "10.00"},
    )
    first = await _run(
        WriteSafetyCoordinator(storage),
        _binding(connection),
        command,
        execute=execute,
    )
    second = await _run(
        WriteSafetyCoordinator(storage),
        _binding(connection),
        command,
        execute=execute,
    )

    assert executions == 1
    assert first.outcome == "unknown"
    assert "synthetic-secret" not in str(first.model_dump())
    assert second.error_code is ErrorCode.CONCURRENT_OPERATION_IN_PROGRESS
    assert [record.final_status for record in storage.audit.list_for_tenant("tenant-a")] == [
        "attempted",
        "conflicted",
    ]


async def test_final_result_replays_after_restart_and_is_tenant_scoped(
    tmp_path, connection: OdooConnectionSettings
) -> None:
    path = tmp_path / "state.sqlite3"
    command = WriteCommand(
        company_id=1,
        dry_run=False,
        idempotency_key="restart-1",
        request_payload={"amount": "10.00"},
    )
    first = await _run(
        WriteSafetyCoordinator(Storage.open(path)),
        _binding(connection),
        command,
    )
    executions = 0

    async def execute() -> AppliedWrite:
        nonlocal executions
        executions += 1
        return await _applied()

    restarted = Storage.open(path)
    replay = await _run(
        WriteSafetyCoordinator(restarted),
        _binding(connection),
        command,
        execute=execute,
    )
    other_tenant = await _run(
        WriteSafetyCoordinator(restarted),
        _binding(connection, tenant_id="tenant-b"),
        command,
        execute=execute,
    )

    assert replay == first
    assert other_tenant.status == "succeeded"
    assert executions == 1
    restarted.audit.verify_chain("tenant-a")
    restarted.audit.verify_chain("tenant-b")
