from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from odoo_mcp.adapters.accounting import RelatedRecord
from odoo_mcp.adapters.base import CapabilitySnapshot, Company
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
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.payroll_schemas import (
    GetAttendanceSummaryInput,
    GetEmployeePayrollContextInput,
    GetPayrollBatchInput,
    GetPayslipInput,
    ListPayrollPeriodsInput,
    ListPayslipsInput,
    ListSalaryRulesInput,
    PayrollToolResponse,
)
from odoo_mcp.workflows.payroll.evidence import (
    get_attendance_summary,
    get_employee_payroll_context,
    get_payroll_batch,
    get_payslip,
    list_payroll_periods,
    list_payslips,
    list_salary_rules,
)

OBSERVED_AT = datetime(2026, 9, 25, 12, tzinfo=UTC)
WRITE_DATE = datetime(2026, 9, 25, 10, tzinfo=UTC)
COMPANY = RelatedRecord(id=1, name="Synthetic Company")
USD = RelatedRecord(id=10, name="USD")
EUR = RelatedRecord(id=20, name="EUR")
BATCH_REF = RelatedRecord(id=50, name="Synthetic January")
STRUCTURE = RelatedRecord(id=60, name="Monthly")
EMPLOYEE_ONE = RelatedRecord(id=101, name="Synthetic Employee One")
EMPLOYEE_TWO = RelatedRecord(id=102, name="Synthetic Employee Two")
CONTRACT_ONE = RelatedRecord(id=201, name="Contract One")
CONTRACT_TWO = RelatedRecord(id=202, name="Contract Two")


def _batch() -> PayrollBatch:
    return PayrollBatch(
        id=BATCH_REF.id,
        name=BATCH_REF.name,
        date_start=date(2026, 1, 1),
        date_end=date(2026, 1, 31),
        state="done",
        source_state="02_close",
        company_id=1,
        write_date=WRITE_DATE,
    )


def _payslip(
    identifier: int,
    employee: RelatedRecord,
    contract: RelatedRecord,
    currency: RelatedRecord,
    *,
    state: str = "done",
    company_id: int = 1,
) -> Payslip:
    source_state = {
        "draft": "draft",
        "waiting": "verify",
        "done": "validated",
        "paid": "paid",
        "cancelled": "cancel",
    }[state]
    return Payslip(
        id=identifier,
        name=f"Payslip {identifier}",
        reference=f"PS-{identifier}",
        employee=employee,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        state=state,  # type: ignore[arg-type]
        source_state=source_state,
        company_id=company_id,
        contract_segment=contract,
        contract_source="hr.version",
        structure=STRUCTURE,
        batch=BATCH_REF,
        credit_note=False,
        currency=currency,
        write_date=WRITE_DATE,
    )


def _line(
    identifier: int,
    payslip: Payslip,
    rule_id: int,
    code: str,
    total: str,
    *,
    category_id: int = 401,
    category_name: str = "Basic",
    sequence: int = 10,
) -> PayslipLine:
    return PayslipLine(
        id=identifier,
        payslip=RelatedRecord(id=payslip.id, name=payslip.name),
        salary_rule=RelatedRecord(id=rule_id, name=f"Rule {code}"),
        employee=payslip.employee,
        contract_segment=payslip.contract_segment,
        contract_source=payslip.contract_source,
        name=f"Line {code}",
        code=code,
        category=RelatedRecord(id=category_id, name=category_name),
        sequence=sequence,
        quantity=Decimal("1"),
        rate=Decimal("100"),
        amount=Decimal(total),
        total=Decimal(total),
        currency=payslip.currency,
        write_date=WRITE_DATE,
    )


