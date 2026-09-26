"""Controlled draft-only Payroll write workflows."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeVar

from odoo_mcp.adapters.base import OdooAdapter
from odoo_mcp.adapters.payroll import (
    DraftPayslipInputCreate,
    DraftPayslipInputUpdate,
    PayrollContractFilters,
    PayrollContractSegment,
    PayrollEmployee,
    PayrollInputType,
    PayrollInputTypeFilters,
    PayrollPage,
    PayrollPageRequest,
    PayrollPeriod,
    PayrollStructure,
    PayrollValue,
    PayrollWriteCheckpoint,
    Payslip,
    PayslipChildFilters,
    PayslipFilters,
    PayslipInput,
    PayslipLine,
    PayslipWorkedDay,
)
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.payroll_schemas import GetPayslipInput
from odoo_mcp.mcp.payroll_write_schemas import (
    RecalculateDraftPayslip,
    RemoveDraftPayrollInput,
    SetDraftPayrollInput,
)
from odoo_mcp.policy.write_safety import AppliedWrite, PreparedWrite
from odoo_mcp.workflows.payroll.evidence import get_payslip

PayrollWriteOperation = Literal["create", "update", "remove", "recalculate"]
ItemT = TypeVar("ItemT", bound=PayrollValue)

_PAGE_SIZE = 200
_MAX_INPUTS = 200
_MAX_INPUT_TYPES_FOR_SELECTION = 200
_MAX_CONTRACT_SEGMENTS = 200
_MAX_CALCULATED_LINES = 500
_MAX_WORKED_DAYS = 200


def _error(code: ErrorCode, message: str, hint: str) -> OdooMcpError:
    return OdooMcpError(code, message, hint)


def _invalid_source() -> OdooMcpError:
    return _error(
        ErrorCode.PAYROLL_SOURCE_INCONSISTENT,
        "Odoo returned inconsistent Payroll source data.",
        "Check the payslip relationships and Payroll configuration, then retry.",
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
        "Reduce the payslip detail in Odoo before retrying.",
    )


def _state_conflict() -> OdooMcpError:
    return _error(
        ErrorCode.ODOO_STATE_CONFLICT,
        "The draft payslip changed after the write was prepared.",
        "Refresh the preview and retry with a new idempotency key.",
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
            or len(page.items) > _PAGE_SIZE
        ):
            raise _malformed_source()
        if expected_total is None:
            expected_total = page.total_count
            if expected_total > cap:
                raise _too_large()
        elif page.total_count != expected_total:
            raise _malformed_source()
        if page.next_cursor is not None and (not page.items or len(page.next_cursor) > 128):
            raise _malformed_source()
        for item in page.items:
            identifier = getattr(item, "id", None)
            if (
                not isinstance(item, item_type)
                or not isinstance(identifier, int)
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


async def _exact_payslip(adapter: OdooAdapter, company_id: int, payslip_id: int) -> Payslip:
    filters = PayslipFilters(payslip_ids=(payslip_id,))

    async def fetch(page: PayrollPageRequest) -> PayrollPage[Payslip]:
        return await adapter.get_payslips(company_id, filters, page)

    items = await _collect_pages(fetch, Payslip, cap=1)
    if not items:
        raise _error(
            ErrorCode.PAYSLIP_NOT_FOUND,
            "The requested payslip was not found.",
            "Use an exact payslip ID from the authorized company.",
        )
    if len(items) != 1 or items[0].id != payslip_id or items[0].company_id != company_id:
        raise _invalid_source()
    return items[0]


def _require_editable(payslip: Payslip, version: int) -> None:
    editable = (
        payslip.source_state in {"draft", "verify"}
        if version == 18
        else payslip.source_state == "draft"
        if version == 19
        else False
    )
    if not editable:
        raise _error(
            ErrorCode.PAYROLL_STATE_NOT_EDITABLE,
            "The requested payslip is not editable.",
            "Use an Odoo draft payslip and retry.",
        )


async def _all_inputs(adapter: OdooAdapter, company_id: int, payslip_id: int) -> list[PayslipInput]:
    filters = PayslipChildFilters(payslip_ids=(payslip_id,))

    async def fetch(page: PayrollPageRequest) -> PayrollPage[PayslipInput]:
        return await adapter.get_payslip_inputs(company_id, filters, page)

    inputs = await _collect_pages(fetch, PayslipInput, cap=_MAX_INPUTS)
    type_ids: set[int] = set()
    codes: set[str] = set()
    for item in inputs:
        if item.payslip.id != payslip_id or item.input_type.id in type_ids or item.code in codes:
            raise _invalid_source()
        type_ids.add(item.input_type.id)
        codes.add(item.code)
    inputs.sort(key=lambda item: (item.sequence, item.id))
    return inputs


async def _exact_employee(
    adapter: OdooAdapter, company_id: int, payslip: Payslip
) -> PayrollEmployee:
    async def fetch(page: PayrollPageRequest) -> PayrollPage[PayrollEmployee]:
        return await adapter.get_payroll_employees(company_id, (payslip.employee.id,), page)

    items = await _collect_pages(fetch, PayrollEmployee, cap=1)
    if (
        len(items) != 1
        or items[0].id != payslip.employee.id
        or items[0].company_id != company_id
        or items[0].name != payslip.employee.name
    ):
        raise _invalid_source()
    return items[0]


async def _exact_contract(
    adapter: OdooAdapter, company_id: int, payslip: Payslip
) -> PayrollContractSegment:
    filters = PayrollContractFilters(
        employee_ids=(payslip.employee.id,),
        period=PayrollPeriod(start=payslip.date_from, end=payslip.date_to),
    )

    async def fetch(page: PayrollPageRequest) -> PayrollPage[PayrollContractSegment]:
        return await adapter.get_payroll_contract_segments(company_id, filters, page)

    segments = await _collect_pages(
        fetch,
        PayrollContractSegment,
        cap=_MAX_CONTRACT_SEGMENTS,
    )
    matches = [
        item
        for item in segments
        if item.id == payslip.contract_segment.id and item.source_model == payslip.contract_source
    ]
    if (
        len(matches) != 1
        or matches[0].employee.id != payslip.employee.id
        or matches[0].company_id != company_id
        or matches[0].currency.id != payslip.currency.id
    ):
        raise _invalid_source()
    return matches[0]


async def _eligible_type(
    adapter: OdooAdapter,
    company_id: int,
    structure: PayrollStructure,
    input_type_id: int,
) -> PayrollInputType:
    filters = PayrollInputTypeFilters(
        structure_id=structure.id,
        input_type_ids=(input_type_id,),
    )

    async def fetch(page: PayrollPageRequest) -> PayrollPage[PayrollInputType]:
        return await adapter.get_payroll_input_types(company_id, filters, page)

    items = await _collect_pages(fetch, PayrollInputType, cap=1)
    if not items:
        raise _error(
            ErrorCode.PAYROLL_INPUT_TYPE_NOT_ALLOWED,
            "The requested Payroll input type is not eligible.",
            "Use an active non-attachment type allowed by the payslip structure.",
        )
    selected = items[0]
    if (
        selected.id != input_type_id
        or selected.id not in structure.input_line_type_ids
        or structure.id not in selected.structure_ids
        or not selected.active
        or selected.available_in_attachments
    ):
        raise _invalid_source()
    return selected


async def _eligible_types_for_selection(
    adapter: OdooAdapter,
    company_id: int,
    structure: PayrollStructure,
) -> tuple[list[PayrollInputType], bool]:
    page = await adapter.get_payroll_input_types(
        company_id,
        PayrollInputTypeFilters(structure_id=structure.id),
        PayrollPageRequest(limit=_MAX_INPUT_TYPES_FOR_SELECTION),
    )
    if (
        not isinstance(page, PayrollPage)
        or not isinstance(page.items, list)
        or not isinstance(page.total_count, int)
        or isinstance(page.total_count, bool)
        or page.total_count < len(page.items)
        or len(page.items) > _MAX_INPUT_TYPES_FOR_SELECTION
        or (page.next_cursor is None) != (page.total_count <= len(page.items))
        or (page.next_cursor is not None and len(page.next_cursor) > 128)
    ):
        raise _malformed_source()
    result: list[PayrollInputType] = []
    seen_ids: set[int] = set()
    seen_codes: set[str] = set()
    for item in page.items:
        if (
            not isinstance(item, PayrollInputType)
            or item.id in seen_ids
            or item.code in seen_codes
            or item.id not in structure.input_line_type_ids
            or structure.id not in item.structure_ids
            or not item.active
            or item.available_in_attachments
        ):
            raise _invalid_source()
        seen_ids.add(item.id)
        seen_codes.add(item.code)
        result.append(item)
    result.sort(key=lambda item: (item.code, item.id))
    return result, page.total_count > len(result)


async def _all_lines(adapter: OdooAdapter, company_id: int, payslip_id: int) -> list[PayslipLine]:
    filters = PayslipChildFilters(payslip_ids=(payslip_id,))

    async def fetch(page: PayrollPageRequest) -> PayrollPage[PayslipLine]:
        return await adapter.get_payslip_lines(company_id, filters, page)

    lines = await _collect_pages(fetch, PayslipLine, cap=_MAX_CALCULATED_LINES)
    if any(item.payslip.id != payslip_id for item in lines):
        raise _invalid_source()
    return lines


async def _all_worked_days(
    adapter: OdooAdapter, company_id: int, payslip_id: int
) -> list[PayslipWorkedDay]:
    filters = PayslipChildFilters(payslip_ids=(payslip_id,))

    async def fetch(page: PayrollPageRequest) -> PayrollPage[PayslipWorkedDay]:
        return await adapter.get_payslip_worked_days(company_id, filters, page)

    rows = await _collect_pages(fetch, PayslipWorkedDay, cap=_MAX_WORKED_DAYS)
    if any(item.payslip.id != payslip_id for item in rows):
        raise _invalid_source()
    return rows


@dataclass(frozen=True, slots=True)
class PayrollWriteSnapshot:
    version: int
    payslip: Payslip
    inputs: tuple[PayslipInput, ...]
    employee: PayrollEmployee
    contract: PayrollContractSegment
    structure: PayrollStructure
    input_types: tuple[PayrollInputType, ...]
    input_types_truncated: bool = False
    calculated_lines: tuple[PayslipLine, ...] = ()
    worked_days: tuple[PayslipWorkedDay, ...] = ()

    @property
    def adapter_checkpoint(self) -> PayrollWriteCheckpoint:
        return PayrollWriteCheckpoint(
            version=self.version,
            payslip=self.payslip,
            inputs=self.inputs,
            employee=self.employee,
            contract=self.contract,
            structure=self.structure,
            input_types=self.input_types,
            input_types_truncated=self.input_types_truncated,
            calculated_lines=self.calculated_lines,
            worked_days=self.worked_days,
        )

    @property
    def checkpoint(self) -> str:
        payload = self.adapter_checkpoint.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


async def _snapshot(
    adapter: OdooAdapter,
    company_id: int,
    payslip_id: int,
    version: int,
    *,
    input_type_id: int | None = None,
    list_eligible_types: bool = False,
    include_calculation_sources: bool = False,
) -> PayrollWriteSnapshot:
    payslip = await _exact_payslip(adapter, company_id, payslip_id)
    _require_editable(payslip, version)
    inputs = await _all_inputs(adapter, company_id, payslip_id)
    if any(
        item.contract_segment.id != payslip.contract_segment.id
        or item.contract_source != payslip.contract_source
        for item in inputs
    ):
        raise _invalid_source()
    employee = await _exact_employee(adapter, company_id, payslip)
    contract = await _exact_contract(adapter, company_id, payslip)
    structure = await adapter.get_payroll_structure(company_id, payslip.structure.id)
    if not isinstance(structure, PayrollStructure):
        raise _malformed_source()
    if structure.id != payslip.structure.id:
        raise _invalid_source()
    input_types: list[PayrollInputType] = []
    input_types_truncated = False
    if input_type_id is not None:
        input_types.append(await _eligible_type(adapter, company_id, structure, input_type_id))
    elif list_eligible_types:
        input_types, input_types_truncated = await _eligible_types_for_selection(
            adapter, company_id, structure
        )
    lines = await _all_lines(adapter, company_id, payslip_id) if include_calculation_sources else []
    worked_days = (
        await _all_worked_days(adapter, company_id, payslip_id)
        if include_calculation_sources
        else []
    )
    return PayrollWriteSnapshot(
        version=version,
        payslip=payslip,
        inputs=tuple(inputs),
        employee=employee,
        contract=contract,
        structure=structure,
        input_types=tuple(input_types),
        input_types_truncated=input_types_truncated,
        calculated_lines=tuple(lines),
        worked_days=tuple(worked_days),
    )


@dataclass(frozen=True, slots=True)
class PayrollWritePlan:
    operation: PayrollWriteOperation
    request: SetDraftPayrollInput | RemoveDraftPayrollInput | RecalculateDraftPayslip
    snapshot: PayrollWriteSnapshot
    current_input: PayslipInput | None = None
    input_type: PayrollInputType | None = None


def _currency(payslip: Payslip) -> dict[str, object]:
    return {"id": payslip.currency.id, "name": payslip.currency.name}


def _type_facts(input_type: PayrollInputType) -> dict[str, object]:
    return {
        "id": input_type.id,
        "name": input_type.name,
        "code": input_type.code,
        "is_quantity": input_type.is_quantity,
        "structure_scope": list(input_type.structure_ids),
    }


def _input_facts(
    item: PayslipInput,
    payslip: Payslip,
    input_type: PayrollInputType,
) -> dict[str, object]:
    return {
        "id": item.id,
        "payslip_id": item.payslip.id,
        "description": item.name,
        "amount": str(item.amount),
        "input_type": _type_facts(input_type),
        "currency": _currency(payslip),
    }


def _target_facts(snapshot: PayrollWriteSnapshot) -> dict[str, object]:
    payslip = snapshot.payslip
    return {
        "payslip_id": payslip.id,
        "source_state": payslip.source_state,
        "normalized_state": payslip.state,
        "employee_id": payslip.employee.id,
        "period_start": payslip.date_from.isoformat(),
        "period_end": payslip.date_to.isoformat(),
        "structure_id": payslip.structure.id,
        "contract_source": payslip.contract_source,
        "contract_id": payslip.contract_segment.id,
        "currency": _currency(payslip),
    }


async def prepare_set_draft_payroll_input(
    adapter: OdooAdapter,
    request: SetDraftPayrollInput,
    version: int,
) -> tuple[PayrollWritePlan, PreparedWrite]:
    operation: Literal["create", "update"] = "create" if request.input_id is None else "update"
    selected_type_id = request.input_type_id
    snapshot = await _snapshot(
        adapter,
        request.company_id,
        request.payslip_id,
        version,
        input_type_id=selected_type_id,
        list_eligible_types=operation == "create" and selected_type_id is None,
    )
    current: PayslipInput | None = None
    selected_type: PayrollInputType | None = None
    if operation == "update":
        current_matches = [item for item in snapshot.inputs if item.id == request.input_id]
        if len(current_matches) != 1:
            raise _error(
                ErrorCode.PAYROLL_INPUT_NOT_FOUND,
                "The requested Payroll input is unavailable.",
                "Use an exact input ID from the editable payslip.",
            )
        current = current_matches[0]
        refreshed = await _snapshot(
            adapter,
            request.company_id,
            request.payslip_id,
            version,
            input_type_id=current.input_type.id,
        )
        if (
            refreshed.payslip != snapshot.payslip
            or refreshed.inputs != snapshot.inputs
            or refreshed.employee != snapshot.employee
            or refreshed.contract != snapshot.contract
            or refreshed.structure != snapshot.structure
        ):
            raise _state_conflict()
        snapshot = refreshed
        selected_type = snapshot.input_types[0]
        if current.code != selected_type.code:
            raise _invalid_source()
    elif selected_type_id is not None:
        selected_type = snapshot.input_types[0]
        if any(
            item.input_type.id == selected_type.id or item.code == selected_type.code
            for item in snapshot.inputs
        ):
            raise _error(
                ErrorCode.PAYROLL_INPUT_TYPE_NOT_ALLOWED,
                "The payslip already contains this Payroll input type.",
                "Update the existing input instead of creating a duplicate.",
            )

    proposed_description = (
        request.description
        if request.description is not None
        else None
        if current is None
        else current.name
    )
    proposed_amount = (
        request.amount
        if request.amount is not None
        else None
        if current is None
        else current.amount
    )
    proposed: dict[str, object] = {
        "description": proposed_description,
        "amount": None if proposed_amount is None else str(proposed_amount),
    }
    if selected_type is not None:
        proposed["input_type"] = _type_facts(selected_type)
    action: dict[str, object] = {
        "operation": operation,
        "payslip_id": request.payslip_id,
        "input_id": request.input_id,
        "before_source_state": snapshot.payslip.source_state,
        "target": _target_facts(snapshot),
        "current_input": (
            None
            if current is None or selected_type is None
            else _input_facts(current, snapshot.payslip, selected_type)
        ),
        "proposed_input": proposed,
        "recalculation_required": True,
        "checkpoint": snapshot.checkpoint,
        "warnings": ["calculated_payroll_values_are_not_updated_automatically"],
        "limitations": [
            "odoo_payroll_formulas_are_not_simulated",
            *(["eligible_input_types_truncated"] if snapshot.input_types_truncated else []),
        ],
    }
    if operation == "create" and selected_type is None:
        action["eligible_input_types"] = [_type_facts(item) for item in snapshot.input_types]
    return (
        PayrollWritePlan(
            operation=operation,
            request=request,
            snapshot=snapshot,
            current_input=current,
            input_type=selected_type,
        ),
        PreparedWrite(
            proposed_action=action,
            material_effects={
                "operation": operation,
                "payslip_id": request.payslip_id,
                "input_id": request.input_id,
                "recalculation_required": True,
                "effects": "deferred_until_execution",
            },
            needs_input=operation == "create" and selected_type is None,
        ),
    )


async def prepare_remove_draft_payroll_input(
    adapter: OdooAdapter,
    request: RemoveDraftPayrollInput,
    version: int,
) -> tuple[PayrollWritePlan, PreparedWrite]:
    initial = await _snapshot(adapter, request.company_id, request.payslip_id, version)
    matches = [item for item in initial.inputs if item.id == request.input_id]
    if len(matches) != 1:
        raise _error(
            ErrorCode.PAYROLL_INPUT_NOT_FOUND,
            "The requested Payroll input is unavailable.",
            "Use an exact input ID from the editable payslip.",
        )
    current = matches[0]
    snapshot = await _snapshot(
        adapter,
        request.company_id,
        request.payslip_id,
        version,
        input_type_id=current.input_type.id,
    )
    if (
        snapshot.payslip != initial.payslip
        or snapshot.inputs != initial.inputs
        or snapshot.employee != initial.employee
        or snapshot.contract != initial.contract
        or snapshot.structure != initial.structure
    ):
        raise _state_conflict()
    input_type = snapshot.input_types[0]
    if current.code != input_type.code:
        raise _invalid_source()
    return (
        PayrollWritePlan(
            operation="remove",
            request=request,
            snapshot=snapshot,
            current_input=current,
            input_type=input_type,
        ),
        PreparedWrite(
            proposed_action={
                "operation": "remove",
                "payslip_id": request.payslip_id,
                "input_id": request.input_id,
                "before_source_state": snapshot.payslip.source_state,
                "target": _target_facts(snapshot),
                "current_input": _input_facts(current, snapshot.payslip, input_type),
                "checkpoint": snapshot.checkpoint,
                "recalculation_required": True,
                "warnings": ["execution_deletes_the_exact_payroll_input_record"],
                "limitations": ["odoo_payroll_formulas_are_not_simulated"],
            },
            material_effects={
                "operation": "remove",
                "payslip_id": request.payslip_id,
                "input_id": request.input_id,
                "recalculation_required": True,
                "effects": "deferred_until_execution",
            },
        ),
    )


async def prepare_recalculate_draft_payslip(
    adapter: OdooAdapter,
    request: RecalculateDraftPayslip,
    version: int,
) -> tuple[PayrollWritePlan, PreparedWrite]:
    snapshot = await _snapshot(
        adapter,
        request.company_id,
        request.payslip_id,
        version,
        include_calculation_sources=True,
    )
    return (
        PayrollWritePlan(
            operation="recalculate",
            request=request,
            snapshot=snapshot,
        ),
        PreparedWrite(
            proposed_action={
                "operation": "recalculate",
                "payslip_id": request.payslip_id,
                "input_id": None,
                "before_source_state": snapshot.payslip.source_state,
                "target": _target_facts(snapshot),
                "input_count": len(snapshot.inputs),
                "worked_day_count": len(snapshot.worked_days),
                "calculated_line_count": len(snapshot.calculated_lines),
                "checkpoint": snapshot.checkpoint,
                "effects": "deferred_until_execution",
                "warnings": ["odoo_may_replace_calculated_draft_lines"],
                "limitations": ["future_payroll_totals_are_not_simulated"],
            },
            material_effects={
                "operation": "recalculate",
                "payslip_id": request.payslip_id,
                "input_id": None,
                "effects": "deferred_until_execution",
            },
        ),
    )


async def validate_payroll_write_plan(adapter: OdooAdapter, plan: PayrollWritePlan) -> None:
    request = plan.request
    refreshed = await _snapshot(
        adapter,
        request.company_id,
        request.payslip_id,
        plan.snapshot.version,
        input_type_id=(None if plan.input_type is None else plan.input_type.id),
        list_eligible_types=(plan.operation == "create" and plan.input_type is None),
        include_calculation_sources=plan.operation == "recalculate",
    )
    if refreshed.checkpoint != plan.snapshot.checkpoint:
        raise _state_conflict()


def _require_set_request(plan: PayrollWritePlan) -> SetDraftPayrollInput:
    if not isinstance(plan.request, SetDraftPayrollInput):
        raise TypeError("Invalid set-input plan")
    return plan.request


async def execute_payroll_write_plan(
    adapter: OdooAdapter,
    plan: PayrollWritePlan,
    *,
    request_id: str,
    observed_at: datetime,
) -> AppliedWrite:
    request = plan.request
    before_state = plan.snapshot.payslip.source_state
    if plan.operation == "create":
        set_request = _require_set_request(plan)
        if plan.input_type is None or set_request.description is None or set_request.amount is None:
            raise TypeError("Invalid create-input plan")
        result = await adapter.create_draft_payslip_input(
            set_request.company_id,
            DraftPayslipInputCreate(
                payslip_id=set_request.payslip_id,
                input_type_id=plan.input_type.id,
                description=set_request.description,
                amount=set_request.amount,
            ),
            plan.snapshot.adapter_checkpoint,
        )
        refreshed = await _snapshot(
            adapter,
            set_request.company_id,
            set_request.payslip_id,
            plan.snapshot.version,
            input_type_id=plan.input_type.id,
        )
        matches = [item for item in refreshed.inputs if item.id == result.id]
        if (
            len(matches) != 1
            or matches[0] != result
            or result.name != set_request.description
            or result.amount != set_request.amount
            or result.input_type.id != plan.input_type.id
            or refreshed.input_types != (plan.input_type,)
        ):
            raise _invalid_source()
        return AppliedWrite(
            material_effects={
                "operation": "create",
                "status": "completed",
                "payslip_id": set_request.payslip_id,
                "input_id": result.id,
                "result_input_id": result.id,
                "before_source_state": before_state,
                "after_source_state": refreshed.payslip.source_state,
                "resulting_input": _input_facts(result, refreshed.payslip, plan.input_type),
                "recalculation_required": True,
            },
            record_refs=(
                f"hr.payslip/{set_request.payslip_id}",
                f"hr.payslip.input/{result.id}",
            ),
        )
    if plan.operation == "update":
        set_request = _require_set_request(plan)
        if plan.current_input is None or plan.input_type is None or set_request.input_id is None:
            raise TypeError("Invalid update-input plan")
        result = await adapter.update_draft_payslip_input(
            set_request.company_id,
            set_request.input_id,
            DraftPayslipInputUpdate(
                payslip_id=set_request.payslip_id,
                description=set_request.description,
                amount=set_request.amount,
            ),
            plan.snapshot.adapter_checkpoint,
        )
        refreshed = await _snapshot(
            adapter,
            set_request.company_id,
            set_request.payslip_id,
            plan.snapshot.version,
            input_type_id=plan.input_type.id,
        )
        matches = [item for item in refreshed.inputs if item.id == result.id]
        expected_description = set_request.description or plan.current_input.name
        expected_amount = (
            set_request.amount if set_request.amount is not None else plan.current_input.amount
        )
        if (
            len(matches) != 1
            or matches[0] != result
            or result.id != set_request.input_id
            or result.name != expected_description
            or result.amount != expected_amount
            or result.input_type.id != plan.input_type.id
            or refreshed.input_types != (plan.input_type,)
        ):
            raise _invalid_source()
        return AppliedWrite(
            material_effects={
                "operation": "update",
                "status": "completed",
                "payslip_id": set_request.payslip_id,
                "input_id": result.id,
                "result_input_id": result.id,
                "before_source_state": before_state,
                "after_source_state": refreshed.payslip.source_state,
                "resulting_input": _input_facts(result, refreshed.payslip, plan.input_type),
                "recalculation_required": True,
            },
            record_refs=(
                f"hr.payslip/{set_request.payslip_id}",
                f"hr.payslip.input/{result.id}",
            ),
        )
    if plan.operation == "remove":
        if not isinstance(request, RemoveDraftPayrollInput) or plan.current_input is None:
            raise TypeError("Invalid remove-input plan")
        deleted = await adapter.delete_draft_payslip_input(
            request.company_id,
            request.payslip_id,
            request.input_id,
            plan.snapshot.adapter_checkpoint,
        )
        refreshed = await _snapshot(
            adapter,
            request.company_id,
            request.payslip_id,
            plan.snapshot.version,
        )
        if (
            deleted.id != request.input_id
            or deleted.payslip_id != request.payslip_id
            or any(item.id == request.input_id for item in refreshed.inputs)
        ):
            raise _invalid_source()
        return AppliedWrite(
            material_effects={
                "operation": "remove",
                "status": "completed",
                "completion": "deleted",
                "payslip_id": request.payslip_id,
                "input_id": request.input_id,
                "result_input_id": request.input_id,
                "before_source_state": before_state,
                "after_source_state": refreshed.payslip.source_state,
                "recalculation_required": True,
            },
            record_refs=(
                f"hr.payslip/{request.payslip_id}",
                f"hr.payslip.input/{request.input_id}",
            ),
        )
    if not isinstance(request, RecalculateDraftPayslip):
        raise TypeError("Invalid recalculation plan")
    recalculated = await adapter.recompute_draft_payslip(
        request.company_id,
        request.payslip_id,
        plan.snapshot.adapter_checkpoint,
    )
    evidence = await get_payslip(
        adapter,
        GetPayslipInput(company_id=request.company_id, payslip_id=request.payslip_id),
        request_id=request_id,
        observed_at=observed_at,
    )
    if (
        recalculated.id != request.payslip_id
        or evidence.payslip.payslip_id != request.payslip_id
        or evidence.payslip.state != recalculated.state
    ):
        raise _invalid_source()
    return AppliedWrite(
        material_effects={
            "operation": "recalculate",
            "status": "completed",
            "payslip_id": request.payslip_id,
            "input_id": None,
            "before_source_state": before_state,
            "after_source_state": recalculated.source_state,
            "normalized_state": recalculated.state,
            "payslip_evidence": evidence.model_dump(mode="json"),
        },
        record_refs=(f"hr.payslip/{request.payslip_id}",),
    )


def payroll_write_request_payload(
    request: SetDraftPayrollInput | RemoveDraftPayrollInput | RecalculateDraftPayslip,
) -> dict[str, object]:
    """Return the full fingerprint input; storage retains only its one-way hash."""

    return request.model_dump(
        mode="json",
        exclude={"company_id", "dry_run", "idempotency_key"},
        exclude_none=True,
    )


def payroll_write_audit_payload(
    operation: PayrollWriteOperation,
    request: SetDraftPayrollInput | RemoveDraftPayrollInput | RecalculateDraftPayslip,
) -> Mapping[str, object]:
    """Return the identifier-only Payroll write audit projection."""

    return {
        "operation": operation,
        "payslip_id": request.payslip_id,
        "input_id": getattr(request, "input_id", None),
    }
