from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from odoo_mcp.adapters.accounting import RecordPage
from odoo_mcp.adapters.base import CapabilitySnapshot, Company
from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings, RuntimeSettings
from odoo_mcp.mcp.error_codes import OdooMcpError

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify_live_odoo19.py"
_SPEC = importlib.util.spec_from_file_location("verify_live_odoo19", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
live = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(live)


class EmptyJournalAdapter:
    def __init__(self) -> None:
        self.payment_method_lines_called = False
        self.closed = False

    async def get_companies(self) -> list[Company]:
        return [Company(id=1, name="Synthetic Company")]

    async def get_capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            edition="enterprise",
            version=19,
            transport="json2",
            modules={"account": True},
        )

    async def get_journals(self, *args: Any, **kwargs: Any) -> RecordPage[Any]:
        return RecordPage(items=[])

    async def get_payment_method_lines(self, *args: Any, **kwargs: Any) -> RecordPage[Any]:
        self.payment_method_lines_called = True
        return RecordPage(items=[])

    async def close(self) -> None:
        self.closed = True

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("get_"):
            raise AttributeError(name)

        async def empty_read(*args: Any, **kwargs: Any) -> RecordPage[Any]:
            return RecordPage(items=[])

        return empty_read


async def test_live_qualification_cannot_pass_without_payment_method_line_read(
    monkeypatch: pytest.MonkeyPatch,
    connection: OdooConnectionSettings,
) -> None:
    adapter = EmptyJournalAdapter()

    async def connect(
        cls: type[live.OdooClient],
        selected: OdooConnectionSettings,
    ) -> EmptyJournalAdapter:
        assert selected is connection
        return adapter

    monkeypatch.setattr(
        live,
        "load_settings",
        lambda profile: RuntimeSettings(profile=DeploymentProfile.LOCAL, connection=connection),
    )
    monkeypatch.setattr(live.OdooClient, "connect", classmethod(connect))

    with pytest.raises(OdooMcpError) as caught:
        await live._qualify()

    assert caught.value.code.value == "CAPABILITY_NOT_AVAILABLE"
    assert adapter.payment_method_lines_called is False
    assert adapter.closed is True
