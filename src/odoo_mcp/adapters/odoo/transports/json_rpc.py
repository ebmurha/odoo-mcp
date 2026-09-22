"""Odoo 18 external JSON-RPC transport."""

from __future__ import annotations

from typing import Any

import httpx

from odoo_mcp.app.settings import OdooConnectionSettings, OdooEnrollmentCredentials
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError


class JsonRpcTransport:
    name = "json_rpc"

    def __init__(
        self,
        connection: OdooConnectionSettings | OdooEnrollmentCredentials,
        client: httpx.AsyncClient,
    ) -> None:
        self._connection = connection
        self._client = client
        self._uid: int | None = None

    async def _call(
        self,
        service: str,
        method: str,
        args: list[Any],
        *,
        rejection_code: ErrorCode = ErrorCode.ODOO_API_ERROR,
    ) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": 1,
        }
        try:
            response = await self._client.post("/jsonrpc", json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo could not complete the request.",
                "Check Odoo availability and retry.",
            ) from exc
        if not isinstance(body, dict):
            raise OdooMcpError(
                rejection_code,
                "Odoo authentication failed."
                if rejection_code is ErrorCode.ODOO_AUTH_FAILED
                else "Odoo rejected the request.",
                "Check the database, username, API key, and technical-user access."
                if rejection_code is ErrorCode.ODOO_AUTH_FAILED
                else "Check the technical user's access and Odoo configuration.",
            )
        if "error" in body:
            error = body.get("error")
            data = error.get("data") if isinstance(error, dict) else None
            name = data.get("name") if isinstance(data, dict) else None
            code = rejection_code
            if rejection_code is not ErrorCode.ODOO_AUTH_FAILED and isinstance(name, str):
                if name.endswith(("AccessError", "AccessDenied")):
                    code = ErrorCode.ODOO_PERMISSION_DENIED
            raise OdooMcpError(
                code,
                "Odoo authentication failed."
                if code is ErrorCode.ODOO_AUTH_FAILED
                else "Odoo denied the requested operation."
                if code is ErrorCode.ODOO_PERMISSION_DENIED
                else "Odoo rejected the request.",
                "Check the database, username, API key, and technical-user access."
                if code is ErrorCode.ODOO_AUTH_FAILED
                else "Grant the required least-privilege Odoo access and retry."
                if code is ErrorCode.ODOO_PERMISSION_DENIED
                else "Check the technical user's access and Odoo configuration.",
            )
        if "result" not in body:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned an invalid response.",
                "Check Odoo compatibility and retry.",
            )
        return body["result"]

    async def probe_model(self, model: str, *, company_ids: tuple[int, ...]) -> bool:
        if self._uid is None:
            raise OdooMcpError(
                ErrorCode.ODOO_AUTH_FAILED,
                "The Odoo connection is not authenticated.",
                "Reconnect to Odoo and retry.",
            )
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "object",
                "method": "execute_kw",
                "args": [
                    self._connection.database,
                    self._uid,
                    self._connection.api_key.get_secret_value(),
                    model,
                    "search_count",
                    [[]],
                    {"context": {"allowed_company_ids": list(company_ids)}},
                ],
            },
            "id": 1,
        }
        try:
            response = await self._client.post("/jsonrpc", json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo could not complete the capability probe.",
                "Check Odoo availability and retry.",
            ) from exc
        if not isinstance(body, dict):
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned an invalid capability response.",
                "Check Odoo compatibility and retry.",
            )
        if "error" in body:
            error = body.get("error")
            data = error.get("data") if isinstance(error, dict) else None
            name = data.get("name") if isinstance(data, dict) else None
            if isinstance(name, str) and name.endswith(("AccessError", "KeyError")):
                return False
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo could not complete the capability probe.",
                "Check Odoo compatibility and retry.",
            )
        result = body.get("result")
        return isinstance(result, int) and not isinstance(result, bool)

    async def authenticate(self) -> None:
        result = await self._call(
            "common",
            "authenticate",
            [
                self._connection.database,
                self._connection.username,
                self._connection.api_key.get_secret_value(),
                {},
            ],
            rejection_code=ErrorCode.ODOO_AUTH_FAILED,
        )
        if not isinstance(result, int) or isinstance(result, bool) or result <= 0:
            raise OdooMcpError(
                ErrorCode.ODOO_AUTH_FAILED,
                "Odoo authentication failed.",
                "Check the database, username, API key, and technical-user access.",
            )
        self._uid = result

    async def discover_companies(self, *, limit: int) -> list[dict[str, Any]]:
        result = await self._execute_kw(
            "res.company",
            "search_read",
            [[]],
            {"fields": ["id", "name"], "limit": limit, "order": "id asc"},
        )
        if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned an invalid company response.",
                "Check Odoo compatibility and retry.",
            )
        return result

    async def _execute_kw(
        self,
        model: str,
        method: str,
        positional: list[Any],
        keywords: dict[str, Any] | None = None,
    ) -> Any:
        if self._uid is None:
            raise OdooMcpError(
                ErrorCode.ODOO_AUTH_FAILED,
                "The Odoo connection is not authenticated.",
                "Reconnect to Odoo and retry.",
            )
        return await self._call(
            "object",
            "execute_kw",
            [
                self._connection.database,
                self._uid,
                self._connection.api_key.get_secret_value(),
                model,
                method,
                positional,
                keywords or {},
            ],
        )

    async def search_count(
        self,
        model: str,
        domain: list[Any],
        *,
        company_ids: tuple[int, ...],
    ) -> int:
        result = await self._execute_kw(
            model,
            "search_count",
            [domain],
            {"context": {"allowed_company_ids": list(company_ids)}},
        )
        if not isinstance(result, int) or isinstance(result, bool):
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned an invalid response.",
                "Check Odoo compatibility and retry.",
            )
        return result

    async def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        *,
        limit: int,
        offset: int = 0,
        order: str = "id",
        company_ids: tuple[int, ...],
    ) -> list[dict[str, Any]]:
        result = await self._execute_kw(
            model,
            "search_read",
            [domain],
            {
                "fields": fields,
                "limit": limit,
                "offset": offset,
                "order": order,
                "context": {"allowed_company_ids": list(company_ids)},
            },
        )
        if not isinstance(result, list) or not all(isinstance(row, dict) for row in result):
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned an invalid response.",
                "Check Odoo compatibility and retry.",
            )
        return result

    async def execute_method(
        self,
        model: str,
        method: str,
        *,
        ids: tuple[int, ...] = (),
        positional: list[Any] | None = None,
        named: dict[str, Any] | None = None,
        company_ids: tuple[int, ...],
    ) -> Any:
        arguments: list[Any] = []
        if ids:
            arguments.append(list(ids))
        arguments.extend(positional or [])
        keywords = dict(named or {})
        if method == "create" and not arguments and "vals_list" in keywords:
            arguments.append(keywords.pop("vals_list"))
        context = keywords.pop("context", {})
        if not isinstance(context, dict):
            context = {}
        context["allowed_company_ids"] = list(company_ids)
        keywords["context"] = context
        return await self._execute_kw(model, method, arguments, keywords)

    async def close(self) -> None:
        await self._client.aclose()
