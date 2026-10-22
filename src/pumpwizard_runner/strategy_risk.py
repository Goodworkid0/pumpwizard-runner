"""Per-strategy wallet allocation and execution limits.

The main wallet remains one on-chain address. These policies partition its local
cash reservations between strategy instances and add stricter entry/exit limits
to the reviewed agent order path. The existing reviewed handoff can release a
managed position for manual wallet exits.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import time
import uuid

from .pump_route import MAX_ACCOUNT_FUNDING
from .transfers import TransferError, _uint


SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_risk_policies (
    agent_id TEXT NOT NULL, genesis TEXT NOT NULL, sender TEXT NOT NULL,
    payload TEXT NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY(agent_id,genesis,sender)
);
CREATE TABLE IF NOT EXISTS strategy_risk_changes (
    id INTEGER PRIMARY KEY, agent_id TEXT NOT NULL, genesis TEXT NOT NULL,
    sender TEXT NOT NULL, payload TEXT NOT NULL, changed_at REAL NOT NULL
);
"""

POLICY_FIELDS = {
    "allocation_lamports",
    "buys_enabled",
    "sells_enabled",
    "min_trade_lamports",
    "max_trade_lamports",
    "max_daily_buy_lamports",
    "max_trades_per_day",
    "max_daily_loss_lamports",
    "max_daily_loss_bps",
    "max_trade_loss_bps",
    "max_open_positions",
}


def _percent_bps(value: object, label: str) -> int:
    try:
        percent = Decimal(str(value).strip())
        raw = percent * 100
        if not percent.is_finite() or raw != raw.to_integral_value() or not 0 < raw <= 10_000:
            raise ValueError
        return int(raw)
    except (InvalidOperation, ValueError, AttributeError):
        raise TransferError(f"{label} must be 0.01–100% with at most 2 decimals") from None


def parse_browser_policy(body: object) -> dict:
    fields = {
        "allocation_sol", "buys_enabled", "sells_enabled", "min_trade_sol",
        "max_trade_sol", "max_daily_buy_sol", "max_trades_per_day",
        "max_daily_loss_sol", "max_daily_loss_percent", "max_trade_loss_percent",
        "max_open_positions",
    }
    if not isinstance(body, dict) or set(body) != fields:
        raise TransferError("Supply every strategy allocation and risk field")
    for name in ("buys_enabled", "sells_enabled"):
        if type(body[name]) is not bool:
            raise TransferError("Buy and sell controls must be enabled or disabled")
    for name in ("max_trades_per_day", "max_open_positions"):
        if type(body[name]) is not int:
            raise TransferError("Daily trades and open positions must be whole numbers")
    if not 1 <= body["max_trades_per_day"] <= 100:
        raise TransferError("Maximum trades per day must be 1–100")
    if not 1 <= body["max_open_positions"] <= 25:
        raise TransferError("Maximum open positions must be 1–25")
    from .transfers import parse_sol

    policy = {
        "allocation_lamports": parse_sol(body["allocation_sol"]),
        "buys_enabled": body["buys_enabled"],
        "sells_enabled": body["sells_enabled"],
        "min_trade_lamports": parse_sol(body["min_trade_sol"]),
        "max_trade_lamports": parse_sol(body["max_trade_sol"]),
        "max_daily_buy_lamports": parse_sol(body["max_daily_buy_sol"]),
        "max_trades_per_day": body["max_trades_per_day"],
        "max_daily_loss_lamports": parse_sol(body["max_daily_loss_sol"]),
        "max_daily_loss_bps": _percent_bps(body["max_daily_loss_percent"], "Daily loss percentage"),
        "max_trade_loss_bps": _percent_bps(body["max_trade_loss_percent"], "Per-trade loss percentage"),
        "max_open_positions": body["max_open_positions"],
    }
    validate(policy)
    return policy


