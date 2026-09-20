"""Single deterministic tool-registration and capability boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mcp.types import ToolAnnotations

from odoo_mcp.workflows.core.capabilities import ToolAvailability

RiskLevel = Literal["read", "propose", "draft_write", "confirm_write"]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    version: str
    title: str
    description: str
    risk_level: RiskLevel
    required_permission: str
    required_capability: str | None
    annotations: ToolAnnotations

    def __post_init__(self) -> None:
        if self.risk_level == "read":
            valid = (
                self.annotations.read_only_hint is True
                and self.annotations.destructive_hint is False
                and self.annotations.idempotent_hint is True
            )
        else:
            expected_destructive = self.risk_level == "confirm_write"
            valid = (
                self.annotations.read_only_hint is False
                and self.annotations.destructive_hint is expected_destructive
                and self.annotations.idempotent_hint is True
                and self.annotations.open_world_hint is True
            )
        if not valid:
            raise ValueError("Tool annotations do not match the declared risk level")

    def availability(self) -> ToolAvailability:
        return ToolAvailability(
            name=self.name,
            required_permission=self.required_permission,
            required_capability=self.required_capability,
        )

    def protocol_meta(self) -> dict[str, str | None]:
        return {
            "toolVersion": self.version,
            "riskLevel": self.risk_level,
            "requiredPermission": self.required_permission,
            "requiredCapability": self.required_capability,
        }


TOOL_REGISTRY: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="get_erp_capabilities",
        version="1.0.0",
        title="Get ERP capabilities",
        description=(
            "Discover the connected Odoo edition, version, installed module capabilities, "
            "available tools, and authorized companies."
        ),
        risk_level="read",
        required_permission="core_read",
        required_capability=None,
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="get_trial_balance",
        version="1.0.0",
        title="Get trial balance",
        description=(
            "Return opening balances, posted period movements, closing balances, "
            "company-currency totals, and a Markdown artifact."
        ),
        risk_level="read",
        required_permission="accounting_read",
        required_capability="account",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="get_aged_receivables",
        version="1.0.0",
        title="Get aged receivables",
        description=(
            "Return posted receivable residuals by partner and due-date bucket as of a date."
        ),
        risk_level="read",
        required_permission="accounting_read",
        required_capability="account",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="get_aged_payables",
        version="1.0.0",
        title="Get aged payables",
        description=(
            "Return posted payable residuals by partner and due-date bucket as of a date."
        ),
        risk_level="read",
        required_permission="accounting_read",
        required_capability="account",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="get_cashbook",
        version="1.0.0",
        title="Get cashbook",
        description=(
            "Return posted cash and bank transactions, opening and closing balances, "
            "period totals, and a Markdown artifact."
        ),
        risk_level="read",
        required_permission="accounting_read",
        required_capability="account",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="flag_unmatched_statement_lines",
        version="1.0.0",
        title="Flag unmatched bank statement lines",
        description=(
            "Identify unreconciled bank statement lines without a unique candidate "
            "at or above the requested confidence threshold."
        ),
        risk_level="read",
        required_permission="accounting_read",
        required_capability="account_accountant",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="reconcile_bank_statement_lines",
        version="1.0.0",
        title="Propose bank statement reconciliation",
        description=(
            "Preview or persist deterministic reconciliation proposals without "
            "changing final reconciliation state in Odoo."
        ),
        risk_level="propose",
        required_permission="accounting_propose",
        required_capability="account_accountant",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
)


def get_tool_definition(name: str) -> ToolDefinition:
    return next(tool for tool in TOOL_REGISTRY if tool.name == name)
