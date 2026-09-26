from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from pydantic import ValidationError

from odoo_mcp.adapters.accounting import RelatedRecord
from odoo_mcp.adapters.base import CapabilitySnapshot, Company, OdooAdapter
from odoo_mcp.adapters.odoo.connections import ConnectionBinding
from odoo_mcp.adapters.payroll import (
    PayrollBatch,
    PayrollBatchFilters,
    PayrollContractFilters,
    PayrollContractSegment,
    PayrollEmployee,
    PayrollInputType,
    PayrollInputTypeFilters,
    PayrollPage,
    PayrollPageRequest,
    PayrollWorkEntry,
    PayrollWorkEntryFilters,
    Payslip,
    PayslipChildFilters,
    PayslipFilters,
    PayslipInput,
    PayslipLine,
    PayslipWorkedDay,
)
from odoo_mcp.app.settings import DeploymentProfile, OdooConnectionSettings
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.payroll_schemas import (
    AnalyzeEmployeePayrollChangeInput,
    ComparePayrollPeriodsInput,
    DetectPayrollAnomaliesInput,
    ExplainPayslipInput,
    PayrollPeriodRange,
    PreparePayrollApprovalPackInput,
    ThresholdProfile,
)
from odoo_mcp.mcp.server import create_mcp_server
from odoo_mcp.storage import Storage
from odoo_mcp.workflows.payroll import analysis as analysis_module
from odoo_mcp.workflows.payroll.analysis import (
    analyze_employee_payroll_change,
    compare_payroll_periods,
    detect_payroll_anomalies,
    explain_payslip,
    prepare_payroll_approval_pack,
)

OBSERVED_AT = datetime(2026, 9, 26, 12, tzinfo=UTC)
WRITE_DATE = datetime(2026, 9, 26, 10, tzinfo=UTC)
USD = RelatedRecord(id=10, name="USD")
EUR = RelatedRecord(id=20, name="EUR")
STRUCTURE = RelatedRecord(id=60, name="Monthly")
BASIC_RULE = RelatedRecord(id=701, name="Basic Salary")
BASIC_CATEGORY = RelatedRecord(id=401, name="Basic")
ALLOWANCE_CATEGORY = RelatedRecord(id=402, name="Allowance")
WORK_TYPE = RelatedRecord(id=801, name="Regular Work")
BASELINE = PayrollPeriodRange(
    period_start=date(2026, 1, 1),
    period_end=date(2026, 1, 31),
)
TARGET = PayrollPeriodRange(
    period_start=date(2026, 2, 1),
    period_end=date(2026, 2, 28),
)
HISTORY = tuple(
    PayrollPeriodRange(
        period_start=date(2025, month, 1),
        period_end=date(2025, month, 30 if month in (9, 11) else 31),
    )
    for month in range(7, 13)
)


def _employee(identifier: int) -> RelatedRecord:
    return RelatedRecord(id=identifier, name=f"Synthetic Employee {identifier}")


def _contract(identifier: int) -> RelatedRecord:
    return RelatedRecord(id=identifier, name=f"Contract {identifier}")


def _payslip(
    identifier: int,
    employee_id: int,
    period: PayrollPeriodRange,
    *,
    contract_id: int,
    currency: RelatedRecord = USD,
    company_id: int = 1,
) -> Payslip:
    return Payslip(
        id=identifier,
        name=f"Payslip {identifier}",
        reference=f"PS-{identifier}",
        employee=_employee(employee_id),
        date_from=period.period_start,
        date_to=period.period_end,
        state="done",
        source_state="validated",
        company_id=company_id,
        contract_segment=_contract(contract_id),
        contract_source="hr.version",
        structure=STRUCTURE,
        batch=None,
        credit_note=False,
        currency=currency,
        write_date=WRITE_DATE,
    )


def _line(
    identifier: int,
    payslip: Payslip,
    total: str,
    *,
    rule: RelatedRecord = BASIC_RULE,
    code: str = "BASIC",
    category: RelatedRecord = BASIC_CATEGORY,
) -> PayslipLine:
    return PayslipLine(
        id=identifier,
        payslip=RelatedRecord(id=payslip.id, name=payslip.name),
        salary_rule=rule,
        employee=payslip.employee,
        contract_segment=payslip.contract_segment,
        contract_source=payslip.contract_source,
        name=f"Line {code}",
        code=code,
        category=category,
        sequence=10,
        quantity=Decimal("1"),
        rate=Decimal("100"),
        amount=Decimal(total),
        total=Decimal(total),
        currency=payslip.currency,
        write_date=WRITE_DATE,
    )


def _segment(
    identifier: int,
    employee_id: int,
    period: PayrollPeriodRange,
    *,
    wage: str,
    currency: RelatedRecord = USD,
    department_id: int = 901,
    revision_date: date | None = None,
) -> PayrollContractSegment:
    return PayrollContractSegment(
        source_model="hr.version",
        id=identifier,
        employee=_employee(employee_id),
        company_id=1,
        active=True,
        source_status="unavailable",
        revision_date=revision_date or period.period_start,
        effective_start=period.period_start,
        effective_end=period.period_end,
        wage=Decimal(wage),
        currency=currency,
        structure_type=RelatedRecord(id=910, name="Employee"),
        resource_calendar=RelatedRecord(id=911, name="40 Hours"),
        department=RelatedRecord(id=department_id, name=f"Department {department_id}"),
        job=RelatedRecord(id=912, name="Synthetic Role"),
        contract_type=RelatedRecord(id=913, name="Permanent"),
        write_date=WRITE_DATE,
    )