class FakePayrollAdapter:
    def __init__(self) -> None:
        first = _payslip(301, EMPLOYEE_ONE, CONTRACT_ONE, USD)
        second = _payslip(302, EMPLOYEE_TWO, CONTRACT_TWO, EUR, state="paid")
        self.batches = [_batch()]
        self.payslips = [first, second]
        self.lines = [
            _line(501, first, 701, "BASIC", "1000"),
            _line(
                502,
                first,
                702,
                "TAX",
                "-100",
                category_id=402,
                category_name="Deduction",
                sequence=20,
            ),
            _line(503, second, 701, "BASIC", "900"),
        ]
        self.worked_days = [
            PayslipWorkedDay(
                id=601,
                payslip=RelatedRecord(id=first.id, name=first.name),
                contract_segment=first.contract_segment,
                contract_source="hr.version",
                work_entry_type=RelatedRecord(id=801, name="Attendance"),
                name="Regular work",
                code="WORK100",
                number_of_days=Decimal("20"),
                number_of_hours=Decimal("160"),
                amount=Decimal("0"),
                currency=USD,
                write_date=WRITE_DATE,
            )
        ]
        self.inputs = [
            PayslipInput(
                id=901,
                name="Synthetic allowance",
                payslip=RelatedRecord(id=first.id, name=first.name),
                sequence=5,
                input_type=RelatedRecord(id=1001, name="Allowance"),
                code="ALLOW",
                amount=Decimal("25"),
                contract_segment=first.contract_segment,
                contract_source="hr.version",
                write_date=WRITE_DATE,
            )
        ]
        self.input_types = [
            PayrollInputType(
                id=1001,
                name="Allowance",
                code="ALLOW",
                structure_ids=(STRUCTURE.id,),
                active=True,
                is_quantity=False,
                available_in_attachments=False,
                write_date=WRITE_DATE,
            )
        ]
        self.employees = [
            PayrollEmployee(
                id=EMPLOYEE_ONE.id,
                name=EMPLOYEE_ONE.name,
                active=True,
                company_id=1,
                write_date=WRITE_DATE,
            ),
            PayrollEmployee(
                id=EMPLOYEE_TWO.id,
                name=EMPLOYEE_TWO.name,
                active=True,
                company_id=1,
                write_date=WRITE_DATE,
            ),
        ]
        self.segments = [
            PayrollContractSegment(
                source_model="hr.version",
                id=CONTRACT_ONE.id,
                employee=EMPLOYEE_ONE,
                company_id=1,
                active=True,
                source_status="unavailable",
                revision_date=date(2025, 12, 1),
                effective_start=date(2026, 1, 1),
                effective_end=date(2026, 1, 31),
                wage=Decimal("1000"),
                currency=USD,
                structure_type=RelatedRecord(id=1101, name="Employee"),
                resource_calendar=RelatedRecord(id=1102, name="40 Hours"),
                department=RelatedRecord(id=1103, name="Synthetic Department"),
                job=RelatedRecord(id=1104, name="Synthetic Role"),
                contract_type=RelatedRecord(id=1105, name="Permanent"),
                write_date=WRITE_DATE,
            )
        ]
        self.work_entries = [
            PayrollWorkEntry(
                id=1201,
                employee=EMPLOYEE_ONE,
                company_id=1,
                contract_segment=CONTRACT_ONE,
                contract_source="hr.version",
                date_start=date(2026, 1, 2),
                date_end=date(2026, 1, 2),
                duration=Decimal("8"),
                work_entry_type=RelatedRecord(id=801, name="Attendance"),
                code="WORK100",
                state="validated",
                conflict=False,
                write_date=WRITE_DATE,
            ),
            PayrollWorkEntry(
                id=1202,
                employee=EMPLOYEE_ONE,
                company_id=1,
                contract_segment=CONTRACT_ONE,
                contract_source="hr.version",
                date_start=date(2026, 1, 3),
                date_end=date(2026, 1, 3),
                duration=Decimal("4"),
                work_entry_type=RelatedRecord(id=801, name="Attendance"),
                code="WORK100",
                state="conflict",
                conflict=True,
                write_date=WRITE_DATE,
            ),
        ]
        self.incomplete_pages = False

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

    async def get_capabilities(self) -> CapabilitySnapshot:
        return CapabilitySnapshot(
            edition="enterprise",
            version=19,
            transport="json2",
            modules={"base": True, "hr_payroll": True},
        )

    async def get_companies(self) -> list[Company]:
        return [Company(id=1, name=COMPANY.name, currency=USD)]

    async def get_payroll_batches(
        self,
        company_id: int,
        filters: PayrollBatchFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayrollBatch]:
        rows = [row for row in self.batches if row.company_id == company_id]
        if filters.batch_ids:
            rows = [row for row in rows if row.id in filters.batch_ids]
        if filters.window is not None:
            rows = [
                row
                for row in rows
                if row.date_start >= filters.window.start and row.date_end <= filters.window.end
            ]
        if filters.states:
            rows = [row for row in rows if row.state in filters.states]
        return self._page(rows, page)

    async def get_payslips(
        self,
        company_id: int,
        filters: PayslipFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[Payslip]:
        rows = [row for row in self.payslips if row.company_id == company_id]
        if filters.payslip_ids:
            rows = [row for row in rows if row.id in filters.payslip_ids]
        if filters.batch_id is not None:
            rows = [
                row for row in rows if row.batch is not None and row.batch.id == filters.batch_id
            ]
        if filters.period is not None:
            rows = [
                row
                for row in rows
                if row.date_from == filters.period.start and row.date_to == filters.period.end
            ]
        if filters.window is not None:
            rows = [
                row
                for row in rows
                if row.date_from >= filters.window.start and row.date_to <= filters.window.end
            ]
        if filters.employee_ids:
            rows = [row for row in rows if row.employee.id in filters.employee_ids]
        if filters.states:
            rows = [row for row in rows if row.state in filters.states]
        rows.sort(key=lambda row: (row.date_from, row.date_to, row.id), reverse=True)
        result = self._page(rows, page)
        if self.incomplete_pages and rows:
            return PayrollPage[Payslip](
                items=[],
                total_count=len(rows),
                next_cursor=None,
            )
        return result

    async def get_payslip_lines(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayslipLine]:
        del company_id
        rows = [row for row in self.lines if row.payslip.id in filters.payslip_ids]
        if filters.record_ids:
            rows = [row for row in rows if row.id in filters.record_ids]
        rows.sort(key=lambda row: (row.sequence, row.id))
        return self._page(rows, page)

    async def get_payslip_worked_days(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayslipWorkedDay]:
        del company_id
        rows = [row for row in self.worked_days if row.payslip.id in filters.payslip_ids]
        return self._page(rows, page)

    async def get_payslip_inputs(
        self,
        company_id: int,
        filters: PayslipChildFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayslipInput]:
        del company_id
        rows = [row for row in self.inputs if row.payslip.id in filters.payslip_ids]
        rows.sort(key=lambda row: (row.sequence, row.id))
        return self._page(rows, page)

    async def get_payroll_input_types(
        self,
        company_id: int,
        filters: PayrollInputTypeFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayrollInputType]:
        del company_id
        rows = [
            row
            for row in self.input_types
            if filters.structure_id in row.structure_ids
            and (not filters.input_type_ids or row.id in filters.input_type_ids)
        ]
        return self._page(rows, page)

    async def get_payroll_employees(
        self,
        company_id: int,
        employee_ids: tuple[int, ...],
        page: PayrollPageRequest,
    ) -> PayrollPage[PayrollEmployee]:
        rows = [
            row for row in self.employees if row.company_id == company_id and row.id in employee_ids
        ]
        return self._page(rows, page)

    async def get_payroll_contract_segments(
        self,
        company_id: int,
        filters: PayrollContractFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayrollContractSegment]:
        rows = [
            row
            for row in self.segments
            if row.company_id == company_id
            and row.employee.id in filters.employee_ids
            and row.effective_start <= filters.period.end
            and row.effective_end >= filters.period.start
        ]
        return self._page(rows, page)

    async def get_payroll_work_entries(
        self,
        company_id: int,
        filters: PayrollWorkEntryFilters,
        page: PayrollPageRequest,
    ) -> PayrollPage[PayrollWorkEntry]:
        rows = [
            row
            for row in self.work_entries
            if row.company_id == company_id
            and row.employee.id in filters.employee_ids
            and row.date_start <= filters.period.end
            and row.date_end >= filters.period.start
            and (not filters.states or row.state in filters.states)
        ]
        return self._page(rows, page)


async def test_all_seven_payroll_evidence_workflows_return_bounded_source_evidence() -> None:
    adapter = FakePayrollAdapter()

    periods = await list_payroll_periods(
        adapter,  # type: ignore[arg-type]
        ListPayrollPeriodsInput(
            company_id=1,
            window_start=date(2026, 1, 1),
            window_end=date(2026, 1, 31),
        ),
        request_id="req-periods",
        observed_at=OBSERVED_AT,
    )
    assert periods.summary.period_count == 1
    assert periods.items[0].batch_ids == (50,)
    assert [currency.id for currency in periods.items[0].currencies] == [10, 20]

    batch = await get_payroll_batch(
        adapter,  # type: ignore[arg-type]
        GetPayrollBatchInput(company_id=1, batch_id=50, limit=1),
        request_id="req-batch",
        observed_at=OBSERVED_AT,
    )
    assert batch.batch.batch_id == 50
    assert batch.summary.employee_count == 2
    assert batch.summary.has_more is True
    assert {(item.currency.id, item.salary_rule.id) for item in batch.summary.rule_totals} == {
        (10, 701),
        (10, 702),
        (20, 701),
    }

    payslips = await list_payslips(
        adapter,  # type: ignore[arg-type]
        ListPayslipsInput(
            company_id=1,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        ),
        request_id="req-payslips",
        observed_at=OBSERVED_AT,
    )
    assert [item.payslip_id for item in payslips.items] == [302, 301]
    assert all(item.contract_source == "hr.version" for item in payslips.items)

    payslip = await get_payslip(
        adapter,  # type: ignore[arg-type]
        GetPayslipInput(company_id=1, payslip_id=301),
        request_id="req-payslip",
        observed_at=OBSERVED_AT,
    )
    assert [item.source_line_id for item in payslip.salary_lines] == [501, 502]
    assert payslip.worked_days[0].hours == Decimal("160")
    assert payslip.inputs[0].amount == Decimal("25")
    assert payslip.inputs[0].quantity is None
    serialized_payslip = PayrollToolResponse.from_success(payslip).model_dump(mode="json")
    assert serialized_payslip["salary_lines"][0]["total"] == "1000"
    assert serialized_payslip["inputs"][0]["amount"] == "25"

    context = await get_employee_payroll_context(
        adapter,  # type: ignore[arg-type]
        GetEmployeePayrollContextInput(
            company_id=1,
            employee_id=101,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        ),
        request_id="req-context",
        observed_at=OBSERVED_AT,
    )
    assert context.employee.name == EMPLOYEE_ONE.name
    assert context.contract_segments[0].wage == Decimal("1000")
    assert context.limitations == ["contract_source_status_unavailable"]

    rules = await list_salary_rules(
        adapter,  # type: ignore[arg-type]
        ListSalaryRulesInput(company_id=1, batch_id=50),
        request_id="req-rules",
        observed_at=OBSERVED_AT,
    )
    assert [item.code for item in rules.items] == ["BASIC", "TAX"]
    assert rules.items[0].affected_payslip_count == 2
    assert {reference.source_model: reference.source_ids for reference in rules.source_refs} == {
        "hr.payslip": (301, 302),
        "hr.payslip.line": (501, 502, 503),
        "hr.payslip.run": (50,),
    }

    attendance = await get_attendance_summary(
        adapter,  # type: ignore[arg-type]
        GetAttendanceSummaryInput(
            company_id=1,
            employee_ids=(101,),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        ),
        request_id="req-attendance",
        observed_at=OBSERVED_AT,
    )
    assert attendance.summary.hours == Decimal("12")
    assert attendance.summary.conflict_count == 1
    assert {item.state for item in attendance.items} == {"validated", "conflict"}
    assert attendance.limitations == ["work_entries_do_not_prove_physical_attendance"]


async def test_batch_cursor_is_bound_to_request_and_source_anchors() -> None:
    adapter = FakePayrollAdapter()
    first = await get_payroll_batch(
        adapter,  # type: ignore[arg-type]
        GetPayrollBatchInput(company_id=1, batch_id=50, limit=1),
        request_id="req-first",
        observed_at=OBSERVED_AT,
    )
    assert first.next_cursor is not None

    second = await get_payroll_batch(
        adapter,  # type: ignore[arg-type]
        GetPayrollBatchInput(
            company_id=1,
            batch_id=50,
            limit=1,
            cursor=first.next_cursor,
        ),
        request_id="req-second",
        observed_at=OBSERVED_AT,
    )
    assert len(second.items) == 1
    assert second.next_cursor is None

    changed = adapter.payslips[0].model_copy(
        update={"write_date": datetime(2026, 9, 25, 11, tzinfo=UTC)}
    )
    adapter.payslips[0] = changed
    with pytest.raises(OdooMcpError) as exc_info:
        await get_payroll_batch(
            adapter,  # type: ignore[arg-type]
            GetPayrollBatchInput(
                company_id=1,
                batch_id=50,
                limit=1,
                cursor=first.next_cursor,
            ),
            request_id="req-changed",
            observed_at=OBSERVED_AT,
        )
    assert exc_info.value.code is ErrorCode.INVALID_INPUT


async def test_cancelled_payslips_are_visible_only_when_requested_and_excluded_from_totals() -> (
    None
):
    adapter = FakePayrollAdapter()
    cancelled = _payslip(
        303,
        EMPLOYEE_ONE,
        CONTRACT_ONE,
        USD,
        state="cancelled",
    )
    adapter.payslips.append(cancelled)
    adapter.lines.append(_line(504, cancelled, 701, "BASIC", "999"))

    default_batch = await get_payroll_batch(
        adapter,  # type: ignore[arg-type]
        GetPayrollBatchInput(company_id=1, batch_id=50),
        request_id="req-default-states",
        observed_at=OBSERVED_AT,
    )
    assert {item.payslip_id for item in default_batch.items} == {301, 302}
    assert all(item.total != Decimal("1999") for item in default_batch.summary.rule_totals)

    cancelled_batch = await get_payroll_batch(
        adapter,  # type: ignore[arg-type]
        GetPayrollBatchInput(company_id=1, batch_id=50, states=("cancelled",)),
        request_id="req-cancelled",
        observed_at=OBSERVED_AT,
    )
    assert [item.payslip_id for item in cancelled_batch.items] == [303]
    assert cancelled_batch.summary.non_cancelled_payslip_count == 0
    assert cancelled_batch.summary.employee_count == 0
    assert cancelled_batch.summary.rule_totals == []
    assert cancelled_batch.summary.category_totals == []


async def test_cancelled_work_entries_require_an_explicit_state_filter() -> None:
    adapter = FakePayrollAdapter()
    adapter.work_entries.append(
        adapter.work_entries[0].model_copy(
            update={
                "id": 1203,
                "state": "cancelled",
                "duration": Decimal("100"),
            }
        )
    )
    default = await get_attendance_summary(
        adapter,  # type: ignore[arg-type]
        GetAttendanceSummaryInput(
            company_id=1,
            employee_ids=(101,),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        ),
        request_id="req-default-work-entries",
        observed_at=OBSERVED_AT,
    )
    cancelled = await get_attendance_summary(
        adapter,  # type: ignore[arg-type]
        GetAttendanceSummaryInput(
            company_id=1,
            employee_ids=(101,),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            states=("cancelled",),
        ),
        request_id="req-cancelled-work-entries",
        observed_at=OBSERVED_AT,
    )

    assert default.summary.hours == Decimal("12")
    assert cancelled.summary.hours == Decimal("100")
    assert [item.state for item in cancelled.items] == ["cancelled"]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "company_id": 1,
            "window_start": date(2020, 1, 1),
            "window_end": date(2026, 1, 2),
        },
        {"company_id": 1, "batch_id": None},
        {
            "company_id": 1,
            "batch_id": 50,
            "period_start": date(2026, 1, 1),
            "period_end": date(2026, 1, 31),
        },
    ],
)
def test_payroll_inputs_reject_unbounded_or_ambiguous_filters(payload: dict[str, object]) -> None:
    model = ListPayrollPeriodsInput if "window_start" in payload else ListSalaryRulesInput
    with pytest.raises(ValidationError):
        model.model_validate(payload)


