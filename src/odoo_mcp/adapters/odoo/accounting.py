"""Typed, bounded accounting reads over the internal Odoo transport."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TypeAlias, TypeVar, cast

from odoo_mcp.adapters.accounting import (
    DEFAULT_PAGE_REQUEST,
    Account,
    AccountMove,
    AccountMoveLine,
    AdapterValue,
    AnalyticAccount,
    BankStatementLine,
    DatePeriod,
    Journal,
    PageRequest,
    PartialReconciliation,
    Partner,
    PaymentMethodLine,
    PaymentTerm,
    Product,
    ReadFilters,
    RecordPage,
    RelatedRecord,
)
from odoo_mcp.adapters.odoo.policy import (
    FIELD_DENYLIST,
    ensure_model_read_allowed,
    strip_denied_fields,
)
from odoo_mcp.adapters.odoo.transports.base import OdooTransport
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError

RawRecord: TypeAlias = Mapping[str, object]
RecordT = TypeVar("RecordT", bound=AdapterValue)
RowValidator = Callable[[list[RawRecord], int], Awaitable[None]]

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
    "payment_state",
    "ref",
]
_ACCOUNT_MOVE_LINE_FIELDS = [
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
_PAYMENT_METHOD_LINE_FIELDS = [
    "id",
    "name",
    "journal_id",
    "payment_method_id",
    "payment_type",
]
_PARTNER_FIELDS = ["id", "name", "company_id"]
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


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise _invalid_response()
    return value


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
        name=_text(raw.get("name")),
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
        payment_state=_optional_text(raw.get("payment_state")),
        reference=_optional_text(raw.get("ref")),
    )


def _normalize_account_move_line(raw: RawRecord) -> AccountMoveLine:
    return AccountMoveLine(
        id=_positive_int(raw.get("id")),
        move=_relation(raw.get("move_id")),
        account=_relation(raw.get("account_id")),
        journal=_relation(raw.get("journal_id")),
        partner=_optional_relation(raw.get("partner_id")),
        company_id=_company_id(raw.get("company_id")),
        currency=_optional_relation(raw.get("currency_id")),
        date=_date(raw.get("date")),
        maturity_date=_optional_date(raw.get("date_maturity")),
        label=_optional_text(raw.get("name")),
        debit=_decimal(raw.get("debit")),
        credit=_decimal(raw.get("credit")),
        balance=_decimal(raw.get("balance")),
        amount_currency=_decimal(raw.get("amount_currency")),
        residual=_decimal(raw.get("amount_residual")),
        residual_currency=_decimal(raw.get("amount_residual_currency")),
        reconciled=_boolean(raw.get("reconciled")),
        analytic_distribution=_analytic_distribution(raw.get("analytic_distribution")),
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


def _normalize_payment_method_line(raw: RawRecord) -> PaymentMethodLine:
    return PaymentMethodLine(
        id=_positive_int(raw.get("id")),
        name=_text(raw.get("name")),
        journal=_relation(raw.get("journal_id")),
        payment_method=_relation(raw.get("payment_method_id")),
        payment_type=_optional_text(raw.get("payment_type")),
    )


def _normalize_partner(raw: RawRecord) -> Partner:
    return Partner(
        id=_positive_int(raw.get("id")),
        name=_text(raw.get("name")),
        company_id=_optional_company_id(raw.get("company_id")),
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

    async def get_payment_method_lines(
        self,
        company_id: int,
        journal_ids: tuple[int, ...],
        *,
        page: PageRequest = DEFAULT_PAGE_REQUEST,
    ) -> RecordPage[PaymentMethodLine]:
        if not journal_ids or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in journal_ids
        ):
            raise _invalid_input(
                "The journal IDs are invalid.",
                "Provide one or more positive journal IDs.",
            )
        return await self._read_page(
            "account.payment.method.line",
            company_id,
            [
                ["journal_id.company_id", "=", company_id],
                ["journal_id", "in", list(dict.fromkeys(journal_ids))],
            ],
            ReadFilters(),
            page,
            _PAYMENT_METHOD_LINE_FIELDS,
            _normalize_payment_method_line,
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
