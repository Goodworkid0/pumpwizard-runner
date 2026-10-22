"""Bounded automatic execution for the separate encrypted Automation Wallet.

The durable journal contains public authorization scope and conservative usage.
The decrypted wallet exists only in this process and is never reconstructed after
a restart.  All transactions are still prepared, validated, reserved, broadcast
and reconciled by the normal agent/manual trade services.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from . import strategy_risk, wallet_risk
from .agent_execution import AgentTradeService, cancel, check_binding
from .manual_trades import ManualTradeService, PREVIEW_TTL, TradeJournal
from .portfolio import CONFIG
from .pump_route import MAX_ACCOUNT_FUNDING, MAX_FEE, ROUTES
from .pump_swap_route import ROUTES as SWAP_ROUTES
from .reservations import wallet_revision
from .signing_sessions import configuration
from .strategies import parse_package
from .strategy_support import reviewed_eligible
from .transfers import GENESIS, TransferError, WithdrawalPreview, WithdrawalService, _uint
from .wallet_vault import WalletVault, WalletVaultError


SCHEMA = """
CREATE TABLE IF NOT EXISTS automation_reviews (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, genesis TEXT NOT NULL,
    sender TEXT NOT NULL, plan_revision INTEGER NOT NULL, payload TEXT NOT NULL,
    created_at REAL NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_authorizations (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, genesis TEXT NOT NULL,
    sender TEXT NOT NULL, revision INTEGER NOT NULL, scope TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL,
    entries_paused INTEGER NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_authorization_orders (
    order_id TEXT PRIMARY KEY, authorization_id TEXT NOT NULL,
    agent_id TEXT NOT NULL, side TEXT NOT NULL, debit TEXT NOT NULL,
    authorized_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS automation_usage
    ON automation_authorization_orders(authorization_id,authorized_at);
CREATE UNIQUE INDEX IF NOT EXISTS active_automation_wallet
    ON automation_authorizations(genesis,sender)
    WHERE status IN ('authorized','running','paused');
CREATE TABLE IF NOT EXISTS automation_events (
    id INTEGER PRIMARY KEY, authorization_id TEXT NOT NULL, at REAL NOT NULL,
    kind TEXT NOT NULL, detail TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_recovery_reviews (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, genesis TEXT NOT NULL,
    sender TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
    revision INTEGER NOT NULL, created_at REAL NOT NULL, status TEXT NOT NULL
);
"""

EXIT_DEBIT = MAX_ACCOUNT_FUNDING + MAX_FEE
REVIEW_SECONDS = 60
TERMINAL = frozenset(("stopped", "expired", "faulted", "locked", "replaced"))


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_encode(value).encode()).hexdigest()


def readiness_identity() -> dict:
    root = Path(__file__).parent
    material = hashlib.sha256()
    for name in (
        "automation_runtime.py", "agent_execution.py", "manual_trades.py",
        "pump_route.py", "pump_swap_route.py", "strategy_risk.py", "wallet_risk.py",
    ):
        path = root / name
        material.update(name.encode())
        material.update(path.read_bytes())
    return {
        "implementation_sha256": material.hexdigest(),
        "strategy_configuration_sha256": _hash(CONFIG),
        "routes": sorted((*ROUTES, *SWAP_ROUTES)),
    }


def production_blockers(directory: Path) -> list[str]:
    """Return evidence blockers. There is deliberately no request-level bypass."""
    path = Path(directory) / "automation-funded-acceptance.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict) or value.get("schema") != "pumpwizard.automation-acceptance/v1":
        return [
            "Reviewed funded partial sell → restart → full sell evidence is still pending",
            "Reviewed strategy-owned funded execution evidence is still pending",
        ]
    expected = readiness_identity()
    if any(value.get(name) != expected[name] for name in expected):
        return ["Material signer, risk, strategy configuration or route code changed after funded acceptance"]
    blockers = []
    if value.get("manual_lifecycle") != "verified":
        blockers.append("Reviewed funded partial sell → restart → full sell evidence is still pending")
    if value.get("strategy_execution") != "verified":
        blockers.append("Reviewed strategy-owned funded execution evidence is still pending")
    return blockers


def _plan_values(plan: dict) -> dict:
    from .transfers import parse_sol

    return {
        name: parse_sol(plan[name])
        for name in (
            "budget_sol", "max_order_sol", "daily_buy_sol", "daily_loss_sol",
            "reserve_sol", "portfolio_drawdown_sol",
        )
    }


def _latest_fx(library, agent_id: str, now: float) -> tuple[float | None, float | None]:
    rows = library.recent_observations(agent_id, now - 120)
    usable = [
        row for row in rows
        if isinstance(row.get("quote_usd"), (int, float))
        and not isinstance(row.get("quote_usd"), bool)
        and float(row["quote_usd"]) > 0
        and isinstance(row.get("observed_at"), (int, float))
    ]
    if not usable:
        return None, None
    row = max(usable, key=lambda item: float(item["observed_at"]))
    return float(row["quote_usd"]), float(row["observed_at"])


def _usage(journal: TradeJournal, authorization_id: str) -> tuple[int, int]:
    values = [
        int(row[0]) for row in journal.db.execute(
            "SELECT debit FROM automation_authorization_orders WHERE authorization_id=?",
            (authorization_id,),
        )
    ]
    return sum(values), len(values)


def authorization_rows(journal: TradeJournal, sender: str, genesis: str) -> list[dict]:
    rows = []
    for row in journal.db.execute(
        "SELECT * FROM automation_authorizations WHERE sender=? AND genesis=? "
        "ORDER BY created_at DESC LIMIT 20", (sender, genesis),
    ):
        used, count = _usage(journal, row["id"])
        value = dict(row)
        value.update(scope=json.loads(row["scope"]), used_lamports=used, used_orders=count,
                     entries_paused=bool(row["entries_paused"]))
        rows.append(value)
    return rows


class AutomationLease:
    def __init__(self, wallet, wall_deadline: float, monotonic_deadline: float):
        self.wallet = wallet
        self.wall_deadline = wall_deadline
        self.monotonic_deadline = monotonic_deadline
        self.cancelled = threading.Event()


class AutomationManager:
    """One wallet-level capability shared by all scoped strategy instances."""

    def __init__(self):
        self._lock = threading.RLock()
        self._leases: dict[str, AutomationLease] = {}
        self._epoch = 0

    def available(self, ident: str) -> bool:
        with self._lock:
            lease = self._leases.get(ident)
            if lease and (time.monotonic() >= lease.monotonic_deadline or time.time() >= lease.wall_deadline):
                lease.cancelled.set()
                lease.wallet = None
                self._leases.pop(ident, None)
                return False
            return bool(lease and lease.wallet is not None and not lease.cancelled.is_set())

    def lock(self, ident: str | None = None) -> None:
        # Signal first. A concurrent signer observes this without waiting for SQLite.
        for key, lease in list(self._leases.items()):
            if ident is None or key == ident:
                lease.cancelled.set()
        with self._lock:
            self._epoch += 1
            for key in list(self._leases):
                if ident is None or key == ident:
                    self._leases.pop(key).wallet = None

    def install(self, ident: str, wallet, expires_at: float, epoch: int) -> None:
        with self._lock:
            if epoch != self._epoch:
                raise TransferError("Automation authorization was cancelled")
            for lease in self._leases.values():
                lease.cancelled.set()
                lease.wallet = None
            self._leases.clear()
            duration = max(0, expires_at - time.time())
            self._leases[ident] = AutomationLease(wallet, expires_at, time.monotonic() + duration)

    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    def submit(self, service: AgentTradeService, ident: str, order_id: str) -> str:
        manager = self
        preview = service.journal.previews(order_id)

        class ScopedSigner:
            address = preview["sender"]

            def sign(self, message):
                return manager._sign(service, ident, preview, message)

            def before_broadcast(self):
                if not manager.available(ident):
                    raise TransferError(
                        "Automation stopped after signing and before broadcast; the original signature remains recorded"
                    )

        return service.submit(order_id, ScopedSigner())

    def _sign(self, service: AgentTradeService, ident: str, preview: dict, message: bytes) -> bytes:
        journal = service.journal
        if not journal.db.in_transaction:
            raise TransferError("Automation signing requires the atomic order-reservation transaction")
        with self._lock:
            if not self.available(ident):
                raise TransferError("Automation authorization is locked, expired or revoked")
            lease = self._leases[ident]
            row = journal.db.execute(
                "SELECT * FROM automation_authorizations WHERE id=?", (ident,)
            ).fetchone()
            if row is None or row["status"] not in ("authorized", "running", "paused"):
                self.lock(ident)
                raise TransferError("Automation authorization is no longer active")
            if not row["created_at"] <= time.time() < row["expires_at"]:
                self.lock(ident)
                raise TransferError("Automation authorization expired")
            scope = json.loads(row["scope"])
            context = preview.get("agent_order") or {}
            agent_id = context.get("agent_id")
            strategy = scope["strategies"].get(agent_id)
            if (
                strategy is None
                or preview["sender"] != row["sender"]
                or preview["genesis"] != row["genesis"]
                or preview["network"] != scope["network"]
                or context.get("cash_hold") != strategy["cash_hold"]
                or lease.wallet.address != row["sender"]
            ):
                raise TransferError("Order is outside this Automation Wallet authorization scope")
            if row["entries_paused"] and preview["side"] == "buy":
                raise TransferError("Automation entries are paused; authorized exits remain available")
            if configuration(service.library.instance(agent_id)) != strategy["configuration"]:
                raise TransferError("Strategy configuration changed; review a new authorization")
            current_strategy_policy = strategy_risk.policy(
                journal, agent_id, row["genesis"], row["sender"]
            )
            if _hash(current_strategy_policy) != strategy["policy_hash"]:
                raise TransferError("Strategy allocation or risk policy changed; review again")
            current_wallet_policy = wallet_risk.policy(journal, row["genesis"], row["sender"])
            if _hash(current_wallet_policy) != scope["wallet_policy_hash"]:
                raise TransferError("Wallet risk policy changed; review again")
            if preview["route"] not in scope["routes"] or preview["slippage_bps"] > scope["slippage_bps"]:
                raise TransferError("Order route or slippage exceeds the authorization")
            import base64

            if base64.b64decode(preview["message"]) != message or journal.previews(preview["id"]) != preview:
                raise TransferError("Automation refuses a message different from the prepared order")
            wallet_risk.check_buy(journal, preview, preview["state"], limits=scope["wallet_policy"])
            check_binding(journal, preview)
            used, orders = _usage(journal, ident)
            debit = preview["fee"] + MAX_ACCOUNT_FUNDING + (
                preview["amount"] if preview["side"] == "buy" else 0
            )
            now = time.time()
            day = int(now // 86400) * 86400
            daily = list(journal.db.execute(
                "SELECT side,debit FROM automation_authorization_orders "
                "WHERE authorization_id=? AND authorized_at>=? AND authorized_at<?",
                (ident, day, day + 86400),
            ))
            if len(daily) >= scope["max_trades_per_day"]:
                raise TransferError("Automation daily trade count is exhausted")
            if preview["side"] == "buy":
                buys = [item for item in daily if item["side"] == "buy"]
                if len(buys) >= scope["max_buys_per_day"]:
                    raise TransferError("Automation daily buy count is exhausted")
                if sum(int(item["debit"]) for item in buys) + debit > scope["daily_buy_lamports"]:
                    raise TransferError("Automation daily buy budget is exhausted")
                if debit > scope["max_order_lamports"]:
                    raise TransferError("Buy exceeds the authorization's total-debit ceiling")
            if orders >= scope["max_orders"] or used + debit > scope["budget_lamports"]:
                raise TransferError("Automation total budget or order count is exhausted")
            open_count = journal.db.execute(
                "SELECT COUNT(*) FROM trade_positions WHERE genesis=? AND sender=? AND status='open'",
                (row["genesis"], row["sender"]),
            ).fetchone()[0]
            if preview["side"] == "buy" and open_count >= scope["max_positions"]:
                raise TransferError("Automation Wallet open-position capacity is full")
            remaining_positions = open_count + (1 if preview["side"] == "buy" else 0)
            if preview["side"] == "sell":
                position = journal.position(preview["position_id"])
                if preview["amount"] == int(position["quantity"]):
                    remaining_positions = max(0, open_count - 1)
            if scope["budget_lamports"] - used - debit < remaining_positions * scope["exit_debit_lamports"]:
                raise TransferError("Automation must retain one conservative full-exit allowance per open position")
            journal.db.execute(
                "INSERT INTO automation_authorization_orders VALUES (?,?,?,?,?,?)",
                (preview["id"], ident, agent_id, preview["side"], str(debit), now),
            )
            if (
                lease.cancelled.is_set()
                or time.monotonic() >= lease.monotonic_deadline
                or time.time() >= lease.wall_deadline
                or not 0 <= time.time() - preview["created_at"] <= PREVIEW_TTL
            ):
                raise TransferError("Automation stopped or the authorization/quote expired before signing")
            return lease.wallet.sign(message)


class AutomationController:
    """Own the signer and worker for one local browser server process."""

    def __init__(self, directory: Path, rpc_factory=None, readiness=None, interval: int = 15):
        self.directory = Path(directory)
        self.rpc_factory = rpc_factory
        self.readiness = readiness or (lambda: production_blockers(self.directory))
        self.interval = interval
        self.manager = AutomationManager()
        self._worker: AutomationSupervisor | None = None
        self._lock = threading.RLock()
        self._reset_done = False

    def rpc(self, network: str):
        if self.rpc_factory is not None:
            return self.rpc_factory(network)
        from .web_trading import rpc_for

        return rpc_for(network, self.directory)

    def _reset_restart_state(self, journal: TradeJournal) -> None:
        with self._lock:
            if self._reset_done:
                return
            with journal.db:
                journal.db.execute(
                    "UPDATE automation_authorizations SET status='locked',"
                    "detail='Runner restarted; authorize again to restore an in-memory signer' "
                    "WHERE status IN ('authorized','running','paused')"
                )
            self._reset_done = True

    def review(self, library, owner: str, network: str, sender: str, plan: dict) -> dict:
        journal = TradeJournal(self.directory)
        try:
            self._reset_restart_state(journal)
            return self._build_review(library, journal, owner, network, sender, plan, persist=True)
        finally:
            journal.close()

    def _build_review(self, library, journal, owner, network, sender, plan, *, persist):
        if network != "mainnet-beta":
            raise TransferError("Automation authorization currently supports mainnet only")
        rpc = self.rpc(network)
        genesis = rpc.genesis()
        if genesis != GENESIS[network]:
            raise TransferError("RPC network identity does not match Mainnet")
        balance = _uint(rpc.call("getBalance", [sender, {"commitment": "confirmed"}])["value"], "balance")
        values = _plan_values(plan)
        blockers: list[str] = []
        now = time.time()
        strategies = {}
        sessions = list(plan["sessions"])
        allocation_base, remainder = divmod(values["budget_sol"], len(sessions))
        required_entries = []
        for index, agent_id in enumerate(sessions):
            instance = library.instance(agent_id)
            if not reviewed_eligible(parse_package(instance["package"])):
                blockers.append(f"{instance['label']}: strategy is not eligible for reviewed execution")
            if instance["status"] != "monitoring" or instance["entry_mode"] == "disabled":
                blockers.append(f"{instance['label']}: monitoring and reviewed entries must be enabled")
            quote_usd, observed_at = _latest_fx(library, agent_id, now)
            required = None
            if quote_usd is None or observed_at is None or now - observed_at > CONFIG["quote_max_age_seconds"]:
                blockers.append(f"{instance['label']}: a fresh SOL/USD observation is required")
            else:
                required = int((Decimal(str(CONFIG["order_usd"])) / Decimal(str(quote_usd)) * Decimal(10**9)).to_integral_value(rounding=ROUND_FLOOR))
                required_entries.append(required)
            allocation = allocation_base + (1 if index < remainder else 0)
            strategy_policy = {
                "allocation_lamports": allocation,
                "buys_enabled": True,
                "sells_enabled": True,
                "min_trade_lamports": 1,
                "max_trade_lamports": min(values["max_order_sol"], allocation),
                "max_daily_buy_lamports": min(values["daily_buy_sol"], allocation),
                "max_trades_per_day": plan["max_trades_per_day"],
                "max_daily_loss_lamports": min(values["daily_loss_sol"], allocation),
                "max_daily_loss_bps": plan["daily_loss_bps"],
                "max_trade_loss_bps": plan["trade_loss_bps"],
                "max_open_positions": plan["max_positions"],
            }
            strategies[agent_id] = {
                "label": instance["label"],
                "configuration": configuration(instance),
                "quote_usd": quote_usd,
                "observed_at": observed_at,
                "required_entry_debit_lamports": required,
                "allocation_lamports": allocation,
                "policy": strategy_policy,
                "policy_hash": _hash(strategy_policy),
            }
            if required is not None and allocation < required:
                blockers.append(
                    f"{instance['label']}: equal allocation is short by {required - allocation} lamports for the pinned $10 order"
                )
        required_entry = max(required_entries) if required_entries else None
        entry_slots = min(plan["max_buys_per_day"], plan["max_positions"])
        exit_headroom = plan["max_positions"] * EXIT_DEBIT
        required_budget = (required_entry * entry_slots + exit_headroom) if required_entry is not None else None
        if required_entry is not None and values["max_order_sol"] < required_entry:
            blockers.append(
                f"Maximum order is short by {required_entry - values['max_order_sol']} lamports for the pinned $10 strategy debit"
            )
        if required_budget is not None and values["budget_sol"] < required_budget:
            blockers.append(
                f"Total budget is short by {required_budget - values['budget_sol']} lamports after reserving {exit_headroom} lamports for exits"
            )
        if balance < values["budget_sol"] + values["reserve_sol"]:
            blockers.append(
                f"Automation Wallet needs {values['budget_sol'] + values['reserve_sol'] - balance} more lamports for budget plus reserve"
            )
        if plan["max_trades_per_day"] < plan["max_buys_per_day"] + plan["max_positions"]:
            blockers.append("Daily trade count must leave one full-exit attempt per configured position")
        if journal.pending(sender, genesis):
            blockers.append("Resolve the Automation Wallet's original pending signature before authorization")
        open_positions = journal.db.execute(
            "SELECT COUNT(*) FROM trade_positions WHERE genesis=? AND sender=? AND status='open'",
            (genesis, sender),
        ).fetchone()[0]
        if open_positions > plan["max_positions"]:
            blockers.append(
                f"Automation Wallet already has {open_positions} open positions, above the configured capacity"
            )
        exposure = values["budget_sol"] - exit_headroom
        if exposure <= 0:
            blockers.append("Total budget must exceed the conservative exit allowance")
            exposure = 1
        wallet_policy = {
            "max_buy_lamports": values["max_order_sol"],
            "max_exposure_lamports": max(values["max_order_sol"], exposure),
            "reserve_lamports": values["reserve_sol"],
            "daily_loss_lamports": values["daily_loss_sol"],
            "drawdown_lamports": values["daily_loss_sol"],
            "portfolio_drawdown_lamports": values["portfolio_drawdown_sol"],
            "entries_paused": False,
        }
        scope = {
            "schema": "pumpwizard.automation-review/v1",
            "owner": owner,
            "sender": sender,
            "network": network,
            "genesis": genesis,
            "plan_revision": plan["revision"],
            "plan_hash": _hash(plan),
            "strategies": strategies,
            "routes": sorted((*ROUTES, *SWAP_ROUTES)),
            "budget_lamports": values["budget_sol"],
            "max_order_lamports": values["max_order_sol"],
            "daily_buy_lamports": values["daily_buy_sol"],
            "max_buys_per_day": plan["max_buys_per_day"],
            "max_trades_per_day": plan["max_trades_per_day"],
            "max_positions": plan["max_positions"],
            "max_orders": plan["max_trades_per_day"],
            "slippage_bps": plan["slippage_bps"],
            "priority_fee_lamports": 0,
            "duration_seconds": plan["duration_minutes"] * 60,
            "wallet_policy": wallet_policy,
            "wallet_policy_hash": _hash(wallet_policy),
            "balance_lamports": balance,
            "required_entry_debit_lamports": required_entry,
            "exit_debit_lamports": EXIT_DEBIT,
            "exit_headroom_lamports": exit_headroom,
            "required_budget_lamports": required_budget,
            "preflight_blockers": blockers,
            "activation_blockers": list(self.readiness()),
            "reviewed_at": now,
            "expires_at": now + REVIEW_SECONDS,
        }
        ident = uuid.uuid4().hex
        scope["id"] = ident
        scope["ready"] = not blockers
        scope["can_authorize"] = not blockers and not scope["activation_blockers"]
        if persist:
            with journal.db:
                journal.db.execute(
                    "INSERT INTO automation_reviews VALUES (?,?,?,?,?,?,?,?)",
                    (ident, owner, genesis, sender, plan["revision"], _encode(scope), now, "reviewed"),
                )
        return scope

    def authorize(self, library, owner: str, network: str, sender: str, plan: dict,
                  review_id: str, password: str) -> str:
        journal = TradeJournal(self.directory)
        wallet = None
        try:
            self._reset_restart_state(journal)
            row = journal.db.execute("SELECT * FROM automation_reviews WHERE id=?", (review_id,)).fetchone()
            if row is None:
                raise TransferError("Authorization review was not found")
            reviewed = json.loads(row["payload"])
            if row["status"] != "reviewed" or (row["owner"], row["sender"], row["genesis"]) != (
                owner, sender, GENESIS[network]
            ):
                raise TransferError("Authorization review belongs to another owner, wallet or network")
            if time.time() >= reviewed["expires_at"]:
                raise TransferError("Authorization review expired; review the current balance and limits again")
            if reviewed["plan_hash"] != _hash(plan) or reviewed["plan_revision"] != plan["revision"]:
                raise TransferError("Authorization draft changed; review it again")
            if reviewed["preflight_blockers"]:
                raise TransferError("Preflight blocked authorization: " + "; ".join(reviewed["preflight_blockers"]))
            blockers = list(self.readiness())
            if blockers:
                raise TransferError("Live activation remains gated: " + "; ".join(blockers))
            epoch = self.manager.epoch()
            wallet = WalletVault(self.directory / "automation-wallet").unlock(password)
            if wallet.address != sender:
                raise TransferError("Vault address changed after review")
            rpc = self.rpc(network)
            balance = _uint(rpc.call("getBalance", [sender, {"commitment": "confirmed"}])["value"], "balance")
            if balance != reviewed["balance_lamports"]:
                raise TransferError("Automation Wallet balance changed; review authorization again")
            wallet_risk.save_policy(journal, reviewed["genesis"], sender, reviewed["wallet_policy"])
            strategies = {}
            for agent_id, item in reviewed["strategies"].items():
                saved = strategy_risk.save_policy(
                    journal, agent_id, reviewed["genesis"], sender, item["policy"], balance
                )
                strategies[agent_id] = {
                    **item,
                    "cash_hold": saved["hold_id"],
                    "policy_hash": _hash(saved["policy"]),
                }
            now = time.time()
            ident = uuid.uuid4().hex
            scope = {
                **{key: value for key, value in reviewed.items() if key not in (
                    "id", "ready", "can_authorize", "preflight_blockers", "activation_blockers",
                    "reviewed_at", "expires_at", "strategies",
                )},
                "schema": "pumpwizard.automation-authorization/v1",
                "review_id": review_id,
                "strategies": strategies,
                "wallet_revision": wallet_revision(journal, reviewed["genesis"], sender),
            }
            expires = now + reviewed["duration_seconds"]
            with journal.db:
                journal.db.execute("BEGIN IMMEDIATE")
                if journal.pending(sender, reviewed["genesis"]):
                    raise TransferError("Resolve the pending wallet operation before authorization")
                journal.db.execute(
                    "UPDATE automation_authorizations SET status='replaced',detail='Replaced by a new authorization' "
                    "WHERE genesis=? AND sender=? AND status IN ('authorized','running','paused')",
                    (reviewed["genesis"], sender),
                )
                journal.db.execute(
                    "INSERT INTO automation_authorizations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ident, owner, reviewed["genesis"], sender, scope["wallet_revision"], _encode(scope),
                     now, expires, "authorized", 0, "Authorized; start explicitly"),
                )
                journal.db.execute("UPDATE automation_reviews SET status='authorized' WHERE id=?", (review_id,))
                journal.db.execute(
                    "INSERT INTO automation_events(authorization_id,at,kind,detail,payload) VALUES (?,?,?,?,?)",
                    (ident, now, "authorized", "Vault unlocked into bounded process memory", "{}"),
                )
            try:
                self.manager.install(ident, wallet, expires, epoch)
            except TransferError:
                with journal.db:
                    journal.db.execute(
                        "UPDATE automation_authorizations SET status='stopped',"
                        "detail='Authorization cancelled before signer installation' WHERE id=?",
                        (ident,),
                    )
                raise
            wallet = None  # Lease owns the only remaining reference.
            return ident
        except WalletVaultError as exc:
            raise TransferError(str(exc)) from None
        finally:
            if wallet is not None:
                del wallet
            journal.close()

    def start(self, ident: str, owner: str, sender: str, network: str) -> None:
        if not self.manager.available(ident):
            raise TransferError("Authorize this run in the current Runner process before starting")
        with self._lock:
            if self._worker and self._worker.running:
                raise TransferError("An Automation Wallet worker is already running")
        journal = TradeJournal(self.directory)
        try:
            with journal.db:
                journal.db.execute("BEGIN IMMEDIATE")
                row = journal.db.execute("SELECT * FROM automation_authorizations WHERE id=?", (ident,)).fetchone()
                if row is None or (row["owner"], row["sender"], row["genesis"]) != (
                    owner, sender, GENESIS[network]
                ) or row["status"] not in ("authorized", "paused"):
                    raise TransferError("Authorization cannot be started for this owner and wallet")
                journal.db.execute(
                    "UPDATE automation_authorizations SET status='running',entries_paused=0,detail='Automation worker running' WHERE id=?",
                    (ident,),
                )
        finally:
            journal.close()
        with self._lock:
            self._worker = AutomationSupervisor(self, ident, network, self.interval)
            self._worker.start()

    def pause_entries(self, ident: str, owner: str, sender: str, network: str, paused: bool) -> None:
        journal = TradeJournal(self.directory)
        try:
            with journal.db:
                journal.db.execute("BEGIN IMMEDIATE")
                row = journal.db.execute("SELECT * FROM automation_authorizations WHERE id=?", (ident,)).fetchone()
                if row is None or (row["owner"], row["sender"], row["genesis"]) != (
                    owner, sender, GENESIS[network]
                ) or row["status"] not in ("authorized", "running", "paused"):
                    raise TransferError("No active authorization belongs to this Automation Wallet")
                status = "paused" if paused else ("running" if self._worker and self._worker.running else "authorized")
                journal.db.execute(
                    "UPDATE automation_authorizations SET entries_paused=?,status=?,detail=? WHERE id=?",
                    (int(paused), status, "Entries paused; exits remain eligible" if paused else "Entries enabled", ident),
                )
        finally:
            journal.close()

    def stop(self, owner: str, sender: str | None = None, detail: str = "Emergency stop requested") -> None:
        self.manager.lock()
        with self._lock:
            worker = self._worker
            if worker:
                worker.stop()
        journal = TradeJournal(self.directory)
        try:
            with journal.db:
                journal.db.execute("BEGIN IMMEDIATE")
                args: tuple = (owner,)
                where = "owner=?"
                if sender is not None:
                    where += " AND sender=?"
                    args = (owner, sender)
                journal.db.execute(
                    f"UPDATE automation_authorizations SET status='stopped',detail=? WHERE {where} "
                    "AND status IN ('authorized','running','paused')",
                    (detail, *args),
                )
        finally:
            journal.close()

    def shutdown(self) -> None:
        self.manager.lock()
        with self._lock:
            worker = self._worker
            if worker:
                worker.stop()
                if worker.thread and worker.thread is not threading.current_thread():
                    worker.thread.join(timeout=2)
            self._worker = None

    def status(self, library, owner: str, network: str, sender: str | None, plan: dict | None) -> dict:
        result = {
            "runtime": {"running": False, "last_check": None, "detail": "No active authorization"},
            "authorizations": [], "balance_lamports": None, "balance_error": None,
            "positions": [], "orders": [], "withdrawals": [], "pending": False,
            "readiness": {"implementation": "available", "activation_blockers": list(self.readiness())},
        }
        if sender is None:
            return result
        journal = TradeJournal(self.directory)
        try:
            self._reset_restart_state(journal)
            genesis = GENESIS[network]
            result["authorizations"] = authorization_rows(journal, sender, genesis)
            from .web_trading import state as trade_state
            result.update({key: value for key, value in trade_state(self.directory, sender, network).items()
                           if key in ("positions", "orders", "withdrawals", "pending")})
            try:
                rpc = self.rpc(network)
                if rpc.genesis() != genesis:
                    raise TransferError("RPC network identity does not match selected network")
                result["balance_lamports"] = str(_uint(
                    rpc.call("getBalance", [sender, {"commitment": "confirmed"}])["value"], "balance"
                ))
            except (TransferError, OSError, TimeoutError) as exc:
                result["balance_error"] = str(exc)
            with self._lock:
                worker = self._worker
                if worker:
                    result["runtime"] = worker.public()
            if result["authorizations"]:
                active = result["authorizations"][0]
                if active["status"] in ("authorized", "running", "paused") and not self.manager.available(active["id"]):
                    status = "expired" if time.time() >= active["expires_at"] else "locked"
                    detail = (
                        "Authorization expired; review and authorize a new run"
                        if status == "expired"
                        else "No in-memory signer is available; authorize again"
                    )
                    with journal.db:
                        journal.db.execute(
                            "UPDATE automation_authorizations SET status=?,detail=? WHERE id=?",
                            (status, detail, active["id"]),
                        )
                    active["status"], active["detail"] = status, detail
        finally:
            journal.close()
        return result

    def recovery_withdraw_review(self, owner: str, network: str, sender: str, amount: str) -> dict:
        self.stop(owner, sender, "Stopped for reviewed recovery")
        journal = TradeJournal(self.directory)
        try:
            service = WithdrawalService(self.rpc(network), journal)
            service.reconcile(sender)
            if journal.pending(sender, GENESIS[network]):
                raise TransferError("Resolve the original pending signature before recovery")
            preview = service.preview(sender, owner, amount)
            revision = wallet_revision(journal, preview.genesis, sender)
            with journal.db:
                journal.db.execute(
                    "INSERT INTO automation_recovery_reviews VALUES (?,?,?,?,?,?,?,?,?)",
                    (preview.id, owner, preview.genesis, sender, "withdraw", _encode(asdict(preview)),
                     revision, time.time(), "reviewed"),
                )
            return {
                **asdict(preview),
                **{key: str(getattr(preview, key)) for key in ("lamports", "fee", "balance")},
                "expires_at": preview.created_at + 90,
            }
        finally:
            journal.close()

    def recovery_withdraw_submit(self, owner: str, network: str, sender: str,
                                 review_id: str, password: str) -> str:
        self.stop(owner, sender, "Stopped for reviewed recovery")
        journal = TradeJournal(self.directory)
        wallet = None
        try:
            row = journal.db.execute(
                "SELECT * FROM automation_recovery_reviews WHERE id=?", (review_id,)
            ).fetchone()
            if row is None or row["status"] != "reviewed" or (
                row["owner"], row["sender"], row["genesis"], row["kind"]
            ) != (owner, sender, GENESIS[network], "withdraw"):
                raise TransferError("Recovery review does not match this owner and wallet")
            if wallet_revision(journal, row["genesis"], sender) != row["revision"]:
                raise TransferError("Wallet state changed; review recovery again")
            preview = WithdrawalPreview(**json.loads(row["payload"]))
            wallet = WalletVault(self.directory / "automation-wallet").unlock(password)
            if wallet.address != sender:
                raise TransferError("Vault address changed after recovery review")
            signature = WithdrawalService(self.rpc(network), journal).submit(preview, wallet)
            with journal.db:
                journal.db.execute(
                    "UPDATE automation_recovery_reviews SET status='submitted' WHERE id=?", (review_id,)
                )
            return signature
        except WalletVaultError as exc:
            raise TransferError(str(exc)) from None
        finally:
            if wallet is not None:
                del wallet
            journal.close()

    def recovery_release_position(self, owner: str, network: str, sender: str,
                                  position_id: str) -> dict:
        """Explicitly end strategy management before a password-reviewed sell."""
        self.stop(owner, sender, "Stopped for manual position recovery")
        from .reservations import release

        journal = TradeJournal(self.directory)
        try:
            genesis = GENESIS[network]
            if journal.pending(sender, genesis):
                raise TransferError("Resolve the original pending signature before manual recovery")
            position = journal.position(position_id)
            if (position["sender"], position["genesis"], position["status"]) != (
                sender, genesis, "open",
            ):
                raise TransferError("Choose an open Automation Wallet position")
            owner_row = journal.db.execute(
                "SELECT agent_id,token_hold FROM agent_positions WHERE position_id=?", (position_id,)
            ).fetchone()
            if owner_row is None:
                return {"position_id": position_id, "released": True, "agent_id": None}
            # Release idle cash first so the manual sell has conservative fee and
            # account-funding room. Bound reviews must be cancelled explicitly.
            cash = list(journal.db.execute(
                "SELECT id FROM agent_reservations WHERE sender=? AND genesis=? "
                "AND status='held' AND position_id IS NULL", (sender, genesis),
            ))
            for hold in cash:
                release(journal, hold["id"])
            release(journal, owner_row["token_hold"])
            return {
                "position_id": position_id, "released": True,
                "agent_id": owner_row["agent_id"], "cash_holds_released": len(cash),
            }
        finally:
            journal.close()

    def recovery_sell_review(self, owner: str, network: str, sender: str,
                             position_id: str, slippage_bps: int) -> dict:
        self.stop(owner, sender, "Stopped for reviewed position recovery")
        journal = TradeJournal(self.directory)
        try:
            if type(slippage_bps) is not int or not 1 <= slippage_bps <= 500:
                raise TransferError("Recovery slippage must be 1–500 basis points")
            if journal.pending(sender, GENESIS[network]):
                raise TransferError("Resolve the original pending signature before recovery")
            position = journal.position(position_id)
            if (position["sender"], position["genesis"], position["status"]) != (
                sender, GENESIS[network], "open",
            ):
                raise TransferError("Choose an open Automation Wallet position")
            held = journal.db.execute(
                "SELECT 1 FROM agent_reservations WHERE position_id=? AND status='held'",
                (position_id,),
            ).fetchone()
            if held:
                raise TransferError("Release strategy management before reviewing a recovery sell")
            entry = journal.previews(position_id)
            decimals = int(entry.get("decimals", 6))
            amount = format(Decimal(int(position["quantity"])) / Decimal(10**decimals), "f")
            service = ManualTradeService(self.rpc(network), journal)
            preview = service.preview(
                sender, position["mint"], "sell", amount, slippage_bps, position_id
            )
            revision = wallet_revision(journal, preview["genesis"], sender)
            with journal.db:
                journal.db.execute(
                    "INSERT INTO automation_recovery_reviews VALUES (?,?,?,?,?,?,?,?,?)",
                    (preview["id"], owner, preview["genesis"], sender, "sell",
                     _encode({"preview_id": preview["id"]}), revision, time.time(), "reviewed"),
                )
            from .web_trading import _preview_view

            view = _preview_view(preview)
            view.pop("unsigned_transaction", None)
            return view
        finally:
            journal.close()

    def recovery_sell_submit(self, owner: str, network: str, sender: str,
                             review_id: str, password: str) -> str:
        self.stop(owner, sender, "Stopped for reviewed position recovery")
        journal = TradeJournal(self.directory)
        wallet = None
        try:
            row = journal.db.execute(
                "SELECT * FROM automation_recovery_reviews WHERE id=?", (review_id,)
            ).fetchone()
            if row is None or row["status"] != "reviewed" or (
                row["owner"], row["sender"], row["genesis"], row["kind"]
            ) != (owner, sender, GENESIS[network], "sell"):
                raise TransferError("Recovery sell review does not match this owner and wallet")
            if wallet_revision(journal, row["genesis"], sender) != row["revision"]:
                raise TransferError("Wallet state changed; review the recovery sell again")
            preview_id = json.loads(row["payload"])["preview_id"]
            wallet = WalletVault(self.directory / "automation-wallet").unlock(password)
            if wallet.address != sender:
                raise TransferError("Vault address changed after recovery review")
            signature = ManualTradeService(self.rpc(network), journal).submit(preview_id, wallet)
            with journal.db:
                journal.db.execute(
                    "UPDATE automation_recovery_reviews SET status='submitted' WHERE id=?", (review_id,)
                )
            return signature
        except WalletVaultError as exc:
            raise TransferError(str(exc)) from None
        finally:
            if wallet is not None:
                del wallet
            journal.close()

    def recovery_reconcile(self, owner: str, network: str, sender: str) -> None:
        self.stop(owner, sender, "Stopped while reconciling recovery receipts")
        journal = TradeJournal(self.directory)
        try:
            rpc = self.rpc(network)
            ManualTradeService(rpc, journal).reconcile(sender)
            WithdrawalService(rpc, journal).reconcile(sender)
        finally:
            journal.close()


class AutomationSupervisor:
    """Production scheduler. It never accepts transaction bytes or a wallet key."""

    def __init__(self, controller: AutomationController, ident: str, network: str, interval: int):
        self.controller, self.ident, self.network, self.interval = controller, ident, network, interval
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_check: float | None = None
        self.detail = "Starting"
        self.error: str | None = None

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive() and not self.stop_event.is_set())

    def public(self):
        return {"running": self.running, "last_check": self.last_check,
                "detail": self.error or self.detail, "authorization_id": self.ident}

    def start(self):
        self.thread = threading.Thread(target=self._run, name="pump-automation-worker", daemon=False)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        failures = 0
        while not self.stop_event.is_set() and self.controller.manager.available(self.ident):
            try:
                self.cycle()
                failures = 0
            except TransferError as exc:
                failures += 1
                self.detail = str(exc)[:350]
                if failures >= 3:
                    self.error = self.detail
                    self.controller.manager.lock(self.ident)
                    self._mark("faulted", self.error)
                    return
            self.stop_event.wait(min(300, self.interval * 2 ** min(failures, 4)))
        if self.error is None:
            if not self.stop_event.is_set():
                self.detail = "Authorization expired or locked; no automatic restart"
                self._mark("expired", self.detail)
            else:
                self.detail = "Stopped; no automatic restart"

    def _mark(self, status: str, detail: str):
        journal = TradeJournal(self.controller.directory)
        try:
            with journal.db:
                journal.db.execute(
                    "UPDATE automation_authorizations SET status=?,detail=? WHERE id=?",
                    (status, detail, self.ident),
                )
        finally:
            journal.close()

    def cycle(self) -> dict:
        if self.stop_event.is_set() or not self.controller.manager.available(self.ident):
            raise TransferError("Automation stopped")
        from .library import StrategyLibrary

        library = StrategyLibrary(self.controller.directory)
        journal = TradeJournal(self.controller.directory)
        try:
            row = journal.db.execute(
                "SELECT * FROM automation_authorizations WHERE id=?", (self.ident,)
            ).fetchone()
            if row is None or row["status"] not in ("running", "paused"):
                raise TransferError("Automation authorization is not running")
            scope = json.loads(row["scope"])
            rpc = self.controller.rpc(self.network)
            if rpc.genesis() != row["genesis"]:
                raise TransferError("Automation RPC network identity changed")
            manual = ManualTradeService(rpc, journal)
            manual.reconcile(row["sender"])
            WithdrawalService(rpc, journal).reconcile(row["sender"])
            if journal.pending(row["sender"], row["genesis"]):
                self.detail = "Original signature unresolved; no new order is created"
                self.last_check = time.time()
                return {"status": "blocked", "detail": self.detail}
            service = AgentTradeService(library, rpc, journal)
            items = []
            # Exits always come first and remain eligible while entries are paused.
            for agent_id, strategy in scope["strategies"].items():
                for position in journal.db.execute(
                    "SELECT p.mint,p.id FROM agent_positions a "
                    "JOIN trade_positions p ON p.id=a.position_id "
                    "JOIN agent_reservations h ON h.id=a.token_hold "
                    "WHERE a.agent_id=? AND p.sender=? AND p.genesis=? "
                    "AND p.status='open' AND h.status='held' ORDER BY p.opened_at,p.id",
                    (agent_id, row["sender"], row["genesis"]),
                ):
                    items.append((0, agent_id, strategy["cash_hold"], position["mint"], position["id"]))
            if not row["entries_paused"]:
                for agent_id, strategy in scope["strategies"].items():
                    attempted = {
                        item[0] for item in journal.db.execute(
                            "SELECT mint FROM agent_orders WHERE agent_id=? AND side='buy' "
                            "AND status IN ('review','submitted','finalized','failed')", (agent_id,)
                        )
                    }
                    seen = set()
                    for decision in library.decisions(agent_id, 1000):
                        mint = decision["mint"]
                        if mint in seen:
                            continue
                        seen.add(mint)
                        if decision["kind"] == "entry_recommended" and mint not in attempted:
                            items.append((1, agent_id, strategy["cash_hold"], mint, None))
            result = {"status": "waiting", "detail": "No eligible strategy action"}
            for _, agent_id, cash_hold, mint, position_id in sorted(items):
                if self.stop_event.is_set():
                    raise TransferError("Automation stopped")
                prepared = None
                try:
                    candidate = service.candidate(
                        agent_id, row["sender"], mint, cash_hold,
                        position_id=position_id, slippage_bps=scope["slippage_bps"],
                    )
                    if candidate["status"] != "ready":
                        continue
                    preview = service.prepare(
                        agent_id, row["sender"], mint, cash_hold,
                        position_id=position_id, slippage_bps=scope["slippage_bps"],
                    )
                    prepared = preview["id"]
                    signature = self.controller.manager.submit(service, self.ident, preview["id"])
                    result = {"status": "submitted", "detail": "Original signature recorded and broadcast",
                              "order_id": preview["id"], "signature": signature, "side": preview["side"]}
                    break
                except TransferError as exc:
                    # A stale unsigned review is safe to cancel. Submitted/unknown orders are never retried.
                    review = journal.db.execute(
                        "SELECT status FROM agent_orders WHERE id=?", (prepared,)
                    ).fetchone() if prepared else None
                    if review and review[0] == "review":
                        cancel(journal, prepared)
                    result = {"status": "checked", "detail": str(exc)[:350]}
            self.last_check = time.time()
            self.detail = result["detail"]
            with journal.db:
                journal.db.execute(
                    "INSERT INTO automation_events(authorization_id,at,kind,detail,payload) VALUES (?,?,?,?,?)",
                    (self.ident, self.last_check, "cycle", self.detail, _encode(result)),
                )
            return result
        finally:
            journal.close()
            library.close()
