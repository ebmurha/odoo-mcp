from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from odoo_mcp.adapters.odoo.payroll import (
    BATCH_STATE_MAP,
    PAYROLL_MODEL_FIELDS_BY_VERSION,
    PAYROLL_SOURCE_CAPS,
    PAYSLIP_STATE_MAP,
    WORK_ENTRY_STATE_MAP,
    PayrollReader,
)
from odoo_mcp.adapters.payroll import (
    DraftPayslipInputCreate,
    DraftPayslipInputUpdate,
    PayrollBatchFilters,
    PayrollContractFilters,
    PayrollDiscoveryWindow,
    PayrollInputTypeFilters,
    PayrollPageRequest,
    PayrollPeriod,
    PayrollWorkEntryFilters,
    PayrollWriteCheckpoint,
    PayrollWriteRejected,
    PayslipChildFilters,
    PayslipFilters,
)
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError

STAMP = "2026-09-01 10:00:00"


def _rows(version: int) -> dict[str, list[dict[str, Any]]]:
    contract_field = "contract_id" if version == 18 else "version_id"
    contract_id = 301
    payslip: dict[str, Any] = {
        "id": 101,
        "name": "Synthetic Payslip 101",
        "employee_id": [501, "Synthetic Worker 501"],
        "date_from": "2026-09-01",
        "date_to": "2026-09-30",
        "state": "draft",
        "company_id": [1, "Synthetic Company 1"],
        contract_field: [contract_id, "Synthetic Contract 301"],
        "struct_id": [201, "Synthetic Structure 201"],
        "payslip_run_id": [111, "Synthetic Run 111"],
        "credit_note": False,
        "currency_id": [1, "SYN"],
        "write_date": STAMP,
    }
    if version == 18:
        payslip["number"] = "SYN/2026/101"
    contract: dict[str, Any]
    if version == 18:
        contract = {
            "id": contract_id,
            "employee_id": [501, "Synthetic Worker 501"],
            "company_id": [1, "Synthetic Company 1"],
            "active": True,
            "state": "open",
            "date_start": "2026-01-01",
            "date_end": "2026-12-31",
            "wage": "1000.125",
            "currency_id": [1, "SYN"],
            "structure_type_id": [701, "Synthetic Type 701"],
            "resource_calendar_id": [702, "Synthetic Calendar 702"],
            "department_id": [703, "Synthetic Department 703"],
            "job_id": [704, "Synthetic Job 704"],
            "contract_type_id": [705, "Synthetic Contract Type 705"],
            "write_date": STAMP,
        }
    else:
        contract = {
            "id": contract_id,
            "employee_id": [501, "Synthetic Worker 501"],
            "company_id": [1, "Synthetic Company 1"],
            "active": True,
            "date_version": "2026-01-01",
            "contract_date_start": "2026-01-01",
            "contract_date_end": "2026-12-31",
            "wage": "1000.125",
            "currency_id": [1, "SYN"],
            "structure_type_id": [701, "Synthetic Type 701"],
            "resource_calendar_id": [702, "Synthetic Calendar 702"],
            "department_id": [703, "Synthetic Department 703"],
            "job_id": [704, "Synthetic Job 704"],
            "contract_type_id": [705, "Synthetic Contract Type 705"],
            "write_date": STAMP,
        }
    work_entry = {
        "id": 601,
        "employee_id": [501, "Synthetic Worker 501"],
        "company_id": [1, "Synthetic Company 1"],
        "duration": "8.125",
        "work_entry_type_id": [801, "Synthetic Work Type 801"],
        "code": "SYN_WORK",
        "state": "validated",
        "conflict": False,
        "write_date": STAMP,
    }
    if version == 18:
        work_entry.update({"date_start": "2026-09-01 08:00:00", "date_stop": "2026-09-01 16:00:00"})
    else:
        work_entry.update(
            {"version_id": [contract_id, "Synthetic Contract 301"], "date": "2026-09-01"}
        )
    return {
        "hr.payslip": [payslip],
        "hr.payslip.run": [
            {
                "id": 111,
                "name": "Synthetic Run 111",
                "date_start": "2026-09-01",
                "date_end": "2026-09-30",
                "state": "verify" if version == 18 else "01_ready",
                "company_id": [1, "Synthetic Company 1"],
                "write_date": STAMP,
            }
        ],
        "hr.payslip.line": [
            {
                "id": 121,
                "slip_id": [101, "Synthetic Payslip 101"],
                "salary_rule_id": [901, "Synthetic Rule 901"],
                "employee_id": [501, "Synthetic Worker 501"],
                contract_field: [contract_id, "Synthetic Contract 301"],
                "name": "Synthetic Line 121",
                "code": "SYN_LINE",
                "category_id": [902, "Synthetic Category 902"],
                "sequence": 10,
                "quantity": "1.25",
                "rate": "100.5",
                "amount": "12.3456",
                "total": "15.432",
                "currency_id": [1, "SYN"],
                "write_date": STAMP,
            }
        ],
        "hr.payslip.worked_days": [
            {
                "id": 131,
                "payslip_id": [101, "Synthetic Payslip 101"],
                contract_field: [contract_id, "Synthetic Contract 301"],
                "work_entry_type_id": [801, "Synthetic Work Type 801"],
                "name": "Synthetic Worked Day 131",
                "code": "SYN_WORK",
                "number_of_days": "1.5",
                "number_of_hours": "12.25",
                "amount": "0",
                "currency_id": [1, "SYN"],
                "write_date": STAMP,
            }
        ],
        "hr.payslip.input": [
            {
                "id": 141,
                "name": "Synthetic Input 141",
                "payslip_id": [101, "Synthetic Payslip 101"],
                "sequence": 20,
                "input_type_id": [401, "Synthetic Input Type 401"],
                "code": "SYN_INPUT",
                "amount": "25.125",
                contract_field: [contract_id, "Synthetic Contract 301"],
                "write_date": STAMP,
            }
        ],
        "hr.payslip.input.type": [
            {
                "id": 401,
                "name": "Synthetic Input Type 401",
                "code": "SYN_INPUT",
                "struct_ids": [201],
                "active": True,
                "is_quantity": False,
                "available_in_attachments": False,
                "write_date": STAMP,
            }
        ],
        "hr.payroll.structure": [{"id": 201, "input_line_type_ids": [401], "write_date": STAMP}],
        "hr.employee": [
            {
                "id": 501,
                "name": "Synthetic Worker 501",
                "active": True,
                "company_id": [1, "Synthetic Company 1"],
                "write_date": STAMP,
            }
        ],
        "hr.contract" if version == 18 else "hr.version": [contract],
        "hr.work.entry": [work_entry],
    }


