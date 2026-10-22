"""Desktop orchestration for explicitly reviewed agent trading, without a signer session."""

from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
import json
from pathlib import Path
import sqlite3
import time

from .agent_execution import AgentTradeService, cancel
from .decision import DecisionEngine, MarketQuote
from .execution_risk import RiskStore
from .library import StrategyLibrary
from .manual_trades import ManualTradeService, PREVIEW_TTL, TradeJournal
from .portfolio import CONFIG
from .pump_route import MAX_ACCOUNT_FUNDING, MAX_FEE
from .reservations import release, reserve, rows
from .shadow import run_shadow
from .strategies import parse_package
from .strategy_support import reviewed_eligible
from .transfers import GENESIS, TransferError, WithdrawalService, _uint, parse_sol, sol_text
from . import wallet_risk
from . import signing_sessions


@contextmanager
def services(directory, rpc):
    library = StrategyLibrary(Path(directory))
    journal = None
    try:
        journal = TradeJournal(Path(directory))
        yield AgentTradeService(library, rpc, journal)
    finally:
        if journal is not None:
            journal.close()
        library.close()


def supported(instance):
    return reviewed_eligible(parse_package(instance["package"]))


def wallet_positions(directory, sender):
    """Fast sidebar read: never run migrations or contend for the writer lock."""
    path = Path(directory) / "wallet-transfers.sqlite"
    if not path.exists():
        return []
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "trade_positions" not in tables:
            return []
        if "agent_positions" in tables:
            query = """SELECT p.*,a.agent_id FROM trade_positions p LEFT JOIN agent_positions a
                ON a.position_id=p.id WHERE p.sender=? AND p.status='open' ORDER BY p.opened_at DESC"""
        else:
            query = "SELECT *,NULL AS agent_id FROM trade_positions WHERE sender=? AND status='open' ORDER BY opened_at DESC"
        return [dict(r) for r in db.execute(query, (sender,))]
    finally:
        db.close()


def test_agent(library):
    """Reuse a pinned instance or create one with entries disabled until setup."""
    for instance in library.instances():
        if supported(library.instance(instance["id"])):
            return instance["id"]
    package = parse_package(
        {
            "schema": "pumpwizard.strategy-handoff/v1",
            "strategy": {"id": "core-frequency", "label": "Core frequency · guided test"},
            "configuration": CONFIG,
        }
    )
    library.import_strategy(package)
    return library.create_instance(package.digest, entry_mode="disabled")


def preferences(directory, agent_id, sender, values=None):
    """Draft UI inputs only. Saving them never allocates funds or enables signing."""
    Path(directory).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(Path(directory) / "trading-workspace.sqlite", timeout=10)
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS preferences (agent TEXT, sender TEXT, payload TEXT, PRIMARY KEY(agent,sender))"
        )
        if values is not None:
            if values["network"] not in ("mainnet-beta", "devnet"):
                raise TransferError("Choose mainnet-beta or devnet")
            if type(values["slippage_bps"]) is not int or not 1 <= values["slippage_bps"] <= 500:
                raise TransferError("Slippage must be 1–500 basis points (0.01–5%)")
            amount = values["allocation"].strip()
            payload = dict(
                network=values["network"],
                slippage_bps=values["slippage_bps"],
                allocation=sol_text(parse_sol(amount)) if amount else "",
            )
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO preferences VALUES (?,?,?)",
                    (agent_id, sender, json.dumps(payload)),
                )
        row = db.execute(
            "SELECT payload FROM preferences WHERE agent=? AND sender=?", (agent_id, sender)
        ).fetchone()
        return (
            json.loads(row[0])
            if row
            else dict(network="mainnet-beta", slippage_bps=100, allocation="")
        )
    finally:
        db.close()


def enable_reviewed_entries(directory, rpc, agent_id):
    with services(directory, rpc) as service:
        if not supported(service.library.instance(agent_id)):
            raise TransferError("Reviewed trading requires the pinned core-frequency configuration")
        # Only an entry preference changes. Tracking is controlled separately.
        with service.library._connection:
            service.library._connection.execute(
                "UPDATE instances SET entry_mode='manual',exit_mode='manual' WHERE id=?",
                (agent_id,),
            )


def tracking(directory, rpc, agent_id, enabled):
    with services(directory, rpc) as service:
        service.library.set_tracking(agent_id, enabled)


def allocate(directory, rpc, agent_id, sender, amount):
    with services(directory, rpc) as service:
        if not supported(service.library.instance(agent_id)):
            raise TransferError("Reviewed trading requires the pinned core-frequency configuration")
        return reserve(service.journal, rpc, agent_id, sender, lamports=parse_sol(amount))


