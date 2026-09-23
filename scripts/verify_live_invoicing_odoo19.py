"""Qualify authorized Odoo 19 invoicing writes with fixed, secret-safe output."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

from mcp import Client

from odoo_mcp.adapters.accounting import FilterClause, InvoiceEffect, PageRequest, ReadFilters
from odoo_mcp.adapters.odoo.client import OdooClient
from odoo_mcp.adapters.odoo.connections import ConnectionBinding, StaticConnectionResolver
from odoo_mcp.app.settings import (
    DeploymentProfile,
    OdooConnectionSettings,
    SettingsError,
    load_settings,
)
from odoo_mcp.mcp.error_codes import OdooMcpError
from odoo_mcp.mcp.server import create_mcp_server
from odoo_mcp.storage import Storage
from odoo_mcp.storage.json_support import canonical_json, parse_mapping

PERMISSIONS = frozenset({"accounting_propose"})
SUCCESS = "Live Odoo 19 invoicing qualification passed."
STATE_PATH = Path(".odoo-mcp/live-invoicing-qualification.sqlite3")
CAMPAIGN_PATH = Path(".odoo-mcp/live-invoicing-qualification.json")
_MARKER_PATTERN = re.compile(r"ODOO-MCP-LIVE-[0-9A-F]{32}")
_check_stage = "startup"


class QualificationFailure(RuntimeError):
    def __init__(self, stage: str, error_class: str) -> None:
        super().__init__(stage)
        self.stage = stage
        self.error_class = error_class


class PrerequisiteUnavailable(QualificationFailure):
    pass


@dataclass(frozen=True, slots=True)
class References:
    company_id: int
    customer_id: int
    supplier_id: int
    income_account_id: int
    expense_account_id: int


@dataclass(frozen=True, slots=True)
class Campaign:
    marker: str
    tenant_id: str
    connection_fingerprint: str


def _fail(stage: str, error_class: str) -> NoReturn:
    raise QualificationFailure(stage, error_class)


def _stage(value: str) -> None:
    global _check_stage
    _check_stage = value


def _connection_fingerprint(connection: OdooConnectionSettings) -> str:
    identity = {
        "url": str(connection.url).rstrip("/"),
        "database": connection.database,
        "username": connection.username,
        "allowed_company_ids": list(connection.allowed_company_ids),
        "default_company_id": connection.default_company_id,
    }
    return hashlib.sha256(canonical_json(identity).encode()).hexdigest()


def _campaign_from_payload(payload: Mapping[str, object], connection_fingerprint: str) -> Campaign:
    if set(payload) != {"version", "connection_fingerprint", "marker"}:
        _fail("campaign_binding", "invalid_campaign")
    marker = payload.get("marker")
    stored_fingerprint = payload.get("connection_fingerprint")
    if (
        payload.get("version") != 1
        or not isinstance(marker, str)
        or _MARKER_PATTERN.fullmatch(marker) is None
        or not isinstance(stored_fingerprint, str)
        or len(stored_fingerprint) != 64
    ):
        _fail("campaign_binding", "invalid_campaign")
    if stored_fingerprint != connection_fingerprint:
        _fail("campaign_binding", "connection_mismatch")
    tenant_suffix = hashlib.sha256(marker.encode()).hexdigest()[:24]
    return Campaign(
        marker=marker,
        tenant_id=f"live-invoicing-qualification-{tenant_suffix}",
        connection_fingerprint=stored_fingerprint,
    )


def _load_campaign(connection: OdooConnectionSettings) -> Campaign:
    fingerprint = _connection_fingerprint(connection)
    if CAMPAIGN_PATH.exists():
        if not STATE_PATH.exists():
            _fail("campaign_binding", "campaign_state_missing")
        try:
            existing_payload = parse_mapping(CAMPAIGN_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _fail("campaign_binding", "invalid_campaign")
        return _campaign_from_payload(existing_payload, fingerprint)
    if STATE_PATH.exists():
        _fail("campaign_binding", "campaign_binding_missing")
    CAMPAIGN_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": 1,
        "connection_fingerprint": fingerprint,
        "marker": f"ODOO-MCP-LIVE-{uuid4().hex.upper()}",
    }
    try:
        with CAMPAIGN_PATH.open("x", encoding="utf-8", newline="\n") as campaign_file:
            campaign_file.write(canonical_json(payload) + "\n")
    except FileExistsError:
        if not STATE_PATH.exists():
            _fail("campaign_binding", "campaign_state_missing")
        try:
            payload = parse_mapping(CAMPAIGN_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _fail("campaign_binding", "invalid_campaign")
    except OSError:
        _fail("campaign_binding", "campaign_storage")
    return _campaign_from_payload(payload, fingerprint)


def _content(result: object, stage: str) -> dict[str, Any]:
    content = getattr(result, "structured_content", None)
    if not isinstance(content, Mapping) or not all(isinstance(key, str) for key in content):
        _fail(stage, "invalid_mcp_response")
    return dict(content)


def _require_status(content: Mapping[str, Any], expected: str, stage: str) -> None:
    if content.get("status") != expected:
        code = content.get("error_code")
        _fail(stage, str(code) if isinstance(code, str) else "unexpected_status")


def _record_id(content: Mapping[str, Any], stage: str) -> int:
    references = content.get("record_refs")
    if not isinstance(references, (list, tuple)) or len(references) != 1:
        _fail(stage, "invalid_record_reference")
    reference = references[0]
    if not isinstance(reference, str) or not reference.startswith("account.move:"):
        _fail(stage, "invalid_record_reference")
    try:
        identifier = int(reference.removeprefix("account.move:"))
    except ValueError:
        _fail(stage, "invalid_record_reference")
    if identifier <= 0:
        _fail(stage, "invalid_record_reference")
    return identifier


async def _preview(
    client: Client, tool: str, arguments: dict[str, object], stage: str
) -> dict[str, Any]:
    _stage(stage)
    result = _content(await client.call_tool(tool, {**arguments, "dry_run": True}), stage)
    _require_status(result, "preview", stage)
    return result


async def _execute_and_replay(
    client: Client,
    tool: str,
    arguments: dict[str, object],
    *,
    key: str,
    stage: str,
    recover_unknown: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    await _preview(client, tool, arguments, f"{stage}_preview")
    execution = {**arguments, "dry_run": False, "idempotency_key": key}
    _stage(stage)
    first = _content(await client.call_tool(tool, execution), stage)
    if first.get("status") != "succeeded":
        if not (
            recover_unknown is not None
            and first.get("status") == "failed"
            and first.get("outcome") == "unknown"
            and first.get("error_code") == "UNKNOWN_ERROR"
        ):
            _require_status(first, "succeeded", stage)
        assert recover_unknown is not None
        recovered = await recover_unknown()
        _stage(f"{stage}_replay")
        replay = _content(await client.call_tool(tool, execution), f"{stage}_replay")
        if replay != first:
            _fail(f"{stage}_replay", "idempotency_mismatch")
        if await recover_unknown() != recovered:
            _fail(f"{stage}_replay", "recovery_state_changed")
        return recovered
    _stage(f"{stage}_replay")
    replay = _content(await client.call_tool(tool, execution), f"{stage}_replay")
    if replay != first:
        _fail(f"{stage}_replay", "idempotency_mismatch")
    return first


def _stored_checkpoint(
    storage: Storage,
    campaign: Campaign,
    company_id: int,
    tool: str,
    key: str,
) -> tuple[str, dict[str, Any] | None] | None:
    with storage.database.transaction() as connection:
        row = connection.execute(
            """
            SELECT company_id, state, response_json
            FROM idempotency_keys
            WHERE tenant_id = ? AND tool_name = ? AND idempotency_key = ?
            """,
            (campaign.tenant_id, tool, key),
        ).fetchone()
    if row is None:
        return None
    if row["company_id"] != company_id:
        _fail("local_recovery", "checkpoint_company_mismatch")
    state = str(row["state"])
    if row["response_json"] is None:
        if state == "in_progress":
            return state, None
        _fail("local_recovery", "invalid_checkpoint")
    try:
        response = parse_mapping(str(row["response_json"]))
    except ValueError:
        _fail("local_recovery", "invalid_checkpoint")
    if state == "succeeded" and response.get("status") != "succeeded":
        _fail("local_recovery", "invalid_checkpoint")
    return state, dict(response)


async def _resume_or_execute(
    storage: Storage,
    campaign: Campaign,
    client: Client,
    tool: str,
    arguments: dict[str, object],
    *,
    key: str,
    stage: str,
    recover_unknown: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    company_id = arguments.get("company_id")
    if not isinstance(company_id, int) or isinstance(company_id, bool) or company_id <= 0:
        _fail(stage, "invalid_company")
    checkpoint = _stored_checkpoint(storage, campaign, company_id, tool, key)
    if checkpoint is not None and checkpoint[0] == "succeeded":
        _stage(f"{stage}_checkpoint")
        execution = {**arguments, "dry_run": False, "idempotency_key": key}
        replay = _content(await client.call_tool(tool, execution), _check_stage)
        _require_status(replay, "succeeded", _check_stage)
        if replay != checkpoint[1]:
            _fail(_check_stage, "checkpoint_response_mismatch")
        return replay
    return await _execute_and_replay(
        client,
        tool,
        arguments,
        key=key,
        stage=stage,
        recover_unknown=recover_unknown,
    )


async def _recover_exact_invoice(
    adapter: OdooClient,
    references: References,
    *,
    partner_id: int,
    move_type: str,
    description: str,
    stage: str,
) -> dict[str, Any]:
    _stage(f"{stage}_recovery")
    moves = await adapter.get_account_moves(
        references.company_id,
        ReadFilters(
            clauses=(
                FilterClause(field="partner_id", operator="=", value=partner_id),
                FilterClause(field="date", operator="=", value=date.today().isoformat()),
                FilterClause(field="move_type", operator="=", value=move_type),
            )
        ),
        PageRequest(limit=500),
    )
    matches: list[int] = []
    for move in moves.items:
        effect = await adapter.get_invoice_effect(references.company_id, move.id)
        if any(line.description == description for line in effect.lines):
            if effect.state not in {"draft", "posted"} or effect.move_type != move_type:
                _fail(f"{stage}_recovery", "unexpected_odoo_state")
            matches.append(effect.id)
    if len(matches) != 1:
        _fail(
            f"{stage}_recovery",
            "record_not_found" if not matches else "ambiguous_records",
        )
    return {"record_refs": [f"account.move:{matches[0]}"]}


async def _matching_credit_notes(
    adapter: OdooClient,
    references: References,
    description: str,
) -> list[int]:
    moves = await adapter.get_account_moves(
        references.company_id,
        ReadFilters(
            clauses=(
                FilterClause(field="partner_id", operator="=", value=references.customer_id),
                FilterClause(field="date", operator="=", value=date.today().isoformat()),
                FilterClause(field="move_type", operator="=", value="out_refund"),
            )
        ),
        PageRequest(limit=500),
    )
    matches: list[int] = []
    for move in moves.items:
        effect = await adapter.get_invoice_effect(references.company_id, move.id)
        if any(line.description == description for line in effect.lines):
            matches.append(effect.id)
    return matches


async def _execute_credit_note(
    storage: Storage,
    campaign: Campaign,
    client: Client,
    adapter: OdooClient,
    references: References,
    arguments: dict[str, object],
    *,
    key: str,
    description: str,
) -> dict[str, Any]:
    recovery_key = f"{key}-reconciled"
    recovered = _stored_checkpoint(
        storage,
        campaign,
        references.company_id,
        "create_credit_note",
        recovery_key,
    )
    if recovered is not None and recovered[0] == "succeeded":
        return await _resume_or_execute(
            storage,
            campaign,
            client,
            "create_credit_note",
            arguments,
            key=recovery_key,
            stage="credit_note_create",
        )
    original = _stored_checkpoint(
        storage, campaign, references.company_id, "create_credit_note", key
    )
    if original is None or original[0] != "unknown":
        return await _resume_or_execute(
            storage,
            campaign,
            client,
            "create_credit_note",
            arguments,
            key=key,
            stage="credit_note_create",
        )

    execution = {**arguments, "dry_run": False, "idempotency_key": key}
    _stage("credit_note_create_recovery_replay")
    first = _content(await client.call_tool("create_credit_note", execution), _check_stage)
    second = _content(await client.call_tool("create_credit_note", execution), _check_stage)
    if first != second or first.get("outcome") != "unknown":
        _fail(_check_stage, "idempotency_mismatch")
    if await _matching_credit_notes(adapter, references, description):
        _fail("credit_note_create_recovery", "ambiguous_outcome")
    return await _resume_or_execute(
        storage,
        campaign,
        client,
        "create_credit_note",
        arguments,
        key=recovery_key,
        stage="credit_note_create",
    )


def _manual_route(content: Mapping[str, Any]) -> tuple[int, int] | None:
    effects = content.get("material_effects")
    if not isinstance(effects, Mapping):
        return None
    choices = effects.get("valid_choices")
    if choices is None:
        selected = effects.get("selected_route")
        choices = [selected] if selected is not None else []
    if not isinstance(choices, list):
        return None
    for choice in choices:
        if not isinstance(choice, Mapping) or choice.get("payment_method_code") != "manual":
            continue
        journal = choice.get("journal")
        method = choice.get("payment_method_line")
        if not isinstance(journal, Mapping) or not isinstance(method, Mapping):
            continue
        journal_id = journal.get("id")
        method_id = method.get("id")
        if (
            isinstance(journal_id, int)
            and not isinstance(journal_id, bool)
            and journal_id > 0
            and isinstance(method_id, int)
            and not isinstance(method_id, bool)
            and method_id > 0
        ):
            return journal_id, method_id
    return None


async def _preflight(connection: OdooConnectionSettings) -> tuple[OdooClient, References]:
    _stage("preflight")
    adapter = await OdooClient.connect(connection)
    company_id = connection.default_company_id
    try:
        companies = await adapter.get_companies()
        capabilities = await adapter.get_capabilities()
        if capabilities.version != 19 or capabilities.transport != "json2":
            raise PrerequisiteUnavailable("preflight", "odoo19_json2")
        if not capabilities.modules.get("account", False):
            raise PrerequisiteUnavailable("preflight", "account_module")
        company = next((item for item in companies if item.id == company_id), None)
        if company is None or company.currency is None:
            raise PrerequisiteUnavailable("preflight", "authorized_company_currency")
        currencies = await adapter.get_currencies(
            company_id, (company.currency.id,), page=PageRequest(limit=1)
        )
        if len(currencies.items) != 1:
            raise PrerequisiteUnavailable("preflight", "authorized_company_currency")
        customer_page = await adapter.get_partners(
            company_id,
            ReadFilters(clauses=(FilterClause(field="customer_rank", operator=">", value=0),)),
            page=PageRequest(limit=1),
        )
        supplier_page = await adapter.get_partners(
            company_id,
            ReadFilters(clauses=(FilterClause(field="supplier_rank", operator=">", value=0),)),
            page=PageRequest(limit=1),
        )
        if not customer_page.items:
            raise PrerequisiteUnavailable("preflight", "customer")
        if not supplier_page.items:
            raise PrerequisiteUnavailable("preflight", "supplier")
        income_page = await adapter.get_account_accounts(
            company_id,
            ReadFilters(
                clauses=(
                    FilterClause(
                        field="account_type",
                        operator="in",
                        value=("income", "income_other"),
                    ),
                )
            ),
            page=PageRequest(limit=1),
        )
        expense_page = await adapter.get_account_accounts(
            company_id,
            ReadFilters(
                clauses=(
                    FilterClause(
                        field="account_type",
                        operator="in",
                        value=("expense", "expense_depreciation", "expense_direct_cost"),
                    ),
                )
            ),
            page=PageRequest(limit=1),
        )
        if not income_page.items:
            raise PrerequisiteUnavailable("preflight", "income_account")
        if not expense_page.items:
            raise PrerequisiteUnavailable("preflight", "expense_account")
        journals = await adapter.get_journals(company_id, page=PageRequest(limit=500))
        journal_types = {item.journal_type for item in journals.items}
        if "sale" not in journal_types:
            raise PrerequisiteUnavailable("preflight", "sales_journal")
        if "purchase" not in journal_types:
            raise PrerequisiteUnavailable("preflight", "purchase_journal")
        return adapter, References(
            company_id=company_id,
            customer_id=customer_page.items[0].id,
            supplier_id=supplier_page.items[0].id,
            income_account_id=income_page.items[0].id,
            expense_account_id=expense_page.items[0].id,
        )
    except BaseException:
        await adapter.close()
        raise


async def _verify_state(
    adapter: OdooClient,
    company_id: int,
    move_id: int,
    *,
    move_type: str,
    state: str,
    stage: str,
    paid: bool = False,
) -> None:
    _stage(stage)
    effect = await adapter.get_invoice_effect(company_id, move_id)
    if effect.move_type != move_type or effect.state != state:
        _fail(stage, "unexpected_odoo_state")
    if paid and (
        effect.amount_residual != Decimal("0") or effect.payment_state not in {"paid", "in_payment"}
    ):
        _fail(stage, "unexpected_payment_state")


async def _verify_synthetic_invoice(
    adapter: OdooClient,
    company_id: int,
    move_id: int,
    *,
    partner_id: int,
    move_type: str,
    description: str,
    allowed_states: frozenset[str],
    stage: str,
    paid: bool = False,
) -> InvoiceEffect:
    _stage(stage)
    effect = await adapter.get_invoice_effect(company_id, move_id)
    if (
        effect.company_id != company_id
        or effect.partner.id != partner_id
        or effect.move_type != move_type
        or effect.state not in allowed_states
        or effect.invoice_date != date.today()
        or len(effect.lines) != 1
        or effect.lines[0].description != description
        or effect.lines[0].quantity != Decimal("1")
        or effect.lines[0].unit_price != Decimal("1")
        or abs(effect.lines[0].subtotal) != Decimal("1")
        or abs(effect.amount_untaxed) != Decimal("1")
        or effect.taxes
    ):
        _fail(stage, "synthetic_record_mismatch")
    if paid and (
        effect.amount_residual != Decimal("0") or effect.payment_state not in {"paid", "in_payment"}
    ):
        _fail(stage, "unexpected_payment_state")
    return effect


async def _register_manual_payment(
    storage: Storage,
    campaign: Campaign,
    client: Client,
    adapter: OdooClient,
    references: References,
    move_id: int,
    *,
    key: str,
    stage: str,
    move_type: str,
    partner_id: int,
    description: str,
) -> None:
    await _verify_synthetic_invoice(
        adapter,
        references.company_id,
        move_id,
        partner_id=partner_id,
        move_type=move_type,
        description=description,
        allowed_states=frozenset({"posted"}),
        stage=f"{stage}_identity",
    )
    checkpoint = _stored_checkpoint(
        storage, campaign, references.company_id, "register_payment", key
    )
    arguments: dict[str, object] = {
        "company_id": references.company_id,
        "invoice_id": move_id,
        "payment_date": date.today().isoformat(),
    }
    if checkpoint is not None and checkpoint[0] == "succeeded":
        if checkpoint[1] is None:
            _fail(f"{stage}_checkpoint", "invalid_checkpoint")
        route = _manual_route(checkpoint[1])
        if route is None:
            _fail(f"{stage}_checkpoint", "invalid_checkpoint")
        arguments.update(journal_id=route[0], payment_method_line_id=route[1])
        await _resume_or_execute(
            storage, campaign, client, "register_payment", arguments, key=key, stage=stage
        )
        await _verify_synthetic_invoice(
            adapter,
            references.company_id,
            move_id,
            partner_id=partner_id,
            move_type=move_type,
            description=description,
            allowed_states=frozenset({"posted"}),
            stage=f"{stage}_checkpoint_state",
            paid=True,
        )
        return
    _stage(f"{stage}_route")
    route_preview = _content(
        await client.call_tool("register_payment", {**arguments, "dry_run": True}),
        f"{stage}_route",
    )
    if route_preview.get("status") not in {"preview", "needs_input"}:
        _require_status(route_preview, "preview", f"{stage}_route")
    route = _manual_route(route_preview)
    if route is None:
        raise PrerequisiteUnavailable(stage, "manual_payment_route")
    arguments.update(journal_id=route[0], payment_method_line_id=route[1])
    await _resume_or_execute(
        storage, campaign, client, "register_payment", arguments, key=key, stage=stage
    )
    await _verify_synthetic_invoice(
        adapter,
        references.company_id,
        move_id,
        partner_id=partner_id,
        move_type=move_type,
        description=description,
        allowed_states=frozenset({"posted"}),
        stage=f"{stage}_state",
        paid=True,
    )


async def _qualify() -> None:
    _stage("startup")
    settings = load_settings(DeploymentProfile.LOCAL)
    if settings.connection is None:
        _fail("startup", "missing_connection")
    connection = settings.connection
    campaign = _load_campaign(connection)
    adapter, references = await _preflight(connection)
    marker = campaign.marker
    binding = ConnectionBinding(
        profile=DeploymentProfile.LOCAL,
        tenant_id=campaign.tenant_id,
        authenticated_subject="local-qualification",
        mcp_client="live-invoicing-qualifier",
        permissions=PERMISSIONS,
        connection=connection,
    )
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        storage = Storage.open(STATE_PATH)
        server = create_mcp_server(StaticConnectionResolver(binding), storage=storage)
        async with Client(server) as client:
            customer_arguments: dict[str, object] = {
                "company_id": references.company_id,
                "partner_id": references.customer_id,
                "invoice_date": date.today().isoformat(),
                "lines": [
                    {
                        "description": f"{marker}-CUSTOMER",
                        "quantity": "1",
                        "unit_price": "1",
                        "account_id": references.income_account_id,
                    }
                ],
            }
            customer = await _resume_or_execute(
                storage,
                campaign,
                client,
                "create_customer_invoice",
                customer_arguments,
                key=f"{marker}-customer-create",
                stage="customer_invoice_create",
                recover_unknown=lambda: _recover_exact_invoice(
                    adapter,
                    references,
                    partner_id=references.customer_id,
                    move_type="out_invoice",
                    description=f"{marker}-CUSTOMER",
                    stage="customer_invoice_create",
                ),
            )
            customer_id = _record_id(customer, "customer_invoice_create")
            customer_effect = await _verify_synthetic_invoice(
                adapter,
                references.company_id,
                customer_id,
                partner_id=references.customer_id,
                move_type="out_invoice",
                description=f"{marker}-CUSTOMER",
                allowed_states=frozenset({"draft", "posted"}),
                stage="customer_invoice_identity",
            )
            if customer_effect.state == "draft":
                await _verify_state(
                    adapter,
                    references.company_id,
                    customer_id,
                    move_type="out_invoice",
                    state="draft",
                    stage="customer_invoice_draft_state",
                )
            await _resume_or_execute(
                storage,
                campaign,
                client,
                "validate_invoice",
                {"company_id": references.company_id, "invoice_id": customer_id},
                key=f"{marker}-customer-post",
                stage="customer_invoice_post",
            )
            await _verify_state(
                adapter,
                references.company_id,
                customer_id,
                move_type="out_invoice",
                state="posted",
                stage="customer_invoice_posted_state",
            )
            await _register_manual_payment(
                storage,
                campaign,
                client,
                adapter,
                references,
                customer_id,
                key=f"{marker}-customer-payment",
                stage="customer_payment",
                move_type="out_invoice",
                partner_id=references.customer_id,
                description=f"{marker}-CUSTOMER",
            )

            bill_arguments: dict[str, object] = {
                "company_id": references.company_id,
                "partner_id": references.supplier_id,
                "invoice_date": date.today().isoformat(),
                "vendor_reference": f"{marker}-BILL",
                "lines": [
                    {
                        "description": f"{marker}-SUPPLIER",
                        "quantity": "1",
                        "unit_price": "1",
                        "account_id": references.expense_account_id,
                    }
                ],
            }
            bill = await _resume_or_execute(
                storage,
                campaign,
                client,
                "create_supplier_bill",
                bill_arguments,
                key=f"{marker}-bill-create",
                stage="supplier_bill_create",
            )
            bill_id = _record_id(bill, "supplier_bill_create")
            bill_effect = await _verify_synthetic_invoice(
                adapter,
                references.company_id,
                bill_id,
                partner_id=references.supplier_id,
                move_type="in_invoice",
                description=f"{marker}-SUPPLIER",
                allowed_states=frozenset({"draft", "posted"}),
                stage="supplier_bill_identity",
            )
            if bill_effect.state == "draft":
                await _verify_state(
                    adapter,
                    references.company_id,
                    bill_id,
                    move_type="in_invoice",
                    state="draft",
                    stage="supplier_bill_draft_state",
                )
            await _resume_or_execute(
                storage,
                campaign,
                client,
                "validate_invoice",
                {"company_id": references.company_id, "invoice_id": bill_id},
                key=f"{marker}-bill-post",
                stage="supplier_bill_post",
            )
            await _verify_state(
                adapter,
                references.company_id,
                bill_id,
                move_type="in_invoice",
                state="posted",
                stage="supplier_bill_posted_state",
            )
            await _register_manual_payment(
                storage,
                campaign,
                client,
                adapter,
                references,
                bill_id,
                key=f"{marker}-bill-payment",
                stage="supplier_payment",
                move_type="in_invoice",
                partner_id=references.supplier_id,
                description=f"{marker}-SUPPLIER",
            )

            source_arguments = {
                **customer_arguments,
                "lines": [
                    {
                        "description": f"{marker}-CREDIT-SOURCE",
                        "quantity": "1",
                        "unit_price": "1",
                        "account_id": references.income_account_id,
                    }
                ],
            }
            source = await _resume_or_execute(
                storage,
                campaign,
                client,
                "create_customer_invoice",
                source_arguments,
                key=f"{marker}-credit-source-create",
                stage="credit_source_create",
            )
            source_id = _record_id(source, "credit_source_create")
            await _verify_synthetic_invoice(
                adapter,
                references.company_id,
                source_id,
                partner_id=references.customer_id,
                move_type="out_invoice",
                description=f"{marker}-CREDIT-SOURCE",
                allowed_states=frozenset({"draft", "posted"}),
                stage="credit_source_identity",
            )
            await _resume_or_execute(
                storage,
                campaign,
                client,
                "validate_invoice",
                {"company_id": references.company_id, "invoice_id": source_id},
                key=f"{marker}-credit-source-post",
                stage="credit_source_post",
            )
            await _verify_state(
                adapter,
                references.company_id,
                source_id,
                move_type="out_invoice",
                state="posted",
                stage="credit_source_posted_state",
            )
            await _verify_synthetic_invoice(
                adapter,
                references.company_id,
                source_id,
                partner_id=references.customer_id,
                move_type="out_invoice",
                description=f"{marker}-CREDIT-SOURCE",
                allowed_states=frozenset({"posted"}),
                stage="credit_source_pre_reversal_identity",
            )
            credit = await _execute_credit_note(
                storage,
                campaign,
                client,
                adapter,
                references,
                {
                    "company_id": references.company_id,
                    "original_move_id": source_id,
                    "credit_date": date.today().isoformat(),
                    "reason": f"{marker}-CREDIT",
                },
                key=f"{marker}-credit-create",
                description=f"{marker}-CREDIT-SOURCE",
            )
            credit_id = _record_id(credit, "credit_note_create")
            credit_effect = await _verify_synthetic_invoice(
                adapter,
                references.company_id,
                credit_id,
                partner_id=references.customer_id,
                move_type="out_refund",
                description=f"{marker}-CREDIT-SOURCE",
                allowed_states=frozenset({"draft", "posted"}),
                stage="credit_note_identity",
            )
            if credit_effect.state == "draft":
                await _verify_state(
                    adapter,
                    references.company_id,
                    credit_id,
                    move_type="out_refund",
                    state="draft",
                    stage="credit_note_draft_state",
                )
            await _resume_or_execute(
                storage,
                campaign,
                client,
                "validate_invoice",
                {"company_id": references.company_id, "invoice_id": credit_id},
                key=f"{marker}-credit-post",
                stage="credit_note_post",
            )
            await _verify_state(
                adapter,
                references.company_id,
                credit_id,
                move_type="out_refund",
                state="posted",
                stage="credit_note_posted_state",
            )
        _stage("local_audit_verification")
        storage.audit.verify_chain(campaign.tenant_id)
        storage.verify()
    finally:
        await adapter.close()


def main(argv: list[str] | None = None) -> None:
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Run authorized live Odoo invoicing checks")
    parser.add_argument("--execute-authorized-writes", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute_authorized_writes:
        print(
            "Live Odoo 19 invoicing qualification refused: explicit write flag required.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        asyncio.run(_qualify())
    except PrerequisiteUnavailable as exc:
        print(
            f"Live Odoo 19 invoicing qualification prerequisite unavailable: {exc.error_class}.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except QualificationFailure as exc:
        print(
            f"Live Odoo 19 invoicing qualification failed safely during {exc.stage}: "
            f"{exc.error_class}.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except OdooMcpError as exc:
        print(
            f"Live Odoo 19 invoicing qualification failed safely: {exc.code.value}.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except SettingsError:
        print("Live Odoo 19 invoicing qualification failed safely: settings.", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print(
            f"Live Odoo 19 invoicing qualification failed safely during {_check_stage}: internal.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(SUCCESS)


if __name__ == "__main__":
    main()
