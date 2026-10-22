"""Unsigned liquidation evidence for all locally recorded wallet positions.

Trading P/L, rather than wallet balance, makes deposits and withdrawals neutral.
Evidence lives separately from financial journals, including in shadow mode.
"""

import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .manual_trades import encode, simulate_swap
from .pump_route import MAX_ACCOUNT_FUNDING, MAX_FEE, build_message, read_state
from .reservations import wallet_revision
from .transfers import TransferError, _uint

VERSION = "wallet-net-portfolio-v1"
TTL = 30
SLIPPAGE_BPS = 500


def path(journal):
    return (
        Path(journal.db.execute("PRAGMA database_list").fetchone()[2]).parent
        / "portfolio-risk.sqlite"
    )


def basis(journal, genesis, sender):
    from .wallet_risk import metrics

    positions = sorted(
        [
            dict(id=p["id"], mint=p["mint"], quantity=p["quantity"], cost=p["remaining_cost"])
            for p in journal.positions(sender, genesis)
            if p["status"] == "open"
        ],
        key=lambda p: p["id"],
    )
    values = metrics(journal, genesis, sender)
    state = dict(
        positions=positions,
        realized_pnl=values["realized_pnl"],
        realized_peak=values["realized_peak"],
        revision=wallet_revision(journal, genesis, sender),
    )
    return state, hashlib.sha256(encode(state).encode()).hexdigest()


def saved(journal, genesis, sender):
    location = path(journal)
    if not location.exists():
        return None
    db = sqlite3.connect(location.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        row = db.execute(
            "SELECT payload FROM portfolios WHERE genesis=? AND sender=?", (genesis, sender)
        ).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        db.close()


def current(journal, genesis, sender, *, wallet_lamports=None):
    report = saved(journal, genesis, sender)
    if report is None:
        raise TransferError("Portfolio valuation unavailable; refresh unsigned wallet valuation")
    if report["status"] != "ready":
        raise TransferError("Portfolio valuation blocked: " + report["detail"])
    if report["version"] != VERSION or not 0 <= time.time() - report["started_at"] <= TTL:
        raise TransferError("Portfolio valuation stale; refresh unsigned wallet valuation")
    if basis(journal, genesis, sender)[1] != report["basis"] or journal.pending(sender, genesis):
        raise TransferError("Portfolio changed or has unresolved operations; refresh valuation")
    if (
        wallet_lamports is not None
        and report["wallet_lamports"] is not None
        and report["wallet_lamports"] != wallet_lamports
    ):
        raise TransferError("Wallet balance changed during portfolio valuation")
    return report


def _quote(rpc, sender, mint, quantity):
    started = time.time()
    state = read_state(rpc, sender, mint, "sell")
    if state["token_atoms"] < quantity:
        raise TransferError("Wallet tokens are below recorded holdings")
    block = rpc.call(
        "getLatestBlockhash", [{"commitment": "confirmed", "minContextSlot": state["slot"]}]
    )["value"]
    trial = build_message(state, "sell", quantity, 1, block["blockhash"])
    fee = _uint(
        rpc.call(
            "getFeeForMessage", [base64.b64encode(trial).decode(), {"commitment": "confirmed"}]
        )["value"],
        "fee",
    )
    if fee > MAX_FEE or state["wallet_lamports"] < fee + MAX_ACCOUNT_FUNDING:
        raise TransferError("Insufficient exit fee reserve or excessive fee")
    preview = dict(sender=sender, mint=mint, side="sell", amount=quantity, fee=fee)
    output, _ = simulate_swap(rpc, preview, state, trial)
    minimum = output * (10_000 - SLIPPAGE_BPS) // 10_000
    if minimum <= 0:
        raise TransferError("No executable minimum sell output")
    message = build_message(state, "sell", quantity, minimum, block["blockhash"])
    output, cash = simulate_swap(rpc, preview, state, message)
    height = _uint(rpc.call("getBlockHeight", [{"commitment": "confirmed"}]), "height")
    if output < minimum or height > _uint(block["lastValidBlockHeight"], "height"):
        raise TransferError("Sell quote moved or expired")
    return dict(
        mint=mint,
        quantity=quantity,
        route=state["route"],
        slot=state["slot"],
        quoted_at=started,
        wallet_lamports=state["wallet_lamports"],
        fee=fee,
        minimum_output=minimum,
        expected_net=cash,
        net_floor=max(0, minimum - fee - MAX_ACCOUNT_FUNDING),
        message_sha256=hashlib.sha256(message).hexdigest(),
    )


def refresh(journal, rpc, sender):
    """Never signs, creates orders, changes allocations or updates wallet journals."""
    from .shadow import UnsignedRpc

    rpc = UnsignedRpc(rpc)
    genesis = rpc.genesis()
    started = time.time()
    state, fingerprint = basis(journal, genesis, sender)
    report = dict(
        version=VERSION,
        genesis=genesis,
        sender=sender,
        started_at=started,
        basis=fingerprint,
        status="blocked",
        detail="",
        quotes=[],
        wallet_lamports=None,
        slippage_bps=SLIPPAGE_BPS,
    )
    try:
        if journal.pending(sender, genesis):
            raise TransferError("Recover unresolved wallet operations first")
        groups = {}
        for position in state["positions"]:
            groups[position["mint"]] = groups.get(position["mint"], 0) + int(position["quantity"])
        # Aggregate same-mint lots: independent quotes would overvalue liquidity.
        for mint, quantity in sorted(groups.items()):
            quote = _quote(rpc, sender, mint, quantity)
            if report["wallet_lamports"] not in (None, quote["wallet_lamports"]):
                raise TransferError("Wallet balance changed during valuation")
            report["wallet_lamports"] = quote["wallet_lamports"]
            report["quotes"].append(quote)
        if basis(journal, genesis, sender)[1] != fingerprint or journal.pending(sender, genesis):
            raise TransferError("Portfolio changed during valuation")
        if not 0 <= time.time() - started <= TTL:
            raise TransferError("Portfolio valuation expired during collection")
        unrealized = sum(q["net_floor"] for q in report["quotes"]) - sum(
            int(p["cost"]) for p in state["positions"]
        )
        report.update(
            status="ready",
            unrealized_pnl=unrealized,
            total_pnl=state["realized_pnl"] + unrealized,
            net_liquidation=sum(q["net_floor"] for q in report["quotes"]),
        )
    except (TransferError, ValueError, KeyError, TypeError) as exc:
        report["detail"] = str(exc)
    db = sqlite3.connect(path(journal), timeout=10)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS portfolios (genesis TEXT, sender TEXT, peak TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(genesis,sender))"
        )
        with db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT peak,payload FROM portfolios WHERE genesis=? AND sender=?",
                (genesis, sender),
            ).fetchone()
            if previous and json.loads(previous[1])["started_at"] > started:
                raise TransferError("A newer portfolio valuation exists; refresh")
            peak = max(0, state["realized_peak"], int(previous[0]) if previous else 0)
            if report["status"] == "ready":
                peak = max(peak, report["total_pnl"])
                report["drawdown"] = peak - report["total_pnl"]
            report["peak"] = peak
            db.execute(
                "INSERT OR REPLACE INTO portfolios VALUES (?,?,?,?)",
                (genesis, sender, str(peak), encode(report)),
            )
    finally:
        db.close()
    return report
