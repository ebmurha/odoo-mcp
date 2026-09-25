"""Bounded Payroll workflows."""

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
    "get_attendance_summary",
    "get_employee_payroll_context",
    "get_payroll_batch",
    "get_payslip",
    "list_payroll_periods",
    "list_payslips",
    "list_salary_rules",
]
