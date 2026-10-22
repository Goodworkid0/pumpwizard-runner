"""Browser-wallet bridge for the existing reviewed manual trade service."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from .manual_trades import PREVIEW_TTL, ManualTradeService, TradeJournal, TradeRpc
from .pump_route import MAX_ACCOUNT_FUNDING, MAX_FEE, key, unsigned_transaction
from .transfers import GENESIS, TransferError, _uint, public_key
from solders.signature import Signature


NETWORKS = frozenset(("mainnet-beta", "devnet"))
CHALLENGE_SECONDS = 300
AUTHORIZATION_SECONDS = 1800


class WalletAuthorizer:
    """Single-use wallet challenges and process-local browser authorization."""

    def __init__(self):
        self._challenges: dict[str, dict] = {}
        self._authorized: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def challenge(self, sender: object, network: object, origin: str) -> dict:
        sender, network = _identity(sender, network)
        parsed = urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise TransferError("Wallet sign-in requires the authenticated loopback origin")
        now = time.time()
        issued = datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")
        expires = datetime.fromtimestamp(now + CHALLENGE_SECONDS, timezone.utc).isoformat().replace("+00:00", "Z")
        nonce = secrets.token_hex(16)
        ident = secrets.token_hex(16)
        chain = "solana:devnet" if network == "devnet" else "solana:mainnet"
        message = (
            f"{parsed.netloc} wants you to sign in with your Solana account:\n"
            f"{sender}\n\n"
            "Prove wallet ownership for reviewed local PumpWizard trades. "
            "This message creates no transaction and costs no SOL.\n\n"
            f"URI: {origin}\n"
            "Version: 1\n"
            f"Chain ID: {chain}\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {issued}\n"
            f"Expiration Time: {expires}"
        ).encode()
        with self._lock:
            self._prune(now)
            self._challenges[ident] = {
                "sender": sender, "network": network, "origin": origin,
                "message": message, "expires_at": now + CHALLENGE_SECONDS,
            }
        return {
            "challenge_id": ident,
            "message": base64.b64encode(message).decode(),
            "expires_at": now + CHALLENGE_SECONDS,
        }

    def verify(self, body: object, origin: str) -> dict:
        fields = {"sender", "network", "challenge_id", "signature"}
        if not isinstance(body, dict) or set(body) != fields or not all(
            isinstance(body[name], str) for name in fields
        ):
            raise TransferError("Wallet sign-in requires sender, network, challenge and signature")
        sender, network = _identity(body["sender"], body["network"])
        now = time.time()
        with self._lock:
            self._prune(now)
            challenge = self._challenges.pop(body["challenge_id"], None)
        if (
            challenge is None
            or challenge["sender"] != sender
            or challenge["network"] != network
            or challenge["origin"] != origin
            or now >= challenge["expires_at"]
        ):
            raise TransferError("Wallet sign-in challenge expired or does not match this connection")
        try:
            raw = base64.b64decode(body["signature"], validate=True)
            signature = Signature.from_bytes(raw)
        except Exception:
            raise TransferError("Wallet returned an invalid sign-in signature") from None
        if not signature.verify(key(sender), challenge["message"]):
            raise TransferError("Wallet sign-in signature does not match the connected address")
        authorized_until = now + AUTHORIZATION_SECONDS
        with self._lock:
            self._authorized[(sender, network)] = authorized_until
        return {"sender": sender, "network": network, "authorized_until": authorized_until}

    def require(self, sender: object, network: object) -> tuple[str, str]:
        sender, network = _identity(sender, network)
        now = time.time()
        with self._lock:
            self._prune(now)
            deadline = self._authorized.get((sender, network), 0)
        if now >= deadline:
            raise TransferError("Wallet sign-in expired; reconnect and sign in again")
        return sender, network

    def revoke(self, sender: object, network: object) -> None:
        sender, network = _identity(sender, network)
        with self._lock:
            self._authorized.pop((sender, network), None)

    def _prune(self, now: float) -> None:
        self._challenges = {
            ident: value for ident, value in self._challenges.items()
            if now < value["expires_at"]
        }
        self._authorized = {
            identity: deadline for identity, deadline in self._authorized.items()
            if now < deadline
        }


def rpc_for(network: str, directory: Path | None = None) -> TradeRpc:
    if network not in NETWORKS:
        raise TransferError("Choose mainnet-beta or devnet")
    from .rpc_settings import endpoint
    return TradeRpc(network, endpoint(network, directory)[0])


def _identity(sender: object, network: object) -> tuple[str, str]:
    if not isinstance(sender, str) or not isinstance(network, str):
        raise TransferError("Choose a connected wallet and Solana network")
    public_key(sender)
    if network not in NETWORKS:
        raise TransferError("Choose mainnet-beta or devnet")
    return sender, network


def _preview_view(preview: dict) -> dict:
    result = {
        key: preview.get(key)
        for key in (
            "id", "network", "genesis", "sender", "mint", "side", "amount", "decimals",
            "slippage_bps", "position_id", "route", "created_at", "last_valid_height", "fee",
            "minimum_output", "expected_output", "expected_wallet_delta", "max_wallet_debit",
        )
    } | {
        "expires_at": preview["created_at"] + PREVIEW_TTL,
        "reward_mode": preview["state"]["reward_mode"],
        # Both supported builders explicitly encode a zero CU price before review.
        "priority_fee": "0",
        "unsigned_transaction": base64.b64encode(
            unsigned_transaction(base64.b64decode(preview["message"]))
        ).decode(),
    }
    for name in ("amount", "fee", "minimum_output", "expected_output",
                 "expected_wallet_delta", "max_wallet_debit"):
        result[name] = str(result[name])
    return result


def state(directory: Path, sender: str, network: str) -> dict:
    sender, network = _identity(sender, network)
    journal = TradeJournal(Path(directory))
    try:
        genesis = GENESIS[network]
        positions = journal.positions(sender, genesis)
        rows = [row for row in journal.trades(sender) if row["genesis"] == genesis]
        entries = {row["id"]: row for row in rows}
        tables = {row[0] for row in journal.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        owners = {}
        if "agent_positions" in tables:
            owners = {row["position_id"]: dict(row) for row in journal.db.execute("SELECT * FROM agent_positions")}
        for position in positions:
            entry = entries.get(position["id"])
            evidenced = bool(entry and entry["status"] == "finalized" and entry["receipt"])
            position["decimals"] = entry["payload"].get("decimals") if evidenced else None
            receipts = [row for row in rows if row["receipt"] and (
                row["id"] == position["id"] or (
                    row["payload"].get("side") == "sell" and row["payload"].get("position_id") == position["id"]
                )
            )]
            failed_fees = sum(row["receipt"]["fee_lamports"] for row in receipts if row["status"] == "failed")
            position["transaction_fees"] = str(sum(row["receipt"]["fee_lamports"] for row in receipts)) if evidenced else None
            position["realized_pnl"] = str(int(position["realized_proceeds"]) - int(position["realized_cost"]) - failed_fees) if evidenced else None
            owner = owners.get(position["id"])
            position["agent_id"] = owner["agent_id"] if owner else None
            hold = journal.db.execute('SELECT status FROM agent_reservations WHERE id=?', (owner['token_hold'],)).fetchone() if owner else None
            position['managed'] = bool(hold and hold[0] == 'held')
            position["entry_fx"] = owner["entry_fx"] if owner else None
            position["sold_stages"] = json.loads(owner["sold_stages"]) if owner else []
            position["peak_price_usd"] = owner["peak_price_usd"] if owner else None
            position["last_usable_at"] = owner["last_usable_at"] if owner else None
        orders = []
        for row in rows:
            payload = row["payload"]
            if row["genesis"] != genesis:
                continue
            orders.append({
                "id": row["id"],
                "signature": row["signature"],
                "status": row["status"],
                "detail": row["detail"],
                "created_at": row["created_at"],
                "network": payload.get("network"),
                "side": payload.get("side"),
                "mint": payload.get("mint"),
                "amount": str(payload["amount"]),
                "decimals": payload.get("decimals"),
                "position_id": payload.get("position_id"),
                "route": payload.get("route"),
                "agent_id": (payload.get('agent_order') or {}).get('agent_id'),
                "receipt": None if row["receipt"] is None else {
                    **{name: row["receipt"][name] for name in ("slot", "block_time", "cost_basis")},
                    **{name: str(row["receipt"][name]) for name in ("fee_lamports", "wallet_delta", "token_delta")},
                },
            })
        withdrawals = [row for row in journal.rows(sender) if row['genesis'] == genesis]
        return {"sender": sender, "network": network, "positions": positions, "orders": orders, 'withdrawals': withdrawals,
                "pending": journal.pending(sender, genesis)}
    finally:
        journal.close()


def inspect_wallet(body: object, directory: Path | None = None) -> dict:
    if not isinstance(body, dict) or set(body) != {"sender", "network"}:
        raise TransferError("Wallet inspection requires sender and network")
    sender, network = _identity(body["sender"], body["network"])
    rpc = rpc_for(network, directory)
    genesis = rpc.genesis()
    if genesis != GENESIS[network]:
        raise TransferError("RPC network does not match the connected wallet")
    balance = _uint(
        rpc.call("getBalance", [sender, {"commitment": "confirmed"}])["value"],
        "wallet balance",
    )
    result = {"sender": sender, "network": network, "genesis": genesis, "balance": str(balance)}
    if directory is not None:
        from .reservations import rows
        from .wallet_risk import status

        journal = TradeJournal(directory)
        try:
            reserved = sum(int(row["lamports"]) for row in rows(journal, sender)
                           if row["genesis"] == genesis and row["status"] == "held")
            risk = status(journal, genesis, sender)
            limits = risk["policy"]
            retained = max(reserved, limits["reserve_lamports"] if limits else 0)
            overhead = MAX_ACCOUNT_FUNDING + MAX_FEE
            capacity = min(1_000_000_000, max(0, balance - retained - overhead))
            if limits:
                capacity = min(capacity, max(0, limits["max_buy_lamports"] - overhead),
                               max(0, limits["max_exposure_lamports"] - risk["metrics"]["open_cost"]
                                   - risk["metrics"]["committed_cost"] - overhead))
            pending = journal.pending(sender, genesis)
            blocks = list(risk["entry_blocks"])
            if pending:
                blocks.append("An original wallet signature is unresolved")
            result.update(reserved_lamports=str(reserved), unallocated_lamports=str(max(0, balance-reserved)),
                          buy_capacity_lamports=str(0 if blocks else capacity),
                          buy_buffer_lamports=str(overhead), entry_blocks=blocks,
                          wallet_risk=risk, inspected_at=time.time())
        finally:
            journal.close()
    return result


def preview(directory: Path, body: object) -> dict:
    fields = {"sender", "network", "mint", "side", "amount", "slippage_bps", "position_id"}
    if not isinstance(body, dict) or set(body) != fields:
        raise TransferError("Trade review requires wallet, network, mint, side, amount and slippage")
    sender, network = _identity(body["sender"], body["network"])
    if not all(isinstance(body[name], str) for name in ("mint", "side", "amount")):
        raise TransferError("Trade mint, side and amount must be text")
    if type(body["slippage_bps"]) is not int:
        raise TransferError("Slippage must be an integer number of basis points")
    if body["position_id"] is not None and not isinstance(body["position_id"], str):
        raise TransferError("Sell position must be a local finalized position ID")
    if body["side"] == "buy" and body["position_id"] is not None:
        raise TransferError("A buy cannot reference an existing sell position")
    journal = TradeJournal(Path(directory))
    try:
        reviewed = ManualTradeService(rpc_for(network, directory), journal).preview(
            sender,
            body["mint"].strip(),
            body["side"],
            body["amount"].strip(),
            body["slippage_bps"],
            body["position_id"],
        )
        return {"preview": _preview_view(reviewed)}
    finally:
        journal.close()


def submit(directory: Path, body: object) -> dict:
    fields = {"sender", "network", "preview_id", "signed_transaction"}
    if not isinstance(body, dict) or set(body) != fields or not all(
        isinstance(body[name], str) for name in fields
    ):
        raise TransferError("Trade submission requires wallet, network, preview and signed transaction")
    sender, network = _identity(body["sender"], body["network"])
    journal = TradeJournal(Path(directory))
    try:
        signature = ManualTradeService(rpc_for(network, directory), journal).submit_signed(
            body["preview_id"], sender, body["signed_transaction"]
        )
        row = next(row for row in journal.trades(sender) if row["signature"] == signature)
        return {"signature": signature, "state": row["status"], "detail": row["detail"]}
    finally:
        journal.close()


def reconcile(directory: Path, body: object) -> dict:
    if not isinstance(body, dict) or set(body) != {"sender", "network"}:
        raise TransferError("Receipt refresh requires sender and network")
    sender, network = _identity(body["sender"], body["network"])
    journal = TradeJournal(Path(directory))
    try:
        ManualTradeService(rpc_for(network, directory), journal).reconcile(sender)
        from .transfers import WithdrawalService
        WithdrawalService(rpc_for(network, directory), journal).reconcile(sender)
    finally:
        journal.close()
    return state(directory, sender, network)