def _work_entry(
    identifier: int,
    employee_id: int,
    period: PayrollPeriodRange,
    *,
    contract_id: int,
    hours: str,
    conflict: bool = False,
) -> PayrollWorkEntry:
    return PayrollWorkEntry(
        id=identifier,
        employee=_employee(employee_id),
        company_id=1,
        contract_segment=_contract(contract_id),
        contract_source="hr.version",
        date_start=period.period_start,
        date_end=period.period_start,
        duration=Decimal(hours),
        work_entry_type=WORK_TYPE,
        code="WORK100",
        state="conflict" if conflict else "validated",
        conflict=conflict,
        write_date=WRITE_DATE,
    )


class AnalysisAdapter:
    def __init__(
        self,
        *,
        changed: bool = True,
        target_basic: str = "120",
    ) -> None:
        self.closed = False
        self.incomplete_lines = False
        self.payslips: list[Payslip] = []
        self.lines: list[PayslipLine] = []
        self.segments: list[PayrollContractSegment] = []
        self.work_entries: list[PayrollWorkEntry] = []
        self.employees = [
            PayrollEmployee(
                id=value,
                name=_employee(value).name,
                active=True,
                company_id=1,
                write_date=WRITE_DATE,
            )
            for value in range(101, 106)
        ]
        baseline_one = _payslip(301, 101, BASELINE, contract_id=201)
        target_one = _payslip(
            401,
            101,
            TARGET,
            contract_id=211 if changed else 201,
        )
        self.payslips.extend((baseline_one, target_one))
        self.lines.extend(
            (
                _line(501, baseline_one, "100"),
                _line(601, target_one, target_basic),
            )
        )
        self.segments.extend(
            (
                _segment(201, 101, BASELINE, wage="100"),
                _segment(
                    211 if changed else 201,
                    101,
                    TARGET,
                    wage="110" if changed else "100",
                    department_id=902 if changed else 901,
                    revision_date=None if changed else BASELINE.period_start,
                ),
            )
        )
        self.work_entries.extend(
            (
                _work_entry(801, 101, BASELINE, contract_id=201, hours="100"),
                _work_entry(
                    802,
                    101,
                    TARGET,
                    contract_id=211 if changed else 201,
                    hours="80" if changed else "100",
                    conflict=changed,
                ),
            )
        )
        if changed:
            removed = RelatedRecord(id=702, name="Removed Rule")
            added = RelatedRecord(id=703, name="Added Rule")
            self.lines.extend(
                (
                    _line(
                        502,
                        baseline_one,
                        "5",
                        rule=removed,
                        code="OLD",
                        category=ALLOWANCE_CATEGORY,
                    ),
                    _line(
                        602,
                        target_one,
                        "10",
                        rule=added,
                        code="BONUS",
                        category=ALLOWANCE_CATEGORY,
                    ),
                )
            )
            baseline_missing = _payslip(302, 102, BASELINE, contract_id=202)
            target_new = _payslip(402, 103, TARGET, contract_id=213)
            baseline_currency = _payslip(303, 104, BASELINE, contract_id=204)
            target_currency = _payslip(
                403,
                104,
                TARGET,
                contract_id=214,
                currency=EUR,
            )
            second_new = _payslip(404, 105, TARGET, contract_id=215)
            self.payslips.extend(
                (
                    baseline_missing,
                    target_new,
                    baseline_currency,
                    target_currency,
                    second_new,
                )
            )
            self.lines.extend(
                (
                    _line(503, baseline_missing, "50"),
                    _line(603, target_new, "60"),
                    _line(504, baseline_currency, "70"),
                    _line(604, target_currency, "80"),
                    _line(605, second_new, "40"),
                )
            )
            self.segments.extend(
                (
                    _segment(202, 102, BASELINE, wage="50"),
                    _segment(213, 103, TARGET, wage="60"),
                    _segment(204, 104, BASELINE, wage="70"),
                    _segment(214, 104, TARGET, wage="80", currency=EUR),
                    _segment(215, 105, TARGET, wage="40"),
                )
            )
        history_values = ("98", "99", "100", "101", "102", "103")
        for offset, (period, amount) in enumerate(zip(HISTORY, history_values, strict=True)):
            payslip = _payslip(100 + offset, 101, period, contract_id=201)
            self.payslips.append(payslip)
            self.lines.append(_line(200 + offset, payslip, amount))

    @staticmethod
    def _page(items: list[Any], page: PayrollPageRequest) -> PayrollPage[Any]:
        offset = int(page.cursor or "0")
        selected = items[offset : offset + page.limit]
        next_offset = offset + len(selected)
        return PayrollPage[Any](
            items=selected,
            total_count=len(items),
            next_cursor=str(next_offset) if next_offset < len(items) else None,
        )

    async def get_companies(self) -> list[Company]:
        return [Company(id=1, name="Synthetic Company", currency=USD)]

    async def get_capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            edition="enterprise",
            version=19,
            transport="json2",
            modules={"base": True, "hr_payroll": True},
        )

    async def get_payroll_batches(
        self,
        company_id: int,
        filters: PayrollBatchFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayrollBatch]:
        del company_id, filters
        return self._page([], page)

    async def get_payslips(
        self,
        company_id: int,
        filters: PayslipFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[Payslip]:
        rows = [value for value in self.payslips if value.company_id == company_id]
        if filters.payslip_ids:
            rows = [value for value in rows if value.id in filters.payslip_ids]
        if filters.period is not None:
            rows = [
                value
                for value in rows
                if value.date_from == filters.period.start and value.date_to == filters.period.end
            ]
        if filters.employee_ids:
            rows = [value for value in rows if value.employee.id in filters.employee_ids]
        if filters.states:
            rows = [value for value in rows if value.state in filters.states]
        rows.sort(key=lambda value: (value.date_from, value.date_to, value.id), reverse=True)
        return self._page(rows, page)

    async def get_payslip_lines(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayslipLine]:
        del company_id
        rows = [value for value in self.lines if value.payslip.id in filters.payslip_ids]
        if filters.record_ids:
            rows = [value for value in rows if value.id in filters.record_ids]
        rows.sort(key=lambda value: (value.sequence, value.id))
        if self.incomplete_lines and rows:
            return PayrollPage[PayslipLine](items=[], total_count=len(rows), next_cursor=None)
        return self._page(rows, page)

    async def get_payslip_worked_days(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayslipWorkedDay]:
        del company_id, filters
        return self._page([], page)

    async def get_payslip_inputs(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayslipInput]:
        del company_id, filters
        return self._page([], page)

    async def get_payroll_input_types(
        self,
        company_id: int,
        filters: PayrollInputTypeFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayrollInputType]:
        del company_id, filters
        return self._page([], page)

    async def get_payroll_employees(
        self,
        company_id: int,
        employee_ids: tuple[int, ...],
        page: PayrollPageRequest,
    ) -> PayrollPage[PayrollEmployee]:
        rows = [
            value
            for value in self.employees
            if value.company_id == company_id and value.id in employee_ids
        ]
        return self._page(rows, page)

    async def get_payroll_contract_segments(
        self,
        company_id: int,
        filters: PayrollContractFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayrollContractSegment]:
        rows = [
            value
            for value in self.segments
            if value.company_id == company_id
            and value.employee.id in filters.employee_ids
            and value.effective_start >= filters.period.start
            and value.effective_end <= filters.period.end
        ]
        return self._page(rows, page)

    async def get_payroll_work_entries(
        self,
        company_id: int,
        filters: PayrollWorkEntryFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayrollWorkEntry]:
        rows = [
            value
            for value in self.work_entries
            if value.company_id == company_id
            and value.employee.id in filters.employee_ids
            and value.date_start >= filters.period.start
            and value.date_end <= filters.period.end
            and (not filters.states or value.state in filters.states)
        ]
        return self._page(rows, page)

    async def close(self) -> None:
        self.closed = True


def _compare_request() -> ComparePayrollPeriodsInput:
    return ComparePayrollPeriodsInput(
        company_id=1,
        baseline_period=BASELINE,
        target_period=TARGET,
    )


def _detect_request(
    *,
    histories: tuple[PayrollPeriodRange, ...] = HISTORY,
    profile: ThresholdProfile = "standard",
) -> DetectPayrollAnomaliesInput:
    return DetectPayrollAnomaliesInput(
        company_id=1,
        baseline_period=BASELINE,
        target_period=TARGET,
        history_periods=histories,
        threshold_profile=profile,
    )


def _pack_request(
    *,
    baseline: PayrollPeriodRange | None = BASELINE,
    employee_ids: tuple[int, ...] = (),
) -> PreparePayrollApprovalPackInput:
    return PreparePayrollApprovalPackInput(
        company_id=1,
        baseline_period=baseline,
        target_period=TARGET,
        employee_ids=employee_ids,
    )


def test_analysis_inputs_reject_overlap_history_order_and_duplicates() -> None:
    with pytest.raises(ValidationError):
        ComparePayrollPeriodsInput(
            company_id=1,
            baseline_period=BASELINE,
            target_period=PayrollPeriodRange(
                period_start=date(2026, 1, 31),
                period_end=date(2026, 2, 28),
            ),
        )
    with pytest.raises(ValidationError):
        DetectPayrollAnomaliesInput(
            company_id=1,
            baseline_period=BASELINE,
            target_period=TARGET,
            history_periods=(BASELINE,),
        )
    with pytest.raises(ValidationError):
        DetectPayrollAnomaliesInput(
            company_id=1,
            baseline_period=BASELINE,
            target_period=TARGET,
            history_periods=(HISTORY[0], HISTORY[0]),
        )
    with pytest.raises(ValidationError):
        PreparePayrollApprovalPackInput(
            company_id=1,
            baseline_period=TARGET,
            target_period=TARGET,
        )
    with pytest.raises(ValidationError):
        PreparePayrollApprovalPackInput(
            company_id=1,
            target_period=TARGET,
            employee_ids=tuple(range(1, 102)),
        )


async def test_compare_no_change_is_source_linked_and_has_no_findings() -> None:
    response = await compare_payroll_periods(
        AnalysisAdapter(changed=False, target_basic="100"),  # type: ignore[arg-type]
        _compare_request(),
        request_id="req-no-change",
        observed_at=OBSERVED_AT,
    )

    assert response.headcount.absolute_delta == 0
    assert response.changed_employees == []
    assert response.findings == []
    assert response.rule_totals[0].total.change == "unchanged"
    assert response.recognized_rule_totals[0].baseline.status == "available"
    assert response.recognized_rule_totals[1].baseline.reason == "exact_rule_code_missing"
    assert response.employer_cost.status == "unavailable"
    assert {value.source_model for value in response.source_refs} >= {
        "hr.payslip",
        "hr.payslip.line",
        "hr.version",
        "hr.work.entry",
    }


async def test_approval_pack_uses_one_evidence_set_and_renders_json_parity() -> None:
    response = await prepare_payroll_approval_pack(
        AnalysisAdapter(target_basic="200"),  # type: ignore[arg-type]
        _pack_request(),
        request_id="req-pack",
        observed_at=OBSERVED_AT,
    )

    assert response.company.id == 1
    assert response.headcount == 4
    assert response.variance.status == "available"
    assert response.variance.employee_changes is not None
    assert {value.id for value in response.variance.employee_changes.new_employees} == {
        103,
        105,
    }
    assert {value.id for value in response.variance.employee_changes.missing_employees} == {102}
    assert response.variance.changed_employees
    assert response.rule_totals
    assert response.category_totals
    assert {value.code for value in response.recognized_rule_totals} == {"GROSS", "NET"}
    assert all(value.value.status == "unavailable" for value in response.recognized_rule_totals)
    assert response.employer_cost.status == "unavailable"
    assert response.anomalies
    assert response.exceptions
    assert all(value.severity == "critical" for value in response.exceptions)
    assert response.unresolved_issues
    assert response.recommended_review_actions
    assert len(response.sign_off_checklist) == 4
    required_sections = (
        "## Period",
        "## Executive Summary",
        "## Headcount Movement",
        "## Odoo Rule/Category Totals",
        "## Recognized Gross/Net Availability",
        "## Variance Analysis",
        "## Anomalies",
        "## Exceptions",
        "## Evidence",
        "## Limitations",
        "## Sign-Off Checklist",
    )
    assert all(value in response.rendered_markdown for value in required_sections)
    for total in response.rule_totals:
        assert f"`{total.salary_rule.id}`" in response.rendered_markdown
        assert f"`{total.currency.id}`" in response.rendered_markdown
        assert f"`{total.total}`" in response.rendered_markdown
    for finding in response.anomalies:
        assert finding.finding_code in response.rendered_markdown
        assert finding.calculation in response.rendered_markdown
        assert finding.rule in response.rendered_markdown
        if finding.threshold is not None:
            assert f"Threshold: `{finding.threshold}`" in response.rendered_markdown
        if finding.threshold_profile is not None:
            assert f"Threshold profile: `{finding.threshold_profile}`" in response.rendered_markdown
        if finding.observed_delta is not None:
            assert f"Observed delta: `{finding.observed_delta}`" in response.rendered_markdown
        for item in finding.evidence:
            assert item.fact in response.rendered_markdown
            for reference in item.source_refs:
                assert reference.source_model in response.rendered_markdown
                for identifier in reference.source_ids:
                    assert f"`{identifier}`" in response.rendered_markdown
        for item in finding.correlations:
            assert item.fact in response.rendered_markdown
        for limitation in finding.limitations:
            assert limitation in response.rendered_markdown
    assert "- Correlations:\n  - None." in response.rendered_markdown
    for reference in response.source_refs:
        assert reference.source_model in response.rendered_markdown
        for identifier in reference.source_ids:
            assert f"`{identifier}`" in response.rendered_markdown
    for limitation in response.limitations:
        assert limitation in response.rendered_markdown


async def test_approval_pack_without_baseline_marks_comparison_not_requested() -> None:
    response = await prepare_payroll_approval_pack(
        AnalysisAdapter(changed=False, target_basic="100"),  # type: ignore[arg-type]
        _pack_request(baseline=None),
        request_id="req-pack-no-baseline",
        observed_at=OBSERVED_AT,
    )

    assert response.variance.status == "not_requested"
    assert response.variance.baseline_period is None
    assert response.anomalies == []
    assert response.exceptions == []
    assert "Status: `not_requested`" in response.rendered_markdown
    assert "unchanged" not in response.rendered_markdown


async def test_approval_pack_without_baseline_retains_target_conflict() -> None:
    response = await prepare_payroll_approval_pack(
        AnalysisAdapter(),  # type: ignore[arg-type]
        _pack_request(baseline=None, employee_ids=(101,)),
        request_id="req-pack-target-conflict",
        observed_at=OBSERVED_AT,
    )

    assert response.variance.status == "not_requested"
    assert len(response.anomalies) == 1
    finding = response.anomalies[0]
    assert finding.finding_code == "work_entry_conflict"
    assert finding.severity == "critical"
    assert finding.baseline_period is None
    assert finding.baseline_value is None
    assert finding.absolute_delta is None
    assert finding.observed_delta == Decimal("1")
    assert response.exceptions == [finding]
    assert "Baseline period: not requested" in response.rendered_markdown
    assert "work_entry_conflict_count" in response.rendered_markdown
    assert "work_entries_do_not_prove_physical_attendance" in response.rendered_markdown
    assert "see the complete source-linked finding in **Anomalies**" in (response.rendered_markdown)


async def test_approval_pack_empty_target_is_explicit_success() -> None:
    response = await prepare_payroll_approval_pack(
        AnalysisAdapter(),  # type: ignore[arg-type]
        _pack_request(baseline=None, employee_ids=(999,)),
        request_id="req-pack-empty",
        observed_at=OBSERVED_AT,
    )

    assert response.headcount == 0
    assert response.rule_totals == []
    assert response.category_totals == []
    assert response.variance.status == "not_requested"
    assert all(value.value.status == "unavailable" for value in response.recognized_rule_totals)
    assert "No eligible rule total" in response.rendered_markdown


async def test_repeat_approval_pack_reads_current_source_without_retaining_pack() -> None:
    adapter = AnalysisAdapter(changed=False, target_basic="100")
    first = await prepare_payroll_approval_pack(
        adapter,  # type: ignore[arg-type]
        _pack_request(),
        request_id="req-pack-first",
        observed_at=OBSERVED_AT,
    )
    target_line = next(value for value in adapter.lines if value.id == 601)
    adapter.lines[adapter.lines.index(target_line)] = target_line.model_copy(
        update={"amount": Decimal("250"), "total": Decimal("250")}
    )
    second = await prepare_payroll_approval_pack(
        adapter,  # type: ignore[arg-type]
        _pack_request(),
        request_id="req-pack-second",
        observed_at=OBSERVED_AT,
    )

    first_basic = next(value for value in first.rule_totals if value.code == "BASIC")
    second_basic = next(value for value in second.rule_totals if value.code == "BASIC")
    assert first_basic.total == Decimal("100")
    assert second_basic.total == Decimal("250")


async def test_detects_every_contractual_finding_and_keeps_currency_uncompared() -> None:
    response = await detect_payroll_anomalies(
        AnalysisAdapter(target_basic="200"),  # type: ignore[arg-type]
        _detect_request(),
        request_id="req-findings",
        observed_at=OBSERVED_AT,
    )

    codes = {value.finding_code for value in response.findings}
    assert codes == {
        "new_employee",
        "missing_employee",
        "employee_count_change",
        "contract_change",
        "rule_line_added",
        "rule_line_removed",
        "currency_change",
        "monetary_deviation",
        "hours_deviation",
        "work_entry_conflict",
        "statistical_deviation",
    }
    assert all(value.rule and value.calculation for value in response.findings)
    assert all(value.source_refs and value.evidence for value in response.findings)
    currency = next(value for value in response.findings if value.finding_code == "currency_change")
    assert currency.employee is not None and currency.employee.id == 104
    assert currency.percentage_delta is None
    assert not any(
        value.finding_code == "monetary_deviation"
        and value.employee is not None
        and value.employee.id == 104
        for value in response.findings
    )
    conflict = next(
        value for value in response.findings if value.finding_code == "work_entry_conflict"
    )
    assert conflict.severity == "critical"
    statistical = next(
        value for value in response.findings if value.finding_code == "statistical_deviation"
    )
    assert statistical.calculation == ("0.6745 * (target - history median) / history MAD")
    assert statistical.observed_delta is not None
    assert statistical.observed_delta >= Decimal("3.5")
    assert "## Critical Findings" in response.rendered_markdown
    assert "## Recommended Human Review Actions" in response.rendered_markdown
    for finding in response.findings:
        assert finding.finding_code in response.rendered_markdown
        assert finding.calculation in response.rendered_markdown
    for reference in response.source_refs:
        assert reference.source_model in response.rendered_markdown
        for identifier in reference.source_ids:
            assert f"`{identifier}`" in response.rendered_markdown
    for limitation in response.limitations:
        assert limitation in response.rendered_markdown


@pytest.mark.parametrize(
    ("profile", "target", "expected"),
    [
        ("strict", "104.99999", False),
        ("strict", "105", True),
        ("strict", "105.00001", True),
        ("standard", "109.99999", False),
        ("standard", "110", True),
        ("standard", "110.00001", True),
        ("relaxed", "119.99999", False),
        ("relaxed", "120", True),
        ("relaxed", "120.00001", True),
    ],
)
async def test_monetary_threshold_uses_unrounded_decimal_boundary(
    profile: ThresholdProfile,
    target: str,
    expected: bool,
) -> None:
    response = await detect_payroll_anomalies(
        AnalysisAdapter(changed=False, target_basic=target),  # type: ignore[arg-type]
        _detect_request(histories=(), profile=profile),
        request_id="req-threshold",
        observed_at=OBSERVED_AT,
    )

    assert (
        any(value.finding_code == "monetary_deviation" for value in response.findings) is expected
    )


@pytest.mark.parametrize(
    ("profile", "target", "expected"),
    [
        ("strict", "104.99999", False),
        ("strict", "105", True),
        ("strict", "105.00001", True),
        ("standard", "109.99999", False),
        ("standard", "110", True),
        ("standard", "110.00001", True),
        ("relaxed", "119.99999", False),
        ("relaxed", "120", True),
        ("relaxed", "120.00001", True),
    ],
)
async def test_hours_threshold_uses_unrounded_decimal_boundary(
    profile: ThresholdProfile,
    target: str,
    expected: bool,
) -> None:
    adapter = AnalysisAdapter(changed=False, target_basic="100")
    target_entry = next(value for value in adapter.work_entries if value.id == 802)
    adapter.work_entries[adapter.work_entries.index(target_entry)] = target_entry.model_copy(
        update={"duration": Decimal(target)}
    )
    response = await detect_payroll_anomalies(
        adapter,  # type: ignore[arg-type]
        _detect_request(histories=(), profile=profile),
        request_id="req-hours-threshold",
        observed_at=OBSERVED_AT,
    )
    assert any(value.finding_code == "hours_deviation" for value in response.findings) is expected


async def test_insufficient_history_and_zero_mad_disable_only_statistics() -> None:
    insufficient = await detect_payroll_anomalies(
        AnalysisAdapter(changed=False),  # type: ignore[arg-type]
        _detect_request(histories=HISTORY[:5]),
        request_id="req-short-history",
        observed_at=OBSERVED_AT,
    )
    assert "statistical_method_unavailable_insufficient_history" in insufficient.limitations

    adapter = AnalysisAdapter(changed=False, target_basic="200")
    for line in adapter.lines:
        if line.payslip.id < 200:
            object.__setattr__(line, "total", Decimal("100"))
            object.__setattr__(line, "amount", Decimal("100"))
    zero_mad = await detect_payroll_anomalies(
        adapter,  # type: ignore[arg-type]
        _detect_request(),
        request_id="req-zero-mad",
        observed_at=OBSERVED_AT,
    )
    assert "statistical_method_unavailable_zero_mad" in zero_mad.limitations
    assert not any(value.finding_code == "statistical_deviation" for value in zero_mad.findings)


async def test_duplicate_recognized_code_is_unavailable_not_guessed() -> None:
    adapter = AnalysisAdapter(changed=False)
    target = next(value for value in adapter.payslips if value.id == 401)
    adapter.lines.append(
        _line(
            999,
            target,
            "1",
            rule=RelatedRecord(id=799, name="Second Basic"),
            code="BASIC",
        )
    )
    response = await compare_payroll_periods(
        adapter,  # type: ignore[arg-type]
        _compare_request(),
        request_id="req-duplicate",
        observed_at=OBSERVED_AT,
    )

    basic = next(value for value in response.recognized_rule_totals if value.code == "BASIC")
    assert basic.target.status == "unavailable"
    assert basic.target.reason == "exact_rule_code_duplicate"


async def test_zero_baseline_is_added_without_percentage_or_false_line_addition() -> None:
    adapter = AnalysisAdapter(changed=False, target_basic="100")
    baseline_line = next(value for value in adapter.lines if value.id == 501)
    adapter.lines[adapter.lines.index(baseline_line)] = baseline_line.model_copy(
        update={"amount": Decimal("0"), "total": Decimal("0")}
    )
    comparison = await compare_payroll_periods(
        adapter,  # type: ignore[arg-type]
        _compare_request(),
        request_id="req-zero-baseline",
        observed_at=OBSERVED_AT,
    )
    basic = next(value for value in comparison.rule_totals if value.code == "BASIC")
    assert basic.total.change == "added"
    assert basic.total.percentage_delta is None

    anomalies = await detect_payroll_anomalies(
        adapter,  # type: ignore[arg-type]
        _detect_request(histories=()),
        request_id="req-zero-baseline-anomalies",
        observed_at=OBSERVED_AT,
    )
    assert not any(value.finding_code == "rule_line_added" for value in anomalies.findings)


async def test_employee_analysis_separates_evidence_correlations_and_unknown_cause() -> None:
    response = await analyze_employee_payroll_change(
        AnalysisAdapter(),  # type: ignore[arg-type]
        AnalyzeEmployeePayrollChangeInput(
            company_id=1,
            employee_id=101,
            baseline_period=BASELINE,
            target_period=TARGET,
        ),
        request_id="req-employee",
        observed_at=OBSERVED_AT,
    )

    assert {value.fact for value in response.demonstrated_contributors} >= {
        "wage",
        "work_entry_hours:801:WORK100",
    }
    assert "department" in {value.fact for value in response.correlations}
    assert response.unresolved_causes
    assert "demonstrated_changes_do_not_establish_payroll_causality" in response.limitations


async def test_explanation_groups_odoo_lines_without_recalculating_rules() -> None:
    response = await explain_payslip(
        AnalysisAdapter(),  # type: ignore[arg-type]
        ExplainPayslipInput(company_id=1, payslip_id=401),
        request_id="req-explain",
        observed_at=OBSERVED_AT,
    )

    assert response.payslip.payslip_id == 401
    assert {value.category.id for value in response.line_groups} == {401, 402}
    assert len(response.line_arithmetic) == len(response.line_groups)
    assert all("did not evaluate" in value.description for value in response.line_arithmetic)
    assert "recognized_gross_unavailable" in response.missing_evidence
    assert "payroll_rule_logic_not_evaluated_locally" in response.limitations
    assert response.contract_segments[0].source_id == 211


async def test_incomplete_analysis_source_fails_without_partial_result() -> None:
    adapter = AnalysisAdapter()
    adapter.incomplete_lines = True
    with pytest.raises(OdooMcpError) as raised:
        await compare_payroll_periods(
            adapter,  # type: ignore[arg-type]
            _compare_request(),
            request_id="req-incomplete",
            observed_at=OBSERVED_AT,
        )
    assert raised.value.code == ErrorCode.ODOO_API_ERROR


async def test_relation_substitution_fails_instead_of_returning_changed_evidence() -> None:
    adapter = AnalysisAdapter(changed=False, target_basic="100")
    index = next(index for index, value in enumerate(adapter.payslips) if value.id == 401)
    adapter.payslips[index] = adapter.payslips[index].model_copy(
        update={"employee": RelatedRecord(id=101, name="Substituted Employee")}
    )
    with pytest.raises(OdooMcpError) as raised:
        await compare_payroll_periods(
            adapter,  # type: ignore[arg-type]
            _compare_request(),
            request_id="req-substitution",
            observed_at=OBSERVED_AT,
        )
    assert raised.value.code == ErrorCode.PAYROLL_SOURCE_INCONSISTENT


async def test_approval_pack_rejects_changed_source_metadata() -> None:
    adapter = AnalysisAdapter(changed=False, target_basic="100")
    index = next(index for index, value in enumerate(adapter.payslips) if value.id == 401)
    adapter.payslips[index] = adapter.payslips[index].model_copy(
        update={"employee": RelatedRecord(id=101, name="Substituted Employee")}
    )
    with pytest.raises(OdooMcpError) as raised:
        await prepare_payroll_approval_pack(
            adapter,  # type: ignore[arg-type]
            _pack_request(),
            request_id="req-pack-substitution",
            observed_at=OBSERVED_AT,
        )
    assert raised.value.code == ErrorCode.PAYROLL_SOURCE_INCONSISTENT


async def test_missing_exact_employee_comparison_is_not_empty_success() -> None:
    with pytest.raises(OdooMcpError) as raised:
        await analyze_employee_payroll_change(
            AnalysisAdapter(changed=False),  # type: ignore[arg-type]
            AnalyzeEmployeePayrollChangeInput(
                company_id=1,
                employee_id=102,
                baseline_period=BASELINE,
                target_period=TARGET,
            ),
            request_id="req-missing-comparison",
            observed_at=OBSERVED_AT,
        )
    assert raised.value.code == ErrorCode.INSUFFICIENT_COMPARISON_DATA


async def test_combined_multi_period_cap_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(analysis_module, "_MAX_PAYSLIPS", 1)
    with pytest.raises(OdooMcpError) as raised:
        await compare_payroll_periods(
            AnalysisAdapter(changed=False, target_basic="100"),  # type: ignore[arg-type]
            _compare_request(),
            request_id="req-cap",
            observed_at=OBSERVED_AT,
        )
    assert raised.value.code == ErrorCode.PAYROLL_RESULT_TOO_LARGE


async def test_repeat_analysis_uses_fresh_request_state_without_retained_baseline() -> None:
    adapter = AnalysisAdapter(changed=False, target_basic="100")
    first = await compare_payroll_periods(
        adapter,  # type: ignore[arg-type]
        _compare_request(),
        request_id="req-first",
        observed_at=OBSERVED_AT,
    )
    second = await compare_payroll_periods(
        adapter,  # type: ignore[arg-type]
        _compare_request(),
        request_id="req-second",
        observed_at=OBSERVED_AT,
    )
    assert first.model_dump(exclude={"request_id"}) == second.model_dump(exclude={"request_id"})


@dataclass
class Resolver:
    binding: ConnectionBinding

    async def resolve(self) -> ConnectionBinding:
        return self.binding


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_key"),
    [
        (
            "compare_payroll_periods",
            {
                "company_id": 1,
                "baseline_period": BASELINE.model_dump(mode="json"),
                "target_period": TARGET.model_dump(mode="json"),
            },
            "headcount",
        ),
        (
            "analyze_employee_payroll_change",
            {
                "company_id": 1,
                "employee_id": 101,
                "baseline_period": BASELINE.model_dump(mode="json"),
                "target_period": TARGET.model_dump(mode="json"),
            },
            "demonstrated_contributors",
        ),
        (
            "detect_payroll_anomalies",
            {
                "company_id": 1,
                "baseline_period": BASELINE.model_dump(mode="json"),
                "target_period": TARGET.model_dump(mode="json"),
            },
            "rendered_markdown",
        ),
        (
            "explain_payslip",
            {"company_id": 1, "payslip_id": 401},
            "line_groups",
        ),
        (
            "prepare_payroll_approval_pack",
            {
                "company_id": 1,
                "baseline_period": BASELINE.model_dump(mode="json"),
                "target_period": TARGET.model_dump(mode="json"),
            },
            "sign_off_checklist",
        ),
    ],
)
async def test_all_analysis_tools_route_through_the_shared_mcp_boundary(
    connection: OdooConnectionSettings,
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, object],
    expected_key: str,
) -> None:
    storage = Storage.open(tmp_path / f"{tool_name}.sqlite3")
    binding = ConnectionBinding(
        profile=DeploymentProfile.LOCAL,
        tenant_id=f"tenant-{tool_name}",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=frozenset({"payroll_read"}),
        connection=connection,
    )
    adapters: list[AnalysisAdapter] = []

    async def factory(_connection: object) -> OdooAdapter:
        adapter = AnalysisAdapter(target_basic="200")
        adapters.append(adapter)
        return adapter  # type: ignore[return-value]

    server = create_mcp_server(
        Resolver(binding),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        result = await client.call_tool(tool_name, arguments)

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "ok"
    assert expected_key in result.structured_content
    assert "artifact_markdown" not in result.structured_content
    assert adapters and all(value.closed for value in adapters)
    assert len(storage.audit.list_for_tenant(f"tenant-{tool_name}")) == 1


async def test_analysis_tool_audit_is_metadata_only_and_retains_no_payroll_state(
    connection: OdooConnectionSettings,
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "analysis.sqlite3"
    storage = Storage.open(storage_path)
    adapter = AnalysisAdapter(target_basic="200")
    binding = ConnectionBinding(
        profile=DeploymentProfile.LOCAL,
        tenant_id="tenant-analysis",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=frozenset({"payroll_read"}),
        connection=connection,
    )

    async def factory(_connection: object) -> OdooAdapter:
        return adapter  # type: ignore[return-value]

    server = create_mcp_server(
        Resolver(binding),
        adapter_factory=factory,
        storage=storage,
    )
    async with Client(server) as client:
        result = await client.call_tool(
            "detect_payroll_anomalies",
            {
                "company_id": 1,
                "baseline_period": {
                    "period_start": "2026-01-01",
                    "period_end": "2026-01-31",
                },
                "target_period": {
                    "period_start": "2026-02-01",
                    "period_end": "2026-02-28",
                },
                "history_periods": [value.model_dump(mode="json") for value in HISTORY],
                "employee_ids": [101],
            },
        )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "ok"
    assert adapter.closed is True
    audit = storage.audit.list_for_tenant("tenant-analysis")[0]
    assert audit.input_payload == {
        "batch_filter_count": 0,
        "comparison_requested": True,
        "continuation_requested": False,
        "employee_filter_count": 1,
        "history_requested": True,
        "normalized_states": ["waiting", "done", "paid"],
        "payslip_filter_count": 0,
        "period_filter_count": 8,
        "query_kind": "detect_payroll_anomalies",
    }
    assert audit.actual_result is not None
    assert audit.actual_result["item_count"] == len(result.structured_content["findings"])
    assert audit.affected_odoo_records == ()
    serialized = json.dumps(
        {"input": audit.input_payload, "result": audit.actual_result},
        sort_keys=True,
    )
    assert "rendered_markdown" not in serialized
    for forbidden in (
        "2026-01-01",
        "2026-02-28",
        "Synthetic Employee",
        "200",
        "101",
    ):
        assert forbidden not in serialized
    with sqlite3.connect(storage_path) as database:
        counts = {
            table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("proposals", "artifacts", "idempotency_keys", "capabilities_cache")
        }
    assert counts == {
        "proposals": 0,
        "artifacts": 0,
        "idempotency_keys": 0,
        "capabilities_cache": 0,
    }


async def test_approval_pack_repeats_without_persisting_pack_or_workflow_state(
    connection: OdooConnectionSettings,
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "approval-pack.sqlite3"
    storage = Storage.open(storage_path)
    binding = ConnectionBinding(
        profile=DeploymentProfile.LOCAL,
        tenant_id="tenant-approval-pack",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=frozenset({"payroll_read"}),
        connection=connection,
    )

    async def factory(_connection: object) -> OdooAdapter:
        return AnalysisAdapter(target_basic="200")  # type: ignore[return-value]

    server = create_mcp_server(
        Resolver(binding),
        adapter_factory=factory,
        storage=storage,
    )
    arguments = {
        "company_id": 1,
        "baseline_period": BASELINE.model_dump(mode="json"),
        "target_period": TARGET.model_dump(mode="json"),
        "employee_ids": [101],
    }
    async with Client(server) as client:
        first = await client.call_tool("prepare_payroll_approval_pack", arguments)
        second = await client.call_tool("prepare_payroll_approval_pack", arguments)

    assert first.is_error is False and second.is_error is False
    assert first.structured_content is not None
    assert second.structured_content is not None
    assert first.structured_content["rule_totals"] == second.structured_content["rule_totals"]
    audits = storage.audit.list_for_tenant("tenant-approval-pack")
    assert len(audits) == 2
    assert all(
        value.input_payload
        == {
            "batch_filter_count": 0,
            "comparison_requested": True,
            "continuation_requested": False,
            "employee_filter_count": 1,
            "history_requested": False,
            "normalized_states": ["waiting", "done", "paid"],
            "payslip_filter_count": 0,
            "period_filter_count": 2,
            "query_kind": "prepare_payroll_approval_pack",
        }
        for value in audits
    )
    serialized = json.dumps(
        [{"input": value.input_payload, "result": value.actual_result} for value in audits],
        sort_keys=True,
    )
    assert "rendered_markdown" not in serialized
    assert "Synthetic Employee" not in serialized
    with sqlite3.connect(storage_path) as database:
        counts = {
            table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("proposals", "artifacts", "idempotency_keys", "capabilities_cache")
        }
    assert counts == {
        "proposals": 0,
        "artifacts": 0,
        "idempotency_keys": 0,
        "capabilities_cache": 0,
    }


async def test_approval_pack_denial_is_structured_and_skips_odoo(
    connection: OdooConnectionSettings,
    tmp_path: Path,
) -> None:
    storage = Storage.open(tmp_path / "approval-pack-denied.sqlite3")
    binding = ConnectionBinding(
        profile=DeploymentProfile.LOCAL,
        tenant_id="tenant-approval-pack-denied",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=frozenset(),
        connection=connection,
    )
    calls = 0

    async def factory(_connection: object) -> OdooAdapter:
        nonlocal calls
        calls += 1
        return AnalysisAdapter()  # type: ignore[return-value]

    server = create_mcp_server(Resolver(binding), adapter_factory=factory, storage=storage)
    async with Client(server) as client:
        result = await client.call_tool(
            "prepare_payroll_approval_pack",
            {
                "company_id": 1,
                "target_period": TARGET.model_dump(mode="json"),
            },
        )

    assert result.structured_content is not None
    assert result.structured_content["status"] == "failed"
    assert result.structured_content["error_code"] == "ODOO_AUTH_FAILED"
    assert calls == 0


async def test_approval_pack_timeout_is_safe_and_audited(
    connection: OdooConnectionSettings,
    tmp_path: Path,
) -> None:
    class TimeoutAdapter(AnalysisAdapter):
        async def get_payslips(
            self,
            company_id: int,
            filters: PayslipFilters,
            page: PayrollPageRequest,
        ) -> PayrollPage[Payslip]:
            del company_id, filters, page
            raise TimeoutError

    storage = Storage.open(tmp_path / "approval-pack-timeout.sqlite3")
    binding = ConnectionBinding(
        profile=DeploymentProfile.LOCAL,
        tenant_id="tenant-approval-pack-timeout",
        authenticated_subject="synthetic-subject",
        mcp_client="synthetic-client",
        permissions=frozenset({"payroll_read"}),
        connection=connection,
    )

    async def factory(_connection: object) -> OdooAdapter:
        return TimeoutAdapter()  # type: ignore[return-value]

    server = create_mcp_server(Resolver(binding), adapter_factory=factory, storage=storage)
    async with Client(server) as client:
        result = await client.call_tool(
            "prepare_payroll_approval_pack",
            {
                "company_id": 1,
                "target_period": TARGET.model_dump(mode="json"),
            },
        )

    assert result.structured_content is not None
    assert result.structured_content == {
        "status": "failed",
        "request_id": result.structured_content["request_id"],
        "error_code": "UNKNOWN_ERROR",
        "error_message": "The Payroll evidence request failed unexpectedly.",
        "remediation_hint": "Retry the request or contact the service operator.",
    }
    audit = storage.audit.list_for_tenant("tenant-approval-pack-timeout")
    assert len(audit) == 1
    assert audit[0].final_status == "failed"