def risk_settings(directory, network, sender, values=None):
    if network not in ("mainnet-beta", "devnet"):
        raise TransferError("Choose mainnet-beta or devnet")
    journal = TradeJournal(Path(directory))
    try:
        if values is not None:
            wallet_risk.save_policy(journal, GENESIS[network], sender, values)
        return wallet_risk.status(journal, GENESIS[network], sender)
    finally:
        journal.close()


def refresh_risk(directory, rpc, sender):
    from .portfolio_risk import refresh

    journal = TradeJournal(Path(directory))
    try:
        report = refresh(journal, rpc, sender)
        return wallet_risk.status(journal, report['genesis'], sender)
    finally:
        journal.close()


def release_allocation(directory, rpc, agent_id, sender, ident):
    with services(directory, rpc) as service:
        row = next((r for r in rows(service.journal, sender) if r["id"] == ident), None)
        if row is None or row["agent_id"] != agent_id or row["genesis"] != GENESIS[rpc.network]:
            raise TransferError("Select an allocation belonging to this agent, wallet and network")
        return release(service.journal, ident)


def prepare(directory, rpc, agent_id, sender, cash_hold, mint, position_id, slippage):
    with services(directory, rpc) as service:
        return service.prepare(
            agent_id, sender, mint, cash_hold, position_id=position_id, slippage_bps=slippage
        )


def order(
    directory,
    rpc,
    agent_id,
    sender,
    ident,
    *,
    action="read",
    wallet=None,
    session_manager=None,
    session_id=None,
):
    with services(directory, rpc) as service:
        p = service.journal.previews(ident)
        ctx = p.get("agent_order") or {}
        if (ctx.get("agent_id"), p["sender"], p["network"]) != (agent_id, sender, rpc.network):
            raise TransferError("Order belongs to another agent, wallet or network")
        row = service.journal.db.execute(
            "SELECT status FROM agent_orders WHERE id=?", (ident,)
        ).fetchone()
        if row is None or row["status"] != "review":
            raise TransferError("Only an unsigned review can be reviewed or cancelled")
        if action == "cancel":
            cancel(service.journal, ident)
        elif action == "submit":
            if session_manager is not None and session_id:
                return session_manager.submit(service, session_id, ident)
            if wallet is None or wallet.address != sender:
                raise TransferError("Authorize with the reviewed wallet")
            return service.submit(ident, wallet)
        elif action != "read":
            raise TransferError("Unknown order action")
        return p


def authorize_session(directory, rpc, agent_id, sender, cash_hold, manager, wallet, options):
    if wallet.address != sender:
        raise TransferError("Wallet changed during session authorization")
    with services(directory, rpc) as service:
        return manager.authorize(service, wallet, agent_id, cash_hold, **options)


def revoke_session(directory, sender, network, ident):
    journal = TradeJournal(Path(directory))
    try:
        signing_sessions.revoke(journal, ident, sender, GENESIS[network])
    finally:
        journal.close()


def shadow(directory, rpc, agent_id, sender, cash_hold, candidate, slippage):
    return run_shadow(
        directory,
        rpc,
        agent_id,
        sender,
        cash_hold,
        mint=candidate["mint"],
        position_id=candidate.get("position_id"),
        slippage_bps=slippage,
    )


