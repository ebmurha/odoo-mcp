"""Run the bounded, read-only Odoo 19 adapter qualification."""

from __future__ import annotations

import asyncio
import sys
from datetime import date

from odoo_mcp.adapters.accounting import DatePeriod, FilterClause, PageRequest, ReadFilters
from odoo_mcp.adapters.odoo.client import OdooClient
from odoo_mcp.app.settings import DeploymentProfile, SettingsError, load_settings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import (
    AgingInput,
    BalanceSheetInput,
    ProfitAndLossInput,
    TrialBalanceInput,
)
from odoo_mcp.workflows.accounting.reports import (
    get_aged_balance,
    get_balance_sheet,
    get_profit_and_loss,
    get_trial_balance,
)

_check_stage = "startup"


async def _qualify() -> None:
    global _check_stage
    settings = load_settings(DeploymentProfile.LOCAL)
    if settings.connection is None:
        raise SettingsError("The local Odoo connection is unavailable")
    connection = settings.connection
    _check_stage = "connection"
    adapter = await OdooClient.connect(connection)
    try:
        _check_stage = "company and capability discovery"
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

        _check_stage = "adapter accounting reads"
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
        await adapter.get_partners(company_id, ReadFilters(), page=page)
        await adapter.get_products(company_id, ReadFilters(), page=page)
        await adapter.get_account_accounts(company_id, ReadFilters(), page=page)
        await adapter.get_analytic_accounts(company_id, ReadFilters(), page=page)

        company = next(company for company in companies if company.id == company_id)
        if company.currency is None:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "The default company currency is unavailable.",
                "Check Odoo company access and retry.",
            )
        company_name = company.name
        _check_stage = "trial balance reconciliation"
        trial = await get_trial_balance(
            adapter,
            TrialBalanceInput(
                company_id=company_id,
                period_start=month.start,
                period_end=month.end,
                limit=500,
            ),
            company_name=company_name,
            request_id="req_live_qualification_trial",
        )
        if trial.summary.opening_balance != 0:
            _check_stage = "trial balance opening reconciliation"
        elif trial.summary.period_debit != trial.summary.period_credit:
            _check_stage = "trial balance period reconciliation"
        elif trial.summary.closing_balance != 0:
            _check_stage = "trial balance closing reconciliation"
        if _check_stage != "trial balance reconciliation":
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "The trial balance did not reconcile.",
                "Check the authorized accounting source data and retry.",
            )
        for payable in (False, True):
            _check_stage = (
                "payable aging reconciliation" if payable else "receivable aging reconciliation"
            )
            aging = await get_aged_balance(
                adapter,
                AgingInput(company_id=company_id, as_of_date=today, limit=500),
                company_name=company_name,
                request_id="req_live_qualification_aging",
                payable=payable,
            )
            bucket_total = sum(aging.summary.buckets.model_dump().values())
            if aging.summary.residual_total != bucket_total:
                raise OdooMcpError(
                    ErrorCode.ODOO_API_ERROR,
                    "The aging report did not reconcile.",
                    "Check the authorized accounting source data and retry.",
                )

        _check_stage = "profit and loss reconciliation"
        profit_and_loss = await get_profit_and_loss(
            adapter,
            ProfitAndLossInput(
                company_id=company_id,
                period_start=month.start,
                period_end=month.end,
                limit=500,
            ),
            company_currency_id=company.currency.id,
            company_name=company_name,
            request_id="req_live_qualification_profit_and_loss",
        )
        if (
            profit_and_loss.summary.net_profit
            != profit_and_loss.summary.income_balance - profit_and_loss.summary.expense_balance
        ):
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "The profit and loss report did not reconcile.",
                "Check the authorized accounting source data and retry.",
            )

        _check_stage = "balance sheet reconciliation"
        balance_sheet = await get_balance_sheet(
            adapter,
            BalanceSheetInput(company_id=company_id, as_of_date=today, limit=500),
            company_currency_id=company.currency.id,
            company_name=company_name,
            request_id="req_live_qualification_balance_sheet",
        )
        if not balance_sheet.summary.is_balanced:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "The balance sheet did not reconcile.",
                "Check the authorized accounting source data and retry.",
            )
    finally:
        await adapter.close()


def main() -> None:
    global _check_stage
    try:
        asyncio.run(_qualify())
    except OdooMcpError as exc:
        for field in (
            "id",
            "move_id",
            "account_id",
            "journal_id",
            "partner_id",
            "company_id",
            "currency_id",
            "date",
            "date_maturity",
            "name",
            "debit",
            "credit",
            "balance",
            "amount_currency",
            "amount_residual",
            "amount_residual_currency",
            "reconciled",
            "analytic_distribution",
        ):
            if exc.safe_message == f"Odoo returned an invalid account.move.line field: {field}.":
                _check_stage = f"account move-line field {field}"
                break
        print(
            f"Live Odoo 19 read-only qualification failed safely during {_check_stage}: "
            f"{exc.code.value}",
            file=sys.stderr,
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
