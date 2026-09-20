from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from odoo_mcp.adapters.accounting import PageRequest, ReadFilters
from odoo_mcp.adapters.odoo.capabilities import CAPABILITY_PROBES
from odoo_mcp.adapters.odoo.client import OdooClient
from odoo_mcp.adapters.odoo.transports.json2 import Json2Transport
from odoo_mcp.adapters.odoo.transports.json_rpc import JsonRpcTransport
from odoo_mcp.app.settings import OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import TrialBalanceInput
from odoo_mcp.workflows.accounting.reports import get_trial_balance
from odoo_mcp.workflows.core.capabilities import get_erp_capabilities


def _version_response(major: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={"result": {"server_version_info": [major, 0, 0, "final", 0, "e"]}},
    )


async def test_write_method_payloads_match_odoo_18_and_19_contracts(
    connection: OdooConnectionSettings,
) -> None:
    json2_requests: list[dict[str, Any]] = []
    rpc_requests: list[dict[str, Any]] = []

    def json2_handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = __import__("json").loads(request.content)
        json2_requests.append(body)
        assert request.url.path in {
            "/json/2/account.move/action_post",
            "/json/2/account.move/create",
        }
        return httpx.Response(200, json=901 if request.url.path.endswith("/create") else True)

    def rpc_handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = __import__("json").loads(request.content)
        rpc_requests.append(body)
        if len(rpc_requests) == 1:
            return httpx.Response(200, json={"result": 7})
        return httpx.Response(200, json={"result": True})

    json2_client = httpx.AsyncClient(
        transport=httpx.MockTransport(json2_handler), base_url=str(connection.url)
    )
    rpc_client = httpx.AsyncClient(
        transport=httpx.MockTransport(rpc_handler), base_url=str(connection.url)
    )
    json2 = Json2Transport(connection, json2_client)
    rpc = JsonRpcTransport(connection, rpc_client)
    try:
        await json2.execute_method("account.move", "action_post", ids=(101,), company_ids=(1,))
        await json2.execute_method(
            "account.move",
            "create",
            named={"vals_list": {"move_type": "out_invoice"}},
            company_ids=(1,),
        )
        await rpc.authenticate()
        await rpc.execute_method("account.move", "action_post", ids=(101,), company_ids=(1,))
        await rpc.execute_method(
            "account.move",
            "create",
            named={"vals_list": {"move_type": "out_invoice"}},
            company_ids=(1,),
        )
    finally:
        await json2.close()
        await rpc.close()

    assert json2_requests == [
        {"ids": [101], "context": {"allowed_company_ids": [1]}},
        {
            "vals_list": {"move_type": "out_invoice"},
            "context": {"allowed_company_ids": [1]},
        },
    ]
    rpc_args = rpc_requests[1]["params"]["args"]
    assert rpc_args[3:6] == ["account.move", "action_post", [[101]]]
    assert rpc_args[6] == {"context": {"allowed_company_ids": [1]}}
    rpc_create_args = rpc_requests[2]["params"]["args"]
    assert rpc_create_args[3:6] == [
        "account.move",
        "create",
        [{"move_type": "out_invoice"}],
    ]
    assert rpc_create_args[6] == {"context": {"allowed_company_ids": [1]}}