class FakePayrollTransport:
    name = "synthetic"

    def __init__(self, version: int, rows: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.version = version
        self.rows = rows if rows is not None else _rows(version)
        self.calls: list[dict[str, Any]] = []
        self.executions: list[dict[str, Any]] = []
        self.ignore_domains: set[str] = set()
        self.extra_field_model: str | None = None
        self.missing_field_model: str | None = None
        self.changed_anchor_model: str | None = None
        self.repeated_page_model: str | None = None
        self.short_page_model: str | None = None
        self.count_overrides: dict[str, int] = {}
        self.failure: OdooMcpError | None = None
        self.execution_failure: OdooMcpError | None = None
        self.post_execution_failure: OdooMcpError | None = None
        self.compute_result: object = True

    async def authenticate(self) -> None:
        return None

    async def probe_model(self, model: str, *, company_ids: tuple[int, ...]) -> bool:
        return model in self.rows

    def _related_model(self, model: str, field: str) -> str | None:
        if field in {"payslip_id", "slip_id"}:
            return "hr.payslip"
        return None

    def _value(self, model: str, row: dict[str, Any], field: str) -> object:
        head, separator, tail = field.partition(".")
        value = row.get(head)
        if not separator:
            return value
        related_model = self._related_model(model, head)
        if related_model is None or not isinstance(value, (list, tuple)) or not value:
            return None
        related_id = value[0]
        related = next(
            (
                candidate
                for candidate in self.rows.get(related_model, [])
                if candidate.get("id") == related_id
            ),
            None,
        )
        return self._value(related_model, related, tail) if related is not None else None

    @staticmethod
    def _scalar(value: object) -> object:
        if (
            isinstance(value, (list, tuple))
            and len(value) == 2
            and isinstance(value[0], int)
            and isinstance(value[1], str)
        ):
            return value[0]
        return value

    def _clause(self, model: str, row: dict[str, Any], clause: list[Any]) -> bool:
        field, operator, expected = clause
        raw = self._value(model, row, field)
        actual = self._scalar(raw)
        if operator == "=":
            if expected is False:
                return actual is False or actual is None
            return actual == expected
        if operator == "in":
            if isinstance(actual, list):
                return any(item in expected for item in actual)
            return actual in expected
        if operator == ">=":
            return actual is not None and actual is not False and actual >= expected
        if operator == "<=":
            return actual is not None and actual is not False and actual <= expected
        raise AssertionError(f"unexpected synthetic operator: {operator}")

    def _matches(self, model: str, row: dict[str, Any], domain: list[Any]) -> bool:
        if model in self.ignore_domains:
            return True
        index = 0
        while index < len(domain):
            term = domain[index]
            if term == "|":
                left = domain[index + 1]
                right = domain[index + 2]
                if not (self._clause(model, row, left) or self._clause(model, row, right)):
                    return False
                index += 3
                continue
            if not isinstance(term, list) or not self._clause(model, row, term):
                return False
            index += 1
        return True

    def _selected(self, model: str, domain: list[Any]) -> list[dict[str, Any]]:
        return [row for row in self.rows.get(model, []) if self._matches(model, row, domain)]

    async def search_count(
        self, model: str, domain: list[Any], *, company_ids: tuple[int, ...]
    ) -> int:
        if self.failure is not None:
            raise self.failure
        self.calls.append(
            {"kind": "count", "model": model, "domain": domain, "company_ids": company_ids}
        )
        return self.count_overrides.get(model, len(self._selected(model, domain)))

    async def search_read(
        self,
        model: str,
        domain: list[Any],
        fields: list[str],
        *,
        limit: int,
        offset: int = 0,
        order: str = "id",
        company_ids: tuple[int, ...],
    ) -> list[dict[str, Any]]:
        if self.failure is not None:
            raise self.failure
        self.calls.append(
            {
                "kind": "read",
                "model": model,
                "domain": domain,
                "fields": fields,
                "limit": limit,
                "offset": offset,
                "order": order,
                "company_ids": company_ids,
            }
        )
        selected = self._selected(model, domain)
        start = 0 if model == self.repeated_page_model and offset else offset
        projected = [
            {field: row[field] for field in fields if field in row}
            for row in selected[start : start + limit]
        ]
        if model == self.short_page_model and fields != ["id", "write_date"]:
            projected = []
        if projected and model == self.extra_field_model:
            projected[0]["synthetic_extra"] = "blocked"
        if projected and model == self.missing_field_model:
            projected[0].pop(fields[-1], None)
        if projected and fields == ["id", "write_date"] and model == self.changed_anchor_model:
            projected[0]["write_date"] = "2026-09-02 10:00:00"
        return projected

    async def execute_method(
        self,
        model: str,
        method: str,
        *,
        ids: tuple[int, ...] = (),
        positional: list[Any] | None = None,
        named: dict[str, Any] | None = None,
        company_ids: tuple[int, ...],
    ) -> Any:
        self.executions.append(
            {
                "model": model,
                "method": method,
                "ids": ids,
                "positional": positional,
                "named": named,
                "company_ids": company_ids,
            }
        )
        if self.execution_failure is not None:
            raise self.execution_failure
        if model == "hr.payslip.input" and method == "create":
            assert named is not None
            values = dict(named["vals_list"])
            payslip = self.rows["hr.payslip"][0]
            input_type = self.rows["hr.payslip.input.type"][0]
            contract_field = "contract_id" if self.version == 18 else "version_id"
            identifier = 902
            self.rows[model].append(
                {
                    "id": identifier,
                    "name": values["name"],
                    "payslip_id": [values["payslip_id"], payslip["name"]],
                    "sequence": 30,
                    "input_type_id": [values["input_type_id"], input_type["name"]],
                    "code": input_type["code"],
                    "amount": values["amount"],
                    contract_field: [values[contract_field], "Synthetic Contract 301"],
                    "write_date": "2026-09-02 10:00:00",
                }
            )
            self.failure = self.post_execution_failure
            return identifier
        if model == "hr.payslip.input" and method == "write":
            assert named is not None and len(ids) == 1
            row = next(item for item in self.rows[model] if item["id"] == ids[0])
            row.update(named["vals"])
            row["write_date"] = "2026-09-03 10:00:00"
            return True
        if model == "hr.payslip.input" and method == "unlink":
            self.rows[model] = [item for item in self.rows[model] if item["id"] not in ids]
            return True
        if model == "hr.payslip" and method == "compute_sheet":
            return self.compute_result
        raise AssertionError(f"unexpected synthetic execution: {model}.{method}")

    async def close(self) -> None:
        return None


def _reader(version: int, transport: FakePayrollTransport | None = None) -> PayrollReader:
    return PayrollReader(transport or FakePayrollTransport(version), version, lambda: (1, 2))


async def _write_checkpoint(
    reader: PayrollReader,
    version: int,
    *,
    input_type_id: int | None = None,
    include_calculation_sources: bool = False,
) -> PayrollWriteCheckpoint:
    payslip = (
        await reader.get_payslips(
            1,
            PayslipFilters(payslip_ids=(101,)),
            PayrollPageRequest(limit=1),
        )
    ).items[0]
    inputs = (
        await reader.get_payslip_inputs(
            1,
            PayslipChildFilters(payslip_ids=(101,)),
            PayrollPageRequest(limit=200),
        )
    ).items
    employee = (
        await reader.get_payroll_employees(
            1,
            (payslip.employee.id,),
            PayrollPageRequest(limit=1),
        )
    ).items[0]
    contracts = (
        await reader.get_payroll_contract_segments(
            1,
            PayrollContractFilters(
                employee_ids=(payslip.employee.id,),
                period=PayrollPeriod(start=payslip.date_from, end=payslip.date_to),
            ),
            PayrollPageRequest(limit=200),
        )
    ).items
    contract = next(
        item
        for item in contracts
        if item.id == payslip.contract_segment.id and item.source_model == payslip.contract_source
    )
    structure = await reader.get_payroll_structure(1, payslip.structure.id)
    input_types = (
        ()
        if input_type_id is None
        else tuple(
            (
                await reader.get_payroll_input_types(
                    1,
                    PayrollInputTypeFilters(
                        structure_id=payslip.structure.id,
                        input_type_ids=(input_type_id,),
                    ),
                    PayrollPageRequest(limit=1),
                )
            ).items
        )
    )
    calculated_lines = (
        tuple(
            (
                await reader.get_payslip_lines(
                    1,
                    PayslipChildFilters(payslip_ids=(101,)),
                    PayrollPageRequest(limit=200),
                )
            ).items
        )
        if include_calculation_sources
        else ()
    )
    worked_days = (
        tuple(
            (
                await reader.get_payslip_worked_days(
                    1,
                    PayslipChildFilters(payslip_ids=(101,)),
                    PayrollPageRequest(limit=200),
                )
            ).items
        )
        if include_calculation_sources
        else ()
    )
    return PayrollWriteCheckpoint(
        version=version,
        payslip=payslip,
        inputs=tuple(inputs),
        employee=employee,
        contract=contract,
        structure=structure,
        input_types=input_types,
        calculated_lines=calculated_lines,
        worked_days=worked_days,
    )


def test_payroll_source_constants_are_exact() -> None:
    common = {
        "hr.payslip.run": (
            "id",
            "name",
            "date_start",
            "date_end",
            "state",
            "company_id",
            "write_date",
        ),
        "hr.payslip.input.type": (
            "id",
            "name",
            "code",
            "struct_ids",
            "active",
            "is_quantity",
            "available_in_attachments",
            "write_date",
        ),
        "hr.payroll.structure": ("id", "input_line_type_ids", "write_date"),
        "hr.employee": ("id", "name", "active", "company_id", "write_date"),
    }
    expected_18 = {
        **common,
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
    }
    expected_19 = {
        **common,
        "hr.payslip": tuple(
            "version_id" if field == "contract_id" else field
            for field in expected_18["hr.payslip"]
            if field != "number"
        ),
        "hr.payslip.line": tuple(
            "version_id" if field == "contract_id" else field
            for field in expected_18["hr.payslip.line"]
        ),
        "hr.payslip.worked_days": tuple(
            "version_id" if field == "contract_id" else field
            for field in expected_18["hr.payslip.worked_days"]
        ),
        "hr.payslip.input": tuple(
            "version_id" if field == "contract_id" else field
            for field in expected_18["hr.payslip.input"]
        ),
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
    }

    assert PAYROLL_MODEL_FIELDS_BY_VERSION == {18: expected_18, 19: expected_19}
    assert PAYROLL_SOURCE_CAPS == {
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
    assert PAYSLIP_STATE_MAP == {
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
    assert BATCH_STATE_MAP == {
        18: {"draft": "draft", "verify": "ready", "close": "done", "paid": "paid"},
        19: {
            "01_ready": "ready",
            "02_close": "done",
            "03_paid": "paid",
            "04_cancel": "cancelled",
        },
    }
    assert WORK_ENTRY_STATE_MAP == {
        "draft": "draft",
        "conflict": "conflict",
        "validated": "validated",
        "cancelled": "cancelled",
    }


def test_payroll_input_models_enforce_bounds_and_unknown_fields() -> None:
    PayrollPeriod(start=date(2024, 1, 1), end=date(2024, 12, 31))
    PayrollDiscoveryWindow(start=date(2020, 2, 29), end=date(2025, 2, 28))
    trimmed = DraftPayslipInputCreate(
        payslip_id=1,
        input_type_id=2,
        description=f"  {'x' * 200}  ",
        amount=Decimal("-1000000000000.000000"),
    )
    assert trimmed.description == "x" * 200

    invalid_payloads = (
        lambda: PayrollPeriod(start=date(2024, 1, 1), end=date(2025, 1, 1)),
        lambda: PayrollDiscoveryWindow(start=date(2020, 2, 29), end=date(2025, 3, 1)),
        lambda: PayrollPageRequest(limit=201),
        lambda: PayrollPageRequest(limit=True),
        lambda: PayslipFilters(payslip_ids=(True,)),
        lambda: PayslipChildFilters(payslip_ids=()),
        lambda: DraftPayslipInputCreate(
            payslip_id=1,
            input_type_id=2,
            description="synthetic",
            amount=Decimal("0.0000001"),
        ),
        lambda: DraftPayslipInputUpdate(payslip_id=1),
        lambda: DraftPayslipInputCreate(
            payslip_id=1,
            input_type_id=2,
            description="x" * 201,
            amount=Decimal("1"),
        ),
        lambda: DraftPayslipInputCreate(
            payslip_id=True,
            input_type_id=2,
            description="synthetic",
            amount=Decimal("1"),
        ),
        lambda: PayslipFilters(payslip_ids=(1,), synthetic_extra=True),
    )
    for build in invalid_payloads:
        with pytest.raises(ValidationError):
            build()


@pytest.mark.parametrize("version", [18, 19])
async def test_all_payroll_read_primitives_normalize_versioned_sources(version: int) -> None:
    transport = FakePayrollTransport(version)
    reader = _reader(version, transport)
    period = PayrollPeriod(start=date(2026, 9, 1), end=date(2026, 9, 30))

    batches = await reader.get_payroll_batches(
        1,
        PayrollBatchFilters(
            window=PayrollDiscoveryWindow(start=date(2026, 1, 1), end=date(2026, 12, 31)),
            states=("ready",),
        ),
    )
    payslips = await reader.get_payslips(1, PayslipFilters(payslip_ids=(101,)))
    lines = await reader.get_payslip_lines(1, PayslipChildFilters(payslip_ids=(101,)))
    worked = await reader.get_payslip_worked_days(1, PayslipChildFilters(payslip_ids=(101,)))
    inputs = await reader.get_payslip_inputs(1, PayslipChildFilters(payslip_ids=(101,)))
    input_types = await reader.get_payroll_input_types(1, PayrollInputTypeFilters(structure_id=201))
    employees = await reader.get_payroll_employees(1, (501,))
    contracts = await reader.get_payroll_contract_segments(
        1, PayrollContractFilters(employee_ids=(501,), period=period)
    )
    work_entries = await reader.get_payroll_work_entries(
        1,
        PayrollWorkEntryFilters(employee_ids=(501,), period=period, states=("validated",)),
    )

    assert batches.items[0].state == "ready"
    assert payslips.items[0].reference == (
        "SYN/2026/101" if version == 18 else "Synthetic Payslip 101"
    )
    assert payslips.items[0].contract_source == ("hr.contract" if version == 18 else "hr.version")
    assert lines.items[0].amount == Decimal("12.3456")
    assert worked.items[0].number_of_hours == Decimal("12.25")
    assert inputs.items[0].amount == Decimal("25.125")
    assert input_types.items[0].structure_ids == (201,)
    assert employees.items[0].company_id == 1
    assert contracts.items[0].effective_start == date(2026, 9, 1)
    assert contracts.items[0].effective_end == date(2026, 9, 30)
    assert work_entries.items[0].duration == Decimal("8.125")
    assert all(call["company_ids"] == (1,) for call in transport.calls)
    full_reads = [
        call
        for call in transport.calls
        if call["kind"] == "read" and call["fields"] != ["id", "write_date"]
    ]
    assert all(
        tuple(call["fields"]) == PAYROLL_MODEL_FIELDS_BY_VERSION[version][call["model"]]
        for call in full_reads
    )
    payslip_read = next(call for call in full_reads if call["model"] == "hr.payslip")
    assert payslip_read["domain"] == [["company_id", "=", 1], ["id", "in", [101]]]
    assert payslip_read["order"] == "date_from desc, date_to desc, id desc"


@pytest.mark.parametrize("version", [18, 19])
async def test_schema_qualification_uses_only_fixed_impossible_id_reads(
    version: int,
) -> None:
    transport = FakePayrollTransport(version)

    await _reader(version, transport).verify_schema(1)

    full_reads = [
        call
        for call in transport.calls
        if call["kind"] == "read" and call["fields"] != ["id", "write_date"]
    ]
    assert [call["model"] for call in full_reads] == list(PAYROLL_MODEL_FIELDS_BY_VERSION[version])
    assert all(call["domain"] == [["id", "=", -1]] for call in full_reads)
    assert all(call["limit"] == 1 and call["company_ids"] == (1,) for call in full_reads)
    assert transport.executions == []


@pytest.mark.parametrize("version", [18, 19])
async def test_draft_input_actions_and_recalculation_use_only_fixed_methods(
    version: int,
) -> None:
    rows = _rows(version)
    rows["hr.payslip.input"] = []
    transport = FakePayrollTransport(version, rows)
    reader = _reader(version, transport)

    create_checkpoint = await _write_checkpoint(reader, version, input_type_id=401)
    created = await reader.create_draft_payslip_input(
        1,
        DraftPayslipInputCreate(
            payslip_id=101,
            input_type_id=401,
            description="Synthetic Adjustment",
            amount=Decimal("40.125"),
        ),
        create_checkpoint,
    )
    update_checkpoint = await _write_checkpoint(reader, version, input_type_id=401)
    updated = await reader.update_draft_payslip_input(
        1,
        created.id,
        DraftPayslipInputUpdate(
            payslip_id=101,
            description="Synthetic Updated Adjustment",
            amount=Decimal("41.125"),
        ),
        update_checkpoint,
    )
    delete_checkpoint = await _write_checkpoint(reader, version, input_type_id=401)
    deleted = await reader.delete_draft_payslip_input(
        1,
        101,
        created.id,
        delete_checkpoint,
    )
    recalculation_checkpoint = await _write_checkpoint(
        reader,
        version,
        include_calculation_sources=True,
    )
    recalculated = await reader.recompute_draft_payslip(
        1,
        101,
        recalculation_checkpoint,
    )

    contract_key = "contract_id" if version == 18 else "version_id"
    assert updated.name == "Synthetic Updated Adjustment"
    assert updated.amount == Decimal("41.125")
    assert deleted.id == created.id
    assert deleted.payslip_id == 101
    assert recalculated.id == 101
    assert transport.executions == [
        {
            "model": "hr.payslip.input",
            "method": "create",
            "ids": (),
            "positional": None,
            "named": {
                "vals_list": {
                    "name": "Synthetic Adjustment",
                    "payslip_id": 101,
                    "input_type_id": 401,
                    "amount": "40.125",
                    contract_key: 301,
                }
            },
            "company_ids": (1,),
        },
        {
            "model": "hr.payslip.input",
            "method": "write",
            "ids": (902,),
            "positional": None,
            "named": {
                "vals": {
                    "name": "Synthetic Updated Adjustment",
                    "amount": "41.125",
                }
            },
            "company_ids": (1,),
        },
        {
            "model": "hr.payslip.input",
            "method": "unlink",
            "ids": (902,),
            "positional": None,
            "named": None,
            "company_ids": (1,),
        },
        {
            "model": "hr.payslip",
            "method": "compute_sheet",
            "ids": (101,),
            "positional": None,
            "named": None,
            "company_ids": (1,),
        },
    ]


@pytest.mark.parametrize(("version", "source_state"), [(18, "done"), (19, "validated")])
async def test_noneditable_payslip_fails_before_mutation(version: int, source_state: str) -> None:
    transport = FakePayrollTransport(version)
    reader = _reader(version, transport)
    checkpoint = await _write_checkpoint(
        reader,
        version,
        include_calculation_sources=True,
    )
    transport.rows["hr.payslip"][0]["state"] = source_state

    with pytest.raises(PayrollWriteRejected) as caught:
        await reader.recompute_draft_payslip(1, 101, checkpoint)

    assert caught.value.error.code is ErrorCode.ODOO_STATE_CONFLICT
    assert transport.executions == []


async def test_false_recalculation_result_has_payroll_error() -> None:
    transport = FakePayrollTransport(19)
    transport.compute_result = False
    reader = _reader(19, transport)
    checkpoint = await _write_checkpoint(
        reader,
        19,
        include_calculation_sources=True,
    )

    with pytest.raises(PayrollWriteRejected) as caught:
        await reader.recompute_draft_payslip(1, 101, checkpoint)

    assert caught.value.error.code is ErrorCode.PAYROLL_RECALCULATION_FAILED


async def test_authoritative_write_acl_denial_is_distinct_from_post_write_acl_failure() -> None:
    denial = OdooMcpError(
        ErrorCode.ODOO_PERMISSION_DENIED,
        "Synthetic permission denial.",
        "Use synthetic access.",
    )
    before_dispatch = FakePayrollTransport(19)
    before_dispatch.rows["hr.payslip.input"] = []
    before_dispatch.execution_failure = denial
    before_reader = _reader(19, before_dispatch)
    before_checkpoint = await _write_checkpoint(before_reader, 19, input_type_id=401)
    with pytest.raises(PayrollWriteRejected) as rejected:
        await before_reader.create_draft_payslip_input(
            1,
            DraftPayslipInputCreate(
                payslip_id=101,
                input_type_id=401,
                description="Synthetic Adjustment",
                amount=Decimal("1"),
            ),
            before_checkpoint,
        )
    assert rejected.value.error.code is ErrorCode.ODOO_PERMISSION_DENIED
    assert before_dispatch.rows["hr.payslip.input"] == []

    after_dispatch = FakePayrollTransport(19)
    after_dispatch.rows["hr.payslip.input"] = []
    after_dispatch.post_execution_failure = denial
    after_reader = _reader(19, after_dispatch)
    after_checkpoint = await _write_checkpoint(after_reader, 19, input_type_id=401)
    with pytest.raises(OdooMcpError) as uncertain:
        await after_reader.create_draft_payslip_input(
            1,
            DraftPayslipInputCreate(
                payslip_id=101,
                input_type_id=401,
                description="Synthetic Adjustment",
                amount=Decimal("1"),
            ),
            after_checkpoint,
        )
    assert uncertain.value.code is ErrorCode.ODOO_PERMISSION_DENIED
    assert len(after_dispatch.rows["hr.payslip.input"]) == 1


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("extra", ErrorCode.ODOO_API_ERROR),
        ("missing", ErrorCode.ODOO_API_ERROR),
        ("anchor", ErrorCode.ODOO_STATE_CONFLICT),
        ("company", ErrorCode.PAYROLL_SOURCE_INCONSISTENT),
    ],
)
async def test_malformed_changed_and_cross_company_sources_fail_closed(
    mutation: str, expected: ErrorCode
) -> None:
    transport = FakePayrollTransport(19)
    if mutation == "extra":
        transport.extra_field_model = "hr.payslip"
    elif mutation == "missing":
        transport.missing_field_model = "hr.payslip"
    elif mutation == "anchor":
        transport.changed_anchor_model = "hr.payslip"
    else:
        transport.rows["hr.payslip"][0]["company_id"] = [2, "Synthetic Company 2"]
        transport.ignore_domains.add("hr.payslip")

    with pytest.raises(OdooMcpError) as caught:
        await _reader(19, transport).get_payslips(1, PayslipFilters(payslip_ids=(101,)))

    assert caught.value.code is expected


async def test_child_with_substituted_parent_fails_relationship_validation() -> None:
    transport = FakePayrollTransport(19)
    transport.rows["hr.payslip.line"][0]["slip_id"] = [999, "Synthetic Payslip 999"]
    transport.ignore_domains.add("hr.payslip.line")

    with pytest.raises(OdooMcpError) as caught:
        await _reader(19, transport).get_payslip_lines(1, PayslipChildFilters(payslip_ids=(101,)))

    assert caught.value.code is ErrorCode.PAYROLL_SOURCE_INCONSISTENT


async def test_exact_payslip_id_cannot_be_substituted() -> None:
    transport = FakePayrollTransport(19)
    transport.rows["hr.payslip"][0]["id"] = 102
    transport.ignore_domains.add("hr.payslip")

    with pytest.raises(OdooMcpError) as caught:
        await _reader(19, transport).get_payslips(1, PayslipFilters(payslip_ids=(101,)))

    assert caught.value.code is ErrorCode.PAYROLL_SOURCE_INCONSISTENT


async def test_child_with_mismatched_employee_contract_or_currency_fails() -> None:
    transport = FakePayrollTransport(19)
    transport.rows["hr.payslip.line"][0]["employee_id"] = [999, "Synthetic Worker 999"]

    with pytest.raises(OdooMcpError) as caught:
        await _reader(19, transport).get_payslip_lines(1, PayslipChildFilters(payslip_ids=(101,)))

    assert caught.value.code is ErrorCode.PAYROLL_SOURCE_INCONSISTENT


async def test_repeated_source_page_is_rejected() -> None:
    rows = _rows(19)
    rows["hr.payslip.run"] = [
        {
            "id": identifier,
            "name": f"Synthetic Run {identifier}",
            "date_start": "2026-01-01",
            "date_end": "2026-01-31",
            "state": "01_ready",
            "company_id": [1, "Synthetic Company 1"],
            "write_date": STAMP,
        }
        for identifier in range(1, 502)
    ]
    transport = FakePayrollTransport(19, rows)
    transport.repeated_page_model = "hr.payslip.run"

    with pytest.raises(OdooMcpError) as caught:
        await _reader(19, transport).get_payroll_batches(
            1,
            PayrollBatchFilters(
                window=PayrollDiscoveryWindow(start=date(2026, 1, 1), end=date(2026, 12, 31))
            ),
        )

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


async def test_incomplete_source_page_is_rejected() -> None:
    transport = FakePayrollTransport(19)
    transport.short_page_model = "hr.payslip"

    with pytest.raises(OdooMcpError) as caught:
        await _reader(19, transport).get_payslips(1, PayslipFilters(payslip_ids=(101,)))

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


async def test_input_change_during_prewrite_validation_blocks_mutation() -> None:
    class InputRaceTransport(FakePayrollTransport):
        def __init__(self) -> None:
            super().__init__(19)
            self.input_count_calls = 0
            self.race_enabled = False

        async def search_count(
            self,
            model: str,
            domain: list[Any],
            *,
            company_ids: tuple[int, ...],
        ) -> int:
            if model == "hr.payslip.input":
                self.input_count_calls += 1
                if self.race_enabled and self.input_count_calls == 3:
                    self.rows[model][0]["amount"] = "26.125"
                    self.rows[model][0]["write_date"] = "2026-09-02 10:00:00"
            return await super().search_count(model, domain, company_ids=company_ids)

    transport = InputRaceTransport()
    reader = _reader(19, transport)
    checkpoint = await _write_checkpoint(reader, 19, input_type_id=401)
    transport.input_count_calls = 0
    transport.race_enabled = True

    with pytest.raises(PayrollWriteRejected) as caught:
        await reader.update_draft_payslip_input(
            1,
            141,
            DraftPayslipInputUpdate(payslip_id=101, amount=Decimal("30.125")),
            checkpoint,
        )

    assert caught.value.error.code is ErrorCode.ODOO_STATE_CONFLICT
    assert transport.executions == []


@pytest.mark.parametrize("version", [18, 19])
async def test_expected_checkpoint_blocks_added_reparented_and_calculation_source_races(
    version: int,
) -> None:
    create_rows = _rows(version)
    intervening_input = create_rows["hr.payslip.input"][0]
    create_rows["hr.payslip.input"] = []
    create_transport = FakePayrollTransport(version, create_rows)
    create_reader = _reader(version, create_transport)
    create_checkpoint = await _write_checkpoint(create_reader, version, input_type_id=401)
    create_transport.rows["hr.payslip.input"].append(intervening_input)

    with pytest.raises(PayrollWriteRejected) as create_conflict:
        await create_reader.create_draft_payslip_input(
            1,
            DraftPayslipInputCreate(
                payslip_id=101,
                input_type_id=401,
                description="Synthetic Adjustment",
                amount=Decimal("1"),
            ),
            create_checkpoint,
        )

    remove_transport = FakePayrollTransport(version)
    remove_reader = _reader(version, remove_transport)
    remove_checkpoint = await _write_checkpoint(remove_reader, version, input_type_id=401)
    remove_transport.rows["hr.payslip.input"][0]["payslip_id"] = [
        102,
        "Synthetic Payslip 102",
    ]

    with pytest.raises(PayrollWriteRejected) as remove_conflict:
        await remove_reader.delete_draft_payslip_input(
            1,
            101,
            141,
            remove_checkpoint,
        )

    recalculate_transport = FakePayrollTransport(version)
    recalculate_reader = _reader(version, recalculate_transport)
    recalculate_checkpoint = await _write_checkpoint(
        recalculate_reader,
        version,
        include_calculation_sources=True,
    )
    recalculate_transport.rows["hr.payslip.line"][0]["amount"] = "99"
    recalculate_transport.rows["hr.payslip.line"][0]["write_date"] = "2026-09-02 10:00:00"

    with pytest.raises(PayrollWriteRejected) as recalculate_conflict:
        await recalculate_reader.recompute_draft_payslip(
            1,
            101,
            recalculate_checkpoint,
        )

    assert create_conflict.value.error.code is ErrorCode.ODOO_STATE_CONFLICT
    assert remove_conflict.value.error.code is ErrorCode.ODOO_STATE_CONFLICT
    assert recalculate_conflict.value.error.code is ErrorCode.ODOO_STATE_CONFLICT
    assert create_transport.executions == []
    assert remove_transport.executions == []
    assert recalculate_transport.executions == []


async def test_source_cap_is_enforced_before_reading_rows() -> None:
    transport = FakePayrollTransport(19)
    transport.count_overrides["hr.payslip.run"] = 5_001

    with pytest.raises(OdooMcpError) as caught:
        await _reader(19, transport).get_payroll_batches(1, PayrollBatchFilters(batch_ids=(111,)))

    assert caught.value.code is ErrorCode.PAYROLL_RESULT_TOO_LARGE
    assert not any(call["kind"] == "read" for call in transport.calls)


async def test_cursor_is_bound_to_the_exact_request() -> None:
    rows = _rows(19)
    second = dict(rows["hr.payslip.run"][0])
    second["id"] = 112
    second["name"] = "Synthetic Run 112"
    rows["hr.payslip.run"].append(second)
    reader = _reader(19, FakePayrollTransport(19, rows))
    first = await reader.get_payroll_batches(
        1,
        PayrollBatchFilters(
            window=PayrollDiscoveryWindow(start=date(2026, 1, 1), end=date(2026, 12, 31))
        ),
        PayrollPageRequest(limit=1),
    )
    assert first.next_cursor is not None

    with pytest.raises(OdooMcpError) as caught:
        await reader.get_payroll_batches(
            1,
            PayrollBatchFilters(batch_ids=(111, 112)),
            PayrollPageRequest(limit=1, cursor=first.next_cursor),
        )

    assert caught.value.code is ErrorCode.INVALID_INPUT


async def test_cursor_is_bound_to_company_and_source_anchors() -> None:
    rows = _rows(19)
    second_type = dict(rows["hr.payslip.input.type"][0])
    second_type.update(
        {
            "id": 402,
            "name": "Synthetic Input Type 402",
            "code": "SYN_INPUT_2",
        }
    )
    rows["hr.payslip.input.type"].append(second_type)
    rows["hr.payroll.structure"][0]["input_line_type_ids"] = [401, 402]
    reader = _reader(19, FakePayrollTransport(19, rows))
    first = await reader.get_payroll_input_types(
        1,
        PayrollInputTypeFilters(structure_id=201),
        PayrollPageRequest(limit=1),
    )
    assert first.next_cursor is not None

    with pytest.raises(OdooMcpError) as wrong_company:
        await reader.get_payroll_input_types(
            2,
            PayrollInputTypeFilters(structure_id=201),
            PayrollPageRequest(limit=1, cursor=first.next_cursor),
        )
    assert wrong_company.value.code is ErrorCode.INVALID_INPUT

    rows["hr.payslip.input.type"][1]["write_date"] = "2026-09-02 10:00:00"
    with pytest.raises(OdooMcpError) as changed_source:
        await reader.get_payroll_input_types(
            1,
            PayrollInputTypeFilters(structure_id=201),
            PayrollPageRequest(limit=1, cursor=first.next_cursor),
        )
    assert changed_source.value.code is ErrorCode.INVALID_INPUT


async def test_odoo19_contract_cursor_binds_period_start_not_only_odoo_domain() -> None:
    rows = _rows(19)
    rows["hr.version"][0]["contract_date_end"] = False
    second = dict(rows["hr.version"][0])
    second.update(
        {
            "id": 302,
            "date_version": "2026-09-16",
            "write_date": "2026-09-02 10:00:00",
        }
    )
    rows["hr.version"].append(second)
    reader = _reader(19, FakePayrollTransport(19, rows))
    first_filter = PayrollContractFilters(
        employee_ids=(501,),
        period=PayrollPeriod(start=date(2026, 9, 1), end=date(2026, 9, 30)),
    )
    first = await reader.get_payroll_contract_segments(1, first_filter, PayrollPageRequest(limit=1))
    assert first.next_cursor is not None

    with pytest.raises(OdooMcpError) as caught:
        await reader.get_payroll_contract_segments(
            1,
            PayrollContractFilters(
                employee_ids=(501,),
                period=PayrollPeriod(start=date(2026, 9, 2), end=date(2026, 9, 30)),
            ),
            PayrollPageRequest(limit=1, cursor=first.next_cursor),
        )

    assert caught.value.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "failure",
    [
        OdooMcpError(
            ErrorCode.ODOO_PERMISSION_DENIED,
            "Synthetic permission denial.",
            "Use synthetic access.",
        ),
        OdooMcpError(
            ErrorCode.ODOO_API_ERROR,
            "Synthetic timeout.",
            "Retry the synthetic request.",
        ),
    ],
)
async def test_transport_failures_retain_structured_safe_errors(
    failure: OdooMcpError,
) -> None:
    transport = FakePayrollTransport(19)
    transport.failure = failure

    with pytest.raises(OdooMcpError) as caught:
        await _reader(19, transport).get_payslips(1, PayslipFilters(payslip_ids=(101,)))

    assert caught.value.code is failure.code
    assert "secret" not in caught.value.safe_message.casefold()