def snapshot(directory, rpc, agent_id, sender, *, recover=True):
    """Load local state even if RPC is down; unknown balances are never zero-filled."""
    with services(directory, rpc) as service:
        library, journal = service.library, service.journal
        instance = library.instance(agent_id)
        genesis = GENESIS[rpc.network]
        balance, rpc_error, recovery_error = None, "", ""
        try:
            if rpc.genesis() != genesis:
                raise TransferError("RPC network does not match the selected network")
            if recover and journal.pending(sender, genesis):
                try:
                    ManualTradeService(rpc, journal).reconcile(sender)
                    WithdrawalService(rpc, journal).reconcile(sender)
                except TransferError as exc:
                    recovery_error = str(exc)
            balance = _uint(
                rpc.call("getBalance", [sender, {"commitment": "confirmed"}])["value"],
                "wallet balance",
            )
        except (TransferError, KeyError, TypeError, ValueError):
            rpc_error = "RPC unavailable or wrong network. Balance is unknown; retry the check."
        all_holds = [
            r for r in rows(journal, sender) if r["genesis"] == genesis and r["status"] == "held"
        ]
        holds = [r for r in all_holds if r["agent_id"] == agent_id]
        held = sum(int(r["lamports"]) for r in all_holds)
        pending = journal.pending(sender, genesis)
        positions = [
            dict(r)
            for r in journal.db.execute(
                """SELECT p.*,a.token_hold,a.sold_stages FROM agent_positions a
            JOIN trade_positions p ON p.id=a.position_id WHERE a.agent_id=? AND p.sender=? AND p.genesis=?
            ORDER BY p.opened_at DESC""",
                (agent_id, sender, genesis),
            )
        ]
        held_ids = {r["id"] for r in holds}
        for position in positions:
            position["managed"] = position["token_hold"] in held_ids
        orders = []
        trades = {r["id"]: r for r in journal.trades(sender)}
        for row in journal.db.execute(
            "SELECT * FROM agent_orders WHERE agent_id=? ORDER BY created_at DESC", (agent_id,)
        ):
            p = journal.previews(row["id"])
            if p["sender"] != sender or p["genesis"] != genesis:
                continue
            trade = trades.get(row["id"])
            orders.append(
                dict(
                    row,
                    preview=p,
                    trade=trade,
                    display_status=(
                        trade["status"]
                        if trade
                        else "expired review"
                        if row["status"] == "review" and time.time() - p["created_at"] > PREVIEW_TTL
                        else row["status"]
                    ),
                )
            )
        busy_holds = {o["cash_hold"] for o in orders if o["status"] in ("review", "submitted")}
        free_cash = [r for r in holds if r["position_id"] is None and r["id"] not in busy_holds]
        candidates = [
            dict(
                id=p["id"],
                position_id=p["id"],
                mint=p["mint"],
                side="sell",
                quantity=p["quantity"],
                reason="Assess remaining position",
            )
            for p in positions
            if p["status"] == "open" and p["managed"]
        ]
        held_mints = {p["mint"] for p in candidates}
        seen = set()
        for decision in library.decisions(agent_id, 1000):
            mint = decision["mint"]
            if mint in seen:
                continue
            seen.add(mint)
            if mint not in held_mints and decision["kind"] == "entry_recommended":
                candidates.append(
                    dict(
                        id="entry:" + mint,
                        position_id=None,
                        mint=mint,
                        side="buy",
                        quantity=None,
                        reason=decision["reason"],
                    )
                )
        now, fresh, required = time.time(), 0, None
        engine = DecisionEngine(parse_package(instance["package"]))
        for candidate in candidates:
            try:
                quote = MarketQuote.from_record(
                    library.latest_observation(agent_id, candidate["mint"])
                )
                usable = engine.quote_usable(quote, now) and quote.quote_usd > 0
                candidate["quote_age"] = max(0, int(now - quote.observed_at))
                candidate["fresh"] = usable
                fresh += int(usable)
                if usable:
                    budget = int(
                        (
                            Decimal(str(CONFIG["order_usd"]))
                            / Decimal(str(quote.quote_usd))
                            * 10**9
                        ).to_integral_value(rounding=ROUND_CEILING)
                    )
                    required = max(required or 0, budget + MAX_ACCOUNT_FUNDING + MAX_FEE)
            except (ValueError, KeyError, TypeError):
                candidate.update(quote_age=None, fresh=False)
        pinned = supported(instance)
        cash = max((int(r["lamports"]) for r in free_cash), default=0)
        risk = wallet_risk.status(journal, genesis, sender)
        checks = [
            dict(
                name="Wallet limits",
                state="Not configured"
                if risk["policy"] is None
                else "Entries blocked"
                if risk["entry_blocks"]
                else "Configured",
                detail="; ".join(risk["entry_blocks"])
                or (
                    "Open Wallet limits to set limits shared by manual buys and every agent"
                    if risk["policy"] is None
                    else "Cost, realized P/L and configured portfolio limits; each buy is checked again before signing"
                ),
            ),
            dict(
                name="Strategy",
                state="Reviewed eligible" if pinned else "Research only",
                detail="Pinned core-frequency; funded lifecycle pending; fresh per-order checks required"
                if pinned
                else "Choose the pinned core-frequency configuration",
            ),
            dict(
                name="Connection",
                state="Ready" if balance is not None else "Unavailable",
                detail=rpc_error or f"{rpc.network} identity verified",
            ),
            dict(
                name="Funding",
                state="Unknown"
                if balance is None
                else "Awaiting deposit"
                if balance == 0
                else "Ready"
                if balance >= held
                else "Deficit",
                detail="Check RPC to read the balance"
                if balance is None
                else f"{sol_text(balance)} SOL total · {sol_text(held)} SOL allocated",
            ),
            dict(
                name="Entry controls",
                state="Ready"
                if instance["entry_mode"] != "disabled" and instance["status"] == "monitoring"
                else "Paused",
                detail="Review required for every trade; open positions can still be assessed",
            ),
            dict(
                name="Allocation",
                state="Waiting"
                if required is None and cash
                else "Ready"
                if required is not None and cash >= required
                else "Action needed",
                detail=(
                    f"Largest available cash allocation: {sol_text(cash)} SOL. "
                    + (
                        f"Entry + exit reserve estimate: {sol_text(required)} SOL."
                        if required
                        else "Fresh SOL/USD data is needed for the entry budget."
                    )
                ),
            ),
            dict(
                name="Market data",
                state="Ready" if fresh else "Waiting",
                detail=f"{fresh} candidates with fresh quotes · start tracking to collect signals",
            ),
            dict(
                name="Recovery",
                state="Pending" if pending else "Ready",
                detail=recovery_error
                or (
                    "Use Refresh & recover for the original signature; do not replace it or release holds"
                    if pending
                    else "No unresolved wallet operation"
                ),
            ),
        ]
        store = RiskStore(directory)
        try:
            reports = [
                r
                for r in store.reports(agent_id)
                if r["report"]["sender"] == sender and r["report"]["network"] == rpc.network
            ]
        finally:
            store.close()
        return dict(
            instance=instance,
            tracking=library.tracking_settings(agent_id),
            supported=pinned,
            decisions=library.decisions(agent_id, 12),
            network=rpc.network,
            checked_at=now,
            balance=balance,
            allocated=held,
            available=None if balance is None else max(0, balance - held),
            pending=pending,
            holds=holds,
            free_cash=free_cash,
            positions=positions,
            orders=orders,
            candidates=candidates,
            reports=reports,
            checks=checks,
            rpc_error=rpc_error,
            recovery_error=recovery_error,
            wallet_risk=risk,
            signing_sessions=signing_sessions.rows(journal, sender, genesis),
        )


