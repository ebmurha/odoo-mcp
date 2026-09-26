"""Bounded Payroll workflows."""

from odoo_mcp.workflows.payroll.analysis import (
    analyze_employee_payroll_change,
    compare_payroll_periods,
    detect_payroll_anomalies,
    explain_payslip,
    prepare_payroll_approval_pack,
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

__all__ = [
    "analyze_employee_payroll_change",
    "compare_payroll_periods",
    "detect_payroll_anomalies",
    "explain_payslip",
    "get_attendance_summary",
    "get_employee_payroll_context",
    "get_payroll_batch",
    "get_payslip",
    "list_payroll_periods",
    "list_payslips",
    "list_salary_rules",
    "prepare_payroll_approval_pack",
]