@pytest.mark.parametrize(
    ("major", "expected_transport"),
    [(18, "json_rpc"), (19, "json2")],
)
async def test_connect_selects_version_transport_and_discovers_authorized_scope(
    connection: OdooConnectionSettings,
    major: int,
    expected_transport: str,
) -> None:
    requests: list[httpx.Request] = []
    probed_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/web/webclient/version_info":
            return _version_response(major)
        body: dict[str, Any] = __import__("json").loads(request.content)
        if major == 18:
            args = body["params"]["args"]
            if body["params"]["service"] == "common":
                assert args[:2] == ["synthetic-db", "synthetic-user"]
                return httpx.Response(200, json={"result": 7})
            model, method = args[3], args[4]
            if method == "search_count":
                probed_models.append(model)
                assert args[6] == {"context": {"allowed_company_ids": [1, 2]}}
                if model in {"res.company", "account.move"}:
                    return httpx.Response(200, json={"result": 1})
                return httpx.Response(
                    200,
                    json={
                        "error": {
                            "message": "synthetic",
                            "data": {"name": "odoo.exceptions.AccessError"},
                        }
                    },
                )
            assert model == "res.company"
            assert args[5] == [[["id", "in", [1, 2]]]]
            assert args[6]["fields"] == ["id", "name", "currency_id"]
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"id": 2, "name": "Beta", "currency_id": [2, "USD"]},
                        {"id": 1, "name": "Alpha", "currency_id": [1, "KES"]},
                    ]
                },
            )
        assert request.headers["authorization"] == "bearer synthetic-secret"
        assert request.headers["x-odoo-database"] == "synthetic-db"
        if request.url.path == "/json/2/res.users/context_get":
            return httpx.Response(200, json={"uid": 7})
        if request.url.path.endswith("/search_count"):
            probed_models.append(request.url.path.split("/")[3])
            assert body["context"] == {"allowed_company_ids": [1, 2]}
            available = request.url.path in {
                "/json/2/res.company/search_count",
                "/json/2/account.move/search_count",
            }
            return httpx.Response(200 if available else 404, json=1 if available else {})
        assert request.url.path == "/json/2/res.company/search_read"
        assert body["domain"] == [["id", "in", [1, 2]]]
        assert body["fields"] == ["id", "name", "currency_id"]
        return httpx.Response(
            200,
            json=[
                {"id": 2, "name": "Beta", "currency_id": [2, "USD"]},
                {"id": 1, "name": "Alpha", "currency_id": [1, "KES"]},
            ],
        )

    adapter = await OdooClient.connect(connection, http_transport=httpx.MockTransport(handler))
    try:
        companies = await adapter.get_companies()
        capabilities = await adapter.get_capabilities()
        discovery = await get_erp_capabilities(
            adapter,
            permissions=frozenset(),
            default_company_id=1,
            tools=(),
            request_id="req_synthetic",
        )
    finally:
        await adapter.close()

    assert capabilities.version == major
    assert capabilities.transport == expected_transport
    assert capabilities.edition == "enterprise"
    assert CAPABILITY_PROBES == {
        "base": "res.company",
        "account": "account.move",
        "account_accountant": "account.bank.statement.line",
    }
    assert probed_models == [*CAPABILITY_PROBES.values(), *CAPABILITY_PROBES.values()]
    assert capabilities.modules == {
        "base": True,
        "account": True,
        "account_accountant": False,
    }
    assert [item.name for item in discovery.installed_modules] == [
        "account",
        "account_accountant",
        "base",
    ]
    assert [(company.id, company.name) for company in companies] == [(1, "Alpha"), (2, "Beta")]
    assert [
        (company.currency.id, company.currency.name) for company in companies if company.currency
    ] == [
        (1, "KES"),
        (2, "USD"),
    ]
    assert requests[0].url.path == "/web/webclient/version_info"


async def test_unsupported_version_stops_before_authentication(
    connection: OdooConnectionSettings,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _version_response(17)

    with pytest.raises(OdooMcpError) as caught:
        await OdooClient.connect(connection, http_transport=httpx.MockTransport(handler))

    assert caught.value.code is ErrorCode.ODOO_VERSION_UNSUPPORTED
    assert [request.url.path for request in requests] == ["/web/webclient/version_info"]


async def test_community_edition_stops_before_authentication(
    connection: OdooConnectionSettings,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"result": {"server_version_info": [18, 0, 0, "final", 0, "c"]}},
        )

    with pytest.raises(OdooMcpError) as caught:
        await OdooClient.connect(connection, http_transport=httpx.MockTransport(handler))

    assert caught.value.code is ErrorCode.ODOO_VERSION_UNSUPPORTED
    assert [request.url.path for request in requests] == ["/web/webclient/version_info"]