def operations_text(data):
    """Explain independent control states and recovery without suggesting a bypass."""
    tracking = data["tracking"]
    lines = [
        "Automatic execution is disabled. Tracking, unsigned rehearsal and signing authorization "
        "are separate controls.",
        "",
        f"Agent: {data['instance']['id']} · {data['instance']['status']}",
        "Tracking: " + ("fault — " + tracking["fault"] if tracking["fault"] else
                        "enabled" if tracking["enabled"] else "entries paused / not started"),
        "Recovery: retry tracking from the agent screen after inspecting a fault. Pause entries "
        "preserves held-position monitoring. Stop worker stops its entry AND exit assessments. "
        "Reopen does not restart the unsigned worker or unlock a signing session.",
        "",
    ]
    if data["pending"]:
        lines.append("UNRESOLVED WALLET OPERATION: Refresh & recover checks the original signature. "
                     "An unknown outcome or expired blockhash does not establish failure. "
                     "Keep allocations held; never prepare a replacement to work around it.")
    if any(o["display_status"] == "expired review" for o in data["orders"]):
        lines.append("EXPIRED UNSIGNED REVIEW: select it in Orders and Cancel unsigned review, "
                     "then prepare a fresh review. The underlying agent allocation remains held.")
    manual = sum(p["status"] == "open" and not p["managed"] for p in data["positions"])
    if manual:
        lines.append(f"MANUAL EXIT CONTROL: {manual} open positions are outside agent exit assessment. "
                     "Use the separately reviewed manual sell workflow.")
    lines += ["", "Current setup checks (individual checks, not a trading certification):"]
    lines.extend(f"{c['name']} — {c['state']}: {c['detail']}" for c in data["checks"])
    lines += ["", "Blocked assessment: inspect its complete reason in Shadow reports or Worker "
              "rehearsal. Refresh stale observations/valuation, check the selected allocation, "
              "network and wallet limits, then assess again. Unsupported routes/configurations "
              "require new implementation and verification; retrying cannot grant support.",
              "Session unavailable/expired: existing positions still need attention. In Signing "
              "session, uncheck Use this session for a separately approved password exit, or "
              "review a new bounded session. No automatic renewal or exit is performed.",
              "", "Recent local decisions (advisory; includes rejected/blocked decisions):"]
    for decision in data["decisions"]:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(decision["at"]))
        lines.append(f"{stamp} · {decision['mint']} · {decision['kind']}: {decision['reason']}")
    if not data["decisions"]:
        lines.append("No decisions recorded. Start tracking and allow the observation window to fill.")
    return "\n".join(lines)
