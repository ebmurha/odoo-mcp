"""Invoice, bill, credit-note, and payment workflows."""

from __future__ import annotations

import base64
import binascii
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from odoo_mcp.adapters.accounting import (
    AccountMove,
    AccountMoveLine,
    FilterClause,
    InvoiceDraft,
    InvoiceDraftLine,
    InvoiceEffect,
    PageRequest,
    PartialReconciliation,
    Partner,
    PaymentPreviewRequest,
    PaymentRegistration,
    ReadFilters,
)
from odoo_mcp.adapters.base import OdooAdapter
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import (
    CreateInvoiceInput,
    CreditNoteInput,
    CurrencyResidualTotal,
    OpenDocumentItem,
    OpenDocumentsInput,
    OpenDocumentsResponse,
    OpenDocumentsSummary,
    RegisterPaymentInput,
    ValidateInvoiceInput,
)
from odoo_mcp.policy.write_safety import AppliedWrite, PreparedWrite

DEFERRED_INVOICE_FIELDS = (
    "journal_id",
    "fiscal_position_id",
    "currency_id",
    "taxes",
    "total",
)
_PAGE_SIZE = 100
_MAX_RECORDS = 100_000


def _invalid(message: str, remediation: str) -> OdooMcpError:
    return OdooMcpError(ErrorCode.INVALID_INPUT, message, remediation)


async def _all_moves(
    adapter: OdooAdapter,
    company_id: int,
    filters: ReadFilters,
) -> list[AccountMove]:
    items: list[AccountMove] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        page = await adapter.get_account_moves(
            company_id, filters, PageRequest(limit=_PAGE_SIZE, cursor=cursor)
        )
        items.extend(page.items)
        if len(items) > _MAX_RECORDS:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "The invoice listing exceeds the safe processing bound.",
                "Narrow the requested date or partner selection.",
            )
        if page.next_cursor is None:
            return items
        if page.next_cursor in seen:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned an invalid invoice page.",
                "Check Odoo compatibility and retry.",
            )
        seen.add(page.next_cursor)
        cursor = page.next_cursor


async def _all_move_lines(
    adapter: OdooAdapter,
    company_id: int,
    filters: ReadFilters,
) -> list[AccountMoveLine]:
    items: list[AccountMoveLine] = []
    cursor: str | None = None
    while True:
        page = await adapter.get_account_move_lines(
            company_id, filters, PageRequest(limit=_PAGE_SIZE, cursor=cursor)
        )
        items.extend(page.items)
        if len(items) > _MAX_RECORDS:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "The invoice listing exceeds the safe processing bound.",
                "Narrow the requested date or partner selection.",
            )
        if page.next_cursor is None:
            return items
        if page.next_cursor == cursor:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned an invalid invoice-line page.",
                "Check Odoo compatibility and retry.",
            )
        cursor = page.next_cursor


async def _all_partials(
    adapter: OdooAdapter,
    company_id: int,
    filters: ReadFilters,
) -> list[PartialReconciliation]:
    items: list[PartialReconciliation] = []
    cursor: str | None = None
    while True:
        page = await adapter.get_partial_reconciliations(
            company_id, filters, PageRequest(limit=_PAGE_SIZE, cursor=cursor)
        )
        items.extend(page.items)
        if len(items) > _MAX_RECORDS:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "The invoice listing exceeds the safe processing bound.",
                "Narrow the requested date or partner selection.",
            )
        if page.next_cursor is None:
            return items
        if page.next_cursor == cursor:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned an invalid reconciliation page.",
                "Check Odoo compatibility and retry.",
            )
        cursor = page.next_cursor


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        version, raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode().split(":")
        offset = int(raw)
    except (ValueError, UnicodeError, binascii.Error):
        raise _invalid("The pagination cursor is invalid.", "Restart without a cursor.") from None
    if version != "v1" or offset <= 0:
        raise _invalid("The pagination cursor is invalid.", "Restart without a cursor.")
    return offset


def _next_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"v1:{offset}".encode()).decode().rstrip("=")


