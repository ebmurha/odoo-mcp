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


class Company(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    name: str
    currency: RelatedRecord | None = None


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