async def test_exact_substitution_cross_company_and_wrong_parent_fail_closed() -> None:
    class SubstitutingAdapter(FakePayrollAdapter):
        async def get_payslips(
            self,
            company_id: int,
            filters: PayslipFilters,
            page: PayrollPageRequest,
        ) -> PayrollPage[Payslip]:
            if filters.payslip_ids:
                return self._page([self.payslips[1]], page)
            return await super().get_payslips(company_id, filters, page)

    adapter = SubstitutingAdapter()
    with pytest.raises(OdooMcpError) as substituted:
        await get_payslip(
            adapter,  # type: ignore[arg-type]
            GetPayslipInput(company_id=1, payslip_id=301),
            request_id="req-substitution",
            observed_at=OBSERVED_AT,
        )
    assert substituted.value.code is ErrorCode.PAYROLL_SOURCE_INCONSISTENT

    class ForeignMemberAdapter(FakePayrollAdapter):
        async def get_payslips(
            self,
            company_id: int,
            filters: PayslipFilters,
            page: PayrollPageRequest,
        ) -> PayrollPage[Payslip]:
            if filters.batch_id is not None:
                foreign = self.payslips[0].model_copy(update={"company_id": 2})
                return self._page([foreign], page)
            return await super().get_payslips(company_id, filters, page)

    adapter = ForeignMemberAdapter()
    with pytest.raises(OdooMcpError) as foreign:
        await get_payroll_batch(
            adapter,  # type: ignore[arg-type]
            GetPayrollBatchInput(company_id=1, batch_id=50),
            request_id="req-foreign",
            observed_at=OBSERVED_AT,
        )
    assert foreign.value.code is ErrorCode.PAYROLL_SOURCE_INCONSISTENT

    class WrongParentAdapter(FakePayrollAdapter):
        async def get_payslip_lines(
            self,
            company_id: int,
            filters: PayslipChildFilters,
            page: PayrollPageRequest,
        ) -> PayrollPage[PayslipLine]:
            del company_id, filters
            wrong = self.lines[0].model_copy(
                update={"payslip": RelatedRecord(id=302, name="Payslip 302")}
            )
            return self._page([wrong], page)

    adapter = WrongParentAdapter()
    with pytest.raises(OdooMcpError) as wrong_parent:
        await get_payslip(
            adapter,  # type: ignore[arg-type]
            GetPayslipInput(company_id=1, payslip_id=301),
            request_id="req-parent",
            observed_at=OBSERVED_AT,
        )
    assert wrong_parent.value.code is ErrorCode.PAYROLL_SOURCE_INCONSISTENT


