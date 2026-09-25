"""Version-aware Odoo Payroll adapter implementation."""

from odoo_mcp.adapters.odoo.payroll.reader import (
    BATCH_STATE_MAP,
    PAYROLL_MODEL_FIELDS_BY_VERSION,
    PAYROLL_SOURCE_CAPS,
    PAYSLIP_STATE_MAP,
    WORK_ENTRY_STATE_MAP,
    PayrollReader,
)

__all__ = [
    "BATCH_STATE_MAP",
    "PAYROLL_MODEL_FIELDS_BY_VERSION",
    "PAYROLL_SOURCE_CAPS",
    "PAYSLIP_STATE_MAP",
    "WORK_ENTRY_STATE_MAP",
    "PayrollReader",
]
