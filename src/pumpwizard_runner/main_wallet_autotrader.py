"""Prepare the next exact Autopilot order for an active signing runtime.

The browser or encrypted background service owns the signer.  This scheduler
reconciles an existing signature first, checks managed exits before entries, and
delegates every quote, simulation, risk check and durable review to
``AgentTradeService``.
"""

from __future__ import annotations

import json
import time

from .agent_execution import AgentTradeService, cancel
from .manual_trades import PREVIEW_TTL
from .portfolio import CONFIG
from .transfers import TransferError, WithdrawalService


def _reviews(journal, session_id: str, sender: str, genesis: str, side: str) -> list[dict]:
    rows = journal.db.execute(
        "SELECT o.id,p.payload FROM agent_orders o JOIN trade_previews p ON p.id=o.id "
        "WHERE o.agent_id=? AND o.side=? AND o.status='review' ORDER BY o.created_at,o.id",
        (session_id, side),
    )
    result = []
    for row in rows:
        preview = json.loads(row["payload"])
        if (preview.get("sender"), preview.get("genesis")) == (sender, genesis):
            result.append(preview)
    return result


def _exit_candidates(journal, session_id: str, sender: str, genesis: str) -> list[dict]:
    return [
        {"mint": row["mint"], "position_id": row["id"]}
        for row in journal.db.execute(
            "SELECT p.mint,p.id FROM agent_positions a "
            "JOIN trade_positions p ON p.id=a.position_id "
            "JOIN agent_reservations h ON h.id=a.token_hold "
            "WHERE a.agent_id=? AND p.sender=? AND p.genesis=? AND p.status='open' "
            "AND h.status='held' ORDER BY p.opened_at,p.id",
            (session_id, sender, genesis),
        )
    ]


def _entry_candidates(library, journal, session_id: str) -> list[dict]:
    attempted = {
        row[0]
        for row in journal.db.execute(
            "SELECT mint FROM agent_orders WHERE agent_id=? AND side='buy' "
            "AND status IN ('review','submitted','finalized','failed')",
            (session_id,),
        )
    }
    result, seen = [], set()
    now = time.time()
    for decision in library.decisions(session_id, 1000):
        mint = decision["mint"]
        if mint in seen:
            continue
        seen.add(mint)
        if (
            mint not in attempted
            and decision["kind"] == "entry_recommended"
            and 0 <= now - decision["at"] <= CONFIG["entry_expiry_seconds"]
        ):
            result.append({"mint": mint, "position_id": None})
    return result


def _issue_review(manager, library, journal, allowance_id: str, runtime_token: str,
                  service: AgentTradeService, previews: list[dict]) -> tuple[dict | None, list[str]]:
    blocked = []
    for preview in previews:
        if time.time() >= preview.get("created_at", 0) + PREVIEW_TTL:
            try:
                cancel(journal, preview["id"])
            except TransferError as exc:
                blocked.append(str(exc))
            continue
        try:
            service.validate(preview)
            return manager.issue_ticket(
                library, journal, allowance_id, runtime_token, preview,
            ), blocked
        except (TransferError, ValueError, KeyError, TypeError) as exc:
            blocked.append(str(exc)[:350])
            try:
                cancel(journal, preview["id"])
            except TransferError:
                pass
    return None, blocked


def _prepare_candidate(manager, library, journal, allowance_id: str, runtime_token: str,
                       service: AgentTradeService, session_id: str, sender: str,
                       cash_hold: str, slippage_bps: int,
                       candidates: list[dict]) -> tuple[dict | None, list[str]]:
    blocked = []
    for item in candidates:
        preview = None
        try:
            preview = service.prepare(
                session_id,
                sender,
                item["mint"],
                cash_hold,
                position_id=item["position_id"],
                slippage_bps=slippage_bps,
            )
            return manager.issue_ticket(
                library, journal, allowance_id, runtime_token, preview,
            ), blocked
        except (TransferError, ValueError, KeyError, TypeError) as exc:
            blocked.append(str(exc)[:350])
            # ``prepare`` durably reserves the cash/token hold before ticket
            # issuance revalidates the allowance.  Release only this newly
            # created unsigned review; submitted intent must remain recoverable.
            if preview is not None:
                try:
                    cancel(journal, preview["id"])
                except (TransferError, KeyError, TypeError):
                    pass
    return None, blocked


