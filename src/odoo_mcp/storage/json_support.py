"""Deterministic JSON and timestamp helpers for persisted state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

_SENSITIVE_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: datetime | None = None) -> str:
    selected = value or utc_now()
    if selected.tzinfo is None:
        raise ValueError("Persisted timestamps must be timezone-aware")
    return selected.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_mapping(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise ValueError("Persisted JSON object is invalid")
    return cast(dict[str, object], parsed)


def parse_string_tuple(value: str) -> tuple[str, ...]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError("Persisted JSON list is invalid")
    return tuple(parsed)


def redact_sensitive(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if any(part in str(key).casefold() for part in _SENSITIVE_PARTS)
                else redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    return value
