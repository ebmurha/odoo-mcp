"""Single deterministic tool-registration and capability boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mcp.types import ToolAnnotations

from odoo_mcp.workflows.core.capabilities import ToolAvailability

RiskLevel = Literal["read", "propose", "draft_write", "confirm_write"]

PERMISSION_GROUPS = frozenset(
    {
        "core_read",
        "accounting_read",
        "accounting_propose",
        "payroll_read",
        "payroll_draft_write",
    }
)


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
        name="get_currency_rate_history",
        version="1.0.0",
        title="Get currency rate history",
        description=(
            "Return company-scoped Odoo currency-rate history for an exact currency "
            "and inclusive period."
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
        name="get_profit_and_loss",
        version="1.0.0",
        title="Get profit and loss",
        description=(
            "Return posted income and expense balances for an inclusive period, "
            "group totals, net profit or loss, and a Markdown artifact."
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
        name="get_balance_sheet",
        version="1.0.0",
        title="Get balance sheet",
        description=(
            "Return posted asset, liability, equity, and unclosed earnings balances "
            "through an inclusive date with a balancing check and Markdown artifact."
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
    ToolDefinition(
        name="list_open_invoices",
        version="1.0.0",
        title="List open customer invoices",
        description="Return posted customer invoices with residual amounts as of a date.",
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
        name="list_open_bills",
        version="1.0.0",
        title="List open supplier bills",
        description="Return posted supplier bills with residual amounts as of a date.",
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
        name="create_customer_invoice",
        version="1.0.0",
        title="Create customer invoice draft",
        description="Preview inputs or explicitly create one unposted customer invoice draft.",
        risk_level="draft_write",
        required_permission="accounting_propose",
        required_capability="account",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="create_supplier_bill",
        version="1.0.0",
        title="Create supplier bill draft",
        description="Preview inputs or explicitly create one unposted supplier bill draft.",
        risk_level="draft_write",
        required_permission="accounting_propose",
        required_capability="account",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="create_credit_note",
        version="1.0.0",
        title="Create linked credit-note draft",
        description="Preview or explicitly create one linked, unposted full credit note.",
        risk_level="draft_write",
        required_permission="accounting_propose",
        required_capability="account",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="validate_invoice",
        version="1.0.0",
        title="Post an existing invoice draft",
        description="Preview or explicitly post one eligible existing draft document.",
        risk_level="confirm_write",
        required_permission="accounting_propose",
        required_capability="account",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="register_payment",
        version="1.0.0",
        title="Register invoice payment",
        description="Preview or execute Odoo's configured standard payment workflow.",
        risk_level="confirm_write",
        required_permission="accounting_propose",
        required_capability="account",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="list_journal_entries",
        version="1.0.0",
        title="List journal entries",
        description="List filtered manual journal entries with bounded compact lines.",
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
        name="create_journal_entry",
        version="1.0.0",
        title="Create manual journal entry draft",
        description="Preview or explicitly create one balanced unposted manual journal entry.",
        risk_level="draft_write",
        required_permission="accounting_propose",
        required_capability="account",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="post_journal_entry",
        version="1.0.0",
        title="Post existing manual journal entry",
        description="Preview or explicitly post one existing balanced draft manual entry.",
        risk_level="confirm_write",
        required_permission="accounting_propose",
        required_capability="account",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="list_payroll_periods",
        version="1.0.0",
        title="List payroll periods",
        description="List bounded exact periods observed in authorized Odoo payslips.",
        risk_level="read",
        required_permission="payroll_read",
        required_capability="hr_payroll",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="get_payroll_batch",
        version="1.0.0",
        title="Get payroll batch",
        description=(
            "Return one exact payroll batch with compact payslips and complete observed totals."
        ),
        risk_level="read",
        required_permission="payroll_read",
        required_capability="hr_payroll",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="list_payslips",
        version="1.0.0",
        title="List payslips",
        description="List compact payslips for an exact batch, exact period, or both.",
        risk_level="read",
        required_permission="payroll_read",
        required_capability="hr_payroll",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="get_payslip",
        version="1.0.0",
        title="Get payslip",
        description=(
            "Return one exact payslip with bounded calculated lines, worked days, and inputs."
        ),
        risk_level="read",
        required_permission="payroll_read",
        required_capability="hr_payroll",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="get_employee_payroll_context",
        version="1.0.0",
        title="Get employee payroll context",
        description=(
            "Return one employee's bounded contract evidence for an exact payroll period."
        ),
        risk_level="read",
        required_permission="payroll_read",
        required_capability="hr_payroll",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="list_salary_rules",
        version="1.0.0",
        title="List observed salary rules",
        description=(
            "List salary-rule snapshots observed on eligible company-scoped payslip lines."
        ),
        risk_level="read",
        required_permission="payroll_read",
        required_capability="hr_payroll",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="get_attendance_summary",
        version="1.0.0",
        title="Get payroll work-entry summary",
        description=(
            "Summarize bounded Odoo payroll work-entry evidence without claiming attendance."
        ),
        risk_level="read",
        required_permission="payroll_read",
        required_capability="hr_payroll",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="compare_payroll_periods",
        version="1.0.0",
        title="Compare payroll periods",
        description=(
            "Compare two exact payroll periods using current, source-linked Odoo evidence."
        ),
        risk_level="read",
        required_permission="payroll_read",
        required_capability="hr_payroll",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="analyze_employee_payroll_change",
        version="1.0.0",
        title="Analyze employee payroll change",
        description=(
            "Analyze one employee's exact line, contract, and work-entry changes across periods."
        ),
        risk_level="read",
        required_permission="payroll_read",
        required_capability="hr_payroll",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="detect_payroll_anomalies",
        version="1.0.0",
        title="Detect payroll anomalies",
        description=(
            "Apply fixed, explainable thresholds and optional request-time history "
            "to payroll evidence."
        ),
        risk_level="read",
        required_permission="payroll_read",
        required_capability="hr_payroll",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
    ToolDefinition(
        name="explain_payslip",
        version="1.0.0",
        title="Explain payslip evidence",
        description=(
            "Organize one exact payslip's Odoo-returned lines and context without recalculating it."
        ),
        risk_level="read",
        required_permission="payroll_read",
        required_capability="hr_payroll",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    ),
)


def get_tool_definition(name: str) -> ToolDefinition:
    return next(tool for tool in TOOL_REGISTRY if tool.name == name)
