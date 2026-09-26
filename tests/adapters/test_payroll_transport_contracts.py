from __future__ import annotations

import json
from typing import Any

import httpx

from odoo_mcp.adapters.odoo.transports.json2 import Json2Transport
from odoo_mcp.adapters.odoo.transports.json_rpc import JsonRpcTransport
from odoo_mcp.app.settings import OdooConnectionSettings


async def test_odoo19_payroll_requests_are_complete_and_ordered(
    connection: OdooConnectionSettings,
) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("/search_count"):
            return httpx.Response(200, json=1)
        if request.url.path.endswith("/search_read"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/create"):
            return httpx.Response(200, json=902)
        return httpx.Response(200, json=True)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=str(connection.url))
    transport = Json2Transport(connection, client)
    domain = [["company_id", "=", 1], ["id", "in", [101]]]
    fields = ["id", "name", "version_id", "write_date"]
    values = {
        "name": "Synthetic Adjustment",
        "payslip_id": 101,
        "input_type_id": 401,
        "amount": "40.125",
        "version_id": 301,
    }
    try:
        await transport.search_count("hr.payslip", domain, company_ids=(1,))
        await transport.search_read(
            "hr.payslip",
            domain,
            fields,
            limit=50,
            offset=0,
            order="date_from desc, date_to desc, id desc",
            company_ids=(1,),
        )
        await transport.execute_method(
            "hr.payslip.input",
            "create",
            named={"vals_list": values},
            company_ids=(1,),
        )
        await transport.execute_method(
            "hr.payslip.input",
            "write",
            ids=(902,),
            named={"vals": {"amount": "41.125"}},
            company_ids=(1,),
        )
        await transport.execute_method("hr.payslip.input", "unlink", ids=(902,), company_ids=(1,))
        await transport.execute_method("hr.payslip", "compute_sheet", ids=(101,), company_ids=(1,))
    finally:
        await transport.close()

    context = {"allowed_company_ids": [1]}
    assert requests == [
        (
            "/json/2/hr.payslip/search_count",
            {"domain": domain, "context": context},
        ),
        (
            "/json/2/hr.payslip/search_read",
            {
                "domain": domain,
                "fields": fields,
                "limit": 50,
                "offset": 0,
                "order": "date_from desc, date_to desc, id desc",
                "context": context,
            },
        ),
        (
            "/json/2/hr.payslip.input/create",
            {"vals_list": values, "context": context},
        ),
        (
            "/json/2/hr.payslip.input/write",
            {"vals": {"amount": "41.125"}, "ids": [902], "context": context},
        ),
        (
            "/json/2/hr.payslip.input/unlink",
            {"ids": [902], "context": context},
        ),
        (
            "/json/2/hr.payslip/compute_sheet",
            {"ids": [101], "context": context},
        ),
    ]


async def test_odoo18_payroll_requests_are_complete_and_ordered(
    connection: OdooConnectionSettings,
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        requests.append(body)
        if body["params"]["service"] == "common":
            return httpx.Response(200, json={"result": 7})
        method = body["params"]["args"][4]
        if method == "search_count":
            return httpx.Response(200, json={"result": 1})
        if method == "search_read":
            return httpx.Response(200, json={"result": []})
        if method == "create":
            return httpx.Response(200, json={"result": 902})
        return httpx.Response(200, json={"result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=str(connection.url))
    transport = JsonRpcTransport(connection, client)
    domain = [["company_id", "=", 1], ["id", "in", [101]]]
    fields = ["id", "name", "number", "contract_id", "write_date"]
    values = {
        "name": "Synthetic Adjustment",
        "payslip_id": 101,
        "input_type_id": 401,
        "amount": "40.125",
        "contract_id": 301,
    }
    try:
        await transport.authenticate()
        await transport.search_count("hr.payslip", domain, company_ids=(1,))
        await transport.search_read(
            "hr.payslip",
            domain,
            fields,
            limit=50,
            offset=0,
            order="date_from desc, date_to desc, id desc",
            company_ids=(1,),
        )
        await transport.execute_method(
            "hr.payslip.input",
            "create",
            named={"vals_list": values},
            company_ids=(1,),
        )
        await transport.execute_method(
            "hr.payslip.input",
            "write",
            ids=(902,),
            named={"vals": {"amount": "41.125"}},
            company_ids=(1,),
        )
        await transport.execute_method("hr.payslip.input", "unlink", ids=(902,), company_ids=(1,))
        await transport.execute_method("hr.payslip", "compute_sheet", ids=(101,), company_ids=(1,))
    finally:
        await transport.close()

    def rpc_call(
        model: str, method: str, args: list[Any], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "object",
                "method": "execute_kw",
                "args": [
                    "synthetic-db",
                    7,
                    "synthetic-secret",
                    model,
                    method,
                    args,
                    kwargs,
                ],
            },
            "id": 1,
        }

    context = {"allowed_company_ids": [1]}
    assert requests == [
        {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "common",
                "method": "authenticate",
                "args": [
                    "synthetic-db",
                    "synthetic-user",
                    "synthetic-secret",
                    {},
                ],
            },
            "id": 1,
        },
        rpc_call("hr.payslip", "search_count", [domain], {"context": context}),
        rpc_call(
            "hr.payslip",
            "search_read",
            [domain],
            {
                "fields": fields,
                "limit": 50,
                "offset": 0,
                "order": "date_from desc, date_to desc, id desc",
                "context": context,
            },
        ),
        rpc_call("hr.payslip.input", "create", [values], {"context": context}),
        rpc_call(
            "hr.payslip.input",
            "write",
            [[902]],
            {"vals": {"amount": "41.125"}, "context": context},
        ),
        rpc_call("hr.payslip.input", "unlink", [[902]], {"context": context}),
        rpc_call("hr.payslip", "compute_sheet", [[101]], {"context": context}),
    ]
