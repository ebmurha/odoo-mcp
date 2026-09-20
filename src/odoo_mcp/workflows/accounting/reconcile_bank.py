"""Deterministic, proposal-only bank reconciliation workflows."""

from __future__ import annotations

import base64
import binascii
import calendar
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from odoo_mcp.adapters.accounting import (
    AccountMoveLine,
    BankStatementLine,
    DatePeriod,
    FilterClause,
    Journal,
    PageRequest,
    ReadFilters,
)
from odoo_mcp.adapters.base import OdooAdapter
from odoo_mcp.mcp.error_codes import ErrorCode, OdooMcpError
from odoo_mcp.mcp.schemas import (
    ReconciliationInput,
    ReconciliationMatch,
    ReconciliationProposal,
    ReconciliationSummary,
    UnmatchedReason,
    UnmatchedStatementLineItem,
    UnmatchedStatementLinesInput,
    UnmatchedStatementLinesResponse,
    UnmatchedStatementLinesSummary,
)

_SOURCE_PAGE_SIZE = 100
_MAX_SOURCE_RECORDS = 100_000
_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class _Candidate:
    line: AccountMoveLine
    score: Decimal
    components: dict[str, Decimal]


@dataclass(frozen=True, slots=True)
class _Decision:
    statement: BankStatementLine
    candidate: _Candidate | None
    best_score: Decimal | None
    reason: UnmatchedReason | None


def _source_error(message: str) -> OdooMcpError:
    return OdooMcpError(
        ErrorCode.ODOO_API_ERROR,
        message,
        "Check Odoo compatibility and accounting access, then retry.",
    )


async def _statement_lines(
    adapter: OdooAdapter,
    company_id: int,
    period: DatePeriod,
    journal_id: int | None,
) -> list[BankStatementLine]:
    result: list[BankStatementLine] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_ids: set[int] = set()
    while True:
        page = await adapter.get_bank_statement_lines(
            company_id,
            period,
            journal_id,
            page=PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
        )
        if len(page.items) > _SOURCE_PAGE_SIZE:
            raise _source_error("Odoo returned an oversized bank statement page.")
        for item in page.items:
            if (
                item.company_id != company_id
                or item.id in seen_ids
                or item.date < period.start
                or item.date > period.end
                or (journal_id is not None and item.journal.id != journal_id)
            ):
                raise _source_error("Odoo returned inconsistent bank statement data.")
            seen_ids.add(item.id)
            result.append(item)
        if len(result) > _MAX_SOURCE_RECORDS:
            raise _source_error("The reconciliation input exceeds the safe processing bound.")
        if page.next_cursor is None:
            return result
        if page.next_cursor in seen_cursors:
            raise _source_error("Odoo returned an invalid bank statement page.")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor


async def _candidate_lines(adapter: OdooAdapter, company_id: int) -> list[AccountMoveLine]:
    filters = ReadFilters(
        clauses=(
            FilterClause(field="move_id.state", operator="=", value="posted"),
            FilterClause(field="reconciled", operator="=", value=False),
        )
    )
    result: list[AccountMoveLine] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_ids: set[int] = set()
    while True:
        page = await adapter.get_account_move_lines(
            company_id,
            filters,
            PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
        )
        if len(page.items) > _SOURCE_PAGE_SIZE:
            raise _source_error("Odoo returned an oversized reconciliation candidate page.")
        for item in page.items:
            if item.company_id != company_id or item.id in seen_ids:
                raise _source_error("Odoo returned inconsistent reconciliation candidate data.")
            seen_ids.add(item.id)
            if not item.reconciled:
                result.append(item)
        if len(result) > _MAX_SOURCE_RECORDS:
            raise _source_error("The reconciliation candidates exceed the safe processing bound.")
        if page.next_cursor is None:
            return result
        if page.next_cursor in seen_cursors:
            raise _source_error("Odoo returned an invalid reconciliation candidate page.")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor


async def _journals(adapter: OdooAdapter, company_id: int) -> dict[int, Journal]:
    result: dict[int, Journal] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        page = await adapter.get_journals(
            company_id,
            page=PageRequest(limit=_SOURCE_PAGE_SIZE, cursor=cursor),
        )
        if len(page.items) > _SOURCE_PAGE_SIZE:
            raise _source_error("Odoo returned an oversized reconciliation journal page.")
        for item in page.items:
            if item.company_id != company_id or item.id in result:
                raise _source_error("Odoo returned inconsistent reconciliation journal data.")
            result[item.id] = item
        if len(result) > _MAX_SOURCE_RECORDS:
            raise _source_error("The reconciliation journals exceed the safe processing bound.")
        if page.next_cursor is None:
            return result
        if page.next_cursor in seen_cursors:
            raise _source_error("Odoo returned an invalid reconciliation journal page.")
        seen_cursors.add(page.next_cursor)
        cursor = page.next_cursor


