from __future__ import annotations

from pathlib import Path

import pytest

from odoo_mcp.app.settings import DeploymentProfile, SettingsError, load_settings

REQUIRED = {
    "ODOO_MCP_ODOO_URL": "https://odoo.invalid",
    "ODOO_MCP_ODOO_DATABASE": "synthetic-db",
    "ODOO_MCP_ODOO_USERNAME": "synthetic-user",
    "ODOO_MCP_ODOO_API_KEY": "synthetic-secret",
    "ODOO_MCP_ALLOWED_COMPANY_IDS": "1,2",
    "ODOO_MCP_DEFAULT_COMPANY_ID": "1",
}


def test_local_settings_load_dotenv_with_process_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in REQUIRED.items()),
        encoding="utf-8",
    )
    monkeypatch.setenv("ODOO_MCP_ODOO_DATABASE", "process-db")

    settings = load_settings(DeploymentProfile.LOCAL, env_file=env_file)

    assert settings.connection.database == "process-db"
    assert settings.connection.allowed_company_ids == (1, 2)
    assert settings.connection.api_key.get_secret_value() == "synthetic-secret"


@pytest.mark.parametrize(
    ("updates", "setting_name"),
    [
        ({"ODOO_MCP_ODOO_URL": "ftp://odoo.invalid"}, "ODOO_MCP_ODOO_URL"),
        ({"ODOO_MCP_ALLOWED_COMPANY_IDS": "0"}, "ODOO_MCP_ALLOWED_COMPANY_IDS"),
        ({"ODOO_MCP_DEFAULT_COMPANY_ID": "3"}, "ODOO_MCP_DEFAULT_COMPANY_ID"),
    ],
)
def test_invalid_settings_are_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, str],
    setting_name: str,
) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    for key, value in updates.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(SettingsError) as caught:
        load_settings(DeploymentProfile.DEDICATED)

    message = str(caught.value)
    assert setting_name in message
    assert "synthetic-secret" not in message
    assert "ftp://odoo.invalid" not in message


def test_shared_profile_does_not_load_odoo_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)

    settings = load_settings(DeploymentProfile.SHARED)

    assert settings.connection is None


def test_dedicated_profile_never_loads_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in REQUIRED.items()),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(SettingsError):
        load_settings(DeploymentProfile.DEDICATED)


def test_configured_odoo_version_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ODOO_MCP_ODOO_VERSION", "19-secret-value")

    with pytest.raises(SettingsError) as caught:
        load_settings(DeploymentProfile.DEDICATED)

    assert "ODOO_MCP_ODOO_VERSION" in str(caught.value)
    assert "19-secret-value" not in str(caught.value)
