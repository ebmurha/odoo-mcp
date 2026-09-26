from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from odoo_mcp.adapters.base import CapabilitySnapshot, Company
from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings, RuntimeSettings

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_live_payroll_odoo19.py"
_SPEC = importlib.util.spec_from_file_location("verify_live_payroll_odoo19", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
live = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(live)


class PayrollQualificationAdapter:
    def __init__(self, *, available: bool) -> None:
        self.available = available
        self.verified_company_id: int | None = None
        self.closed = False

    async def get_companies(self) -> list[Company]:
        return [Company(id=1, name="Synthetic Company 1")]

    async def get_capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            edition="enterprise",
            version=19,
            transport="json2",
            modules={"hr_payroll": self.available},
        )

    async def verify_payroll_schema(self, company_id: int) -> None:
        self.verified_company_id = company_id

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("available", [False, True])
async def test_live_payroll_qualifier_is_capability_gated_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
    connection: OdooConnectionSettings,
    available: bool,
) -> None:
    adapter = PayrollQualificationAdapter(available=available)

    async def connect(
        cls: type[live.OdooClient], selected: OdooConnectionSettings
    ) -> PayrollQualificationAdapter:
        assert selected is connection
        return adapter

    monkeypatch.setattr(
        live,
        "load_settings",
        lambda profile: RuntimeSettings(profile=DeploymentProfile.LOCAL, connection=connection),
    )
    monkeypatch.setattr(live.OdooClient, "connect", classmethod(connect))

    assert await live._qualify() is available
    assert adapter.verified_company_id == (1 if available else None)
    assert adapter.closed is True


def test_live_payroll_qualifier_has_fixed_unavailable_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def unavailable() -> bool:
        return False

    monkeypatch.setattr(live, "_qualify", unavailable)

    live.main()

    captured = capsys.readouterr()
    assert captured.out == (
        "Live Odoo 19 Payroll schema qualification unavailable: capability not present.\n"
    )
    assert captured.err == ""
