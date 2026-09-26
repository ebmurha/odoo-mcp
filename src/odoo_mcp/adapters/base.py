"""Stable typed interface consumed by workflows."""

from __future__ import annotations

from datetime import date as Date
from typing import Protocol

from pydantic import BaseModel, ConfigDict

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


class Company(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    name: str
    currency: RelatedRecord | None = None
    root_id: int | None = None


class CapabilitySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    edition: str
    version: int
    transport: str
    modules: dict[str, bool]


class OdooAdapter(Protocol):
    """Odoo-only adapter protocol; transport details stay in its implementation."""

    async def get_capabilities(self) -> CapabilitySnapshot: ...

    async def get_companies(self) -> list[Company]: ...

    async def get_account_moves(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[AccountMove]: ...

    async def get_account_move_lines(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[AccountMoveLine]: ...

    async def get_partial_reconciliations(
        self, company_id: int, filters: ReadFilters, page: PageRequest
    ) -> RecordPage[PartialReconciliation]: ...

    async def get_journals(
        self, company_id: int, *, page: PageRequest = DEFAULT_PAGE_REQUEST
    ) -> RecordPage[Journal]: ...

    async def get_currencies(
        self,
        company_id: int,
        currency_ids: tuple[int, ...],
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[Currency]: ...

    async def get_currency_rates(
        self,
        company_id: int,
        currency_id: int,
        through_date: Date,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> CurrencyRatePage: ...

    async def get_bank_statement_lines(
        self,
        company_id: int,
        period: DatePeriod,
        journal_id: int | None,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[BankStatementLine]: ...

    async def get_payment_terms(
        self, company_id: int, *, page: PageRequest = DEFAULT_PAGE_REQUEST
    ) -> RecordPage[PaymentTerm]: ...

    async def get_partners(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[Partner]: ...

    async def get_products(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[Product]: ...

    async def get_account_accounts(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[Account]: ...

    async def get_analytic_accounts(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[AnalyticAccount]: ...

    async def get_invoice_effect(self, company_id: int, move_id: int) -> InvoiceEffect: ...

    async def create_draft_invoice(self, draft: InvoiceDraft) -> InvoiceEffect: ...

    async def get_journal_entry(self, company_id: int, move_id: int) -> JournalEntry: ...

    async def create_journal_entry_draft(self, draft: JournalEntryDraft) -> JournalEntry: ...

    async def post_journal_entry(self, company_id: int, move_id: int) -> JournalEntry: ...

    async def post_invoice(self, company_id: int, move_id: int) -> InvoiceEffect: ...

    async def create_credit_note(
        self,
        company_id: int,
        original_move_id: int,
        credit_date: Date,
        reason: str,
    ) -> InvoiceEffect: ...

    async def get_payment_registration_preview(
        self, request: PaymentPreviewRequest
    ) -> PaymentRegistrationPreview: ...

    async def register_payment(self, registration: PaymentRegistration) -> InvoiceEffect: ...

    async def get_payroll_batches(
        self,
        company_id: int,
        filters: PayrollBatchFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollBatch]: ...

    async def get_payslips(
        self,
        company_id: int,
        filters: PayslipFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[Payslip]: ...

    async def get_payslip_lines(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayslipLine]: ...

    async def get_payslip_worked_days(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayslipWorkedDay]: ...

    async def get_payslip_inputs(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayslipInput]: ...

    async def get_payroll_input_types(
        self,
        company_id: int,
        filters: PayrollInputTypeFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollInputType]: ...

    async def get_payroll_structure(
        self,
        company_id: int,
        structure_id: int,
    ) -> PayrollStructure: ...

    async def get_payroll_employees(
        self,
        company_id: int,
        employee_ids: tuple[int, ...],
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollEmployee]: ...

    async def get_payroll_contract_segments(
        self,
        company_id: int,
        filters: PayrollContractFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollContractSegment]: ...

    async def get_payroll_work_entries(
        self,
        company_id: int,
        filters: PayrollWorkEntryFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollWorkEntry]: ...

    async def create_draft_payslip_input(
        self,
        company_id: int,
        payload: DraftPayslipInputCreate,
        expected: PayrollWriteCheckpoint,
    ) -> PayslipInput: ...

    async def update_draft_payslip_input(
        self,
        company_id: int,
        input_id: int,
        payload: DraftPayslipInputUpdate,
        expected: PayrollWriteCheckpoint,
    ) -> PayslipInput: ...

    async def delete_draft_payslip_input(
        self,
        company_id: int,
        payslip_id: int,
        input_id: int,
        expected: PayrollWriteCheckpoint,
    ) -> DeletedPayslipInput: ...

    async def recompute_draft_payslip(
        self,
        company_id: int,
        payslip_id: int,
        expected: PayrollWriteCheckpoint,
    ) -> Payslip: ...
