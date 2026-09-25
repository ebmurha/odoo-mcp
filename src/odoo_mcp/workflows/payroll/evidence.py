"""Request-time Payroll evidence workflows over the typed adapter protocol."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import TypeVar

from odoo_mcp.adapters.base import OdooAdapter
from odoo_mcp.adapters.payroll import (
    PayrollBatch,
    PayrollBatchFilters,
    PayrollContractFilters,
    PayrollContractSegment,
    PayrollDiscoveryWindow,
    PayrollEmployee,
    PayrollInputType,
    PayrollInputTypeFilters,
    PayrollPage,
    PayrollPageRequest,
    PayrollPeriod,
    PayrollState,
    PayrollValue,
    PayrollWorkEntry,
    PayrollWorkEntryFilters,
    Payslip,
    PayslipChildFilters,
    PayslipFilters,
    PayslipInput,
    PayslipLine,
    PayslipWorkedDay,
)
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.payroll_schemas import (
    ALL_PAYROLL_STATES,
    AttendanceSummary,
    AttendanceSummaryItem,
    CompactPayslip,
    GetAttendanceSummaryInput,
    GetAttendanceSummaryResponse,
    GetEmployeePayrollContextInput,
    GetEmployeePayrollContextResponse,
    GetPayrollBatchInput,
    GetPayrollBatchResponse,
    GetPayslipInput,
    GetPayslipResponse,
    ListPayrollPeriodsInput,
    ListPayrollPeriodsResponse,
    ListPayslipsInput,
    ListPayslipsResponse,
    ListSalaryRulesInput,
    ListSalaryRulesResponse,
    ObservedCategoryTotal,
    ObservedRuleTotal,
    PayrollBatchDetails,
    PayrollBatchSummary,
    PayrollContractSegmentItem,
    PayrollCurrencyReference,
    PayrollEmployeeDetails,
    PayrollInputItem,
    PayrollNamedReference,
    PayrollPeriodItem,
    PayrollPeriodsSummary,
    PayrollSalaryLineItem,
    PayrollSourceReference,
    PayrollStateCount,
    PayrollWorkedDayItem,
    PayslipListSummary,
    SalaryRuleSnapshot,
    SalaryRulesSummary,
)

ItemT = TypeVar("ItemT", bound=PayrollValue)
PageT = TypeVar("PageT")

_PAGE_SIZE = 200
_MAX_BATCHES = 5_000
_MAX_PAYSLIPS = 5_000
_MAX_LINES = 100_000
_MAX_WORKED_DAYS = 100_000
_MAX_INPUTS = 100_000
_MAX_INPUT_TYPES = 5_000
_MAX_EMPLOYEES = 5_000
_MAX_SEGMENTS = 20_000
_MAX_WORK_ENTRIES = 100_000
_EXACT_PAYSLIP_LINE_LIMIT = 500
_EXACT_PAYSLIP_WORKED_DAY_LIMIT = 200
_EXACT_PAYSLIP_INPUT_LIMIT = 200


def _error(code: ErrorCode, message: str, hint: str) -> OdooMcpError:
    return OdooMcpError(code, message, hint)


def _invalid_source() -> OdooMcpError:
    return _error(
        ErrorCode.PAYROLL_SOURCE_INCONSISTENT,
        "Odoo returned inconsistent Payroll source data.",
        "Check the requested company, relationships, and Payroll records, then retry.",
    )


def _malformed_source() -> OdooMcpError:
    return _error(
        ErrorCode.ODOO_API_ERROR,
        "Odoo returned invalid or incomplete Payroll data.",
        "Check Odoo Payroll compatibility, data integrity, and access, then retry.",
    )


def _too_large() -> OdooMcpError:
    return _error(
        ErrorCode.PAYROLL_RESULT_TOO_LARGE,
        "The Payroll result exceeds the safe processing bound.",
        "Narrow the Payroll period or identifiers and retry.",
    )


async def _collect_pages(
    fetch: Callable[[PayrollPageRequest], Awaitable[PayrollPage[ItemT]]],
    item_type: type[ItemT],
    *,
    cap: int,
) -> list[ItemT]:
    items: list[ItemT] = []
    seen_ids: set[int] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None
    expected_total: int | None = None
    while True:
        page = await fetch(PayrollPageRequest(limit=_PAGE_SIZE, cursor=cursor))
        if (
            not isinstance(page, PayrollPage)
            or not isinstance(page.items, list)
            or not isinstance(page.total_count, int)
            or isinstance(page.total_count, bool)
            or page.total_count < 0
            or (page.next_cursor is not None and not isinstance(page.next_cursor, str))
        ):
            raise _malformed_source()
        if len(page.items) > _PAGE_SIZE or (
            page.next_cursor is not None and len(page.next_cursor) > 128
        ):
            raise _malformed_source()
        if expected_total is None:
            expected_total = page.total_count
            if expected_total > cap:
                raise _too_large()
        elif page.total_count != expected_total:
            raise _malformed_source()
        if page.next_cursor is not None and not page.items:
            raise _malformed_source()
        for item in page.items:
            if not isinstance(item, item_type):
                raise _malformed_source()
            identifier = getattr(item, "id", None)
            if (
                not isinstance(identifier, int)
                or isinstance(identifier, bool)
                or identifier <= 0
                or identifier in seen_ids
            ):
                raise _malformed_source()
            seen_ids.add(identifier)
            items.append(item)
            if len(items) > cap or len(items) > expected_total:
                raise _malformed_source()
        if page.next_cursor is None:
            if len(items) != expected_total:
                raise _malformed_source()
            return items
        if page.next_cursor in seen_cursors:
            raise _malformed_source()
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor


async def _all_batches(
    adapter: OdooAdapter, company_id: int, filters: PayrollBatchFilters
) -> list[PayrollBatch]:
    async def fetch(page: PayrollPageRequest) -> PayrollPage[PayrollBatch]:
        return await adapter.get_payroll_batches(company_id, filters, page)

    batches = await _collect_pages(fetch, PayrollBatch, cap=_MAX_BATCHES)
    names: dict[int, str] = {}
    for batch in batches:
        if batch.id in names and names[batch.id] != batch.name:
            raise _invalid_source()
        names[batch.id] = batch.name
    return batches


async def _all_payslips(
    adapter: OdooAdapter, company_id: int, filters: PayslipFilters
) -> list[Payslip]:
    async def fetch(page: PayrollPageRequest) -> PayrollPage[Payslip]:
        return await adapter.get_payslips(company_id, filters, page)

    payslips = await _collect_pages(fetch, Payslip, cap=_MAX_PAYSLIPS)
    _validate_payslip_references(payslips)
    return payslips


def _validate_payslip_references(payslips: Iterable[Payslip]) -> None:
    references: dict[tuple[str, int], str] = {}
    for payslip in payslips:
        values = (
            ("hr.employee", payslip.employee.id, payslip.employee.name),
            (payslip.contract_source, payslip.contract_segment.id, payslip.contract_segment.name),
            ("hr.payroll.structure", payslip.structure.id, payslip.structure.name),
            ("res.currency", payslip.currency.id, payslip.currency.name),
        )
        for model, identifier, name in values:
            key = (model, identifier)
            if key in references and references[key] != name:
                raise _invalid_source()
            references[key] = name
        if payslip.batch is not None:
            key = ("hr.payslip.run", payslip.batch.id)
            if key in references and references[key] != payslip.batch.name:
                raise _invalid_source()
            references[key] = payslip.batch.name


def _chunks(values: Sequence[int], size: int = _PAGE_SIZE) -> Iterable[tuple[int, ...]]:
    for index in range(0, len(values), size):
        yield tuple(values[index : index + size])


async def _all_lines(
    adapter: OdooAdapter,
    company_id: int,
    payslip_ids: Sequence[int],
    *,
    cap: int = _MAX_LINES,
) -> list[PayslipLine]:
    result: list[PayslipLine] = []
    seen: set[int] = set()
    for identifiers in _chunks(payslip_ids):
        filters = PayslipChildFilters(payslip_ids=identifiers)

        async def fetch(
            page: PayrollPageRequest,
            selected_filters: PayslipChildFilters = filters,
        ) -> PayrollPage[PayslipLine]:
            return await adapter.get_payslip_lines(company_id, selected_filters, page)

        rows = await _collect_pages(fetch, PayslipLine, cap=cap - len(result))
        if any(row.id in seen for row in rows):
            raise _malformed_source()
        seen.update(row.id for row in rows)
        result.extend(rows)
        if len(result) > cap:
            raise _too_large()
    return result


async def _all_worked_days(
    adapter: OdooAdapter,
    company_id: int,
    payslip_ids: Sequence[int],
    *,
    cap: int = _MAX_WORKED_DAYS,
) -> list[PayslipWorkedDay]:
    result: list[PayslipWorkedDay] = []
    seen: set[int] = set()
    for identifiers in _chunks(payslip_ids):
        filters = PayslipChildFilters(payslip_ids=identifiers)

        async def fetch(
            page: PayrollPageRequest,
            selected_filters: PayslipChildFilters = filters,
        ) -> PayrollPage[PayslipWorkedDay]:
            return await adapter.get_payslip_worked_days(company_id, selected_filters, page)

        rows = await _collect_pages(fetch, PayslipWorkedDay, cap=cap - len(result))
        if any(row.id in seen for row in rows):
            raise _malformed_source()
        seen.update(row.id for row in rows)
        result.extend(rows)
        if len(result) > cap:
            raise _too_large()
    return result


async def _all_inputs(
    adapter: OdooAdapter,
    company_id: int,
    payslip_ids: Sequence[int],
    *,
    cap: int = _MAX_INPUTS,
) -> list[PayslipInput]:
    result: list[PayslipInput] = []
    seen: set[int] = set()
    for identifiers in _chunks(payslip_ids):
        filters = PayslipChildFilters(payslip_ids=identifiers)

        async def fetch(
            page: PayrollPageRequest,
            selected_filters: PayslipChildFilters = filters,
        ) -> PayrollPage[PayslipInput]:
            return await adapter.get_payslip_inputs(company_id, selected_filters, page)

        rows = await _collect_pages(fetch, PayslipInput, cap=cap - len(result))
        if any(row.id in seen for row in rows):
            raise _malformed_source()
        seen.update(row.id for row in rows)
        result.extend(rows)
        if len(result) > cap:
            raise _too_large()
    return result


async def _all_input_types(
    adapter: OdooAdapter,
    company_id: int,
    structure_id: int,
    identifiers: tuple[int, ...],
) -> list[PayrollInputType]:
    filters = PayrollInputTypeFilters(
        structure_id=structure_id,
        input_type_ids=identifiers,
    )

    async def fetch(page: PayrollPageRequest) -> PayrollPage[PayrollInputType]:
        return await adapter.get_payroll_input_types(company_id, filters, page)

    return await _collect_pages(fetch, PayrollInputType, cap=_MAX_INPUT_TYPES)


async def _all_employees(
    adapter: OdooAdapter, company_id: int, identifiers: tuple[int, ...]
) -> list[PayrollEmployee]:
    async def fetch(page: PayrollPageRequest) -> PayrollPage[PayrollEmployee]:
        return await adapter.get_payroll_employees(company_id, identifiers, page)

    return await _collect_pages(fetch, PayrollEmployee, cap=_MAX_EMPLOYEES)


async def _all_segments(
    adapter: OdooAdapter, company_id: int, filters: PayrollContractFilters
) -> list[PayrollContractSegment]:
    async def fetch(page: PayrollPageRequest) -> PayrollPage[PayrollContractSegment]:
        return await adapter.get_payroll_contract_segments(company_id, filters, page)

    return await _collect_pages(fetch, PayrollContractSegment, cap=_MAX_SEGMENTS)


async def _all_work_entries(
    adapter: OdooAdapter, company_id: int, filters: PayrollWorkEntryFilters
) -> list[PayrollWorkEntry]:
    async def fetch(page: PayrollPageRequest) -> PayrollPage[PayrollWorkEntry]:
        return await adapter.get_payroll_work_entries(company_id, filters, page)

    return await _collect_pages(fetch, PayrollWorkEntry, cap=_MAX_WORK_ENTRIES)


async def _exact_batch(adapter: OdooAdapter, company_id: int, batch_id: int) -> PayrollBatch:
    batches = await _all_batches(
        adapter,
        company_id,
        PayrollBatchFilters(batch_ids=(batch_id,)),
    )
    if not batches:
        raise _error(
            ErrorCode.PAYROLL_BATCH_NOT_FOUND,
            "The requested Payroll batch was not found.",
            "Use an exact batch ID from the authorized company.",
        )
    if len(batches) != 1 or batches[0].id != batch_id or batches[0].company_id != company_id:
        raise _invalid_source()
    return batches[0]


async def _exact_payslip(adapter: OdooAdapter, company_id: int, payslip_id: int) -> Payslip:
    payslips = await _all_payslips(
        adapter,
        company_id,
        PayslipFilters(payslip_ids=(payslip_id,)),
    )
    if not payslips:
        raise _error(
            ErrorCode.PAYSLIP_NOT_FOUND,
            "The requested payslip was not found.",
            "Use an exact payslip ID from the authorized company.",
        )
    if len(payslips) != 1 or payslips[0].id != payslip_id:
        raise _invalid_source()
    _validate_payslip(payslips[0], company_id)
    return payslips[0]


def _validate_payslip(
    payslip: Payslip,
    company_id: int,
    *,
    batch_id: int | None = None,
    period: PayrollPeriod | None = None,
) -> None:
    if (
        payslip.company_id != company_id
        or (batch_id is not None and (payslip.batch is None or payslip.batch.id != batch_id))
        or (
            period is not None
            and (payslip.date_from != period.start or payslip.date_to != period.end)
        )
    ):
        raise _invalid_source()


async def _batch_members(
    adapter: OdooAdapter, company_id: int, batch: PayrollBatch
) -> list[Payslip]:
    payslips = await _all_payslips(
        adapter,
        company_id,
        PayslipFilters(batch_id=batch.id, states=ALL_PAYROLL_STATES),
    )
    for payslip in payslips:
        _validate_payslip(payslip, company_id, batch_id=batch.id)
        if payslip.date_from < batch.date_start or payslip.date_to > batch.date_end:
            raise _invalid_source()
    payslips.sort(key=lambda row: (row.date_from, row.date_to, row.id), reverse=True)
    return payslips


async def _period_payslips(
    adapter: OdooAdapter, company_id: int, period: PayrollPeriod
) -> list[Payslip]:
    payslips = await _all_payslips(
        adapter,
        company_id,
        PayslipFilters(period=period, states=ALL_PAYROLL_STATES),
    )
    if not payslips:
        raise _error(
            ErrorCode.PAYROLL_PERIOD_NOT_FOUND,
            "The requested Payroll period was not found.",
            "Use an exact period observed in the authorized company.",
        )
    for payslip in payslips:
        _validate_payslip(payslip, company_id, period=period)
    payslips.sort(key=lambda row: (row.date_from, row.date_to, row.id), reverse=True)
    return payslips


def _compact_payslip(payslip: Payslip) -> CompactPayslip:
    return CompactPayslip(
        payslip_id=payslip.id,
        reference=payslip.reference,
        employee=PayrollNamedReference(id=payslip.employee.id, name=payslip.employee.name),
        period_start=payslip.date_from,
        period_end=payslip.date_to,
        state=payslip.state,
        batch=(
            None
            if payslip.batch is None
            else PayrollNamedReference(id=payslip.batch.id, name=payslip.batch.name)
        ),
        contract_source=payslip.contract_source,
        contract=PayrollNamedReference(
            id=payslip.contract_segment.id,
            name=payslip.contract_segment.name,
        ),
        structure=PayrollNamedReference(id=payslip.structure.id, name=payslip.structure.name),
        credit_note=payslip.credit_note,
        currency=PayrollCurrencyReference(id=payslip.currency.id, name=payslip.currency.name),
    )


def _state_counts(payslips: Iterable[Payslip]) -> list[PayrollStateCount]:
    counts: dict[PayrollState, int] = defaultdict(int)
    for payslip in payslips:
        counts[payslip.state] += 1
    return [
        PayrollStateCount(state=state, count=counts[state])
        for state in ALL_PAYROLL_STATES
        if counts[state]
    ]


def _source_refs(values: Mapping[str, Iterable[int]]) -> list[PayrollSourceReference]:
    result: list[PayrollSourceReference] = []
    for model in sorted(values):
        identifiers = tuple(sorted(set(values[model])))
        if identifiers:
            result.append(PayrollSourceReference(source_model=model, source_ids=identifiers))
    return result


def _anchors(values: Mapping[str, Iterable[PayrollValue]]) -> list[tuple[str, int, str]]:
    result: list[tuple[str, int, str]] = []
    for model, records in values.items():
        for record in records:
            write_date = getattr(record, "write_date", None)
            identifier = getattr(record, "id", None)
            if not isinstance(write_date, datetime) or not isinstance(identifier, int):
                raise _malformed_source()
            result.append((model, identifier, write_date.isoformat()))
    return sorted(result)


def _fingerprint(binding: object, anchors: list[tuple[str, int, str]]) -> str:
    encoded = json.dumps(
        {"binding": binding, "anchors": anchors},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()[:20]


def _cursor_offset(cursor: str | None, fingerprint: str) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        version, bound, raw_offset = (
            base64.b64decode(padded, altchars=b"-_", validate=True).decode().split(":")
        )
        offset = int(raw_offset)
    except (ValueError, UnicodeError, binascii.Error):
        raise _error(
            ErrorCode.INVALID_INPUT,
            "The Payroll pagination cursor is invalid.",
            "Restart the Payroll request without a cursor.",
        ) from None
    if version != "v1" or bound != fingerprint or offset <= 0:
        raise _error(
            ErrorCode.INVALID_INPUT,
            "The Payroll pagination cursor does not match this request.",
            "Restart the Payroll request without a cursor.",
        )
    return offset


def _paginate(
    items: list[PageT],
    *,
    limit: int,
    cursor: str | None,
    binding: object,
    anchors: list[tuple[str, int, str]],
) -> tuple[list[PageT], str | None]:
    fingerprint = _fingerprint(binding, anchors)
    offset = _cursor_offset(cursor, fingerprint)
    if offset > len(items):
        raise _error(
            ErrorCode.INVALID_INPUT,
            "The Payroll pagination cursor is outside this result.",
            "Restart the Payroll request without a cursor.",
        )
    selected = items[offset : offset + limit]
    next_offset = offset + len(selected)
    next_cursor = None
    if next_offset < len(items):
        next_cursor = (
            base64.urlsafe_b64encode(f"v1:{fingerprint}:{next_offset}".encode())
            .decode()
            .rstrip("=")
        )
    return selected, next_cursor


def _validate_lines(lines: Iterable[PayslipLine], parents: Mapping[int, Payslip]) -> None:
    for line in lines:
        parent = parents.get(line.payslip.id)
        if (
            parent is None
            or line.employee.id != parent.employee.id
            or line.employee.name != parent.employee.name
            or line.contract_segment.id != parent.contract_segment.id
            or line.contract_segment.name != parent.contract_segment.name
            or line.contract_source != parent.contract_source
            or line.currency.id != parent.currency.id
            or line.currency.name != parent.currency.name
        ):
            raise _invalid_source()


def _validate_worked_days(rows: Iterable[PayslipWorkedDay], parents: Mapping[int, Payslip]) -> None:
    for row in rows:
        parent = parents.get(row.payslip.id)
        if (
            parent is None
            or row.contract_segment.id != parent.contract_segment.id
            or row.contract_segment.name != parent.contract_segment.name
            or row.contract_source != parent.contract_source
            or row.currency.id != parent.currency.id
            or row.currency.name != parent.currency.name
        ):
            raise _invalid_source()


def _validate_inputs(rows: Iterable[PayslipInput], parents: Mapping[int, Payslip]) -> None:
    for row in rows:
        parent = parents.get(row.payslip.id)
        if (
            parent is None
            or row.contract_segment.id != parent.contract_segment.id
            or row.contract_segment.name != parent.contract_segment.name
            or row.contract_source != parent.contract_source
        ):
            raise _invalid_source()


def _validate_line_metadata(lines: Iterable[PayslipLine]) -> None:
    rules: dict[int, tuple[object, ...]] = {}
    categories: dict[int, str] = {}
    currencies: dict[int, str] = {}
    for line in lines:
        metadata = (
            line.salary_rule.name,
            line.code,
            line.category.id,
            line.category.name,
            line.sequence,
        )
        if line.salary_rule.id in rules and rules[line.salary_rule.id] != metadata:
            raise _invalid_source()
        if line.category.id in categories and categories[line.category.id] != line.category.name:
            raise _invalid_source()
        if line.currency.id in currencies and currencies[line.currency.id] != line.currency.name:
            raise _invalid_source()
        rules[line.salary_rule.id] = metadata
        categories[line.category.id] = line.category.name
        currencies[line.currency.id] = line.currency.name


def _observed_totals(
    lines: list[PayslipLine],
) -> tuple[list[ObservedRuleTotal], list[ObservedCategoryTotal]]:
    _validate_line_metadata(lines)
    rule_values: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    category_values: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    line_by_rule: dict[tuple[int, int], PayslipLine] = {}
    line_by_category: dict[tuple[int, int], PayslipLine] = {}
    for line in lines:
        rule_key = (line.currency.id, line.salary_rule.id)
        category_key = (line.currency.id, line.category.id)
        rule_values[rule_key] += line.total
        category_values[category_key] += line.total
        line_by_rule[rule_key] = line
        line_by_category[category_key] = line
    rule_totals = [
        ObservedRuleTotal(
            currency=PayrollCurrencyReference(id=line.currency.id, name=line.currency.name),
            salary_rule=PayrollNamedReference(
                id=line.salary_rule.id,
                name=line.salary_rule.name,
            ),
            code=line.code,
            total=rule_values[key],
        )
        for key, line in line_by_rule.items()
    ]
    rule_totals.sort(key=lambda item: (item.currency.id, item.code.casefold(), item.salary_rule.id))
    category_totals = [
        ObservedCategoryTotal(
            currency=PayrollCurrencyReference(id=line.currency.id, name=line.currency.name),
            category=PayrollNamedReference(id=line.category.id, name=line.category.name),
            total=category_values[key],
        )
        for key, line in line_by_category.items()
    ]
    category_totals.sort(
        key=lambda item: (item.currency.id, item.category.name.casefold(), item.category.id)
    )
    return rule_totals, category_totals


async def list_payroll_periods(
    adapter: OdooAdapter,
    request: ListPayrollPeriodsInput,
    *,
    request_id: str,
    observed_at: datetime,
) -> ListPayrollPeriodsResponse:
    states = request.states or ("waiting", "done", "paid")
    payslips = await _all_payslips(
        adapter,
        request.company_id,
        PayslipFilters(
            window=PayrollDiscoveryWindow(
                start=request.window_start,
                end=request.window_end,
            ),
            states=states,
        ),
    )
    grouped: dict[tuple[date, date], list[Payslip]] = defaultdict(list)
    for payslip in payslips:
        _validate_payslip(payslip, request.company_id)
        if (
            payslip.date_from < request.window_start
            or payslip.date_to > request.window_end
            or payslip.state not in states
        ):
            raise _invalid_source()
        grouped[(payslip.date_from, payslip.date_to)].append(payslip)
    all_items: list[PayrollPeriodItem] = []
    for (period_start, period_end), rows in grouped.items():
        active = [row for row in rows if row.state != "cancelled"]
        currencies = {row.currency.id: row.currency.name for row in active}
        all_items.append(
            PayrollPeriodItem(
                period_start=period_start,
                period_end=period_end,
                observed_payslip_count=len(rows),
                non_cancelled_payslip_count=len(active),
                batch_ids=tuple(sorted({row.batch.id for row in active if row.batch is not None})),
                currencies=[
                    PayrollCurrencyReference(id=identifier, name=name)
                    for identifier, name in sorted(currencies.items())
                ],
                state_counts=_state_counts(rows),
            )
        )
    all_items.sort(key=lambda item: (item.period_start, item.period_end), reverse=True)
    anchor_values = _anchors({"hr.payslip": payslips})
    selected, next_cursor = _paginate(
        all_items,
        limit=request.limit,
        cursor=request.cursor,
        binding=request.model_dump(mode="json", exclude={"cursor"}),
        anchors=anchor_values,
    )
    return ListPayrollPeriodsResponse(
        request_id=request_id,
        company_id=request.company_id,
        observed_at=observed_at,
        source_refs=_source_refs({"hr.payslip": (row.id for row in payslips)}),
        limitations=[],
        items=selected,
        next_cursor=next_cursor,
        summary=PayrollPeriodsSummary(
            period_count=len(all_items),
            returned_count=len(selected),
            non_cancelled_payslip_count=sum(item.non_cancelled_payslip_count for item in all_items),
            has_more=next_cursor is not None,
        ),
    )


async def get_payroll_batch(
    adapter: OdooAdapter,
    request: GetPayrollBatchInput,
    *,
    request_id: str,
    observed_at: datetime,
) -> GetPayrollBatchResponse:
    states = request.states or ("waiting", "done", "paid")
    batch = await _exact_batch(adapter, request.company_id, request.batch_id)
    all_members = await _batch_members(adapter, request.company_id, batch)
    selected_members = [row for row in all_members if row.state in states]
    included = [row for row in selected_members if row.state != "cancelled"]
    parents = {row.id: row for row in included}
    lines = await _all_lines(adapter, request.company_id, tuple(parents)) if parents else []
    _validate_lines(lines, parents)
    rule_totals, category_totals = _observed_totals(lines)
    items = [_compact_payslip(row) for row in selected_members]
    anchor_values = _anchors(
        {
            "hr.payslip.run": [batch],
            "hr.payslip": all_members,
            "hr.payslip.line": lines,
        }
    )
    selected_items, next_cursor = _paginate(
        items,
        limit=request.limit,
        cursor=request.cursor,
        binding=request.model_dump(mode="json", exclude={"cursor"}),
        anchors=anchor_values,
    )
    return GetPayrollBatchResponse(
        request_id=request_id,
        company_id=request.company_id,
        observed_at=observed_at,
        source_refs=_source_refs(
            {
                "hr.payslip.run": (batch.id,),
                "hr.payslip": (row.id for row in selected_members),
                "hr.payslip.line": (row.id for row in lines),
            }
        ),
        limitations=[],
        batch=PayrollBatchDetails(
            batch_id=batch.id,
            name=batch.name,
            period_start=batch.date_start,
            period_end=batch.date_end,
            state=batch.state,
        ),
        items=selected_items,
        next_cursor=next_cursor,
        summary=PayrollBatchSummary(
            non_cancelled_payslip_count=len(included),
            returned_count=len(selected_items),
            employee_count=len({row.employee.id for row in included}),
            state_counts=_state_counts(included),
            rule_totals=rule_totals,
            category_totals=category_totals,
            has_more=next_cursor is not None,
        ),
    )


async def _payslip_scope(
    adapter: OdooAdapter,
    request: ListPayslipsInput | ListSalaryRulesInput,
) -> tuple[list[Payslip], PayrollBatch | None, PayrollPeriod | None]:
    period = (
        None
        if request.period_start is None or request.period_end is None
        else PayrollPeriod(start=request.period_start, end=request.period_end)
    )
    batch = (
        None
        if request.batch_id is None
        else await _exact_batch(adapter, request.company_id, request.batch_id)
    )
    if batch is not None:
        base = await _batch_members(adapter, request.company_id, batch)
        if period is not None:
            period_rows = await _period_payslips(adapter, request.company_id, period)
            period_ids = {row.id for row in period_rows}
            base = [
                row
                for row in base
                if row.id in period_ids
                and row.date_from == period.start
                and row.date_to == period.end
            ]
    else:
        if period is None:
            raise AssertionError("validated Payroll scope requires a batch or period")
        base = await _period_payslips(adapter, request.company_id, period)
    for row in base:
        _validate_payslip(
            row,
            request.company_id,
            batch_id=request.batch_id,
            period=period,
        )
    return base, batch, period


async def list_payslips(
    adapter: OdooAdapter,
    request: ListPayslipsInput,
    *,
    request_id: str,
    observed_at: datetime,
) -> ListPayslipsResponse:
    base, batch, _period = await _payslip_scope(adapter, request)
    states = request.states or ("waiting", "done", "paid")
    selected = [
        row
        for row in base
        if row.state in states
        and (not request.employee_ids or row.employee.id in request.employee_ids)
    ]
    selected.sort(key=lambda row: (row.date_from, row.date_to, row.id), reverse=True)
    items = [_compact_payslip(row) for row in selected]
    anchors_by_model: dict[str, Iterable[PayrollValue]] = {"hr.payslip": base}
    if batch is not None:
        anchors_by_model["hr.payslip.run"] = [batch]
    selected_items, next_cursor = _paginate(
        items,
        limit=request.limit,
        cursor=request.cursor,
        binding=request.model_dump(mode="json", exclude={"cursor"}),
        anchors=_anchors(anchors_by_model),
    )
    source_values: dict[str, Iterable[int]] = {"hr.payslip": (row.id for row in selected)}
    if batch is not None:
        source_values["hr.payslip.run"] = (batch.id,)
    return ListPayslipsResponse(
        request_id=request_id,
        company_id=request.company_id,
        observed_at=observed_at,
        source_refs=_source_refs(source_values),
        limitations=[],
        items=selected_items,
        next_cursor=next_cursor,
        summary=PayslipListSummary(
            payslip_count=len(selected),
            returned_count=len(selected_items),
            state_counts=_state_counts(selected),
            has_more=next_cursor is not None,
        ),
    )


async def get_payslip(
    adapter: OdooAdapter,
    request: GetPayslipInput,
    *,
    request_id: str,
    observed_at: datetime,
) -> GetPayslipResponse:
    payslip = await _exact_payslip(adapter, request.company_id, request.payslip_id)
    parents = {payslip.id: payslip}
    lines = await _all_lines(
        adapter,
        request.company_id,
        (payslip.id,),
        cap=_EXACT_PAYSLIP_LINE_LIMIT,
    )
    worked_days = await _all_worked_days(
        adapter,
        request.company_id,
        (payslip.id,),
        cap=_EXACT_PAYSLIP_WORKED_DAY_LIMIT,
    )
    inputs = await _all_inputs(
        adapter,
        request.company_id,
        (payslip.id,),
        cap=_EXACT_PAYSLIP_INPUT_LIMIT,
    )
    if (
        len(lines) > _EXACT_PAYSLIP_LINE_LIMIT
        or len(worked_days) > _EXACT_PAYSLIP_WORKED_DAY_LIMIT
        or len(inputs) > _EXACT_PAYSLIP_INPUT_LIMIT
    ):
        raise _too_large()
    _validate_lines(lines, parents)
    _validate_worked_days(worked_days, parents)
    _validate_inputs(inputs, parents)
    _validate_line_metadata(lines)
    input_type_ids = tuple(sorted({row.input_type.id for row in inputs}))
    input_types = (
        await _all_input_types(
            adapter,
            request.company_id,
            payslip.structure.id,
            input_type_ids,
        )
        if input_type_ids
        else []
    )
    type_map = {row.id: row for row in input_types}
    if set(type_map) != set(input_type_ids):
        raise _invalid_source()
    lines.sort(key=lambda row: (row.sequence, row.id))
    worked_days.sort(key=lambda row: row.id)
    inputs.sort(key=lambda row: (row.sequence, row.id))
    salary_items = [
        PayrollSalaryLineItem(
            source_line_id=row.id,
            salary_rule=PayrollNamedReference(
                id=row.salary_rule.id,
                name=row.salary_rule.name,
            ),
            category=PayrollNamedReference(id=row.category.id, name=row.category.name),
            name=row.name,
            code=row.code,
            sequence=row.sequence,
            quantity=row.quantity,
            rate=row.rate,
            amount=row.amount,
            total=row.total,
            currency=PayrollCurrencyReference(id=row.currency.id, name=row.currency.name),
        )
        for row in lines
    ]
    worked_day_items = [
        PayrollWorkedDayItem(
            source_line_id=row.id,
            contract_source=row.contract_source,
            contract=PayrollNamedReference(
                id=row.contract_segment.id,
                name=row.contract_segment.name,
            ),
            work_entry_type=PayrollNamedReference(
                id=row.work_entry_type.id,
                name=row.work_entry_type.name,
            ),
            name=row.name,
            code=row.code,
            days=row.number_of_days,
            hours=row.number_of_hours,
        )
        for row in worked_days
    ]
    input_items: list[PayrollInputItem] = []
    for row in inputs:
        input_type = type_map[row.input_type.id]
        if input_type.name != row.input_type.name or input_type.code != row.code:
            raise _invalid_source()
        input_items.append(
            PayrollInputItem(
                source_input_id=row.id,
                input_type=PayrollNamedReference(id=input_type.id, name=input_type.name),
                contract_source=row.contract_source,
                contract=PayrollNamedReference(
                    id=row.contract_segment.id,
                    name=row.contract_segment.name,
                ),
                name=row.name,
                code=row.code,
                amount=None if input_type.is_quantity else row.amount,
                quantity=row.amount if input_type.is_quantity else None,
                currency=PayrollCurrencyReference(
                    id=payslip.currency.id,
                    name=payslip.currency.name,
                ),
                sequence=row.sequence,
            )
        )
    return GetPayslipResponse(
        request_id=request_id,
        company_id=request.company_id,
        observed_at=observed_at,
        source_refs=_source_refs(
            {
                "hr.payslip": (payslip.id,),
                "hr.payslip.line": (row.id for row in lines),
                "hr.payslip.worked_days": (row.id for row in worked_days),
                "hr.payslip.input": (row.id for row in inputs),
                "hr.payslip.input.type": (row.id for row in input_types),
            }
        ),
        limitations=[],
        payslip=_compact_payslip(payslip),
        salary_lines=salary_items,
        worked_days=worked_day_items,
        inputs=input_items,
    )


async def get_employee_payroll_context(
    adapter: OdooAdapter,
    request: GetEmployeePayrollContextInput,
    *,
    request_id: str,
    observed_at: datetime,
) -> GetEmployeePayrollContextResponse:
    employees = await _all_employees(adapter, request.company_id, (request.employee_id,))
    if not employees:
        raise _error(
            ErrorCode.EMPLOYEE_NOT_FOUND,
            "The requested Payroll employee was not found.",
            "Use an exact employee ID from the authorized company.",
        )
    if (
        len(employees) != 1
        or employees[0].id != request.employee_id
        or employees[0].company_id != request.company_id
    ):
        raise _invalid_source()
    employee = employees[0]
    period = PayrollPeriod(start=request.period_start, end=request.period_end)
    segments = await _all_segments(
        adapter,
        request.company_id,
        PayrollContractFilters(employee_ids=(request.employee_id,), period=period),
    )
    if not segments:
        raise _error(
            ErrorCode.CONTRACT_DATA_MISSING,
            "Required Payroll contract evidence is unavailable.",
            "Create or correct the applicable contract evidence in Odoo, then retry.",
        )
    segments.sort(key=lambda segment: (segment.effective_start, segment.id))
    previous_end = None
    for segment in segments:
        if (
            segment.company_id != request.company_id
            or segment.employee.id != request.employee_id
            or segment.employee.name != employee.name
            or segment.effective_start < period.start
            or segment.effective_end > period.end
            or segment.effective_end < segment.effective_start
            or (previous_end is not None and previous_end >= segment.effective_start)
        ):
            raise _invalid_source()
        previous_end = segment.effective_end
    limitations = (
        ["contract_source_status_unavailable"]
        if any(segment.source_status == "unavailable" for segment in segments)
        else []
    )
    segment_items = [
        PayrollContractSegmentItem(
            source_model=segment.source_model,
            source_id=segment.id,
            effective_start=segment.effective_start,
            effective_end=segment.effective_end,
            revision_date=segment.revision_date,
            active=segment.active,
            source_status=segment.source_status,
            wage=segment.wage,
            currency=PayrollCurrencyReference(
                id=segment.currency.id,
                name=segment.currency.name,
            ),
            structure_type=(
                None
                if segment.structure_type is None
                else PayrollNamedReference(
                    id=segment.structure_type.id,
                    name=segment.structure_type.name,
                )
            ),
            working_schedule=(
                None
                if segment.resource_calendar is None
                else PayrollNamedReference(
                    id=segment.resource_calendar.id,
                    name=segment.resource_calendar.name,
                )
            ),
            department=(
                None
                if segment.department is None
                else PayrollNamedReference(
                    id=segment.department.id,
                    name=segment.department.name,
                )
            ),
            job=(
                None
                if segment.job is None
                else PayrollNamedReference(id=segment.job.id, name=segment.job.name)
            ),
            contract_type=(
                None
                if segment.contract_type is None
                else PayrollNamedReference(
                    id=segment.contract_type.id,
                    name=segment.contract_type.name,
                )
            ),
        )
        for segment in segments
    ]
    models: dict[str, list[int]] = defaultdict(list)
    models["hr.employee"].append(employee.id)
    for segment in segments:
        models[segment.source_model].append(segment.id)
    return GetEmployeePayrollContextResponse(
        request_id=request_id,
        company_id=request.company_id,
        observed_at=observed_at,
        source_refs=_source_refs(models),
        limitations=limitations,
        employee=PayrollEmployeeDetails(
            employee_id=employee.id,
            name=employee.name,
            active=employee.active,
        ),
        contract_segments=segment_items,
    )


async def list_salary_rules(
    adapter: OdooAdapter,
    request: ListSalaryRulesInput,
    *,
    request_id: str,
    observed_at: datetime,
) -> ListSalaryRulesResponse:
    base, batch, _period = await _payslip_scope(adapter, request)
    states = request.states or ("waiting", "done", "paid")
    payslips = [
        row
        for row in base
        if row.state in states
        and (not request.employee_ids or row.employee.id in request.employee_ids)
    ]
    parents = {row.id: row for row in payslips}
    lines = await _all_lines(adapter, request.company_id, tuple(parents)) if parents else []
    _validate_lines(lines, parents)
    _validate_line_metadata(lines)
    grouped: dict[int, list[PayslipLine]] = defaultdict(list)
    for line in lines:
        grouped[line.salary_rule.id].append(line)
    all_items: list[SalaryRuleSnapshot] = []
    for rows in grouped.values():
        sample = rows[0]
        currencies = {row.currency.id: row.currency.name for row in rows}
        all_items.append(
            SalaryRuleSnapshot(
                salary_rule=PayrollNamedReference(
                    id=sample.salary_rule.id,
                    name=sample.salary_rule.name,
                ),
                code=sample.code,
                category=PayrollNamedReference(
                    id=sample.category.id,
                    name=sample.category.name,
                ),
                sequence=sample.sequence,
                affected_payslip_count=len({row.payslip.id for row in rows}),
                currencies=[
                    PayrollCurrencyReference(id=identifier, name=name)
                    for identifier, name in sorted(currencies.items())
                ],
            )
        )
    all_items.sort(key=lambda item: (item.sequence, item.salary_rule.id))
    anchor_models: dict[str, Iterable[PayrollValue]] = {
        "hr.payslip": base,
        "hr.payslip.line": lines,
    }
    if batch is not None:
        anchor_models["hr.payslip.run"] = [batch]
    selected, next_cursor = _paginate(
        all_items,
        limit=request.limit,
        cursor=request.cursor,
        binding=request.model_dump(mode="json", exclude={"cursor"}),
        anchors=_anchors(anchor_models),
    )
    refs: dict[str, Iterable[int]] = {
        "hr.payslip": (row.id for row in payslips),
        "hr.payslip.line": (row.id for row in lines),
    }
    if batch is not None:
        refs["hr.payslip.run"] = (batch.id,)
    return ListSalaryRulesResponse(
        request_id=request_id,
        company_id=request.company_id,
        observed_at=observed_at,
        source_refs=_source_refs(refs),
        limitations=[],
        items=selected,
        next_cursor=next_cursor,
        summary=SalaryRulesSummary(
            rule_count=len(all_items),
            returned_count=len(selected),
            affected_payslip_count=len({row.payslip.id for row in lines}),
            has_more=next_cursor is not None,
        ),
    )


async def get_attendance_summary(
    adapter: OdooAdapter,
    request: GetAttendanceSummaryInput,
    *,
    request_id: str,
    observed_at: datetime,
) -> GetAttendanceSummaryResponse:
    employees = await _all_employees(adapter, request.company_id, request.employee_ids)
    employee_map = {row.id: row for row in employees}
    if set(employee_map) != set(request.employee_ids):
        if any(
            row.company_id != request.company_id or row.id not in request.employee_ids
            for row in employees
        ):
            raise _invalid_source()
        raise _error(
            ErrorCode.EMPLOYEE_NOT_FOUND,
            "A requested Payroll employee was not found.",
            "Use exact employee IDs from the authorized company.",
        )
    if any(row.company_id != request.company_id for row in employees):
        raise _invalid_source()
    period = PayrollPeriod(start=request.period_start, end=request.period_end)
    entries = await _all_work_entries(
        adapter,
        request.company_id,
        PayrollWorkEntryFilters(
            employee_ids=request.employee_ids,
            period=period,
            states=request.states,
        ),
    )
    segments: list[PayrollContractSegment] = []
    if any(entry.contract_segment is not None for entry in entries):
        segments = await _all_segments(
            adapter,
            request.company_id,
            PayrollContractFilters(employee_ids=request.employee_ids, period=period),
        )
    segment_map = {segment.id: segment for segment in segments}
    if len(segment_map) != len(segments):
        raise _malformed_source()
    type_names: dict[int, str] = {}
    grouped: dict[tuple[int, int, str, str], list[PayrollWorkEntry]] = defaultdict(list)
    for entry in entries:
        if (
            entry.company_id != request.company_id
            or entry.employee.id not in employee_map
            or entry.date_end < period.start
            or entry.date_start > period.end
            or (request.states and entry.state not in request.states)
            or entry.duration < 0
        ):
            raise _invalid_source()
        employee = employee_map[entry.employee.id]
        if employee.name != entry.employee.name:
            raise _invalid_source()
        if entry.contract_segment is None:
            if entry.contract_source is not None or segments:
                raise _invalid_source()
        else:
            segment = segment_map.get(entry.contract_segment.id)
            if (
                entry.contract_source is None
                or segment is None
                or segment.source_model != entry.contract_source
                or segment.employee.id != entry.employee.id
                or segment.employee.name != employee.name
                or segment.company_id != request.company_id
                or entry.date_start < segment.effective_start
                or entry.date_end > segment.effective_end
            ):
                raise _invalid_source()
        prior_name = type_names.get(entry.work_entry_type.id)
        if prior_name is not None and prior_name != entry.work_entry_type.name:
            raise _invalid_source()
        type_names[entry.work_entry_type.id] = entry.work_entry_type.name
        grouped[
            (
                entry.employee.id,
                entry.work_entry_type.id,
                entry.code,
                entry.state,
            )
        ].append(entry)
    all_items: list[AttendanceSummaryItem] = []
    for rows in grouped.values():
        sample = rows[0]
        employee = employee_map[sample.employee.id]
        all_items.append(
            AttendanceSummaryItem(
                employee=PayrollNamedReference(id=employee.id, name=employee.name),
                work_entry_type=PayrollNamedReference(
                    id=sample.work_entry_type.id,
                    name=sample.work_entry_type.name,
                ),
                code=sample.code,
                state=sample.state,
                entry_count=len(rows),
                hours=sum((row.duration for row in rows), Decimal("0")),
                earliest_source_date=min(row.date_start for row in rows),
                latest_source_date=max(row.date_end for row in rows),
                conflict_count=sum(row.conflict for row in rows),
            )
        )
    all_items.sort(
        key=lambda item: (
            item.employee.id,
            item.work_entry_type.id,
            item.code.casefold(),
            item.state,
        )
    )
    anchor_models: dict[str, list[PayrollValue]] = {
        "hr.employee": list(employees),
        "hr.work.entry": list(entries),
    }
    source_models: dict[str, list[int]] = {
        "hr.employee": [row.id for row in employees],
        "hr.work.entry": [row.id for row in entries],
    }
    for segment in segments:
        anchor_models.setdefault(segment.source_model, []).append(segment)
        source_models.setdefault(segment.source_model, []).append(segment.id)
    selected, next_cursor = _paginate(
        all_items,
        limit=request.limit,
        cursor=request.cursor,
        binding=request.model_dump(mode="json", exclude={"cursor"}),
        anchors=_anchors(anchor_models),
    )
    return GetAttendanceSummaryResponse(
        request_id=request_id,
        company_id=request.company_id,
        observed_at=observed_at,
        source_refs=_source_refs(source_models),
        limitations=["work_entries_do_not_prove_physical_attendance"],
        items=selected,
        next_cursor=next_cursor,
        summary=AttendanceSummary(
            selected_employee_count=len(employees),
            employees_with_entries=len({row.employee.id for row in entries}),
            entry_count=len(entries),
            returned_count=len(selected),
            hours=sum((row.duration for row in entries), Decimal("0")),
            conflict_count=sum(row.conflict for row in entries),
            has_more=next_cursor is not None,
        ),
    )