def prepare_next_ticket(library, journal, rpc, manager, allowance_id: str,
                        runtime_token: str) -> dict[str, object]:
    """Return one exit-first exact ticket or a non-authorizing wait reason."""
    row = journal.db.execute(
        "SELECT * FROM main_wallet_allowances WHERE id=?", (allowance_id,),
    ).fetchone()
    if row is None:
        raise TransferError("Main-wallet allowance was not found")
    existing = manager.next_ticket(allowance_id, runtime_token)
    if existing is not None:
        preview = journal.previews(existing['order_id'])
        if row['entries_paused'] and preview['side'] == 'buy':
            manager.complete_ticket(allowance_id, existing['id'])
        else:
            return {"ticket": existing, "detail": "Exact reviewed transaction ready"}
    scope = json.loads(row["scope"])
    permissions = json.loads(row["granted_permissions"])
    session_id, sender, genesis = row["session_id"], row["sender"], row["genesis"]
    service = AgentTradeService(library, rpc, journal)

    # An uncertain original signature always owns the wallet until finality is
    # recovered.  No replacement review or ticket is created in that state.
    if journal.pending(sender, genesis):
        service.manual.reconcile(sender)
        WithdrawalService(rpc, journal).reconcile(sender)
        if journal.pending(sender, genesis):
            return {
                "ticket": None,
                "detail": "Original signature is unresolved; no replacement will be signed",
            }

    blocked: list[str] = []
    if permissions.get("automatic_protective_exits"):
        ticket, reasons = _issue_review(
            manager, library, journal, allowance_id, runtime_token, service,
            _reviews(journal, session_id, sender, genesis, "sell"),
        )
        blocked.extend(reasons)
        if ticket is not None:
            return {"ticket": ticket, "detail": "Eligible managed exit is ready"}
        ticket, reasons = _prepare_candidate(
            manager, library, journal, allowance_id, runtime_token, service,
            session_id, sender, scope["cash_hold"], scope["slippage_bps"],
            _exit_candidates(journal, session_id, sender, genesis),
        )
        blocked.extend(reasons)
        if ticket is not None:
            return {"ticket": ticket, "detail": "Eligible managed exit is ready"}

    entries_allowed = (
        permissions.get("automatic_entries") is True
        and row["status"] == "active"
        and not bool(row["entries_paused"])
    )
    if entries_allowed:
        from .portfolio_risk import refresh
        from .wallet_risk import policy

        limits = policy(journal, genesis, sender)
        if limits and limits.get("portfolio_drawdown_lamports") is not None:
            valuation = None
            try:
                valuation = refresh(journal, rpc, sender)
            except TransferError as exc:
                blocked.append(str(exc)[:350])
            else:
                if valuation["status"] != "ready":
                    blocked.append(
                        (valuation.get("detail") or "Portfolio valuation is not executable")[:350]
                    )
                    valuation = None
            if valuation is not None:
                ticket, reasons = _issue_review(
                    manager, library, journal, allowance_id, runtime_token, service,
                    _reviews(journal, session_id, sender, genesis, "buy"),
                )
                blocked.extend(reasons)
                if ticket is not None:
                    return {"ticket": ticket, "detail": "Eligible strategy entry is ready"}
                ticket, reasons = _prepare_candidate(
                    manager, library, journal, allowance_id, runtime_token, service,
                    session_id, sender, scope["cash_hold"], scope["slippage_bps"],
                    _entry_candidates(library, journal, session_id),
                )
                blocked.extend(reasons)
                if ticket is not None:
                    return {"ticket": ticket, "detail": "Eligible strategy entry is ready"}

    if blocked:
        return {"ticket": None, "detail": "No executable order: " + blocked[0]}
    if row["status"] == "entry-paused" or not permissions.get("automatic_entries"):
        return {"ticket": None, "detail": "No eligible managed exit; automatic entries are off"}
    return {"ticket": None, "detail": "No eligible managed exit or fresh strategy entry"}
