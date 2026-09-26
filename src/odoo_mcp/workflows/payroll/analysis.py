"""Deterministic request-time Payroll comparison and analysis workflows."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from odoo_mcp.adapters.base import OdooAdapter
from odoo_mcp.adapters.payroll import (
    PayrollContractFilters,
    PayrollContractSegment,
    PayrollEmployee,
    PayrollPeriod,
    PayrollState,
    PayrollWorkEntry,
    PayrollWorkEntryFilters,
    Payslip,
    PayslipLine,
)
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.payroll_schemas import (
    AnalyzeEmployeePayrollChangeInput,
    AnalyzeEmployeePayrollChangeResponse,
    ComparePayrollPeriodsInput,
    ComparePayrollPeriodsResponse,
    DetectPayrollAnomaliesInput,
    DetectPayrollAnomaliesResponse,
    ExplainPayslipInput,
    ExplainPayslipResponse,
    GetPayslipInput,
    GetPayslipResponse,
    PayrollCategoryTotalComparison,
    PayrollChangeKind,
    PayrollContractFactChange,
    PayrollContractSegmentItem,
    PayrollCountComparison,
    PayrollCurrencyReference,
    PayrollDecimalComparison,
    PayrollEmployeeSetChanges,
    PayrollEvidenceChange,
    PayrollExplanationGroup,
    PayrollFinding,
    PayrollLineArithmetic,
    PayrollLineChange,
    PayrollNamedReference,
    PayrollPayslipTotalComparison,
    PayrollPeriodRange,
    PayrollRecognizedCurrencyTotal,
    PayrollRecognizedPeriodValue,
    PayrollRecognizedRuleComparison,
    PayrollRecognizedRuleValue,
    PayrollRuleTotalComparison,
    PayrollSalaryLineItem,
    PayrollSourceReference,
    PayrollUnavailableMetric,
    PayrollWorkEntryHoursComparison,
    ThresholdProfile,
)
from odoo_mcp.workflows.payroll import evidence

_FOUR_PLACES = Decimal("0.0001")
_MODIFIED_Z_FACTOR = Decimal("0.6745")
_MODIFIED_Z_THRESHOLD = Decimal("3.5")
_THRESHOLDS: Mapping[ThresholdProfile, Decimal] = {
    "strict": Decimal("5"),
    "standard": Decimal("10"),
    "relaxed": Decimal("20"),
}
_RECOGNIZED_CODES = ("BASIC", "GROSS", "NET")
_MAX_PAYSLIPS = 5_000
_MAX_LINES = 100_000
_MAX_SEGMENTS = 20_000
_MAX_WORK_ENTRIES = 100_000


@dataclass(frozen=True)
class _Snapshot:
    period: PayrollPeriodRange
    payslips: tuple[Payslip, ...]
    lines: tuple[PayslipLine, ...]
    segments: tuple[PayrollContractSegment, ...]
    work_entries: tuple[PayrollWorkEntry, ...]


@dataclass(frozen=True)
class _LineAggregate:
    employee: PayrollNamedReference
    currency: PayrollCurrencyReference
    salary_rule: PayrollNamedReference
    category: PayrollNamedReference
    code: str
    total: Decimal
    lines: tuple[PayslipLine, ...]


def _error(code: ErrorCode, message: str, hint: str) -> OdooMcpError:
    return OdooMcpError(code, message, hint)


def _invalid_source() -> OdooMcpError:
    return _error(
        ErrorCode.PAYROLL_SOURCE_INCONSISTENT,
        "Odoo returned inconsistent Payroll source data.",
        "Check the requested company, identities, relationships, and Payroll records, then retry.",
    )


def _too_large() -> OdooMcpError:
    return _error(
        ErrorCode.PAYROLL_RESULT_TOO_LARGE,
        "The Payroll analysis exceeds the safe processing bound.",
        "Narrow the Payroll periods or employee identifiers and retry.",
    )


def _period(value: PayrollPeriodRange) -> PayrollPeriod:
    return PayrollPeriod(start=value.period_start, end=value.period_end)


def _named(identifier: int, name: str) -> PayrollNamedReference:
    return PayrollNamedReference(id=identifier, name=name)


def _currency(identifier: int, name: str) -> PayrollCurrencyReference:
    return PayrollCurrencyReference(id=identifier, name=name)


def _merge_source_refs(
    *groups: Iterable[PayrollSourceReference],
) -> list[PayrollSourceReference]:
    identifiers: dict[str, set[int]] = defaultdict(set)
    for group in groups:
        for reference in group:
            identifiers[reference.source_model].update(reference.source_ids)
    return [
        PayrollSourceReference(source_model=model, source_ids=tuple(sorted(values)))
        for model, values in sorted(identifiers.items())
        if values
    ]


def _snapshot_refs(snapshot: _Snapshot) -> list[PayrollSourceReference]:
    models: dict[str, list[int]] = {
        "hr.payslip": [row.id for row in snapshot.payslips],
        "hr.payslip.line": [row.id for row in snapshot.lines],
        "hr.work.entry": [row.id for row in snapshot.work_entries],
    }
    for segment in snapshot.segments:
        models.setdefault(segment.source_model, []).append(segment.id)
    return evidence._source_refs(models)


def _employee_refs(snapshot: _Snapshot, employee_id: int) -> list[PayrollSourceReference]:
    payslip_ids = {row.id for row in snapshot.payslips if row.employee.id == employee_id}
    models: dict[str, list[int]] = {
        "hr.payslip": sorted(payslip_ids),
        "hr.payslip.line": [row.id for row in snapshot.lines if row.payslip.id in payslip_ids],
        "hr.work.entry": [
            row.id for row in snapshot.work_entries if row.employee.id == employee_id
        ],
    }
    for segment in snapshot.segments:
        if segment.employee.id == employee_id:
            models.setdefault(segment.source_model, []).append(segment.id)
    return evidence._source_refs(models)


def _validate_segments(
    segments: Sequence[PayrollContractSegment],
    *,
    company_id: int,
    employees: Mapping[int, str],
    period: PayrollPeriodRange,
) -> None:
    previous: dict[int, PayrollContractSegment] = {}
    reference_names: dict[tuple[str, int], str] = {}
    for segment in sorted(segments, key=lambda row: (row.employee.id, row.effective_start, row.id)):
        if (
            segment.company_id != company_id
            or segment.employee.id not in employees
            or employees[segment.employee.id] != segment.employee.name
            or segment.effective_start < period.period_start
            or segment.effective_end > period.period_end
            or segment.effective_end < segment.effective_start
        ):
            raise _invalid_source()
        prior = previous.get(segment.employee.id)
        if prior is not None and prior.effective_end >= segment.effective_start:
            raise _invalid_source()
        previous[segment.employee.id] = segment
        related = (
            ("res.currency", segment.currency.id, segment.currency.name),
            ("hr.payroll.structure.type", segment.structure_type),
            ("resource.calendar", segment.resource_calendar),
            ("hr.department", segment.department),
            ("hr.job", segment.job),
            ("hr.contract.type", segment.contract_type),
        )
        for value in related:
            if len(value) == 3:
                model, identifier, name = value
            else:
                model, record = value
                if record is None:
                    continue
                identifier, name = record.id, record.name
            key = (str(model), int(identifier))
            if key in reference_names and reference_names[key] != str(name):
                raise _invalid_source()
            reference_names[key] = str(name)


def _validate_work_entries(
    rows: Sequence[PayrollWorkEntry],
    *,
    company_id: int,
    employees: Mapping[int, str],
    segments: Sequence[PayrollContractSegment],
    period: PayrollPeriodRange,
) -> None:
    segment_map = {(row.source_model, row.id): row for row in segments}
    type_names: dict[int, str] = {}
    for row in rows:
        if (
            row.company_id != company_id
            or row.employee.id not in employees
            or employees[row.employee.id] != row.employee.name
            or row.date_start < period.period_start
            or row.date_end > period.period_end
            or row.date_end < row.date_start
            or row.duration < 0
            or row.state == "cancelled"
        ):
            raise _invalid_source()
        if row.contract_segment is not None:
            if row.contract_source is None:
                raise _invalid_source()
            segment = segment_map.get((row.contract_source, row.contract_segment.id))
            if (
                segment is None
                or segment.employee.id != row.employee.id
                or row.date_start < segment.effective_start
                or row.date_end > segment.effective_end
            ):
                raise _invalid_source()
        elif row.contract_source is not None:
            raise _invalid_source()
        prior_name = type_names.get(row.work_entry_type.id)
        if prior_name is not None and prior_name != row.work_entry_type.name:
            raise _invalid_source()
        type_names[row.work_entry_type.id] = row.work_entry_type.name


async def _load_snapshot(
    adapter: OdooAdapter,
    *,
    company_id: int,
    period: PayrollPeriodRange,
    employee_ids: tuple[int, ...],
    states: tuple[PayrollState, ...],
    include_context: bool = True,
) -> _Snapshot:
    all_payslips = await evidence._period_payslips(adapter, company_id, _period(period))
    selected = [
        row
        for row in all_payslips
        if row.state in states
        and row.state != "cancelled"
        and (not employee_ids or row.employee.id in employee_ids)
    ]
    employees: dict[int, str] = {}
    currencies: dict[int, str] = {}
    for row in selected:
        prior_name = employees.get(row.employee.id)
        prior_currency = currencies.get(row.currency.id)
        if (prior_name is not None and prior_name != row.employee.name) or (
            prior_currency is not None and prior_currency != row.currency.name
        ):
            raise _invalid_source()
        employees[row.employee.id] = row.employee.name
        currencies[row.currency.id] = row.currency.name
    payslip_ids = tuple(row.id for row in selected)
    lines = await evidence._all_lines(adapter, company_id, payslip_ids) if payslip_ids else []
    parents = {row.id: row for row in selected}
    evidence._validate_lines(lines, parents)
    evidence._validate_line_metadata(lines)
    segments: list[PayrollContractSegment] = []
    work_entries: list[PayrollWorkEntry] = []
    selected_employee_ids = tuple(sorted(employees))
    if include_context and selected_employee_ids:
        segments = await evidence._all_segments(
            adapter,
            company_id,
            PayrollContractFilters(
                employee_ids=selected_employee_ids,
                period=_period(period),
            ),
        )
        _validate_segments(
            segments,
            company_id=company_id,
            employees=employees,
            period=period,
        )
        work_entries = await evidence._all_work_entries(
            adapter,
            company_id,
            PayrollWorkEntryFilters(
                employee_ids=selected_employee_ids,
                period=_period(period),
                states=("draft", "conflict", "validated"),
            ),
        )
        _validate_work_entries(
            work_entries,
            company_id=company_id,
            employees=employees,
            segments=segments,
            period=period,
        )
    return _Snapshot(
        period=period,
        payslips=tuple(selected),
        lines=tuple(lines),
        segments=tuple(segments),
        work_entries=tuple(work_entries),
    )


def _enforce_combined_caps(snapshots: Sequence[_Snapshot]) -> None:
    if (
        sum(len(value.payslips) for value in snapshots) > _MAX_PAYSLIPS
        or sum(len(value.lines) for value in snapshots) > _MAX_LINES
        or sum(len(value.segments) for value in snapshots) > _MAX_SEGMENTS
        or sum(len(value.work_entries) for value in snapshots) > _MAX_WORK_ENTRIES
    ):
        raise _too_large()


def _round(value: Decimal) -> Decimal:
    return value.quantize(_FOUR_PLACES, rounding=ROUND_HALF_EVEN)


def _percentage(baseline: Decimal, target: Decimal) -> Decimal | None:
    if baseline == 0:
        return None
    return _round((target - baseline) / abs(baseline) * Decimal("100"))


def _change(baseline: Decimal, target: Decimal) -> PayrollChangeKind:
    if baseline == target:
        return "unchanged"
    if baseline == 0:
        return "added"
    if target == 0:
        return "removed"
    return "increased" if target > baseline else "decreased"


def _decimal_comparison(baseline: Decimal, target: Decimal) -> PayrollDecimalComparison:
    return PayrollDecimalComparison(
        baseline=baseline,
        target=target,
        absolute_delta=target - baseline,
        percentage_delta=_percentage(baseline, target),
        change=_change(baseline, target),
    )


def _count_comparison(baseline: int, target: int) -> PayrollCountComparison:
    decimal_change = _decimal_comparison(Decimal(baseline), Decimal(target))
    return PayrollCountComparison(
        baseline=baseline,
        target=target,
        absolute_delta=target - baseline,
        percentage_delta=decimal_change.percentage_delta,
        change=decimal_change.change,
    )


def _employee_names(snapshot: _Snapshot) -> dict[int, str]:
    return {row.employee.id: row.employee.name for row in snapshot.payslips}


def _validate_cross_snapshot_metadata(snapshots: Sequence[_Snapshot]) -> None:
    references: dict[tuple[str, int], str] = {}
    rules: dict[tuple[int, str], tuple[str, int, str]] = {}

    def register(model: str, identifier: int, name: str) -> None:
        key = (model, identifier)
        if key in references and references[key] != name:
            raise _invalid_source()
        references[key] = name

    for snapshot in snapshots:
        for payslip in snapshot.payslips:
            register("hr.employee", payslip.employee.id, payslip.employee.name)
            register("res.currency", payslip.currency.id, payslip.currency.name)
            register(
                payslip.contract_source,
                payslip.contract_segment.id,
                payslip.contract_segment.name,
            )
            register("hr.payroll.structure", payslip.structure.id, payslip.structure.name)
            if payslip.batch is not None:
                register("hr.payslip.run", payslip.batch.id, payslip.batch.name)
        for line in snapshot.lines:
            key = (line.salary_rule.id, line.code)
            metadata = (line.salary_rule.name, line.category.id, line.category.name)
            prior_rule = rules.get(key)
            if prior_rule is not None and prior_rule != metadata:
                raise _invalid_source()
            rules[key] = metadata
            register("hr.salary.rule", line.salary_rule.id, line.salary_rule.name)
            register("hr.salary.rule.category", line.category.id, line.category.name)
            register("res.currency", line.currency.id, line.currency.name)
        for segment in snapshot.segments:
            register("hr.employee", segment.employee.id, segment.employee.name)
            register("res.currency", segment.currency.id, segment.currency.name)
        for entry in snapshot.work_entries:
            register("hr.employee", entry.employee.id, entry.employee.name)
            register(
                "hr.work.entry.type",
                entry.work_entry_type.id,
                entry.work_entry_type.name,
            )


def _line_aggregates(snapshot: _Snapshot) -> dict[tuple[int, int, str], _LineAggregate]:
    grouped: dict[tuple[int, int, str], list[PayslipLine]] = defaultdict(list)
    employee_currencies: dict[int, int] = {}
    for line in snapshot.lines:
        prior_currency = employee_currencies.get(line.employee.id)
        if prior_currency is not None and prior_currency != line.currency.id:
            raise _invalid_source()
        employee_currencies[line.employee.id] = line.currency.id
        grouped[(line.employee.id, line.salary_rule.id, line.code)].append(line)
    result: dict[tuple[int, int, str], _LineAggregate] = {}
    for key, rows in grouped.items():
        sample = rows[0]
        if any(
            row.currency.id != sample.currency.id
            or row.salary_rule.name != sample.salary_rule.name
            or row.category.id != sample.category.id
            or row.category.name != sample.category.name
            for row in rows
        ):
            raise _invalid_source()
        result[key] = _LineAggregate(
            employee=_named(sample.employee.id, sample.employee.name),
            currency=_currency(sample.currency.id, sample.currency.name),
            salary_rule=_named(sample.salary_rule.id, sample.salary_rule.name),
            category=_named(sample.category.id, sample.category.name),
            code=sample.code,
            total=sum((row.total for row in rows), Decimal("0")),
            lines=tuple(rows),
        )
    return result


def _line_refs(*aggregates: _LineAggregate | None) -> list[PayrollSourceReference]:
    return evidence._source_refs(
        {
            "hr.payslip.line": (
                row.id
                for aggregate in aggregates
                if aggregate is not None
                for row in aggregate.lines
            ),
            "hr.payslip": (
                row.payslip.id
                for aggregate in aggregates
                if aggregate is not None
                for row in aggregate.lines
            ),
        }
    )


def _line_changes(
    baseline: _Snapshot,
    target: _Snapshot,
) -> list[PayrollLineChange]:
    baseline_values = _line_aggregates(baseline)
    target_values = _line_aggregates(target)
    common_employees = set(_employee_names(baseline)) & set(_employee_names(target))
    changes: list[PayrollLineChange] = []
    for key in sorted(set(baseline_values) | set(target_values)):
        employee_id, _rule_id, _code = key
        if employee_id not in common_employees:
            continue
        before = baseline_values.get(key)
        after = target_values.get(key)
        sample = after or before
        assert sample is not None
        before_total = None if before is None else before.total
        after_total = None if after is None else after.total
        if before_total == after_total:
            continue
        if before is not None and after is not None and before.currency.id != after.currency.id:
            changes.append(
                PayrollLineChange(
                    employee=sample.employee,
                    currency=None,
                    salary_rule=sample.salary_rule,
                    code=sample.code,
                    category=sample.category,
                    baseline_total=before.total,
                    target_total=after.total,
                    absolute_delta=None,
                    percentage_delta=None,
                    change="unavailable",
                    source_refs=_line_refs(before, after),
                    limitations=["currency_change_prevents_amount_comparison"],
                )
            )
            continue
        baseline_value = before_total or Decimal("0")
        target_value = after_total or Decimal("0")
        if (before is None and target_value == 0) or (after is None and baseline_value == 0):
            continue
        comparison = _decimal_comparison(baseline_value, target_value)
        changes.append(
            PayrollLineChange(
                employee=sample.employee,
                currency=sample.currency,
                salary_rule=sample.salary_rule,
                code=sample.code,
                category=sample.category,
                baseline_total=before_total,
                target_total=after_total,
                absolute_delta=comparison.absolute_delta,
                percentage_delta=comparison.percentage_delta,
                change=comparison.change,
                source_refs=_line_refs(before, after),
                limitations=[],
            )
        )
    return changes


def _payslip_totals(
    baseline: _Snapshot,
    target: _Snapshot,
) -> list[PayrollPayslipTotalComparison]:
    def collect(
        snapshot: _Snapshot,
    ) -> tuple[
        dict[tuple[int, int], Decimal],
        dict[tuple[int, int], list[PayslipLine]],
        dict[tuple[int, int], set[int]],
    ]:
        totals: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
        lines: dict[tuple[int, int], list[PayslipLine]] = defaultdict(list)
        payslips: dict[tuple[int, int], set[int]] = defaultdict(set)
        payslip_map = {row.id: row for row in snapshot.payslips}
        for line in snapshot.lines:
            key = (line.employee.id, line.currency.id)
            totals[key] += line.total
            lines[key].append(line)
            payslips[key].add(line.payslip.id)
        for payslip in snapshot.payslips:
            key = (payslip.employee.id, payslip.currency.id)
            totals.setdefault(key, Decimal("0"))
            payslips[key].add(payslip.id)
            if payslip.id not in payslip_map:
                raise _invalid_source()
        return totals, lines, payslips

    base_totals, base_lines, base_payslips = collect(baseline)
    target_totals, target_lines, target_payslips = collect(target)
    names = {**_employee_names(baseline), **_employee_names(target)}
    currencies = {
        (row.currency.id): row.currency.name
        for snapshot in (baseline, target)
        for row in snapshot.payslips
    }
    result: list[PayrollPayslipTotalComparison] = []
    for key in sorted(set(base_totals) | set(target_totals)):
        employee_id, currency_id = key
        line_ids = [row.id for row in base_lines[key]] + [row.id for row in target_lines[key]]
        payslip_ids = base_payslips[key] | target_payslips[key]
        result.append(
            PayrollPayslipTotalComparison(
                employee=_named(employee_id, names[employee_id]),
                currency=_currency(currency_id, currencies[currency_id]),
                baseline_payslip_ids=tuple(sorted(base_payslips[key])),
                target_payslip_ids=tuple(sorted(target_payslips[key])),
                observed_line_total=_decimal_comparison(
                    base_totals.get(key, Decimal("0")),
                    target_totals.get(key, Decimal("0")),
                ),
                source_refs=evidence._source_refs(
                    {
                        "hr.payslip": payslip_ids,
                        "hr.payslip.line": line_ids,
                    }
                ),
            )
        )
    return result


def _rule_totals(
    baseline: _Snapshot,
    target: _Snapshot,
) -> list[PayrollRuleTotalComparison]:
    def collect(
        snapshot: _Snapshot,
    ) -> dict[tuple[int, int, str, int], tuple[Decimal, list[PayslipLine]]]:
        grouped: dict[tuple[int, int, str, int], list[PayslipLine]] = defaultdict(list)
        for line in snapshot.lines:
            grouped[(line.currency.id, line.salary_rule.id, line.code, line.category.id)].append(
                line
            )
        return {
            key: (sum((row.total for row in rows), Decimal("0")), rows)
            for key, rows in grouped.items()
        }

    base = collect(baseline)
    target_values = collect(target)
    result: list[PayrollRuleTotalComparison] = []
    for key in sorted(set(base) | set(target_values)):
        before = base.get(key)
        after = target_values.get(key)
        rows = (after or before)[1]  # type: ignore[index]
        sample = rows[0]
        before_total = Decimal("0") if before is None else before[0]
        after_total = Decimal("0") if after is None else after[0]
        all_rows = ([] if before is None else before[1]) + ([] if after is None else after[1])
        result.append(
            PayrollRuleTotalComparison(
                currency=_currency(sample.currency.id, sample.currency.name),
                salary_rule=_named(sample.salary_rule.id, sample.salary_rule.name),
                code=sample.code,
                category=_named(sample.category.id, sample.category.name),
                total=_decimal_comparison(before_total, after_total),
                source_refs=evidence._source_refs(
                    {
                        "hr.payslip.line": (row.id for row in all_rows),
                        "hr.payslip": (row.payslip.id for row in all_rows),
                    }
                ),
            )
        )
    return result


def _category_totals(
    baseline: _Snapshot,
    target: _Snapshot,
) -> list[PayrollCategoryTotalComparison]:
    def collect(
        snapshot: _Snapshot,
    ) -> dict[tuple[int, int], tuple[Decimal, list[PayslipLine]]]:
        grouped: dict[tuple[int, int], list[PayslipLine]] = defaultdict(list)
        for line in snapshot.lines:
            grouped[(line.currency.id, line.category.id)].append(line)
        return {
            key: (sum((row.total for row in rows), Decimal("0")), rows)
            for key, rows in grouped.items()
        }

    base = collect(baseline)
    target_values = collect(target)
    result: list[PayrollCategoryTotalComparison] = []
    for key in sorted(set(base) | set(target_values)):
        before = base.get(key)
        after = target_values.get(key)
        rows = (after or before)[1]  # type: ignore[index]
        sample = rows[0]
        all_rows = ([] if before is None else before[1]) + ([] if after is None else after[1])
        result.append(
            PayrollCategoryTotalComparison(
                currency=_currency(sample.currency.id, sample.currency.name),
                category=_named(sample.category.id, sample.category.name),
                total=_decimal_comparison(
                    Decimal("0") if before is None else before[0],
                    Decimal("0") if after is None else after[0],
                ),
                source_refs=evidence._source_refs(
                    {
                        "hr.payslip.line": (row.id for row in all_rows),
                        "hr.payslip": (row.payslip.id for row in all_rows),
                    }
                ),
            )
        )
    return result


def _recognized_value(lines: Sequence[PayslipLine], code: str) -> PayrollRecognizedPeriodValue:
    selected = [row for row in lines if row.code == code]
    rule_ids = {row.salary_rule.id for row in selected}
    if not selected:
        return PayrollRecognizedPeriodValue(
            status="unavailable",
            values=[],
            reason="exact_rule_code_missing",
        )
    if len(rule_ids) != 1:
        return PayrollRecognizedPeriodValue(
            status="unavailable",
            values=[],
            reason="exact_rule_code_duplicate",
        )
    grouped: dict[int, list[PayslipLine]] = defaultdict(list)
    for row in selected:
        grouped[row.currency.id].append(row)
    values = []
    for currency_id, rows in sorted(grouped.items()):
        sample = rows[0]
        values.append(
            PayrollRecognizedCurrencyTotal(
                currency=_currency(currency_id, sample.currency.name),
                total=sum((row.total for row in rows), Decimal("0")),
                source_refs=evidence._source_refs(
                    {
                        "hr.payslip.line": (row.id for row in rows),
                        "hr.payslip": (row.payslip.id for row in rows),
                    }
                ),
            )
        )
    return PayrollRecognizedPeriodValue(status="available", values=values)


def _recognized_comparisons(
    baseline: _Snapshot,
    target: _Snapshot,
) -> list[PayrollRecognizedRuleComparison]:
    return [
        PayrollRecognizedRuleComparison(
            code=code,  # type: ignore[arg-type]
            baseline=_recognized_value(baseline.lines, code),
            target=_recognized_value(target.lines, code),
        )
        for code in _RECOGNIZED_CODES
    ]


def _render_related(value: object | None) -> str | None:
    if value is None:
        return None
    identifier = getattr(value, "id", None)
    name = getattr(value, "name", None)
    if not isinstance(identifier, int) or not isinstance(name, str):
        raise _invalid_source()
    return f"{identifier}:{name}"


def _segment_facts(segments: Sequence[PayrollContractSegment]) -> dict[str, str | None]:
    ordered = sorted(segments, key=lambda row: (row.effective_start, row.id))

    def joined(values: Iterable[str]) -> str | None:
        materialized = list(values)
        return ";".join(materialized) if materialized else None

    return {
        "source_segments": joined(
            f"{row.source_model}:{row.id}:"
            f"{row.revision_date.isoformat() if row.revision_date else 'unavailable'}"
            for row in ordered
        ),
        "effective_dates": joined(
            f"{row.effective_start.isoformat()}..{row.effective_end.isoformat()}" for row in ordered
        ),
        "active": joined(str(row.active).lower() for row in ordered),
        "source_status": joined(row.source_status for row in ordered),
        "wage": joined(str(row.wage) for row in ordered),
        "currency": joined(f"{row.currency.id}:{row.currency.name}" for row in ordered),
        "structure_type": joined(str(_render_related(row.structure_type)) for row in ordered),
        "working_schedule": joined(str(_render_related(row.resource_calendar)) for row in ordered),
        "department": joined(str(_render_related(row.department)) for row in ordered),
        "job": joined(str(_render_related(row.job)) for row in ordered),
        "contract_type": joined(str(_render_related(row.contract_type)) for row in ordered),
    }


def _contract_changes(
    baseline: _Snapshot,
    target: _Snapshot,
) -> list[PayrollContractFactChange]:
    base_grouped: dict[int, list[PayrollContractSegment]] = defaultdict(list)
    target_grouped: dict[int, list[PayrollContractSegment]] = defaultdict(list)
    for row in baseline.segments:
        base_grouped[row.employee.id].append(row)
    for row in target.segments:
        target_grouped[row.employee.id].append(row)
    names = {**_employee_names(baseline), **_employee_names(target)}
    changes: list[PayrollContractFactChange] = []

    def coverage(
        rows: Sequence[PayrollContractSegment],
        period: PayrollPeriodRange,
    ) -> tuple[tuple[int, int], ...]:
        return tuple(
            (
                (row.effective_start - period.period_start).days,
                (period.period_end - row.effective_end).days,
            )
            for row in sorted(rows, key=lambda value: (value.effective_start, value.id))
        )

    for employee_id in sorted(set(base_grouped) | set(target_grouped)):
        before_rows = base_grouped[employee_id]
        after_rows = target_grouped[employee_id]
        before = _segment_facts(before_rows)
        after = _segment_facts(after_rows)
        source_models: dict[str, list[int]] = defaultdict(list)
        for row in (*before_rows, *after_rows):
            source_models[row.source_model].append(row.id)
        source_refs = evidence._source_refs(source_models)
        for fact in before:
            if (
                fact == "effective_dates"
                and before["source_segments"] == after["source_segments"]
                and coverage(before_rows, baseline.period) == coverage(after_rows, target.period)
            ):
                continue
            if before[fact] != after[fact]:
                changes.append(
                    PayrollContractFactChange(
                        employee=_named(employee_id, names[employee_id]),
                        fact=fact,
                        baseline_value=before[fact],
                        target_value=after[fact],
                        source_refs=source_refs,
                    )
                )
    return changes


def _work_entry_hours(
    baseline: _Snapshot,
    target: _Snapshot,
) -> list[PayrollWorkEntryHoursComparison]:
    def collect(
        snapshot: _Snapshot,
    ) -> dict[tuple[int, int, str], tuple[Decimal, int, list[PayrollWorkEntry]]]:
        grouped: dict[tuple[int, int, str], list[PayrollWorkEntry]] = defaultdict(list)
        for row in snapshot.work_entries:
            grouped[(row.employee.id, row.work_entry_type.id, row.code)].append(row)
        return {
            key: (
                sum((row.duration for row in rows), Decimal("0")),
                sum(int(row.conflict or row.state == "conflict") for row in rows),
                rows,
            )
            for key, rows in grouped.items()
        }

    base = collect(baseline)
    target_values = collect(target)
    names = {**_employee_names(baseline), **_employee_names(target)}
    result: list[PayrollWorkEntryHoursComparison] = []
    for key in sorted(set(base) | set(target_values)):
        employee_id, _type_id, code = key
        before = base.get(key)
        after = target_values.get(key)
        rows = (after or before)[2]  # type: ignore[index]
        sample = rows[0]
        all_rows = ([] if before is None else before[2]) + ([] if after is None else after[2])
        result.append(
            PayrollWorkEntryHoursComparison(
                employee=_named(employee_id, names[employee_id]),
                work_entry_type=_named(
                    sample.work_entry_type.id,
                    sample.work_entry_type.name,
                ),
                code=code,
                hours=_decimal_comparison(
                    Decimal("0") if before is None else before[0],
                    Decimal("0") if after is None else after[0],
                ),
                conflict_count=0 if after is None else after[1],
                source_refs=evidence._source_refs({"hr.work.entry": (row.id for row in all_rows)}),
            )
        )
    return result


def _fact(
    fact: str,
    baseline_value: str | None,
    target_value: str | None,
    refs: list[PayrollSourceReference],
) -> PayrollEvidenceChange:
    return PayrollEvidenceChange(
        fact=fact,
        baseline_value=baseline_value,
        target_value=target_value,
        source_refs=refs,
    )


def _finding(
    *,
    code: str,
    severity: str,
    baseline: PayrollPeriodRange,
    target: PayrollPeriodRange,
    calculation: str,
    rule: str,
    refs: list[PayrollSourceReference],
    employee: PayrollNamedReference | None = None,
    currency: PayrollCurrencyReference | None = None,
    salary_rule: PayrollNamedReference | None = None,
    line_code: str | None = None,
    baseline_value: str | None = None,
    target_value: str | None = None,
    absolute_delta: Decimal | None = None,
    percentage_delta: Decimal | None = None,
    observed_delta: Decimal | None = None,
    threshold: Decimal | None = None,
    threshold_profile: ThresholdProfile | None = None,
    facts: Sequence[PayrollEvidenceChange] = (),
    correlations: Sequence[PayrollEvidenceChange] = (),
    limitations: Sequence[str] = (),
) -> PayrollFinding:
    return PayrollFinding(
        finding_code=code,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        employee=employee,
        baseline_period=baseline,
        target_period=target,
        currency=currency,
        salary_rule=salary_rule,
        code=line_code,
        baseline_value=baseline_value,
        target_value=target_value,
        absolute_delta=absolute_delta,
        percentage_delta=percentage_delta,
        observed_delta=observed_delta,
        calculation=calculation,
        rule=rule,
        threshold=threshold,
        threshold_profile=threshold_profile,
        source_refs=refs,
        evidence=list(facts),
        correlations=list(correlations),
        limitations=list(limitations),
    )


def _base_findings(
    baseline: _Snapshot,
    target: _Snapshot,
    line_changes: Sequence[PayrollLineChange],
    contract_changes: Sequence[PayrollContractFactChange],
) -> list[PayrollFinding]:
    before_names = _employee_names(baseline)
    after_names = _employee_names(target)
    before_ids = set(before_names)
    after_ids = set(after_names)
    result: list[PayrollFinding] = []
    for employee_id in sorted(after_ids - before_ids):
        employee = _named(employee_id, after_names[employee_id])
        refs = _employee_refs(target, employee_id)
        fact = _fact("employee_set_membership", "absent", "present", refs)
        result.append(
            _finding(
                code="new_employee",
                severity="info",
                baseline=baseline.period,
                target=target.period,
                employee=employee,
                baseline_value="absent",
                target_value="present",
                calculation="target employee IDs minus baseline employee IDs",
                rule="Exact employee-ID set difference; no hire inference.",
                refs=refs,
                facts=[fact],
                limitations=["employee_presence_does_not_prove_hire"],
            )
        )
    for employee_id in sorted(before_ids - after_ids):
        employee = _named(employee_id, before_names[employee_id])
        refs = _employee_refs(baseline, employee_id)
        fact = _fact("employee_set_membership", "present", "absent", refs)
        result.append(
            _finding(
                code="missing_employee",
                severity="review",
                baseline=baseline.period,
                target=target.period,
                employee=employee,
                baseline_value="present",
                target_value="absent",
                calculation="baseline employee IDs minus target employee IDs",
                rule="Exact employee-ID set difference; no termination inference.",
                refs=refs,
                facts=[fact],
                limitations=["employee_absence_does_not_prove_termination"],
            )
        )
    if len(before_ids) != len(after_ids):
        refs = _merge_source_refs(_snapshot_refs(baseline), _snapshot_refs(target))
        delta = Decimal(len(after_ids) - len(before_ids))
        fact = _fact("employee_count", str(len(before_ids)), str(len(after_ids)), refs)
        result.append(
            _finding(
                code="employee_count_change",
                severity="info",
                baseline=baseline.period,
                target=target.period,
                baseline_value=str(len(before_ids)),
                target_value=str(len(after_ids)),
                absolute_delta=delta,
                calculation="target headcount minus baseline headcount",
                rule="Any non-zero exact employee-count delta.",
                refs=refs,
                facts=[fact],
            )
        )
    by_employee: dict[int, list[PayrollContractFactChange]] = defaultdict(list)
    for change in contract_changes:
        by_employee[change.employee.id].append(change)
    for _employee_id, changes in sorted(by_employee.items()):
        refs = _merge_source_refs(*(change.source_refs for change in changes))
        result.append(
            _finding(
                code="contract_change",
                severity="review",
                baseline=baseline.period,
                target=target.period,
                employee=changes[0].employee,
                calculation="exact normalized contract facts compared field by field",
                rule="Any exact contract fact change.",
                refs=refs,
                facts=changes,
                limitations=["contract_change_does_not_establish_payroll_causality"],
            )
        )
    for line_change in line_changes:
        if line_change.baseline_total is None and line_change.target_total not in (
            None,
            Decimal("0"),
        ):
            finding_code = "rule_line_added"
            value = line_change.target_total
        elif line_change.target_total is None and line_change.baseline_total not in (
            None,
            Decimal("0"),
        ):
            finding_code = "rule_line_removed"
            value = line_change.baseline_total
        else:
            continue
        assert value is not None
        fact = _fact(
            "salary_rule_line_total",
            (None if line_change.baseline_total is None else str(line_change.baseline_total)),
            None if line_change.target_total is None else str(line_change.target_total),
            line_change.source_refs,
        )
        result.append(
            _finding(
                code=finding_code,
                severity="review",
                baseline=baseline.period,
                target=target.period,
                employee=line_change.employee,
                currency=line_change.currency,
                salary_rule=line_change.salary_rule,
                line_code=line_change.code,
                baseline_value=(
                    None if line_change.baseline_total is None else str(line_change.baseline_total)
                ),
                target_value=(
                    None if line_change.target_total is None else str(line_change.target_total)
                ),
                absolute_delta=line_change.absolute_delta,
                calculation="exact salary-rule ID and code presence comparison",
                rule="A non-zero exact rule line exists in only one comparable period.",
                refs=line_change.source_refs,
                facts=[fact],
            )
        )
    baseline_currencies: dict[int, set[int]] = defaultdict(set)
    target_currencies: dict[int, set[int]] = defaultdict(set)
    currency_names: dict[int, str] = {}
    for snapshot, destination in (
        (baseline, baseline_currencies),
        (target, target_currencies),
    ):
        for row in snapshot.payslips:
            destination[row.employee.id].add(row.currency.id)
            currency_names[row.currency.id] = row.currency.name
    for employee_id in sorted(before_ids & after_ids):
        if baseline_currencies[employee_id] == target_currencies[employee_id]:
            continue
        refs = _merge_source_refs(
            _employee_refs(baseline, employee_id),
            _employee_refs(target, employee_id),
        )
        before = ",".join(
            f"{value}:{currency_names[value]}" for value in sorted(baseline_currencies[employee_id])
        )
        after = ",".join(
            f"{value}:{currency_names[value]}" for value in sorted(target_currencies[employee_id])
        )
        fact = _fact("currency", before, after, refs)
        result.append(
            _finding(
                code="currency_change",
                severity="review",
                baseline=baseline.period,
                target=target.period,
                employee=_named(employee_id, after_names[employee_id]),
                baseline_value=before,
                target_value=after,
                calculation="exact Odoo currency-ID set comparison",
                rule="Currency changes are reported without conversion or amount comparison.",
                refs=refs,
                facts=[fact],
                limitations=["currency_change_prevents_amount_comparison"],
            )
        )
    return result


def _threshold_findings(
    baseline: _Snapshot,
    target: _Snapshot,
    line_changes: Sequence[PayrollLineChange],
    work_hours: Sequence[PayrollWorkEntryHoursComparison],
    *,
    profile: ThresholdProfile,
) -> list[PayrollFinding]:
    threshold = _THRESHOLDS[profile]
    result: list[PayrollFinding] = []
    for change in line_changes:
        raw_percentage = (
            None
            if change.baseline_total in (None, Decimal("0")) or change.target_total is None
            else (change.target_total - change.baseline_total)
            / abs(change.baseline_total)
            * Decimal("100")
        )
        if (
            raw_percentage is None
            or change.currency is None
            or change.baseline_total is None
            or change.target_total is None
            or abs(raw_percentage) < threshold
        ):
            continue
        percentage = _round(raw_percentage)
        fact = _fact(
            "salary_rule_line_total",
            str(change.baseline_total),
            str(change.target_total),
            change.source_refs,
        )
        result.append(
            _finding(
                code="monetary_deviation",
                severity="review",
                baseline=baseline.period,
                target=target.period,
                employee=change.employee,
                currency=change.currency,
                salary_rule=change.salary_rule,
                line_code=change.code,
                baseline_value=str(change.baseline_total),
                target_value=str(change.target_total),
                absolute_delta=change.absolute_delta,
                percentage_delta=percentage,
                observed_delta=abs(percentage),
                calculation="(target - baseline) / abs(baseline) * 100",
                rule="Same-currency absolute percentage change meets the selected threshold.",
                threshold=threshold,
                threshold_profile=profile,
                refs=change.source_refs,
                facts=[fact],
            )
        )
    for comparison in work_hours:
        raw_percentage = (
            None
            if comparison.hours.baseline == 0
            else (comparison.hours.target - comparison.hours.baseline)
            / abs(comparison.hours.baseline)
            * Decimal("100")
        )
        if raw_percentage is None or abs(raw_percentage) < threshold:
            continue
        percentage = _round(raw_percentage)
        fact = _fact(
            "work_entry_hours",
            str(comparison.hours.baseline),
            str(comparison.hours.target),
            comparison.source_refs,
        )
        result.append(
            _finding(
                code="hours_deviation",
                severity="review",
                baseline=baseline.period,
                target=target.period,
                employee=comparison.employee,
                line_code=comparison.code,
                baseline_value=str(comparison.hours.baseline),
                target_value=str(comparison.hours.target),
                absolute_delta=comparison.hours.absolute_delta,
                percentage_delta=percentage,
                observed_delta=abs(percentage),
                calculation="(target hours - baseline hours) / abs(baseline hours) * 100",
                rule="Absolute work-entry-hour percentage change meets the selected threshold.",
                threshold=threshold,
                threshold_profile=profile,
                refs=comparison.source_refs,
                facts=[fact],
                limitations=["work_entries_do_not_prove_physical_attendance"],
            )
        )
    return result


def _work_entry_conflict_findings(
    baseline: _Snapshot,
    target: _Snapshot,
) -> list[PayrollFinding]:
    grouped: dict[int, list[PayrollWorkEntry]] = defaultdict(list)
    names = _employee_names(target)
    for row in target.work_entries:
        if row.conflict or row.state == "conflict":
            grouped[row.employee.id].append(row)
    result: list[PayrollFinding] = []
    for employee_id, rows in sorted(grouped.items()):
        refs = evidence._source_refs({"hr.work.entry": (row.id for row in rows)})
        count = sum(int(row.conflict or row.state == "conflict") for row in rows)
        fact = _fact("work_entry_conflict_count", "0", str(count), refs)
        result.append(
            _finding(
                code="work_entry_conflict",
                severity="critical",
                baseline=baseline.period,
                target=target.period,
                employee=_named(employee_id, names[employee_id]),
                baseline_value="0",
                target_value=str(count),
                absolute_delta=Decimal(count),
                calculation="count of target work entries marked conflict",
                rule="Any target work-entry conflict is critical review evidence.",
                refs=refs,
                facts=[fact],
                limitations=["work_entries_do_not_prove_physical_attendance"],
            )
        )
    return result


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")


def _statistical_findings(
    baseline: _Snapshot,
    target: _Snapshot,
    histories: Sequence[_Snapshot],
) -> tuple[list[PayrollFinding], list[str]]:
    target_values = _line_aggregates(target)
    history_values = [_line_aggregates(snapshot) for snapshot in histories]
    results: list[PayrollFinding] = []
    limitations: set[str] = set()
    for key, target_value in sorted(target_values.items()):
        comparable: list[Decimal] = []
        refs: list[PayrollSourceReference] = list(_line_refs(target_value))
        for values in history_values:
            historical = values.get(key)
            if historical is None or historical.currency.id != target_value.currency.id:
                continue
            comparable.append(historical.total)
            refs = _merge_source_refs(refs, _line_refs(historical))
        if len(comparable) < 6:
            limitations.add("statistical_method_unavailable_insufficient_history")
            continue
        median = _median(comparable)
        mad = _median([abs(value - median) for value in comparable])
        if mad == 0:
            limitations.add("statistical_method_unavailable_zero_mad")
            continue
        score = _MODIFIED_Z_FACTOR * (target_value.total - median) / mad
        if abs(score) < _MODIFIED_Z_THRESHOLD:
            continue
        rendered_score = _round(score)
        fact = _fact(
            "historical_rule_total",
            str(median),
            str(target_value.total),
            refs,
        )
        results.append(
            _finding(
                code="statistical_deviation",
                severity="review",
                baseline=baseline.period,
                target=target.period,
                employee=target_value.employee,
                currency=target_value.currency,
                salary_rule=target_value.salary_rule,
                line_code=target_value.code,
                baseline_value=str(median),
                target_value=str(target_value.total),
                absolute_delta=target_value.total - median,
                observed_delta=abs(rendered_score),
                calculation="0.6745 * (target - history median) / history MAD",
                rule="At least six comparable history values and absolute modified z-score >= 3.5.",
                threshold=_MODIFIED_Z_THRESHOLD,
                refs=refs,
                facts=[fact],
                limitations=["statistical_baseline_uses_request_history_only"],
            )
        )
    if not histories:
        limitations.add("statistical_method_unavailable_insufficient_history")
    return results, sorted(limitations)


def _employee_sets(
    baseline: _Snapshot,
    target: _Snapshot,
) -> PayrollEmployeeSetChanges:
    before = _employee_names(baseline)
    after = _employee_names(target)
    return PayrollEmployeeSetChanges(
        new_employees=[_named(value, after[value]) for value in sorted(set(after) - set(before))],
        missing_employees=[
            _named(value, before[value]) for value in sorted(set(before) - set(after))
        ],
        common_employees=[
            _named(value, after[value]) for value in sorted(set(before) & set(after))
        ],
    )


def _limitations(
    baseline: _Snapshot,
    target: _Snapshot,
    *,
    states: Sequence[PayrollState],
) -> list[str]:
    values = {
        "employer_cost_not_authoritatively_available",
        "work_entries_do_not_prove_physical_attendance",
    }
    if "cancelled" in states:
        values.add("cancelled_payslips_excluded_from_comparison")
    for snapshot in (baseline, target):
        employee_ids = set(_employee_names(snapshot))
        segment_ids = {row.employee.id for row in snapshot.segments}
        if employee_ids - segment_ids:
            values.add("contract_evidence_unavailable_for_some_employees")
    return sorted(values)


def _markdown_text(value: object) -> str:
    return str(value).replace("`", "'").replace("\r", " ").replace("\n", " ")


def _render_anomalies(
    request: DetectPayrollAnomaliesInput,
    findings: Sequence[PayrollFinding],
    limitations: Sequence[str],
    source_refs: Sequence[PayrollSourceReference],
) -> str:
    rows = [
        "# Payroll Anomaly Review",
        "",
        (
            f"Baseline: `{request.baseline_period.period_start.isoformat()}` to "
            f"`{request.baseline_period.period_end.isoformat()}`"
        ),
        (
            f"Target: `{request.target_period.period_start.isoformat()}` to "
            f"`{request.target_period.period_end.isoformat()}`"
        ),
        f"Threshold profile: `{request.threshold_profile}`",
        "",
        "## Summary",
        "",
        f"Findings: {len(findings)}",
        "",
        "## Critical Findings",
        "",
    ]
    critical = [value for value in findings if value.severity == "critical"]
    rows.extend(
        [f"- `{value.finding_code}`" for value in critical]
        or ["- No critical finding was observed."]
    )
    rows.extend(["", "## Employee-Level Anomalies", ""])
    if not findings:
        rows.append("- No finding was observed under the selected rules.")
    for finding in findings:
        employee = (
            "company scope"
            if finding.employee is None
            else f"employee `{finding.employee.id}` ({_markdown_text(finding.employee.name)})"
        )
        rows.extend(
            [
                f"### `{finding.finding_code}` — {employee}",
                "",
                f"- Severity: `{finding.severity}`",
                f"- Calculation: {_markdown_text(finding.calculation)}",
                f"- Rule: {_markdown_text(finding.rule)}",
                (
                    "- Threshold: not applicable"
                    if finding.threshold is None
                    else f"- Threshold: `{finding.threshold}`"
                ),
                (
                    "- Observed delta: unavailable"
                    if finding.observed_delta is None
                    else f"- Observed delta: `{finding.observed_delta}`"
                ),
                "- Evidence:",
            ]
        )
        rows.extend(
            [
                f"  - `{_markdown_text(value.fact)}`: "
                f"`{_markdown_text(value.baseline_value)}` → "
                f"`{_markdown_text(value.target_value)}`"
                for value in finding.evidence
            ]
            or ["  - No additional evidence item."]
        )
        rows.append("- Correlations:")
        rows.extend(
            [
                f"  - `{_markdown_text(value.fact)}`: "
                f"`{_markdown_text(value.baseline_value)}` → "
                f"`{_markdown_text(value.target_value)}`"
                for value in finding.correlations
            ]
            or ["  - None reported."]
        )
        rows.append("- Limitations:")
        rows.extend(
            [f"  - `{_markdown_text(value)}`" for value in finding.limitations]
            or ["  - None specific to this finding."]
        )
        rows.append("")
    rows.extend(["## Evidence", ""])
    rows.extend(
        [
            f"- `{value.source_model}`: "
            + ", ".join(f"`{identifier}`" for identifier in value.source_ids)
            for value in source_refs
        ]
        or ["- No eligible source record."]
    )
    rows.extend(["", "## Calculations and Rules", ""])
    rows.extend(
        [
            f"- `{value.finding_code}`: {_markdown_text(value.calculation)}; "
            f"{_markdown_text(value.rule)}"
            for value in findings
        ]
        or ["- No calculation produced a finding."]
    )
    rows.extend(["", "## Limitations", ""])
    rows.extend([f"- `{_markdown_text(value)}`" for value in limitations] or ["- None."])
    rows.extend(
        [
            "",
            "## Recommended Human Review Actions",
            "",
            "- Review each source-linked finding in Odoo before taking any action.",
            "- Resolve work-entry conflicts and missing evidence in Odoo where applicable.",
            "- Treat correlations as non-causal until verified by an authorized reviewer.",
            "",
            "## Supporting Source References",
            "",
        ]
    )
    rows.extend(
        [
            f"- `{value.source_model}`: "
            + ", ".join(f"`{identifier}`" for identifier in value.source_ids)
            for value in source_refs
        ]
        or ["- No eligible source record."]
    )
    return "\n".join(rows)


async def _comparison_snapshots(
    adapter: OdooAdapter,
    request: ComparePayrollPeriodsInput,
) -> tuple[_Snapshot, _Snapshot]:
    baseline = await _load_snapshot(
        adapter,
        company_id=request.company_id,
        period=request.baseline_period,
        employee_ids=request.employee_ids,
        states=request.states,
    )
    target = await _load_snapshot(
        adapter,
        company_id=request.company_id,
        period=request.target_period,
        employee_ids=request.employee_ids,
        states=request.states,
    )
    _enforce_combined_caps((baseline, target))
    _validate_cross_snapshot_metadata((baseline, target))
    return baseline, target


async def compare_payroll_periods(
    adapter: OdooAdapter,
    request: ComparePayrollPeriodsInput,
    *,
    request_id: str,
    observed_at: datetime,
) -> ComparePayrollPeriodsResponse:
    baseline, target = await _comparison_snapshots(adapter, request)
    line_changes = _line_changes(baseline, target)
    contract_changes = _contract_changes(baseline, target)
    work_hours = _work_entry_hours(baseline, target)
    findings = _base_findings(baseline, target, line_changes, contract_changes)
    employee_sets = _employee_sets(baseline, target)
    changed_ids = (
        {value.employee.id for value in line_changes}
        | {value.employee.id for value in contract_changes}
        | {value.employee.id for value in work_hours if value.hours.change != "unchanged"}
        | {value.id for value in employee_sets.new_employees}
        | {value.id for value in employee_sets.missing_employees}
    )
    names = {**_employee_names(baseline), **_employee_names(target)}
    return ComparePayrollPeriodsResponse(
        request_id=request_id,
        company_id=request.company_id,
        observed_at=observed_at,
        source_refs=_merge_source_refs(_snapshot_refs(baseline), _snapshot_refs(target)),
        limitations=_limitations(baseline, target, states=request.states),
        baseline_period=request.baseline_period,
        target_period=request.target_period,
        headcount=_count_comparison(
            len(_employee_names(baseline)),
            len(_employee_names(target)),
        ),
        employee_changes=employee_sets,
        payslip_totals=_payslip_totals(baseline, target),
        rule_totals=_rule_totals(baseline, target),
        category_totals=_category_totals(baseline, target),
        recognized_rule_totals=_recognized_comparisons(baseline, target),
        employer_cost=PayrollUnavailableMetric(
            reason="No authoritative employer-cost aggregate is in the supported source contract."
        ),
        contract_changes=contract_changes,
        work_entry_hours=work_hours,
        changed_employees=[_named(value, names[value]) for value in sorted(changed_ids)],
        findings=findings,
    )


async def _exact_employee(
    adapter: OdooAdapter,
    *,
    company_id: int,
    employee_id: int,
) -> PayrollEmployee:
    employees = await evidence._all_employees(adapter, company_id, (employee_id,))
    if not employees:
        raise _error(
            ErrorCode.EMPLOYEE_NOT_FOUND,
            "The requested Payroll employee was not found.",
            "Use an exact employee ID from the authorized company.",
        )
    if (
        len(employees) != 1
        or employees[0].id != employee_id
        or employees[0].company_id != company_id
    ):
        raise _invalid_source()
    return employees[0]


def _change_fact(change: PayrollLineChange) -> PayrollEvidenceChange:
    return _fact(
        f"salary_rule:{change.salary_rule.id}:{change.code}",
        None if change.baseline_total is None else str(change.baseline_total),
        None if change.target_total is None else str(change.target_total),
        change.source_refs,
    )


async def analyze_employee_payroll_change(
    adapter: OdooAdapter,
    request: AnalyzeEmployeePayrollChangeInput,
    *,
    request_id: str,
    observed_at: datetime,
) -> AnalyzeEmployeePayrollChangeResponse:
    employee = await _exact_employee(
        adapter,
        company_id=request.company_id,
        employee_id=request.employee_id,
    )
    states: tuple[PayrollState, ...] = ("waiting", "done", "paid")
    baseline = await _load_snapshot(
        adapter,
        company_id=request.company_id,
        period=request.baseline_period,
        employee_ids=(request.employee_id,),
        states=states,
    )
    target = await _load_snapshot(
        adapter,
        company_id=request.company_id,
        period=request.target_period,
        employee_ids=(request.employee_id,),
        states=states,
    )
    if any(
        row.employee.id != employee.id or row.employee.name != employee.name
        for snapshot in (baseline, target)
        for row in snapshot.payslips
    ):
        raise _invalid_source()
    if not baseline.payslips and not target.payslips:
        raise _error(
            ErrorCode.INSUFFICIENT_COMPARISON_DATA,
            "The requested employee has no comparable Payroll evidence.",
            "Select exact periods containing an eligible payslip for the employee.",
        )
    for snapshot in (baseline, target):
        if snapshot.payslips and not snapshot.segments:
            raise _error(
                ErrorCode.CONTRACT_DATA_MISSING,
                "Required Payroll contract evidence is unavailable.",
                "Create or correct the applicable contract evidence in Odoo, then retry.",
            )
    _enforce_combined_caps((baseline, target))
    _validate_cross_snapshot_metadata((baseline, target))
    line_changes = _line_changes(baseline, target)
    contract_changes = _contract_changes(baseline, target)
    work_hours = [
        value
        for value in _work_entry_hours(baseline, target)
        if value.hours.change != "unchanged" or value.conflict_count
    ]
    findings = _base_findings(baseline, target, line_changes, contract_changes)
    demonstrated = [_change_fact(value) for value in line_changes]
    demonstrated.extend(
        PayrollEvidenceChange(
            fact=value.fact,
            baseline_value=value.baseline_value,
            target_value=value.target_value,
            source_refs=value.source_refs,
        )
        for value in contract_changes
        if value.fact == "wage"
    )
    demonstrated.extend(
        _fact(
            f"work_entry_hours:{value.work_entry_type.id}:{value.code}",
            str(value.hours.baseline),
            str(value.hours.target),
            value.source_refs,
        )
        for value in work_hours
    )
    correlations = [
        PayrollEvidenceChange(
            fact=value.fact,
            baseline_value=value.baseline_value,
            target_value=value.target_value,
            source_refs=value.source_refs,
        )
        for value in contract_changes
        if value.fact != "wage"
    ]
    limitations = _limitations(baseline, target, states=states)
    limitations.append("demonstrated_changes_do_not_establish_payroll_causality")
    refs = _merge_source_refs(
        _snapshot_refs(baseline),
        _snapshot_refs(target),
        evidence._source_refs({"hr.employee": (employee.id,)}),
    )
    return AnalyzeEmployeePayrollChangeResponse(
        request_id=request_id,
        company_id=request.company_id,
        observed_at=observed_at,
        source_refs=refs,
        limitations=sorted(set(limitations)),
        employee=_named(employee.id, employee.name),
        baseline_period=request.baseline_period,
        target_period=request.target_period,
        baseline_payslips=[evidence._compact_payslip(value) for value in baseline.payslips],
        target_payslips=[evidence._compact_payslip(value) for value in target.payslips],
        line_changes=line_changes,
        contract_changes=contract_changes,
        work_entry_hours=work_hours,
        demonstrated_contributors=demonstrated,
        correlations=correlations,
        unresolved_causes=[
            "Odoo's allowed evidence does not expose authoritative causal links "
            "between facts and calculated lines."
        ],
        findings=findings,
    )


async def detect_payroll_anomalies(
    adapter: OdooAdapter,
    request: DetectPayrollAnomaliesInput,
    *,
    request_id: str,
    observed_at: datetime,
) -> DetectPayrollAnomaliesResponse:
    baseline, target = await _comparison_snapshots(adapter, request)
    histories: list[_Snapshot] = []
    for period in sorted(
        request.history_periods,
        key=lambda value: (value.period_start, value.period_end),
    ):
        histories.append(
            await _load_snapshot(
                adapter,
                company_id=request.company_id,
                period=period,
                employee_ids=request.employee_ids,
                states=request.states,
                include_context=False,
            )
        )
        _enforce_combined_caps([*histories, baseline, target])
    all_snapshots = [*histories, baseline, target]
    _validate_cross_snapshot_metadata(all_snapshots)
    line_changes = _line_changes(baseline, target)
    contract_changes = _contract_changes(baseline, target)
    work_hours = _work_entry_hours(baseline, target)
    findings = _base_findings(baseline, target, line_changes, contract_changes)
    findings.extend(
        _threshold_findings(
            baseline,
            target,
            line_changes,
            work_hours,
            profile=request.threshold_profile,
        )
    )
    findings.extend(_work_entry_conflict_findings(baseline, target))
    statistical, statistical_limitations = _statistical_findings(
        baseline,
        target,
        histories,
    )
    findings.extend(statistical)
    findings.sort(
        key=lambda value: (
            value.finding_code,
            -1 if value.employee is None else value.employee.id,
            -1 if value.salary_rule is None else value.salary_rule.id,
            value.code or "",
        )
    )
    refs = _merge_source_refs(*(_snapshot_refs(value) for value in all_snapshots))
    limitations = set(_limitations(baseline, target, states=request.states))
    limitations.update(statistical_limitations)
    rendered_limitations = sorted(limitations)
    return DetectPayrollAnomaliesResponse(
        request_id=request_id,
        company_id=request.company_id,
        observed_at=observed_at,
        source_refs=refs,
        limitations=rendered_limitations,
        baseline_period=request.baseline_period,
        target_period=request.target_period,
        threshold_profile=request.threshold_profile,
        evaluated_employee_count=len(set(_employee_names(baseline)) | set(_employee_names(target))),
        statistical_period_count=len(histories),
        findings=findings,
        rendered_markdown=_render_anomalies(
            request,
            findings,
            rendered_limitations,
            refs,
        ),
    )


def _contract_item(segment: PayrollContractSegment) -> PayrollContractSegmentItem:
    return PayrollContractSegmentItem(
        source_model=segment.source_model,
        source_id=segment.id,
        effective_start=segment.effective_start,
        effective_end=segment.effective_end,
        revision_date=segment.revision_date,
        active=segment.active,
        source_status=segment.source_status,
        wage=segment.wage,
        currency=_currency(segment.currency.id, segment.currency.name),
        structure_type=(
            None
            if segment.structure_type is None
            else _named(segment.structure_type.id, segment.structure_type.name)
        ),
        working_schedule=(
            None
            if segment.resource_calendar is None
            else _named(segment.resource_calendar.id, segment.resource_calendar.name)
        ),
        department=(
            None
            if segment.department is None
            else _named(segment.department.id, segment.department.name)
        ),
        job=None if segment.job is None else _named(segment.job.id, segment.job.name),
        contract_type=(
            None
            if segment.contract_type is None
            else _named(segment.contract_type.id, segment.contract_type.name)
        ),
    )


def _recognized_explanation(
    response: GetPayslipResponse,
) -> list[PayrollRecognizedRuleValue]:
    salary_lines = response.salary_lines
    result: list[PayrollRecognizedRuleValue] = []
    for code in _RECOGNIZED_CODES:
        selected = [value for value in salary_lines if value.code == code]
        rule_ids = {value.salary_rule.id for value in selected}
        if not selected:
            recognized = PayrollRecognizedPeriodValue(
                status="unavailable",
                values=[],
                reason="exact_rule_code_missing",
            )
        elif len(rule_ids) != 1:
            recognized = PayrollRecognizedPeriodValue(
                status="unavailable",
                values=[],
                reason="exact_rule_code_duplicate",
            )
        else:
            grouped: dict[int, list[PayrollSalaryLineItem]] = defaultdict(list)
            for value in selected:
                grouped[value.currency.id].append(value)
            totals: list[PayrollRecognizedCurrencyTotal] = []
            for _currency_id, values in sorted(grouped.items()):
                sample = values[0]
                totals.append(
                    PayrollRecognizedCurrencyTotal(
                        currency=sample.currency,
                        total=sum((value.total for value in values), Decimal("0")),
                        source_refs=evidence._source_refs(
                            {"hr.payslip.line": (value.source_line_id for value in values)}
                        ),
                    )
                )
            recognized = PayrollRecognizedPeriodValue(status="available", values=totals)
        result.append(
            PayrollRecognizedRuleValue(
                code=code,  # type: ignore[arg-type]
                value=recognized,
            )
        )
    return result


async def explain_payslip(
    adapter: OdooAdapter,
    request: ExplainPayslipInput,
    *,
    request_id: str,
    observed_at: datetime,
) -> ExplainPayslipResponse:
    detail = await evidence.get_payslip(
        adapter,
        GetPayslipInput(company_id=request.company_id, payslip_id=request.payslip_id),
        request_id=request_id,
        observed_at=observed_at,
    )
    period = PayrollPeriodRange(
        period_start=detail.payslip.period_start,
        period_end=detail.payslip.period_end,
    )
    segments = await evidence._all_segments(
        adapter,
        request.company_id,
        PayrollContractFilters(
            employee_ids=(detail.payslip.employee.id,),
            period=_period(period),
        ),
    )
    _validate_segments(
        segments,
        company_id=request.company_id,
        employees={detail.payslip.employee.id: detail.payslip.employee.name},
        period=period,
    )
    if segments and not any(
        value.source_model == detail.payslip.contract_source
        and value.id == detail.payslip.contract.id
        for value in segments
    ):
        raise _invalid_source()
    grouped: dict[int, list[PayrollSalaryLineItem]] = defaultdict(list)
    for line in detail.salary_lines:
        grouped[line.category.id].append(line)
    groups: list[PayrollExplanationGroup] = []
    for category_id, rows in sorted(grouped.items()):
        sample = rows[0]
        if any(
            value.category.name != sample.category.name
            or value.currency.id != sample.currency.id
            or value.currency.name != sample.currency.name
            for value in rows
        ):
            raise _invalid_source()
        groups.append(
            PayrollExplanationGroup(
                category=_named(category_id, sample.category.name),
                currency=sample.currency,
                observed_total=sum((value.total for value in rows), Decimal("0")),
                lines=rows,
            )
        )
    recognized = _recognized_explanation(detail)
    missing = [
        f"recognized_{value.code.lower()}_unavailable"
        for value in recognized
        if value.value.status == "unavailable"
    ]
    if not segments:
        missing.append("contract_context_unavailable")
    if not detail.worked_days:
        missing.append("worked_day_evidence_unavailable")
    segment_models: dict[str, list[int]] = defaultdict(list)
    for segment in segments:
        segment_models[segment.source_model].append(segment.id)
    limitations = {
        "employer_cost_not_authoritatively_available",
        "payroll_rule_logic_not_evaluated_locally",
        "work_entries_do_not_prove_physical_attendance",
        *missing,
    }
    if any(value.source_status == "unavailable" for value in segments):
        limitations.add("contract_source_status_unavailable")
    return ExplainPayslipResponse(
        request_id=request_id,
        company_id=request.company_id,
        observed_at=observed_at,
        source_refs=_merge_source_refs(
            detail.source_refs,
            evidence._source_refs(segment_models),
        ),
        limitations=sorted(limitations),
        payslip=detail.payslip,
        line_groups=groups,
        line_arithmetic=[
            PayrollLineArithmetic(
                source_line_id=value.source_line_id,
                quantity=value.quantity,
                rate=value.rate,
                amount=value.amount,
                odoo_returned_total=value.total,
                description=(
                    "Odoo returned these quantity, rate, amount, and total fields; "
                    "the MCP did not evaluate the salary rule."
                ),
            )
            for value in detail.salary_lines
        ],
        worked_days=detail.worked_days,
        inputs=detail.inputs,
        contract_segments=[_contract_item(value) for value in segments],
        recognized_rule_totals=recognized,
        employer_cost=PayrollUnavailableMetric(
            reason="No authoritative employer-cost aggregate is in the supported source contract."
        ),
        missing_evidence=sorted(missing),
    )