async def test_incomplete_sources_conflicting_rule_metadata_and_detail_caps_fail() -> None:
    adapter = FakePayrollAdapter()
    adapter.incomplete_pages = True
    with pytest.raises(OdooMcpError) as incomplete:
        await list_payroll_periods(
            adapter,  # type: ignore[arg-type]
            ListPayrollPeriodsInput(
                company_id=1,
                window_start=date(2026, 1, 1),
                window_end=date(2026, 1, 31),
            ),
            request_id="req-incomplete",
            observed_at=OBSERVED_AT,
        )
    assert incomplete.value.code is ErrorCode.ODOO_API_ERROR

    adapter = FakePayrollAdapter()
    adapter.lines[2] = adapter.lines[2].model_copy(update={"code": "CONFLICTING"})
    with pytest.raises(OdooMcpError) as conflicting:
        await list_salary_rules(
            adapter,  # type: ignore[arg-type]
            ListSalaryRulesInput(company_id=1, batch_id=50),
            request_id="req-conflict",
            observed_at=OBSERVED_AT,
        )
    assert conflicting.value.code is ErrorCode.PAYROLL_SOURCE_INCONSISTENT

    adapter = FakePayrollAdapter()
    parent = adapter.payslips[0]
    adapter.lines = [
        _line(2_000 + index, parent, 3_000 + index, f"R{index}", "1", sequence=index)
        for index in range(501)
    ]
    with pytest.raises(OdooMcpError) as excessive:
        await get_payslip(
            adapter,  # type: ignore[arg-type]
            GetPayslipInput(company_id=1, payslip_id=301),
            request_id="req-cap",
            observed_at=OBSERVED_AT,
        )
    assert excessive.value.code is ErrorCode.PAYROLL_RESULT_TOO_LARGE


