"""Typed, version-aware, bounded Payroll access over the Odoo transport."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeAlias, TypeVar, cast

from odoo_mcp.adapters.accounting import RelatedRecord
from odoo_mcp.adapters.odoo.policy import (
    FIELD_DENYLIST,
    ensure_payroll_action_allowed,
    ensure_payroll_model_read_allowed,
)
from odoo_mcp.adapters.odoo.transports.base import OdooTransport
from odoo_mcp.adapters.payroll import (
    DEFAULT_PAYROLL_PAGE_REQUEST,
    DeletedPayslipInput,
    DraftPayslipInputCreate,
    DraftPayslipInputUpdate,
    PayrollBatch,
    PayrollBatchFilters,
    PayrollBatchState,
    PayrollContractFilters,
    PayrollContractSegment,
    PayrollEmployee,
    PayrollInputType,
    PayrollInputTypeFilters,
    PayrollPage,
    PayrollPageRequest,
    PayrollPeriod,
    PayrollState,
    PayrollStructure,
    PayrollValue,
    PayrollWorkEntry,
    PayrollWorkEntryFilters,
    PayrollWorkEntryState,
    PayrollWriteCheckpoint,
    PayrollWriteRejected,
    Payslip,
    PayslipChildFilters,
    PayslipFilters,
    PayslipInput,
    PayslipLine,
    PayslipWorkedDay,
)
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError

RawRecord: TypeAlias = Mapping[str, object]
ItemT = TypeVar("ItemT", bound=PayrollValue)
StateT = TypeVar("StateT", bound=str)
ChildT = TypeVar("ChildT", PayslipLine, PayslipWorkedDay, PayslipInput)

PAYROLL_SOURCE_CAPS: dict[str, int] = {
    "hr.payslip.run": 5_000,
    "hr.payslip": 5_000,
    "hr.payslip.line": 100_000,
    "hr.payslip.worked_days": 100_000,
    "hr.payslip.input": 100_000,
    "hr.payslip.input.type": 5_000,
    "hr.payroll.structure": 5_000,
    "hr.employee": 5_000,
    "hr.contract": 20_000,
    "hr.version": 20_000,
    "hr.work.entry": 100_000,
}

_MAX_WRITE_CALCULATED_LINES = 500
_MAX_WRITE_WORKED_DAYS = 200
_CHECKPOINT_CONFLICT_CODES = {
    ErrorCode.CONTRACT_DATA_MISSING,
    ErrorCode.EMPLOYEE_NOT_FOUND,
    ErrorCode.PAYROLL_INPUT_NOT_FOUND,
    ErrorCode.PAYROLL_INPUT_TYPE_NOT_ALLOWED,
    ErrorCode.PAYROLL_RESULT_TOO_LARGE,
    ErrorCode.PAYROLL_SOURCE_INCONSISTENT,
    ErrorCode.PAYROLL_STATE_NOT_EDITABLE,
    ErrorCode.PAYROLL_STRUCTURE_NOT_FOUND,
    ErrorCode.PAYSLIP_NOT_FOUND,
    ErrorCode.ODOO_STATE_CONFLICT,
}

_BATCH_FIELDS = (
    "id",
    "name",
    "date_start",
    "date_end",
    "state",
    "company_id",
    "write_date",
)
_INPUT_TYPE_FIELDS = (
    "id",
    "name",
    "code",
    "struct_ids",
    "active",
    "is_quantity",
    "available_in_attachments",
    "write_date",
)
_STRUCTURE_FIELDS = ("id", "input_line_type_ids", "write_date")
_EMPLOYEE_FIELDS = ("id", "name", "active", "company_id", "write_date")

PAYROLL_MODEL_FIELDS_BY_VERSION: dict[int, dict[str, tuple[str, ...]]] = {
    18: {
        "hr.payslip": (
            "id",
            "name",
            "number",
            "employee_id",
            "date_from",
            "date_to",
            "state",
            "company_id",
            "contract_id",
            "struct_id",
            "payslip_run_id",
            "credit_note",
            "currency_id",
            "write_date",
        ),
        "hr.payslip.run": _BATCH_FIELDS,
        "hr.payslip.line": (
            "id",
            "slip_id",
            "salary_rule_id",
            "employee_id",
            "contract_id",
            "name",
            "code",
            "category_id",
            "sequence",
            "quantity",
            "rate",
            "amount",
            "total",
            "currency_id",
            "write_date",
        ),
        "hr.payslip.worked_days": (
            "id",
            "payslip_id",
            "contract_id",
            "work_entry_type_id",
            "name",
            "code",
            "number_of_days",
            "number_of_hours",
            "amount",
            "currency_id",
            "write_date",
        ),
        "hr.payslip.input": (
            "id",
            "name",
            "payslip_id",
            "sequence",
            "input_type_id",
            "code",
            "amount",
            "contract_id",
            "write_date",
        ),
        "hr.payslip.input.type": _INPUT_TYPE_FIELDS,
        "hr.payroll.structure": _STRUCTURE_FIELDS,
        "hr.employee": _EMPLOYEE_FIELDS,
        "hr.contract": (
            "id",
            "employee_id",
            "company_id",
            "active",
            "state",
            "date_start",
            "date_end",
            "wage",
            "currency_id",
            "structure_type_id",
            "resource_calendar_id",
            "department_id",
            "job_id",
            "contract_type_id",
            "write_date",
        ),
        "hr.work.entry": (
            "id",
            "employee_id",
            "company_id",
            "date_start",
            "date_stop",
            "duration",
            "work_entry_type_id",
            "code",
            "state",
            "conflict",
            "write_date",
        ),
    },
    19: {
        "hr.payslip": (
            "id",
            "name",
            "employee_id",
            "date_from",
            "date_to",
            "state",
            "company_id",
            "version_id",
            "struct_id",
            "payslip_run_id",
            "credit_note",
            "currency_id",
            "write_date",
        ),
        "hr.payslip.run": _BATCH_FIELDS,
        "hr.payslip.line": (
            "id",
            "slip_id",
            "salary_rule_id",
            "employee_id",
            "version_id",
            "name",
            "code",
            "category_id",
            "sequence",
            "quantity",
            "rate",
            "amount",
            "total",
            "currency_id",
            "write_date",
        ),
        "hr.payslip.worked_days": (
            "id",
            "payslip_id",
            "version_id",
            "work_entry_type_id",
            "name",
            "code",
            "number_of_days",
            "number_of_hours",
            "amount",
            "currency_id",
            "write_date",
        ),
        "hr.payslip.input": (
            "id",
            "name",
            "payslip_id",
            "sequence",
            "input_type_id",
            "code",
            "amount",
            "version_id",
            "write_date",
        ),
        "hr.payslip.input.type": _INPUT_TYPE_FIELDS,
        "hr.payroll.structure": _STRUCTURE_FIELDS,
        "hr.employee": _EMPLOYEE_FIELDS,
        "hr.version": (
            "id",
            "employee_id",
            "company_id",
            "active",
            "date_version",
            "contract_date_start",
            "contract_date_end",
            "wage",
            "currency_id",
            "structure_type_id",
            "resource_calendar_id",
            "department_id",
            "job_id",
            "contract_type_id",
            "write_date",
        ),
        "hr.work.entry": (
            "id",
            "employee_id",
            "company_id",
            "version_id",
            "date",
            "duration",
            "work_entry_type_id",
            "code",
            "state",
            "conflict",
            "write_date",
        ),
    },
}

PAYSLIP_STATE_MAP: dict[int, dict[str, PayrollState]] = {
    18: {
        "draft": "draft",
        "verify": "waiting",
        "done": "done",
        "paid": "paid",
        "cancel": "cancelled",
    },
    19: {
        "draft": "draft",
        "validated": "done",
        "paid": "paid",
        "cancel": "cancelled",
    },
}

BATCH_STATE_MAP: dict[int, dict[str, PayrollBatchState]] = {
    18: {"draft": "draft", "verify": "ready", "close": "done", "paid": "paid"},
    19: {
        "01_ready": "ready",
        "02_close": "done",
        "03_paid": "paid",
        "04_cancel": "cancelled",
    },
}

WORK_ENTRY_STATE_MAP: dict[str, PayrollWorkEntryState] = {
    "draft": "draft",
    "conflict": "conflict",
    "validated": "validated",
    "cancelled": "cancelled",
}

_SOURCE_PAGE_SIZE = 500


def _api_error(message: str = "Odoo returned invalid Payroll data.") -> OdooMcpError:
    return OdooMcpError(
        ErrorCode.ODOO_API_ERROR,
        message,
        "Check Odoo Payroll compatibility, data integrity, and access, then retry.",
    )


def _inconsistent() -> OdooMcpError:
    return OdooMcpError(
        ErrorCode.PAYROLL_SOURCE_INCONSISTENT,
        "Odoo returned inconsistent Payroll source data.",
        "Check the requested company, relationships, and Payroll records, then retry.",
    )


def _too_large() -> OdooMcpError:
    return OdooMcpError(
        ErrorCode.PAYROLL_RESULT_TOO_LARGE,
        "The Payroll source exceeds the safe processing bound.",
        "Narrow the Payroll period or identifiers and retry.",
    )


def _state_conflict() -> OdooMcpError:
    return OdooMcpError(
        ErrorCode.ODOO_STATE_CONFLICT,
        "The Payroll source changed while it was being read.",
        "Retry against fresh Odoo Payroll state.",
    )


def _require_requested_id(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            f"The Payroll {label} ID is invalid.",
            f"Use a positive {label} ID from the authorized company.",
        )
    return value


def _exact_record(value: object, fields: Sequence[str]) -> RawRecord:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise _api_error()
    keys = set(value)
    expected = set(fields)
    if keys != expected or any(key.casefold() in FIELD_DENYLIST for key in keys):
        raise _api_error()
    return cast(RawRecord, value)


def _positive_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _api_error()
    return value


def _non_negative_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _api_error()
    return value


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise _api_error()
    return value


def _optional_text(value: object) -> str | None:
    if value is False or value is None:
        return None
    return _text(value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise _api_error()
    return value


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise _api_error()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _api_error() from None


def _optional_date(value: object) -> date | None:
    if value is False or value is None:
        return None
    return _date(value)


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise _api_error()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _api_error() from None


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise _api_error()
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise _api_error() from None
    if not result.is_finite():
        raise _api_error()
    return result


def _relation(value: object) -> RelatedRecord:
    if not isinstance(value, (list, tuple)) or len(value) != 2 or not isinstance(value[1], str):
        raise _api_error()
    return RelatedRecord(id=_positive_int(value[0]), name=value[1])


def _optional_relation(value: object) -> RelatedRecord | None:
    if value is False or value is None:
        return None
    return _relation(value)


def _many_ids(value: object, *, maximum: int) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise _api_error()
    result = tuple(_positive_int(item) for item in value)
    if len(result) != len(set(result)):
        raise _api_error()
    return result


def _created_id(value: object) -> int:
    if isinstance(value, list) and len(value) == 1:
        return _positive_int(value[0])
    return _positive_int(value)


def _source_states(selected: Sequence[StateT], mapping: Mapping[str, StateT]) -> list[str]:
    return [raw for raw, normalized in mapping.items() if normalized in selected]


def _cursor_fingerprint(
    version: int,
    company_id: int,
    model: str,
    domain: list[object],
    order: str,
    binding: object,
    anchors: list[tuple[int, str]],
) -> str:
    canonical = json.dumps(
        {
            "version": version,
            "company_id": company_id,
            "model": model,
            "domain": domain,
            "order": order,
            "binding": binding,
            "anchors": anchors,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _decode_cursor(cursor: str | None, fingerprint: str) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode()
        version, bound, raw_offset = raw.split(":")
        offset = int(raw_offset)
    except (ValueError, UnicodeError, binascii.Error):
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The Payroll pagination cursor is invalid.",
            "Restart the Payroll request without a cursor.",
        ) from None
    if version != "v1" or bound != fingerprint or offset <= 0:
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The Payroll pagination cursor does not match this request.",
            "Restart the Payroll request without a cursor.",
        )
    return offset


def _encode_cursor(fingerprint: str, offset: int) -> str:
    return base64.urlsafe_b64encode(f"v1:{fingerprint}:{offset}".encode()).decode().rstrip("=")


class PayrollReader:
    """Odoo-owned Payroll implementation used by the concrete client."""

    def __init__(
        self,
        transport: OdooTransport,
        version: int,
        validated_company_ids: Callable[[], tuple[int, ...] | None],
    ) -> None:
        if version not in PAYROLL_MODEL_FIELDS_BY_VERSION:
            raise OdooMcpError(
                ErrorCode.ODOO_VERSION_UNSUPPORTED,
                "The detected Odoo version is unsupported.",
                "Use Odoo Enterprise 18 or 19.",
            )
        self._transport = transport
        self._version = version
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

    def _fields(self, model: str) -> tuple[str, ...]:
        ensure_payroll_model_read_allowed(self._version, model)
        fields = PAYROLL_MODEL_FIELDS_BY_VERSION[self._version].get(model)
        if fields is None:
            raise _api_error("The Payroll model is unavailable for this Odoo version.")
        return fields

    async def _fetch_rows(
        self,
        model: str,
        company_id: int,
        domain: list[object],
        fields: tuple[str, ...],
        order: str,
        count: int,
    ) -> list[RawRecord]:
        rows: list[RawRecord] = []
        seen: set[int] = set()
        if count == 0:
            page = await self._transport.search_read(
                model,
                domain,
                list(fields),
                limit=1,
                offset=0,
                order=order,
                company_ids=(company_id,),
            )
            if page:
                raise _state_conflict()
            return rows
        while len(rows) < count:
            requested = min(_SOURCE_PAGE_SIZE, count - len(rows))
            page = await self._transport.search_read(
                model,
                domain,
                list(fields),
                limit=requested,
                offset=len(rows),
                order=order,
                company_ids=(company_id,),
            )
            if len(page) != requested:
                current = await self._transport.search_count(
                    model, domain, company_ids=(company_id,)
                )
                if current != count:
                    raise _state_conflict()
                raise _api_error("Odoo returned an incomplete Payroll source page.")
            for value in page:
                row = _exact_record(value, fields)
                identifier = _positive_int(row.get("id"))
                if identifier in seen:
                    raise _api_error("Odoo returned duplicate Payroll source rows.")
                seen.add(identifier)
                rows.append(row)
        return rows

    async def _stable_rows(
        self,
        model: str,
        company_id: int,
        domain: list[object],
        order: str,
        *,
        cap: int | None = None,
    ) -> list[RawRecord]:
        self._require_company(company_id)
        fields = self._fields(model)
        maximum = cap if cap is not None else PAYROLL_SOURCE_CAPS[model]
        count = await self._transport.search_count(model, domain, company_ids=(company_id,))
        if count < 0:
            raise _api_error()
        if count > maximum:
            raise _too_large()
        rows = await self._fetch_rows(model, company_id, domain, fields, order, count)
        anchors = [(_positive_int(row.get("id")), _datetime(row.get("write_date"))) for row in rows]
        final_count = await self._transport.search_count(model, domain, company_ids=(company_id,))
        if final_count != count:
            raise _state_conflict()
        final_rows = await self._fetch_rows(
            model,
            company_id,
            domain,
            ("id", "write_date"),
            order,
            final_count,
        )
        final_anchors = [
            (_positive_int(row.get("id")), _datetime(row.get("write_date"))) for row in final_rows
        ]
        if final_anchors != anchors:
            raise _state_conflict()
        return rows

    async def verify_schema(self, company_id: int) -> None:
        """Validate the fixed Payroll field contract without reading business rows."""

        for model in PAYROLL_MODEL_FIELDS_BY_VERSION[self._version]:
            rows = await self._stable_rows(
                model,
                company_id,
                [["id", "=", -1]],
                "id asc",
                cap=1,
            )
            if rows:
                raise _api_error("The Payroll schema qualification returned source data.")

    def _page(
        self,
        items: list[ItemT],
        company_id: int,
        page: PayrollPageRequest,
        model: str,
        domain: list[object],
        order: str,
        *,
        binding: object = None,
    ) -> PayrollPage[ItemT]:
        anchors: list[tuple[int, str]] = []
        for item in items:
            identifier = getattr(item, "id", None)
            write_date = getattr(item, "write_date", None)
            if (
                not isinstance(identifier, int)
                or isinstance(identifier, bool)
                or identifier <= 0
                or not isinstance(write_date, datetime)
            ):
                raise _api_error()
            anchors.append((identifier, write_date.isoformat()))
        fingerprint = _cursor_fingerprint(
            self._version,
            company_id,
            model,
            domain,
            order,
            binding,
            anchors,
        )
        offset = _decode_cursor(page.cursor, fingerprint)
        if offset > len(items):
            raise OdooMcpError(
                ErrorCode.INVALID_INPUT,
                "The Payroll pagination cursor is outside this result.",
                "Restart the Payroll request without a cursor.",
            )
        selected = items[offset : offset + page.limit]
        next_offset = offset + len(selected)
        return PayrollPage[ItemT](
            items=selected,
            total_count=len(items),
            next_cursor=(
                _encode_cursor(fingerprint, next_offset) if next_offset < len(items) else None
            ),
        )

    def _payslip_state(self, raw: object) -> tuple[str, PayrollState]:
        source = _text(raw)
        normalized = PAYSLIP_STATE_MAP[self._version].get(source)
        if normalized is None:
            raise _api_error("Odoo returned an unknown Payroll payslip state.")
        return source, normalized

    def _batch_state(self, raw: object) -> tuple[str, PayrollBatchState]:
        source = _text(raw)
        normalized = BATCH_STATE_MAP[self._version].get(source)
        if normalized is None:
            raise _api_error("Odoo returned an unknown Payroll batch state.")
        return source, normalized

    def _work_entry_state(self, raw: object) -> PayrollWorkEntryState:
        source = _text(raw)
        normalized = WORK_ENTRY_STATE_MAP.get(source)
        if normalized is None:
            raise _api_error("Odoo returned an unknown Payroll work-entry state.")
        return normalized

    def _normalize_batch(self, row: RawRecord) -> PayrollBatch:
        source_state, state = self._batch_state(row.get("state"))
        start = _date(row.get("date_start"))
        end = _date(row.get("date_end"))
        if end < start:
            raise _inconsistent()
        return PayrollBatch(
            id=_positive_int(row.get("id")),
            name=_text(row.get("name")),
            date_start=start,
            date_end=end,
            state=state,
            source_state=source_state,
            company_id=_relation(row.get("company_id")).id,
            write_date=_datetime(row.get("write_date")),
        )

    def _normalize_payslip(self, row: RawRecord) -> Payslip:
        source_state, state = self._payslip_state(row.get("state"))
        start = _date(row.get("date_from"))
        end = _date(row.get("date_to"))
        if end < start:
            raise _inconsistent()
        contract_field = "contract_id" if self._version == 18 else "version_id"
        contract_source: Literal["hr.contract", "hr.version"] = (
            "hr.contract" if self._version == 18 else "hr.version"
        )
        name = _text(row.get("name"))
        reference = _optional_text(row.get("number")) or name if self._version == 18 else name
        return Payslip(
            id=_positive_int(row.get("id")),
            name=name,
            reference=reference,
            employee=_relation(row.get("employee_id")),
            date_from=start,
            date_to=end,
            state=state,
            source_state=source_state,
            company_id=_relation(row.get("company_id")).id,
            contract_segment=_relation(row.get(contract_field)),
            contract_source=contract_source,
            structure=_relation(row.get("struct_id")),
            batch=_optional_relation(row.get("payslip_run_id")),
            credit_note=_boolean(row.get("credit_note")),
            currency=_relation(row.get("currency_id")),
            write_date=_datetime(row.get("write_date")),
        )

    def _normalize_line(self, row: RawRecord) -> PayslipLine:
        contract_field = "contract_id" if self._version == 18 else "version_id"
        contract_source: Literal["hr.contract", "hr.version"] = (
            "hr.contract" if self._version == 18 else "hr.version"
        )
        return PayslipLine(
            id=_positive_int(row.get("id")),
            payslip=_relation(row.get("slip_id")),
            salary_rule=_relation(row.get("salary_rule_id")),
            employee=_relation(row.get("employee_id")),
            contract_segment=_relation(row.get(contract_field)),
            contract_source=contract_source,
            name=_text(row.get("name")),
            code=_text(row.get("code")),
            category=_relation(row.get("category_id")),
            sequence=_non_negative_int(row.get("sequence")),
            quantity=_decimal(row.get("quantity")),
            rate=_decimal(row.get("rate")),
            amount=_decimal(row.get("amount")),
            total=_decimal(row.get("total")),
            currency=_relation(row.get("currency_id")),
            write_date=_datetime(row.get("write_date")),
        )

    def _normalize_worked_day(self, row: RawRecord) -> PayslipWorkedDay:
        contract_field = "contract_id" if self._version == 18 else "version_id"
        contract_source: Literal["hr.contract", "hr.version"] = (
            "hr.contract" if self._version == 18 else "hr.version"
        )
        return PayslipWorkedDay(
            id=_positive_int(row.get("id")),
            payslip=_relation(row.get("payslip_id")),
            contract_segment=_relation(row.get(contract_field)),
            contract_source=contract_source,
            work_entry_type=_relation(row.get("work_entry_type_id")),
            name=_text(row.get("name")),
            code=_text(row.get("code")),
            number_of_days=_decimal(row.get("number_of_days")),
            number_of_hours=_decimal(row.get("number_of_hours")),
            amount=_decimal(row.get("amount")),
            currency=_relation(row.get("currency_id")),
            write_date=_datetime(row.get("write_date")),
        )

    def _normalize_input(self, row: RawRecord) -> PayslipInput:
        contract_field = "contract_id" if self._version == 18 else "version_id"
        contract_source: Literal["hr.contract", "hr.version"] = (
            "hr.contract" if self._version == 18 else "hr.version"
        )
        return PayslipInput(
            id=_positive_int(row.get("id")),
            name=_text(row.get("name")),
            payslip=_relation(row.get("payslip_id")),
            sequence=_non_negative_int(row.get("sequence")),
            input_type=_relation(row.get("input_type_id")),
            code=_text(row.get("code")),
            amount=_decimal(row.get("amount")),
            contract_segment=_relation(row.get(contract_field)),
            contract_source=contract_source,
            write_date=_datetime(row.get("write_date")),
        )

    def _normalize_input_type(self, row: RawRecord) -> PayrollInputType:
        return PayrollInputType(
            id=_positive_int(row.get("id")),
            name=_text(row.get("name")),
            code=_text(row.get("code")),
            structure_ids=_many_ids(row.get("struct_ids"), maximum=5_000),
            active=_boolean(row.get("active")),
            is_quantity=_boolean(row.get("is_quantity")),
            available_in_attachments=_boolean(row.get("available_in_attachments")),
            write_date=_datetime(row.get("write_date")),
        )

    def _normalize_structure(self, row: RawRecord) -> PayrollStructure:
        return PayrollStructure(
            id=_positive_int(row.get("id")),
            input_line_type_ids=_many_ids(row.get("input_line_type_ids"), maximum=5_000),
            write_date=_datetime(row.get("write_date")),
        )

    def _normalize_employee(self, row: RawRecord) -> PayrollEmployee:
        return PayrollEmployee(
            id=_positive_int(row.get("id")),
            name=_text(row.get("name")),
            active=_boolean(row.get("active")),
            company_id=_relation(row.get("company_id")).id,
            write_date=_datetime(row.get("write_date")),
        )

    async def get_payroll_batches(
        self,
        company_id: int,
        filters: PayrollBatchFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollBatch]:
        domain: list[object] = [["company_id", "=", company_id]]
        if filters.batch_ids:
            domain.append(["id", "in", list(filters.batch_ids)])
        if filters.window is not None:
            domain.extend(
                [
                    ["date_start", ">=", filters.window.start.isoformat()],
                    ["date_end", "<=", filters.window.end.isoformat()],
                ]
            )
        if filters.states:
            domain.append(
                [
                    "state",
                    "in",
                    _source_states(filters.states, BATCH_STATE_MAP[self._version]),
                ]
            )
        order = "date_start desc, date_end desc, id desc"
        rows = await self._stable_rows("hr.payslip.run", company_id, domain, order)
        items = [self._normalize_batch(row) for row in rows]
        items.sort(key=lambda item: (item.date_start, item.date_end, item.id), reverse=True)
        if any(
            item.company_id != company_id
            or (filters.batch_ids and item.id not in filters.batch_ids)
            or (
                filters.window is not None
                and (item.date_start < filters.window.start or item.date_end > filters.window.end)
            )
            or (filters.states and item.state not in filters.states)
            for item in items
        ):
            raise _inconsistent()
        return self._page(items, company_id, page, "hr.payslip.run", domain, order)

    async def get_payslips(
        self,
        company_id: int,
        filters: PayslipFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[Payslip]:
        domain: list[object] = [["company_id", "=", company_id]]
        if filters.payslip_ids:
            domain.append(["id", "in", list(filters.payslip_ids)])
        if filters.batch_id is not None:
            domain.append(["payslip_run_id", "=", filters.batch_id])
        if filters.period is not None:
            domain.extend(
                [
                    ["date_from", "=", filters.period.start.isoformat()],
                    ["date_to", "=", filters.period.end.isoformat()],
                ]
            )
        if filters.window is not None:
            domain.extend(
                [
                    ["date_from", ">=", filters.window.start.isoformat()],
                    ["date_to", "<=", filters.window.end.isoformat()],
                ]
            )
        if filters.employee_ids:
            domain.append(["employee_id", "in", list(filters.employee_ids)])
        if filters.states:
            domain.append(
                [
                    "state",
                    "in",
                    _source_states(filters.states, PAYSLIP_STATE_MAP[self._version]),
                ]
            )
        order = "date_from desc, date_to desc, id desc"
        rows = await self._stable_rows("hr.payslip", company_id, domain, order)
        items = [self._normalize_payslip(row) for row in rows]
        items.sort(key=lambda item: (item.date_from, item.date_to, item.id), reverse=True)
        for item in items:
            if (
                item.company_id != company_id
                or (filters.payslip_ids and item.id not in filters.payslip_ids)
                or (
                    filters.batch_id is not None
                    and (item.batch is None or item.batch.id != filters.batch_id)
                )
                or (
                    filters.period is not None
                    and (
                        item.date_from != filters.period.start or item.date_to != filters.period.end
                    )
                )
                or (
                    filters.window is not None
                    and (item.date_from < filters.window.start or item.date_to > filters.window.end)
                )
                or (filters.employee_ids and item.employee.id not in filters.employee_ids)
                or (filters.states and item.state not in filters.states)
            ):
                raise _inconsistent()
        return self._page(items, company_id, page, "hr.payslip", domain, order)

    async def _children(
        self,
        model: str,
        parent_field: str,
        order: str,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest,
        normalize: Callable[[RawRecord], ChildT],
    ) -> PayrollPage[ChildT]:
        domain: list[object] = [
            [f"{parent_field}.company_id", "=", company_id],
            [parent_field, "in", list(filters.payslip_ids)],
        ]
        if filters.record_ids:
            domain.append(["id", "in", list(filters.record_ids)])
        rows = await self._stable_rows(model, company_id, domain, order)
        items = [normalize(row) for row in rows]
        if model == "hr.payslip.worked_days":
            items.sort(key=lambda item: item.id)
        else:
            items.sort(
                key=lambda item: (
                    item.sequence if isinstance(item, (PayslipLine, PayslipInput)) else 0,
                    item.id,
                )
            )
        parent_ids = tuple(sorted({item.payslip.id for item in items}))
        parents: dict[int, Payslip] = {}
        if parent_ids:
            parent_page = await self.get_payslips(
                company_id,
                PayslipFilters(payslip_ids=parent_ids),
                PayrollPageRequest(limit=min(len(parent_ids), 200)),
            )
            parents = {parent.id: parent for parent in parent_page.items}
            if set(parents) != set(parent_ids) or parent_page.next_cursor is not None:
                raise _inconsistent()
        for item in items:
            parent = item.payslip
            source_parent = parents.get(parent.id)
            if (
                source_parent is None
                or parent.id not in filters.payslip_ids
                or (filters.record_ids and item.id not in filters.record_ids)
                or (
                    isinstance(item, PayslipLine)
                    and (
                        item.employee.id != source_parent.employee.id
                        or item.contract_segment.id != source_parent.contract_segment.id
                        or item.currency.id != source_parent.currency.id
                    )
                )
                or (
                    isinstance(item, PayslipWorkedDay)
                    and (
                        item.contract_segment.id != source_parent.contract_segment.id
                        or item.currency.id != source_parent.currency.id
                    )
                )
                or (
                    isinstance(item, PayslipInput)
                    and item.contract_segment.id != source_parent.contract_segment.id
                )
            ):
                raise _inconsistent()
        return self._page(items, company_id, page, model, domain, order)

    async def get_payslip_lines(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayslipLine]:
        return await self._children(
            "hr.payslip.line",
            "slip_id",
            "sequence asc, id asc",
            company_id,
            filters,
            page,
            self._normalize_line,
        )

    async def get_payslip_worked_days(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayslipWorkedDay]:
        return await self._children(
            "hr.payslip.worked_days",
            "payslip_id",
            "id asc",
            company_id,
            filters,
            page,
            self._normalize_worked_day,
        )

    async def get_payslip_inputs(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayslipInput]:
        return await self._children(
            "hr.payslip.input",
            "payslip_id",
            "sequence asc, id asc",
            company_id,
            filters,
            page,
            self._normalize_input,
        )

    async def get_payroll_structure(self, company_id: int, structure_id: int) -> PayrollStructure:
        domain: list[object] = [["id", "=", structure_id]]
        rows = await self._stable_rows("hr.payroll.structure", company_id, domain, "id asc", cap=1)
        if not rows:
            raise OdooMcpError(
                ErrorCode.PAYROLL_STRUCTURE_NOT_FOUND,
                "The requested Payroll structure is unavailable.",
                "Use the exact structure from the authorized payslip.",
            )
        structure = self._normalize_structure(rows[0])
        if structure.id != structure_id:
            raise _inconsistent()
        return structure

    async def _structure(self, company_id: int, structure_id: int) -> PayrollStructure:
        return await self.get_payroll_structure(company_id, structure_id)

    async def get_payroll_input_types(
        self,
        company_id: int,
        filters: PayrollInputTypeFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollInputType]:
        structure = await self._structure(company_id, filters.structure_id)
        eligible = structure.input_line_type_ids
        domain: list[object] = [
            ["id", "in", list(eligible)],
            ["active", "=", True],
            ["available_in_attachments", "=", False],
        ]
        if filters.input_type_ids:
            domain.append(["id", "in", list(filters.input_type_ids)])
        order = "code asc, id asc"
        rows = await self._stable_rows("hr.payslip.input.type", company_id, domain, order)
        items = [self._normalize_input_type(row) for row in rows]
        items.sort(key=lambda item: (item.code, item.id))
        if any(
            item.id not in eligible
            or filters.structure_id not in item.structure_ids
            or not item.active
            or item.available_in_attachments
            or (filters.input_type_ids and item.id not in filters.input_type_ids)
            for item in items
        ):
            raise _inconsistent()
        return self._page(items, company_id, page, "hr.payslip.input.type", domain, order)

    async def get_payroll_employees(
        self,
        company_id: int,
        employee_ids: tuple[int, ...],
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollEmployee]:
        if (
            not employee_ids
            or len(employee_ids) > 100
            or len(employee_ids) != len(set(employee_ids))
            or any(
                not isinstance(identifier, int) or isinstance(identifier, bool) or identifier <= 0
                for identifier in employee_ids
            )
        ):
            raise OdooMcpError(
                ErrorCode.INVALID_INPUT,
                "The Payroll employee selection is invalid.",
                "Use one to 100 unique positive employee IDs.",
            )
        domain: list[object] = [
            ["company_id", "=", company_id],
            ["id", "in", list(employee_ids)],
        ]
        order = "id asc"
        rows = await self._stable_rows("hr.employee", company_id, domain, order)
        items = [self._normalize_employee(row) for row in rows]
        items.sort(key=lambda item: item.id)
        if any(item.company_id != company_id or item.id not in employee_ids for item in items):
            raise _inconsistent()
        return self._page(items, company_id, page, "hr.employee", domain, order)

    def _contract_common(
        self,
        row: RawRecord,
        *,
        source_model: Literal["hr.contract", "hr.version"],
        source_status: Literal["draft", "open", "close", "cancel", "unavailable"],
        revision_date: date | None,
        effective_start: date,
        effective_end: date,
    ) -> PayrollContractSegment:
        return PayrollContractSegment(
            source_model=source_model,
            id=_positive_int(row.get("id")),
            employee=_relation(row.get("employee_id")),
            company_id=_relation(row.get("company_id")).id,
            active=_boolean(row.get("active")),
            source_status=source_status,
            revision_date=revision_date,
            effective_start=effective_start,
            effective_end=effective_end,
            wage=_decimal(row.get("wage")),
            currency=_relation(row.get("currency_id")),
            structure_type=_optional_relation(row.get("structure_type_id")),
            resource_calendar=_optional_relation(row.get("resource_calendar_id")),
            department=_optional_relation(row.get("department_id")),
            job=_optional_relation(row.get("job_id")),
            contract_type=_optional_relation(row.get("contract_type_id")),
            write_date=_datetime(row.get("write_date")),
        )

    def _contract_segments_18(
        self, rows: list[RawRecord], filters: PayrollContractFilters
    ) -> list[PayrollContractSegment]:
        items: list[PayrollContractSegment] = []
        for row in rows:
            start = _date(row.get("date_start"))
            source_end = _optional_date(row.get("date_end"))
            if source_end is not None and source_end < start:
                raise _inconsistent()
            if source_end is not None and source_end < filters.period.start:
                raise _inconsistent()
            state = _text(row.get("state"))
            if state not in {"draft", "open", "close", "cancel"}:
                raise _api_error("Odoo returned an unknown Payroll contract state.")
            item = self._contract_common(
                row,
                source_model="hr.contract",
                source_status=cast(Literal["draft", "open", "close", "cancel"], state),
                revision_date=None,
                effective_start=max(start, filters.period.start),
                effective_end=min(source_end or filters.period.end, filters.period.end),
            )
            items.append(item)
        return items

    def _contract_segments_19(
        self, rows: list[RawRecord], filters: PayrollContractFilters
    ) -> list[PayrollContractSegment]:
        by_employee: dict[int, list[RawRecord]] = defaultdict(list)
        for row in rows:
            by_employee[_relation(row.get("employee_id")).id].append(row)
        items: list[PayrollContractSegment] = []
        for employee_rows in by_employee.values():
            revisions = [
                (_date(row.get("date_version")), _positive_int(row.get("id")))
                for row in employee_rows
            ]
            if len({value[0] for value in revisions}) != len(revisions):
                raise _inconsistent()
            ordered = sorted(
                employee_rows,
                key=lambda row: (
                    _date(row.get("date_version")),
                    _positive_int(row.get("id")),
                ),
            )
            for index, row in enumerate(ordered):
                revision = _date(row.get("date_version"))
                contract_start = _date(row.get("contract_date_start"))
                contract_end = _optional_date(row.get("contract_date_end"))
                start = max(revision, contract_start)
                next_revision = (
                    _date(ordered[index + 1].get("date_version"))
                    if index + 1 < len(ordered)
                    else None
                )
                source_end = contract_end
                if next_revision is not None:
                    preceding = next_revision - timedelta(days=1)
                    source_end = min(source_end, preceding) if source_end else preceding
                if source_end is not None and source_end < start:
                    raise _inconsistent()
                overlaps = not (
                    start > filters.period.end
                    or (source_end is not None and source_end < filters.period.start)
                )
                item = self._contract_common(
                    row,
                    source_model="hr.version",
                    source_status="unavailable",
                    revision_date=revision,
                    effective_start=max(start, filters.period.start),
                    effective_end=min(source_end or filters.period.end, filters.period.end),
                )
                if overlaps:
                    items.append(item)
        return items

    async def get_payroll_contract_segments(
        self,
        company_id: int,
        filters: PayrollContractFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollContractSegment]:
        if self._version == 18:
            model = "hr.contract"
            domain: list[object] = [
                ["company_id", "=", company_id],
                ["employee_id", "in", list(filters.employee_ids)],
                ["date_start", "<=", filters.period.end.isoformat()],
                "|",
                ["date_end", "=", False],
                ["date_end", ">=", filters.period.start.isoformat()],
            ]
            order = "employee_id asc, date_start asc, id asc"
        else:
            model = "hr.version"
            domain = [
                ["company_id", "=", company_id],
                ["employee_id", "in", list(filters.employee_ids)],
                ["date_version", "<=", filters.period.end.isoformat()],
            ]
            order = "employee_id asc, date_version asc, id asc"
        rows = await self._stable_rows(model, company_id, domain, order)
        for row in rows:
            if (
                _relation(row.get("company_id")).id != company_id
                or _relation(row.get("employee_id")).id not in filters.employee_ids
            ):
                raise _inconsistent()
        items = (
            self._contract_segments_18(rows, filters)
            if self._version == 18
            else self._contract_segments_19(rows, filters)
        )
        items.sort(key=lambda item: (item.employee.id, item.effective_start, item.id))
        previous: dict[int, PayrollContractSegment] = {}
        for item in items:
            if (
                item.company_id != company_id
                or item.employee.id not in filters.employee_ids
                or item.effective_start > item.effective_end
            ):
                raise _inconsistent()
            prior = previous.get(item.employee.id)
            if prior is not None and prior.effective_end >= item.effective_start:
                raise _inconsistent()
            previous[item.employee.id] = item
        return self._page(
            items,
            company_id,
            page,
            model,
            domain,
            order,
            binding=filters.model_dump(mode="json"),
        )

    async def get_payroll_work_entries(
        self,
        company_id: int,
        filters: PayrollWorkEntryFilters,
        page: PayrollPageRequest = DEFAULT_PAYROLL_PAGE_REQUEST,
    ) -> PayrollPage[PayrollWorkEntry]:
        domain: list[object] = [
            ["company_id", "=", company_id],
            ["employee_id", "in", list(filters.employee_ids)],
        ]
        if self._version == 18:
            domain.extend(
                [
                    [
                        "date_start",
                        "<=",
                        datetime.combine(filters.period.end, time(23, 59, 59)).isoformat(sep=" "),
                    ],
                    [
                        "date_stop",
                        ">=",
                        datetime.combine(filters.period.start, time.min).isoformat(sep=" "),
                    ],
                ]
            )
            order = "date_start asc, id asc"
        else:
            domain.extend(
                [
                    ["date", ">=", filters.period.start.isoformat()],
                    ["date", "<=", filters.period.end.isoformat()],
                ]
            )
            order = "date asc, id asc"
        if filters.states:
            domain.append(["state", "in", _source_states(filters.states, WORK_ENTRY_STATE_MAP)])
        rows = await self._stable_rows("hr.work.entry", company_id, domain, order)
        items: list[PayrollWorkEntry] = []
        for row in rows:
            if self._version == 18:
                start_value = _datetime(row.get("date_start"))
                end_value = _datetime(row.get("date_stop"))
                if end_value < start_value:
                    raise _inconsistent()
                start = start_value.date()
                end = end_value.date()
                segment = None
                source: Literal["hr.version"] | None = None
            else:
                start = end = _date(row.get("date"))
                segment = _relation(row.get("version_id"))
                source = "hr.version"
            duration = _decimal(row.get("duration"))
            if duration < 0:
                raise _inconsistent()
            item = PayrollWorkEntry(
                id=_positive_int(row.get("id")),
                employee=_relation(row.get("employee_id")),
                company_id=_relation(row.get("company_id")).id,
                contract_segment=segment,
                contract_source=source,
                date_start=start,
                date_end=end,
                duration=duration,
                work_entry_type=_relation(row.get("work_entry_type_id")),
                code=_text(row.get("code")),
                state=self._work_entry_state(row.get("state")),
                conflict=_boolean(row.get("conflict")),
                write_date=_datetime(row.get("write_date")),
            )
            if (
                item.company_id != company_id
                or item.employee.id not in filters.employee_ids
                or item.date_end < filters.period.start
                or item.date_start > filters.period.end
                or (filters.states and item.state not in filters.states)
            ):
                raise _inconsistent()
            items.append(item)
        items.sort(key=lambda item: (item.date_start, item.id))
        return self._page(items, company_id, page, "hr.work.entry", domain, order)

    async def _exact_payslip(self, company_id: int, payslip_id: int) -> Payslip:
        payslip_id = _require_requested_id(payslip_id, "payslip")
        page = await self.get_payslips(
            company_id,
            PayslipFilters(payslip_ids=(payslip_id,)),
            PayrollPageRequest(limit=1),
        )
        if len(page.items) != 1 or page.items[0].id != payslip_id:
            raise OdooMcpError(
                ErrorCode.PAYSLIP_NOT_FOUND,
                "The requested payslip is unavailable.",
                "Use an exact payslip ID from the authorized company.",
            )
        return page.items[0]

    def _require_editable(self, payslip: Payslip) -> None:
        editable = (
            payslip.source_state in {"draft", "verify"}
            if self._version == 18
            else payslip.source_state == "draft"
        )
        if not editable:
            raise OdooMcpError(
                ErrorCode.PAYROLL_STATE_NOT_EDITABLE,
                "The requested payslip is not editable.",
                "Use an Odoo draft payslip and retry.",
            )

    async def _all_inputs(self, company_id: int, payslip_id: int) -> list[PayslipInput]:
        page = await self.get_payslip_inputs(
            company_id,
            PayslipChildFilters(payslip_ids=(payslip_id,)),
            PayrollPageRequest(limit=200),
        )
        if page.total_count > 200 or page.next_cursor is not None:
            raise _too_large()
        type_ids = [item.input_type.id for item in page.items]
        codes = [item.code for item in page.items]
        if len(type_ids) != len(set(type_ids)) or len(codes) != len(set(codes)):
            raise _inconsistent()
        return page.items

    async def _complete_write_children(
        self,
        fetch: Callable[[PayrollPageRequest], Awaitable[PayrollPage[ChildT]]],
        *,
        cap: int,
    ) -> tuple[ChildT, ...]:
        items: list[ChildT] = []
        seen_ids: set[int] = set()
        seen_cursors: set[str] = set()
        expected_total: int | None = None
        cursor: str | None = None
        while True:
            page = await fetch(PayrollPageRequest(limit=200, cursor=cursor))
            if expected_total is None:
                expected_total = page.total_count
                if expected_total > cap:
                    raise _too_large()
            elif page.total_count != expected_total:
                raise _inconsistent()
            for item in page.items:
                if item.id in seen_ids:
                    raise _inconsistent()
                seen_ids.add(item.id)
                items.append(item)
                if len(items) > cap or len(items) > expected_total:
                    raise _inconsistent()
            if page.next_cursor is None:
                if len(items) != expected_total:
                    raise _inconsistent()
                return tuple(items)
            if not page.items or page.next_cursor in seen_cursors:
                raise _inconsistent()
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor

    async def _read_write_checkpoint(
        self,
        company_id: int,
        expected: PayrollWriteCheckpoint,
        *,
        payslip_id: int,
        include_calculation_sources: bool,
    ) -> PayrollWriteCheckpoint:
        if (
            expected.version != self._version
            or expected.payslip.id != payslip_id
            or expected.payslip.company_id != company_id
            or expected.input_types_truncated
            or (
                not include_calculation_sources
                and (expected.calculated_lines or expected.worked_days)
            )
        ):
            raise _state_conflict()
        payslip = await self._action_payslip(company_id, payslip_id)
        inputs = tuple(await self._all_inputs(company_id, payslip_id))
        employees = await self.get_payroll_employees(
            company_id,
            (payslip.employee.id,),
            PayrollPageRequest(limit=1),
        )
        if len(employees.items) != 1 or employees.next_cursor is not None:
            raise _state_conflict()
        contracts = await self.get_payroll_contract_segments(
            company_id,
            PayrollContractFilters(
                employee_ids=(payslip.employee.id,),
                period=PayrollPeriod(start=payslip.date_from, end=payslip.date_to),
            ),
            PayrollPageRequest(limit=200),
        )
        matching_contracts = [
            item
            for item in contracts.items
            if item.id == payslip.contract_segment.id
            and item.source_model == payslip.contract_source
        ]
        if len(matching_contracts) != 1 or contracts.next_cursor is not None:
            raise _state_conflict()
        structure = await self._structure(company_id, payslip.structure.id)
        input_types = tuple(
            [
                await self._eligible_type(company_id, payslip, item.id)
                for item in expected.input_types
            ]
        )
        calculated_lines: tuple[PayslipLine, ...] = ()
        worked_days: tuple[PayslipWorkedDay, ...] = ()
        if include_calculation_sources:

            async def fetch_lines(page: PayrollPageRequest) -> PayrollPage[PayslipLine]:
                return await self.get_payslip_lines(
                    company_id,
                    PayslipChildFilters(payslip_ids=(payslip_id,)),
                    page,
                )

            async def fetch_worked_days(
                page: PayrollPageRequest,
            ) -> PayrollPage[PayslipWorkedDay]:
                return await self.get_payslip_worked_days(
                    company_id,
                    PayslipChildFilters(payslip_ids=(payslip_id,)),
                    page,
                )

            calculated_lines = await self._complete_write_children(
                fetch_lines,
                cap=_MAX_WRITE_CALCULATED_LINES,
            )
            worked_days = await self._complete_write_children(
                fetch_worked_days,
                cap=_MAX_WRITE_WORKED_DAYS,
            )
        return PayrollWriteCheckpoint(
            version=self._version,
            payslip=payslip,
            inputs=inputs,
            employee=employees.items[0],
            contract=matching_contracts[0],
            structure=structure,
            input_types=input_types,
            calculated_lines=calculated_lines,
            worked_days=worked_days,
        )

    async def _validated_write_checkpoint(
        self,
        company_id: int,
        expected: PayrollWriteCheckpoint,
        *,
        payslip_id: int,
        include_calculation_sources: bool = False,
    ) -> PayrollWriteCheckpoint:
        try:
            observed = await self._read_write_checkpoint(
                company_id,
                expected,
                payslip_id=payslip_id,
                include_calculation_sources=include_calculation_sources,
            )
            final = await self._read_write_checkpoint(
                company_id,
                expected,
                payslip_id=payslip_id,
                include_calculation_sources=include_calculation_sources,
            )
        except OdooMcpError as exc:
            if exc.code in _CHECKPOINT_CONFLICT_CODES:
                raise PayrollWriteRejected(_state_conflict()) from None
            raise
        if observed != expected or final != expected:
            raise PayrollWriteRejected(_state_conflict())
        return final

    @staticmethod
    def _selected_input(inputs: list[PayslipInput], input_id: int) -> PayslipInput:
        matches = [item for item in inputs if item.id == input_id]
        if len(matches) != 1:
            raise OdooMcpError(
                ErrorCode.PAYROLL_INPUT_NOT_FOUND,
                "The requested Payroll input is unavailable.",
                "Use an exact input ID from the editable payslip.",
            )
        return matches[0]

    async def _eligible_type(
        self, company_id: int, payslip: Payslip, input_type_id: int
    ) -> PayrollInputType:
        page = await self.get_payroll_input_types(
            company_id,
            PayrollInputTypeFilters(
                structure_id=payslip.structure.id, input_type_ids=(input_type_id,)
            ),
            PayrollPageRequest(limit=1),
        )
        if len(page.items) != 1 or page.items[0].id != input_type_id:
            raise OdooMcpError(
                ErrorCode.PAYROLL_INPUT_TYPE_NOT_ALLOWED,
                "The requested Payroll input type is not eligible.",
                "Use an active non-attachment type allowed by the payslip structure.",
            )
        return page.items[0]

    async def _exact_input(self, company_id: int, payslip_id: int, input_id: int) -> PayslipInput:
        payslip_id = _require_requested_id(payslip_id, "payslip")
        input_id = _require_requested_id(input_id, "input")
        page = await self.get_payslip_inputs(
            company_id,
            PayslipChildFilters(payslip_ids=(payslip_id,), record_ids=(input_id,)),
            PayrollPageRequest(limit=1),
        )
        if len(page.items) != 1 or page.items[0].id != input_id:
            raise OdooMcpError(
                ErrorCode.PAYROLL_INPUT_NOT_FOUND,
                "The requested Payroll input is unavailable.",
                "Use an exact input ID from the editable payslip.",
            )
        return page.items[0]

    async def _action_payslip(self, company_id: int, payslip_id: int) -> Payslip:
        payslip = await self._exact_payslip(company_id, payslip_id)
        self._require_editable(payslip)
        employees = await self.get_payroll_employees(
            company_id,
            (payslip.employee.id,),
            PayrollPageRequest(limit=1),
        )
        if len(employees.items) != 1 or employees.items[0].id != payslip.employee.id:
            raise OdooMcpError(
                ErrorCode.EMPLOYEE_NOT_FOUND,
                "The payslip employee is unavailable in the authorized company.",
                "Check the payslip and employee access in Odoo, then retry.",
            )
        segments = await self.get_payroll_contract_segments(
            company_id,
            PayrollContractFilters(
                employee_ids=(payslip.employee.id,),
                period=PayrollPeriod(start=payslip.date_from, end=payslip.date_to),
            ),
            PayrollPageRequest(limit=200),
        )
        if segments.next_cursor is not None:
            raise _too_large()
        if not any(
            segment.id == payslip.contract_segment.id
            and segment.source_model == payslip.contract_source
            for segment in segments.items
        ):
            raise OdooMcpError(
                ErrorCode.CONTRACT_DATA_MISSING,
                "The payslip contract evidence is unavailable.",
                "Check the payslip contract or version in Odoo, then retry.",
            )
        structure = await self._structure(company_id, payslip.structure.id)
        fresh = await self._exact_payslip(company_id, payslip_id)
        fresh_structure = await self._structure(company_id, payslip.structure.id)
        self._require_editable(fresh)
        if fresh != payslip or fresh_structure != structure:
            raise _state_conflict()
        return fresh

    @staticmethod
    def _payslip_scope(payslip: Payslip) -> tuple[object, ...]:
        return (
            payslip.id,
            payslip.employee.id,
            payslip.company_id,
            payslip.date_from,
            payslip.date_to,
            payslip.contract_segment.id,
            payslip.contract_source,
            payslip.structure.id,
            payslip.batch.id if payslip.batch is not None else None,
            payslip.credit_note,
            payslip.currency.id,
        )

    def _validate_post_action_parent(
        self,
        expected: Payslip,
        observed: Payslip,
        *,
        allow_state_change: bool,
    ) -> None:
        self._require_editable(observed)
        if self._payslip_scope(observed) != self._payslip_scope(expected) or (
            not allow_state_change and observed.source_state != expected.source_state
        ):
            raise _state_conflict()

    async def create_draft_payslip_input(
        self,
        company_id: int,
        payload: DraftPayslipInputCreate,
        expected: PayrollWriteCheckpoint,
    ) -> PayslipInput:
        checkpoint = await self._validated_write_checkpoint(
            company_id,
            expected,
            payslip_id=payload.payslip_id,
        )
        payslip = checkpoint.payslip
        current = list(checkpoint.inputs)
        matching_types = [
            item for item in checkpoint.input_types if item.id == payload.input_type_id
        ]
        if len(matching_types) != 1:
            raise PayrollWriteRejected(_state_conflict())
        input_type = matching_types[0]
        if any(
            item.input_type.id == input_type.id or item.code == input_type.code for item in current
        ):
            raise OdooMcpError(
                ErrorCode.PAYROLL_INPUT_TYPE_NOT_ALLOWED,
                "The payslip already contains this Payroll input type.",
                "Update the existing input instead of creating a duplicate.",
            )
        ensure_payroll_action_allowed(self._version, "hr.payslip.input", "create_draft_input")
        values: dict[str, object] = {
            "name": payload.description,
            "payslip_id": payslip.id,
            "input_type_id": input_type.id,
            "amount": str(payload.amount),
            "contract_id" if self._version == 18 else "version_id": payslip.contract_segment.id,
        }
        try:
            result = await self._transport.execute_method(
                "hr.payslip.input",
                "create",
                named={"vals_list": values},
                company_ids=(company_id,),
            )
        except OdooMcpError as exc:
            if exc.code is ErrorCode.ODOO_PERMISSION_DENIED:
                raise PayrollWriteRejected(exc) from None
            raise
        created_id = _created_id(result)
        created = await self._exact_input(company_id, payslip.id, created_id)
        if (
            created.name != payload.description
            or created.amount != payload.amount
            or created.input_type.id != input_type.id
            or created.code != input_type.code
            or created.contract_segment.id != payslip.contract_segment.id
        ):
            raise _inconsistent()
        post_inputs = await self._all_inputs(company_id, payslip.id)
        created_matches = [item for item in post_inputs if item.id == created.id]
        unchanged = [item for item in post_inputs if item.id != created.id]
        if created_matches != [created] or unchanged != current:
            raise _state_conflict()
        parent = await self._exact_payslip(company_id, payslip.id)
        self._validate_post_action_parent(payslip, parent, allow_state_change=False)
        return created

    async def update_draft_payslip_input(
        self,
        company_id: int,
        input_id: int,
        payload: DraftPayslipInputUpdate,
        expected: PayrollWriteCheckpoint,
    ) -> PayslipInput:
        input_id = _require_requested_id(input_id, "input")
        checkpoint = await self._validated_write_checkpoint(
            company_id,
            expected,
            payslip_id=payload.payslip_id,
        )
        payslip = checkpoint.payslip
        current_inputs = list(checkpoint.inputs)
        current = self._selected_input(current_inputs, input_id)
        matching_types = [
            item for item in checkpoint.input_types if item.id == current.input_type.id
        ]
        if len(matching_types) != 1 or current.code != matching_types[0].code:
            raise PayrollWriteRejected(_state_conflict())
        ensure_payroll_action_allowed(self._version, "hr.payslip.input", "update_draft_input")
        values: dict[str, object] = {}
        if payload.description is not None:
            values["name"] = payload.description
        if payload.amount is not None:
            values["amount"] = str(payload.amount)
        try:
            result = await self._transport.execute_method(
                "hr.payslip.input",
                "write",
                ids=(input_id,),
                named={"vals": values},
                company_ids=(company_id,),
            )
        except OdooMcpError as exc:
            if exc.code is ErrorCode.ODOO_PERMISSION_DENIED:
                raise PayrollWriteRejected(exc) from None
            raise
        if result is not True:
            raise _api_error("Odoo returned an invalid Payroll input update result.")
        updated = await self._exact_input(company_id, payslip.id, input_id)
        if (
            (payload.description is not None and updated.name != payload.description)
            or (payload.amount is not None and updated.amount != payload.amount)
            or updated.input_type.id != current.input_type.id
            or updated.code != current.code
            or updated.contract_segment.id != current.contract_segment.id
        ):
            raise _inconsistent()
        expected_inputs = [updated if item.id == updated.id else item for item in current_inputs]
        expected_inputs.sort(key=lambda item: (item.sequence, item.id))
        if await self._all_inputs(company_id, payslip.id) != expected_inputs:
            raise _state_conflict()
        parent = await self._exact_payslip(company_id, payslip.id)
        self._validate_post_action_parent(payslip, parent, allow_state_change=False)
        return updated

    async def delete_draft_payslip_input(
        self,
        company_id: int,
        payslip_id: int,
        input_id: int,
        expected: PayrollWriteCheckpoint,
    ) -> DeletedPayslipInput:
        payslip_id = _require_requested_id(payslip_id, "payslip")
        input_id = _require_requested_id(input_id, "input")
        checkpoint = await self._validated_write_checkpoint(
            company_id,
            expected,
            payslip_id=payslip_id,
        )
        payslip = checkpoint.payslip
        current_inputs = list(checkpoint.inputs)
        current = self._selected_input(current_inputs, input_id)
        matching_types = [
            item for item in checkpoint.input_types if item.id == current.input_type.id
        ]
        if (
            current.payslip.id != payslip_id
            or len(matching_types) != 1
            or current.code != matching_types[0].code
        ):
            raise PayrollWriteRejected(_state_conflict())
        ensure_payroll_action_allowed(self._version, "hr.payslip.input", "delete_draft_input")
        try:
            result = await self._transport.execute_method(
                "hr.payslip.input",
                "unlink",
                ids=(input_id,),
                company_ids=(company_id,),
            )
        except OdooMcpError as exc:
            if exc.code is ErrorCode.ODOO_PERMISSION_DENIED:
                raise PayrollWriteRejected(exc) from None
            raise
        if result is not True:
            raise _api_error("Odoo returned an invalid Payroll input deletion result.")
        remaining = await self.get_payslip_inputs(
            company_id,
            PayslipChildFilters(payslip_ids=(payslip.id,), record_ids=(input_id,)),
            PayrollPageRequest(limit=1),
        )
        parent = await self._exact_payslip(company_id, payslip.id)
        self._validate_post_action_parent(payslip, parent, allow_state_change=False)
        if remaining.items:
            raise _inconsistent()
        expected_inputs = [item for item in current_inputs if item.id != input_id]
        if await self._all_inputs(company_id, payslip.id) != expected_inputs:
            raise _state_conflict()
        return DeletedPayslipInput(id=input_id, payslip_id=payslip.id)

    async def recompute_draft_payslip(
        self,
        company_id: int,
        payslip_id: int,
        expected: PayrollWriteCheckpoint,
    ) -> Payslip:
        checkpoint = await self._validated_write_checkpoint(
            company_id,
            expected,
            payslip_id=payslip_id,
            include_calculation_sources=True,
        )
        payslip = checkpoint.payslip
        current_inputs = list(checkpoint.inputs)
        ensure_payroll_action_allowed(
            self._version, "hr.payslip", "compute_sheet_on_editable_payslip"
        )
        try:
            result = await self._transport.execute_method(
                "hr.payslip",
                "compute_sheet",
                ids=(payslip_id,),
                company_ids=(company_id,),
            )
        except OdooMcpError as exc:
            if exc.code is ErrorCode.ODOO_PERMISSION_DENIED:
                raise PayrollWriteRejected(exc) from None
            raise
        if result is False:
            raise PayrollWriteRejected(
                OdooMcpError(
                    ErrorCode.PAYROLL_RECALCULATION_FAILED,
                    "Odoo did not recalculate the draft payslip.",
                    "Review the draft payslip in Odoo and retry after correcting it.",
                )
            )
        if result is not None and result is not True:
            raise _api_error("Odoo returned an invalid Payroll recalculation result.")
        refreshed = await self._exact_payslip(company_id, payslip_id)
        self._validate_post_action_parent(payslip, refreshed, allow_state_change=True)
        if await self._all_inputs(company_id, payslip.id) != current_inputs:
            raise _state_conflict()
        return refreshed
