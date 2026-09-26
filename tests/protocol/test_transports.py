from __future__ import annotations

import sys
import time
from dataclasses import dataclass

import jwt
import pytest
from mcp import Client, StdioServerParameters
from starlette.testclient import TestClient

from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.connections import ConnectionBinding, DedicatedConnectionResolver
from odoo_mcp.app.remote_auth import DedicatedAuthSettings, protect_dedicated_app
from odoo_mcp.app.settings import OdooConnectionSettings
from odoo_mcp.mcp.server import create_mcp_server


@dataclass
class Resolver:
    binding: ConnectionBinding

    async def resolve(self) -> ConnectionBinding:
        return self.binding


class FakeAdapter:
    async def get_capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            edition="enterprise",
            version=18,
            transport="json_rpc",
            modules={"base": True},
        )

    async def get_companies(self) -> list[Company]:
        return [Company(id=1, name="Synthetic Company")]


async def _factory(_connection: object) -> OdooAdapter:
    return FakeAdapter()


def test_streamable_http_lists_the_shared_registry(
    connection: OdooConnectionSettings,
) -> None:
    resolver = DedicatedConnectionResolver(
        tenant_id="tenant-http",
        permissions=frozenset({"core_read"}),
        connection=connection,
    )
    server = create_mcp_server(resolver, adapter_factory=_factory)
    raw_app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host="127.0.0.1",
    )
    auth = DedicatedAuthSettings(
        issuer="https://identity.invalid",
        audience="odoo-mcp",
        signing_key="a" * 32,
    )
    app = protect_dedicated_app(raw_app, auth)
    headers = {"Accept": "application/json, text/event-stream"}
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": auth.issuer,
            "aud": auth.audience,
            "sub": "synthetic-subject",
            "client_id": "synthetic-client",
            "iat": now,
            "exp": now + 300,
        },
        auth.signing_key.get_secret_value(),
        algorithm="HS256",
    )
    authorized_headers = {**headers, "Authorization": f"Bearer {token}"}

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        health = client.get("/healthz")
        unauthorized = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}},
        )
        initialize = client.post(
            "/mcp",
            headers=authorized_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic-client", "version": "1"},
                },
            },
        )
        listed = client.post(
            "/mcp",
            headers=authorized_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        called = client.post(
            "/mcp",
            headers=authorized_headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_erp_capabilities", "arguments": {}},
            },
        )

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert unauthorized.status_code == 401
    assert initialize.status_code == 200
    assert initialize.json()["result"]["serverInfo"]["title"] == "Odoo MCP"
    assert initialize.json()["result"]["serverInfo"]["icons"] == [
        {
            "src": (
                "https://raw.githubusercontent.com/ebmurha/odoo-mcp/main/assets/odoo-mcp-logo.png"
            ),
            "mimeType": "image/png",
            "sizes": ["256x256"],
        }
    ]
    assert listed.status_code == 200
    assert [tool["name"] for tool in listed.json()["result"]["tools"]] == [
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
    assert called.status_code == 200
    assert called.json()["result"]["structuredContent"]["status"] == "ok"


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://wrong.invalid"},
        {"aud": "wrong-audience"},
        {"exp": 1},
        {"iat": 32_503_680_000},
        {"sub": ""},
        {"client_id": ""},
    ],
)
def test_dedicated_remote_rejects_invalid_identity_claims(
    connection: OdooConnectionSettings,
    claims: dict[str, object],
) -> None:
    resolver = DedicatedConnectionResolver(
        tenant_id="tenant-http",
        permissions=frozenset({"core_read"}),
        connection=connection,
    )
    server = create_mcp_server(resolver, adapter_factory=_factory)
    auth = DedicatedAuthSettings(
        issuer="https://identity.invalid",
        audience="odoo-mcp",
        signing_key="a" * 32,
    )
    app = protect_dedicated_app(
        server.streamable_http_app(stateless_http=True, json_response=True), auth
    )
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": auth.issuer,
        "aud": auth.audience,
        "sub": "subject-a",
        "client_id": "client-a",
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims)
    token = jwt.encode(
        payload,
        auth.signing_key.get_secret_value(),
        algorithm="HS256",
    )

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

    assert response.status_code == 401


def test_dedicated_remote_rejects_wrong_signature(connection: OdooConnectionSettings) -> None:
    resolver = DedicatedConnectionResolver(
        tenant_id="tenant-http",
        permissions=frozenset({"core_read"}),
        connection=connection,
    )
    server = create_mcp_server(resolver, adapter_factory=_factory)
    auth = DedicatedAuthSettings(
        issuer="https://identity.invalid",
        audience="odoo-mcp",
        signing_key="a" * 32,
    )
    app = protect_dedicated_app(
        server.streamable_http_app(stateless_http=True, json_response=True), auth
    )
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": auth.issuer,
            "aud": auth.audience,
            "sub": "subject-a",
            "client_id": "client-a",
            "iat": now,
            "exp": now + 300,
        },
        "b" * 32,
        algorithm="HS256",
    )

    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_stdio_profile_lists_the_shared_registry(tmp_path) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "odoo_mcp",
            "--profile",
            "local",
            "--transport",
            "stdio",
            "--storage",
            str(tmp_path / "stdio-state.sqlite3"),
        ],
        env={
            "ODOO_MCP_ODOO_URL": "https://odoo.invalid",
            "ODOO_MCP_ODOO_DATABASE": "synthetic-db",
            "ODOO_MCP_ODOO_USERNAME": "synthetic-user",
            "ODOO_MCP_ODOO_API_KEY": "synthetic-secret",
            "ODOO_MCP_ALLOWED_COMPANY_IDS": "1",
            "ODOO_MCP_DEFAULT_COMPANY_ID": "1",
        },
    )

    async with Client(parameters) as client:
        listed = await client.list_tools()

    assert [tool.name for tool in listed.tools] == [
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
