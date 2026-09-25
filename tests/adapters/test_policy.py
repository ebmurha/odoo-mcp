from __future__ import annotations

import pytest

from odoo_mcp.adapters.odoo.capabilities import CAPABILITY_PROBES
from odoo_mcp.adapters.odoo.policy import (
    ACCOUNTING_MODEL_ACTION_ALLOWLIST,
    CORE_MODEL_READ_ALLOWLIST,
    FIELD_DENYLIST,
    MODULE_MODEL_READ_ALLOWLIST,
    PAYROLL_MODEL_ACTION_ALLOWLIST_BY_VERSION,
    PAYROLL_MODEL_READ_ALLOWLIST_BY_VERSION,
    ensure_accounting_action_allowed,
    ensure_model_read_allowed,
    ensure_payroll_action_allowed,
    ensure_payroll_model_read_allowed,
    ensure_probe_allowed,
    strip_denied_fields,
)
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError


def test_policy_constants_match_the_public_adapter_contract() -> None:
    assert CORE_MODEL_READ_ALLOWLIST == frozenset({"res.company"})
    assert MODULE_MODEL_READ_ALLOWLIST == {
        "accounting": frozenset(
            {
                "account.move",
                "account.move.line",
                "account.journal",
                "account.bank.statement.line",
                "account.payment",
                "account.payment.term",
                "account.account",
                "account.partial.reconcile",
                "account.analytic.account",
                "res.currency",
                "res.currency.rate",
                "res.partner",
                "product.product",
            }
        ),
    }
    assert ACCOUNTING_MODEL_ACTION_ALLOWLIST == {
        "account.move": frozenset({"create_draft", "post_existing_draft", "reverse_existing_move"}),
        "account.move.reversal": frozenset({"create_transient", "execute_standard_workflow"}),
        "account.payment.register": frozenset(
            {
                "create_preview_transient",
                "read_preview_transient",
                "create_execution_transient",
                "execute_standard_workflow",
            }
        ),
    }
    common_payroll_models = {
        "hr.payslip",
        "hr.payslip.run",
        "hr.payslip.line",
        "hr.payslip.worked_days",
        "hr.payslip.input",
        "hr.payslip.input.type",
        "hr.payroll.structure",
        "hr.employee",
        "hr.work.entry",
    }
    assert PAYROLL_MODEL_READ_ALLOWLIST_BY_VERSION == {
        18: frozenset({*common_payroll_models, "hr.contract"}),
        19: frozenset({*common_payroll_models, "hr.version"}),
    }
    payroll_actions = {
        "hr.payslip.input": frozenset(
            {"create_draft_input", "update_draft_input", "delete_draft_input"}
        ),
        "hr.payslip": frozenset({"compute_sheet_on_editable_payslip"}),
    }
    assert PAYROLL_MODEL_ACTION_ALLOWLIST_BY_VERSION == {
        18: payroll_actions,
        19: payroll_actions,
    }
    assert FIELD_DENYLIST == frozenset(
        {"password", "password_crypt", "api_key", "bank_account_number", "acc_number"}
    )


@pytest.mark.parametrize(
    ("module", "model"),
    [
        (None, "res.users"),
        ("accounting", "ir.config_parameter"),
        ("payroll", "account.move"),
    ],
)
def test_unlisted_model_reads_fail_closed(module: str | None, model: str) -> None:
    with pytest.raises(OdooMcpError) as caught:
        ensure_model_read_allowed(model, module=module)

    assert caught.value.code is ErrorCode.MODEL_NOT_ALLOWED


@pytest.mark.parametrize(
    ("model", "action"),
    [
        ("account.move", "unlink"),
        ("account.journal", "create_draft"),
        ("account.payment.term", "write"),
        ("account.move", "create_and_post"),
    ],
)
def test_unlisted_or_configuration_actions_fail_closed(model: str, action: str) -> None:
    with pytest.raises(OdooMcpError) as caught:
        ensure_accounting_action_allowed(model, action)

    assert caught.value.code is ErrorCode.MODEL_NOT_ALLOWED


