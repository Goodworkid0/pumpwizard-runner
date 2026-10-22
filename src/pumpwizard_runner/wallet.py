"""Read-only local wallet reconciliation for positions already in the library.

No private key, seed phrase, signing method, or transaction payload is accepted
here. The user provides a public address and a selected RPC URL; the resulting
balance snapshot is retained locally beside the position ledger.
"""
from __future__ import annotations

import math
import time
from typing import Any, Protocol

from .market import MINT
from .ownership import JsonRpcClient


TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}
LAMPORTS_PER_SOL = 1_000_000_000
MAX_TRACKED_MINTS = 100


class WalletDataError(RuntimeError):
    """The selected read-only RPC could not provide a complete account snapshot."""


class WalletSource(Protocol):
    def snapshot(self, address: str, mints: set[str], now: float) -> dict[str, object]: ...


class SolanaWalletSource:
    """Collect SOL and explicit local-position token balances from an HTTPS RPC."""

    def __init__(self, rpc_url: str):
        self.rpc = JsonRpcClient(rpc_url)

    def snapshot(self, address: str, mints: set[str], now: float | None = None) -> dict[str, object]:
        checked_at = time.time() if now is None else now
        if not isinstance(address, str) or not MINT.fullmatch(address):
            raise ValueError("Wallet address must be a Solana public key")
        if not isinstance(checked_at, (int, float)) or isinstance(checked_at, bool) or not math.isfinite(checked_at):
            raise ValueError("Wallet check time must be finite")
        if any(not isinstance(mint, str) or not MINT.fullmatch(mint) for mint in mints):
            raise ValueError("Tracked position mints must be Solana public keys")
        selected = sorted(mints)
        if len(selected) > MAX_TRACKED_MINTS:
            raise ValueError(f"Wallet check supports at most {MAX_TRACKED_MINTS} tracked mints")
        try:
            balance_response = self.rpc.call("getBalance", [address, {"commitment": "confirmed"}])
            slot, lamports = _balance_response(balance_response)
            balances = {mint: self._token_balance(address, mint, slot) for mint in selected}
        except WalletDataError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise WalletDataError("Wallet balance response was incomplete or could not be decoded") from exc
        return {
            "address": address, "checked_at": float(checked_at), "slot": slot,
            "sol_lamports": str(lamports), "sol": lamports / LAMPORTS_PER_SOL,
            "token_balances": balances, "tracked_mints": selected,
            "source": "solana_json_rpc", "coverage": (
                "Confirmed SOL balance and current parsed token-account balances for open local-position mints; "
                "this is read-only reconciliation, not wallet control or transaction confirmation."
            ),
        }

    def _token_balance(self, address: str, mint: str, min_slot: int) -> dict[str, object]:
        response = self.rpc.call("getTokenAccountsByOwner", [address, {"mint": mint}, {
            "encoding": "jsonParsed", "commitment": "confirmed", "minContextSlot": min_slot,
        }])
        if not isinstance(response, dict) or not isinstance(response.get("context"), dict):
            raise WalletDataError("Token-account response is incomplete")
        context_slot = response["context"].get("slot")
        accounts = response.get("value")
        if not isinstance(context_slot, int) or context_slot < min_slot or not isinstance(accounts, list):
            raise WalletDataError("Token-account response did not meet the requested ledger context")
        amount = 0
        decimals: int | None = None
        seen: set[str] = set()
        for record in accounts:
            pubkey, raw_amount, account_decimals = _token_account(record, address, mint)
            if pubkey in seen:
                raise WalletDataError("Token-account response contains a duplicate account")
            seen.add(pubkey)
            if decimals is not None and decimals != account_decimals:
                raise WalletDataError("Token-account response has inconsistent decimals")
            decimals = account_decimals
            amount += raw_amount
        return {
            "amount": str(amount), "decimals": decimals if decimals is not None else 0,
            "accounts": len(accounts), "slot": context_slot,
        }


def _balance_response(response: Any) -> tuple[int, int]:
    if not isinstance(response, dict) or not isinstance(response.get("context"), dict):
        raise WalletDataError("SOL balance response is incomplete")
    slot = response["context"].get("slot")
    lamports = response.get("value")
    if (not isinstance(slot, int) or slot < 0 or isinstance(lamports, bool) or
            not isinstance(lamports, int) or lamports < 0):
        raise WalletDataError("SOL balance response is invalid")
    return slot, lamports


def _token_account(record: Any, address: str, mint: str) -> tuple[str, int, int]:
    if not isinstance(record, dict) or not isinstance(record.get("pubkey"), str):
        raise WalletDataError("Token-account response contains an unsupported account")
    account = record.get("account")
    if not isinstance(account, dict) or account.get("owner") not in TOKEN_PROGRAMS:
        raise WalletDataError("Token-account response contains an unsupported account")
    data = account.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = parsed.get("info") if isinstance(parsed, dict) else None
    if (not isinstance(info, dict) or parsed.get("type") != "account" or info.get("owner") != address or
            info.get("mint") != mint or info.get("state") not in ("initialized", "frozen")):
        raise WalletDataError("Token-account response does not match the requested wallet and mint")
    token_amount = info.get("tokenAmount")
    raw_amount = token_amount.get("amount") if isinstance(token_amount, dict) else None
    decimals = token_amount.get("decimals") if isinstance(token_amount, dict) else None
    if (not isinstance(raw_amount, str) or not raw_amount.isdecimal() or not isinstance(decimals, int) or
            not 0 <= decimals <= 18):
        raise WalletDataError("Token-account balance is invalid")
    return record["pubkey"], int(raw_amount), decimals
