"""Bounded in-memory authorization for the existing reviewed agent order path.

Only public scope and conservative usage are durable. Keys never enter the
session journal. This module does not discover candidates or run an auto trader.
"""

import base64
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import wallet_risk
from .strategy_support import reviewed_eligible
from .pump_route import MAX_ACCOUNT_FUNDING, ROUTES
from .pump_swap_route import ROUTES as SWAP_ROUTES
from .strategies import parse_package
from .transfers import TransferError, _uint


SCHEMA = """
CREATE TABLE IF NOT EXISTS signing_sessions (
    id TEXT PRIMARY KEY, genesis TEXT NOT NULL, sender TEXT NOT NULL,
    agent_id TEXT NOT NULL, scope TEXT NOT NULL, created_at REAL NOT NULL,
    expires_at REAL NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signing_session_orders (
    order_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, debit TEXT NOT NULL,
    authorized_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS session_order_usage ON signing_session_orders(session_id);
"""


def configuration(instance):
    return hashlib.sha256(json.dumps(instance["package"], sort_keys=True).encode()).hexdigest()


def rows(journal, sender, genesis):
    result = []
    for row in journal.db.execute(
        "SELECT * FROM signing_sessions WHERE sender=? AND genesis=? ORDER BY created_at DESC",
        (sender, genesis),
    ):
        usage = [
            int(r[0])
            for r in journal.db.execute(
                "SELECT debit FROM signing_session_orders WHERE session_id=?", (row["id"],)
            )
        ]
        result.append(
            dict(
                row,
                scope=json.loads(row["scope"]),
                used_lamports=sum(usage),
                used_orders=len(usage),
            )
        )
    return result


def revoke(journal, ident, sender, genesis):
    with journal.db:
        journal.db.execute("BEGIN IMMEDIATE")
        journal.db.execute(
            "UPDATE signing_sessions SET status='revoked' WHERE id=? AND sender=? AND genesis=?",
            (ident, sender, genesis),
        )


@dataclass
class Lease:
    wallet: object = field(repr=False)
    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)


