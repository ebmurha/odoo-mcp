from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest
from mcp import Client

from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.connections import ConnectionBinding
from odoo_mcp.app.settings import (
    DeploymentProfile,
    OdooConnectionSettings,
    load_permission_config,
)
from odoo_mcp.mcp.registry import PERMISSION_GROUPS, TOOL_REGISTRY
from odoo_mcp.mcp.server import create_mcp_server

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Resolver:
    binding: ConnectionBinding

    async def resolve(self) -> ConnectionBinding:
        return self.binding


class FakeAdapter:
    def __init__(self) -> None:
        self.closed = False

    async def get_capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            edition="enterprise",
            version=19,
            transport="json2",
            modules={"base": True, "account": True},
        )

    async def get_companies(self) -> list[Company]:
        return [Company(id=1, name="Synthetic Company")]

    async def close(self) -> None:
        self.closed = True


def _binding(
    profile: DeploymentProfile,
    connection: OdooConnectionSettings,
    permissions: frozenset[str] = frozenset({"core_read"}),
) -> ConnectionBinding:
    return ConnectionBinding(
        profile=profile,
        tenant_id=f"tenant-{profile.value}",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=permissions,
        connection=connection,
    )


async def test_all_profiles_expose_identical_registry_and_discovery(
    connection: OdooConnectionSettings,
) -> None:
    contracts: list[list[dict[str, object]]] = []
    outputs: list[dict[str, object]] = []
    adapters: list[FakeAdapter] = []

    for profile in DeploymentProfile:

        async def factory(_connection: object) -> OdooAdapter:
            adapter = FakeAdapter()
            adapters.append(adapter)
            return adapter

        server = create_mcp_server(Resolver(_binding(profile, connection)), adapter_factory=factory)
        async with Client(server) as client:
            listed = await client.list_tools()
            contracts.append([tool.model_dump(by_alias=True) for tool in listed.tools])
            result = await client.call_tool("get_erp_capabilities", {})
            assert result.is_error is False
            assert result.structured_content is not None
            outputs.append(result.structured_content)

    assert contracts[0] == contracts[1] == contracts[2]
    assert [tool["name"] for tool in contracts[0]] == [
        "get_erp_capabilities",
        "get_currency_rate_history",
        "get_trial_balance",
        "get_profit_and_loss",
        "get_balance_sheet",
        "get_aged_receivables",
        "get_aged_payables",
        "get_cashbook",
        "flag_unmatched_statement_lines",
        "reconcile_bank_statement_lines",
        "list_open_invoices",
        "list_open_bills",
        "create_customer_invoice",
        "create_supplier_bill",
        "create_credit_note",
        "validate_invoice",
        "register_payment",
        "list_journal_entries",
        "create_journal_entry",
        "post_journal_entry",
        "list_payroll_periods",
        "get_payroll_batch",
        "list_payslips",
        "get_payslip",
        "get_employee_payroll_context",
        "list_salary_rules",
        "get_attendance_summary",
        "compare_payroll_periods",
        "analyze_employee_payroll_change",
        "detect_payroll_anomalies",
        "explain_payslip",
        "prepare_payroll_approval_pack",
        "set_draft_payroll_input",
        "remove_draft_payroll_input",
        "recalculate_draft_payslip",
    ]
    for output in outputs:
        output.pop("request_id")
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0]["available_tools"] == ["get_erp_capabilities"]
    assert outputs[0]["authorized_companies"] == [
        {"id": 1, "name": "Synthetic Company", "is_default": True}
    ]
    assert all(adapter.closed for adapter in adapters)


