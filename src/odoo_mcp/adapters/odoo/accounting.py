"""Typed, bounded accounting reads over the internal Odoo transport."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeAlias, TypeVar, cast

from odoo_mcp.adapters.accounting import (
    DEFAULT_PAGE_REQUEST,
    Account,
    AccountMove,
    AccountMoveLine,
    AdapterValue,
    AnalyticAccount,
    BankStatementLine,
    Currency,
    DatePeriod,
    FilterClause,
    InvoiceDraft,
    InvoiceEffect,
    InvoiceLineEffect,
    InvoiceTax,
    InvoiceValidationLine,
    Journal,
    JournalEntry,
    JournalEntryDraft,
    JournalEntryLine,
    PageRequest,
    PartialReconciliation,
    Partner,
    PaymentPreviewRequest,
    PaymentRegistration,
    PaymentRegistrationPreview,
    PaymentRoute,
    PaymentScheduleLine,
    PaymentTerm,
    Product,
    ReadFilters,
    RecordPage,
    RelatedRecord,
)
from odoo_mcp.adapters.odoo.policy import (
    FIELD_DENYLIST,
    ensure_accounting_action_allowed,
    ensure_model_read_allowed,
    strip_denied_fields,
)
from odoo_mcp.adapters.odoo.transports.base import OdooTransport
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError

RawRecord: TypeAlias = Mapping[str, object]
RecordT = TypeVar("RecordT", bound=AdapterValue)
ParsedT = TypeVar("ParsedT")
RowValidator = Callable[[list[RawRecord], int], Awaitable[None]]
_MAX_CURRENCY_IDS = 500

_ACCOUNT_MOVE_FIELDS = [
    "id",
    "name",
    "move_type",
    "state",
    "date",
    "invoice_date",
    "invoice_date_due",
    "partner_id",
    "journal_id",
    "company_id",
    "currency_id",
    "amount_total",
    "amount_residual",
    "amount_residual_signed",
    "payment_state",
    "ref",
]
_ACCOUNT_MOVE_LINE_FIELDS = [
    "id",
    "move_id",
    "parent_state",
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
]
_PARTIAL_RECONCILIATION_FIELDS = [
    "id",
    "debit_move_id",
    "credit_move_id",
    "amount",
    "debit_amount_currency",
    "credit_amount_currency",
    "max_date",
]
_JOURNAL_FIELDS = ["id", "name", "code", "type", "company_id", "currency_id"]
_CURRENCY_FIELDS = ["id", "name", "rounding"]
_BANK_STATEMENT_LINE_FIELDS = [
    "id",
    "date",
    "payment_ref",
    "amount",
    "amount_currency",
    "foreign_currency_id",
    "partner_id",
    "journal_id",
    "company_id",
    "is_reconciled",
    "move_id",
]
_PAYMENT_TERM_FIELDS = ["id", "name", "company_id"]
_PARTNER_FIELDS = ["id", "name", "company_id", "customer_rank", "supplier_rank"]
_PRODUCT_FIELDS = ["id", "name", "default_code", "company_id", "list_price", "currency_id"]
_ACCOUNT_FIELDS = [
    "id",
    "code",
    "name",
    "account_type",
    "company_ids",
    "currency_id",
    "reconcile",
]
_ANALYTIC_ACCOUNT_FIELDS = ["id", "name", "code", "company_id", "currency_id"]
_INVOICE_EFFECT_FIELDS = [
    "id",
    "name",
    "move_type",
    "state",
    "company_id",
    "partner_id",
    "invoice_date",
    "invoice_date_due",
    "journal_id",
    "fiscal_position_id",
    "currency_id",
    "invoice_payment_term_id",
    "amount_untaxed",
    "amount_tax",
    "amount_total",
    "amount_residual",
    "payment_state",
]
_INVOICE_EFFECT_LINE_FIELDS = [
    "id",
    "move_id",
    "company_id",
    "display_type",
    "name",
    "quantity",
    "price_unit",
    "price_subtotal",
    "price_total",
    "tax_line_id",
    "account_id",
    "debit",
    "credit",
    "balance",
    "currency_id",
    "amount_currency",
    "analytic_distribution",
    "date_maturity",
    "amount_residual",
]

_PAYMENT_PREVIEW_FIELDS = [
    "available_journal_ids",
    "journal_id",
    "available_payment_method_line_ids",
    "payment_method_line_id",
    "payment_method_code",
    "amount",
    "currency_id",
    "payment_date",
    "payment_type",
    "partner_type",
    "company_id",
    "can_edit_wizard",
]
_MAX_PAYMENT_JOURNALS = 50
_MAX_PAYMENT_METHODS = 100
_MAX_PAYMENT_TRANSIENTS = 200

_FILTER_FIELDS: dict[str, frozenset[str]] = {
    "account.move": frozenset(
        {
            "id",
            "move_type",
            "state",
            "date",
            "invoice_date",
            "invoice_date_due",
            "partner_id",
            "journal_id",
            "currency_id",
            "payment_state",
            "amount_residual",
        }
    ),
    "account.move.line": frozenset(
        {
            "id",
            "move_id",
            "move_id.state",
            "parent_state",
            "account_id",
            "account_id.account_type",
            "journal_id",
            "partner_id",
            "currency_id",
            "date",
            "date_maturity",
            "reconciled",
            "analytic_distribution",
        }
    ),
    "account.partial.reconcile": frozenset({"id", "debit_move_id", "credit_move_id", "max_date"}),
    "res.partner": frozenset({"id", "name", "active", "supplier_rank", "customer_rank"}),
    "product.product": frozenset({"id", "name", "default_code", "active"}),
    "account.account": frozenset({"id", "code", "name", "account_type", "reconcile"}),
    "account.analytic.account": frozenset({"id", "name", "code", "active"}),
}


def _invalid_response() -> OdooMcpError:
    return OdooMcpError(
        ErrorCode.ODOO_API_ERROR,
        "Odoo returned an invalid accounting response.",
        "Check Odoo compatibility and retry.",
    )


def _read_field(
    raw: RawRecord,
    model: str,
    field: str,
    parser: Callable[[object], ParsedT],
) -> ParsedT:
    try:
        return parser(raw.get(field))
    except OdooMcpError:
        raise OdooMcpError(
            ErrorCode.ODOO_API_ERROR,
            f"Odoo returned an invalid {model} field: {field}.",
            "Check Odoo compatibility and data integrity, then retry.",
        ) from None


def _invalid_input(message: str, remediation: str) -> OdooMcpError:
    return OdooMcpError(ErrorCode.INVALID_INPUT, message, remediation)


def _record(value: object) -> RawRecord:
    sanitized = strip_denied_fields(value)
    if not isinstance(sanitized, Mapping) or not all(isinstance(key, str) for key in sanitized):
        raise _invalid_response()
    return cast(RawRecord, sanitized)


def _positive_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _invalid_response()
    return value


def _positive_or_zero_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _invalid_response()
    return value


def _positive_ids(value: object, *, maximum: int) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise _invalid_response()
    identifiers = tuple(_positive_int(item) for item in value)
    if len(identifiers) != len(set(identifiers)):
        raise _invalid_response()
    return identifiers


def _created_id(value: object) -> int:
    if isinstance(value, list) and len(value) == 1:
        return _positive_int(value[0])
    return _positive_int(value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise _invalid_response()
    return value


def _move_state(value: object) -> Literal["draft", "posted"]:
    if value == "draft":
        return "draft"
    if value == "posted":
        return "posted"
    raise _invalid_response()


def _optional_text(value: object) -> str | None:
    if value is False or value is None:
        return None
    return _text(value)


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise _invalid_response()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _invalid_response() from None


def _optional_date(value: object) -> date | None:
    if value is False or value is None:
        return None
    return _date(value)


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise _invalid_response()
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise _invalid_response() from None
    if not result.is_finite():
        raise _invalid_response()
    return result


def _optional_decimal(value: object) -> Decimal | None:
    if value is False or value is None:
        return None
    return _decimal(value)


def _positive_decimal(value: object) -> Decimal:
    result = _decimal(value)
    if result <= 0:
        raise _invalid_response()
    return result


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise _invalid_response()
    return value


def _relation(value: object) -> RelatedRecord:
    if not isinstance(value, (list, tuple)) or len(value) != 2 or not isinstance(value[1], str):
        raise _invalid_response()
    return RelatedRecord(id=_positive_int(value[0]), name=value[1])


def _optional_relation(value: object) -> RelatedRecord | None:
    if value is False or value is None:
        return None
    return _relation(value)


def _company_id(value: object) -> int:
    return _relation(value).id


def _optional_company_id(value: object) -> int | None:
    relation = _optional_relation(value)
    return relation.id if relation else None


def _many_ids(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise _invalid_response()
    return tuple(_positive_int(item) for item in value)


def _analytic_distribution(value: object) -> dict[str, Decimal]:
    if value is False or value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _invalid_response()
    result: dict[str, Decimal] = {}
    for key, amount in value.items():
        if not isinstance(key, str):
            raise _invalid_response()
        result[key] = _decimal(amount)
    return result


def _normalize_account_move(raw: RawRecord) -> AccountMove:
    return AccountMove(
        id=_positive_int(raw.get("id")),
        name=_optional_text(raw.get("name")) or "/",
        move_type=_text(raw.get("move_type")),
        state=_text(raw.get("state")),
        date=_date(raw.get("date")),
        invoice_date=_optional_date(raw.get("invoice_date")),
        invoice_date_due=_optional_date(raw.get("invoice_date_due")),
        partner=_optional_relation(raw.get("partner_id")),
        journal=_relation(raw.get("journal_id")),
        company_id=_company_id(raw.get("company_id")),
        currency=_relation(raw.get("currency_id")),
        amount_total=_decimal(raw.get("amount_total")),
        amount_residual=_decimal(raw.get("amount_residual")),
        amount_residual_company=_optional_decimal(raw.get("amount_residual_signed")),
        payment_state=_optional_text(raw.get("payment_state")),
        reference=_optional_text(raw.get("ref")),
    )


def _normalize_account_move_line(raw: RawRecord) -> AccountMoveLine:
    return AccountMoveLine(
        id=_read_field(raw, "account.move.line", "id", _positive_int),
        move=_read_field(raw, "account.move.line", "move_id", _relation),
        move_state=_read_field(raw, "account.move.line", "parent_state", _move_state),
        account=_read_field(raw, "account.move.line", "account_id", _relation),
        journal=_read_field(raw, "account.move.line", "journal_id", _relation),
        partner=_read_field(raw, "account.move.line", "partner_id", _optional_relation),
        company_id=_read_field(raw, "account.move.line", "company_id", _company_id),
        currency=_read_field(raw, "account.move.line", "currency_id", _optional_relation),
        date=_read_field(raw, "account.move.line", "date", _date),
        maturity_date=_read_field(raw, "account.move.line", "date_maturity", _optional_date),
        label=_read_field(raw, "account.move.line", "name", _optional_text),
        debit=_read_field(raw, "account.move.line", "debit", _decimal),
        credit=_read_field(raw, "account.move.line", "credit", _decimal),
        balance=_read_field(raw, "account.move.line", "balance", _decimal),
        amount_currency=_read_field(raw, "account.move.line", "amount_currency", _decimal),
        residual=_read_field(raw, "account.move.line", "amount_residual", _decimal),
        residual_currency=_read_field(
            raw, "account.move.line", "amount_residual_currency", _decimal
        ),
        reconciled=_read_field(raw, "account.move.line", "reconciled", _boolean),
        analytic_distribution=_read_field(
            raw,
            "account.move.line",
            "analytic_distribution",
            _analytic_distribution,
        ),
    )


def _normalize_partial_reconciliation(raw: RawRecord) -> PartialReconciliation:
    return PartialReconciliation(
        id=_positive_int(raw.get("id")),
        debit_move_line=_relation(raw.get("debit_move_id")),
        credit_move_line=_relation(raw.get("credit_move_id")),
        amount=_decimal(raw.get("amount")),
        debit_amount_currency=_decimal(raw.get("debit_amount_currency")),
        credit_amount_currency=_decimal(raw.get("credit_amount_currency")),
        max_date=_date(raw.get("max_date")),
    )


def _normalize_journal(raw: RawRecord) -> Journal:
    return Journal(
        id=_positive_int(raw.get("id")),
        name=_text(raw.get("name")),
        code=_text(raw.get("code")),
        journal_type=_text(raw.get("type")),
        company_id=_company_id(raw.get("company_id")),
        currency=_optional_relation(raw.get("currency_id")),
    )


def _normalize_currency(raw: RawRecord) -> Currency:
    return Currency(
        id=_read_field(raw, "res.currency", "id", _positive_int),
        name=_read_field(raw, "res.currency", "name", _text),
        rounding=_read_field(raw, "res.currency", "rounding", _positive_decimal),
    )


def _normalize_bank_statement_line(raw: RawRecord) -> BankStatementLine:
    return BankStatementLine(
        id=_positive_int(raw.get("id")),
        date=_date(raw.get("date")),
        payment_reference=_optional_text(raw.get("payment_ref")),
        amount=_decimal(raw.get("amount")),
        amount_currency=_optional_decimal(raw.get("amount_currency")),
        foreign_currency=_optional_relation(raw.get("foreign_currency_id")),
        partner=_optional_relation(raw.get("partner_id")),
        journal=_relation(raw.get("journal_id")),
        company_id=_company_id(raw.get("company_id")),
        reconciled=_boolean(raw.get("is_reconciled")),
        move=_optional_relation(raw.get("move_id")),
    )


def _normalize_payment_term(raw: RawRecord) -> PaymentTerm:
    return PaymentTerm(
        id=_positive_int(raw.get("id")),
        name=_text(raw.get("name")),
        company_id=_optional_company_id(raw.get("company_id")),
    )


def _normalize_partner(raw: RawRecord) -> Partner:
    return Partner(
        id=_positive_int(raw.get("id")),
        name=_text(raw.get("name")),
        company_id=_optional_company_id(raw.get("company_id")),
        customer_rank=_positive_or_zero_int(raw.get("customer_rank", 0)),
        supplier_rank=_positive_or_zero_int(raw.get("supplier_rank", 0)),
    )


def _normalize_product(raw: RawRecord) -> Product:
    return Product(
        id=_positive_int(raw.get("id")),
        name=_text(raw.get("name")),
        default_code=_optional_text(raw.get("default_code")),
        company_id=_optional_company_id(raw.get("company_id")),
        list_price=_decimal(raw.get("list_price")),
        currency=_optional_relation(raw.get("currency_id")),
    )


def _normalize_account(raw: RawRecord) -> Account:
    return Account(
        id=_positive_int(raw.get("id")),
        code=_text(raw.get("code")),
        name=_text(raw.get("name")),
        account_type=_text(raw.get("account_type")),
        company_ids=_many_ids(raw.get("company_ids")),
        currency=_optional_relation(raw.get("currency_id")),
        reconcile=_boolean(raw.get("reconcile")),
    )


def _normalize_analytic_account(raw: RawRecord) -> AnalyticAccount:
    return AnalyticAccount(
        id=_positive_int(raw.get("id")),
        name=_text(raw.get("name")),
        code=_optional_text(raw.get("code")),
        company_id=_optional_company_id(raw.get("company_id")),
        currency=_optional_relation(raw.get("currency_id")),
    )


def _encode_cursor(identifier: int) -> str:
    payload = f"v1:{identifier}".encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> int | None:
    if cursor is None:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = base64.b64decode(padded, altchars=b"-_", validate=True).decode()
        version, raw_identifier = payload.split(":", 1)
        identifier = int(raw_identifier)
    except (ValueError, UnicodeError, binascii.Error):
        raise _invalid_input(
            "The pagination cursor is invalid.",
            "Restart the listing without a cursor.",
        ) from None
    if version != "v1" or identifier <= 0:
        raise _invalid_input(
            "The pagination cursor is invalid.",
            "Restart the listing without a cursor.",
        )
    return identifier


def _domain_value(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise _invalid_input("A filter value is invalid.", "Use a finite decimal value.")
        return float(value)
    if isinstance(value, tuple):
        return [_domain_value(item) for item in value]
    return value


def _filter_domain(model: str, filters: ReadFilters) -> list[object]:
    allowed = _FILTER_FIELDS.get(model, frozenset())
    domain: list[object] = []
    for clause in filters.clauses:
        leaf = clause.field.rsplit(".", 1)[-1]
        if leaf.casefold() in FIELD_DENYLIST:
            raise OdooMcpError(
                ErrorCode.FIELD_DENIED,
                "The requested Odoo field is denied.",
                "Remove the denied field and retry.",
            )
        if "company_id" in clause.field or clause.field not in allowed:
            raise _invalid_input(
                "The requested accounting filter is not allowed.",
                "Use a supported workflow filter.",
            )
        domain.append([clause.field, clause.operator, _domain_value(clause.value)])
    return domain


def _enforce_output_scope(
    model: str,
    item: RecordT,
    company_id: int,
) -> RecordT:
    if isinstance(item, Account):
        if company_id not in item.company_ids:
            raise _invalid_response()
        return cast(RecordT, item.model_copy(update={"company_ids": (company_id,)}))
    scoped = (
        AccountMove,
        AccountMoveLine,
        Journal,
        BankStatementLine,
        PaymentTerm,
        Partner,
        Product,
        AnalyticAccount,
    )
    if isinstance(item, scoped) and item.company_id not in {None, company_id}:
        raise _invalid_response()
    return item


class AccountingReader:
    """Odoo-owned read implementation used by the concrete client."""

    def __init__(
        self,
        transport: OdooTransport,
        validated_company_ids: Callable[[], tuple[int, ...] | None],
    ) -> None:
        self._transport = transport
        self._validated_company_ids = validated_company_ids

    def _require_company(self, company_id: int) -> None:
        validated = self._validated_company_ids()
        if (
            not isinstance(company_id, int)
            or isinstance(company_id, bool)
            or company_id <= 0
            or validated is None
            or company_id not in validated
        ):
            raise OdooMcpError(
                ErrorCode.COMPANY_NOT_FOUND,
                "The requested company is not authorized for this connection.",
                "Use an authorized company ID and retry.",
            )

    async def _read_page(
        self,
        model: str,
        company_id: int,
        scope: list[object],
        filters: ReadFilters,
        page: PageRequest,
        fields: list[str],
        normalize: Callable[[RawRecord], RecordT],
        validate_rows: RowValidator | None = None,
    ) -> RecordPage[RecordT]:
        self._require_company(company_id)
        ensure_model_read_allowed(model, module="accounting")
        cursor_id = _decode_cursor(page.cursor)
        domain = [*scope, *_filter_domain(model, filters)]
        if cursor_id is not None:
            domain.append(["id", ">", cursor_id])
        rows = await self._transport.search_read(
            model,
            domain,
            fields,
            limit=page.limit + 1,
            offset=0,
            order="id asc",
            company_ids=(company_id,),
        )
        if len(rows) > page.limit + 1:
            raise _invalid_response()
        identifiers = [_positive_int(row.get("id")) for row in rows]
        if identifiers != sorted(set(identifiers)) or (
            cursor_id is not None and identifiers and identifiers[0] <= cursor_id
        ):
            raise _invalid_response()
        has_more = len(rows) > page.limit
        selected = [_record(row) for row in rows[: page.limit]]
        if validate_rows is not None:
            await validate_rows(selected, company_id)
        items = [_enforce_output_scope(model, normalize(row), company_id) for row in selected]
        next_cursor = _encode_cursor(identifiers[len(selected) - 1]) if has_more else None
        return RecordPage[RecordT](items=items, next_cursor=next_cursor)

    async def _validate_partial_reconciliation_rows(
        self,
        rows: list[RawRecord],
        company_id: int,
    ) -> None:
        line_ids = {
            _relation(row.get(field)).id
            for row in rows
            for field in ("debit_move_id", "credit_move_id")
        }
        if not line_ids:
            return
        ensure_model_read_allowed("account.move.line", module="accounting")
        attribution_rows = await self._transport.search_read(
            "account.move.line",
            [
                ["id", "in", sorted(line_ids)],
                ["company_id", "=", company_id],
            ],
            ["id", "company_id"],
            limit=len(line_ids),
            offset=0,
            order="id asc",
            company_ids=(company_id,),
        )
        attributed: set[int] = set()
        for raw in attribution_rows:
            row = _record(raw)
            identifier = _positive_int(row.get("id"))
            if identifier in attributed or _company_id(row.get("company_id")) != company_id:
                raise _invalid_response()
            attributed.add(identifier)
        if attributed != line_ids:
            raise _invalid_response()

    async def get_account_moves(
        self,
        company_id: int,
        filters: ReadFilters,
        page: PageRequest,
    ) -> RecordPage[AccountMove]:
        return await self._read_page(
            "account.move",
            company_id,
            [["company_id", "=", company_id]],
            filters,
            page,
            _ACCOUNT_MOVE_FIELDS,
            _normalize_account_move,
        )

    async def get_account_move_lines(
        self,
        company_id: int,
        filters: ReadFilters,
        page: PageRequest,
    ) -> RecordPage[AccountMoveLine]:
        return await self._read_page(
            "account.move.line",
            company_id,
            [["company_id", "=", company_id]],
            filters,
            page,
            _ACCOUNT_MOVE_LINE_FIELDS,
            _normalize_account_move_line,
        )

    async def get_partial_reconciliations(
        self,
        company_id: int,
        filters: ReadFilters,
        page: PageRequest,
    ) -> RecordPage[PartialReconciliation]:
        return await self._read_page(
            "account.partial.reconcile",
            company_id,
            [
                ["debit_move_id.company_id", "=", company_id],
                ["credit_move_id.company_id", "=", company_id],
            ],
            filters,
            page,
            _PARTIAL_RECONCILIATION_FIELDS,
            _normalize_partial_reconciliation,
            self._validate_partial_reconciliation_rows,
        )

    async def get_journals(
        self, company_id: int, *, page: PageRequest = DEFAULT_PAGE_REQUEST
    ) -> RecordPage[Journal]:
        return await self._read_page(
            "account.journal",
            company_id,
            [["company_id", "=", company_id]],
            ReadFilters(),
            page,
            _JOURNAL_FIELDS,
            _normalize_journal,
        )

    async def get_currencies(
        self,
        company_id: int,
        currency_ids: tuple[int, ...],
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[Currency]:
        if (
            not currency_ids
            or len(currency_ids) > _MAX_CURRENCY_IDS
            or len(set(currency_ids)) != len(currency_ids)
            or any(
                not isinstance(identifier, int) or isinstance(identifier, bool) or identifier <= 0
                for identifier in currency_ids
            )
        ):
            raise _invalid_input(
                "The currency selection is invalid.",
                "Use unique positive currency IDs and retry.",
            )
        result = await self._read_page(
            "res.currency",
            company_id,
            [["id", "in", list(currency_ids)]],
            ReadFilters(),
            page,
            _CURRENCY_FIELDS,
            _normalize_currency,
        )
        if any(item.id not in currency_ids for item in result.items):
            raise _invalid_response()
        return result

    async def get_bank_statement_lines(
        self,
        company_id: int,
        period: DatePeriod,
        journal_id: int | None,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[BankStatementLine]:
        scope: list[object] = [
            ["company_id", "=", company_id],
            ["date", ">=", period.start.isoformat()],
            ["date", "<=", period.end.isoformat()],
        ]
        if journal_id is not None:
            if not isinstance(journal_id, int) or isinstance(journal_id, bool) or journal_id <= 0:
                raise _invalid_input(
                    "The journal ID is invalid.", "Use a positive journal ID and retry."
                )
            scope.append(["journal_id", "=", journal_id])
        return await self._read_page(
            "account.bank.statement.line",
            company_id,
            scope,
            ReadFilters(),
            page,
            _BANK_STATEMENT_LINE_FIELDS,
            _normalize_bank_statement_line,
        )

    async def get_payment_terms(
        self, company_id: int, *, page: PageRequest = DEFAULT_PAGE_REQUEST
    ) -> RecordPage[PaymentTerm]:
        return await self._read_page(
            "account.payment.term",
            company_id,
            [["company_id", "in", [False, company_id]]],
            ReadFilters(),
            page,
            _PAYMENT_TERM_FIELDS,
            _normalize_payment_term,
        )

    async def get_partners(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[Partner]:
        return await self._read_page(
            "res.partner",
            company_id,
            [["company_id", "in", [False, company_id]]],
            filters,
            page,
            _PARTNER_FIELDS,
            _normalize_partner,
        )

    async def get_products(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[Product]:
        return await self._read_page(
            "product.product",
            company_id,
            [["company_id", "in", [False, company_id]]],
            filters,
            page,
            _PRODUCT_FIELDS,
            _normalize_product,
        )

    async def get_account_accounts(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[Account]:
        return await self._read_page(
            "account.account",
            company_id,
            [["company_ids", "in", [company_id]]],
            filters,
            page,
            _ACCOUNT_FIELDS,
            _normalize_account,
        )

    async def get_analytic_accounts(
        self,
        company_id: int,
        filters: ReadFilters,
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[AnalyticAccount]:
        return await self._read_page(
            "account.analytic.account",
            company_id,
            [["company_id", "in", [False, company_id]]],
            filters,
            page,
            _ANALYTIC_ACCOUNT_FIELDS,
            _normalize_analytic_account,
        )

    async def get_invoice_effect(self, company_id: int, move_id: int) -> InvoiceEffect:
        self._require_company(company_id)
        if not isinstance(move_id, int) or isinstance(move_id, bool) or move_id <= 0:
            raise _invalid_input("The invoice ID is invalid.", "Use a positive invoice ID.")
        ensure_model_read_allowed("account.move", module="accounting")
        rows = await self._transport.search_read(
            "account.move",
            [["id", "=", move_id], ["company_id", "=", company_id]],
            _INVOICE_EFFECT_FIELDS,
            limit=2,
            offset=0,
            order="id asc",
            company_ids=(company_id,),
        )
        if len(rows) != 1:
            raise OdooMcpError(
                ErrorCode.INVALID_INPUT,
                "The requested invoice or bill is unavailable.",
                "Use an invoice or bill ID from the authorized company.",
            )
        raw = _record(rows[0])
        ensure_model_read_allowed("account.move.line", module="accounting")
        line_rows = await self._transport.search_read(
            "account.move.line",
            [["move_id", "=", move_id], ["company_id", "=", company_id]],
            _INVOICE_EFFECT_LINE_FIELDS,
            limit=1001,
            offset=0,
            order="id asc",
            company_ids=(company_id,),
        )
        if len(line_rows) > 1000:
            raise _invalid_response()
        invoice_lines: list[InvoiceLineEffect] = []
        validation_lines: list[InvoiceValidationLine] = []
        tax_totals: dict[int, tuple[str, Decimal]] = {}
        schedule: list[PaymentScheduleLine] = []
        for value in line_rows:
            line = _record(value)
            if _company_id(line.get("company_id")) != company_id:
                raise _invalid_response()
            display_type = _optional_text(line.get("display_type"))
            tax = _optional_relation(line.get("tax_line_id"))
            validation_lines.append(
                InvoiceValidationLine(
                    id=_positive_int(line.get("id")),
                    display_type=display_type,
                    account=_optional_relation(line.get("account_id")),
                    debit=_decimal(line.get("debit")),
                    credit=_decimal(line.get("credit")),
                    balance=_decimal(line.get("balance")),
                    currency=_optional_relation(line.get("currency_id")),
                    amount_currency=_decimal(line.get("amount_currency")),
                    tax_line=tax,
                    subtotal=_decimal(line.get("price_subtotal")),
                    total=_decimal(line.get("price_total")),
                )
            )
            if tax is not None:
                existing = tax_totals.get(tax.id)
                if existing is not None and existing[0] != tax.name:
                    raise _invalid_response()
                tax_totals[tax.id] = (
                    tax.name,
                    (existing[1] if existing is not None else Decimal("0"))
                    + abs(_decimal(line.get("balance"))),
                )
            elif display_type in {None, "product"}:
                invoice_lines.append(
                    InvoiceLineEffect(
                        id=_positive_int(line.get("id")),
                        description=_text(line.get("name")),
                        quantity=_decimal(line.get("quantity")),
                        unit_price=_decimal(line.get("price_unit")),
                        subtotal=_decimal(line.get("price_subtotal")),
                        total=_decimal(line.get("price_total")),
                        analytic_distribution=_analytic_distribution(
                            line.get("analytic_distribution")
                        ),
                    )
                )
            elif display_type == "payment_term":
                maturity = _optional_date(line.get("date_maturity"))
                if maturity is None:
                    raise _invalid_response()
                schedule.append(
                    PaymentScheduleLine(
                        due_date=maturity,
                        amount=abs(_decimal(line.get("amount_residual"))),
                    )
                )
        return InvoiceEffect(
            id=_positive_int(raw.get("id")),
            name=_text(raw.get("name")),
            move_type=_text(raw.get("move_type")),
            state=_text(raw.get("state")),
            company_id=_company_id(raw.get("company_id")),
            partner=_relation(raw.get("partner_id")),
            invoice_date=_date(raw.get("invoice_date")),
            due_date=_optional_date(raw.get("invoice_date_due")),
            journal=_relation(raw.get("journal_id")),
            fiscal_position=_optional_relation(raw.get("fiscal_position_id")),
            currency=_relation(raw.get("currency_id")),
            payment_term=_optional_relation(raw.get("invoice_payment_term_id")),
            amount_untaxed=_decimal(raw.get("amount_untaxed")),
            amount_tax=_decimal(raw.get("amount_tax")),
            amount_total=_decimal(raw.get("amount_total")),
            amount_residual=_decimal(raw.get("amount_residual")),
            payment_state=_optional_text(raw.get("payment_state")),
            taxes=tuple(
                InvoiceTax(id=identifier, name=value[0], amount=value[1])
                for identifier, value in sorted(tax_totals.items())
            ),
            lines=tuple(invoice_lines),
            payment_schedule=tuple(schedule),
            validation_lines=tuple(validation_lines),
        )

    async def create_draft_invoice(self, draft: InvoiceDraft) -> InvoiceEffect:
        self._require_company(draft.company_id)
        ensure_accounting_action_allowed("account.move", "create_draft")
        values: dict[str, object] = {
            "company_id": draft.company_id,
            "move_type": draft.move_type,
            "partner_id": draft.partner_id,
            "invoice_date": draft.invoice_date.isoformat(),
            "invoice_line_ids": [
                [
                    0,
                    0,
                    {
                        "name": line.description,
                        "quantity": float(line.quantity),
                        "price_unit": float(line.unit_price),
                        "account_id": line.account_id,
                        **({"product_id": line.product_id} if line.product_id else {}),
                        **(
                            {
                                "analytic_distribution": {
                                    str(line.analytic_account_id or draft.analytic_account_id): 100
                                }
                            }
                            if line.analytic_account_id or draft.analytic_account_id
                            else {}
                        ),
                    },
                ]
                for line in draft.lines
            ],
        }
        if draft.currency_id is not None:
            values["currency_id"] = draft.currency_id
        if draft.payment_term_id is not None:
            values["invoice_payment_term_id"] = draft.payment_term_id
        if draft.vendor_reference is not None:
            values["ref"] = draft.vendor_reference
        result = await self._transport.execute_method(
            "account.move",
            "create",
            named={"vals_list": values},
            company_ids=(draft.company_id,),
        )
        move_id = _created_id(result)
        effect = await self.get_invoice_effect(draft.company_id, move_id)
        if effect.state != "draft" or effect.move_type != draft.move_type:
            raise _invalid_response()
        return effect

    async def get_journal_entry(self, company_id: int, move_id: int) -> JournalEntry:
        self._require_company(company_id)
        if not isinstance(move_id, int) or isinstance(move_id, bool) or move_id <= 0:
            raise _invalid_input("The journal entry ID is invalid.", "Use a positive entry ID.")
        moves = await self.get_account_moves(
            company_id,
            ReadFilters(clauses=(FilterClause(field="id", operator="=", value=move_id),)),
            PageRequest(limit=2),
        )
        if len(moves.items) != 1:
            raise OdooMcpError(
                ErrorCode.INVALID_INPUT,
                "The requested journal entry is unavailable.",
                "Use a journal entry ID from the authorized company.",
            )
        move = moves.items[0]
        if move.id != move_id:
            raise _invalid_response()
        lines: list[JournalEntryLine] = []
        cursor: str | None = None
        while True:
            page = await self.get_account_move_lines(
                company_id,
                ReadFilters(clauses=(FilterClause(field="move_id", operator="=", value=move_id),)),
                PageRequest(limit=500, cursor=cursor),
            )
            if any(line.move.id != move_id for line in page.items):
                raise _invalid_response()
            lines.extend(
                JournalEntryLine(
                    id=line.id,
                    account=line.account,
                    partner=line.partner,
                    description=line.label,
                    debit=line.debit,
                    credit=line.credit,
                    analytic_distribution=line.analytic_distribution,
                )
                for line in page.items
            )
            if len(lines) > 10_000:
                raise _invalid_response()
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        return JournalEntry(
            id=move.id,
            name=move.name,
            move_type=move.move_type,
            state=move.state,
            date=move.date,
            journal=move.journal,
            company_id=move.company_id,
            currency=move.currency,
            reference=move.reference,
            lines=tuple(lines),
        )

    async def create_journal_entry_draft(self, draft: JournalEntryDraft) -> JournalEntry:
        self._require_company(draft.company_id)
        ensure_accounting_action_allowed("account.move", "create_draft")
        values: dict[str, object] = {
            "company_id": draft.company_id,
            "move_type": "entry",
            "journal_id": draft.journal_id,
            "date": draft.entry_date.isoformat(),
            "line_ids": [
                [
                    0,
                    0,
                    {
                        "account_id": line.account_id,
                        "debit": float(line.debit),
                        "credit": float(line.credit),
                        **({"partner_id": line.partner_id} if line.partner_id else {}),
                        **({"name": line.description} if line.description else {}),
                        **(
                            {"analytic_distribution": {str(line.analytic_account_id): 100}}
                            if line.analytic_account_id
                            else {}
                        ),
                    },
                ]
                for line in draft.lines
            ],
        }
        if draft.reference is not None:
            values["ref"] = draft.reference
        result = await self._transport.execute_method(
            "account.move",
            "create",
            named={"vals_list": values},
            company_ids=(draft.company_id,),
        )
        effect = await self.get_journal_entry(draft.company_id, _created_id(result))
        if effect.state != "draft" or effect.move_type != "entry":
            raise _invalid_response()
        return effect

    async def post_journal_entry(self, company_id: int, move_id: int) -> JournalEntry:
        self._require_company(company_id)
        ensure_accounting_action_allowed("account.move", "post_existing_draft")
        current = await self.get_journal_entry(company_id, move_id)
        if current.state != "draft" or current.move_type != "entry":
            raise OdooMcpError(
                ErrorCode.JOURNAL_ENTRY_NOT_DRAFT,
                "The journal entry is not an eligible draft manual entry.",
                "Use an existing draft manual journal entry from the authorized company.",
            )
        await self._transport.execute_method(
            "account.move", "action_post", ids=(move_id,), company_ids=(company_id,)
        )
        effect = await self.get_journal_entry(company_id, move_id)
        if effect.state != "posted" or effect.move_type != "entry":
            raise _invalid_response()
        return effect

    async def post_invoice(self, company_id: int, move_id: int) -> InvoiceEffect:
        self._require_company(company_id)
        ensure_accounting_action_allowed("account.move", "post_existing_draft")
        await self._transport.execute_method(
            "account.move",
            "action_post",
            ids=(move_id,),
            company_ids=(company_id,),
        )
        effect = await self.get_invoice_effect(company_id, move_id)
        if effect.state != "posted":
            raise _invalid_response()
        return effect

    async def create_credit_note(
        self,
        company_id: int,
        original_move_id: int,
        credit_date: date,
        reason: str,
    ) -> InvoiceEffect:
        self._require_company(company_id)
        ensure_accounting_action_allowed("account.move", "reverse_existing_move")
        ensure_accounting_action_allowed("account.move.reversal", "create_transient")
        context = {
            "allowed_company_ids": [company_id],
            "active_model": "account.move",
            "active_ids": [original_move_id],
            "active_id": original_move_id,
        }
        existing_rows = await self._transport.search_read(
            "account.move",
            [
                ["reversed_entry_id", "=", original_move_id],
                ["company_id", "=", company_id],
            ],
            ["id"],
            limit=1001,
            offset=0,
            order="id asc",
            company_ids=(company_id,),
        )
        if len(existing_rows) > 1000:
            raise _invalid_response()
        existing_ids = {_positive_int(row.get("id")) for row in existing_rows}
        wizard_id = _created_id(
            await self._transport.execute_method(
                "account.move.reversal",
                "create",
                named={
                    "vals_list": {
                        "move_ids": [[6, 0, [original_move_id]]],
                        "date": credit_date.isoformat(),
                        "reason": reason,
                    },
                    "context": context,
                },
                company_ids=(company_id,),
            )
        )
        ensure_accounting_action_allowed("account.move.reversal", "execute_standard_workflow")
        result = await self._transport.execute_method(
            "account.move.reversal",
            "reverse_moves",
            ids=(wizard_id,),
            named={"context": context},
            company_ids=(company_id,),
        )
        action_id: int | None = None
        if isinstance(result, Mapping):
            candidate = result.get("res_id")
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
                action_id = candidate
        rows = await self._transport.search_read(
            "account.move",
            [
                ["reversed_entry_id", "=", original_move_id],
                ["company_id", "=", company_id],
                ["state", "=", "draft"],
            ],
            ["id"],
            limit=1001,
            offset=0,
            order="id desc",
            company_ids=(company_id,),
        )
        if len(rows) > 1000:
            raise _invalid_response()
        new_ids = {_positive_int(row.get("id")) for row in rows} - existing_ids
        if action_id is not None:
            if action_id not in new_ids:
                raise _invalid_response()
            move_id = action_id
        elif len(new_ids) == 1:
            move_id = new_ids.pop()
        else:
            raise _invalid_response()
        return await self.get_invoice_effect(company_id, move_id)

    async def _payment_wizard_row(
        self,
        request: PaymentPreviewRequest,
        *,
        journal_id: int | None = None,
        payment_method_line_id: int | None = None,
        execution: bool = False,
    ) -> tuple[int, RawRecord]:
        context = {
            "allowed_company_ids": [request.company_id],
            "active_model": "account.move",
            "active_ids": [request.invoice_id],
            "active_id": request.invoice_id,
        }
        values: dict[str, object] = {
            "payment_date": request.payment_date.isoformat(),
            "amount": float(request.amount),
        }
        if journal_id is not None:
            values["journal_id"] = journal_id
        if payment_method_line_id is not None:
            values["payment_method_line_id"] = payment_method_line_id
        ensure_accounting_action_allowed(
            "account.payment.register",
            "create_execution_transient" if execution else "create_preview_transient",
        )
        wizard_id = _created_id(
            await self._transport.execute_method(
                "account.payment.register",
                "create",
                named={"vals_list": values, "context": context},
                company_ids=(request.company_id,),
            )
        )
        ensure_accounting_action_allowed("account.payment.register", "read_preview_transient")
        result = await self._transport.execute_method(
            "account.payment.register",
            "read",
            ids=(wizard_id,),
            named={"fields": list(_PAYMENT_PREVIEW_FIELDS), "context": context},
            company_ids=(request.company_id,),
        )
        if not isinstance(result, list) or len(result) != 1:
            raise _invalid_response()
        row = _record(result[0])
        if (
            _company_id(row.get("company_id")) != request.company_id
            or _date(row.get("payment_date")) != request.payment_date
            or _decimal(row.get("amount")) <= 0
        ):
            raise _invalid_response()
        return wizard_id, row

    @staticmethod
    def _payment_row_signature(row: RawRecord) -> tuple[object, ...]:
        return (
            _decimal(row.get("amount")),
            _relation(row.get("currency_id")),
            _text(row.get("payment_type")),
            _text(row.get("partner_type")),
            _company_id(row.get("company_id")),
            _boolean(row.get("can_edit_wizard")),
            _positive_ids(row.get("available_journal_ids"), maximum=_MAX_PAYMENT_JOURNALS),
        )

    @staticmethod
    def _payment_route(row: RawRecord, journal_id: int, method_id: int) -> PaymentRoute:
        journal = _relation(row.get("journal_id"))
        method = _relation(row.get("payment_method_line_id"))
        if journal.id != journal_id or method.id != method_id:
            raise _invalid_response()
        code = _optional_text(row.get("payment_method_code"))
        return PaymentRoute(
            journal=journal,
            payment_method_line=method,
            payment_method_code=code,
            payment_type=_text(row.get("payment_type")),
            external_effect_status=("not_initiated_by_odoo" if code == "manual" else "unknown"),
        )

    async def get_payment_registration_preview(
        self, request: PaymentPreviewRequest
    ) -> PaymentRegistrationPreview:
        self._require_company(request.company_id)
        invoice = await self.get_invoice_effect(request.company_id, request.invoice_id)
        if (
            invoice.state != "posted"
            or invoice.move_type not in {"out_invoice", "in_invoice"}
            or request.amount > invoice.amount_residual
        ):
            raise _invalid_input(
                "The invoice is not eligible for payment registration.",
                "Use a posted invoice or bill and an amount within its residual.",
            )
        _default_id, default_row = await self._payment_wizard_row(request)
        default_signature = self._payment_row_signature(default_row)
        journal_ids = _positive_ids(
            default_row.get("available_journal_ids"), maximum=_MAX_PAYMENT_JOURNALS
        )
        default_journal = _optional_relation(default_row.get("journal_id"))
        default_method = _optional_relation(default_row.get("payment_method_line_id"))
        if (default_journal is None) != (default_method is None) or (
            default_journal is not None and default_journal.id not in journal_ids
        ):
            raise _invalid_response()
        routes: list[PaymentRoute] = []
        transient_count = 1
        for journal_id in journal_ids:
            transient_count += 1
            if transient_count > _MAX_PAYMENT_TRANSIENTS:
                raise _invalid_response()
            _journal_wizard_id, journal_row = await self._payment_wizard_row(
                request, journal_id=journal_id
            )
            if (
                self._payment_row_signature(journal_row) != default_signature
                or _relation(journal_row.get("journal_id")).id != journal_id
            ):
                raise _invalid_response()
            method_ids = _positive_ids(
                journal_row.get("available_payment_method_line_ids"),
                maximum=_MAX_PAYMENT_METHODS,
            )
            selected_method = _optional_relation(journal_row.get("payment_method_line_id"))
            if selected_method is not None and selected_method.id not in method_ids:
                raise _invalid_response()
            for method_id in method_ids:
                if selected_method is not None and method_id == selected_method.id:
                    method_row = journal_row
                else:
                    transient_count += 1
                    if transient_count > _MAX_PAYMENT_TRANSIENTS:
                        raise _invalid_response()
                    _method_wizard_id, method_row = await self._payment_wizard_row(
                        request,
                        journal_id=journal_id,
                        payment_method_line_id=method_id,
                    )
                    if self._payment_row_signature(method_row) != default_signature:
                        raise _invalid_response()
                routes.append(self._payment_route(method_row, journal_id, method_id))
        route_ids = [(item.journal.id, item.payment_method_line.id) for item in routes]
        default_route = (
            (default_journal.id, default_method.id)
            if default_journal is not None and default_method is not None
            else None
        )
        if len(route_ids) != len(set(route_ids)) or (
            default_route is not None and default_route not in route_ids
        ):
            raise _invalid_response()
        currency = _relation(default_row.get("currency_id"))
        amount = _decimal(default_row.get("amount"))
        expected_payment_type = "inbound" if invoice.move_type == "out_invoice" else "outbound"
        expected_partner_type = "customer" if invoice.move_type == "out_invoice" else "supplier"
        if (
            currency.id != invoice.currency.id
            or amount > invoice.amount_residual
            or _text(default_row.get("payment_type")) != expected_payment_type
            or _text(default_row.get("partner_type")) != expected_partner_type
        ):
            raise _invalid_response()
        return PaymentRegistrationPreview(
            invoice_id=request.invoice_id,
            company_id=request.company_id,
            payment_date=_date(default_row.get("payment_date")),
            amount=amount,
            currency=currency,
            payment_type=_text(default_row.get("payment_type")),
            partner_type=_text(default_row.get("partner_type")),
            can_edit_wizard=_boolean(default_row.get("can_edit_wizard")),
            routes=tuple(routes),
            default_journal_id=default_journal.id if default_journal else None,
            default_payment_method_line_id=default_method.id if default_method else None,
        )

    async def register_payment(self, registration: PaymentRegistration) -> InvoiceEffect:
        self._require_company(registration.company_id)
        request = PaymentPreviewRequest(
            invoice_id=registration.invoice_id,
            company_id=registration.company_id,
            payment_date=registration.payment_date,
            amount=registration.amount,
        )
        effect = await self.get_invoice_effect(registration.company_id, registration.invoice_id)
        if effect.state != "posted" or registration.amount > effect.amount_residual:
            raise _invalid_input(
                "The invoice is no longer eligible for payment registration.",
                "Refresh the invoice and retry with a new idempotency key.",
            )
        wizard_id, row = await self._payment_wizard_row(
            request,
            journal_id=registration.journal_id,
            payment_method_line_id=registration.payment_method_line_id,
            execution=True,
        )
        available_journals = _positive_ids(
            row.get("available_journal_ids"), maximum=_MAX_PAYMENT_JOURNALS
        )
        available_methods = _positive_ids(
            row.get("available_payment_method_line_ids"), maximum=_MAX_PAYMENT_METHODS
        )
        route = self._payment_route(
            row, registration.journal_id, registration.payment_method_line_id
        )
        if (
            registration.journal_id not in available_journals
            or registration.payment_method_line_id not in available_methods
            or _relation(row.get("currency_id")).id != effect.currency.id
            or _decimal(row.get("amount")) != registration.amount
            or _text(row.get("payment_type"))
            != ("inbound" if effect.move_type == "out_invoice" else "outbound")
            or _text(row.get("partner_type"))
            != ("customer" if effect.move_type == "out_invoice" else "supplier")
            or route.payment_method_code != registration.payment_method_code
            or route.external_effect_status != registration.external_effect_status
        ):
            raise _invalid_response()
        context = {
            "allowed_company_ids": [registration.company_id],
            "active_model": "account.move",
            "active_ids": [registration.invoice_id],
            "active_id": registration.invoice_id,
        }
        ensure_accounting_action_allowed("account.payment.register", "execute_standard_workflow")
        await self._transport.execute_method(
            "account.payment.register",
            "action_create_payments",
            ids=(wizard_id,),
            named={"context": context},
            company_ids=(registration.company_id,),
        )
        return await self.get_invoice_effect(registration.company_id, registration.invoice_id)
