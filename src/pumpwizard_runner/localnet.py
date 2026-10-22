"""Strictly local Solana signer verification using one System Program transfer.

This is not a trading route. It proves that the packaged app can load a local
test keypair, build a deterministic legacy transaction, sign it, submit it to
the user's local validator, and obtain a confirmation. The RPC endpoint is
locked to loopback HTTP on port 8899, so this module cannot target devnet or
mainnet.
"""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path
import stat
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .market import MINT


SYSTEM_PROGRAM = "11111111111111111111111111111111"
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_LOCAL_HOSTS = {"127.0.0.1", "::1"}
_ALLOWED_METHODS = {"getBalance", "getLatestBlockhash", "getSignatureStatuses", "sendTransaction"}


class LocalnetError(RuntimeError):
    """A local-validator signing check could not be completed."""


class LocalnetRpc:
    """Bounded JSON-RPC client which refuses every non-local endpoint."""

    def __init__(self, url: str, *, timeout_seconds: float = 8):
        try:
            parsed = urllib.parse.urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Localnet RPC URL must be loopback HTTP on port 8899") from exc
        if (parsed.scheme != "http" or parsed.hostname not in _LOCAL_HOSTS or port != 8899 or
                parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("Localnet RPC URL must be loopback HTTP on port 8899")
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("Localnet RPC timeout must be between 1 and 30 seconds")
        self.url = url
        self.hostname = parsed.hostname
        self.port = port
        self.timeout_seconds = timeout_seconds

    def call(self, method: str, params: list[Any]) -> Any:
        if method not in _ALLOWED_METHODS:
            raise ValueError("Localnet RPC method is not allowed")
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                             separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(self.url, data=payload, headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": "SolanaMarketWizard/0.2-localnet-check",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                redirected = urllib.parse.urlsplit(response.geturl())
                if (redirected.scheme != "http" or redirected.hostname != self.hostname or
                        redirected.port != self.port):
                    raise LocalnetError("Localnet RPC redirected away from the configured local validator")
                body = response.read(4_000_001)
                if len(body) > 4_000_000:
                    raise LocalnetError("Localnet RPC response exceeded the local limit")
            value = json.loads(body)
        except urllib.error.HTTPError as exc:
            raise LocalnetError(f"Localnet RPC HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise LocalnetError("Localnet RPC was unavailable or returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise LocalnetError("Localnet RPC returned an invalid response")
        error = value.get("error")
        if error:
            detail = error.get("message") if isinstance(error, dict) else str(error)
            raise LocalnetError(f"Localnet RPC rejected {method}: {str(detail)[:300]}")
        if "result" not in value:
            raise LocalnetError("Localnet RPC response has no result")
        return value["result"]


def signing_check(keypair_path: Path, recipient: str, lamports: int, *, rpc_url: str = "http://127.0.0.1:8899",
                  now: float | None = None, confirm_timeout_seconds: float = 8) -> dict[str, object]:
    """Submit one explicit local-only test transfer and return public evidence.

    The keypair never leaves this function. It is never returned, persisted, or
    placed in an RPC request. The resulting transaction only invokes the fixed
    System Program transfer instruction.
    """
    checked_at = time.time() if now is None else _finite_time(now)
    if not isinstance(recipient, str) or not MINT.fullmatch(recipient) or recipient == SYSTEM_PROGRAM:
        raise ValueError("Recipient must be a non-system Solana public key")
    if isinstance(lamports, bool) or not isinstance(lamports, int) or not 1 <= lamports <= 100_000_000:
        raise ValueError("Localnet test transfer must be an integer from 1 to 100000000 lamports")
    if not isinstance(confirm_timeout_seconds, (int, float)) or not 1 <= confirm_timeout_seconds <= 30:
        raise ValueError("Localnet confirmation timeout must be between 1 and 30 seconds")
    private, sender = _load_test_keypair(keypair_path)
    if sender == recipient:
        raise ValueError("Localnet test recipient must differ from the signer")
    rpc = LocalnetRpc(rpc_url)
    before = _balance(rpc.call("getBalance", [sender, {"commitment": "confirmed"}]))
    recipient_before = _balance(rpc.call("getBalance", [recipient, {"commitment": "confirmed"}]))
    if recipient_before <= 0:
        raise LocalnetError("Localnet test recipient must already be funded to avoid creating an under-rent account")
    blockhash = _latest_blockhash(rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}]))
    transaction, signature = _signed_transfer(private, sender, recipient, lamports, blockhash)
    returned_signature = rpc.call("sendTransaction", [base64.b64encode(transaction).decode("ascii"), {
        "encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed", "maxRetries": 0,
    }])
    if not isinstance(returned_signature, str) or returned_signature != signature:
        raise LocalnetError("Localnet RPC returned a different transaction signature")
    confirmation = _wait_for_confirmation(rpc, signature, float(confirm_timeout_seconds))
    after = _balance(rpc.call("getBalance", [sender, {"commitment": "confirmed"}]))
    recipient_after = _balance(rpc.call("getBalance", [recipient, {"commitment": "confirmed"}]))
    return {
        "checked_at": checked_at, "network": "localnet", "rpc": "loopback:8899",
        "sender": sender, "recipient": recipient, "lamports": lamports,
        "system_program": SYSTEM_PROGRAM, "blockhash": blockhash, "signature": signature,
        "sender_lamports_before": before, "sender_lamports_after": after,
        "recipient_lamports_before": recipient_before, "recipient_lamports_after": recipient_after,
        "confirmation": confirmation, "status": "confirmed",
        "coverage": "One explicit local-validator System Program transfer; no trading route, token instruction, or external network was used.",
    }


