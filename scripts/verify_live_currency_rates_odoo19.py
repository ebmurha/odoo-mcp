"""Run the narrow, read-only Odoo 19 currency-rate qualification."""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta

from odoo_mcp.adapters.accounting import PageRequest
from odoo_mcp.adapters.odoo.client import OdooClient
from odoo_mcp.app.settings import DeploymentProfile, SettingsError, load_settings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import CurrencyRateHistoryInput
from odoo_mcp.workflows.accounting.currency_rates import get_currency_rate_history

SUCCESS = "Live Odoo 19 currency-rate qualification passed."
FAILURE = "Live Odoo 19 currency-rate qualification failed safely."


async def _qualify() -> None:
    settings = load_settings(DeploymentProfile.LOCAL)
    if settings.connection is None:
        raise SettingsError("The local Odoo connection is unavailable")
    connection = settings.connection
    adapter = await OdooClient.connect(connection)
    try:
        companies = await adapter.get_companies()
        capabilities = await adapter.get_capabilities()
        if capabilities.version != 19 or capabilities.transport != "json2":
            raise OdooMcpError(
                ErrorCode.ODOO_VERSION_UNSUPPORTED,
                "The qualification target is not Odoo 19 JSON-2.",
                "Use an authorized non-production Odoo 19 connection.",
            )
        if not capabilities.modules.get("account", False):
            raise OdooMcpError(
                ErrorCode.CAPABILITY_NOT_AVAILABLE,
                "Accounting is unavailable.",
                "Grant the required least-privilege access.",
            )
        company = next(
            (item for item in companies if item.id == connection.default_company_id), None
        )
        if company is None:
            raise OdooMcpError(
                ErrorCode.COMPANY_NOT_FOUND,
                "The default company is unavailable.",
                "Use an authorized default company.",
            )
        if company.currency is None or company.root_id is None:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "The company currency context is incomplete.",
                "Check Odoo company access.",
            )
        today = date.today()
        await adapter.get_currency_rates(
            company.id,
            company.currency.id,
            today,
            page=PageRequest(limit=1),
        )
        result = await get_currency_rate_history(
            adapter,
            CurrencyRateHistoryInput(
                company_id=company.id,
                currency_id=company.currency.id,
                period_start=today - timedelta(days=30),
                period_end=today,
                limit=1,
            ),
            company=company,
            request_id="req_live_currency_rate_qualification",
        )
        if result.summary.history_status != "company_currency_identity":
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "The company-currency identity result is invalid.",
                "Check Odoo currency data and retry.",
            )
    finally:
        await adapter.close()


def main() -> None:
    try:
        asyncio.run(_qualify())
    except (OdooMcpError, SettingsError):
        print(FAILURE, file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print(FAILURE, file=sys.stderr)
        raise SystemExit(1) from None
    print(SUCCESS)


if __name__ == "__main__":
    main()
