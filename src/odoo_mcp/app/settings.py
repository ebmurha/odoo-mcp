"""Validated, secret-safe runtime settings."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError as PydanticSettingsError


class DeploymentProfile(StrEnum):
    """Supported deployment profiles."""

    LOCAL = "local"
    DEDICATED = "dedicated"
    SHARED = "shared"


class SettingsError(RuntimeError):
    """A configuration failure safe to return without configured values."""


def _parse_company_ids(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return tuple(int(item.strip()) for item in value.split(",") if item.strip())
        except ValueError as exc:
            raise ValueError("must be a comma-separated list of integers") from exc
    return value


class OdooConnectionSettings(BaseModel):
    """The single normalized Odoo connection type used by every profile."""

    model_config = ConfigDict(frozen=True)

    url: HttpUrl
    database: str = Field(min_length=1)
    username: str = Field(min_length=1)
    api_key: SecretStr
    allowed_company_ids: tuple[int, ...]
    default_company_id: int

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme not in {"http", "https"}:
            raise ValueError("must use HTTP or HTTPS")
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("must be a base HTTP(S) URL without credentials, query, or fragment")
        return value

    @field_validator("database", "username")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("api_key")
    @classmethod
    def reject_blank_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("allowed_company_ids", mode="before")
    @classmethod
    def parse_company_ids(cls, value: Any) -> Any:
        return _parse_company_ids(value)

    @field_validator("allowed_company_ids")
    @classmethod
    def validate_company_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or any(company_id <= 0 for company_id in value):
            raise ValueError("must contain positive integers")
        if len(set(value)) != len(value):
            raise ValueError("must not contain duplicates")
        return value

    @field_validator("default_company_id")
    @classmethod
    def validate_default_id(cls, value: int, info: ValidationInfo) -> int:
        if value <= 0:
            raise ValueError("must be a positive integer")
        allowed = info.data.get("allowed_company_ids")
        if allowed is not None and value not in allowed:
            raise ValueError("must appear in ODOO_MCP_ALLOWED_COMPANY_IDS")
        return value


class RuntimeSettings(BaseModel):
    """Profile-level settings consumed by the application factory."""

    model_config = ConfigDict(frozen=True)

    profile: DeploymentProfile
    connection: OdooConnectionSettings | None


class _EnvironmentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ODOO_MCP_",
        case_sensitive=False,
        extra="forbid",
    )

    odoo_url: HttpUrl
    odoo_database: str
    odoo_username: str
    odoo_api_key: SecretStr
    allowed_company_ids: str
    default_company_id: int


_SETTING_NAMES = {
    "odoo_url": "ODOO_MCP_ODOO_URL",
    "odoo_database": "ODOO_MCP_ODOO_DATABASE",
    "odoo_username": "ODOO_MCP_ODOO_USERNAME",
    "odoo_api_key": "ODOO_MCP_ODOO_API_KEY",
    "allowed_company_ids": "ODOO_MCP_ALLOWED_COMPANY_IDS",
    "default_company_id": "ODOO_MCP_DEFAULT_COMPANY_ID",
    "url": "ODOO_MCP_ODOO_URL",
    "database": "ODOO_MCP_ODOO_DATABASE",
    "username": "ODOO_MCP_ODOO_USERNAME",
    "api_key": "ODOO_MCP_ODOO_API_KEY",
    "odoo_mcp_odoo_version": "ODOO_MCP_ODOO_VERSION",
}


def _safe_validation_error(exc: ValidationError) -> SettingsError:
    names: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        location = str(error["loc"][-1]) if error["loc"] else "configuration"
        name = _SETTING_NAMES.get(location, location)
        if name not in names:
            names.append(name)
    return SettingsError("Missing or invalid setting(s): " + ", ".join(names))


def load_settings(
    profile: DeploymentProfile,
    *,
    env_file: Path | None = None,
) -> RuntimeSettings:
    """Load settings without exposing validation inputs in failures."""

    if profile is DeploymentProfile.SHARED:
        return RuntimeSettings(profile=profile, connection=None)
    if "ODOO_MCP_ODOO_VERSION" in os.environ:
        raise SettingsError("Unsupported setting: ODOO_MCP_ODOO_VERSION")
    selected_env_file = env_file if profile is DeploymentProfile.LOCAL else None
    if profile is DeploymentProfile.LOCAL and selected_env_file is None:
        selected_env_file = Path(".env.local")
    try:
        raw = _EnvironmentSettings(  # type: ignore[call-arg]
            _env_file=selected_env_file,
            _env_file_encoding="utf-8",
        )
        connection = OdooConnectionSettings(
            url=raw.odoo_url,
            database=raw.odoo_database,
            username=raw.odoo_username,
            api_key=raw.odoo_api_key,
            allowed_company_ids=cast(tuple[int, ...], _parse_company_ids(raw.allowed_company_ids)),
            default_company_id=raw.default_company_id,
        )
    except ValidationError as exc:
        raise _safe_validation_error(exc) from None
    except PydanticSettingsError as exc:
        raise SettingsError("Missing or invalid Odoo MCP setting") from exc
    return RuntimeSettings(profile=profile, connection=connection)


class PermissionConfig(BaseModel):
    """Validated MCP permission-to-tool mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    permissions: dict[str, tuple[str, ...]]


def load_permission_config(path: Path) -> PermissionConfig:
    """Load the safe public YAML permission map."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return PermissionConfig.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise SettingsError(f"Permission configuration is missing or invalid: {path.name}") from exc
