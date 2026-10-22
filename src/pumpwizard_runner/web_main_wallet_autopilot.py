"""Narrow browser adapter for the main-wallet Autopilot allowance module.

Shared route registration remains with the integration session.  These methods
take an already authenticated wallet identity and never accept transaction bytes
for ticket creation; only an existing reviewed order ID can become signable.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading

from .main_wallet_autopilot import (
    RuntimeManager,
    allowance_rows,
    authorize,
    install,
    review,
    submit_signed_ticket,
)
from .manual_trades import TradeJournal
from .transfers import GENESIS, TransferError, public_key


def _identity(sender: object, network: object) -> tuple[str, str, str]:
    if not isinstance(sender, str) or not isinstance(network, str):
        raise TransferError("Choose an authenticated wallet and Solana network")
    public_key(sender)
    if network not in GENESIS:
        raise TransferError("Choose mainnet-beta or devnet")
    return sender, network, GENESIS[network]


class MainWalletAutopilotController:
    def __init__(self, directory: Path, *, manager=None, rpc_factory=None):
        self.directory = Path(directory)
        self.manager = manager or RuntimeManager()
        self.rpc_factory = rpc_factory
        self._startup_lock = threading.Lock()
        self._startup_done = False
        self._schedule_lock = threading.Lock()
        self._recover_startup_state()

    @staticmethod
    def _inactive_entry_mode(row) -> str:
        return "disabled" if json.loads(row["granted_permissions"]).get("automatic_entries") else "manual"

    @classmethod
    def _disable_sessions(cls, library, rows) -> None:
        for row in rows:
            library.set_execution_modes(
                row["session_id"], entries=cls._inactive_entry_mode(row), exits="manual",
            )

    def _recover_startup_state(self) -> None:
        journal = TradeJournal(self.directory)
        install(journal)
        try:
            rows = [dict(row) for row in journal.db.execute(
                "SELECT * FROM main_wallet_allowances "
                "WHERE status IN ('authorized','active','entry-paused')"
            )]
            self.manager.recover_restart(journal)
            self._startup_done = True
        finally:
            journal.close()
        if rows:
            from .library import StrategyLibrary

            library = StrategyLibrary(self.directory)
            try:
                self._disable_sessions(library, rows)
            finally:
                library.close()

    def _journal(self) -> TradeJournal:
        journal = TradeJournal(self.directory)
        install(journal)
        with self._startup_lock:
            if not self._startup_done:
                self.manager.recover_restart(journal)
                self._startup_done = True
        return journal

    def _rpc(self, network: str):
        if self.rpc_factory is not None:
            return self.rpc_factory(network)
        from .web_trading import rpc_for

        return rpc_for(network, self.directory)

    @staticmethod
    def _owned(journal, allowance_id: str, sender: str, genesis: str, session_id=None):
        row = journal.db.execute(
            "SELECT * FROM main_wallet_allowances WHERE id=?",
            (allowance_id,),
        ).fetchone()
        if row is None or (row["sender"], row["genesis"]) != (sender, genesis):
            raise TransferError("Allowance does not belong to this wallet and network")
        if session_id is not None and row["session_id"] != session_id:
            raise TransferError("Allowance does not belong to this session")
        return row

    def status(self, sender: object, network: object, session_id: str | None = None) -> dict:
        sender, _, genesis = _identity(sender, network)
        journal = self._journal()
        try:
            rows = allowance_rows(journal, sender, genesis)
            if session_id is not None:
                rows = [row for row in rows if row["session_id"] == session_id]
            return {
                "allowances": rows,
                "activation": self.manager.activation_capability(),
                "runtime": {
                    row["id"]: {
                        "available": self.manager.available(row["id"]),
                        "signing": "browser-memory-only",
                    }
                    for row in rows
                },
            }
        finally:
            journal.close()

    def review(
        self,
        library,
        *,
        sender: object,
        network: object,
        session_id: str,
        wallet_kind: str,
        plan: object,
    ) -> dict:
        sender, network, genesis = _identity(sender, network)
        journal = self._journal()
        try:
            return review(
                library,
                journal,
                session_id=session_id,
                sender=sender,
                network=network,
                genesis=genesis,
                wallet_kind=wallet_kind,
                plan=plan,
            )
        finally:
            journal.close()

    def authorize(
        self,
        library,
        *,
        sender: object,
        network: object,
        session_id: str,
        review_id: str,
        confirmation: str,
    ) -> dict:
        sender, _, genesis = _identity(sender, network)
        expected = f"ALLOW {session_id[:8].upper()}"
        if confirmation != expected:
            raise TransferError(f"Type {expected} to authorize this inactive allowance")
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
            return authorize(library, journal, review_id)
        finally:
            journal.close()

    def activate(
        self,
        library,
        *,
        sender: object,
        network: object,
        allowance_id: str,
        wallet_kind: str,
    ) -> dict:
        sender, _, genesis = _identity(sender, network)
        journal = self._journal()
        try:
            row = self._owned(journal, allowance_id, sender, genesis)
            runtime = self.manager.activate(
                journal,
                allowance_id,
                sender=sender,
                genesis=genesis,
                wallet_kind=wallet_kind,
            )
            try:
                permissions = json.loads(row["granted_permissions"])
                library.set_execution_modes(
                    row["session_id"],
                    entries="automatic" if permissions.get("automatic_entries") else "manual",
                    exits="automatic" if json.loads(row["granted_permissions"]).get("automatic_protective_exits") else "manual",
                )
                library.set_tracking(row["session_id"], True)
            except Exception:
                self.manager.lock(journal, allowance_id)
                raise
            return runtime
        finally:
            journal.close()

    def heartbeat(
        self,
        *,
        sender: object,
        network: object,
        allowance_id: str,
        runtime_token: str,
    ) -> dict:
        sender, _, genesis = _identity(sender, network)
        journal = self._journal()
        try:
            self._owned(journal, allowance_id, sender, genesis)
            return self.manager.heartbeat(allowance_id, runtime_token)
        finally:
            journal.close()

    def pause_entries(
        self,
        library,
        *,
        sender: object,
        network: object,
        allowance_id: str,
        runtime_token: str,
        paused: bool,
    ) -> None:
        if type(paused) is not bool:
            raise TransferError("Entry pause must be true or false")
        sender, _, genesis = _identity(sender, network)
        journal = self._journal()
        try:
            row = self._owned(journal, allowance_id, sender, genesis)
            if not json.loads(row["granted_permissions"]).get("automatic_entries"):
                raise TransferError("This allowance is exit-only; automatic entries remain off")
            self.manager.pause_entries(journal, allowance_id, runtime_token, paused)
            library.set_execution_modes(
                row["session_id"], entries="disabled" if paused else "automatic",
                exits="automatic" if json.loads(row["granted_permissions"]).get("automatic_protective_exits") else "manual",
            )
        finally:
            journal.close()

    def revoke(
        self,
        library,
        *,
        sender: object,
        network: object,
        allowance_id: str,
    ) -> None:
        sender, _, genesis = _identity(sender, network)
        journal = self._journal()
        try:
            row = self._owned(journal, allowance_id, sender, genesis)
            self.manager.lock(journal, allowance_id, revoked=True)
            library.set_execution_modes(
                row["session_id"], entries=self._inactive_entry_mode(row), exits="manual",
            )
        finally:
            journal.close()

    def lock(
        self,
        library=None,
        *,
        sender: object,
        network: object,
        allowance_id: str,
    ) -> None:
        sender, _, genesis = _identity(sender, network)
        journal = self._journal()
        try:
            row = self._owned(journal, allowance_id, sender, genesis)
            self.manager.lock(journal, allowance_id)
            if library is not None:
                library.set_execution_modes(
                    row["session_id"], entries=self._inactive_entry_mode(row), exits="manual",
                )
        finally:
            journal.close()

    def ticket(
        self,
        library,
        *,
        sender: object,
        network: object,
        allowance_id: str,
        runtime_token: str,
        order_id: str,
    ) -> dict:
        sender, _, genesis = _identity(sender, network)
        journal = self._journal()
        try:
            self._owned(journal, allowance_id, sender, genesis)
            preview = journal.previews(order_id)
            return self.manager.issue_ticket(
                library,
                journal,
                allowance_id,
                runtime_token,
                preview,
            )
        finally:
            journal.close()

    def next_ticket(
        self,
        library,
        *,
        sender: object,
        network: object,
        allowance_id: str,
        runtime_token: str,
    ) -> dict:
        sender, network, genesis = _identity(sender, network)
        journal = self._journal()
        try:
            self._owned(journal, allowance_id, sender, genesis)
            from .main_wallet_autotrader import prepare_next_ticket

            with self._schedule_lock:
                return prepare_next_ticket(
                    library,
                    journal,
                    self._rpc(network),
                    self.manager,
                    allowance_id,
                    runtime_token,
                )
        finally:
            journal.close()

    def submit_signed(
        self,
        library,
        *,
        sender: object,
        network: object,
        allowance_id: str,
        runtime_token: str,
        ticket_id: str,
        signed_transaction: str,
    ) -> str:
        sender, network, genesis = _identity(sender, network)
        journal = self._journal()
        try:
            self._owned(journal, allowance_id, sender, genesis)
        finally:
            journal.close()
        signature = submit_signed_ticket(
            library,
            self._rpc(network),
            self.directory,
            self.manager,
            allowance_id=allowance_id,
            runtime_token=runtime_token,
            ticket_id=ticket_id,
            signed_transaction=signed_transaction,
        )
        self.manager.complete_ticket(allowance_id, ticket_id)
        return signature

    def shutdown(self) -> None:
        journal = self._journal()
        try:
            rows = [dict(row) for row in journal.db.execute(
                "SELECT * FROM main_wallet_allowances "
                "WHERE status IN ('authorized','active','entry-paused')"
            )]
            self.manager.lock(journal)
        finally:
            journal.close()
        if rows:
            from .library import StrategyLibrary

            library = StrategyLibrary(self.directory)
            try:
                self._disable_sessions(library, rows)
            finally:
                library.close()
