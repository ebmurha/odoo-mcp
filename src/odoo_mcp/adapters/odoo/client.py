"""Concrete Odoo adapter composed from a detected version transport."""

from __future__ import annotations

from datetime import date
from typing import cast

import httpx

from odoo_mcp.adapters.accounting import (
    DEFAULT_PAGE_REQUEST,
    Account,
    AccountMove,
    AccountMoveLine,
    AnalyticAccount,
    BankStatementLine,
    Currency,
    CurrencyRatePage,
    DatePeriod,
    InvoiceDraft,
    InvoiceEffect,
    Journal,
    JournalEntry,
    JournalEntryDraft,
    PageRequest,
    PartialReconciliation,
    Partner,
    PaymentPreviewRequest,
    PaymentRegistration,
    PaymentRegistrationPreview,
    PaymentTerm,
    Product,
    ReadFilters,
    RecordPage,
    RelatedRecord,
)
from odoo_mcp.adapters.base import CapabilitySnapshot, Company
from odoo_mcp.adapters.odoo.accounting import AccountingReader
from odoo_mcp.adapters.odoo.capabilities import detect_capabilities
from odoo_mcp.adapters.odoo.payroll import PayrollReader
from odoo_mcp.adapters.odoo.policy import ensure_model_read_allowed
from odoo_mcp.adapters.odoo.transports.base import OdooTransport
from odoo_mcp.adapters.odoo.transports.json2 import Json2Transport
from odoo_mcp.adapters.odoo.transports.json_rpc import JsonRpcTransport
from odoo_mcp.adapters.odoo.versioning import detect_major_version
from odoo_mcp.adapters.payroll import (
    DEFAULT_PAYROLL_PAGE_REQUEST,
    DeletedPayslipInput,
    DraftPayslipInputCreate,
    DraftPayslipInputUpdate,
    PayrollBatch,
    PayrollBatchFilters,
    PayrollContractFilters,
    PayrollContractSegment,
    PayrollEmployee,
    PayrollInputType,
    PayrollInputTypeFilters,
    PayrollPage,
    PayrollPageRequest,
    PayrollStructure,
    PayrollWorkEntry,
    PayrollWorkEntryFilters,
    PayrollWriteCheckpoint,
    Payslip,
    PayslipChildFilters,
    PayslipFilters,
    PayslipInput,
    PayslipLine,
    PayslipWorkedDay,
)
from odoo_mcp.app.settings import OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError


