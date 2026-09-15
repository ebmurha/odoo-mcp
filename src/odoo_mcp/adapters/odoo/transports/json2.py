"""Odoo 19 external JSON-2 transport."""

from __future__ import annotations

from typing import Any

import httpx

from odoo_mcp.app.settings import OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError


class Json2Transport:
    name = "json2"

    def __init__(self, connection: OdooConnectionSettings, client: httpx.AsyncClient) -> None:
        self._client = client
        self._headers = {
            "Authorization": f"bearer {connection.api_key.get_secret_value()}",
            "X-Odoo-Database": connection.database,
            "User-Agent": "odoo-mcp/0.1.0",
        }

    async def _call(
        self,
        model: str,
        method: str,
        payload: dict[str, Any],
        *,
        authenticating: bool = False,
    ) -> Any:
        try:
            response = await self._client.post(
                f"/json/2/{model}/{method}",
                headers=self._headers,
                json=payload,
            )
            if response.status_code == 401 or (
                authenticating and response.status_code in {400, 403, 404}
            ):
                raise OdooMcpError(
                    ErrorCode.ODOO_AUTH_FAILED,
                    "Odoo authentication failed.",
                    "Check the database, API key, and technical-user access.",
                )
            if response.status_code == 403:
                raise OdooMcpError(
                    ErrorCode.ODOO_PERMISSION_DENIED,
                    "Odoo denied the requested operation.",
                    "Grant the required least-privilege Odoo access and retry.",
                )
            response.raise_for_status()
            return response.json()
        except OdooMcpError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo could not complete the request.",
                "Check Odoo availability, access rights, and configuration.",
            ) from exc

    async def authenticate(self) -> None:
        result = await self._call("res.users", "context_get", {}, authenticating=True)
        if not isinstance(result, dict):
            raise OdooMcpError(
                ErrorCode.ODOO_AUTH_FAILED,
                "Odoo authentication failed.",
                "Check the database, API key, and technical-user access.",
            )

    async def probe_model(self, model: str, *, company_ids: tuple[int, ...]) -> bool:
        try:
            response = await self._client.post(
                f"/json/2/{model}/search_count",
                headers=self._headers,
                json={
                    "domain": [],
                    "context": {"allowed_company_ids": list(company_ids)},
                },
            )
            if response.status_code == 401:
                raise OdooMcpError(
                    ErrorCode.ODOO_AUTH_FAILED,
                    "Odoo authentication failed.",
                    "Check the database, API key, and technical-user access.",
                )
            if response.status_code in {403, 404}:
                return False
            response.raise_for_status()
            result = response.json()
        except OdooMcpError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo could not complete the capability probe.",
                "Check Odoo availability and retry.",
            ) from exc
        return isinstance(result, int) and not isinstance(result, bool)

    async def search_count(
        self,
        model: str,
        domain: list[Any],
        *,
        company_ids: tuple[int, ...],
    ) -> int:
        result = await self._call(
            model,
            "search_count",
            {
                "domain": domain,
                "context": {"allowed_company_ids": list(company_ids)},
            },
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
        result = await self._call(
            model,
            "search_read",
            {
                "domain": domain,
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

    async def close(self) -> None:
        await self._client.aclose()
