"""Bounded wallet-approved or synthetic execution for an enrolled local wallet.

This module is the narrow signer injected into the background service.  It
reuses main-wallet allowance reviews, exact signing tickets, normal trade
validation, atomic allowance usage, broadcast and receipt recovery.  The only
private material it accepts is a transient vault password during ``start``.
"""

from __future__ import annotations

import json
from pathlib import Path
import secrets
import threading
import time
from typing import Callable

from .library import StrategyLibrary
from .main_wallet_autopilot import (
    RuntimeLease,
    RuntimeManager,
    _allowance_row,
    _policy_hash,
    _session_permissions,
    allowance_rows,
    authorize as authorize_allowance,
    install,
    policy_revision,
    review as review_allowance,
    submit_signed_ticket,
)
from .main_wallet_autotrader import prepare_next_ticket
from .manual_trades import ManualTradeService, TradeJournal
from . import session_allocations, strategy_risk, wallet_risk
from .service_acceptance import (
    ServiceAcceptanceCapability,
    acceptance_approval_request,
    acceptance_evidence as build_acceptance_evidence,
    verify_acceptance_approval,
    wallet_approval_available,
)
from .service_wallet_vault import SERVICE_CUSTODY, ServiceTicketSigner, ServiceWalletVault
from .transfers import GENESIS, TransferError, WithdrawalService, public_key


SERVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS service_acceptance_approvals (
    approval_id TEXT PRIMARY KEY, allowance_id TEXT NOT NULL UNIQUE,
    activated_at REAL NOT NULL, message_sha256 TEXT NOT NULL,
    payload TEXT NOT NULL, signature TEXT NOT NULL
);
"""


class ServiceRuntimeManager(RuntimeManager):
    """Exact-ticket runtime with a process lease instead of a tab heartbeat."""

    def __init__(
        self,
        *,
        allow_synthetic: bool = False,
        acceptance: ServiceAcceptanceCapability | None = None,
    ):
        if allow_synthetic and acceptance is not None:
            raise ValueError("Synthetic and acceptance service capabilities are mutually exclusive")
        if acceptance is not None and not isinstance(acceptance, ServiceAcceptanceCapability):
            raise TypeError("Service acceptance requires a verified capability")
        super().__init__(allow_synthetic=allow_synthetic)
        self._acceptance = acceptance
        self._acceptance_consumed = False

    def install_acceptance(self, capability: ServiceAcceptanceCapability) -> None:
        """Install verified authority in memory; no request boolean can enable it."""
        if not isinstance(capability, ServiceAcceptanceCapability):
            raise TypeError("Service acceptance requires a verified capability")
        with self._lock:
            if self._allow_synthetic:
                raise TransferError(
                    "Synthetic service runtime cannot be promoted to acceptance mode"
                )
            if self._leases:
                raise TransferError("Lock the service signer before changing acceptance authority")
            self._acceptance = capability
            self._acceptance_consumed = False

    def activation_capability(self) -> dict:
        if self._acceptance is not None:
            blockers = []
            if self._acceptance_consumed:
                blockers.append(
                    "Acceptance approval was already activated and cannot be replayed"
                )
            else:
                try:
                    self._acceptance.validate_current()
                except TransferError as exc:
                    blockers.append(str(exc))
            return {
                "available": not blockers,
                "mode": "acceptance",
                "blockers": blockers,
                "approval": self._acceptance.public(),
            }
        return {
            "available": self._allow_synthetic,
            "mode": "synthetic" if self._allow_synthetic else "blocked",
            "blockers": []
            if self._allow_synthetic
            else ["Approve the selected wallet's exact auto-trading limits before Start."]
            if wallet_approval_available()
            else [
                "Live main-wallet service signing has no funded lifecycle acceptance",
                "This build exposes only an injected synthetic service runtime",
            ],
        }

    def _validate_activation(
        self,
        journal,
        row,
        *,
        sender: str,
        genesis: str,
        vault_id: str,
        now: float,
    ) -> str:
        permissions = json.loads(row["granted_permissions"])
        scope = json.loads(row["scope"])
        if permissions.get("custody") != SERVICE_CUSTODY:
            raise TransferError("Browser-custody allowances require the browser runtime")
        if scope.get("custody_identity") != vault_id:
            raise TransferError("Allowance belongs to another service vault enrollment")
        if (row["sender"], row["genesis"], row["status"]) != (
            sender,
            genesis,
            "authorized",
        ):
            raise TransferError("Allowance is not authorized for this wallet and network")
        if now >= row["expires_at"]:
            raise TransferError("Main-wallet allowance expired")
        if self._acceptance is not None:
            if self._acceptance_consumed:
                raise TransferError("Service acceptance approval was already activated")
            self._acceptance.validate_allowance(row, vault_id=vault_id, now=now)
            if journal.pending(sender, genesis):
                raise TransferError(
                    "Reconcile the unresolved wallet signature before acceptance activation"
                )
            used = journal.db.execute(
                "SELECT 1 FROM service_acceptance_approvals WHERE approval_id=? OR allowance_id=?",
                (self._acceptance.approval_id, row["id"]),
            ).fetchone()
            if used is not None:
                raise TransferError("Service acceptance approval was already activated")
            return "acceptance"
        if self._allow_synthetic:
            return "synthetic"
        raise TransferError("Live main-wallet service signing remains blocked in this build")

    def validate_activation(
        self,
        journal,
        ident: str,
        *,
        sender: str,
        genesis: str,
        vault_id: str,
    ) -> str:
        """Fail before decrypting the service vault when capability is absent/stale."""
        install(journal)
        journal.db.executescript(SERVICE_SCHEMA)
        row = _allowance_row(journal, ident)
        return self._validate_activation(
            journal,
            row,
            sender=sender,
            genesis=genesis,
            vault_id=vault_id,
            now=time.time(),
        )

    def _lease(self, ident: str, token: str) -> RuntimeLease:
        lease = self._leases.get(ident)
        if lease is None or not secrets.compare_digest(
            lease.token_sha256, self._token_digest(token)
        ):
            raise TransferError("Service signing runtime is locked or the token is invalid")
        if (
            lease.cancelled.is_set()
            or time.time() >= lease.wall_deadline
            or time.monotonic() >= lease.monotonic_deadline
        ):
            lease.cancelled.set()
            self._leases.pop(ident, None)
            raise TransferError("Service signing runtime expired")
        return lease

    def available(self, ident: str) -> bool:
        with self._lock:
            lease = self._leases.get(ident)
            if lease is None:
                return False
            if (
                lease.cancelled.is_set()
                or time.time() >= lease.wall_deadline
                or time.monotonic() >= lease.monotonic_deadline
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
        vault_id: str,
    ) -> dict:
        install(journal)
        journal.db.executescript(SERVICE_SCHEMA)
        with self._lock:
            epoch = self._epoch
        token = secrets.token_urlsafe(32)
        now_wall, now_mono = time.time(), time.monotonic()
        with journal.db:
            journal.db.execute("BEGIN IMMEDIATE")
            row = _allowance_row(journal, ident)
            try:
                mode = self._validate_activation(
                    journal,
                    row,
                    sender=sender,
                    genesis=genesis,
                    vault_id=vault_id,
                    now=now_wall,
                )
            except TransferError:
                if now_wall >= row["expires_at"]:
                    journal.db.execute(
                        "UPDATE main_wallet_allowances SET status='expired',detail=? WHERE id=?",
                        ("Allowance expired before service signer unlock", ident),
                    )
                raise
            detail = (
                "Wallet-approved bounded auto trading active"
                if mode == "acceptance"
                else "Synthetic service signer active"
            )
            if mode == "acceptance":
                assert self._acceptance is not None
                record = self._acceptance.audit_record()
                journal.db.execute(
                    "INSERT INTO service_acceptance_approvals VALUES (?,?,?,?,?,?)",
                    (
                        self._acceptance.approval_id,
                        ident,
                        now_wall,
                        self._acceptance.message_sha256,
                        json.dumps(record["payload"], sort_keys=True, separators=(",", ":")),
                        record["signature"],
                    ),
                )
            journal.db.execute(
                "UPDATE main_wallet_allowances SET status='active',detail=? WHERE id=?",
                (detail, ident),
            )
            journal.db.execute(
                "INSERT INTO main_wallet_allowance_events"
                "(allowance_id,at,kind,detail,payload) VALUES (?,?,?,?,?)",
                (
                    ident,
                    now_wall,
                    "acceptance-activated" if mode == "acceptance" else "activated",
                    detail,
                    json.dumps(
                        self._acceptance.public() if self._acceptance is not None else {},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
        if mode == "acceptance":
            self._acceptance_consumed = True
        duration = row["expires_at"] - now_wall
        lease = RuntimeLease(
            token_sha256=self._token_digest(token),
            sender=sender,
            genesis=genesis,
            authorization_revision=row["authorization_revision"],
            wall_deadline=row["expires_at"],
            monotonic_deadline=now_mono + duration,
            heartbeat_deadline=float("inf"),
        )
        with self._lock:
            if epoch != self._epoch:
                lease.cancelled.set()
                cancelled = True
            else:
                cancelled = False
                old = self._leases.pop(ident, None)
                if old:
                    old.cancelled.set()
                self._leases[ident] = lease
        if cancelled:
            with journal.db:
                journal.db.execute(
                    "UPDATE main_wallet_allowances SET status='locked',detail=? WHERE id=?",
                    ("Service activation was cancelled before signer installation", ident),
                )
            raise TransferError("Service signing activation was cancelled")
        return {
            "allowance_id": ident,
            "runtime_token": token,
            "authorization_revision": row["authorization_revision"],
            "expires_at": row["expires_at"],
            "mode": mode,
            "custody": SERVICE_CUSTODY,
        }

    def heartbeat(self, ident: str, token: str) -> dict:
        del ident, token
        raise TransferError("Service signing does not use a browser heartbeat")

    def pause_entries(self, journal, ident: str, token: str, paused: bool) -> None:
        super().pause_entries(journal, ident, token, paused)
        with journal.db:
            journal.db.execute(
                "UPDATE main_wallet_allowances SET detail=? WHERE id=?",
                (
                    "Service entries paused; eligible exits remain available"
                    if paused
                    else (
                        "Bounded acceptance service signer active"
                        if self._acceptance is not None
                        else "Synthetic service signer active"
                    ),
                    ident,
                ),
            )

    def _validate_scope(self, library, journal, row, scope: dict, preview: dict) -> None:
        super()._validate_scope(library, journal, row, scope, preview)
        if self._acceptance is not None and preview.get("route") not in self._acceptance.routes:
            raise TransferError("Prepared order route is outside the acceptance approval")

    def lock(
        self,
        journal,
        ident: str | None = None,
        *,
        revoked: bool = False,
        expired: bool = False,
    ) -> None:
        # Signal first, before waiting for SQLite, so no ticket can race a stop.
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
        clause, values = ("id=?", (ident,)) if ident else ("1=1", ())
        status = "expired" if expired else "revoked" if revoked else "locked"
        detail = "Service allowance expired; signer removed" if expired else (
            "Service allowance revoked; signer removed"
            if revoked
            else "Service signer locked; fresh unlock and authorization required"
        )
        with journal.db:
            journal.db.execute(
                f"UPDATE main_wallet_allowances SET status=?,detail=? WHERE {clause} "
                "AND json_extract(granted_permissions,'$.custody')=? "
                "AND status IN ('authorized','active','entry-paused')",
                (status, detail, *values, SERVICE_CUSTODY),
            )

    def recover_restart(self, journal) -> None:
        """A new service process never reconstructs an unlocked signer."""
        self.lock(journal)


class ServiceSigningController:
    """Service-vault enrollment, allowance control and one-cycle supervisor."""

    def __init__(
        self,
        directory: Path,
        *,
        manager: ServiceRuntimeManager | None = None,
        rpc_factory: Callable[[str], object] | None = None,
        vault: ServiceWalletVault | None = None,
    ):
        self.directory = Path(directory)
        self.manager = manager or ServiceRuntimeManager()
        self.rpc_factory = rpc_factory
        self.vault = vault or ServiceWalletVault(self.directory)
        self._guard = threading.RLock()
        self._signer: ServiceTicketSigner | None = None
        self._runtime: dict[str, object] | None = None
        self._identity: dict[str, str] | None = None
        journal = self._journal()
        try:
            restart_rows = [
                dict(row)
                for row in journal.db.execute(
                    "SELECT id,session_id,sender,genesis FROM main_wallet_allowances "
                    "WHERE json_extract(granted_permissions,'$.custody')=? "
                    "AND status IN ('authorized','active','entry-paused')",
                    (SERVICE_CUSTODY,),
                )
            ]
            self.manager.recover_restart(journal)
        finally:
            journal.close()
        for row in restart_rows:
            self._cancel_reviews(
                {
                    "allowance_id": row["id"],
                    "session_id": row["session_id"],
                    "sender": row["sender"],
                    "genesis": row["genesis"],
                },
                sides=("buy", "sell"),
                reason="Service restart locked signing",
            )
            self._set_execution_modes(
                {
                    "allowance_id": row["id"],
                    "session_id": row["session_id"],
                },
                active=False,
                paused=True,
            )

    def _journal(self) -> TradeJournal:
        journal = TradeJournal(self.directory)
        install(journal)
        session_allocations.install(journal)
        journal.db.executescript(SERVICE_SCHEMA)
        return journal

    def _rpc(self, network: str):
        if self.rpc_factory is not None:
            return self.rpc_factory(network)
        from .web_trading import rpc_for

        return rpc_for(network, self.directory)

    def _validate_acceptance_context(self, journal, row) -> None:
        """Recheck every mutable rule binding before acceptance can unlock."""
        scope = json.loads(row["scope"])
        library = StrategyLibrary(self.directory)
        try:
            instance = library.instance(row["session_id"])
            requested, automation_scope = _session_permissions(library, row["session_id"])
        finally:
            library.close()
        if instance["strategy_digest"] != row["strategy_digest"]:
            raise TransferError("Exact strategy digest changed; create a new acceptance allowance")
        if (
            requested != json.loads(row["requested_permissions"])
            or automation_scope != scope.get("automation_scope")
        ):
            raise TransferError("Session execution permissions changed; review acceptance again")
        if policy_revision(
            journal,
            row["session_id"],
            row["genesis"],
            row["sender"],
        ) != row["policy_revision"]:
            raise TransferError(
                "Wallet or allocation policy revision changed; review acceptance again"
            )
        if _policy_hash(wallet_risk.policy(journal, row["genesis"], row["sender"])) != scope[
            "wallet_policy_hash"
        ]:
            raise TransferError("Shared wallet policy changed; review acceptance again")
        if _policy_hash(
            strategy_risk.policy(
                journal,
                row["session_id"],
                row["genesis"],
                row["sender"],
            )
        ) != scope["strategy_policy_hash"]:
            raise TransferError("Session policy changed; review acceptance again")
        cash = journal.db.execute(
            "SELECT agent_id,genesis,sender,lamports,status,position_id "
            "FROM agent_reservations WHERE id=?",
            (scope["cash_hold"],),
        ).fetchone()
        expected_cash_owner = session_allocations.cash_owner(
            journal, row["session_id"], row["genesis"], row["sender"],
        )
        if cash is None or (
            cash["agent_id"],
            cash["genesis"],
            cash["sender"],
            cash["status"],
            cash["position_id"],
            int(cash["lamports"]),
        ) != (
            expected_cash_owner,
            row["genesis"],
            row["sender"],
            "held",
            None,
            int(scope["cash_lamports"]),
        ):
            raise TransferError("Session cash allocation changed; review acceptance again")

    def _set_execution_modes(self, identity: dict[str, str], *, active: bool, paused: bool) -> None:
        journal = self._journal()
        try:
            row = _allowance_row(journal, identity["allowance_id"])
            permissions = json.loads(row["granted_permissions"])
            automatic_entries = permissions.get("automatic_entries")
            automatic_exits = permissions.get("automatic_protective_exits")
        finally:
            journal.close()
        library = StrategyLibrary(self.directory)
        try:
            if active:
                entries = "disabled" if paused else (
                    "automatic" if automatic_entries else "manual"
                )
                exits = "automatic" if automatic_exits else "manual"
            else:
                entries = "disabled" if automatic_entries else "manual"
                exits = "manual"
            library.set_execution_modes(identity["session_id"], entries=entries, exits=exits)
            if active:
                library.set_tracking(identity["session_id"], True)
        finally:
            library.close()

    def _cancel_reviews(
        self,
        identity: dict[str, str],
        *,
        sides: tuple[str, ...],
        reason: str,
    ) -> list[str]:
        """Cancel unsigned reviews while leaving submitted signatures recoverable."""
        selected = tuple(side for side in sides if side in {"buy", "sell"})
        if not selected:
            return []
        journal = self._journal()
        try:
            placeholders = ",".join("?" for _ in selected)
            with journal.db:
                journal.db.execute("BEGIN IMMEDIATE")
                rows = list(journal.db.execute(
                    "SELECT o.id FROM agent_orders o "
                    "JOIN trade_previews p ON p.id=o.id "
                    "WHERE o.agent_id=? AND o.status='review' "
                    f"AND o.side IN ({placeholders}) "
                    "AND json_extract(p.payload,'$.sender')=? "
                    "AND json_extract(p.payload,'$.genesis')=? "
                    "ORDER BY o.created_at,o.id",
                    (
                        identity["session_id"],
                        *selected,
                        identity["sender"],
                        identity["genesis"],
                    ),
                ))
                cancelled = [str(row["id"]) for row in rows]
                if cancelled:
                    journal.db.executemany(
                        "UPDATE agent_orders SET status='cancelled' "
                        "WHERE id=? AND status='review'",
                        ((ident,) for ident in cancelled),
                    )
                    detail = str(reason).strip()[:300] or "Service signing control changed"
                    journal.db.execute(
                        "INSERT INTO main_wallet_allowance_events"
                        "(allowance_id,at,kind,detail,payload) VALUES (?,?,?,?,?)",
                        (
                            identity["allowance_id"],
                            time.time(),
                            "unsigned-reviews-cancelled",
                            detail,
                            json.dumps(
                                {"order_ids": cancelled, "sides": list(selected)},
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                return cancelled
        finally:
            journal.close()

    @staticmethod
    def _network(network: object) -> tuple[str, str]:
        if not isinstance(network, str) or network not in GENESIS:
            raise TransferError("Choose mainnet-beta or devnet")
        return network, GENESIS[network]

    def enroll(
        self,
        envelope: object,
        unlock_secret: str,
        *,
        expected_address: str,
        expected_network: str,
    ) -> dict[str, object]:
        public_key(expected_address)
        self._network(expected_network)
        return self.vault.enroll(
            envelope,
            unlock_secret,
            expected_address=expected_address,
            expected_network=expected_network,
        )

    def review(
        self,
        library,
        *,
        sender: str,
        network: str,
        session_id: str,
        plan: object,
    ) -> dict:
        public_key(sender)
        _, genesis = self._network(network)
        metadata = self.vault.metadata(sender)
        if metadata["network"] != network:
            raise TransferError("Service wallet enrollment belongs to another network")
        journal = self._journal()
        try:
            return review_allowance(
                library,
                journal,
                session_id=session_id,
                sender=sender,
                network=network,
                genesis=genesis,
                wallet_kind="local",
                plan=plan,
                custody_identity=str(metadata["vault_id"]),
            )
        finally:
            journal.close()

    def authorize(
        self,
        library,
        *,
        sender: str,
        network: str,
        session_id: str,
        review_id: str,
        confirmation: str,
    ) -> dict:
        public_key(sender)
        _, genesis = self._network(network)
        expected = f"ALLOW SERVICE {session_id[:8].upper()}"
        if confirmation != expected:
            raise TransferError(f"Type {expected} to authorize service custody")
        journal = self._journal()
        try:
            row = journal.db.execute(
                "SELECT * FROM main_wallet_allowance_reviews WHERE id=?", (review_id,)
            ).fetchone()
            if row is None or (row["sender"], row["genesis"], row["session_id"]) != (
                sender,
                genesis,
                session_id,
            ):
                raise TransferError("Allowance review does not belong to this wallet and session")
            return authorize_allowance(library, journal, review_id)
        finally:
            journal.close()

    def acceptance_request(
        self,
        *,
        allowance_id: str,
        routes: object,
        duration_seconds: object,
    ) -> dict[str, object]:
        """Prepare an exact message; this does not grant signing authority."""
        journal = self._journal()
        try:
            row = _allowance_row(journal, allowance_id)
            if journal.pending(row["sender"], row["genesis"]):
                raise TransferError(
                    "Reconcile the unresolved wallet signature before acceptance review"
                )
            return acceptance_approval_request(row, routes, duration_seconds)
        finally:
            journal.close()

    def install_acceptance(self, payload: object, signature: object) -> dict[str, object]:
        """Verify the selected wallet's approval and install it only in this process."""
        capability = verify_acceptance_approval(payload, signature)
        with self._guard:
            if self._signer is not None:
                raise TransferError("Lock the service signer before changing acceptance authority")
            journal = self._journal()
            try:
                row = _allowance_row(journal, capability.allowance_id)
                metadata = self.vault.metadata(row["sender"])
                capability.validate_allowance(
                    row,
                    vault_id=str(metadata["vault_id"]),
                )
                if row["status"] != "authorized":
                    raise TransferError("Acceptance requires a fresh authorized allowance")
                if journal.pending(row["sender"], row["genesis"]):
                    raise TransferError(
                        "Reconcile the unresolved wallet signature before acceptance activation"
                    )
                if journal.db.execute(
                    "SELECT 1 FROM service_acceptance_approvals "
                    "WHERE approval_id=? OR allowance_id=?",
                    (capability.approval_id, row["id"]),
                ).fetchone() is not None:
                    raise TransferError("Service acceptance approval was already activated")
                self._validate_acceptance_context(journal, row)
                self.manager.install_acceptance(capability)
            finally:
                journal.close()
        return self.state()

    def acceptance_evidence(self, approval_id: str) -> dict[str, object]:
        """Return deterministic signed-approval, usage and receipt evidence."""
        journal = self._journal()
        try:
            return build_acceptance_evidence(journal, approval_id)
        finally:
            journal.close()

    def start(
        self,
        *,
        allowance_id: str,
        unlock_secret: str,
        confirmation: str,
    ) -> dict[str, object]:
        with self._guard:
            if self._signer is not None:
                raise TransferError("Lock the current service signer before starting another")
            journal = self._journal()
            try:
                row = _allowance_row(journal, allowance_id)
                scope = json.loads(row["scope"])
                expected = f"START {row['session_id'][:8].upper()}"
                if confirmation != expected:
                    raise TransferError(f"Type {expected} to unlock service signing")
                remaining = int(row["expires_at"] - time.time())
                if remaining < 1:
                    raise TransferError("Main-wallet allowance expired")
                metadata = self.vault.metadata(row["sender"])
                mode = self.manager.validate_activation(
                    journal,
                    allowance_id,
                    sender=row["sender"],
                    genesis=row["genesis"],
                    vault_id=str(metadata["vault_id"]),
                )
                if mode == "acceptance":
                    self._validate_acceptance_context(journal, row)
                signer = self.vault.unlock(
                    row["sender"],
                    scope["network"],
                    unlock_secret,
                    duration_seconds=min(3600, remaining),
                    allowance_id=allowance_id,
                    authorization_revision=row["authorization_revision"],
                )
                try:
                    runtime = self.manager.activate(
                        journal,
                        allowance_id,
                        sender=row["sender"],
                        genesis=row["genesis"],
                        vault_id=signer.vault_id,
                    )
                except Exception:
                    signer.lock()
                    raise
                self._signer = signer
                self._runtime = runtime
                self._identity = {
                    "allowance_id": allowance_id,
                    "sender": row["sender"],
                    "network": scope["network"],
                    "genesis": row["genesis"],
                    "session_id": row["session_id"],
                }
            finally:
                journal.close()
            try:
                self._set_execution_modes(self._identity, active=True, paused=False)
            except Exception:
                self._lock("Session execution-mode update failed")
                raise
        return self.state()

    def _active(self) -> tuple[ServiceTicketSigner, dict[str, object], dict[str, str]]:
        with self._guard:
            if self._signer is None or self._runtime is None or self._identity is None:
                raise TransferError("Service signer is locked; authorize and unlock it again")
            if not self._signer.active or not self.manager.available(
                self._identity["allowance_id"]
            ):
                self._lock("Service signer or allowance expired", expired=True)
                raise TransferError("Service signer or allowance expired")
            return self._signer, self._runtime, self._identity

    def state(self) -> dict[str, object]:
        with self._guard:
            active = (
                self._signer is not None
                and self._runtime is not None
                and self._identity is not None
                and self._signer.active
                and self.manager.available(self._identity["allowance_id"])
            )
            identity = dict(self._identity) if self._identity else None
            signer = self._signer.public() if self._signer else None
            stale = not active and (self._signer is not None or self._runtime is not None)
        if stale:
            self._lock("Service signer or allowance expired", expired=True)
        status = "active" if active else "none"
        entries_paused = True
        authorization_expires_at = None
        recovery_required = False
        runtime_mode = None
        if identity:
            journal = self._journal()
            try:
                row = journal.db.execute(
                    "SELECT status,entries_paused,expires_at FROM main_wallet_allowances "
                    "WHERE id=?",
                    (identity["allowance_id"],),
                ).fetchone()
                if row is not None:
                    status = {
                        "entry-paused": "paused",
                        "locked": "none",
                        "replaced": "none",
                    }.get(row["status"], row["status"])
                    entries_paused = bool(row["entries_paused"])
                    authorization_expires_at = row["expires_at"]
                recovery_required = journal.pending(identity["sender"], identity["genesis"])
            finally:
                journal.close()
            runtime_mode = self._runtime.get("mode") if self._runtime else None
        else:
            journal = self._journal()
            try:
                recovery_required = journal.db.execute(
                    "SELECT 1 FROM wallet_intents i JOIN main_wallet_allowances a "
                    "ON a.sender=i.sender AND a.genesis=i.genesis "
                    "WHERE json_extract(a.granted_permissions,'$.custody')=? LIMIT 1",
                    (SERVICE_CUSTODY,),
                ).fetchone() is not None
            finally:
                journal.close()
        capability = self.manager.activation_capability()
        return {
            "schema": "pumpwizard.service-trading-state/v1",
            "custody_mode": SERVICE_CUSTODY if identity else "not-enrolled",
            "signer_state": "unlocked" if active else "locked",
            "authorization_status": status,
            "entries_paused": entries_paused,
            "recovery_required": recovery_required,
            "production_activation": (
                "active" if active and runtime_mode == "acceptance"
                else "wallet-approved" if capability["mode"] == "acceptance" and capability["available"]
                else "wallet-approval-required" if capability["mode"] != "synthetic" and wallet_approval_available()
                else "blocked"
            ),
            "acceptance_activation": (
                "active"
                if active and runtime_mode == "acceptance"
                else "available"
                if capability["mode"] == "acceptance" and capability["available"]
                else "blocked"
            ),
            "activation_capability": capability,
            "blockers": capability["blockers"],
            "authorization_id": identity["allowance_id"] if identity else None,
            "authorization_expires_at": authorization_expires_at,
            "sender": identity["sender"] if identity else None,
            "network": identity["network"] if identity else None,
            "session_id": identity["session_id"] if identity else None,
            "signer_expires_at": signer["expires_at"] if signer else None,
        }

    def pause_entries(self, reason: str) -> dict[str, object]:
        _, runtime, identity = self._active()
        journal = self._journal()
        try:
            self.manager.pause_entries(
                journal,
                identity["allowance_id"],
                str(runtime["runtime_token"]),
                True,
            )
        finally:
            journal.close()
        self._cancel_reviews(
            identity,
            sides=("buy",),
            reason=reason or "Service entries paused",
        )
        self._set_execution_modes(identity, active=True, paused=True)
        return self.state()

    def resume_entries(self, authorization: object) -> dict[str, object]:
        _, runtime, identity = self._active()
        expected = {
            "allowance_id": identity["allowance_id"],
            "confirmation": f"RESUME {identity['session_id'][:8].upper()}",
        }
        if authorization != expected:
            raise TransferError(
                f"Type {expected['confirmation']} to resume entries for this allowance"
            )
        journal = self._journal()
        try:
            row = _allowance_row(journal, identity["allowance_id"])
            if not json.loads(row["granted_permissions"]).get("automatic_entries"):
                raise TransferError("This allowance is exit-only; automatic entries remain off")
            self.manager.pause_entries(
                journal,
                identity["allowance_id"],
                str(runtime["runtime_token"]),
                False,
            )
        finally:
            journal.close()
        self._set_execution_modes(identity, active=True, paused=False)
        return self.state()

    def _lock(
        self,
        reason: str,
        *,
        revoked: bool = False,
        expired: bool = False,
    ) -> None:
        with self._guard:
            if self._signer is not None:
                self._signer.lock()
            journal = self._journal()
            try:
                self.manager.lock(
                    journal,
                    self._identity["allowance_id"] if self._identity else None,
                    revoked=revoked,
                    expired=expired,
                )
            finally:
                journal.close()
            self._signer = None
            self._runtime = None
            identity = dict(self._identity) if self._identity else None
        if identity is not None:
            self._cancel_reviews(
                identity,
                sides=("buy", "sell"),
                reason=reason or "Service signer locked",
            )
            self._set_execution_modes(identity, active=False, paused=True)

    def lock(self, reason: str) -> dict[str, object]:
        self._lock(reason)
        return self.state()

    def revoke(self, reason: str) -> dict[str, object]:
        self._lock(reason, revoked=True)
        return self.state()

    def reconcile(self) -> dict[str, object]:
        # Reconciliation verifies public chain evidence for already recorded
        # signatures and never needs a private key or an active allowance.  In
        # particular, a restarted service must remain locked while it settles
        # an uncertain submission made by the previous process.
        journal = self._journal()
        try:
            rows = journal.db.execute(
                "SELECT DISTINCT a.sender,a.genesis,a.scope "
                "FROM main_wallet_allowances a "
                "JOIN wallet_intents i ON i.sender=a.sender AND i.genesis=a.genesis "
                "WHERE json_extract(a.granted_permissions,'$.custody')=?",
                (SERVICE_CUSTODY,),
            ).fetchall()
            reconciled = 0
            for row in rows:
                scope = json.loads(row["scope"])
                network = scope.get("network")
                if network not in GENESIS or GENESIS[network] != row["genesis"]:
                    raise TransferError("Service allowance has an invalid network identity")
                metadata = self.vault.metadata(row["sender"])
                if metadata["network"] != network:
                    raise TransferError("Service wallet enrollment belongs to another network")
                rpc = self._rpc(network)
                trades = ManualTradeService(rpc, journal).reconcile(row["sender"])
                withdrawals = WithdrawalService(rpc, journal).reconcile(row["sender"])
                reconciled += len(trades) + len(withdrawals)
            return {
                "status": "ready" if self.state()["signer_state"] == "unlocked" else "locked",
                "reconciled": reconciled,
            }
        finally:
            journal.close()

    def run_cycle(self) -> dict[str, object] | None:
        signer, runtime, identity = self._active()
        # Reconcile uncertain original signatures before preparing replacement work.
        self.reconcile()
        library = StrategyLibrary(self.directory)
        journal = self._journal()
        try:
            result = prepare_next_ticket(
                library,
                journal,
                self._rpc(identity["network"]),
                self.manager,
                identity["allowance_id"],
                str(runtime["runtime_token"]),
            )
            ticket = result["ticket"]
            if ticket is None:
                return result
            signed = signer.sign_ticket(ticket)
            signature = submit_signed_ticket(
                library,
                self._rpc(identity["network"]),
                self.directory,
                self.manager,
                allowance_id=identity["allowance_id"],
                runtime_token=str(runtime["runtime_token"]),
                ticket_id=ticket["id"],
                signed_transaction=signed,
            )
            self.manager.complete_ticket(identity["allowance_id"], ticket["id"])
            return {
                "ticket": None,
                "detail": (
                    "Submitted one exact reviewed acceptance transaction"
                    if runtime.get("mode") == "acceptance"
                    else "Submitted one exact reviewed synthetic transaction"
                ),
                "signature": signature,
                "order_id": ticket["order_id"],
            }
        finally:
            journal.close()
            library.close()

    def allowances(self, sender: str, network: str) -> list[dict]:
        public_key(sender)
        _, genesis = self._network(network)
        journal = self._journal()
        try:
            return [
                row
                for row in allowance_rows(journal, sender, genesis)
                if row["granted_permissions"].get("custody") == SERVICE_CUSTODY
            ]
        finally:
            journal.close()
