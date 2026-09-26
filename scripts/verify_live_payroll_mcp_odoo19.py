"""Run fixed-output live Payroll tool qualification through Local stdio."""

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
_stage = "startup"
PAYROLL_EVIDENCE_TOOLS = frozenset(
    {
        "list_payroll_periods",
        "get_payroll_batch",
        "list_payslips",
        "get_payslip",
        "get_employee_payroll_context",
        "list_salary_rules",
        "get_attendance_summary",
    }
)
PAYROLL_ANALYSIS_TOOLS = frozenset(
    {
        "compare_payroll_periods",
        "analyze_employee_payroll_change",
        "detect_payroll_anomalies",
        "explain_payslip",
    }
)


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


def _positive_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail()
    return value


def _date_text(value: object) -> str:
    if not isinstance(value, str):
        _fail()
    date.fromisoformat(value)
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
    window_end = today

    with tempfile.TemporaryDirectory(prefix="odoo-mcp-payroll-qualification-") as temp_name:
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
                registered = {tool.name for tool in listed.tools}
                if not PAYROLL_EVIDENCE_TOOLS | PAYROLL_ANALYSIS_TOOLS <= registered:
                    _fail()

                periods_payload = await _call_ok(
                    session,
                    "list_payroll_periods",
                    {
                        "company_id": company_id,
                        "window_start": window_start.isoformat(),
                        "window_end": window_end.isoformat(),
                        "limit": 200,
                    },
                    company_id=company_id,
                )
                periods = _items(periods_payload)
                if len(periods) < 2:
                    raise _PrerequisiteUnavailable

                period_slips: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
                ordered_periods = sorted(
                    periods,
                    key=lambda item: (
                        _date_text(item.get("period_start")),
                        _date_text(item.get("period_end")),
                    ),
                    reverse=True,
                )
                selected: (
                    tuple[
                        Mapping[str, Any],
                        Mapping[str, Any],
                        Mapping[str, Any],
                        int,
                    ]
                    | None
                ) = None
                for target in ordered_periods:
                    target_start = _date_text(target.get("period_start"))
                    target_end = _date_text(target.get("period_end"))
                    target_key = (target_start, target_end)
                    target_payload = await _call_ok(
                        session,
                        "list_payslips",
                        {
                            "company_id": company_id,
                            "period_start": target_start,
                            "period_end": target_end,
                            "limit": 200,
                        },
                        company_id=company_id,
                    )
                    target_slips = _items(target_payload)
                    period_slips[target_key] = target_slips
                    target_by_employee = {
                        _positive_id(_mapping(slip.get("employee")).get("id")): slip
                        for slip in target_slips
                    }
                    if not target_by_employee:
                        continue
                    for baseline in ordered_periods:
                        baseline_start = _date_text(baseline.get("period_start"))
                        baseline_end = _date_text(baseline.get("period_end"))
                        if baseline_end >= target_start:
                            continue
                        baseline_key = (baseline_start, baseline_end)
                        if baseline_key not in period_slips:
                            baseline_payload = await _call_ok(
                                session,
                                "list_payslips",
                                {
                                    "company_id": company_id,
                                    "period_start": baseline_start,
                                    "period_end": baseline_end,
                                    "limit": 200,
                                },
                                company_id=company_id,
                            )
                            period_slips[baseline_key] = _items(baseline_payload)
                        baseline_employees = {
                            _positive_id(_mapping(slip.get("employee")).get("id"))
                            for slip in period_slips[baseline_key]
                        }
                        common = sorted(baseline_employees & target_by_employee.keys())
                        if common:
                            employee_id = common[0]
                            selected = (
                                baseline,
                                target,
                                target_by_employee[employee_id],
                                employee_id,
                            )
                            break
                    if selected is not None:
                        break
                if selected is None:
                    raise _PrerequisiteUnavailable

                baseline, target, payslip, employee_id = selected
                baseline_period = {
                    "period_start": _date_text(baseline.get("period_start")),
                    "period_end": _date_text(baseline.get("period_end")),
                }
                target_period = {
                    "period_start": _date_text(target.get("period_start")),
                    "period_end": _date_text(target.get("period_end")),
                }
                payslip_id = _positive_id(payslip.get("payslip_id"))

                batch_ids: list[int] = []
                for period in (target, baseline):
                    raw_batch_ids = period.get("batch_ids")
                    if isinstance(raw_batch_ids, list):
                        batch_ids.extend(_positive_id(value) for value in raw_batch_ids)
                if not batch_ids:
                    raise _PrerequisiteUnavailable
                batch_id = batch_ids[0]

                batch_payload = await _call_ok(
                    session,
                    "get_payroll_batch",
                    {"company_id": company_id, "batch_id": batch_id, "limit": 200},
                    company_id=company_id,
                )
                if _mapping(batch_payload.get("batch")).get("batch_id") != batch_id:
                    _fail()

                payslip_payload = await _call_ok(
                    session,
                    "get_payslip",
                    {"company_id": company_id, "payslip_id": payslip_id},
                    company_id=company_id,
                )
                if _mapping(payslip_payload.get("payslip")).get("payslip_id") != payslip_id:
                    _fail()
                salary_lines = payslip_payload.get("salary_lines")
                if not isinstance(salary_lines, list) or not salary_lines:
                    raise _PrerequisiteUnavailable

                context_payload = await _call_ok(
                    session,
                    "get_employee_payroll_context",
                    {
                        "company_id": company_id,
                        "employee_id": employee_id,
                        **target_period,
                    },
                    company_id=company_id,
                )
                if _mapping(context_payload.get("employee")).get("employee_id") != employee_id:
                    _fail()
                if not context_payload.get("contract_segments"):
                    raise _PrerequisiteUnavailable

                rules_payload = await _call_ok(
                    session,
                    "list_salary_rules",
                    {"company_id": company_id, **target_period, "limit": 200},
                    company_id=company_id,
                )
                if not _items(rules_payload):
                    raise _PrerequisiteUnavailable

                attendance_payload = await _call_ok(
                    session,
                    "get_attendance_summary",
                    {
                        "company_id": company_id,
                        "employee_ids": [employee_id],
                        **target_period,
                        "limit": 200,
                    },
                    company_id=company_id,
                )
                if not _items(attendance_payload):
                    raise _PrerequisiteUnavailable

                comparison_arguments = {
                    "company_id": company_id,
                    "baseline_period": baseline_period,
                    "target_period": target_period,
                }
                comparison_payload = await _call_ok(
                    session,
                    "compare_payroll_periods",
                    comparison_arguments,
                    company_id=company_id,
                )
                headcount = _mapping(comparison_payload.get("headcount"))
                if not _positive_id(headcount.get("baseline")) or not _positive_id(
                    headcount.get("target")
                ):
                    _fail()

                employee_payload = await _call_ok(
                    session,
                    "analyze_employee_payroll_change",
                    {**comparison_arguments, "employee_id": employee_id},
                    company_id=company_id,
                )
                if _mapping(employee_payload.get("employee")).get("id") != employee_id:
                    _fail()
                if not employee_payload.get("baseline_payslips") or not employee_payload.get(
                    "target_payslips"
                ):
                    _fail()

                anomaly_payload = await _call_ok(
                    session,
                    "detect_payroll_anomalies",
                    comparison_arguments,
                    company_id=company_id,
                )
                evaluated = anomaly_payload.get("evaluated_employee_count")
                if isinstance(evaluated, bool) or not isinstance(evaluated, int) or evaluated <= 0:
                    _fail()

                explanation_payload = await _call_ok(
                    session,
                    "explain_payslip",
                    {"company_id": company_id, "payslip_id": payslip_id},
                    company_id=company_id,
                )
                explained = _mapping(explanation_payload.get("payslip"))
                if explained.get("payslip_id") != payslip_id:
                    _fail()
                if not explanation_payload.get("line_arithmetic"):
                    _fail()


def main() -> None:
    try:
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            asyncio.run(_qualify(errlog))
    except _PrerequisiteUnavailable:
        print(
            "Live Odoo 19 Payroll MCP qualification prerequisite unavailable.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except Exception:
        print(
            f"Live Odoo 19 Payroll MCP qualification failed safely during {_stage}.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print("Live Odoo 19 Payroll MCP qualification passed.")


if __name__ == "__main__":
    main()
