"""Run the bounded, read-only Odoo 19 adapter qualification."""

from __future__ import annotations

import asyncio
import sys
from datetime import date

from odoo_mcp.adapters.accounting import DatePeriod, FilterClause, PageRequest, ReadFilters
from odoo_mcp.adapters.odoo.client import OdooClient
from odoo_mcp.app.settings import DeploymentProfile, SettingsError, load_settings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError


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
                "The configured qualification target is not Odoo 19 JSON-2.",
                "Configure an authorized non-production Odoo 19 connection and retry.",
            )
        if not capabilities.modules.get("account", False):
            raise OdooMcpError(
                ErrorCode.CAPABILITY_NOT_AVAILABLE,
                "The Accounting application is unavailable to the technical user.",
                "Install Accounting and grant the required least-privilege access.",
            )

        company_id = connection.default_company_id
        if company_id not in {company.id for company in companies}:
            raise OdooMcpError(
                ErrorCode.COMPANY_NOT_FOUND,
                "The default company is unavailable.",
                "Configure an authorized default company and retry.",
            )
        page = PageRequest(limit=1)
        today = date.today()
        month = DatePeriod(start=today.replace(day=1), end=today)

        await adapter.get_account_moves(
            company_id,
            ReadFilters(clauses=(FilterClause(field="state", operator="=", value="posted"),)),
            page,
        )
        await adapter.get_account_move_lines(
            company_id,
            ReadFilters(
                clauses=(FilterClause(field="move_id.state", operator="=", value="posted"),)
            ),
            page,
        )
        await adapter.get_partial_reconciliations(
            company_id,
            ReadFilters(clauses=(FilterClause(field="max_date", operator="<=", value=today),)),
            page,
        )
        journals = await adapter.get_journals(company_id, page=page)
        await adapter.get_bank_statement_lines(company_id, month, None, page=page)
        await adapter.get_payment_terms(company_id, page=page)
        if not journals.items:
            raise OdooMcpError(
                ErrorCode.CAPABILITY_NOT_AVAILABLE,
                "No authorized journal is available for read qualification.",
                "Configure an authorized accounting journal and retry.",
            )
        await adapter.get_payment_method_lines(
            company_id,
            (journals.items[0].id,),
            page=page,
        )
        await adapter.get_partners(company_id, ReadFilters(), page=page)
        await adapter.get_products(company_id, ReadFilters(), page=page)
        await adapter.get_account_accounts(company_id, ReadFilters(), page=page)
        await adapter.get_analytic_accounts(company_id, ReadFilters(), page=page)
    finally:
        await adapter.close()


def main() -> None:
    try:
        asyncio.run(_qualify())
    except OdooMcpError as exc:
        print(
            f"Live Odoo 19 read-only qualification failed safely: {exc.code.value}", file=sys.stderr
        )
        raise SystemExit(1) from None
    except SettingsError:
        print("Live Odoo 19 read-only qualification failed safely.", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("Live Odoo 19 read-only qualification failed safely.", file=sys.stderr)
        raise SystemExit(1) from None
    print("Live Odoo 19 read-only qualification passed.")


if __name__ == "__main__":
    main()