class SessionManager:
    def __init__(self):
        self._leases = {}
        self._lock = threading.RLock()
        self._epoch = 0

    def available(self, ident):
        with self._lock:
            lease = self._leases.get(ident)
            if lease and time.monotonic() >= lease.deadline:
                lease.cancelled.set()
                lease.wallet = None
                self._leases.pop(ident, None)
                return False
            return bool(lease and not lease.cancelled.is_set() and lease.wallet is not None)

    def lock(self, ident=None):
        """Stop local authorization immediately, before any journal write can wait."""
        # Event signaling is independent of the journal lock held during signing.
        for key, lease in list(self._leases.items()):
            if ident is None or key == ident:
                lease.cancelled.set()
        with self._lock:
            self._epoch += 1
            for key in list(self._leases):
                if ident is None or key == ident:
                    self._leases.pop(key).wallet = None

    def authorize(
        self,
        service,
        wallet,
        agent_id,
        cash_hold,
        *,
        budget_lamports,
        duration_seconds,
        max_orders,
        slippage_bps,
        reviewed_limits,
        cancellation=None,
    ):
        with self._lock:
            epoch = self._epoch
        _uint(budget_lamports, "session spending cap")
        if not budget_lamports:
            raise TransferError("Set a positive session spending cap")
        if (
            type(duration_seconds) is not int
            or not 60 <= duration_seconds <= 3600
            or duration_seconds % 60
        ):
            raise TransferError("Session duration must be 1–60 whole minutes")
        if type(max_orders) is not int or not 1 <= max_orders <= 100:
            raise TransferError("Session order count must be 1–100")
        if type(slippage_bps) is not int or not 1 <= slippage_bps <= 500:
            raise TransferError("Session slippage must be 1–500 bps")
        instance = service.library.instance(agent_id)
        strategy = parse_package(instance["package"])
        if not reviewed_eligible(strategy):
            raise TransferError("Only the pinned core-frequency agent supports a signing session")
        genesis = service.rpc.genesis()
        journal = service.journal
        now, monotonic = time.time(), time.monotonic()
        ident = uuid.uuid4().hex
        with journal.db:
            journal.db.execute("BEGIN IMMEDIATE")
            limits = wallet_risk.policy(journal, genesis, wallet.address)
            if limits is None:
                raise TransferError("Configure wallet risk limits before authorizing a session")
            if limits.get("portfolio_drawdown_lamports") is None:
                raise TransferError("Configure portfolio drawdown before authorizing a session")
            if limits != reviewed_limits:
                raise TransferError("Wallet limits changed after session review; review again")
            if journal.pending(wallet.address, genesis):
                raise TransferError(
                    "Recover the unresolved wallet operation before authorizing a session"
                )
            hold = journal.db.execute(
                "SELECT * FROM agent_reservations WHERE id=?", (cash_hold,)
            ).fetchone()
            if hold is None or (
                hold["sender"],
                hold["genesis"],
                hold["agent_id"],
                hold["status"],
                hold["position_id"],
            ) != (wallet.address, genesis, agent_id, "held", None):
                raise TransferError(
                    "Select a dedicated cash allocation for this wallet, network and agent"
                )
            if budget_lamports > int(hold["lamports"]):
                raise TransferError(
                    "Session spending cap cannot exceed the selected cash allocation"
                )
            scope = dict(
                network=service.rpc.network,
                agent_id=agent_id,
                cash_hold=cash_hold,
                configuration=configuration(instance),
                budget_lamports=budget_lamports,
                max_orders=max_orders,
                slippage_bps=slippage_bps,
                limits=limits,
                routes=sorted((*ROUTES, *SWAP_ROUTES)),
                mode="reviewed-agent",
            )
            # Explicit replacement prevents overlapping authorizations for a wallet.
            journal.db.execute(
                "UPDATE signing_sessions SET status='replaced' WHERE sender=? AND genesis=? AND status='active'",
                (wallet.address, genesis),
            )
            journal.db.execute(
                "INSERT INTO signing_sessions VALUES (?,?,?,?,?,?,?,?)",
                (
                    ident,
                    genesis,
                    wallet.address,
                    agent_id,
                    json.dumps(scope, sort_keys=True),
                    now,
                    now + duration_seconds,
                    "active",
                ),
            )
        with self._lock:
            cancelled = epoch != self._epoch or (cancellation is not None and cancellation.is_set())
            if not cancelled:
                for lease in self._leases.values():
                    lease.cancelled.set()
                    lease.wallet = None
                self._leases.clear()
                self._leases[ident] = Lease(wallet, monotonic + duration_seconds)
        if cancelled:
            revoke(journal, ident, wallet.address, genesis)
            raise TransferError("Session authorization was cancelled")
        return ident

    def submit(self, service, ident, order_id):
        manager = self
        preview = service.journal.previews(order_id)

        class ScopedSigner:
            address = preview["sender"]

            def sign(self, message):
                return manager._sign(service, ident, preview, message)

        return service.submit(order_id, ScopedSigner())

    def _sign(self, service, ident, preview, message):
        journal = service.journal
        if not journal.db.in_transaction:
            raise TransferError("Session signing requires the atomic order-reservation transaction")
        with self._lock:
            if not self.available(ident):
                raise TransferError(
                    "Signing session is locked, expired or revoked; authorize a new session"
                )
            lease = self._leases[ident]
            row = journal.db.execute(
                "SELECT * FROM signing_sessions WHERE id=?", (ident,)
            ).fetchone()
            if (
                row is None
                or row["status"] != "active"
                or not row["created_at"] <= time.time() < row["expires_at"]
            ):
                self.lock(ident)
                raise TransferError("Signing session expired, was replaced or was revoked")
            scope = json.loads(row["scope"])
            context = preview.get("agent_order") or {}
            if (
                preview["sender"],
                preview["genesis"],
                preview["network"],
                context.get("agent_id"),
                context.get("cash_hold"),
            ) != (
                row["sender"],
                row["genesis"],
                scope["network"],
                row["agent_id"],
                scope["cash_hold"],
            ) or lease.wallet.address != row["sender"]:
                raise TransferError(
                    "Order is outside this signing session wallet, network, agent or allocation"
                )
            if configuration(service.library.instance(row["agent_id"])) != scope["configuration"]:
                raise TransferError("Agent configuration changed; authorize a new signing session")
            if (
                preview["route"] not in scope["routes"]
                or preview["slippage_bps"] > scope["slippage_bps"]
            ):
                raise TransferError("Order route or slippage exceeds the signing session scope")
            if (
                base64.b64decode(preview["message"]) != message
                or journal.previews(preview["id"]) != preview
            ):
                raise TransferError(
                    "Signing session refuses a message different from the reviewed order"
                )
            if wallet_risk.policy(journal, row["genesis"], row["sender"]) is None:
                raise TransferError("Wallet risk limits are required for session signing")
            # Existing checks enforce the current policy; these retain the original
            # session ceiling even if someone subsequently loosens wallet limits.
            wallet_risk.check_buy(journal, preview, preview["state"], limits=scope["limits"])
            from .agent_execution import check_binding

            check_binding(journal, preview)
            usage = [
                int(r[0])
                for r in journal.db.execute(
                    "SELECT debit FROM signing_session_orders WHERE session_id=?", (ident,)
                )
            ]
            debit = (
                preview["fee"]
                + MAX_ACCOUNT_FUNDING
                + (preview["amount"] if preview["side"] == "buy" else 0)
            )
            if len(usage) >= scope["max_orders"] or sum(usage) + debit > scope["budget_lamports"]:
                raise TransferError("Signing session spending cap or order count is exhausted")
            # This insert commits only with the signature/order reservation. Every
            # submission consumes its conservative cap permanently, even on failure.
            journal.db.execute(
                "INSERT INTO signing_session_orders VALUES (?,?,?,?)",
                (preview["id"], ident, str(debit), time.time()),
            )
            from .manual_trades import PREVIEW_TTL

            if (
                lease.cancelled.is_set()
                or time.monotonic() >= lease.deadline
                or time.time() >= row["expires_at"]
                or not 0 <= time.time() - preview["created_at"] <= PREVIEW_TTL
            ):
                raise TransferError(
                    "Signing session revoked, or session/quote expired before signing"
                )
            return lease.wallet.sign(message)