def _reference(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _candidate(
    statement: BankStatementLine,
    line: AccountMoveLine,
    journal: Journal,
) -> _Candidate | None:
    if journal.currency is None:
        amounts_match = statement.amount == -line.residual
    else:
        amounts_match = (
            line.currency is not None
            and line.currency.id == journal.currency.id
            and statement.amount == -line.residual_currency
        )
    if line.reconciled or not amounts_match:
        return None
    if statement.move is not None and statement.move.id == line.move.id:
        return None
    components = {
        "amount": Decimal("0.55"),
        "partner": Decimal("0"),
        "reference": Decimal("0"),
        "date_proximity": Decimal("0"),
    }
    if statement.partner is not None and line.partner is not None:
        if statement.partner.id == line.partner.id:
            components["partner"] = Decimal("0.20")
    statement_reference = _reference(statement.payment_reference)
    line_reference = _reference(line.label)
    if statement_reference and statement_reference == line_reference:
        components["reference"] = Decimal("0.15")
    days = abs((statement.date - line.date).days)
    if days <= 30:
        components["date_proximity"] = Decimal("0.10") * Decimal(30 - days) / Decimal(30)
    return _Candidate(line=line, score=sum(components.values(), _ZERO), components=components)


def _decisions(
    statements: list[BankStatementLine],
    candidates: list[AccountMoveLine],
    threshold: Decimal,
    journals: dict[int, Journal],
) -> list[_Decision]:
    company_candidates: dict[Decimal, list[AccountMoveLine]] = {}
    currency_candidates: dict[tuple[int, Decimal], list[AccountMoveLine]] = {}
    for line in candidates:
        company_candidates.setdefault(line.residual, []).append(line)
        if line.currency is not None:
            currency_candidates.setdefault((line.currency.id, line.residual_currency), []).append(
                line
            )
    provisional: list[_Decision] = []
    for statement in statements:
        journal = journals.get(statement.journal.id)
        if journal is None or journal.journal_type not in {"bank", "cash"}:
            raise _source_error("Odoo returned an unavailable statement journal.")
        if journal.currency is None:
            eligible_amounts = company_candidates.get(-statement.amount, [])
        else:
            eligible_amounts = currency_candidates.get((journal.currency.id, -statement.amount), [])
        scored = [
            candidate
            for line in eligible_amounts
            if (candidate := _candidate(statement, line, journal)) is not None
        ]
        scored.sort(key=lambda item: (-item.score, item.line.id))
        if not scored:
            provisional.append(_Decision(statement, None, None, "no_eligible_candidate"))
            continue
        best_score = scored[0].score
        best = [item for item in scored if item.score == best_score]
        if len(best) > 1:
            provisional.append(_Decision(statement, None, best_score, "ambiguous_best_match"))
        elif best_score < threshold:
            provisional.append(_Decision(statement, None, best_score, "below_confidence_threshold"))
        else:
            provisional.append(_Decision(statement, best[0], best_score, None))
    chosen_counts = Counter(
        decision.candidate.line.id for decision in provisional if decision.candidate is not None
    )
    return [
        _Decision(
            decision.statement,
            None,
            decision.best_score,
            "candidate_conflict",
        )
        if decision.candidate is not None and chosen_counts[decision.candidate.line.id] > 1
        else decision
        for decision in provisional
    ]


def _unmatched(decision: _Decision, journal: Journal) -> UnmatchedStatementLineItem:
    statement = decision.statement
    if decision.reason is None:
        raise AssertionError("matched decisions cannot become unmatched items")
    return UnmatchedStatementLineItem(
        statement_line_id=statement.id,
        date=statement.date,
        amount=statement.amount,
        currency_id=None if journal.currency is None else journal.currency.id,
        currency_name="Company currency" if journal.currency is None else journal.currency.name,
        partner_id=None if statement.partner is None else statement.partner.id,
        partner_name=None if statement.partner is None else statement.partner.name,
        reference=statement.payment_reference,
        best_rejected_score=decision.best_score,
        reason_code=decision.reason,
    )


def _markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _proposal_artifact(
    request: ReconciliationInput,
    company_name: str,
    matches: list[ReconciliationMatch],
    unmatched: list[UnmatchedStatementLineItem],
    summary: ReconciliationSummary,
    request_id: str,
) -> str:
    rows = [
        "# Bank Reconciliation Proposal",
        "",
        f"- Period: {request.period}",
        f"- Journal ID: {request.bank_journal_id}",
        f"- Company: {_markdown(company_name)} ({request.company_id})",
        f"- Audit reference: `{request_id}`",
        f"- Statement lines reviewed: {summary.statement_line_count}",
        "",
        "## Proposed Matches",
        "",
        "| Statement line | Move line | Amount | Confidence |",
        "|---:|---:|---:|---:|",
    ]
    rows.extend(
        f"| {item.statement_line_id} | {item.move_line_id} | {item.amount} | {item.confidence} |"
        for item in matches
    )
    rows.extend(
        [
            "",
            "## Unmatched Lines",
            "",
            "| Statement line | Amount | Reason | Best score |",
            "|---:|---:|---|---:|",
        ]
    )
    rows.extend(
        f"| {item.statement_line_id} | {item.amount} | {item.reason_code} | "
        f"{'' if item.best_rejected_score is None else item.best_rejected_score} |"
        for item in unmatched
    )
    rows.extend(
        [
            "",
            "## Risk Flags",
            "",
            "- Proposal only; no Odoo reconciliation state is changed.",
            "- Ambiguous, conflicting, and below-threshold candidates remain unmatched.",
            "",
            "## Approval Checklist",
            "",
            "- Verify each amount, partner, reference, date, and confidence score in Odoo.",
            "- Complete any final reconciliation directly through an authorized Odoo workflow.",
            "",
            f"Totals: matched {summary.matched_count} ({summary.matched_amount}); "
            f"unmatched {summary.unmatched_count} ({summary.unmatched_amount}).",
        ]
    )
    return "\n".join(rows)


def _period(value: str) -> DatePeriod:
    year, month = (int(part) for part in value.split("-", 1))
    return DatePeriod(
        start=date(year, month, 1),
        end=date(year, month, calendar.monthrange(year, month)[1]),
    )


async def build_reconciliation_proposal(
    adapter: OdooAdapter,
    request: ReconciliationInput,
    *,
    company_name: str,
    request_id: str,
) -> ReconciliationProposal:
    statement_lines = await _statement_lines(
        adapter,
        request.company_id,
        _period(request.period),
        request.bank_journal_id,
    )
    journals = await _journals(adapter, request.company_id)
    journal = journals.get(request.bank_journal_id)
    if journal is None or journal.journal_type not in {"bank", "cash"}:
        raise OdooMcpError(
            ErrorCode.JOURNAL_NOT_FOUND,
            "The requested bank journal is unavailable.",
            "Use a cash or bank journal available to the authorized company.",
        )
    by_id = {item.id: item for item in statement_lines}
    missing = set(request.statement_line_ids) - set(by_id)
    if missing:
        raise OdooMcpError(
            ErrorCode.STATEMENT_LINE_NOT_FOUND,
            "One or more requested bank statement lines are unavailable.",
            "Use unreconciled statement line IDs from the selected period and journal.",
        )
    selected = [by_id[identifier] for identifier in request.statement_line_ids]
    if any(item.reconciled for item in selected):
        raise OdooMcpError(
            ErrorCode.ODOO_STATE_CONFLICT,
            "One or more bank statement lines are already reconciled.",
            "Refresh the statement lines and submit only unreconciled records.",
        )
    decisions = _decisions(
        selected,
        await _candidate_lines(adapter, request.company_id),
        request.match_confidence_threshold,
        journals,
    )
    matches = [
        ReconciliationMatch(
            statement_line_id=decision.statement.id,
            move_line_id=decision.candidate.line.id,
            amount=decision.statement.amount,
            currency_id=None if journal.currency is None else journal.currency.id,
            currency_name="Company currency" if journal.currency is None else journal.currency.name,
            confidence=decision.candidate.score,
            score_components=decision.candidate.components,
        )
        for decision in decisions
        if decision.candidate is not None
    ]
    unmatched = [
        _unmatched(decision, journal) for decision in decisions if decision.candidate is None
    ]
    summary = ReconciliationSummary(
        statement_line_count=len(selected),
        matched_count=len(matches),
        unmatched_count=len(unmatched),
        currency_id=None if journal.currency is None else journal.currency.id,
        currency_name="Company currency" if journal.currency is None else journal.currency.name,
        matched_amount=sum(
            (decision.statement.amount for decision in decisions if decision.candidate is not None),
            _ZERO,
        ),
        unmatched_amount=sum((item.amount for item in unmatched), _ZERO),
    )
    return ReconciliationProposal(
        company_id=request.company_id,
        period=request.period,
        bank_journal_id=request.bank_journal_id,
        matches=matches,
        unmatched=unmatched,
        summary=summary,
        risk_flags=[
            "proposal_only",
            "no_final_reconciliation",
            *(["manual_review_required"] if unmatched else []),
        ],
        artifact_markdown=_proposal_artifact(
            request, company_name, matches, unmatched, summary, request_id
        ),
    )


def _cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        version, raw_offset = (
            base64.b64decode(padded, altchars=b"-_", validate=True).decode().split(":", 1)
        )
        offset = int(raw_offset)
    except (ValueError, UnicodeError, binascii.Error):
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor is invalid.",
            "Restart the unmatched-line report without a cursor.",
        ) from None
    if version != "v1" or offset <= 0:
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor is invalid.",
            "Restart the unmatched-line report without a cursor.",
        )
    return offset


