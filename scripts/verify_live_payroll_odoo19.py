"""Run the fixed-output, read-only Odoo 19 Payroll schema qualification."""

from __future__ import annotations

import asyncio
import sys

from odoo_mcp.adapters.odoo.client import OdooClient
from odoo_mcp.app.settings import DeploymentProfile, SettingsError, load_settings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError


async def _qualify() -> bool:
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
        if not capabilities.modules.get("hr_payroll", False):
            return False
        company_id = connection.default_company_id
        if company_id not in {company.id for company in companies}:
            raise OdooMcpError(
                ErrorCode.COMPANY_NOT_FOUND,
                "The default company is unavailable.",
                "Configure an authorized default company and retry.",
            )
        await adapter.verify_payroll_schema(company_id)
        return True
    finally:
        await adapter.close()


def main() -> None:
    try:
        qualified = asyncio.run(_qualify())
    except OdooMcpError as exc:
        print(
            f"Live Odoo 19 Payroll schema qualification failed safely: {exc.code.value}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except SettingsError:
        print(
            "Live Odoo 19 Payroll schema qualification failed safely.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except Exception:
        print(
            "Live Odoo 19 Payroll schema qualification failed safely.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if not qualified:
        print("Live Odoo 19 Payroll schema qualification unavailable: capability not present.")
        return
    print("Live Odoo 19 Payroll schema qualification passed.")


if __name__ == "__main__":
    main()