async def test_transport_failure_is_safe_and_not_a_false_empty_result(
    connection: OdooConnectionSettings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/web/webclient/version_info":
            return _version_response(19)
        if request.url.path == "/json/2/res.users/context_get":
            return httpx.Response(200, json={"uid": 7})
        if request.url.path == "/json/2/res.company/search_read":
            return httpx.Response(
                200,
                json=[{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}],
            )
        raise httpx.ReadTimeout("contains synthetic-secret", request=request)

    adapter = await OdooClient.connect(connection, http_transport=httpx.MockTransport(handler))
    try:
        await adapter.get_companies()
        with pytest.raises(OdooMcpError) as caught:
            await adapter.get_capabilities()
    finally:
        await adapter.close()

    assert caught.value.code is ErrorCode.ODOO_API_ERROR
    assert "synthetic-secret" not in str(caught.value)


async def test_missing_authorized_company_fails_explicitly(
    connection: OdooConnectionSettings,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/web/webclient/version_info":
            return _version_response(19)
        if request.url.path == "/json/2/res.users/context_get":
            return httpx.Response(200, json={"uid": 7})
        if request.url.path.endswith("/search_read"):
            return httpx.Response(200, json=[{"id": 1, "name": "Alpha"}])
        return httpx.Response(200, json=1)

    requests: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    adapter = await OdooClient.connect(
        connection,
        http_transport=httpx.MockTransport(recording_handler),
    )
    try:
        with pytest.raises(OdooMcpError) as caught:
            await get_erp_capabilities(
                adapter,
                permissions=frozenset({"core_read"}),
                default_company_id=connection.default_company_id,
                tools=(),
            )
    finally:
        await adapter.close()

    assert caught.value.code is ErrorCode.COMPANY_NOT_FOUND
    assert not any(request.url.path.endswith("/search_count") for request in requests)


async def test_capability_probes_require_validated_companies(
    connection: OdooConnectionSettings,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/web/webclient/version_info":
            return _version_response(19)
        if request.url.path == "/json/2/res.users/context_get":
            return httpx.Response(200, json={"uid": 7})
        pytest.fail("A capability probe ran before company validation")

    adapter = await OdooClient.connect(
        connection,
        http_transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(OdooMcpError) as caught:
            await adapter.get_capabilities()
    finally:
        await adapter.close()

    assert caught.value.code is ErrorCode.ODOO_AUTH_FAILED
    assert not any(request.url.path.endswith("/search_count") for request in requests)


@pytest.mark.parametrize("major", [18, 19])
async def test_authentication_rejection_is_translated_without_raw_error(
    connection: OdooConnectionSettings,
    major: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/web/webclient/version_info":
            return _version_response(major)
        if major == 18:
            return httpx.Response(
                200,
                json={"error": {"message": "raw synthetic-secret authentication failure"}},
            )
        return httpx.Response(401, json={"message": "raw synthetic-secret"})

    with pytest.raises(OdooMcpError) as caught:
        await OdooClient.connect(connection, http_transport=httpx.MockTransport(handler))

    assert caught.value.code is ErrorCode.ODOO_AUTH_FAILED
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.parametrize("major", [18, 19])
async def test_accounting_read_uses_the_selected_transport_contract(
    connection: OdooConnectionSettings,
    major: int,
) -> None:
    accounting_requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/web/webclient/version_info":
            return _version_response(major)
        body: dict[str, Any] = __import__("json").loads(request.content)
        if major == 18:
            args = body["params"]["args"]
            if body["params"]["service"] == "common":
                return httpx.Response(200, json={"result": 7})
            model, method = args[3], args[4]
            if model == "res.company":
                return httpx.Response(
                    200,
                    json={
                        "result": [
                            {"id": 1, "name": "Alpha"},
                            {"id": 2, "name": "Beta"},
                        ]
                    },
                )
            assert model == "account.move" and method == "search_read"
            accounting_requests.append(
                {"domain": args[5][0], "keywords": args[6], "path": request.url.path}
            )
            return httpx.Response(200, json={"result": [_account_move_row()]})
        if request.url.path == "/json/2/res.users/context_get":
            return httpx.Response(200, json={"uid": 7})
        if request.url.path == "/json/2/res.company/search_read":
            return httpx.Response(
                200,
                json=[{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}],
            )
        assert request.url.path == "/json/2/account.move/search_read"
        accounting_requests.append({**body, "path": request.url.path})
        return httpx.Response(200, json=[_account_move_row()])

    adapter = await OdooClient.connect(
        connection,
        http_transport=httpx.MockTransport(handler),
    )
    try:
        await adapter.get_companies()
        page = await adapter.get_account_moves(1, ReadFilters(), PageRequest(limit=1))
    finally:
        await adapter.close()

    assert page.items[0].id == 10
    request = accounting_requests[0]
    assert request["domain"] == [["company_id", "=", 1]]
    if major == 18:
        assert request["keywords"]["limit"] == 2
        assert request["keywords"]["offset"] == 0
        assert request["keywords"]["order"] == "id asc"
        assert request["keywords"]["context"] == {"allowed_company_ids": [1]}
    else:
        assert request["limit"] == 2
        assert request["offset"] == 0
        assert request["order"] == "id asc"
        assert request["context"] == {"allowed_company_ids": [1]}


@pytest.mark.parametrize("major", [18, 19])
async def test_trial_balance_reconciles_through_both_transport_contracts(
    connection: OdooConnectionSettings,
    major: int,
) -> None:
    def result_for(model: str) -> list[dict[str, object]]:
        if model == "res.company":
            return [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}]
        if model == "account.move.line":
            opening = _account_move_line_row()
            opening.update(
                {
                    "id": 1,
                    "date": "2025-12-31",
                    "debit": "100",
                    "balance": "100",
                    "amount_currency": "100",
                }
            )
            movement = _account_move_line_row()
            movement["id"] = 2
            return [opening, movement]
        if model == "account.account":
            return [_account_row()]
        pytest.fail(f"Unexpected model: {model}")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/web/webclient/version_info":
            return _version_response(major)
        body: dict[str, Any] = __import__("json").loads(request.content)
        if major == 18:
            args = body["params"]["args"]
            if body["params"]["service"] == "common":
                return httpx.Response(200, json={"result": 7})
            return httpx.Response(200, json={"result": result_for(args[3])})
        if request.url.path == "/json/2/res.users/context_get":
            return httpx.Response(200, json={"uid": 7})
        model = request.url.path.split("/")[3]
        return httpx.Response(200, json=result_for(model))

    adapter = await OdooClient.connect(
        connection,
        http_transport=httpx.MockTransport(handler),
    )
    try:
        await adapter.get_companies()
        result = await get_trial_balance(
            adapter,
            TrialBalanceInput(
                company_id=1,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
                account_ids=(3,),
            ),
            company_name="Alpha",
            request_id=f"req_{major}",
        )
    finally:
        await adapter.close()

    assert result.summary.period_debit == Decimal("25.50")
    assert result.summary.period_credit == Decimal("0")
    assert result.summary.closing_balance == Decimal("125.50")
    assert result.items[0].opening_balance == Decimal("100")


@pytest.mark.parametrize("major", [18, 19])
async def test_accounting_acl_denial_is_safe_and_specific(
    connection: OdooConnectionSettings,
    major: int,
) -> None:
    marker = "raw-odoo-permission-synthetic-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/web/webclient/version_info":
            return _version_response(major)
        body: dict[str, Any] = __import__("json").loads(request.content)
        if major == 18:
            args = body["params"]["args"]
            if body["params"]["service"] == "common":
                return httpx.Response(200, json={"result": 7})
            if args[3] == "res.company":
                return httpx.Response(
                    200,
                    json={
                        "result": [
                            {"id": 1, "name": "Alpha"},
                            {"id": 2, "name": "Beta"},
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "error": {
                        "message": marker,
                        "data": {
                            "name": "odoo.exceptions.AccessError",
                            "debug": marker,
                        },
                    }
                },
            )
        if request.url.path == "/json/2/res.users/context_get":
            return httpx.Response(200, json={"uid": 7})
        if request.url.path == "/json/2/res.company/search_read":
            return httpx.Response(
                200,
                json=[{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}],
            )
        return httpx.Response(403, json={"message": marker})

    adapter = await OdooClient.connect(
        connection,
        http_transport=httpx.MockTransport(handler),
    )
    try:
        await adapter.get_companies()
        with pytest.raises(OdooMcpError) as caught:
            await adapter.get_account_moves(1, ReadFilters(), PageRequest(limit=1))
    finally:
        await adapter.close()

    assert caught.value.code is ErrorCode.ODOO_PERMISSION_DENIED
    assert marker not in str(caught.value)


def _account_move_row() -> dict[str, object]:
    return {
        "id": 10,
        "name": "MVE/10",
        "move_type": "entry",
        "state": "posted",
        "date": "2026-09-01",
        "invoice_date": False,
        "invoice_date_due": False,
        "partner_id": False,
        "journal_id": [30, "Synthetic Journal"],
        "company_id": [1, "Alpha"],
        "currency_id": [40, "Synthetic Currency"],
        "amount_total": 1.0,
        "amount_residual": 0.0,
        "payment_state": False,
        "ref": False,
    }


def _account_move_line_row() -> dict[str, object]:
    return {
        "id": 1,
        "move_id": [2, "MVE/2"],
        "account_id": [3, "Cash"],
        "journal_id": [4, "General"],
        "partner_id": False,
        "company_id": [1, "Alpha"],
        "currency_id": False,
        "date": "2026-01-15",
        "date_maturity": False,
        "name": "Synthetic line",
        "debit": "25.50",
        "credit": "0",
        "balance": "25.50",
        "amount_currency": "25.50",
        "amount_residual": "0",
        "amount_residual_currency": "0",
        "reconciled": True,
        "analytic_distribution": {},
    }


def _account_row() -> dict[str, object]:
    return {
        "id": 3,
        "code": "1000",
        "name": "Cash",
        "account_type": "asset_cash",
        "company_ids": [1],
        "currency_id": False,
        "reconcile": False,
    }