def _load_test_keypair(path: Path) -> tuple[Ed25519PrivateKey, str]:
    if path.is_symlink():
        raise LocalnetError("Local test keypair path must not be a symbolic link")
    try:
        details = path.stat()
    except OSError as exc:
        raise LocalnetError(f"Cannot read local test keypair: {exc}") from None
    if not stat.S_ISREG(details.st_mode):
        raise LocalnetError("Local test keypair path is not a regular file")
    if details.st_mode & 0o077:
        raise LocalnetError("Local test keypair file must not be readable by group or others")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalnetError("Local test keypair is not a valid JSON file") from exc
    if (not isinstance(raw, list) or len(raw) != 64 or any(isinstance(item, bool) or not isinstance(item, int) or
                                                           not 0 <= item <= 255 for item in raw)):
        raise LocalnetError("Local test keypair must contain exactly 64 byte values")
    secret, expected_public = bytes(raw[:32]), bytes(raw[32:])
    try:
        private = Ed25519PrivateKey.from_private_bytes(secret)
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    except ValueError as exc:
        raise LocalnetError("Local test keypair could not be decoded") from exc
    if public != expected_public:
        raise LocalnetError("Local test keypair public bytes do not match its private seed")
    return private, _encode58(public)


def _signed_transfer(private: Ed25519PrivateKey, sender: str, recipient: str, lamports: int,
                     blockhash: str) -> tuple[bytes, str]:
    sender_bytes = _decode58(sender)
    recipient_bytes = _decode58(recipient)
    system_bytes = _decode58(SYSTEM_PROGRAM)
    blockhash_bytes = _decode58(blockhash)
    # Legacy message header: signer/writable sender, writable recipient, readonly System Program.
    message = b"\x01\x00\x01" + _shortvec(3) + sender_bytes + recipient_bytes + system_bytes + blockhash_bytes
    data = struct.pack("<IQ", 2, lamports)  # SystemInstruction::Transfer plus lamport amount.
    message += _shortvec(1) + bytes((2,)) + _shortvec(2) + bytes((0, 1)) + _shortvec(len(data)) + data
    signature = private.sign(message)
    return _shortvec(1) + signature + message, _encode58(signature)


def _balance(response: Any) -> int:
    if not isinstance(response, dict) or not isinstance(response.get("context"), dict):
        raise LocalnetError("Localnet balance response is incomplete")
    value = response.get("value")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LocalnetError("Localnet balance response is invalid")
    return value


def _latest_blockhash(response: Any) -> str:
    if not isinstance(response, dict) or not isinstance(response.get("value"), dict):
        raise LocalnetError("Localnet blockhash response is incomplete")
    blockhash = response["value"].get("blockhash")
    if not isinstance(blockhash, str):
        raise LocalnetError("Localnet blockhash response is invalid")
    _decode58(blockhash)
    return blockhash


def _wait_for_confirmation(rpc: LocalnetRpc, signature: str, timeout_seconds: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        result = rpc.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
        values = result.get("value") if isinstance(result, dict) else None
        status = values[0] if isinstance(values, list) and len(values) == 1 else None
        if isinstance(status, dict):
            if status.get("err") is not None:
                raise LocalnetError("Localnet test transaction failed during confirmation")
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return {
                    "slot": status.get("slot"), "confirmation_status": status["confirmationStatus"],
                    "confirmations": status.get("confirmations"),
                }
        time.sleep(.15)
    raise LocalnetError("Localnet test transaction was not confirmed before the local timeout")


def _shortvec(value: int) -> bytes:
    if value < 0:
        raise ValueError("Short vector length must not be negative")
    result = bytearray()
    while True:
        current = value & 0x7F
        value >>= 7
        result.append(current | 0x80 if value else current)
        if not value:
            return bytes(result)


def _decode58(value: str) -> bytes:
    number = 0
    for char in value:
        try:
            number = number * 58 + _BASE58.index(char)
        except ValueError as exc:
            raise LocalnetError("Invalid base58 public key or blockhash") from exc
    decoded = b"\0" * (len(value) - len(value.lstrip("1")))
    decoded += number.to_bytes((number.bit_length() + 7) // 8, "big")
    if len(decoded) != 32:
        raise LocalnetError("Public key or blockhash does not decode to 32 bytes")
    return decoded


def _encode58(value: bytes) -> str:
    number = int.from_bytes(value, "big")
    text = ""
    while number:
        number, remainder = divmod(number, 58)
        text = _BASE58[remainder] + text
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + text


def _finite_time(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Localnet check time must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Localnet check time must be finite")
    return result
