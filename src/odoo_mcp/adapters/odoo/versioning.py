"""Odoo major-version detection with no transport fallback."""

from __future__ import annotations

from typing import Any

import httpx

from odoo_mcp.app.settings import OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError

VERSION_PATH = "/web/webclient/version_info"


async def detect_major_version(
    connection: OdooConnectionSettings,
    client: httpx.AsyncClient,
) -> int:
    """Detect and validate the Odoo major version from the public web endpoint."""

    payload = {"jsonrpc": "2.0", "method": "call", "params": {}, "id": 1}
    try:
        response = await client.post(VERSION_PATH, json=payload)
        response.raise_for_status()
        body: Any = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OdooMcpError(
            ErrorCode.ODOO_TRANSPORT_NEGOTIATION_FAILED,
            "Odoo version detection failed.",
            "Check the Odoo URL and network access, then retry.",
        ) from exc
    result = body.get("result") if isinstance(body, dict) else None
    version_info = result.get("server_version_info") if isinstance(result, dict) else None
    if not isinstance(version_info, list) or not version_info:
        raise OdooMcpError(
            ErrorCode.ODOO_TRANSPORT_NEGOTIATION_FAILED,
            "Odoo returned an invalid version response.",
            "Verify that the URL points to a supported Odoo server.",
        )
    major = version_info[0]
    if not isinstance(major, int) or isinstance(major, bool):
        raise OdooMcpError(
            ErrorCode.ODOO_TRANSPORT_NEGOTIATION_FAILED,
            "Odoo returned an invalid version response.",
            "Verify that the URL points to a supported Odoo server.",
        )
    if major not in {18, 19}:
        raise OdooMcpError(
            ErrorCode.ODOO_VERSION_UNSUPPORTED,
            "The detected Odoo major version is not supported.",
            "Use Odoo Enterprise 18 or 19.",
        )
    edition_marker = version_info[5] if len(version_info) > 5 else None
    if edition_marker != "e":
        raise OdooMcpError(
            ErrorCode.ODOO_VERSION_UNSUPPORTED,
            "The detected Odoo edition is not supported.",
            "Use Odoo Enterprise 18 or 19.",
        )
    return major
