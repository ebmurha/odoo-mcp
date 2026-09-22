from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from odoo_mcp.app import main as main_module
from odoo_mcp.app.settings import PermissionConfig, SettingsError


class FakeServer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, transport: str, **kwargs: Any) -> None:
        self.calls.append((transport, kwargs))

    def streamable_http_app(self, **kwargs: Any) -> object:
        self.calls.append(("streamable-http", kwargs))
        return object()


@pytest.mark.parametrize(
    ("profile", "expected_transport"),
    [("local", "stdio"), ("dedicated", "streamable-http")],
)
def test_profile_selects_approved_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
        "ODOO_MCP_AUTH_ISSUER": "https://identity.invalid",
        "ODOO_MCP_AUTH_AUDIENCE": "odoo-mcp",
        "ODOO_MCP_AUTH_SIGNING_KEY": "a" * 32,
    }.items():
        monkeypatch.setenv(key, value)
    fake = FakeServer()
    monkeypatch.setattr(main_module, "create_mcp_server", lambda _resolver, **_kwargs: fake)
    monkeypatch.setattr(main_module.uvicorn, "run", lambda *_args, **_kwargs: None)

    main_module.main(["--profile", profile, "--storage", str(tmp_path / f"{profile}.sqlite3")])

    assert fake.calls[0][0] == expected_transport
    if expected_transport == "streamable-http":
        assert fake.calls[0][1] == {
            "host": "127.0.0.1",
            "stateless_http": True,
            "json_response": True,
        }


def test_shared_profile_uses_complete_shared_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Lease:
        released = False

        def release(self) -> None:
            self.released = True

    lease = Lease()
    shared_app = object()
    seen: list[object] = []
    monkeypatch.setattr(main_module, "load_shared_settings", lambda: object())
    monkeypatch.setattr(
        main_module,
        "open_shared_app",
        lambda *_args, **_kwargs: (shared_app, lease),
    )
    monkeypatch.setattr(main_module, "protect_shared_app", lambda app: app)
    monkeypatch.setattr(main_module.uvicorn, "run", lambda app, **_kwargs: seen.append(app))

    main_module.main(["--profile", "shared"])

    assert seen == [shared_app]
    assert lease.released is True


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


def test_dedicated_profile_rejects_missing_authentication_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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
    for key in (
        "ODOO_MCP_AUTH_ISSUER",
        "ODOO_MCP_AUTH_AUDIENCE",
        "ODOO_MCP_AUTH_SIGNING_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    fake = FakeServer()
    monkeypatch.setattr(main_module, "create_mcp_server", lambda _resolver, **_kwargs: fake)

    with pytest.raises(SystemExit):
        main_module.main(["--profile", "dedicated", "--storage", str(tmp_path / "state.sqlite3")])


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