async def test_wrong_version_source_shape_is_rejected() -> None:
    transport = FakePayrollTransport(19)
    payslip = transport.rows["hr.payslip"][0]
    payslip["contract_id"] = payslip.pop("version_id")

    with pytest.raises(OdooMcpError) as caught:
        await _reader(19, transport).get_payslips(1, PayslipFilters(payslip_ids=(101,)))

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


@pytest.mark.parametrize(
    ("field", "value"),
    [("state", "synthetic_unknown"), ("credit_note", "not-a-boolean")],
)
async def test_unknown_state_and_malformed_fixed_values_are_rejected(
    field: str, value: object
) -> None:
    transport = FakePayrollTransport(19)
    transport.rows["hr.payslip"][0][field] = value

    with pytest.raises(OdooMcpError) as caught:
        await _reader(19, transport).get_payslips(1, PayslipFilters(payslip_ids=(101,)))

    assert caught.value.code is ErrorCode.ODOO_API_ERROR


async def test_unauthorized_company_fails_before_transport_access() -> None:
    transport = FakePayrollTransport(19)

    with pytest.raises(OdooMcpError) as caught:
        await _reader(19, transport).get_payslips(3, PayslipFilters(payslip_ids=(101,)))

    assert caught.value.code is ErrorCode.COMPANY_NOT_FOUND
    assert transport.calls == []


