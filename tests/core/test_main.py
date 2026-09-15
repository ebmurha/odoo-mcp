from __future__ import annotations

from typing import Any

import pytest

from odoo_mcp.app import main as main_module
from odoo_mcp.app.settings import PermissionConfig, SettingsError


class FakeServer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, transport: str, **kwargs: Any) -> None:
        self.calls.append((transport, kwargs))


@pytest.mark.parametrize(
    ("profile", "expected_transport"),
    [("local", "stdio"), ("dedicated", "streamable-http"), ("shared", "streamable-http")],
)
def test_profile_selects_approved_transport(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    expected_transport: str,
) -> None:
    for key, value in {
        "ODOO_MCP_ODOO_URL": "https://odoo.invalid",
        "ODOO_MCP_ODOO_DATABASE": "synthetic-db",
        "ODOO_MCP_ODOO_USERNAME": "synthetic-user",
        "ODOO_MCP_ODOO_API_KEY": "synthetic-secret",
        "ODOO_MCP_ALLOWED_COMPANY_IDS": "1",
        "ODOO_MCP_DEFAULT_COMPANY_ID": "1",
    }.items():
        monkeypatch.setenv(key, value)
    fake = FakeServer()
    monkeypatch.setattr(main_module, "create_mcp_server", lambda _resolver: fake)

    main_module.main(["--profile", profile])

    assert fake.calls[0][0] == expected_transport
    if expected_transport == "streamable-http":
        assert fake.calls[0][1] == {
            "host": "127.0.0.1",
            "port": 8000,
            "stateless_http": True,
            "json_response": True,
        }


@pytest.mark.parametrize(
    "arguments",
    [
        ["--profile", "local", "--transport", "streamable-http"],
        ["--profile", "dedicated", "--transport", "stdio"],
        ["--profile", "shared", "--transport", "stdio"],
    ],
)
def test_profile_rejects_unapproved_transport(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        main_module.main(arguments)


def test_empty_permission_mapping_grants_no_permissions() -> None:
    config = PermissionConfig(permissions={"core_read": ()})

    assert main_module._permissions(config) == frozenset()


@pytest.mark.parametrize(
    "permissions",
    [
        {"core_read": ("unknown_tool",)},
        {"unknown_permission": ("get_erp_capabilities",)},
    ],
)
def test_permission_mapping_rejects_registry_drift(
    permissions: dict[str, tuple[str, ...]],
) -> None:
    config = PermissionConfig(permissions=permissions)

    with pytest.raises(SettingsError, match="does not match the tool registry"):
        main_module._permissions(config)
