"""Fail-closed Odoo model, action, and field policy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeAlias

from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError

CORE_MODEL_READ_ALLOWLIST = frozenset({"res.company"})

MODULE_MODEL_READ_ALLOWLIST: dict[str, frozenset[str]] = {
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
            "res.partner",
            "product.product",
        }
    ),
}

ACCOUNTING_MODEL_ACTION_ALLOWLIST: dict[str, frozenset[str]] = {
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

FIELD_DENYLIST = frozenset(
    {"password", "password_crypt", "api_key", "bank_account_number", "acc_number"}
)

SanitizedValue: TypeAlias = object


def _model_denied() -> OdooMcpError:
    return OdooMcpError(
        ErrorCode.MODEL_NOT_ALLOWED,
        "The requested Odoo model or action is not allowed.",
        "Use a supported workflow operation.",
    )


def ensure_model_read_allowed(model: str, *, module: str | None) -> None:
    allowed = (
        CORE_MODEL_READ_ALLOWLIST if module is None else MODULE_MODEL_READ_ALLOWLIST.get(module)
    )
    if allowed is None or model not in allowed:
        raise _model_denied()


def ensure_accounting_action_allowed(model: str, action: str) -> None:
    if action == "unlink" or action not in ACCOUNTING_MODEL_ACTION_ALLOWLIST.get(
        model, frozenset()
    ):
        raise _model_denied()


def ensure_probe_allowed(capability: str, model: str) -> None:
    from odoo_mcp.adapters.odoo.capabilities import CAPABILITY_PROBES

    read_allowed = model in CORE_MODEL_READ_ALLOWLIST or any(
        model in module_models for module_models in MODULE_MODEL_READ_ALLOWLIST.values()
    )
    if CAPABILITY_PROBES.get(capability) != model or not read_allowed:
        raise _model_denied()


def strip_denied_fields(value: SanitizedValue) -> SanitizedValue:
    if isinstance(value, Mapping):
        return {
            key: strip_denied_fields(item)
            for key, item in value.items()
            if isinstance(key, str) and key.casefold() not in FIELD_DENYLIST
        }
    if isinstance(value, list):
        return [strip_denied_fields(item) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_denied_fields(item) for item in value)
    return value