async def test_registry_metadata_and_schemas_match_the_contract(
    connection: OdooConnectionSettings,
) -> None:
    server = create_mcp_server(Resolver(_binding(DeploymentProfile.LOCAL, connection)))
    tools = await server.list_tools()

    assert len(TOOL_REGISTRY) == 35
    assert len(tools) == 35
    assert [tool.name for tool in tools] == [definition.name for definition in TOOL_REGISTRY]
    for tool, definition in zip(tools, TOOL_REGISTRY, strict=True):
        assert tool.input_schema["type"] == "object"
        assert tool.output_schema is not None
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is (definition.risk_level == "read")
        assert tool.annotations.destructive_hint is definition.annotations.destructive_hint
        assert tool.annotations.idempotent_hint is True
        assert tool.annotations.open_world_hint is True
        assert tool.meta == definition.protocol_meta()
    assert tools[0].input_schema.get("properties") == {}
    assert set(tools[1].input_schema["required"]) == {
        "company_id",
        "currency_id",
        "period_start",
        "period_end",
    }
    assert set(tools[2].input_schema["required"]) == {
        "period_start",
        "period_end",
        "company_id",
    }
    assert set(tools[3].input_schema["required"]) == {
        "period_start",
        "period_end",
        "company_id",
    }
    assert set(tools[4].input_schema["required"]) == {"as_of_date", "company_id"}
    assert set(tools[5].input_schema["required"]) == {"as_of_date", "company_id"}
    assert set(tools[7].input_schema["required"]) == {
        "period_start",
        "period_end",
        "company_id",
    }
    assert set(tools[9].input_schema["required"]) == {
        "period",
        "company_id",
        "bank_journal_id",
        "statement_line_ids",
    }
    assert "vendor_reference" not in tools[12].input_schema["properties"]
    assert "vendor_reference" in tools[13].input_schema["properties"]
    assert tools[15].annotations.destructive_hint is True
    assert tools[16].annotations.destructive_hint is True
    assert set(tools[17].input_schema["required"]) == {
        "period_start",
        "period_end",
        "company_id",
    }
    assert tools[18].annotations.destructive_hint is False
    assert tools[19].annotations.destructive_hint is True
    assert set(tools[20].input_schema["required"]) == {
        "window_start",
        "window_end",
        "company_id",
    }
    assert set(tools[21].input_schema["required"]) == {"batch_id", "company_id"}
    assert set(tools[22].input_schema["required"]) == {"company_id"}
    assert set(tools[23].input_schema["required"]) == {"payslip_id", "company_id"}
    assert set(tools[24].input_schema["required"]) == {
        "employee_id",
        "period_start",
        "period_end",
        "company_id",
    }
    assert set(tools[25].input_schema["required"]) == {"company_id"}
    assert set(tools[26].input_schema["required"]) == {
        "employee_ids",
        "period_start",
        "period_end",
        "company_id",
    }
    assert set(tools[27].input_schema["required"]) == {
        "baseline_period",
        "target_period",
        "company_id",
    }
    assert set(tools[28].input_schema["required"]) == {
        "employee_id",
        "baseline_period",
        "target_period",
        "company_id",
    }
    assert set(tools[29].input_schema["required"]) == {
        "baseline_period",
        "target_period",
        "company_id",
    }
    assert set(tools[30].input_schema["required"]) == {"payslip_id", "company_id"}
    assert all(tool.annotations.read_only_hint is True for tool in tools[20:32])
    assert all("dry_run" not in tool.input_schema["properties"] for tool in tools[20:32])
    assert set(tools[32].input_schema["required"]) == {"company_id", "payslip_id"}
    assert set(tools[33].input_schema["required"]) == {
        "company_id",
        "payslip_id",
        "input_id",
    }
    assert set(tools[34].input_schema["required"]) == {"company_id", "payslip_id"}
    assert tools[32].annotations.destructive_hint is False
    assert tools[33].annotations.destructive_hint is True
    assert tools[34].annotations.destructive_hint is True
    assert all(tool.annotations.read_only_hint is False for tool in tools[32:])
    assert all("dry_run" in tool.input_schema["properties"] for tool in tools[32:])


async def test_permission_denial_happens_before_adapter_creation(
    connection: OdooConnectionSettings,
) -> None:
    called = False

    async def factory(_connection: object) -> OdooAdapter:
        nonlocal called
        called = True
        return FakeAdapter()

    server = create_mcp_server(
        Resolver(_binding(DeploymentProfile.SHARED, connection, frozenset())),
        adapter_factory=factory,
    )
    async with Client(server) as client:
        result = await client.call_tool("get_erp_capabilities", {})

    assert called is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "failed"
    assert result.structured_content["error_code"] == "ODOO_AUTH_FAILED"


async def test_adapter_close_failure_is_structured_and_secret_safe(
    connection: OdooConnectionSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "raw-close-synthetic-secret"

    class CloseFailureAdapter(FakeAdapter):
        async def close(self) -> None:
            raise RuntimeError(marker)

    async def factory(_connection: object) -> OdooAdapter:
        return CloseFailureAdapter()

    server = create_mcp_server(
        Resolver(_binding(DeploymentProfile.LOCAL, connection)),
        adapter_factory=factory,
    )
    with caplog.at_level(logging.DEBUG):
        async with Client(server) as client:
            result = await client.call_tool("get_erp_capabilities", {})

    assert result.structured_content is not None
    assert result.structured_content["status"] == "failed"
    assert result.structured_content["error_code"] == "UNKNOWN_ERROR"
    assert marker not in str(result.structured_content)
    assert marker not in caplog.text
    assert "Traceback" not in caplog.text


def test_public_permission_example_matches_registry() -> None:
    config = load_permission_config(ROOT / "config" / "config.example.yaml")
    mapped = {permission: set(tool_names) for permission, tool_names in config.permissions.items()}

    assert mapped == {
        "core_read": {"get_erp_capabilities"},
        "accounting_read": {
            "get_currency_rate_history",
            "get_trial_balance",
            "get_profit_and_loss",
            "get_balance_sheet",
            "get_aged_receivables",
            "get_aged_payables",
            "get_cashbook",
            "flag_unmatched_statement_lines",
            "list_open_invoices",
            "list_open_bills",
            "list_journal_entries",
        },
        "accounting_propose": {
            "reconcile_bank_statement_lines",
            "create_customer_invoice",
            "create_supplier_bill",
            "create_credit_note",
            "validate_invoice",
            "register_payment",
            "create_journal_entry",
            "post_journal_entry",
        },
        "payroll_read": {
            "list_payroll_periods",
            "get_payroll_batch",
            "list_payslips",
            "get_payslip",
            "get_employee_payroll_context",
            "list_salary_rules",
            "get_attendance_summary",
            "compare_payroll_periods",
            "analyze_employee_payroll_change",
            "detect_payroll_anomalies",
            "explain_payslip",
            "prepare_payroll_approval_pack",
        },
        "payroll_draft_write": {
            "set_draft_payroll_input",
            "remove_draft_payroll_input",
            "recalculate_draft_payslip",
        },
    }
    assert PERMISSION_GROUPS == frozenset(
        {
            "core_read",
            "accounting_read",
            "accounting_propose",
            "payroll_read",
            "payroll_draft_write",
        }
    )