def _next_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"v1:{offset}".encode()).decode().rstrip("=")


async def flag_unmatched_statement_lines(
    adapter: OdooAdapter,
    request: UnmatchedStatementLinesInput,
    *,
    company_name: str,
    request_id: str,
) -> UnmatchedStatementLinesResponse:
    statements = [
        item
        for item in await _statement_lines(
            adapter,
            request.company_id,
            DatePeriod(start=request.period_start, end=request.period_end),
            request.journal_id,
        )
        if not item.reconciled
    ]
    journals = await _journals(adapter, request.company_id)
    decisions = _decisions(
        statements,
        await _candidate_lines(adapter, request.company_id),
        request.match_confidence_threshold,
        journals,
    )
    all_items = [
        _unmatched(item, journals[item.statement.journal.id])
        for item in decisions
        if item.candidate is None
    ]
    all_items.sort(key=lambda item: (item.date, item.statement_line_id))
    counts = Counter(item.reason_code for item in all_items)
    summary = UnmatchedStatementLinesSummary(
        statement_line_count=len(statements),
        unmatched_count=len(all_items),
        no_candidate_count=counts["no_eligible_candidate"],
        below_threshold_count=counts["below_confidence_threshold"],
        ambiguous_count=counts["ambiguous_best_match"],
        conflict_count=counts["candidate_conflict"],
    )
    offset = _cursor_offset(request.cursor)
    if offset > len(all_items):
        raise OdooMcpError(
            ErrorCode.INVALID_INPUT,
            "The pagination cursor is outside this unmatched-line report.",
            "Restart the report without a cursor.",
        )
    items = all_items[offset : offset + request.limit]
    next_offset = offset + len(items)
    next_cursor = _next_cursor(next_offset) if next_offset < len(all_items) else None
    first = offset + 1 if items else 0
    last = offset + len(items) if items else 0
    artifact_rows = [
        "# Unmatched Bank Statement Lines",
        "",
        f"- Period: {request.period_start.isoformat()} to {request.period_end.isoformat()}",
        f"- Company: {_markdown(company_name)} ({request.company_id})",
        f"- Audit reference: `{request_id}`",
        f"- Page rows: {first}-{last} of {summary.unmatched_count}",
        "- Continuation: more rows are available through `next_cursor`"
        if next_cursor is not None
        else "- Continuation: complete",
        "",
        "| Date | Statement line | Amount | Reference | Reason | Best score |",
        "|---|---:|---:|---|---|---:|",
    ]
    artifact_rows.extend(
        f"| {item.date.isoformat()} | {item.statement_line_id} | {item.amount} | "
        f"{_markdown(item.reference or '')} | {item.reason_code} | "
        f"{'' if item.best_rejected_score is None else item.best_rejected_score} |"
        for item in items
    )
    artifact_rows.extend(
        [
            "",
            f"Whole-report totals: reviewed {summary.statement_line_count}; "
            f"unmatched {summary.unmatched_count}.",
        ]
    )
    return UnmatchedStatementLinesResponse(
        request_id=request_id,
        company_id=request.company_id,
        period_start=request.period_start,
        period_end=request.period_end,
        items=items,
        next_cursor=next_cursor,
        summary=summary,
        artifact_markdown="\n".join(artifact_rows),
    )