async def test_boolean_direct_action_identifier_is_rejected_before_reads() -> None:
    transport = FakePayrollTransport(19)
    reader = _reader(19, transport)
    checkpoint = await _write_checkpoint(reader, 19, input_type_id=401)
    transport.calls.clear()

    with pytest.raises(OdooMcpError) as caught:
        await reader.delete_draft_payslip_input(1, 101, True, checkpoint)

    assert caught.value.code is ErrorCode.INVALID_INPUT
    assert transport.calls == []


async def test_odoo19_contract_revisions_use_inclusive_effective_intervals() -> None:
    rows = _rows(19)
    first = rows["hr.version"][0]
    first["contract_date_end"] = False
    second = dict(first)
    second.update(
        {
            "id": 302,
            "date_version": "2026-09-16",
            "wage": "1100.125",
            "write_date": "2026-09-02 10:00:00",
        }
    )
    rows["hr.version"].append(second)
    period = PayrollPeriod(start=date(2026, 9, 1), end=date(2026, 9, 30))

    result = await _reader(19, FakePayrollTransport(19, rows)).get_payroll_contract_segments(
        1, PayrollContractFilters(employee_ids=(501,), period=period)
    )

    assert [(item.id, item.effective_start, item.effective_end) for item in result.items] == [
        (301, date(2026, 9, 1), date(2026, 9, 15)),
        (302, date(2026, 9, 16), date(2026, 9, 30)),
    ]


async def test_duplicate_odoo19_revision_dates_fail_closed() -> None:
    rows = _rows(19)
    duplicate = dict(rows["hr.version"][0])
    duplicate["id"] = 302
    rows["hr.version"].append(duplicate)

    with pytest.raises(OdooMcpError) as caught:
        await _reader(19, FakePayrollTransport(19, rows)).get_payroll_contract_segments(
            1,
            PayrollContractFilters(
                employee_ids=(501,),
                period=PayrollPeriod(start=date(2026, 9, 1), end=date(2026, 9, 30)),
            ),
        )

    assert caught.value.code is ErrorCode.PAYROLL_SOURCE_INCONSISTENT
