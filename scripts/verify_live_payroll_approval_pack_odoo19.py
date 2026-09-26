"""Qualify the Payroll approval pack through the packaged Local stdio server."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, NoReturn, TextIO

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from odoo_mcp.app.settings import DeploymentProfile, SettingsError, load_settings

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "config.example.yaml"
TOOL_NAME = "prepare_payroll_approval_pack"
_stage = "startup"


class _QualificationFailed(RuntimeError):
    pass


class _PrerequisiteUnavailable(RuntimeError):
    pass


def _fail() -> NoReturn:
    raise _QualificationFailed


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail()
    return value


def _items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = payload.get("items")
    if not isinstance(value, list):
        _fail()
    return [_mapping(item) for item in value]


def _date_text(value: object) -> str:
    if not isinstance(value, str):
        _fail()
    date.fromisoformat(value)
    return value


def _non_negative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail()
    return value


async def _call_ok(
    session: ClientSession,
    name: str,
    arguments: dict[str, Any],
    *,
    company_id: int,
) -> Mapping[str, Any]:
    global _stage
    _stage = name
    result = await session.call_tool(name, arguments, read_timeout_seconds=120)
    if not isinstance(result, types.CallToolResult) or result.is_error:
        _fail()
    payload = _mapping(result.structured_content)
    if payload.get("status") != "ok" or payload.get("company_id") != company_id:
        _fail()
    return payload


def _select_periods(
    periods: list[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    eligible = [
        value
        for value in periods
        if _non_negative_integer(value.get("non_cancelled_payslip_count")) > 0
    ]
    ordered = sorted(
        eligible,
        key=lambda value: (
            _date_text(value.get("period_start")),
            _date_text(value.get("period_end")),
        ),
        reverse=True,
    )
    for target in ordered:
        target_start = _date_text(target.get("period_start"))
        target_end = _date_text(target.get("period_end"))
        for baseline in ordered:
            baseline_start = _date_text(baseline.get("period_start"))
            baseline_end = _date_text(baseline.get("period_end"))
            if baseline_end < target_start:
                return (
                    {"period_start": baseline_start, "period_end": baseline_end},
                    {"period_start": target_start, "period_end": target_end},
                )
    raise _PrerequisiteUnavailable


async def _qualify(errlog: TextIO) -> None:
    global _stage
    settings = load_settings(DeploymentProfile.LOCAL)
    if settings.connection is None:
        raise SettingsError("The local Odoo connection is unavailable")
    company_id = settings.connection.default_company_id

    executable_name = "odoo-mcp.exe" if os.name == "nt" else "odoo-mcp"
    executable = Path(sys.executable).with_name(executable_name)
    if not executable.is_file() or not CONFIG.is_file():
        _fail()

    today = date.today()
    try:
        window_start = today.replace(year=today.year - 1)
    except ValueError:
        window_start = today.replace(year=today.year - 1, day=28)

    with tempfile.TemporaryDirectory(prefix="odoo-mcp-payroll-pack-qualification-") as temp_name:
        parameters = StdioServerParameters(
            command=os.fspath(executable),
            args=[
                "--profile",
                "local",
                "--transport",
                "stdio",
                "--config",
                os.fspath(CONFIG),
                "--storage",
                os.fspath(Path(temp_name) / "qualification.sqlite3"),
            ],
            cwd=ROOT,
        )
        async with stdio_client(parameters, errlog=errlog) as streams:
            async with ClientSession(*streams, read_timeout_seconds=120) as session:
                _stage = "initialize"
                await session.initialize()
                _stage = "tool_discovery"
                listed = await session.list_tools()
                if TOOL_NAME not in {tool.name for tool in listed.tools}:
                    _fail()

                periods_payload = await _call_ok(
                    session,
                    "list_payroll_periods",
                    {
                        "company_id": company_id,
                        "window_start": window_start.isoformat(),
                        "window_end": today.isoformat(),
                        "limit": 200,
                    },
                    company_id=company_id,
                )
                baseline_period, target_period = _select_periods(_items(periods_payload))
                pack = await _call_ok(
                    session,
                    TOOL_NAME,
                    {
                        "company_id": company_id,
                        "baseline_period": baseline_period,
                        "target_period": target_period,
                    },
                    company_id=company_id,
                )
                if _mapping(pack.get("company")).get("id") != company_id:
                    _fail()
                _non_negative_integer(pack.get("headcount"))
                if _mapping(pack.get("variance")).get("status") != "available":
                    _fail()
                for field in (
                    "rule_totals",
                    "category_totals",
                    "recognized_rule_totals",
                    "anomalies",
                    "exceptions",
                    "source_refs",
                    "limitations",
                    "unresolved_issues",
                    "recommended_review_actions",
                    "sign_off_checklist",
                ):
                    if not isinstance(pack.get(field), list):
                        _fail()
                markdown = pack.get("rendered_markdown")
                if not isinstance(markdown, str) or "## Sign-Off Checklist" not in markdown:
                    _fail()


def main() -> None:
    try:
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            asyncio.run(_qualify(errlog))
    except _PrerequisiteUnavailable:
        print(
            "Live Odoo 19 Payroll approval-pack prerequisite unavailable.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except Exception:
        print(
            f"Live Odoo 19 Payroll approval-pack qualification failed safely during {_stage}.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print("Live Odoo 19 Payroll approval-pack qualification passed.")


if __name__ == "__main__":
    main()
