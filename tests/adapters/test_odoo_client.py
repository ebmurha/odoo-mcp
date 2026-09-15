from __future__ import annotations

from typing import Any

import httpx
import pytest

from odoo_mcp.adapters.accounting import PageRequest, ReadFilters
from odoo_mcp.adapters.odoo.capabilities import CAPABILITY_PROBES
from odoo_mcp.adapters.odoo.client import OdooClient
from odoo_mcp.app.settings import OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.workflows.core.capabilities import get_erp_capabilities


def _version_response(major: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={"result": {"server_version_info": [major, 0, 0, "final", 0, "e"]}},
    )


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
            return httpx.Response(
                200,
                json={"result": [{"id": 2, "name": "Beta"}, {"id": 1, "name": "Alpha"}]},
            )
        assert request.headers["authorization"] == "bearer synthetic-secret"
        assert request.headers["x-odoo-database"] == "synthetic-db"
        if request.url.path == "/json/2/res.users/context_get":
            return httpx.Response(200, json={"uid": 7})
        if request.url.path.endswith("/search_count"):
            assert body["context"] == {"allowed_company_ids": [1, 2]}
            available = request.url.path in {
                "/json/2/res.company/search_count",
                "/json/2/account.move/search_count",
            }
            return httpx.Response(200 if available else 404, json=1 if available else {})
        assert request.url.path == "/json/2/res.company/search_read"
        assert body["domain"] == [["id", "in", [1, 2]]]
        return httpx.Response(
            200,
            json=[{"id": 2, "name": "Beta"}, {"id": 1, "name": "Alpha"}],
        )

    adapter = await OdooClient.connect(connection, http_transport=httpx.MockTransport(handler))
    try:
        companies = await adapter.get_companies()
        capabilities = await adapter.get_capabilities()
    finally:
        await adapter.close()

    assert capabilities.version == major
    assert capabilities.transport == expected_transport
    assert capabilities.edition == "enterprise"
    assert capabilities.modules == {name: name in {"base", "account"} for name in CAPABILITY_PROBES}
    assert [(company.id, company.name) for company in companies] == [(1, "Alpha"), (2, "Beta")]
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