def validate(values: object) -> dict:
    if not isinstance(values, dict) or set(values) != POLICY_FIELDS:
        raise TransferError("Supply every strategy risk limit")
    for field in (
        "allocation_lamports", "min_trade_lamports", "max_trade_lamports",
        "max_daily_buy_lamports", "max_daily_loss_lamports",
    ):
        _uint(values[field], field)
        if values[field] <= 0:
            raise TransferError("Strategy SOL limits must be greater than zero")
    for field in ("max_daily_loss_bps", "max_trade_loss_bps"):
        if type(values[field]) is not int or not 1 <= values[field] <= 10_000:
            raise TransferError("Strategy percentage limits must be 1–10,000 basis points")
    for field, maximum in (("max_trades_per_day", 100), ("max_open_positions", 25)):
        if type(values[field]) is not int or not 1 <= values[field] <= maximum:
            raise TransferError("Strategy count limits are outside the supported range")
    if type(values["buys_enabled"]) is not bool or type(values["sells_enabled"]) is not bool:
        raise TransferError("Buy and sell controls must be enabled or disabled")
    if values["min_trade_lamports"] > values["max_trade_lamports"]:
        raise TransferError("Minimum trade cannot exceed maximum trade")
    if values["max_trade_lamports"] > values["allocation_lamports"]:
        raise TransferError("Maximum trade cannot exceed the strategy allocation")
    if values["max_daily_buy_lamports"] > values["allocation_lamports"]:
        raise TransferError("Daily buy cap cannot exceed the strategy allocation")
    if values["max_daily_loss_lamports"] > values["allocation_lamports"]:
        raise TransferError("Daily loss cap cannot exceed the strategy allocation")
    return dict(values)


def policy(journal, agent_id: str, genesis: str, sender: str) -> dict | None:
    row = journal.db.execute(
        "SELECT payload FROM strategy_risk_policies WHERE agent_id=? AND genesis=? AND sender=?",
        (agent_id, genesis, sender),
    ).fetchone()
    return json.loads(row[0]) if row else None


def update_trade_loss(journal, agent_id: str, genesis: str, sender: str, basis_points: int) -> bool:
    """Keep an existing reviewed-order policy aligned with the strategy loss exit."""
    current = policy(journal, agent_id, genesis, sender)
    if current is None:
        return False
    if type(basis_points) is not int or not 1 <= basis_points < 10_000:
        raise TransferError("Per-trade loss must be 1–9,999 basis points")
    current["max_trade_loss_bps"] = basis_points
    validate(current)
    encoded = json.dumps(current, sort_keys=True, separators=(",", ":"))
    now = time.time()
    with journal.db:
        journal.db.execute(
            "UPDATE strategy_risk_policies SET payload=?,updated_at=? WHERE agent_id=? AND genesis=? AND sender=?",
            (encoded, now, agent_id, genesis, sender),
        )
        journal.db.execute(
            "INSERT INTO strategy_risk_changes(agent_id,genesis,sender,payload,changed_at) VALUES (?,?,?,?,?)",
            (agent_id, genesis, sender, encoded, now),
        )
        journal.db.execute(
            "INSERT INTO wallet_revisions VALUES (?,?,1) ON CONFLICT(genesis,sender) "
            "DO UPDATE SET revision=revision+1",
            (genesis, sender),
        )
    return True


def _cash_holds(journal, agent_id: str, genesis: str, sender: str) -> list:
    from .session_allocations import cash_holds

    return cash_holds(journal, agent_id, genesis, sender)


def save_policy(journal, agent_id: str, genesis: str, sender: str, values: object, balance: int,
                *, workspace_id: str | None = None) -> dict:
    values = validate(values)
    _uint(balance, "wallet balance")
    now = time.time()
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        if journal.pending(sender, genesis):
            raise TransferError("Resolve the pending wallet operation before changing strategy allocations")
        if journal.db.execute(
            "SELECT 1 FROM agent_orders WHERE agent_id=? AND status IN ('review','submitted')", (agent_id,)
        ).fetchone():
            raise TransferError("Finish or cancel this strategy's active order before changing its limits")
        if journal.db.execute(
            "SELECT 1 FROM signing_sessions WHERE agent_id=? AND sender=? AND genesis=? AND status='active'",
            (agent_id, sender, genesis),
        ).fetchone():
            raise TransferError("Revoke this strategy's signing session before changing its limits")
        if workspace_id is not None:
            from .session_allocations import bind_strategy

            shared = bind_strategy(journal, workspace_id, agent_id, genesis, sender)
            if values["allocation_lamports"] > int(shared["allocation_lamports"]):
                raise TransferError("Strategy allocation cap cannot exceed its session balance")
            holds = _cash_holds(journal, agent_id, genesis, sender)
            if len(holds) != 1 or holds[0]["id"] != shared["cash_hold"]:
                raise TransferError("The trading session needs exactly one durable cash allocation")
            hold_id = str(shared["cash_hold"])
        else:
            other = sum(int(row[0]) for row in journal.db.execute(
                "SELECT lamports FROM agent_reservations WHERE genesis=? AND sender=? AND status='held' "
                "AND position_id IS NULL AND agent_id!=?",
                (genesis, sender, agent_id),
            ))
            if other + values["allocation_lamports"] > balance:
                raise TransferError("Strategy allocations exceed the main wallet's live SOL balance")
            holds = _cash_holds(journal, agent_id, genesis, sender)
            if holds:
                primary = holds[0]
                journal.db.execute(
                    "UPDATE agent_reservations SET lamports=? WHERE id=?",
                    (str(values["allocation_lamports"]), primary["id"]),
                )
                for extra in holds[1:]:
                    journal.db.execute(
                        "UPDATE agent_reservations SET status='released',released_at=? WHERE id=?",
                        (now, extra["id"]),
                    )
                hold_id = primary["id"]
            else:
                hold_id = uuid.uuid4().hex
                journal.db.execute(
                    "INSERT INTO agent_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (hold_id, agent_id, genesis, sender, str(values["allocation_lamports"]),
                     None, None, "0", now, None, "held"),
                )
        journal.db.execute(
            "INSERT OR REPLACE INTO strategy_risk_policies VALUES (?,?,?,?,?)",
            (agent_id, genesis, sender, encoded, now),
        )
        journal.db.execute(
            "INSERT INTO strategy_risk_changes(agent_id,genesis,sender,payload,changed_at) VALUES (?,?,?,?,?)",
            (agent_id, genesis, sender, encoded, now),
        )
        journal.db.execute(
            "INSERT INTO wallet_revisions VALUES (?,?,1) ON CONFLICT(genesis,sender) "
            "DO UPDATE SET revision=revision+1",
            (genesis, sender),
        )
    return {"policy": dict(values), "hold_id": hold_id}


