"""Capability probes owned by the Odoo adapter boundary."""

from __future__ import annotations

from odoo_mcp.adapters.odoo.policy import ensure_probe_allowed
from odoo_mcp.adapters.odoo.transports.base import OdooTransport

CAPABILITY_PROBES: dict[str, str] = {
    "base": "res.company",
    "account": "account.move",
    "account_accountant": "account.bank.statement.line",
    "hr_payroll": "hr.payslip",
}


async def detect_capabilities(
    transport: OdooTransport,
    *,
    company_ids: tuple[int, ...],
) -> dict[str, bool]:
    """Probe model availability without returning raw model errors."""

    detected: dict[str, bool] = {}
    for capability, model in CAPABILITY_PROBES.items():
        ensure_probe_allowed(capability, model)
        detected[capability] = await transport.probe_model(
            model,
            company_ids=company_ids,
        )
    return detected