async def test_missing_exact_records_use_namespaced_errors_and_empty_filters_succeed() -> None:
    adapter = FakePayrollAdapter()
    adapter.batches = []
    with pytest.raises(OdooMcpError) as missing_batch:
        await get_payroll_batch(
            adapter,  # type: ignore[arg-type]
            GetPayrollBatchInput(company_id=1, batch_id=50),
            request_id="req-missing-batch",
            observed_at=OBSERVED_AT,
        )
    assert missing_batch.value.code is ErrorCode.PAYROLL_BATCH_NOT_FOUND

    adapter = FakePayrollAdapter()
    adapter.segments = []
    with pytest.raises(OdooMcpError) as missing_contract:
        await get_employee_payroll_context(
            adapter,  # type: ignore[arg-type]
            GetEmployeePayrollContextInput(
                company_id=1,
                employee_id=101,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
            ),
            request_id="req-missing-contract",
            observed_at=OBSERVED_AT,
        )
    assert missing_contract.value.code is ErrorCode.CONTRACT_DATA_MISSING

    empty = await list_payslips(
        FakePayrollAdapter(),  # type: ignore[arg-type]
        ListPayslipsInput(
            company_id=1,
            batch_id=50,
            employee_ids=(999,),
        ),
        request_id="req-empty",
        observed_at=OBSERVED_AT,
    )
    assert empty.items == []
    assert empty.summary.payslip_count == 0


