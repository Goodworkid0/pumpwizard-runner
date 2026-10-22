"""Read-only wallet inventory and recent onchain activity for the browser Portfolio."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import math
import threading
import time
from pathlib import Path

from solders.transaction import Transaction

from .manual_trades import TradeJournal, TradeRpc
from .market import MarketDataError
from .market_history import SOURCE, pool_histories
from .holding_valuation import wallet_asset_valuation
from .ownership import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from .transfers import GENESIS, TransferError, _uint, public_key


HISTORY_LIMIT = 40
MAX_HISTORY_LIMIT = 50
CACHE_SECONDS = 10
METADATA_CACHE_SECONDS = 3600
MAX_METADATA_MINTS = 40
_CACHE: dict[tuple[str, str, str, int], tuple[float, dict]] = {}
_CACHE_LOCK = threading.RLock()
_METADATA_REFRESH_AT: dict[tuple[str, str], float] = {}


class WalletPortfolioRpc(TradeRpc):
    allowed_methods = TradeRpc.allowed_methods | {
        "getSignaturesForAddress", "getTokenAccountsByOwner",
    }


def _integer(value, label: str, maximum: int = 2**64 - 1) -> int:
    if isinstance(value, bool):
        raise TransferError(f"Wallet RPC returned an invalid {label}")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise TransferError(f"Wallet RPC returned an invalid {label}") from None
    if str(parsed) != str(value) or not 0 <= parsed <= maximum:
        raise TransferError(f"Wallet RPC returned an invalid {label}")
    return parsed


def _token_accounts(rpc, sender: str) -> tuple[list[dict], int, list[str]]:
    grouped: dict[str, dict] = {}
    slots = []
    gaps: list[str] = []
    for program, label in ((TOKEN_PROGRAM, "SPL Token"), (TOKEN_2022_PROGRAM, "Token-2022")):
        response = rpc.call("getTokenAccountsByOwner", [
            sender, {"programId": program},
            {"encoding": "jsonParsed", "commitment": "confirmed"},
        ])
        try:
            slot = _uint(response["context"]["slot"], "token-account slot")
            records = response["value"]
            if not isinstance(records, list):
                raise TypeError
        except (KeyError, TypeError):
            raise TransferError("Wallet RPC returned an incomplete token-account list") from None
        slots.append(slot)
        for record in records:
            try:
                account_address = record["pubkey"]
                account = record["account"]
                info = account["data"]["parsed"]["info"]
                mint = info["mint"]
                owner = info["owner"]
                token = info["tokenAmount"]
                decimals = token["decimals"]
                public_key(account_address)
                public_key(mint)
                if owner != sender or account["owner"] != program:
                    raise ValueError
                if type(decimals) is not int or not 0 <= decimals <= 255:
                    raise ValueError
                amount = _integer(token["amount"], "token amount")
            except (KeyError, TypeError, ValueError, TransferError):
                gaps.append(f"One {label} account could not be decoded and was omitted")
                continue
            asset = grouped.setdefault(mint, {
                "mint": mint, "amount": 0, "decimals": decimals,
                "program": label, "token_program": program, "accounts": 0,
            })
            if asset["decimals"] != decimals or asset["token_program"] != program:
                gaps.append(f"{mint}: inconsistent token account metadata was omitted")
                continue
            asset["amount"] += amount
            if asset["amount"] >= 2**128:
                raise TransferError("Wallet token-account total is outside the supported range")
            asset["accounts"] += 1
    return list(grouped.values()), max(slots, default=0), gaps


def _account_keys(transaction: object) -> tuple[list[str], set[str]]:
    if isinstance(transaction, dict):
        message = transaction.get("message")
        records = message.get("accountKeys") if isinstance(message, dict) else None
        if not isinstance(records, list):
            return [], set()
        keys, signers = [], set()
        for record in records:
            if isinstance(record, str):
                keys.append(record)
            elif isinstance(record, dict) and isinstance(record.get("pubkey"), str):
                keys.append(record["pubkey"])
                if record.get("signer") is True:
                    signers.add(record["pubkey"])
            else:
                return [], set()
        return keys, signers
    if isinstance(transaction, list) and len(transaction) == 2 and transaction[1] == "base64":
        try:
            parsed = Transaction.from_bytes(base64.b64decode(transaction[0], validate=True))
            keys = [str(value) for value in parsed.message.account_keys]
            signers = {keys[index] for index in range(parsed.message.header.num_required_signatures)}
            return keys, signers
        except Exception:
            return [], set()
    return [], set()


def _token_totals(rows: object, sender: str) -> dict[tuple[str, int], int]:
    result: dict[tuple[str, int], int] = {}
    if not isinstance(rows, list):
        return result
    for row in rows:
        try:
            if row.get("owner") != sender:
                continue
            mint = row["mint"]
            token = row["uiTokenAmount"]
            decimals = token["decimals"]
            public_key(mint)
            if type(decimals) is not int or not 0 <= decimals <= 255:
                continue
            amount = _integer(token["amount"], "transaction token amount")
        except (KeyError, TypeError, TransferError):
            continue
        key = (mint, decimals)
        result[key] = result.get(key, 0) + amount
    return result


def _activity_detail(rpc, summary: dict, sender: str, runner: dict[str, dict]) -> dict:
    signature = summary["signature"]
    base = {
        "signature": signature,
        "slot": summary.get("slot"),
        "block_time": summary.get("blockTime"),
        "confirmation_status": summary.get("confirmationStatus"),
        "error": summary.get("err"),
        "source": "Runner" if signature in runner else "External wallet activity",
        "runner_record": runner.get(signature),
        "wallet_signer": None,
        "sol_delta": None,
        "fee": None,
        "token_deltas": [],
        "kind": "failed" if summary.get("err") is not None else "contract interaction",
        "detail_available": False,
    }
    try:
        transaction = rpc.call("getTransaction", [signature, {
            "encoding": "jsonParsed", "commitment": "confirmed",
            "maxSupportedTransactionVersion": 0,
        }])
    except TransferError:
        return base
    if not isinstance(transaction, dict):
        return base
    meta = transaction.get("meta")
    if not isinstance(meta, dict):
        return base
    keys, signers = _account_keys(transaction.get("transaction"))
    wallet_signer = sender in signers
    sol_delta = None
    if sender in keys:
        index = keys.index(sender)
        before, after = meta.get("preBalances"), meta.get("postBalances")
        if isinstance(before, list) and isinstance(after, list) and index < len(before) and index < len(after):
            try:
                sol_delta = _integer(after[index], "post balance") - _integer(before[index], "pre balance")
            except TransferError:
                sol_delta = None
    previous = _token_totals(meta.get("preTokenBalances"), sender)
    current = _token_totals(meta.get("postTokenBalances"), sender)
    deltas = []
    for mint, decimals in sorted(set(previous) | set(current)):
        delta = current.get((mint, decimals), 0) - previous.get((mint, decimals), 0)
        if delta:
            deltas.append({"mint": mint, "decimals": decimals, "amount": str(delta)})
    error = meta.get("err", summary.get("err"))
    if error is not None:
        kind = "failed"
    elif any(int(item["amount"]) < 0 for item in deltas) and sol_delta is not None and sol_delta > 0:
        kind = "likely sell"
    elif any(int(item["amount"]) > 0 for item in deltas) and sol_delta is not None and sol_delta < 0:
        kind = "likely buy"
    elif any(int(item["amount"]) < 0 for item in deltas):
        kind = "token sent"
    elif any(int(item["amount"]) > 0 for item in deltas):
        kind = "token received"
    elif sol_delta is not None and sol_delta > 0:
        kind = "SOL received"
    elif sol_delta is not None and sol_delta < 0:
        kind = "SOL sent or program debit"
    else:
        kind = "contract interaction"
    fee = meta.get("fee")
    try:
        fee = str(_integer(fee, "transaction fee")) if fee is not None else None
    except TransferError:
        fee = None
    return {
        **base,
        "error": error,
        "wallet_signer": wallet_signer,
        "sol_delta": None if sol_delta is None else str(sol_delta),
        "fee": fee,
        "token_deltas": deltas,
        "kind": kind,
        "detail_available": True,
    }


def _trend_points(library, mint: str) -> list[dict[str, float]]:
    local = [
        {"at": float(row["observed_at"]), "price_usd": float(row["price_usd"])}
        for row in reversed(library.coin_history(mint, 128))
        if float(row.get("price_usd") or 0) > 0
    ]
    pools = pool_histories(library, mint)
    selected = max(
        (pool["candles"] for pool in pools if pool.get("candles")),
        key=lambda rows: float(rows[0]["opened_at"]), default=[],
    )
    if not selected:
        selected = library.market_history_candles(mint, 128, source=SOURCE)
    candles = [
        {"at": float(row["opened_at"]), "price_usd": float(row["close_usd"])}
        for row in reversed(selected[:128]) if float(row["close_usd"]) > 0
    ]
    if len(local) >= 2 and (not candles or local[-1]["at"] >= candles[-1]["at"]):
        return local
    return candles or local


def _valuation_state(
    details: dict, estimated_value: Decimal | None, valuation: dict,
) -> tuple[str, str]:
    """Classify display-only value evidence without turning missing data into zero."""
    if details["price_usd"] is None:
        return "unpriced", "No reliable exact-mint USD price is available"
    freshness = valuation["mark"]["freshness"]
    if freshness != "fresh":
        # Keep the established display state union fail-closed while the nested
        # valuation retains the exact future/invalid freshness reason.
        display_state = "missing_time" if freshness == "missing_time" else "stale"
        return display_state, (
            "The retained mark time is missing" if freshness == "missing_time" else
            f"The retained mark is {freshness}; current value is unavailable"
        )
    if details.get("suspicious"):
        return "flagged", "The public token profile marks this mint as suspicious"
    liquidity = details.get("liquidity_usd")
    holders = details.get("holder_count")
    inactive = details.get("volume_change_24h") is not None and details["volume_change_24h"] <= -90
    low_quality = details.get("organic_score_label") == "low"
    concentrated = details.get("top_holders_percent") is not None and details["top_holders_percent"] >= 95
    tiny_value = estimated_value is not None and estimated_value < Decimal("0.01")
    near_empty_market = liquidity is not None and liquidity < 250
    if tiny_value and near_empty_market and (inactive or low_quality or concentrated or holders is not None and holders <= 3):
        return "likely-worthless", "Under $0.01 held value, under $250 liquidity, and weak public market-quality evidence"
    if liquidity is not None and liquidity < 5_000:
        return "illiquid", f"Only ${liquidity:,.0f} reported liquidity; an executable exit may be poor or unavailable"
    return "priced", "Exact-mint public price context is available; this is not an executable quote"


def _metadata(library, mints: set[str], profiles: dict[str, dict], slot: int) -> dict[str, dict]:
    latest = {row["mint"]: row for row in library.market_watch_rows(500) if row["mint"] in mints}
    result = {}
    for mint in mints:
        row = latest.get(mint, {})
        metadata = library.coin_metadata(mint)
        profile = profiles.get(mint, {})
        profile_metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
        profile_price = profile.get("price_usd")
        price = profile_price if isinstance(profile_price, (int, float)) else row.get("price_usd")
        block = profile.get("price_block_id")
        result[mint] = {
            "symbol": row.get("symbol") or metadata.get("symbol") or profile_metadata.get("symbol"),
            "name": metadata.get("name") or profile_metadata.get("name"),
            "website": metadata.get("website") or profile_metadata.get("website"),
            "image_url": metadata.get("image_url") or profile_metadata.get("image_url"),
            "creator": metadata.get("creator") or profile_metadata.get("creator"),
            "metadata_source": metadata.get("source") or profile_metadata.get("source"),
            "created_at": profile.get("created_at") or row.get("created_at"),
            "price_usd": str(price) if price is not None else None,
            "observed_at": None if profile_price is not None else row.get("observed_at"),
            "price_source": "Jupiter Tokens V2" if profile_price is not None else ("Runner watchlist" if price is not None else None),
            "price_source_slot": block if isinstance(block, int) else None,
            "price_slot_lag": max(0, slot - block) if isinstance(block, int) else None,
            "liquidity_usd": profile.get("liquidity_usd"),
            "market_cap_usd": profile.get("market_cap_usd"),
            "price_change_24h": profile.get("price_change_24h"),
            "volume_change_24h": profile.get("volume_change_24h"),
            "holder_count": profile.get("holder_count"),
            "organic_score": profile.get("organic_score"),
            "organic_score_label": profile.get("organic_score_label"),
            "suspicious": bool(profile.get("suspicious")),
            "top_holders_percent": profile.get("top_holders_percent"),
            "trend_points": _trend_points(library, mint),
        }
    return result


def _refresh_metadata(directory: Path, library, mints: set[str], source) -> None:
    now = time.time()
    with _CACHE_LOCK:
        selected = {
            mint for mint in sorted(mints)[:MAX_METADATA_MINTS]
            if now - _METADATA_REFRESH_AT.get((str(directory.resolve()), mint), 0) >= METADATA_CACHE_SECONDS
        }
    if not selected:
        return
    rows = source.token_metadata(selected)
    for mint, metadata in rows.items():
        if mint in selected and metadata:
            library.record_coin_metadata(mint, metadata, updated_at=now)
    with _CACHE_LOCK:
        for mint in selected:
            _METADATA_REFRESH_AT[(str(directory.resolve()), mint)] = now


def _load(directory: Path, library, sender: str, network: str, rpc, limit: int,
          metadata_source=None, market_source=None) -> dict:
    checked_at = time.time()
    if network not in GENESIS:
        raise TransferError("Wallet Portfolio currently supports mainnet and devnet")
    public_key(sender)
    if rpc.genesis() != GENESIS[network]:
        raise TransferError("RPC network identity does not match the connected wallet")
    balance = _uint(rpc.call("getBalance", [sender, {"commitment": "confirmed"}])["value"], "wallet balance")
    sol_usd_price = None
    if market_source is not None:
        try:
            candidate = float(market_source.sol_usd_price())
            if math.isfinite(candidate) and candidate > 0:
                sol_usd_price = candidate
        except (AttributeError, MarketDataError, OSError, TypeError, ValueError):
            # A display-only estimate must never hide authoritative holdings.
            pass
    assets, slot, gaps = _token_accounts(rpc, sender)
    journal = TradeJournal(directory)
    try:
        positions = [row for row in journal.positions(sender, GENESIS[network]) if row["status"] == "open"]
        runner_by_mint: dict[str, dict] = {}
        for position in positions:
            item = runner_by_mint.setdefault(position["mint"], {"atoms": 0, "position_ids": []})
            item["atoms"] += int(position["quantity"])
            item["position_ids"].append(position["id"])
        runner_signatures = {
            row["signature"]: {"kind": "trade", "id": row["id"], "status": row["status"]}
            for row in journal.trades(sender) if row["genesis"] == GENESIS[network]
        }
        runner_signatures.update({
            row["signature"]: {"kind": "withdrawal", "id": row["id"], "status": row["status"]}
            for row in journal.rows(sender) if row["genesis"] == GENESIS[network]
        })
    finally:
        journal.close()
    amounts = {asset["mint"]: asset["amount"] for asset in assets}
    mints = set(amounts) | set(runner_by_mint)
    if metadata_source is not None:
        try:
            _refresh_metadata(directory, library, set(amounts), metadata_source)
        except (OSError, ValueError):
            # Token identity and balances remain authoritative even when a
            # public project-metadata provider is unavailable.
            pass
    try:
        profiles = market_source.token_profiles(set(amounts)) if market_source is not None else {}
    except (AttributeError, MarketDataError, OSError, TypeError, ValueError):
        profiles = {}
    names = _metadata(library, mints, profiles, slot)
    reconciliations = []
    for mint, recorded in sorted(runner_by_mint.items()):
        actual = amounts.get(mint, 0)
        if actual == recorded["atoms"]:
            status = "matched"
        elif actual == 0:
            status = "externally closed"
        elif actual < recorded["atoms"]:
            status = "wallet shortfall"
        else:
            status = "additional wallet tokens"
        reconciliations.append({
            "mint": mint,
            "runner_atoms": str(recorded["atoms"]),
            "wallet_atoms": str(actual),
            "difference_atoms": str(actual - recorded["atoms"]),
            "position_ids": recorded["position_ids"],
            "status": status,
            "sellable": actual >= recorded["atoms"],
        })
    views = []
    for asset in assets:
        if not asset["amount"]:
            continue
        recorded = runner_by_mint.get(asset["mint"], {"atoms": 0, "position_ids": []})
        details = names[asset["mint"]]
        valuation = wallet_asset_valuation(
            amount_atoms=asset["amount"], decimals=asset["decimals"],
            price_usd=details["price_usd"], observed_at=details["observed_at"],
            source_slot=details["price_source_slot"], current_slot=slot,
            mark_source=details["price_source"], now=checked_at,
        )
        estimated_value = None
        if valuation["current_value"]["amount"] is not None:
            try:
                estimated_value = Decimal(valuation["current_value"]["amount"])
            except (InvalidOperation, ValueError):
                estimated_value = None
        valuation_state, valuation_detail = _valuation_state(details, estimated_value, valuation)
        views.append({
            **asset,
            "amount": str(asset["amount"]),
            "runner_atoms": str(recorded["atoms"]),
            "untracked_atoms": str(max(0, asset["amount"] - recorded["atoms"])),
            "runner_position_ids": recorded["position_ids"],
            **details,
            "estimated_value_usd": None if estimated_value is None else str(estimated_value),
            "valuation_state": valuation_state,
            "valuation_detail": valuation_detail,
            "valuation": valuation,
        })
    views.sort(key=lambda item: (-(1 if int(item["runner_atoms"]) else 0),
                                 (item["symbol"] or item["name"] or item["mint"]).lower()))
    summaries = rpc.call("getSignaturesForAddress", [sender, {
        "commitment": "confirmed", "limit": limit,
    }])
    if not isinstance(summaries, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("signature"), str)
        for row in summaries
    ):
        raise TransferError("Wallet RPC returned an incomplete transaction history")
    # The production RPC client is stateless and safe to fan out. Test and
    # embedded RPC adapters can carry per-call selection state, so keep those
    # adapters sequential rather than allowing one request to select another
    # wallet's synthetic bank.
    workers = min(5, max(1, len(summaries))) if isinstance(rpc, WalletPortfolioRpc) else 1
    with ThreadPoolExecutor(max_workers=workers) as executor:
        activity = list(executor.map(
            lambda row: _activity_detail(rpc, row, sender, runner_signatures), summaries
        ))
    return {
        "sender": sender,
        "network": network,
        "balance": str(balance),
        "sol_usd_price": sol_usd_price,
        "estimated_usd": None if sol_usd_price is None else balance / 1_000_000_000 * sol_usd_price,
        "slot": slot,
        "checked_at": checked_at,
        "assets": views,
        "reconciliations": reconciliations,
        "activity": activity,
        "coverage_gaps": sorted(set(gaps)),
        "history_limit": limit,
        "history_complete": len(summaries) < limit,
        "coverage": (
            "Confirmed SPL Token and Token-2022 owner accounts plus recent confirmed address history. "
            "Activity labels are inferred from wallet balance deltas; Runner accounting uses only Runner-reviewed finalized receipts."
        ),
    }


def read(directory: Path, library, sender: str, network: str, *, limit: int = HISTORY_LIMIT,
         refresh: bool = False, rpc=None, market_source=None) -> dict:
    if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_LIMIT:
        raise TransferError(f"Wallet history limit must be 1–{MAX_HISTORY_LIMIT}")
    directory = Path(directory)
    key = (str(directory.resolve()), sender, network, limit)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if not refresh and cached and time.time() - cached[0] < CACHE_SECONDS:
            return deepcopy(cached[1])
        metadata_source = None
        if rpc is None:
            from .web_trading import rpc_for

            base = rpc_for(network, directory)
            if hasattr(base, "url"):
                rpc = WalletPortfolioRpc(network, base.url)
                metadata_source = market_source
            else:
                rpc = base
        result = _load(directory, library, sender, network, rpc, limit, metadata_source, market_source)
        _CACHE[key] = (time.time(), deepcopy(result))
        return result


_INVENTORY_CACHE: dict[tuple[str, str, str], tuple[float, dict]] = {}


def inventory(directory: Path, library, sender: str, network: str, *, refresh=False,
              rpc=None, market_source=None) -> dict:
    """Public address balances only; no wallet authority, journal or transaction history."""
    public_key(sender)
    if network not in GENESIS:
        raise TransferError("Choose mainnet or devnet")
    cache_key = (str(Path(directory).resolve()), sender, network)
    with _CACHE_LOCK:
        cached = _INVENTORY_CACHE.get(cache_key)
        if not refresh and cached and time.time() - cached[0] < CACHE_SECONDS:
            return deepcopy(cached[1])
    if rpc is None:
        from .web_trading import rpc_for
        base = rpc_for(network, directory)
        rpc = WalletPortfolioRpc(network, base.url) if hasattr(base, "url") else base
    if rpc.genesis() != GENESIS[network]:
        raise TransferError("RPC network identity does not match the selected network")
    balance = _uint(rpc.call("getBalance", [sender, {"commitment": "confirmed"}])["value"], "wallet balance")
    assets, slot, gaps = _token_accounts(rpc, sender)
    tokens = []
    for asset in assets:
        if asset["amount"] <= 0:
            continue
        metadata = library.coin_metadata(asset["mint"]) or {}
        tokens.append({
            "mint": asset["mint"], "amount": str(asset["amount"]),
            "decimals": asset["decimals"], "program": asset["program"],
            "symbol": metadata.get("symbol"), "name": metadata.get("name"),
        })
    sol_usd = None
    if market_source is not None:
        try:
            price = float(market_source.sol_usd_price())
            if math.isfinite(price) and price > 0:
                sol_usd = price
        except (AttributeError, MarketDataError, OSError, TypeError, ValueError):
            pass
    result = {"sender": sender, "network": network, "balance": str(balance),
              "sol_usd_price": sol_usd, "assets": tokens, "slot": slot,
              "checked_at": time.time(), "coverage_gaps": gaps}
    with _CACHE_LOCK:
        if len(_INVENTORY_CACHE) >= 100:
            _INVENTORY_CACHE.pop(next(iter(_INVENTORY_CACHE)))
        _INVENTORY_CACHE[cache_key] = (time.time(), deepcopy(result))
    return result
