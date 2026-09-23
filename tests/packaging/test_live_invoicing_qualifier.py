from __future__ import annotations

import importlib.util
import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from odoo_mcp.app.settings import OdooConnectionSettings
from odoo_mcp.storage import Storage
from odoo_mcp.storage.models import IdempotencyState

ROOT = Path(__file__).resolve().parents[2]


def _load_qualifier() -> ModuleType:
    path = ROOT / "scripts" / "verify_live_invoicing_odoo19.py"
    spec = importlib.util.spec_from_file_location("live_invoicing_qualifier", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _connection(
    *,
    database: str = "synthetic-db",
    allowed_company_ids: tuple[int, ...] = (1,),
    default_company_id: int = 1,
) -> OdooConnectionSettings:
    return OdooConnectionSettings(
        url="https://odoo.invalid",
        database=database,
        username="synthetic-user",
        api_key=SecretStr("synthetic-key"),
        allowed_company_ids=allowed_company_ids,
        default_company_id=default_company_id,
    )


class StubClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, tool: str, arguments: dict[str, object]) -> object:
        self.calls.append((tool, arguments))
        return SimpleNamespace(structured_content=self.response)


def _seed_success(
    storage: Storage,
    campaign: object,
    *,
    company_id: int = 1,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    response: dict[str, object] = {
        "status": "succeeded",
        "outcome": "known",
        "request_id": "synthetic-request",
        "company_id": company_id,
        "proposed_action": {},
        "material_effects": {},
        "record_refs": ["account.move:101"],
    }
    tenant_id = campaign.tenant_id
    storage.idempotency.reserve(
        tenant_id,
        company_id,
        "create_supplier_bill",
        "synthetic-key",
        payload or {"value": "original"},
        "synthetic-request",
    )
    storage.idempotency.finish(
        tenant_id,
        company_id,
        "create_supplier_bill",
        "synthetic-key",
        "synthetic-request",
        IdempotencyState.SUCCEEDED,
        response,
    )
    return response


def test_manual_route_rejects_external_methods() -> None:
    qualifier = _load_qualifier()
    external = {
        "material_effects": {
            "valid_choices": [
                {
                    "journal": {"id": 10, "name": "Synthetic"},
                    "payment_method_line": {"id": 20, "name": "Synthetic"},
                    "payment_method_code": "electronic",
                }
            ]
        }
    }
    manual = {
        "material_effects": {
            "valid_choices": [
                {
                    "journal": {"id": 11, "name": "Synthetic"},
                    "payment_method_line": {"id": 21, "name": "Synthetic"},
                    "payment_method_code": "manual",
                }
            ]
        }
    }

    assert qualifier._manual_route(external) is None
    assert qualifier._manual_route(manual) == (11, 21)


def test_qualifier_suppresses_dependency_logs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    qualifier = _load_qualifier()
    logger = logging.getLogger("httpx")
    logger.disabled = False
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    logger.addHandler(handler)

    async def synthetic_qualification() -> None:
        logger.info("configured-endpoint.invalid")

    monkeypatch.setattr(qualifier, "_qualify", synthetic_qualification)
    try:
        qualifier.main(["--execute-authorized-writes"])
    finally:
        logger.removeHandler(handler)
        logging.disable(logging.NOTSET)

    captured = capsys.readouterr()
    assert captured.out == f"{qualifier.SUCCESS}\n"
    assert captured.err == ""


def test_campaign_is_restart_stable_and_rejects_changed_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qualifier = _load_qualifier()
    monkeypatch.setattr(qualifier, "CAMPAIGN_PATH", tmp_path / "campaign.json")
    monkeypatch.setattr(qualifier, "STATE_PATH", tmp_path / "state.sqlite3")

    first = qualifier._load_campaign(_connection())
    qualifier.STATE_PATH.touch()
    restarted = qualifier._load_campaign(_connection())

    assert restarted == first
    for changed in (
        _connection(database="different-db"),
        _connection(allowed_company_ids=(2,), default_company_id=2),
    ):
        with pytest.raises(qualifier.QualificationFailure) as raised:
            qualifier._load_campaign(changed)
        assert raised.value.error_class == "connection_mismatch"


def test_existing_state_without_campaign_binding_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qualifier = _load_qualifier()
    state_path = tmp_path / "state.sqlite3"
    state_path.touch()
    monkeypatch.setattr(qualifier, "CAMPAIGN_PATH", tmp_path / "campaign.json")
    monkeypatch.setattr(qualifier, "STATE_PATH", state_path)

    with pytest.raises(qualifier.QualificationFailure) as raised:
        qualifier._load_campaign(_connection())

    assert raised.value.error_class == "campaign_binding_missing"


def test_campaign_without_state_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    qualifier = _load_qualifier()
    campaign_path = tmp_path / "campaign.json"
    state_path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(qualifier, "CAMPAIGN_PATH", campaign_path)
    monkeypatch.setattr(qualifier, "STATE_PATH", state_path)
    qualifier._load_campaign(_connection())

    with pytest.raises(qualifier.QualificationFailure) as raised:
        qualifier._load_campaign(_connection())

    assert raised.value.error_class == "campaign_state_missing"


@pytest.mark.asyncio
async def test_changed_checkpoint_payload_uses_server_idempotency_validation(
    tmp_path: Path,
) -> None:
    qualifier = _load_qualifier()
    campaign = qualifier.Campaign("ODOO-MCP-LIVE-" + "A" * 32, "synthetic-tenant", "a" * 64)
    storage = Storage.open(tmp_path / "state.sqlite3")
    _seed_success(storage, campaign)
    client = StubClient(
        {
            "status": "failed",
            "outcome": "not_attempted",
            "error_code": "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH",
        }
    )

    with pytest.raises(qualifier.QualificationFailure) as raised:
        await qualifier._resume_or_execute(
            storage,
            campaign,
            client,
            "create_supplier_bill",
            {"company_id": 1, "value": "changed"},
            key="synthetic-key",
            stage="synthetic_stage",
        )

    assert raised.value.error_class == "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"
    assert len(client.calls) == 1
    assert client.calls[0][1]["dry_run"] is False


@pytest.mark.asyncio
async def test_changed_checkpoint_company_fails_before_mcp_call(tmp_path: Path) -> None:
    qualifier = _load_qualifier()
    campaign = qualifier.Campaign("ODOO-MCP-LIVE-" + "A" * 32, "synthetic-tenant", "a" * 64)
    storage = Storage.open(tmp_path / "state.sqlite3")
    _seed_success(storage, campaign)
    client = StubClient({"status": "succeeded"})

    with pytest.raises(qualifier.QualificationFailure) as raised:
        await qualifier._resume_or_execute(
            storage,
            campaign,
            client,
            "create_supplier_bill",
            {"company_id": 2, "value": "original"},
            key="synthetic-key",
            stage="synthetic_stage",
        )

    assert raised.value.error_class == "checkpoint_company_mismatch"
    assert client.calls == []


@pytest.mark.asyncio
async def test_stale_record_reference_fails_synthetic_identity_before_follow_on() -> None:
    qualifier = _load_qualifier()
    effect = SimpleNamespace(
        company_id=1,
        partner=SimpleNamespace(id=20),
        move_type="in_invoice",
        state="draft",
        invoice_date=date.today(),
        lines=(
            SimpleNamespace(
                description="unrelated-record",
                quantity=Decimal("1"),
                unit_price=Decimal("1"),
                subtotal=Decimal("1"),
            ),
        ),
        amount_untaxed=Decimal("1"),
        taxes=(),
        amount_residual=Decimal("1"),
        payment_state="not_paid",
    )

    class StubAdapter:
        async def get_invoice_effect(self, company_id: int, move_id: int) -> Any:
            return effect

    with pytest.raises(qualifier.QualificationFailure) as raised:
        await qualifier._verify_synthetic_invoice(
            StubAdapter(),
            1,
            101,
            partner_id=20,
            move_type="in_invoice",
            description="expected-marker",
            allowed_states=frozenset({"draft", "posted"}),
            stage="synthetic_identity",
        )

    assert raised.value.error_class == "synthetic_record_mismatch"