async def test_attendance_requires_all_exact_employees_and_rejects_relation_drift() -> None:
    adapter = FakePayrollAdapter()
    with pytest.raises(OdooMcpError) as missing:
        await get_attendance_summary(
            adapter,  # type: ignore[arg-type]
            GetAttendanceSummaryInput(
                company_id=1,
                employee_ids=(101, 999),
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
            ),
            request_id="req-missing-employee",
            observed_at=OBSERVED_AT,
        )
    assert missing.value.code is ErrorCode.EMPLOYEE_NOT_FOUND

    adapter = FakePayrollAdapter()
    adapter.work_entries[0] = adapter.work_entries[0].model_copy(
        update={"employee": RelatedRecord(id=101, name="Substituted Name")}
    )
    with pytest.raises(OdooMcpError) as drift:
        await get_attendance_summary(
            adapter,  # type: ignore[arg-type]
            GetAttendanceSummaryInput(
                company_id=1,
                employee_ids=(101,),
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
            ),
            request_id="req-drift",
            observed_at=OBSERVED_AT,
        )
    assert drift.value.code is ErrorCode.PAYROLL_SOURCE_INCONSISTENT


async def test_employee_context_preserves_odoo18_contract_evidence_without_version_logic() -> None:
    adapter = FakePayrollAdapter()
    adapter.segments = [
        adapter.segments[0].model_copy(
            update={
                "source_model": "hr.contract",
                "source_status": "open",
                "revision_date": None,
            }
        )
    ]

    context = await get_employee_payroll_context(
        adapter,  # type: ignore[arg-type]
        GetEmployeePayrollContextInput(
            company_id=1,
            employee_id=101,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        ),
        request_id="req-odoo18-context",
        observed_at=OBSERVED_AT,
    )

    assert context.contract_segments[0].source_model == "hr.contract"
    assert context.contract_segments[0].source_status == "open"
    assert context.limitations == []


def test_empty_state_filters_normalize_to_the_contracted_defaults() -> None:
    payslip_request = GetPayrollBatchInput(company_id=1, batch_id=50, states=())
    attendance_request = GetAttendanceSummaryInput(
        company_id=1,
        employee_ids=(101,),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        states=(),
    )

    assert payslip_request.states == ("waiting", "done", "paid")
    assert attendance_request.states == (
        "draft",
        "conflict",
        "validated",
    )


def test_payroll_workflow_depends_only_on_the_typed_adapter_boundary() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "odoo_mcp"
        / "workflows"
        / "payroll"
        / "evidence.py"
    ).read_text(encoding="utf-8")

    assert "odoo_mcp.adapters.odoo" not in source
    assert "OdooClient" not in source