async def list_open_documents(
    adapter: OdooAdapter,
    request: OpenDocumentsInput,
    *,
    bills: bool,
    company_name: str,
    request_id: str,
) -> OpenDocumentsResponse:
    move_type = "in_invoice" if bills else "out_invoice"
    clauses = [
        FilterClause(field="state", operator="=", value="posted"),
        FilterClause(field="move_type", operator="=", value=move_type),
        FilterClause(field="invoice_date", operator="<=", value=request.as_of_date),
    ]
    if request.partner_ids:
        clauses.append(FilterClause(field="partner_id", operator="in", value=request.partner_ids))
    moves = await _all_moves(adapter, request.company_id, ReadFilters(clauses=tuple(clauses)))
    move_ids = tuple(move.id for move in moves)
    account_type = "liability_payable" if bills else "asset_receivable"
    lines = (
        await _all_move_lines(
            adapter,
            request.company_id,
            ReadFilters(
                clauses=(
                    FilterClause(field="move_id", operator="in", value=move_ids),
                    FilterClause(field="account_id.account_type", operator="=", value=account_type),
                )
            ),
        )
        if move_ids
        else []
    )
    residual_company = {line.id: line.residual for line in lines}
    residual_currency = {line.id: line.residual_currency for line in lines}
    partials = await _all_partials(
        adapter,
        request.company_id,
        ReadFilters(
            clauses=(FilterClause(field="max_date", operator=">", value=request.as_of_date),)
        ),
    )
    for partial in partials:
        if partial.debit_move_line.id in residual_company:
            residual_company[partial.debit_move_line.id] += partial.amount
            residual_currency[partial.debit_move_line.id] += partial.debit_amount_currency
        if partial.credit_move_line.id in residual_company:
            residual_company[partial.credit_move_line.id] -= partial.amount
            residual_currency[partial.credit_move_line.id] -= partial.credit_amount_currency
    company_by_move: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    currency_by_move: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    move_id_set = set(move_ids)
    for line in lines:
        if line.move.id not in move_id_set:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned an invoice line outside the requested documents.",
                "Check Odoo compatibility and retry.",
            )
        company_by_move[line.move.id] += residual_company[line.id]
        currency_by_move[line.move.id] += residual_currency[line.id]
    items = []
    for move in moves:
        if move.state != "posted" or move.move_type != move_type or move.invoice_date is None:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned inconsistent invoice data.",
                "Check Odoo compatibility and retry.",
            )
        if move.partner is None:
            raise OdooMcpError(
                ErrorCode.ODOO_API_ERROR,
                "Odoo returned an invoice without a partner.",
                "Correct the invoice and retry.",
            )
        company_residual = abs(company_by_move[move.id])
        record_residual = abs(currency_by_move[move.id])
        if company_residual == 0 and record_residual == 0:
            continue
        due = move.invoice_date_due
        overdue_days = max((request.as_of_date - due).days, 0) if due else 0
        if request.overdue_only and overdue_days == 0:
            continue
        items.append(
            OpenDocumentItem(
                move_id=move.id,
                partner_id=move.partner.id,
                partner_name=move.partner.name,
                number=move.name,
                invoice_date=move.invoice_date,
                due_date=due,
                currency_id=move.currency.id,
                currency_name=move.currency.name,
                amount_total=move.amount_total,
                residual_currency=record_residual,
                residual_company=company_residual,
                payment_state=(
                    "not_paid" if record_residual >= abs(move.amount_total) else "partial"
                ),
                overdue_days=overdue_days,
            )
        )
    items.sort(key=lambda item: (item.due_date or date.max, item.move_id))
    totals: dict[tuple[int, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for item in items:
        totals[(item.currency_id, item.currency_name)] += item.residual_currency
    summary = OpenDocumentsSummary(
        document_count=len(items),
        currency_totals=[
            CurrencyResidualTotal(currency_id=key[0], currency_name=key[1], residual=value)
            for key, value in sorted(totals.items())
        ],
        company_currency_residual=sum((item.residual_company for item in items), Decimal("0")),
    )
    offset = _cursor_offset(request.cursor)
    if offset > len(items):
        raise _invalid("The pagination cursor is outside this report.", "Restart without a cursor.")
    selected = items[offset : offset + request.limit]
    next_offset = offset + len(selected)
    cursor = _next_cursor(next_offset) if next_offset < len(items) else None
    title = "Open Supplier Bills" if bills else "Open Customer Invoices"
    rows = [
        f"# {title}",
        "",
        f"- Company: {company_name} ({request.company_id})",
        f"- As of: {request.as_of_date.isoformat()}",
        f"- Audit reference: `{request_id}`",
        "",
        "| ID | Number | Partner | Due | Residual | Currency | Overdue days |",
        "|---:|---|---|---|---:|---|---:|",
    ]
    rows.extend(
        f"| {item.move_id} | {item.number} | {item.partner_name} | "
        f"{item.due_date or ''} | {item.residual_currency} | {item.currency_name} | "
        f"{item.overdue_days} |"
        for item in selected
    )
    rows.extend(["", f"Whole-report documents: {summary.document_count}."])
    return OpenDocumentsResponse(
        request_id=request_id,
        company_id=request.company_id,
        as_of_date=request.as_of_date,
        items=selected,
        next_cursor=cursor,
        summary=summary,
        artifact_markdown="\n".join(rows),
    )


async def _one_partner(
    adapter: OdooAdapter, company_id: int, partner_id: int, *, supplier: bool
) -> Partner:
    page = await adapter.get_partners(
        company_id,
        ReadFilters(clauses=(FilterClause(field="id", operator="=", value=partner_id),)),
        page=PageRequest(limit=2),
    )
    if len(page.items) != 1:
        raise _invalid("The partner is unavailable.", "Use an exact authorized partner ID.")
    partner = page.items[0]
    if (supplier and partner.supplier_rank <= 0) or (not supplier and partner.customer_rank <= 0):
        raise _invalid(
            "The partner does not have the required accounting role.",
            "Use a customer for invoices or supplier for bills.",
        )
    return partner


async def _validate_draft_references(
    adapter: OdooAdapter,
    request: CreateInvoiceInput,
    *,
    supplier: bool,
) -> InvoiceDraft:
    await _one_partner(adapter, request.company_id, request.partner_id, supplier=supplier)
    product_ids = tuple(sorted({line.product_id for line in request.lines if line.product_id}))
    account_ids = tuple(sorted({line.account_id for line in request.lines}))
    analytic_ids = tuple(
        sorted(
            {
                identifier
                for identifier in (
                    request.analytic_account_id,
                    *(line.analytic_account_id for line in request.lines),
                )
                if identifier is not None
            }
        )
    )
    if product_ids:
        products = await adapter.get_products(
            request.company_id,
            ReadFilters(clauses=(FilterClause(field="id", operator="in", value=product_ids),)),
            page=PageRequest(limit=len(product_ids)),
        )
        if {item.id for item in products.items} != set(product_ids):
            raise _invalid("A product is unavailable.", "Use exact authorized product IDs.")
    accounts = await adapter.get_account_accounts(
        request.company_id,
        ReadFilters(clauses=(FilterClause(field="id", operator="in", value=account_ids),)),
        page=PageRequest(limit=len(account_ids)),
    )
    expected_prefix = "expense" if supplier else "income"
    if {item.id for item in accounts.items} != set(account_ids) or any(
        not item.account_type.startswith(expected_prefix) for item in accounts.items
    ):
        raise _invalid("An invoice account is invalid.", "Use an applicable exact account ID.")
    if analytic_ids:
        analytics = await adapter.get_analytic_accounts(
            request.company_id,
            ReadFilters(clauses=(FilterClause(field="id", operator="in", value=analytic_ids),)),
            page=PageRequest(limit=len(analytic_ids)),
        )
        if {item.id for item in analytics.items} != set(analytic_ids):
            raise _invalid("An analytic account is unavailable.", "Use an exact analytic ID.")
    if request.currency_id is not None:
        currencies = await adapter.get_currencies(
            request.company_id, (request.currency_id,), page=PageRequest(limit=1)
        )
        if len(currencies.items) != 1:
            raise _invalid("The currency is unavailable.", "Use an exact currency ID.")
    if request.payment_term_id is not None:
        terms = await adapter.get_payment_terms(request.company_id, page=PageRequest(limit=500))
        if request.payment_term_id not in {item.id for item in terms.items}:
            raise _invalid("The payment term is unavailable.", "Use an exact payment-term ID.")
    return InvoiceDraft(
        company_id=request.company_id,
        move_type="in_invoice" if supplier else "out_invoice",
        partner_id=request.partner_id,
        invoice_date=request.invoice_date,
        lines=tuple(
            InvoiceDraftLine(
                description=line.description.strip(),
                quantity=line.quantity,
                unit_price=line.unit_price,
                account_id=line.account_id,
                product_id=line.product_id,
                analytic_account_id=line.analytic_account_id,
            )
            for line in request.lines
        ),
        currency_id=request.currency_id,
        payment_term_id=request.payment_term_id,
        analytic_account_id=request.analytic_account_id,
        vendor_reference=(request.vendor_reference.strip() if request.vendor_reference else None),
    )


def _effect(effect: InvoiceEffect) -> dict[str, object]:
    return effect.model_dump(mode="json", exclude={"validation_lines"})


async def prepare_invoice_draft(
    adapter: OdooAdapter,
    request: CreateInvoiceInput,
    *,
    supplier: bool,
) -> tuple[InvoiceDraft, PreparedWrite]:
    draft = await _validate_draft_references(adapter, request, supplier=supplier)
    subtotals = [line.quantity * line.unit_price for line in draft.lines]
    proposed = draft.model_dump(mode="json", exclude_none=True)
    known: dict[str, object] = {
        "calculation_status": "deferred_to_execution",
        "proposed_draft": proposed,
        "input_subtotals": [str(value) for value in subtotals],
        "input_subtotal_total": str(sum(subtotals, Decimal("0"))),
    }
    if draft.currency_id is not None:
        known["requested_currency_id"] = draft.currency_id
    material = {"known_effects": known, "deferred_fields": list(DEFERRED_INVOICE_FIELDS)}
    kind = "supplier_bill" if supplier else "customer_invoice"
    artifact = "\n".join(
        [
            f"# Proposed {kind.replace('_', ' ').title()}",
            "",
            "Input subtotal only; Odoo calculation is deferred to explicit draft creation.",
            f"Input subtotal total: {known['input_subtotal_total']}",
            f"Deferred fields: {', '.join(DEFERRED_INVOICE_FIELDS)}",
        ]
    )
    return draft, PreparedWrite(
        proposed_action={"action": f"create_draft_{kind}", "posts_document": False},
        material_effects=material,
        artifact_markdown=artifact,
    )


async def execute_invoice_draft(adapter: OdooAdapter, draft: InvoiceDraft) -> AppliedWrite:
    effect = await adapter.create_draft_invoice(draft)
    return AppliedWrite(
        material_effects=_effect(effect),
        record_refs=(f"account.move:{effect.id}",),
    )


async def prepare_validation(
    adapter: OdooAdapter, request: ValidateInvoiceInput
) -> tuple[InvoiceEffect, PreparedWrite]:
    effect = await adapter.get_invoice_effect(request.company_id, request.invoice_id)
    if effect.state != "draft" or effect.move_type not in {
        "out_invoice",
        "in_invoice",
        "out_refund",
        "in_refund",
    }:
        raise OdooMcpError(
            ErrorCode.ODOO_STATE_CONFLICT,
            "The document is not an eligible draft invoice, bill, or credit note.",
            "Refresh the document and submit an eligible draft.",
        )
    companies = await adapter.get_companies()
    company = next((item for item in companies if item.id == request.company_id), None)
    if company is None or company.currency is None:
        raise OdooMcpError(
            ErrorCode.ODOO_API_ERROR,
            "Odoo returned incomplete company currency data.",
            "Correct the authorized company currency and retry.",
        )
    currency_ids = tuple(dict.fromkeys((company.currency.id, effect.currency.id)))
    currencies = await adapter.get_currencies(
        request.company_id, currency_ids, page=PageRequest(limit=len(currency_ids))
    )
    currency_by_id = {item.id: item for item in currencies.items}
    if set(currency_by_id) != set(currency_ids):
        raise OdooMcpError(
            ErrorCode.ODOO_API_ERROR,
            "Odoo returned incomplete currency precision data.",
            "Correct the configured currencies and retry.",
        )

    def rounded(value: Decimal, currency_id: int) -> Decimal:
        increment = currency_by_id[currency_id].rounding
        return (value / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * increment

    findings: list[str] = []
    postable = [
        line
        for line in effect.validation_lines
        if line.display_type not in {"line_section", "line_note"}
    ]
    if not postable:
        findings.append("NO_POSTABLE_LINES")
    if any(line.account is None for line in postable):
        findings.append("LINE_ACCOUNT_MISSING")
    debit = sum((line.debit for line in postable), Decimal("0"))
    credit = sum((line.credit for line in postable), Decimal("0"))
    if (
        rounded(debit, company.currency.id) != rounded(credit, company.currency.id)
        or rounded(sum((line.balance for line in postable), Decimal("0")), company.currency.id) != 0
        or any(
            rounded(line.debit - line.credit, company.currency.id)
            != rounded(line.balance, company.currency.id)
            for line in postable
        )
    ):
        findings.append("ENTRY_UNBALANCED")
    if any(
        line.amount_currency != 0
        and (line.currency is None or line.currency.id != effect.currency.id)
        for line in postable
    ):
        findings.append("CURRENCY_INCONSISTENT")
    product_lines = [
        line for line in effect.validation_lines if line.display_type in {None, "product"}
    ]
    subtotal = sum((abs(line.subtotal) for line in product_lines), Decimal("0"))
    total = sum((abs(line.total) for line in product_lines), Decimal("0"))
    if rounded(subtotal, effect.currency.id) != rounded(
        abs(effect.amount_untaxed), effect.currency.id
    ) or rounded(total, effect.currency.id) != rounded(
        abs(effect.amount_total), effect.currency.id
    ):
        findings.append("TOTAL_ARITHMETIC_MISMATCH")
    if rounded(abs(effect.amount_untaxed) + abs(effect.amount_tax), effect.currency.id) != rounded(
        abs(effect.amount_total), effect.currency.id
    ):
        findings.append("TAX_ARITHMETIC_MISMATCH")
    return effect, PreparedWrite(
        proposed_action={"action": "post_existing_draft", "invoice_id": effect.id},
        material_effects={
            "invoice": _effect(effect),
            "blocking_validation_findings": findings,
            "validation_status": ("blocked" if findings else "locally_clear_with_deferred_checks"),
            "deferred_checks": ["odoo_posting_constraints"],
        },
        needs_input=bool(findings),
    )


async def prepare_credit_note(
    adapter: OdooAdapter, request: CreditNoteInput
) -> tuple[InvoiceEffect, PreparedWrite]:
    effect = await adapter.get_invoice_effect(request.company_id, request.original_move_id)
    if effect.state != "posted" or effect.move_type not in {"out_invoice", "in_invoice"}:
        raise OdooMcpError(
            ErrorCode.ODOO_STATE_CONFLICT,
            "The original document is not an eligible posted invoice or bill.",
            "Use one posted customer invoice or supplier bill.",
        )
    return effect, PreparedWrite(
        proposed_action={
            "action": "create_linked_draft_credit_note",
            "original_move_id": effect.id,
            "posts_document": False,
        },
        material_effects={"original": _effect(effect), "reason": request.reason.strip()},
    )


async def prepare_payment(
    adapter: OdooAdapter, request: RegisterPaymentInput
) -> tuple[InvoiceEffect, PaymentRegistration | None, PreparedWrite]:
    effect = await adapter.get_invoice_effect(request.company_id, request.invoice_id)
    if effect.state != "posted" or effect.move_type not in {"out_invoice", "in_invoice"}:
        raise OdooMcpError(
            ErrorCode.ODOO_STATE_CONFLICT,
            "The document is not an eligible posted invoice or bill.",
            "Use a posted invoice or bill with a residual amount.",
        )
    amount = request.amount or effect.amount_residual
    if amount <= 0 or amount > effect.amount_residual:
        raise _invalid("The payment amount is invalid.", "Use a positive amount within residual.")
    preview = await adapter.get_payment_registration_preview(
        PaymentPreviewRequest(
            invoice_id=request.invoice_id,
            company_id=request.company_id,
            payment_date=request.payment_date,
            amount=amount,
        )
    )
    routes = preview.routes
    selected = [
        route
        for route in routes
        if request.journal_id in {None, route.journal.id}
        and request.payment_method_line_id in {None, route.payment_method_line.id}
    ]
    default_route = next(
        (
            route
            for route in routes
            if route.journal.id == preview.default_journal_id
            and route.payment_method_line.id == preview.default_payment_method_line_id
        ),
        None,
    )
    choices = [route.model_dump(mode="json") for route in routes]
    if len(selected) != 1:
        return (
            effect,
            None,
            PreparedWrite(
                proposed_action={"action": "register_payment", "invoice_id": effect.id},
                material_effects={
                    "reason_code": "PAYMENT_ROUTE_SELECTION_REQUIRED",
                    "valid_choices": choices,
                    "preparation_state": "odoo_transient_created",
                    "amount": str(preview.amount),
                    "currency": preview.currency.model_dump(),
                    "default_journal": (
                        default_route.journal.model_dump(mode="json") if default_route else None
                    ),
                    "default_payment_method_line": (
                        default_route.payment_method_line.model_dump(mode="json")
                        if default_route
                        else None
                    ),
                    "expected_invoice_state": "unknown_until_execution",
                    "external_effect_status": (
                        default_route.external_effect_status if default_route else "unknown"
                    ),
                },
                needs_input=True,
            ),
        )
    route = selected[0]
    registration = PaymentRegistration(
        invoice_id=effect.id,
        company_id=request.company_id,
        payment_date=request.payment_date,
        amount=preview.amount,
        journal_id=route.journal.id,
        payment_method_line_id=route.payment_method_line.id,
        payment_method_code=route.payment_method_code,
        external_effect_status=route.external_effect_status,
    )
    return (
        effect,
        registration,
        PreparedWrite(
            proposed_action={"action": "register_payment", "invoice_id": effect.id},
            material_effects={
                "amount": str(preview.amount),
                "currency": preview.currency.model_dump(),
                "selected_route": route.model_dump(mode="json"),
                "preparation_state": "odoo_transient_created",
                "expected_invoice_state": "unknown_until_execution",
                "external_effect_status": route.external_effect_status,
            },
        ),
    )