def test_only_the_exact_accounting_actions_are_allowed() -> None:
    ensure_accounting_action_allowed("account.move", "create_draft")
    ensure_accounting_action_allowed("account.move", "post_existing_draft")
    ensure_accounting_action_allowed("account.move.reversal", "execute_standard_workflow")
    ensure_accounting_action_allowed("account.payment.register", "create_preview_transient")
    ensure_accounting_action_allowed("account.payment.register", "read_preview_transient")
    ensure_accounting_action_allowed("account.payment.register", "create_execution_transient")


@pytest.mark.parametrize(
    ("version", "model"),
    [
        (18, "hr.version"),
        (19, "hr.contract"),
        (19, "hr.salary.rule"),
        (19, "hr.attendance"),
        (17, "hr.payslip"),
    ],
)
def test_wrong_version_and_unlisted_payroll_reads_fail_closed(version: int, model: str) -> None:
    with pytest.raises(OdooMcpError) as caught:
        ensure_payroll_model_read_allowed(version, model)

    assert caught.value.code is ErrorCode.MODEL_NOT_ALLOWED


@pytest.mark.parametrize(
    ("model", "action"),
    [
        ("hr.payslip.input", "create"),
        ("hr.payslip.input", "write"),
        ("hr.payslip.input", "unlink"),
        ("hr.payslip", "action_payslip_done"),
        ("hr.payslip", "action_refresh_from_work_entries"),
        ("hr.payslip", "compute_sheet"),
        ("hr.work.entry", "write"),
    ],
)
def test_generic_and_final_payroll_actions_fail_closed(model: str, action: str) -> None:
    with pytest.raises(OdooMcpError) as caught:
        ensure_payroll_action_allowed(19, model, action)

    assert caught.value.code is ErrorCode.MODEL_NOT_ALLOWED


def test_only_the_four_symbolic_payroll_actions_are_allowed() -> None:
    for version in (18, 19):
        ensure_payroll_action_allowed(version, "hr.payslip.input", "create_draft_input")
        ensure_payroll_action_allowed(version, "hr.payslip.input", "update_draft_input")
        ensure_payroll_action_allowed(version, "hr.payslip.input", "delete_draft_input")
        ensure_payroll_action_allowed(version, "hr.payslip", "compute_sheet_on_editable_payslip")


def test_only_exact_capability_sentinel_is_probeable() -> None:
    ensure_probe_allowed("account", "account.move")

    with pytest.raises(OdooMcpError) as caught:
        ensure_probe_allowed("account", "account.payment")

    assert caught.value.code is ErrorCode.MODEL_NOT_ALLOWED

    CAPABILITY_PROBES["synthetic_unlisted"] = "ir.config_parameter"
    try:
        with pytest.raises(OdooMcpError) as unlisted:
            ensure_probe_allowed("synthetic_unlisted", "ir.config_parameter")
    finally:
        del CAPABILITY_PROBES["synthetic_unlisted"]

    assert unlisted.value.code is ErrorCode.MODEL_NOT_ALLOWED


def test_every_capability_probe_model_is_read_allowlisted() -> None:
    assert CAPABILITY_PROBES == {
        "base": "res.company",
        "account": "account.move",
        "account_accountant": "account.bank.statement.line",
        "hr_payroll": "hr.payslip",
    }
    read_models = set(CORE_MODEL_READ_ALLOWLIST) | set(MODULE_MODEL_READ_ALLOWLIST["accounting"])
    read_models |= set().union(*PAYROLL_MODEL_READ_ALLOWLIST_BY_VERSION.values())

    assert set(CAPABILITY_PROBES.values()) <= read_models


def test_field_denylist_is_removed_recursively() -> None:
    raw = {
        "id": 1,
        "password": "synthetic-secret",
        "nested": {
            "name": "safe",
            "API_KEY": "synthetic-secret",
            "rows": [{"acc_number": "synthetic-bank", "value": 2}],
        },
    }

    assert strip_denied_fields(raw) == {
        "id": 1,
        "nested": {"name": "safe", "rows": [{"value": 2}]},
    }
