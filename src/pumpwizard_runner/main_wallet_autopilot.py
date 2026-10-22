"""Main-wallet Autopilot allowance and browser-runtime boundaries.

This module deliberately does not contain a wallet key or a transaction builder.
It binds an already reviewed strategy order to a durable allowance and an
ephemeral browser runtime.  The normal agent/manual trade services remain
responsible for construction, simulation, exact-message validation, durable
wallet exclusion, broadcast and receipt recovery.

Production activation is intentionally unavailable in this development slice.
Tests may opt into the synthetic runtime explicitly when constructing
``RuntimeManager``; the default can never be enabled by request data.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import uuid

from solders.transaction import Transaction

from . import strategy_risk, wallet_risk
from .pump_route import MAX_ACCOUNT_FUNDING, MAX_FEE, ROUTES, key, unsigned_transaction
from .pump_swap_route import ROUTES as SWAP_ROUTES
from .transfers import TransferError, _uint


SCHEMA = """
CREATE TABLE IF NOT EXISTS main_wallet_allowance_reviews (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, genesis TEXT NOT NULL,
    sender TEXT NOT NULL, strategy_digest TEXT NOT NULL,
    policy_revision INTEGER NOT NULL, requested_permissions TEXT NOT NULL,
    scope TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS main_wallet_allowances (
    id TEXT PRIMARY KEY, review_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL, genesis TEXT NOT NULL, sender TEXT NOT NULL,
    strategy_digest TEXT NOT NULL, policy_revision INTEGER NOT NULL,
    authorization_revision INTEGER NOT NULL, requested_permissions TEXT NOT NULL,
    granted_permissions TEXT NOT NULL, scope TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, status TEXT NOT NULL,
    entries_paused INTEGER NOT NULL, detail TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS active_main_wallet_session_allowance
    ON main_wallet_allowances(session_id,genesis,sender)
    WHERE status IN ('authorized','active','entry-paused');
CREATE UNIQUE INDEX IF NOT EXISTS active_main_wallet_cash_allowance
    ON main_wallet_allowances(json_extract(scope,'$.cash_hold'))
    WHERE status IN ('authorized','active','entry-paused');
CREATE TABLE IF NOT EXISTS main_wallet_allowance_orders (
    order_id TEXT PRIMARY KEY, allowance_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,
    side TEXT NOT NULL, debit TEXT NOT NULL, authorized_at REAL NOT NULL,
    authorization_revision INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS main_wallet_allowance_usage
    ON main_wallet_allowance_orders(allowance_id,authorized_at);
CREATE TABLE IF NOT EXISTS main_wallet_allowance_events (
    id INTEGER PRIMARY KEY, allowance_id TEXT NOT NULL, at REAL NOT NULL,
    kind TEXT NOT NULL, detail TEXT NOT NULL, payload TEXT NOT NULL
);
"""

REVIEW_SECONDS = 60
HEARTBEAT_SECONDS = 20
RUNTIME_GRACE_SECONDS = 45
MIN_DURATION_SECONDS = 60
MAX_DURATION_SECONDS = 3600
ACTIVE_STATUSES = frozenset(("authorized", "active", "entry-paused"))
TERMINAL_STATUSES = frozenset(("revoked", "expired", "locked", "replaced"))
EXIT_DEBIT = MAX_ACCOUNT_FUNDING + MAX_FEE
ROUTE_ALLOWLIST = tuple(sorted((*ROUTES, *SWAP_ROUTES)))

PLAN_FIELDS = {
    "duration_seconds",
    "budget_lamports",
    "max_order_debit_lamports",
    "max_daily_debit_lamports",
    "max_orders",
    "max_trades_per_day",
    "max_open_positions",
    "slippage_bps",
}


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_encode(value).encode()).hexdigest()


def _policy_hash(value: object) -> str:
    return _hash(value)


def policy_revision(journal, session_id: str, genesis: str, sender: str) -> int:
    """Revision only risk/allocation policy writes, not ordinary wallet intents."""
    wallet_change = journal.db.execute(
        "SELECT COALESCE(MAX(id),0) FROM wallet_risk_changes WHERE genesis=? AND sender=?",
        (genesis, sender),
    ).fetchone()[0]
    session_change = journal.db.execute(
        "SELECT COALESCE(MAX(id),0) FROM strategy_risk_changes "
        "WHERE agent_id=? AND genesis=? AND sender=?",
        (session_id, genesis, sender),
    ).fetchone()[0]
    # Change IDs are monotonically increasing SQLite rowids. The policy hashes
    # below remain the authoritative exact-content comparison.
    return (int(wallet_change) << 32) | int(session_change)


def install(journal) -> None:
    """Install only the additive allowance tables in an existing wallet journal."""
    journal.db.executescript(SCHEMA)


def requested_permissions(
    automation_scope: str,
    custody: str = "main-wallet-browser",
) -> dict:
    """Derive permission intent from the immutable session brief."""
    if automation_scope not in ("entries-only", "exits-only", "entries-and-exits"):
        raise TransferError("Choose automatic entries only, exits only, or entries and exits")
    if custody not in ("main-wallet-browser", "main-wallet-service"):
        raise TransferError("Choose browser or local-service main-wallet custody")
    permissions = {
        "prepare_orders": True,
        "automatic_entries": automation_scope in ("entries-only", "entries-and-exits"),
        "automatic_protective_exits": automation_scope != "entries-only",
        "automatic_profit_exits": automation_scope != "entries-only",
        "winner_handoff": False,
        "user_signature_each_order": False,
        "custody": custody,
        "activation_required": True,
    }
    if custody == "main-wallet-service":
        permissions.update(
            service_vault_required=True,
            continues_without_browser=True,
        )
    return permissions


def granted_permissions(requested: dict) -> dict:
    """Return the currently implementable dry-run grant, never a live claim."""
    if requested.get("winner_handoff"):
        raise TransferError("Winner handoff is not implemented; choose strategy-managed exits")
    return {
        **requested,
        "winner_handoff": False,
        "synthetic_signing": True,
        "live_submission": False,
    }


def activation_blockers(custody: str = "main-wallet-browser") -> list[str]:
    runtime = (
        "This build exposes only the synthetic service-signing runtime"
        if custody == "main-wallet-service"
        else "This build exposes only the synthetic browser-signing runtime"
    )
    return [
        "Live main-wallet Autopilot activation has no funded lifecycle acceptance",
        runtime,
    ]


def _validate_plan(plan: object) -> dict:
    if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
        raise TransferError("Supply every main-wallet Autopilot limit")
    values = dict(plan)
    for name in (
        "duration_seconds",
        "budget_lamports",
        "max_order_debit_lamports",
        "max_daily_debit_lamports",
        "max_orders",
        "max_trades_per_day",
        "max_open_positions",
        "slippage_bps",
    ):
        _uint(values[name], name.replace("_", " "))
    duration = values["duration_seconds"]
    if type(duration) is not int or not MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS:
        raise TransferError("Autopilot duration must be 1–60 minutes")
    if type(values["max_orders"]) is not int or not 1 <= values["max_orders"] <= 100:
        raise TransferError("Autopilot total order count must be 1–100")
    if (
        type(values["max_trades_per_day"]) is not int
        or not 1 <= values["max_trades_per_day"] <= 100
    ):
        raise TransferError("Autopilot daily trade count must be 1–100")
    if (
        type(values["max_open_positions"]) is not int
        or not 1 <= values["max_open_positions"] <= 25
    ):
        raise TransferError("Autopilot open-position count must be 1–25")
    if type(values["slippage_bps"]) is not int or not 1 <= values["slippage_bps"] <= 500:
        raise TransferError("Autopilot slippage must be 1–500 basis points")
    for name in ("budget_lamports", "max_order_debit_lamports", "max_daily_debit_lamports"):
        if values[name] <= 0:
            raise TransferError("Autopilot debit limits must be greater than zero")
    if values["max_order_debit_lamports"] > values["budget_lamports"]:
        raise TransferError("Maximum order debit cannot exceed the total allowance budget")
    if values["max_daily_debit_lamports"] > values["budget_lamports"]:
        raise TransferError("Daily debit cannot exceed the total allowance budget")
    return values


def _session_permissions(library, session_id: str) -> tuple[dict, str]:
    brief = library.session_brief(session_id)
    permissions = brief.get("permissions")
    custody = permissions.get("custody") if isinstance(permissions, dict) else None
    if (
        brief.get("mode") != "trade-autopilot"
        or brief.get("execution_style") != "autopilot"
        or not isinstance(permissions, dict)
        or custody not in ("main-wallet-browser", "main-wallet-service")
        or permissions.get("generated_wallet_required") is not True
        or permissions.get("user_signature_each_order") is not False
    ):
        raise TransferError("This session does not have a generated main-wallet Autopilot brief")
    if custody == "main-wallet-service" and (
        brief.get("schema") != "pumpwizard.session-brief/v4"
        or permissions.get("service_vault_required") is not True
        or permissions.get("continues_without_browser") is not True
    ):
        raise TransferError("Service custody requires an explicit v4 session brief")
    scope = brief.get("automation_scope")
    if scope is None:
        # v2 Autopilot briefs only represented the full entries-and-exits mode.
        scope = "entries-and-exits"
    requested = requested_permissions(str(scope), str(custody))
    for name, value in requested.items():
        if permissions.get(name) != value:
            raise TransferError("Session Autopilot permissions no longer match its immutable brief")
    return requested, str(scope)


def _cash_hold(journal, session_id: str, genesis: str, sender: str):
    rows = strategy_risk._cash_holds(journal, session_id, genesis, sender)
    if len(rows) != 1:
        raise TransferError("The session needs exactly one durable main-wallet cash allocation")
    return rows[0]


def _current_binding(library, journal, session_id: str, genesis: str, sender: str) -> dict:
    instance = library.instance(session_id)
    strategy_policy = strategy_risk.policy(journal, session_id, genesis, sender)
    wallet_policy = wallet_risk.policy(journal, genesis, sender)
    if strategy_policy is None:
        raise TransferError("Configure this session's allocation and risk limits first")
    if wallet_policy is None or wallet_policy.get("portfolio_drawdown_lamports") is None:
        raise TransferError("Configure every shared wallet limit, including portfolio drawdown")
    cash = _cash_hold(journal, session_id, genesis, sender)
    from .session_allocations import binding

    if (binding(journal, session_id, genesis, sender) is None
            and int(cash["lamports"]) > strategy_policy["allocation_lamports"]):
        raise TransferError("The session cash hold exceeds its current allocation policy")
    return {
        "strategy_digest": instance["strategy_digest"],
        "strategy_policy": strategy_policy,
        "strategy_policy_hash": _policy_hash(strategy_policy),
        "wallet_policy": wallet_policy,
        "wallet_policy_hash": _policy_hash(wallet_policy),
        "policy_revision": policy_revision(journal, session_id, genesis, sender),
        "cash_hold": cash["id"],
        "cash_lamports": int(cash["lamports"]),
    }


def _review_scope(
    library,
    journal,
    *,
    session_id: str,
    sender: str,
    network: str,
    genesis: str,
    wallet_kind: str,
    plan: object,
    now: float,
    custody_identity: str | None = None,
) -> tuple[dict, dict]:
    values = _validate_plan(plan)
    requested, automation_scope = _session_permissions(library, session_id)
    binding = _current_binding(library, journal, session_id, genesis, sender)
    strategy_policy = binding["strategy_policy"]
    wallet_policy = binding["wallet_policy"]
    blockers = []
    if wallet_kind != "local":
        blockers.append("Extension wallets cannot provide unattended signing")
    custody = requested["custody"]
    if custody == "main-wallet-service":
        if not isinstance(custody_identity, str) or not re.fullmatch(r"[0-9a-f]{32}", custody_identity):
            blockers.append("Choose the exact enrolled service wallet before review")
    elif custody_identity is not None:
        blockers.append("Browser custody cannot bind a service vault identity")
    if requested["automatic_entries"] and not strategy_policy["buys_enabled"]:
        blockers.append("Session buys are disabled")
    if not strategy_policy["sells_enabled"]:
        blockers.append("Session managed exits are disabled")
    if requested["automatic_entries"] and wallet_policy["entries_paused"]:
        blockers.append("Shared wallet entries are paused")
    if values["budget_lamports"] > binding["cash_lamports"]:
        blockers.append("Allowance budget exceeds the session's existing cash reservation")
    order_ceiling = min(
        wallet_policy["max_buy_lamports"],
        strategy_policy["max_trade_lamports"] + EXIT_DEBIT,
    )
    if values["max_order_debit_lamports"] > order_ceiling:
        blockers.append("Fee-inclusive order debit exceeds current wallet or session limits")
    daily_ceiling = min(
        values["budget_lamports"],
        strategy_policy["max_daily_buy_lamports"]
        + values["max_trades_per_day"] * EXIT_DEBIT,
    )
    if values["max_daily_debit_lamports"] > daily_ceiling:
        blockers.append("Fee-inclusive daily debit exceeds the current session limit")
    if values["max_trades_per_day"] > strategy_policy["max_trades_per_day"]:
        blockers.append("Daily trade count exceeds the current session limit")
    if values["max_open_positions"] > strategy_policy["max_open_positions"]:
        blockers.append("Open-position count exceeds the current session limit")
    exit_headroom = values["max_open_positions"] * EXIT_DEBIT
    if values["budget_lamports"] <= exit_headroom:
        blockers.append("Allowance budget must retain one conservative exit debit per position")
    if values["max_order_debit_lamports"] < EXIT_DEBIT:
        blockers.append("Maximum order debit must cover one conservative exit debit")
    if values["max_daily_debit_lamports"] < exit_headroom:
        blockers.append("Daily debit must cover conservative exits for every allowed position")
    scope = {
        "schema": (
            "pumpwizard.main-wallet-allowance-review/v2"
            if custody == "main-wallet-service"
            else "pumpwizard.main-wallet-allowance-review/v1"
        ),
        "session_id": session_id,
        "sender": sender,
        "network": network,
        "genesis": genesis,
        "wallet_kind": wallet_kind,
        **binding,
        **values,
        "automation_scope": automation_scope,
        "routes": list(ROUTE_ALLOWLIST),
        "exit_debit_lamports": EXIT_DEBIT,
        "exit_headroom_lamports": exit_headroom,
        "utc_day_boundary": "00:00 UTC",
        "valuation_rule": "fresh wallet-risk liquidation snapshot required for entries",
        "preflight_blockers": blockers,
        "activation_blockers": activation_blockers(custody),
        "reviewed_at": now,
    }
    if custody == "main-wallet-service":
        scope.update(custody=custody, custody_identity=custody_identity)
    return requested, scope


def review(
    library,
    journal,
    *,
    session_id: str,
    sender: str,
    network: str,
    genesis: str,
    wallet_kind: str,
    plan: object,
    persist: bool = True,
    now: float | None = None,
    custody_identity: str | None = None,
) -> dict:
    """Review current policy/reservation state without granting signing permission."""
    install(journal)
    checked_at = time.time() if now is None else now
    requested, scope = _review_scope(
        library,
        journal,
        session_id=session_id,
        sender=sender,
        network=network,
        genesis=genesis,
        wallet_kind=wallet_kind,
        plan=plan,
        now=checked_at,
        custody_identity=custody_identity,
    )
    ident = uuid.uuid4().hex
    expires_at = checked_at + REVIEW_SECONDS
    scope = {
        **scope,
        "id": ident,
        "expires_at": expires_at,
        "ready": not scope["preflight_blockers"],
        "can_activate": False,
    }
    scope["binding_sha256"] = _hash({
        key: value for key, value in scope.items()
        if key not in ("id", "expires_at", "ready", "can_activate", "reviewed_at")
    })
    if persist:
        with journal.db:
            journal.db.execute(
                "INSERT INTO main_wallet_allowance_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    session_id,
                    genesis,
                    sender,
                    scope["strategy_digest"],
                    scope["policy_revision"],
                    _encode(requested),
                    _encode(scope),
                    checked_at,
                    expires_at,
                    "reviewed",
                ),
            )
    return {**scope, "requested_permissions": requested}


def _allowance_row(journal, ident: str):
    row = journal.db.execute(
        "SELECT * FROM main_wallet_allowances WHERE id=?", (ident,)
    ).fetchone()
    if row is None:
        raise TransferError("Main-wallet allowance was not found")
    return row


def _public_allowance(journal, row) -> dict:
    usage = list(journal.db.execute(
        "SELECT side,debit,authorized_at FROM main_wallet_allowance_orders "
        "WHERE allowance_id=? ORDER BY authorized_at,order_id",
        (row["id"],),
    ))
    value = dict(row)
    value.update(
        requested_permissions=json.loads(row["requested_permissions"]),
        granted_permissions=json.loads(row["granted_permissions"]),
        scope=json.loads(row["scope"]),
        entries_paused=bool(row["entries_paused"]),
        used_lamports=sum(int(item["debit"]) for item in usage),
        used_orders=len(usage),
    )
    return value


def allowance_rows(journal, sender: str, genesis: str) -> list[dict]:
    install(journal)
    return [
        _public_allowance(journal, row)
        for row in journal.db.execute(
            "SELECT * FROM main_wallet_allowances WHERE sender=? AND genesis=? "
            "ORDER BY created_at DESC,id DESC",
            (sender, genesis),
        )
    ]


def authorize(library, journal, review_id: str, *, now: float | None = None) -> dict:
    """Turn one fresh review into durable authorization; runtime remains inactive."""
    install(journal)
    authorized_at = time.time() if now is None else now
    row = journal.db.execute(
        "SELECT * FROM main_wallet_allowance_reviews WHERE id=?", (review_id,)
    ).fetchone()
    if row is None or row["status"] != "reviewed":
        raise TransferError("Choose a fresh, unused main-wallet allowance review")
    reviewed = json.loads(row["scope"])
    if authorized_at >= row["expires_at"]:
        raise TransferError("Main-wallet allowance review expired; review again")
    plan = {name: reviewed[name] for name in PLAN_FIELDS}
    requested, current = _review_scope(
        library,
        journal,
        session_id=row["session_id"],
        sender=row["sender"],
        network=reviewed["network"],
        genesis=row["genesis"],
        wallet_kind=reviewed["wallet_kind"],
        plan=plan,
        now=reviewed["reviewed_at"],
        custody_identity=reviewed.get("custody_identity"),
    )
    current_hash = _hash({
        key: value for key, value in current.items()
        if key != "reviewed_at"
    })
    if current_hash != reviewed["binding_sha256"]:
        raise TransferError("Wallet, strategy, allocation or policy changed; review again")
    if current["preflight_blockers"]:
        raise TransferError("Allowance preflight is blocked: " + "; ".join(current["preflight_blockers"]))
    granted = granted_permissions(requested)
    ident = uuid.uuid4().hex
    expires_at = authorized_at + reviewed["duration_seconds"]
    scope = {
        **{key: value for key, value in reviewed.items() if key not in (
            "id", "expires_at", "ready", "can_activate", "binding_sha256",
            "preflight_blockers", "activation_blockers", "reviewed_at",
        )},
        "schema": (
            "pumpwizard.main-wallet-allowance/v2"
            if requested["custody"] == "main-wallet-service"
            else "pumpwizard.main-wallet-allowance/v1"
        ),
        "review_id": review_id,
        "authorization_revision": 1,
    }
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        if policy_revision(
            journal, row["session_id"], row["genesis"], row["sender"]
        ) != row["policy_revision"]:
            raise TransferError("Wallet policy revision changed while authorizing; review again")
        journal.db.execute(
            "UPDATE main_wallet_allowances SET status='replaced',"
            "detail='Replaced by a new allowance' WHERE session_id=? AND genesis=? "
            "AND sender=? AND status IN ('authorized','active','entry-paused')",
            (row["session_id"], row["genesis"], row["sender"]),
        )
        journal.db.execute(
            "INSERT INTO main_wallet_allowances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ident,
                review_id,
                row["session_id"],
                row["genesis"],
                row["sender"],
                row["strategy_digest"],
                row["policy_revision"],
                1,
                _encode(requested),
                _encode(granted),
                _encode(scope),
                authorized_at,
                expires_at,
                "authorized",
                0,
                (
                    "Authorized; service signer unlock remains separate"
                    if requested["custody"] == "main-wallet-service"
                    else "Authorized; browser runtime activation remains separate"
                ),
            ),
        )
        journal.db.execute(
            "UPDATE main_wallet_allowance_reviews SET status='authorized' WHERE id=?",
            (review_id,),
        )
        journal.db.execute(
            "INSERT INTO main_wallet_allowance_events"
            "(allowance_id,at,kind,detail,payload) VALUES (?,?,?,?,?)",
            (ident, authorized_at, "authorized", "Durable allowance created", "{}"),
        )
    return _public_allowance(journal, _allowance_row(journal, ident))


@dataclass
class RuntimeLease:
    token_sha256: bytes = field(repr=False)
    sender: str
    genesis: str
    authorization_revision: int
    wall_deadline: float
    monotonic_deadline: float
    heartbeat_deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)


@dataclass(frozen=True)
class SigningTicket:
    id: str
    allowance_id: str
    order_id: str
    sender: str
    authorization_revision: int
    message_sha256: str
    wire_sha256: str
    unsigned_transaction: str
    created_at: float
    expires_at: float

    def public(self) -> dict:
        return {
            "schema": "pumpwizard.main-wallet-signing-ticket/v1",
            **self.__dict__,
        }


class RuntimeManager:
    """Ephemeral browser presence and exact-transaction tickets.

    ``allow_synthetic`` is a constructor-only test harness capability.  It is
    intentionally absent from every request/scope payload.
    """

    def __init__(self, *, allow_synthetic: bool = False):
        self._allow_synthetic = allow_synthetic
        self._lock = threading.RLock()
        self._leases: dict[str, RuntimeLease] = {}
        self._tickets: dict[str, SigningTicket] = {}
        self._epoch = 0

    def activation_capability(self) -> dict:
        return {
            "available": self._allow_synthetic,
            "mode": "synthetic" if self._allow_synthetic else "blocked",
            "blockers": [] if self._allow_synthetic else activation_blockers(),
        }

    @staticmethod
    def _token_digest(token: str) -> bytes:
        return hashlib.sha256(token.encode()).digest()

    def _lease(self, ident: str, token: str) -> RuntimeLease:
        lease = self._leases.get(ident)
        if lease is None or not hmac.compare_digest(
            lease.token_sha256, self._token_digest(token)
        ):
            raise TransferError("Browser Autopilot runtime is locked or the token is invalid")
        now_wall, now_mono = time.time(), time.monotonic()
        if (
            lease.cancelled.is_set()
            or now_wall >= lease.wall_deadline
            or now_mono >= lease.monotonic_deadline
            or now_mono >= lease.heartbeat_deadline
        ):
            lease.cancelled.set()
            self._leases.pop(ident, None)
            raise TransferError("Browser Autopilot runtime expired or missed its heartbeat")
        return lease

    def available(self, ident: str) -> bool:
        with self._lock:
            lease = self._leases.get(ident)
            if lease is None:
                return False
            if (
                lease.cancelled.is_set()
                or time.time() >= lease.wall_deadline
                or time.monotonic() >= min(lease.monotonic_deadline, lease.heartbeat_deadline)
            ):
                lease.cancelled.set()
                self._leases.pop(ident, None)
                return False
            return True

    def activate(
        self,
        journal,
        ident: str,
        *,
        sender: str,
        genesis: str,
        wallet_kind: str,
    ) -> dict:
        install(journal)
        if not self._allow_synthetic:
            raise TransferError("Live main-wallet Autopilot activation remains blocked in this build")
        if wallet_kind != "local":
            raise TransferError("Only an unlocked PumpWizard Generated Wallet supports Autopilot")
        with self._lock:
            epoch = self._epoch
        token = secrets.token_urlsafe(32)
        now_wall, now_mono = time.time(), time.monotonic()
        with journal.db:
            journal.db.execute("BEGIN IMMEDIATE")
            row = _allowance_row(journal, ident)
            permissions = json.loads(row["granted_permissions"])
            if permissions.get("custody") != "main-wallet-browser":
                raise TransferError(
                    "Service-custody allowances require the local service-signing runtime"
                )
            if (row["sender"], row["genesis"], row["status"]) != (
                sender,
                genesis,
                "authorized",
            ):
                raise TransferError("Allowance is not authorized for this wallet and network")
            if now_wall >= row["expires_at"]:
                journal.db.execute(
                    "UPDATE main_wallet_allowances SET status='expired',detail=? WHERE id=?",
                    ("Allowance expired before activation", ident),
                )
                raise TransferError("Main-wallet allowance expired")
            journal.db.execute(
                "UPDATE main_wallet_allowances SET status='active',detail=? WHERE id=?",
                ("Synthetic browser runtime active", ident),
            )
            journal.db.execute(
                "INSERT INTO main_wallet_allowance_events"
                "(allowance_id,at,kind,detail,payload) VALUES (?,?,?,?,?)",
                (ident, now_wall, "activated", "Synthetic browser runtime activated", "{}"),
            )
        duration = row["expires_at"] - now_wall
        lease = RuntimeLease(
            token_sha256=self._token_digest(token),
            sender=sender,
            genesis=genesis,
            authorization_revision=row["authorization_revision"],
            wall_deadline=row["expires_at"],
            monotonic_deadline=now_mono + duration,
            heartbeat_deadline=now_mono + RUNTIME_GRACE_SECONDS,
        )
        cancelled = False
        with self._lock:
            cancelled = epoch != self._epoch
            if not cancelled:
                old = self._leases.pop(ident, None)
                if old:
                    old.cancelled.set()
                self._leases[ident] = lease
        if cancelled:
            with journal.db:
                journal.db.execute(
                    "UPDATE main_wallet_allowances SET status='locked',detail=? WHERE id=?",
                    ("Activation was cancelled before runtime installation", ident),
                )
            raise TransferError("Browser Autopilot activation was cancelled")
        return {
            "allowance_id": ident,
            "runtime_token": token,
            "authorization_revision": row["authorization_revision"],
            "heartbeat_seconds": HEARTBEAT_SECONDS,
            "heartbeat_deadline": now_wall + RUNTIME_GRACE_SECONDS,
            "expires_at": row["expires_at"],
            "mode": "synthetic",
        }

    def heartbeat(self, ident: str, token: str) -> dict:
        with self._lock:
            lease = self._lease(ident, token)
            lease.heartbeat_deadline = min(
                lease.monotonic_deadline,
                time.monotonic() + RUNTIME_GRACE_SECONDS,
            )
            return {
                "allowance_id": ident,
                "authorization_revision": lease.authorization_revision,
                "heartbeat_deadline": time.time()
                + min(RUNTIME_GRACE_SECONDS, lease.monotonic_deadline - time.monotonic()),
            }

    def pause_entries(self, journal, ident: str, token: str, paused: bool) -> None:
        with self._lock:
            self._lease(ident, token)
            with journal.db:
                row = _allowance_row(journal, ident)
                if row["status"] not in ("active", "entry-paused"):
                    raise TransferError("Allowance runtime is not active")
                journal.db.execute(
                    "UPDATE main_wallet_allowances SET status=?,entries_paused=?,detail=? WHERE id=?",
                    (
                        "entry-paused" if paused else "active",
                        int(paused),
                        "Entries paused; eligible exits remain available"
                        if paused
                        else "Synthetic browser runtime active",
                        ident,
                    ),
                )
            if paused:
                for ticket_id, ticket in list(self._tickets.items()):
                    if ticket.allowance_id != ident:
                        continue
                    try:
                        side = journal.previews(ticket.order_id).get("side")
                    except TransferError:
                        side = None
                    if side == "buy":
                        self._tickets.pop(ticket_id, None)

    def lock(self, journal, ident: str | None = None, *, revoked: bool = False) -> None:
        # Signal before waiting for SQLite so no new ticket can race revocation.
        for allowance_id, lease in list(self._leases.items()):
            if ident is None or allowance_id == ident:
                lease.cancelled.set()
        with self._lock:
            self._epoch += 1
            for allowance_id in list(self._leases):
                if ident is None or allowance_id == ident:
                    self._leases.pop(allowance_id, None)
            for ticket_id, ticket in list(self._tickets.items()):
                if ident is None or ticket.allowance_id == ident:
                    self._tickets.pop(ticket_id, None)
        install(journal)
        where, args = ("id=?", (ident,)) if ident else ("1=1", ())
        status = "revoked" if revoked else "locked"
        detail = "Allowance revoked; signing runtime removed" if revoked else (
            "Browser locked, disconnected, refreshed or Runner restarted"
        )
        with journal.db:
            journal.db.execute(
                f"UPDATE main_wallet_allowances SET status=?,detail=? WHERE {where} "
                "AND status IN ('authorized','active','entry-paused')",
                (status, detail, *args),
            )

    def issue_ticket(
        self,
        library,
        journal,
        ident: str,
        token: str,
        preview: dict,
        *,
        now: float | None = None,
    ) -> dict:
        install(journal)
        created_at = time.time() if now is None else now
        with self._lock:
            lease = self._lease(ident, token)
            for existing in list(self._tickets.values()):
                if existing.allowance_id == ident and existing.order_id == preview.get("id"):
                    if created_at < existing.expires_at:
                        return existing.public()
                    self._tickets.pop(existing.id, None)
                    break
            row = _allowance_row(journal, ident)
            scope = json.loads(row["scope"])
            self._validate_scope(library, journal, row, scope, preview)
            message = base64.b64decode(preview["message"], validate=True)
            wire = unsigned_transaction(message)
            ticket = SigningTicket(
                id=uuid.uuid4().hex,
                allowance_id=ident,
                order_id=preview["id"],
                sender=preview["sender"],
                authorization_revision=row["authorization_revision"],
                message_sha256=hashlib.sha256(message).hexdigest(),
                wire_sha256=hashlib.sha256(wire).hexdigest(),
                unsigned_transaction=base64.b64encode(wire).decode(),
                created_at=created_at,
                expires_at=min(
                    row["expires_at"],
                    preview["created_at"] + 60,
                    created_at + RUNTIME_GRACE_SECONDS,
                ),
            )
            if ticket.expires_at <= created_at or lease.cancelled.is_set():
                raise TransferError("Prepared order or browser runtime expired")
            self._tickets[ticket.id] = ticket
            return ticket.public()

    def next_ticket(self, ident: str, token: str) -> dict | None:
        with self._lock:
            self._lease(ident, token)
            now = time.time()
            candidates = sorted(
                (
                    ticket for ticket in self._tickets.values()
                    if ticket.allowance_id == ident and now < ticket.expires_at
                ),
                key=lambda ticket: (ticket.created_at, ticket.id),
            )
            return candidates[0].public() if candidates else None

    def complete_ticket(self, ident: str, ticket_id: str) -> None:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket and ticket.allowance_id == ident:
                self._tickets.pop(ticket_id, None)

    def _validate_scope(self, library, journal, row, scope: dict, preview: dict) -> None:
        context = preview.get("agent_order") or {}
        if row["status"] not in ("active", "entry-paused"):
            raise TransferError("Main-wallet allowance runtime is not active")
        if time.time() >= row["expires_at"]:
            raise TransferError("Main-wallet allowance expired")
        if (
            preview.get("sender"),
            preview.get("genesis"),
            preview.get("network"),
            context.get("agent_id"),
            context.get("cash_hold"),
        ) != (
            row["sender"],
            row["genesis"],
            scope["network"],
            row["session_id"],
            scope["cash_hold"],
        ):
            raise TransferError("Prepared order is outside this wallet, network, session or allocation")
        if preview.get("route") not in scope["routes"]:
            raise TransferError("Prepared order route is outside the allowance")
        if preview.get("slippage_bps", 501) > scope["slippage_bps"]:
            raise TransferError("Prepared order slippage exceeds the allowance")
        instance = library.instance(row["session_id"])
        if instance["strategy_digest"] != row["strategy_digest"]:
            raise TransferError("Exact strategy digest changed; create a new allowance")
        if policy_revision(
            journal, row["session_id"], row["genesis"], row["sender"]
        ) != row["policy_revision"]:
            raise TransferError("Wallet or allocation policy revision changed; create a new allowance")
        if _policy_hash(wallet_risk.policy(journal, row["genesis"], row["sender"])) != scope[
            "wallet_policy_hash"
        ]:
            raise TransferError("Shared wallet policy changed; create a new allowance")
        if _policy_hash(
            strategy_risk.policy(journal, row["session_id"], row["genesis"], row["sender"])
        ) != scope["strategy_policy_hash"]:
            raise TransferError("Session policy changed; create a new allowance")
        if row["entries_paused"] and preview.get("side") == "buy":
            raise TransferError("Allowance entries are paused; eligible exits remain available")
        if preview.get("side") == "buy" and not json.loads(row["granted_permissions"])[
            "automatic_entries"
        ]:
            raise TransferError("Allowance does not grant automatic entries")
        if preview.get("side") == "sell" and not json.loads(row["granted_permissions"])[
            "automatic_protective_exits"
        ]:
            raise TransferError("Allowance does not grant automatic exits")
        try:
            message = base64.b64decode(preview["message"], validate=True)
        except (KeyError, ValueError):
            raise TransferError("Prepared order has no valid reviewed transaction message") from None
        if not message:
            raise TransferError("Prepared order has no valid reviewed transaction message")
        stored = journal.previews(preview["id"])
        if stored != preview:
            raise TransferError("Prepared order changed after review")

    def reserve_order(
        self,
        library,
        journal,
        ident: str,
        token: str,
        ticket_id: str,
        preview: dict,
        message: bytes,
        *,
        now: float | None = None,
    ) -> None:
        """Consume allowance usage inside the normal trade reservation transaction."""
        if not journal.db.in_transaction:
            raise TransferError("Allowance usage must share the atomic trade reservation")
        authorized_at = time.time() if now is None else now
        with self._lock:
            lease = self._lease(ident, token)
            ticket = self._tickets.get(ticket_id)
            if ticket is None or (
                ticket.allowance_id,
                ticket.order_id,
                ticket.authorization_revision,
            ) != (ident, preview.get("id"), lease.authorization_revision):
                raise TransferError("Signing ticket does not match this allowance and order")
            if authorized_at >= ticket.expires_at:
                raise TransferError("Signing ticket expired")
            row = _allowance_row(journal, ident)
            scope = json.loads(row["scope"])
            self._validate_scope(library, journal, row, scope, preview)
            if hashlib.sha256(message).hexdigest() != ticket.message_sha256:
                raise TransferError("Allowance refuses a transaction message different from the ticket")
            if base64.b64decode(preview["message"], validate=True) != message:
                raise TransferError("Allowance refuses a transaction message different from review")
            wallet_risk.check_buy(journal, preview, preview["state"], limits=scope["wallet_policy"])
            strategy_risk.check_preview(journal, preview)
            debit = preview["fee"] + MAX_ACCOUNT_FUNDING + (
                preview["amount"] if preview["side"] == "buy" else 0
            )
            usage = list(journal.db.execute(
                "SELECT side,debit,authorized_at FROM main_wallet_allowance_orders "
                "WHERE allowance_id=?",
                (ident,),
            ))
            if len(usage) >= scope["max_orders"]:
                raise TransferError("Allowance total order count is exhausted")
            if sum(int(item["debit"]) for item in usage) + debit > scope["budget_lamports"]:
                raise TransferError("Allowance total fee-inclusive budget is exhausted")
            if debit > scope["max_order_debit_lamports"]:
                raise TransferError("Order exceeds the allowance's fee-inclusive debit limit")
            day = int(authorized_at // 86400) * 86400
            daily = [item for item in usage if day <= item["authorized_at"] < day + 86400]
            if len(daily) >= scope["max_trades_per_day"]:
                raise TransferError("Allowance UTC-day trade count is exhausted")
            daily_debit = sum(int(item["debit"]) for item in daily)
            if daily_debit + debit > scope["max_daily_debit_lamports"]:
                raise TransferError("Allowance UTC-day fee-inclusive debit budget is exhausted")
            if preview["side"] == "buy":
                open_positions = journal.db.execute(
                    "SELECT COUNT(*) FROM agent_positions a JOIN trade_positions p "
                    "ON p.id=a.position_id JOIN agent_reservations h ON h.id=a.token_hold "
                    "WHERE a.agent_id=? AND p.genesis=? AND p.sender=? "
                    "AND p.status='open' AND h.status='held'",
                    (row["session_id"], row["genesis"], row["sender"]),
                ).fetchone()[0]
                if open_positions >= scope["max_open_positions"]:
                    raise TransferError("Allowance session open-position capacity is full")
                used = sum(int(item["debit"]) for item in usage)
                remaining_positions = open_positions + 1
                if (
                    scope["budget_lamports"] - used - debit
                    < remaining_positions * scope["exit_debit_lamports"]
                ):
                    raise TransferError("Allowance must retain a conservative exit debit per position")
                if (
                    scope["max_daily_debit_lamports"] - daily_debit - debit
                    < remaining_positions * scope["exit_debit_lamports"]
                ):
                    raise TransferError(
                        "Allowance UTC-day budget must retain a conservative exit debit per position"
                    )
            journal.db.execute(
                "INSERT INTO main_wallet_allowance_orders VALUES (?,?,?,?,?,?,?,?)",
                (
                    preview["id"],
                    ident,
                    ticket_id,
                    row["session_id"],
                    preview["side"],
                    str(debit),
                    authorized_at,
                    row["authorization_revision"],
                ),
            )
            if lease.cancelled.is_set():
                raise TransferError("Browser Autopilot runtime was revoked before reservation")

    def validate_signed_ticket(
        self,
        ident: str,
        token: str,
        ticket_id: str,
        signed_transaction: str,
    ) -> str:
        """Verify the browser returned the exact ticket wire and signer signature."""
        if not isinstance(signed_transaction, str) or not 1 <= len(signed_transaction) <= 4096:
            raise TransferError("Browser returned an invalid signed transaction")
        with self._lock:
            self._lease(ident, token)
            ticket = self._tickets.get(ticket_id)
            if ticket is None or ticket.allowance_id != ident:
                raise TransferError("Signing ticket is missing or belongs to another allowance")
            if time.time() >= ticket.expires_at:
                raise TransferError("Signing ticket expired")
            try:
                raw = base64.b64decode(signed_transaction, validate=True)
                transaction = Transaction.from_bytes(raw)
                unsigned = base64.b64decode(ticket.unsigned_transaction, validate=True)
            except Exception:
                raise TransferError("Browser returned an unreadable signed transaction") from None
            signatures = list(transaction.signatures)
            message = bytes(transaction.message)
            if (
                len(signatures) != 1
                or hashlib.sha256(message).hexdigest() != ticket.message_sha256
                or raw[65:] != unsigned[65:]
                or hashlib.sha256(unsigned).hexdigest() != ticket.wire_sha256
                or not signatures[0].verify(key(ticket.sender), message)
            ):
                raise TransferError("Browser did not sign the exact authorized ticket transaction")
            return str(signatures[0])

    def recover_restart(self, journal) -> None:
        """Invalidate durable-looking runtime state once at server process startup."""
        self.lock(journal)


class AllowanceTradeJournal:
    """TradeJournal adapter that consumes allowance usage in its existing lock.

    The import and subclass are created dynamically to avoid changing the shared
    journal class during parallel integration.  Session 3 may replace this with
    an explicit reservation callback on ``TradeJournal.reserve_trade``.
    """

    @staticmethod
    def create(data_dir, manager, library, ident: str, token: str, ticket_id: str):
        from .manual_trades import TradeJournal

        class BoundJournal(TradeJournal):
            def check_agent_reservations(self, preview, state):
                super().check_agent_reservations(preview, state)
                if self.db.in_transaction:
                    manager.reserve_order(
                        library,
                        self,
                        ident,
                        token,
                        ticket_id,
                        preview,
                        base64.b64decode(preview["message"], validate=True),
                    )

        journal = BoundJournal(data_dir)
        install(journal)
        return journal


def submit_signed_ticket(
    library,
    rpc,
    data_dir,
    manager: RuntimeManager,
    *,
    allowance_id: str,
    runtime_token: str,
    ticket_id: str,
    signed_transaction: str,
) -> str:
    """Submit one ticket through normal validation with atomic allowance usage."""
    manager.validate_signed_ticket(
        allowance_id,
        runtime_token,
        ticket_id,
        signed_transaction,
    )
    ticket = manager._tickets.get(ticket_id)
    if ticket is None:
        raise TransferError("Signing ticket is no longer available")
    journal = AllowanceTradeJournal.create(
        data_dir,
        manager,
        library,
        allowance_id,
        runtime_token,
        ticket_id,
    )
    try:
        from .agent_execution import AgentTradeService

        service = AgentTradeService(library, rpc, journal)
        return service.submit_signed(ticket.order_id, ticket.sender, signed_transaction)
    finally:
        journal.close()
