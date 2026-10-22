"""Bounded local on-chain ownership evidence for strict entry policies.

The copied strategy never supplies an RPC endpoint or an ownership verdict.
This module only performs fixed read-only Solana JSON-RPC calls against an
endpoint selected by the person running the local application.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from .market import MINT
from .strategies import content_hash


TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
# All allowed calls are fixed read-only JSON-RPC methods.  A future submitting
# client must be separate so this client cannot be widened into a signer.
_ALLOWED_METHODS = {
    "getAccountInfo", "getBalance", "getProgramAccounts", "getTokenAccountsByOwner",
}


class OwnershipDataError(RuntimeError):
    """An RPC response could not provide complete ownership evidence."""


class OwnershipSource(Protocol):
    def check(self, mint: str, now: float) -> dict[str, Any]: ...


@dataclass(frozen=True)
class OwnershipPolicy:
    """Versioned local checks used when a strategy has a strict ownership gate."""

    version: str = "ownership-basic-v1"
    max_holder_accounts: int = 5_000
    max_holder_fraction: float = 0.10
    max_top10_fraction: float = 0.35

    @property
    def policy_id(self) -> str:
        return self.version + ":" + content_hash(asdict(self))[:16]


class JsonRpcClient:
    """A small, read-only, bounded JSON-RPC client without wallet material."""

    def __init__(self, url: str, *, timeout_seconds: float = 10, min_spacing_seconds: float = 0.15):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or
                parsed.fragment):
            raise ValueError("Ownership RPC URL must be an HTTPS endpoint without embedded credentials")
        if not 1 <= timeout_seconds <= 30 or not 0 <= min_spacing_seconds <= 5:
            raise ValueError("Invalid ownership RPC timeout or request spacing")
        self.url = url
        self.hostname = parsed.hostname
        self.timeout_seconds = timeout_seconds
        self.min_spacing_seconds = min_spacing_seconds
        self._last_request_at = 0.0

    def call(self, method: str, params: list[Any]) -> Any:
        if method not in _ALLOWED_METHODS:
            raise ValueError("Ownership RPC method is not allowed")
        wait = self.min_spacing_seconds - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                             separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(self.url, data=payload, headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": "SolanaMarketWizard/0.2",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                redirected = urllib.parse.urlsplit(response.geturl())
                if redirected.scheme != "https" or redirected.hostname != self.hostname:
                    raise OwnershipDataError("Ownership RPC redirected to a different endpoint")
                body = response.read(8_000_001)
                if len(body) > 8_000_000:
                    raise OwnershipDataError("Ownership RPC response exceeded the local scan limit")
            response_json = json.loads(body)
        except urllib.error.HTTPError as exc:
            raise OwnershipDataError(f"Ownership RPC HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise OwnershipDataError("Ownership RPC was unavailable or returned invalid JSON") from exc
        if not isinstance(response_json, dict) or response_json.get("error") or "result" not in response_json:
            raise OwnershipDataError("Ownership RPC could not provide the requested evidence")
        return response_json["result"]


class SolanaOwnershipSource:
    def __init__(self, rpc_url: str, policy: OwnershipPolicy | None = None):
        self.policy = policy or OwnershipPolicy()
        self.rpc = JsonRpcClient(rpc_url)

    def check(self, mint: str, now: float) -> dict[str, Any]:
        return check_mint(mint, self.rpc, self.policy, now)


def check_mint(mint: str, rpc: JsonRpcClient | Any, policy: OwnershipPolicy, now: float) -> dict[str, Any]:
    """Return a fail-closed, durable evidence record for one mint."""
    result: dict[str, Any] = {
        "mint": mint,
        "checked_at": now,
        "policy_id": policy.policy_id,
        "source": "solana_json_rpc",
    }
    try:
        if not isinstance(mint, str) or not MINT.fullmatch(mint):
            raise OwnershipDataError("Invalid mint address")
        mint_response = rpc.call("getAccountInfo", [mint, {
            "encoding": "jsonParsed", "commitment": "confirmed",
        }])
        if not isinstance(mint_response, dict):
            raise OwnershipDataError("Mint account response is incomplete")
        mint_account = mint_response.get("value")
        context = mint_response.get("context")
        if not isinstance(mint_account, dict) or not isinstance(context, dict) or not isinstance(context.get("slot"), int):
            raise OwnershipDataError("Mint account is unavailable")
        token_program, info = _mint_info(mint_account)
        flags: list[str] = []
        unknown: list[str] = []
        if info.get("mintAuthority"):
            flags.append("Mint authority can create more tokens")
        if info.get("freezeAuthority"):
            flags.append("Freeze authority can freeze token accounts")
        _check_extensions(token_program, info, flags, unknown)
        holders = _holder_accounts(rpc, token_program, mint, context["slot"], policy)
        holder_result = _analyze_holders(mint, mint_account, info, holders, rpc, policy, flags, unknown)
        result.update(holder_result)
        result["status"] = "blocked" if flags else "unknown" if unknown else "clear"
        result["reasons"] = list(dict.fromkeys(flags + unknown))
    except OwnershipDataError as exc:
        result.update(status="unknown", reasons=[str(exc)])
    except (KeyError, TypeError, ValueError, IndexError, OverflowError):
        result.update(status="unknown", reasons=["Ownership evidence was incomplete or could not be decoded"])
    return result


def _mint_info(account: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    token_program = account.get("owner")
    data = account.get("data")
    if token_program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) or not isinstance(data, dict):
        raise OwnershipDataError("Mint account has an unsupported token program")
    parsed = data.get("parsed")
    if not isinstance(parsed, dict) or parsed.get("type") != "mint" or not isinstance(parsed.get("info"), dict):
        raise OwnershipDataError("Mint account is not JSON-parsed token mint data")
    if _token_amount(parsed["info"].get("supply")) <= 0:
        raise OwnershipDataError("Mint supply is missing or zero")
    return token_program, parsed["info"]


def _check_extensions(token_program: str, info: dict[str, Any], flags: list[str], unknown: list[str]) -> None:
    extensions = info.get("extensions")
    if token_program == TOKEN_2022_PROGRAM and not isinstance(extensions, list):
        unknown.append("Token-2022 extension data is unavailable")
        return
    if extensions is None:
        return
    if not isinstance(extensions, list):
        unknown.append("Token extension data is malformed")
        return
    for extension in extensions:
        name = extension.get("extension") if isinstance(extension, dict) else None
        if name not in ("metadataPointer", "tokenMetadata"):
            flags.append("Unsupported or restrictive token extension")


def _holder_accounts(rpc: JsonRpcClient | Any, token_program: str, mint: str, slot: int,
                     policy: OwnershipPolicy) -> list[dict[str, Any]]:
    result = rpc.call("getProgramAccounts", [token_program, {
        "encoding": "jsonParsed", "commitment": "confirmed", "minContextSlot": slot,
        "withContext": True, "filters": [{"memcmp": {"offset": 0, "bytes": mint}}],
    }])
    if not isinstance(result, dict) or not isinstance(result.get("value"), list):
        raise OwnershipDataError("Holder-account response is incomplete")
    holders = result["value"]
    if len(holders) > policy.max_holder_accounts:
        raise OwnershipDataError(f"Holder scan exceeds {policy.max_holder_accounts:,}-account coverage limit")
    return holders


def _analyze_holders(mint: str, mint_account: dict[str, Any], mint_info: dict[str, Any],
                     holders: list[dict[str, Any]], rpc: JsonRpcClient | Any, policy: OwnershipPolicy,
                     flags: list[str], unknown: list[str]) -> dict[str, Any]:
    expected_program = mint_account["owner"]
    supply = _token_amount(mint_info["supply"])
    owner_balances: dict[str, int] = {}
    total = 0
    curve_vault_amount = 0
    curve = _curve_address(mint)
    curve_program = _account_owner(rpc, curve)
    if curve_program != PUMP_PROGRAM:
        unknown.append("Bonding-curve program account could not be verified")
    seen: set[str] = set()
    for holder in holders:
        pubkey, owner, amount = _holder_info(holder, mint, expected_program)
        if pubkey in seen:
            raise OwnershipDataError("Holder scan contains a duplicate token account")
        seen.add(pubkey)
        total += amount
        if owner == curve and curve_program == PUMP_PROGRAM:
            curve_vault_amount += amount
        else:
            owner_balances[owner] = owner_balances.get(owner, 0) + amount
    if total != supply:
        unknown.append("Holder balances do not reconcile to the mint supply")
    distributed = supply - curve_vault_amount
    ranked = sorted(owner_balances.values(), reverse=True)
    top = ranked[0] / distributed if ranked and distributed > 0 else None
    top10 = sum(ranked[:10]) / distributed if distributed > 0 else None
    if distributed <= 0:
        unknown.append("No distributed supply remains after the verified curve vault")
    if top is not None and top > policy.max_holder_fraction:
        flags.append("One owner exceeds the local concentration limit")
    if top10 is not None and top10 > policy.max_top10_fraction:
        flags.append("Top ten owners exceed the local concentration limit")
    return {
        "holder_accounts_checked": len(holders), "holder_count": sum(amount > 0 for amount in owner_balances.values()),
        "top_owner_fraction": top, "top10_fraction": top10,
        "curve_vault_tokens": str(curve_vault_amount), "supply_tokens": str(supply),
        "coverage": "Current mint authorities and full current token-account holder scan; linked wallets and transaction history are not proven.",
    }


def _holder_info(holder: dict[str, Any], mint: str, expected_program: str) -> tuple[str, str, int]:
    if not isinstance(holder, dict) or not isinstance(holder.get("pubkey"), str) or not isinstance(holder.get("account"), dict):
        raise OwnershipDataError("Holder account record is malformed")
    account = holder["account"]
    data = account.get("data")
    if account.get("owner") != expected_program or not isinstance(data, dict) or not isinstance(data.get("parsed"), dict):
        raise OwnershipDataError("Holder account has an unsupported token program")
    parsed = data["parsed"]
    info = parsed.get("info")
    if parsed.get("type") != "account" or not isinstance(info, dict) or info.get("mint") != mint:
        raise OwnershipDataError("Holder account does not match the requested mint")
    owner = info.get("owner")
    if not isinstance(owner, str) or not owner:
        raise OwnershipDataError("Holder owner is missing")
    if info.get("state") == "frozen" or info.get("delegate"):
        raise OwnershipDataError("Holder scan contains a frozen or delegated token account")
    token_amount = info.get("tokenAmount")
    amount = _token_amount(token_amount.get("amount") if isinstance(token_amount, dict) else None)
    return holder["pubkey"], owner, amount


def _account_owner(rpc: JsonRpcClient | Any, address: str) -> str | None:
    response = rpc.call("getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}])
    if not isinstance(response, dict) or not isinstance(response.get("value"), dict):
        return None
    owner = response["value"].get("owner")
    return owner if isinstance(owner, str) else None


def _token_amount(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise OwnershipDataError("Token amount is missing")
    amount = int(value)
    if amount < 0:
        raise OwnershipDataError("Token amount is invalid")
    return amount


def _curve_address(mint: str) -> str:
    mint_bytes = _decode58(mint)
    program_bytes = _decode58(PUMP_PROGRAM)
    prime = 2 ** 255 - 19
    d = -121665 * pow(121666, prime - 2, prime) % prime
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(
            b"bonding-curve" + mint_bytes + bytes([bump]) + program_bytes + b"ProgramDerivedAddress"
        ).digest()
        y = int.from_bytes(digest, "little") & (2 ** 255 - 1)
        x_squared = (y * y - 1) * pow(d * y * y + 1, prime - 2, prime) % prime
        on_curve = y < prime and (x_squared == 0 or pow(x_squared, (prime - 1) // 2, prime) == 1)
        if not on_curve:
            return _encode58(digest)
    raise OwnershipDataError("Bonding-curve address could not be derived")


def _decode58(value: str) -> bytes:
    number = 0
    for char in value:
        try:
            number = number * 58 + _BASE58.index(char)
        except ValueError as exc:
            raise OwnershipDataError("Invalid base58 address") from exc
    decoded = b"\0" * (len(value) - len(value.lstrip("1")))
    decoded += number.to_bytes((number.bit_length() + 7) // 8, "big")
    if len(decoded) != 32:
        raise OwnershipDataError("Address does not decode to a public key")
    return decoded


def _encode58(value: bytes) -> str:
    number = int.from_bytes(value, "big")
    out = ""
    while number:
        number, remainder = divmod(number, 58)
        out = _BASE58[remainder] + out
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + out