def metrics(journal, agent_id: str, genesis: str, sender: str, *, now: float | None = None,
            exclude_order: str | None = None) -> dict:
    now = time.time() if now is None else now
    day = int(now // 86400) * 86400
    lots: dict[str, tuple[int, int]] = {}
    daily_loss = 0
    for row in journal.db.execute(
        "SELECT f.position_id,f.receipt,t.payload FROM agent_fills f JOIN trades t ON t.id=f.trade_id "
        "WHERE f.agent_id=? AND t.genesis=? AND t.sender=? ORDER BY t.created_at,t.rowid",
        (agent_id, genesis, sender),
    ):
        preview, receipt = json.loads(row["payload"]), json.loads(row["receipt"])
        realized = receipt["wallet_delta"] if receipt["err"] is not None else 0
        if receipt["err"] is None and preview["side"] == "buy":
            lots[row["position_id"]] = (receipt["token_delta"], -receipt["wallet_delta"])
        elif receipt["err"] is None:
            held, cost = lots.get(row["position_id"], (0, 0))
            sold = -receipt["token_delta"]
            allocated = cost if sold == held else cost * sold // held if held else 0
            lots[row["position_id"]] = (held - sold, cost - allocated)
            realized = receipt["wallet_delta"] - allocated
        if day <= receipt["block_time"] < day + 86400:
            daily_loss += max(0, -realized)
    daily_trades = 0
    daily_buy = 0
    for row in journal.db.execute(
        "SELECT o.id,o.side,o.status,o.created_at,p.payload FROM agent_orders o "
        "JOIN trade_previews p ON p.id=o.id WHERE o.agent_id=? AND o.created_at>=? AND o.created_at<?",
        (agent_id, day, day + 86400),
    ):
        if row["id"] == exclude_order or row["status"] == "cancelled":
            continue
        preview = json.loads(row["payload"])
        daily_trades += 1
        if row["side"] == "buy":
            daily_buy += int(preview["amount"])
    open_positions = journal.db.execute(
        "SELECT COUNT(*) FROM agent_positions a JOIN trade_positions p ON p.id=a.position_id "
        "JOIN agent_reservations h ON h.id=a.token_hold "
        "WHERE a.agent_id=? AND p.genesis=? AND p.sender=? AND p.status='open' "
        "AND h.status='held'",
        (agent_id, genesis, sender),
    ).fetchone()[0]
    open_cost = journal.db.execute(
        "SELECT COALESCE(SUM(CAST(p.remaining_cost AS INTEGER)),0) FROM agent_positions a "
        "JOIN trade_positions p ON p.id=a.position_id "
        "JOIN agent_reservations h ON h.id=a.token_hold "
        "WHERE a.agent_id=? AND p.genesis=? AND p.sender=? AND p.status='open' "
        "AND h.status='held'",
        (agent_id, genesis, sender),
    ).fetchone()[0]
    attributed = journal.db.execute(
        "SELECT COUNT(*),COALESCE(SUM(CAST(p.remaining_cost AS INTEGER)),0) "
        "FROM agent_positions a JOIN trade_positions p ON p.id=a.position_id "
        "WHERE a.agent_id=? AND p.genesis=? AND p.sender=? AND p.status='open'",
        (agent_id, genesis, sender),
    ).fetchone()
    holds = _cash_holds(journal, agent_id, genesis, sender)
    return {
        "utc_day": time.strftime("%Y-%m-%d", time.gmtime(now)),
        "daily_trades": daily_trades,
        "daily_buy_lamports": daily_buy,
        "daily_loss_lamports": daily_loss,
        "open_positions": open_positions,
        "open_cost_lamports": int(open_cost),
        "attributed_open_positions": int(attributed[0]),
        "attributed_open_cost_lamports": int(attributed[1]),
        "allocated_lamports": sum(int(row["lamports"]) for row in holds),
        "hold_id": holds[0]["id"] if len(holds) == 1 else None,
    }


def status(journal, agent_id: str, genesis: str, sender: str) -> dict:
    configured = policy(journal, agent_id, genesis, sender)
    values = metrics(journal, agent_id, genesis, sender)
    reasons: list[str] = []
    if configured:
        if not configured["buys_enabled"]:
            reasons.append("Strategy buys are disabled")
        percent_cap = configured["allocation_lamports"] * configured["max_daily_loss_bps"] // 10_000
        if values["daily_loss_lamports"] >= min(configured["max_daily_loss_lamports"], percent_cap):
            reasons.append("Strategy daily loss limit reached")
        if values["daily_trades"] >= configured["max_trades_per_day"]:
            reasons.append("Strategy daily trade limit reached")
        if values["open_positions"] >= configured["max_open_positions"]:
            reasons.append("Strategy open-position limit reached")
    return {"policy": configured, "metrics": values, "entry_blocks": reasons}


def check_preview(journal, preview: dict) -> None:
    context = preview.get("agent_order") or {}
    agent_id = context.get("agent_id")
    if not agent_id:
        return
    configured = policy(journal, agent_id, preview["genesis"], preview["sender"])
    if configured is None:
        return
    values = metrics(
        journal, agent_id, preview["genesis"], preview["sender"], exclude_order=preview["id"]
    )
    if preview["side"] == "sell":
        if not configured["sells_enabled"]:
            raise TransferError("Managed strategy sells are disabled; use the reviewed manual-exit handoff")
        return
    reasons = []
    if not configured["buys_enabled"]:
        reasons.append("strategy buys are disabled")
    if not configured["min_trade_lamports"] <= preview["amount"] <= configured["max_trade_lamports"]:
        reasons.append("buy amount is outside the strategy minimum/maximum")
    if values["daily_buy_lamports"] + preview["amount"] > configured["max_daily_buy_lamports"]:
        reasons.append("daily strategy buy cap would be exceeded")
    if values["daily_trades"] + 1 > configured["max_trades_per_day"]:
        reasons.append("daily strategy trade count would be exceeded")
    if values["open_positions"] >= configured["max_open_positions"]:
        reasons.append("strategy open-position limit reached")
    percent_cap = configured["allocation_lamports"] * configured["max_daily_loss_bps"] // 10_000
    if values["daily_loss_lamports"] >= min(configured["max_daily_loss_lamports"], percent_cap):
        reasons.append("strategy daily loss limit reached")
    holds = _cash_holds(journal, agent_id, preview["genesis"], preview["sender"])
    from .session_allocations import binding, cash_owner

    shared = binding(journal, agent_id, preview["genesis"], preview["sender"])
    expected_owner = cash_owner(journal, agent_id, preview["genesis"], preview["sender"])
    hold = journal.db.execute(
        "SELECT * FROM agent_reservations WHERE id=?", (context.get("cash_hold"),)
    ).fetchone()
    if hold is None or len(holds) != 1 or hold["id"] != holds[0]["id"] or (
        hold["agent_id"], hold["genesis"], hold["sender"], hold["status"]
    ) != (
        expected_owner, preview["genesis"], preview["sender"], "held"
    ) or (shared is None and int(hold["lamports"]) > configured["allocation_lamports"]):
        reasons.append("strategy cash allocation changed or does not match this order")
    debit = preview["amount"] + preview["fee"] + MAX_ACCOUNT_FUNDING
    if hold is None or debit > int(hold["lamports"]):
        reasons.append("session allocation cannot cover spend, fee and account funding")
    if values["open_cost_lamports"] + debit > configured["allocation_lamports"]:
        reasons.append("strategy exposure cap cannot cover this position")
    if reasons:
        raise TransferError("Strategy risk: " + "; ".join(reasons))


def stop_loss_fraction(journal, agent_id: str, genesis: str, sender: str) -> Decimal | None:
    configured = policy(journal, agent_id, genesis, sender)
    if configured is None:
        return None
    return -Decimal(configured["max_trade_loss_bps"]) / Decimal(10_000)
