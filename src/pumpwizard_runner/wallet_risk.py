"""Local wallet entry limits, shared by all agents and manual trading.

Amounts are lamports. Exposure means remaining recorded cost. Realized metrics
use finalized receipts; the separate portfolio gate adds conservative unsigned
liquidation values. Deposits and withdrawals are not trading profits or losses.
"""

import json
import time

from .pump_route import MAX_ACCOUNT_FUNDING
from .transfers import TransferError, _uint, public_key, sol_text


SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_risk_policies (
    genesis TEXT NOT NULL, sender TEXT NOT NULL, payload TEXT NOT NULL,
    updated_at REAL NOT NULL, PRIMARY KEY(genesis,sender)
);
CREATE TABLE IF NOT EXISTS wallet_risk_changes (
    id INTEGER PRIMARY KEY, genesis TEXT NOT NULL, sender TEXT NOT NULL,
    payload TEXT NOT NULL, changed_at REAL NOT NULL
);
"""

FIELDS = {
    "max_buy_lamports": "Maximum buy cost",
    "max_exposure_lamports": "Maximum open cost",
    "reserve_lamports": "SOL reserve after buys",
    "daily_loss_lamports": "Daily realized loss (UTC, gross)",
    "drawdown_lamports": "Realized P/L drawdown",
    "portfolio_drawdown_lamports": "Portfolio drawdown including open positions",
}


def policy(journal, genesis, sender):
    # Read-only assessments can inspect journals from before this feature.
    if not journal.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wallet_risk_policies'"
    ).fetchone():
        return None
    row = journal.db.execute(
        "SELECT payload FROM wallet_risk_policies WHERE genesis=? AND sender=?", (genesis, sender)
    ).fetchone()
    return json.loads(row[0]) if row else None


def save_policy(journal, genesis, sender, values):
    public_key(sender)
    if not isinstance(genesis, str) or not genesis:
        raise TransferError("Select a verified network")
    if set(values) != set(FIELDS) | {"entries_paused"}:
        raise TransferError("Supply every wallet risk limit")
    for name, label in FIELDS.items():
        _uint(values[name], label)
        if not values[name] and name != "reserve_lamports":
            raise TransferError(f"{label} must be greater than zero")
    if type(values["entries_paused"]) is not bool:
        raise TransferError("Entry pause must be true or false")
    if values["max_buy_lamports"] > values["max_exposure_lamports"]:
        raise TransferError("Maximum buy cost cannot exceed maximum open cost")
    encoded = json.dumps(values, sort_keys=True)
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        journal.db.execute(
            "INSERT OR REPLACE INTO wallet_risk_policies VALUES (?,?,?,?)",
            (genesis, sender, encoded, time.time()),
        )
        journal.db.execute(
            "INSERT INTO wallet_risk_changes(genesis,sender,payload,changed_at) VALUES (?,?,?,?)",
            (genesis, sender, encoded, time.time()),
        )
        # Invalidates any in-flight balance/check snapshot before broadcasting.
        journal.db.execute(
            "INSERT INTO wallet_revisions VALUES (?,?,1) ON CONFLICT(genesis,sender) "
            "DO UPDATE SET revision=revision+1",
            (genesis, sender),
        )
    return dict(values)


def metrics(journal, genesis, sender, *, now=None, exclude_order=None):
    """Derive exact metrics from existing receipts, including pre-feature history.

    Journal insertion order orders receipts because the wallet permits one unresolved
    operation at a time. Cost allocation matches TradeJournal.settle exactly.
    No history reset or approximate backfill is needed on policy activation.
    """
    now = time.time() if now is None else now
    day = int(now // 86400) * 86400
    lots = {}
    pnl = peak = daily_loss = pending_cost = 0
    for row in journal.db.execute(
        "SELECT * FROM trades WHERE genesis=? AND sender=? ORDER BY rowid",
        (genesis, sender),
    ):
        p = json.loads(row["payload"])
        if row["status"] in ("pending", "unknown", "confirmed"):
            if row["id"] != exclude_order and p["side"] == "buy":
                pending_cost += p["amount"] + p["fee"] + MAX_ACCOUNT_FUNDING
            continue
        if row["status"] not in ("finalized", "failed") or not row["receipt"]:
            raise TransferError("Wallet risk accounting needs finalized trade receipts")
        receipt = json.loads(row["receipt"])
        cash, tokens = receipt["wallet_delta"], receipt["token_delta"]
        realized = cash if receipt["err"] is not None else 0
        if receipt["err"] is None:
            if p["side"] == "buy":
                lots[row["id"]] = (tokens, -cash)
            else:
                held, cost = lots.get(p["position_id"], (0, 0))
                sold = -tokens
                if not 0 < sold <= held:
                    raise TransferError("Wallet risk accounting cannot reconcile a sold position")
                allocated = cost if sold == held else cost * sold // held
                lots[p["position_id"]] = (held - sold, cost - allocated)
                realized = cash - allocated
        pnl += realized
        peak = max(peak, pnl)
        if day <= receipt["block_time"] < day + 86400:
            daily_loss += max(0, -realized)
    reviewed_cost = 0
    for row in journal.db.execute(
        "SELECT o.id,p.payload FROM agent_orders o JOIN trade_previews p ON p.id=o.id "
        "WHERE o.status='review' AND o.side='buy'"
    ):
        p = json.loads(row["payload"])
        if (p["genesis"], p["sender"]) == (genesis, sender) and row["id"] != exclude_order:
            reviewed_cost += p["amount"] + p["fee"] + MAX_ACCOUNT_FUNDING
    open_cost = sum(
        int(row[0])
        for row in journal.db.execute(
            "SELECT remaining_cost FROM trade_positions WHERE genesis=? AND sender=? AND status='open'",
            (genesis, sender),
        )
    )
    return dict(
        open_cost=open_cost,
        committed_cost=pending_cost + reviewed_cost,
        daily_loss=daily_loss,
        realized_pnl=pnl,
        realized_peak=peak,
        realized_drawdown=peak - pnl,
        utc_day=time.strftime("%Y-%m-%d", time.gmtime(now)),
    )


def entry_blocks(limits, values):
    reasons = []
    if limits["entries_paused"]:
        reasons.append("Wallet entries are paused; exits remain available")
    for measured, limit, label in (
        ("daily_loss", "daily_loss_lamports", "Daily realized loss limit"),
        ("realized_drawdown", "drawdown_lamports", "Realized P/L drawdown limit"),
    ):
        if values[measured] >= limits[limit]:
            reasons.append(f"{label} reached ({sol_text(values[measured])} SOL)")
    return reasons


def check_buy(journal, preview, state, *, limits=None):
    # An entry circuit breaker must never prevent a risk-reducing exit.
    if preview["side"] != "buy":
        return
    limits = limits if limits is not None else policy(journal, preview["genesis"], preview["sender"])
    if limits is None:
        return  # Existing guided trading remains available; UI reports unconfigured.
    values = metrics(journal, preview["genesis"], preview["sender"], exclude_order=preview["id"])
    reasons = entry_blocks(limits, values)
    if limits.get("portfolio_drawdown_lamports") is not None:
        from .portfolio_risk import current

        portfolio = current(journal, preview["genesis"], preview["sender"],
                            wallet_lamports=state["wallet_lamports"])
        if portfolio["drawdown"] >= limits["portfolio_drawdown_lamports"]:
            reasons.append("Portfolio drawdown limit reached (including unrealized losses)")
    debit = preview["amount"] + preview["fee"] + MAX_ACCOUNT_FUNDING
    if debit > limits["max_buy_lamports"]:
        reasons.append("Buy including fee and account funding exceeds maximum buy cost")
    if values["open_cost"] + values["committed_cost"] + debit > limits["max_exposure_lamports"]:
        reasons.append("Open positions and committed buys exceed maximum open cost")
    if state["wallet_lamports"] - debit < limits["reserve_lamports"]:
        reasons.append("Buy would leave less than the configured SOL reserve")
    if reasons:
        raise TransferError("Wallet risk: " + "; ".join(reasons))


def status(journal, genesis, sender):
    limits = policy(journal, genesis, sender)
    values = metrics(journal, genesis, sender)
    reasons = entry_blocks(limits, values) if limits else []
    from .portfolio_risk import current, saved

    portfolio = saved(journal, genesis, sender)
    if limits and limits.get("portfolio_drawdown_lamports") is not None:
        try:
            portfolio = current(journal, genesis, sender)
            if portfolio["drawdown"] >= limits["portfolio_drawdown_lamports"]:
                reasons.append("Portfolio drawdown limit reached (including unrealized losses)")
        except TransferError as exc:
            reasons.append(str(exc))
    return dict(policy=limits, metrics=values, entry_blocks=reasons, portfolio=portfolio)
