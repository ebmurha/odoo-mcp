from __future__ import annotations

import pytest

from odoo_mcp.app.settings import OdooConnectionSettings


@pytest.fixture
def connection() -> OdooConnectionSettings:
    return OdooConnectionSettings(
        url="https://odoo.invalid",
        database="synthetic-db",
        username="synthetic-user",
        api_key="synthetic-secret",
        allowed_company_ids=(1, 2),
        default_company_id=1,
    )