class OdooClient:
    """The only component that selects and calls an Odoo API transport."""

    def __init__(
        self,
        connection: OdooConnectionSettings,
        version: int,
        transport: OdooTransport,
    ) -> None:
        self._connection = connection
        self._version = version
        self._transport = transport
        self._validated_company_ids: tuple[int, ...] | None = None
        self._accounting = AccountingReader(
            transport,
            lambda: self._validated_company_ids,
        )
        self._payroll = PayrollReader(
            transport,
            version,
            lambda: self._validated_company_ids,
        )

    @classmethod
    async def connect(
        cls,
        connection: OdooConnectionSettings,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> OdooClient:
        base_url = str(connection.url).rstrip("/")
        client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
            transport=http_transport,
        )
        try:
            version = await detect_major_version(connection, client)
            selected: OdooTransport
            if version == 18:
                selected = JsonRpcTransport(connection, client)
            else:
                selected = Json2Transport(connection, client)
            await selected.authenticate()
        except Exception:
            await client.aclose()
            raise
        return cls(connection, version, selected)

    async def get_capabilities(self) -> CapabilitySnapshot:
        if self._validated_company_ids is None:
            raise OdooMcpError(
                ErrorCode.ODOO_AUTH_FAILED,
                "Allowed companies must be validated before capability discovery.",
                "Validate the configured company access and retry.",
            )
        modules = await detect_capabilities(
            self._transport,
            company_ids=self._validated_company_ids,
        )
        if not modules.get("base"):
            raise OdooMcpError(
                ErrorCode.ODOO_AUTH_FAILED,
                "The technical user cannot access authorized companies.",
                "Grant least-privilege access to the allowed companies and retry.",
            )
        return CapabilitySnapshot(
            edition="enterprise",
            version=self._version,
            transport=self._transport.name,
            modules=modules,
        )

    async def get_companies(self) -> list[Company]:
        self._validated_company_ids = None
        allowed = self._connection.allowed_company_ids
        ensure_model_read_allowed("res.company", module=None)
        rows = await self._transport.search_read(
            "res.company",
            [["id", "in", list(allowed)]],
            ["id", "name", "currency_id", "root_id"],
            limit=len(allowed),
            offset=0,
            order="id asc",
            company_ids=allowed,
        )
        companies: list[Company] = []
        for row in rows:
            identifier = row.get("id")
            name = row.get("name")
            raw_currency = row.get("currency_id")
            raw_root = row.get("root_id")
            currency = (
                RelatedRecord(id=raw_currency[0], name=raw_currency[1])
                if isinstance(raw_currency, (list, tuple))
                and len(raw_currency) == 2
                and isinstance(raw_currency[0], int)
                and not isinstance(raw_currency[0], bool)
                and raw_currency[0] > 0
                and isinstance(raw_currency[1], str)
                else None
            )
            if (
                isinstance(identifier, int)
                and not isinstance(identifier, bool)
                and isinstance(name, str)
            ):
                root_id = (
                    raw_root[0]
                    if isinstance(raw_root, (list, tuple))
                    and len(raw_root) == 2
                    and isinstance(raw_root[0], int)
                    and not isinstance(raw_root[0], bool)
                    and raw_root[0] > 0
                    else None
                )
                companies.append(
                    Company(id=identifier, name=name, currency=currency, root_id=root_id)
                )
        found = {company.id for company in companies}
        if found != set(allowed):
            raise OdooMcpError(
                ErrorCode.COMPANY_NOT_FOUND,
                "One or more allowed companies are unavailable to the technical user.",
                "Check allowed company IDs and Odoo company access, then retry.",
            )
        self._validated_company_ids = allowed
        return sorted(companies, key=lambda company: company.id)

    async def get_account_moves(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[AccountMove]:
        return await self._accounting.get_account_moves(company_id, filters, page)

    async def get_account_move_lines(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[AccountMoveLine]:
        return await self._accounting.get_account_move_lines(company_id, filters, page)

    async def get_partial_reconciliations(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[PartialReconciliation]:
        return await self._accounting.get_partial_reconciliations(company_id, filters, page)

    async def get_journals(
        self, company_id: int, *, page: PageRequest = DEFAULT_PAGE_REQUEST
    ) -> RecordPage[Journal]:
        return await self._accounting.get_journals(company_id, page=page)

    async def get_currencies(
        self,
        company_id: int,
        currency_ids: tuple[int, ...],
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[Currency]:
        return await self._accounting.get_currencies(
            company_id,
            currency_ids,
            page=page,
        )

    async def get_currency_rates(
        self,
        company_id: int,
        currency_id: int,
        through_date: date,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> CurrencyRatePage:
        return await self._accounting.get_currency_rates(
            company_id, currency_id, through_date, page=page
        )

    async def get_bank_statement_lines(
        self,
        company_id: int,
        period: DatePeriod,
        journal_id: int | None,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[BankStatementLine]:
        return await self._accounting.get_bank_statement_lines(
            company_id,
            period,
            journal_id,
            page=page,
        )

    async def get_payment_terms(
        self, company_id: int, *, page: PageRequest = DEFAULT_PAGE_REQUEST
    ) -> RecordPage[PaymentTerm]:
        return await self._accounting.get_payment_terms(company_id, page=page)

    async def get_partners(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[Partner]:
        return await self._accounting.get_partners(company_id, filters, page=page)

    async def get_products(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[Product]:
        return await self._accounting.get_products(company_id, filters, page=page)

    async def get_account_accounts(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[Account]:
        return await self._accounting.get_account_accounts(company_id, filters, page=page)

    async def get_analytic_accounts(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[AnalyticAccount]:
        return await self._accounting.get_analytic_accounts(company_id, filters, page=page)

    async def get_invoice_effect(self, company_id: int, move_id: int) -> InvoiceEffect:
        return await self._accounting.get_invoice_effect(company_id, move_id)

    async def create_draft_invoice(self, draft: InvoiceDraft) -> InvoiceEffect:
        return await self._accounting.create_draft_invoice(draft)

    async def get_journal_entry(self, company_id: int, move_id: int) -> JournalEntry:
        return await self._accounting.get_journal_entry(company_id, move_id)

    async def create_journal_entry_draft(self, draft: JournalEntryDraft) -> JournalEntry:
        return await self._accounting.create_journal_entry_draft(draft)

    async def post_journal_entry(self, company_id: int, move_id: int) -> JournalEntry:
        return await self._accounting.post_journal_entry(company_id, move_id)

    async def post_invoice(self, company_id: int, move_id: int) -> InvoiceEffect:
        return await self._accounting.post_invoice(company_id, move_id)

    async def create_credit_note(
        self,
        company_id: int,
        original_move_id: int,
        credit_date: date,
        reason: str,
    ) -> InvoiceEffect:
        return await self._accounting.create_credit_note(
            company_id, original_move_id, credit_date, reason
        )

    async def get_payment_registration_preview(
        self, request: PaymentPreviewRequest
    ) -> PaymentRegistrationPreview:
        return await self._accounting.get_payment_registration_preview(request)

    async def register_payment(self, registration: PaymentRegistration) -> InvoiceEffect:
        return await self._accounting.register_payment(registration)

    async def get_payroll_batches(
        self,
        company_id: int,
        filters: PayrollBatchFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollBatch]:
        return await self._payroll.get_payroll_batches(company_id, filters, page)

    async def get_payslips(
        self,
        company_id: int,
        filters: PayslipFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[Payslip]:
        return await self._payroll.get_payslips(company_id, filters, page)

    async def get_payslip_lines(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayslipLine]:
        return await self._payroll.get_payslip_lines(company_id, filters, page)

    async def get_payslip_worked_days(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayslipWorkedDay]:
        return await self._payroll.get_payslip_worked_days(company_id, filters, page)

    async def get_payslip_inputs(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayslipInput]:
        return await self._payroll.get_payslip_inputs(company_id, filters, page)

    async def get_payroll_input_types(
        self,
        company_id: int,
        filters: PayrollInputTypeFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollInputType]:
        return await self._payroll.get_payroll_input_types(company_id, filters, page)

    async def get_payroll_structure(
        self,
        company_id: int,
        structure_id: int,
    ) -> PayrollStructure:
        return await self._payroll.get_payroll_structure(company_id, structure_id)

    async def get_payroll_employees(
        self,
        company_id: int,
        employee_ids: tuple[int, ...],
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollEmployee]:
        return await self._payroll.get_payroll_employees(company_id, employee_ids, page)

    async def get_payroll_contract_segments(
        self,
        company_id: int,
        filters: PayrollContractFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollContractSegment]:
        return await self._payroll.get_payroll_contract_segments(company_id, filters, page)

    async def get_payroll_work_entries(
        self,
        company_id: int,
        filters: PayrollWorkEntryFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollWorkEntry]:
        return await self._payroll.get_payroll_work_entries(company_id, filters, page)

    async def create_draft_payslip_input(
        self,
        company_id: int,
        payload: DraftPayslipInputCreate,
        expected: PayrollWriteCheckpoint,
    ) -> PayslipInput:
        return await self._payroll.create_draft_payslip_input(company_id, payload, expected)

    async def update_draft_payslip_input(
        self,
        company_id: int,
        input_id: int,
        payload: DraftPayslipInputUpdate,
        expected: PayrollWriteCheckpoint,
    ) -> PayslipInput:
        return await self._payroll.update_draft_payslip_input(
            company_id, input_id, payload, expected
        )

    async def delete_draft_payslip_input(
        self,
        company_id: int,
        payslip_id: int,
        input_id: int,
        expected: PayrollWriteCheckpoint,
    ) -> DeletedPayslipInput:
        return await self._payroll.delete_draft_payslip_input(
            company_id, payslip_id, input_id, expected
        )

    async def recompute_draft_payslip(
        self,
        company_id: int,
        payslip_id: int,
        expected: PayrollWriteCheckpoint,
    ) -> Payslip:
        return await self._payroll.recompute_draft_payslip(company_id, payslip_id, expected)

    async def verify_payroll_schema(self, company_id: int) -> None:
        """Run the fixed, read-only Payroll schema qualification."""

        await self._payroll.verify_schema(company_id)

    async def close(self) -> None:
        await self._transport.close()


def as_odoo_client(adapter: object) -> OdooClient:
    """Narrow helper used only by lifecycle-aware routing."""

    return cast(OdooClient, adapter)
