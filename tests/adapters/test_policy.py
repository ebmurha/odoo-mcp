from __future__ import annotations

import pytest

from odoo_mcp.adapters.odoo.policy import (
    ACCOUNTING_MODEL_ACTION_ALLOWLIST,
    CORE_MODEL_READ_ALLOWLIST,
    FIELD_DENYLIST,
    MODULE_MODEL_READ_ALLOWLIST,
    ensure_accounting_action_allowed,
    ensure_model_read_allowed,
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
                "account.payment.method.line",
                "account.account",
                "account.partial.reconcile",
                "account.analytic.account",
                "res.currency",
                "res.partner",
                "product.product",
            }
        ),
        "payroll": frozenset(
            {
                "hr.payslip",
                "hr.payslip.line",
                "hr.payslip.run",
                "hr.employee",
                "hr.contract",
                "hr.salary.rule",
                "hr.attendance",
            }
        ),
        "projects": frozenset(
            {
                "project.project",
                "project.task",
                "account.analytic.line",
                "account.analytic.account",
                "hr.timesheet",
            }
        ),
    }
    assert ACCOUNTING_MODEL_ACTION_ALLOWLIST == {
        "account.move": frozenset({"create_draft", "post_existing_draft", "reverse_existing_move"}),
        "account.move.reversal": frozenset({"create_transient", "execute_standard_workflow"}),
        "account.payment.register": frozenset({"create_transient", "execute_standard_workflow"}),
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
    ensure_accounting_action_allowed("account.payment.register", "create_transient")


def test_only_exact_capability_sentinel_is_probeable() -> None:
    ensure_probe_allowed("account", "account.move")

    with pytest.raises(OdooMcpError) as caught:
        ensure_probe_allowed("account", "account.payment")

    assert caught.value.code is ErrorCode.MODEL_NOT_ALLOWED


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
