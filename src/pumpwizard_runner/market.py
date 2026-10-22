"""Independent public market collection for the local runner.

The source adapter talks directly to a market provider. It never calls a
hosted strategy service and only accepts fixed read-only market-data paths.
"""
from __future__ import annotations

import concurrent.futures
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from datetime import datetime
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from .decision import MarketQuote


PUMP_PUBLIC_BASE = "https://frontend-api-v3.pump.fun"
DEXSCREENER_BASE = "https://api.dexscreener.com"
JUPITER_LITE_BASE = "https://lite-api.jup.ag"
MINT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SOL_QUOTES = {"So11111111111111111111111111111111111111112", "11111111111111111111111111111111"}
PROVIDER_DIAGNOSTICS_SCHEMA = "pumpwizard.provider-diagnostics/v1"
PROVIDER_REJECTION_CATEGORIES = {
    "empty_eligible_universe",
    "unsupported_shape",
    "invalid_identity_or_units",
    "stale_or_unknown_source_time",
    "unavailable_route",
    "request_failure",
    "exhausted_quota",
    "outside_requested_universe",
    "quality_gate",
}


class MarketDataError(RuntimeError):
    pass


class MarketCollectionBoundError(MarketDataError):
    pass


class MarketNormalizationError(ValueError):
    """A safe, typed reason why a provider row could not become a quote."""

    def __init__(self, category: str, message: str):
        if category not in PROVIDER_REJECTION_CATEGORIES:
            raise ValueError("Unknown market-data rejection category")
        super().__init__(message)
        self.category = category


class _CollectionDiagnostics:
    """Bounded, credential-free accounting for one Pump collection attempt."""

    def __init__(self, *, maximum_http_requests: int, maximum_elapsed_seconds: float,
                 maximum_response_bytes: int, maximum_concurrency: int):
        self.started_at = time.time()
        self.bounds = {
            "maximum_http_requests": maximum_http_requests,
            "maximum_elapsed_seconds": maximum_elapsed_seconds,
            "maximum_response_bytes": maximum_response_bytes,
            "maximum_concurrency": maximum_concurrency,
        }
        self.stages = {
            "fx_requests": 0,
            "discovery_requests": 0,
            "listing_rows": 0,
            "unique_listings": 0,
            "eligible_candidates": 0,
            "selected_records": 0,
            "selected_detail_requests": 0,
            "returned_details": 0,
            "normalized_quotes": 0,
            "http_requests": 0,
            "usable_discovery_responses": 0,
            "response_bytes": 0,
        }
        self.rejections = {category: 0 for category in sorted(PROVIDER_REJECTION_CATEGORIES)}
        self.endpoint_failures = 0
        self.shape_failures = 0
        self.quota_exhausted = False

    def reject(self, category: str, count: int = 1) -> None:
        if category not in self.rejections or count < 0:
            raise ValueError("Invalid provider rejection accounting")
        self.rejections[category] += count

    def request(self, source: "PumpPublicSource", path: str, *, stage: str) -> Any:
        elapsed = time.time() - self.started_at
        if (self.quota_exhausted
                or self.stages["http_requests"] >= self.bounds["maximum_http_requests"]
                or self.stages["response_bytes"] >= self.bounds["maximum_response_bytes"]
                or elapsed > self.bounds["maximum_elapsed_seconds"]):
            if not self.quota_exhausted:
                self.reject("exhausted_quota")
                self.quota_exhausted = True
            raise MarketDataError("Market-data collection bound was exhausted")
        self.stages["http_requests"] += 1
        self.stages["fx_requests" if stage == "fx" else "discovery_requests"] += 1
        source._response_byte_limit = (
            self.bounds["maximum_response_bytes"] - self.stages["response_bytes"]
        )
        try:
            value = source._get_json(path)
        except MarketCollectionBoundError:
            self.quota_exhausted = True
            self.reject("exhausted_quota")
            raise
        except MarketDataError:
            self.endpoint_failures += 1
            self.reject("request_failure")
            raise
        finally:
            source._response_byte_limit = None
        try:
            response_bytes = len(json.dumps(
                value, allow_nan=False, ensure_ascii=False, separators=(",", ":"),
            ).encode())
        except (TypeError, ValueError):
            self.shape_failures += 1
            self.reject("unsupported_shape")
            raise MarketDataError("Market-data response was not bounded JSON") from None
        remaining_bytes = self.bounds["maximum_response_bytes"] - self.stages["response_bytes"]
        self.stages["response_bytes"] += min(response_bytes, remaining_bytes)
        if (response_bytes > remaining_bytes
                or time.time() - self.started_at > self.bounds["maximum_elapsed_seconds"]):
            self.quota_exhausted = True
            self.reject("exhausted_quota")
            raise MarketDataError("Market-data collection bound was exhausted")
        return value

    def finish(self, quotes: list[MarketQuote]) -> dict[str, Any]:
        finished_at = time.time()
        self.stages["normalized_quotes"] = len(quotes)
        if (not quotes and not self.stages["eligible_candidates"]
                and self.stages["usable_discovery_responses"] and not self.quota_exhausted):
            self.reject("empty_eligible_universe")
        selected_rejections = max(0, self.stages["selected_records"] - len(quotes))
        if self.quota_exhausted:
            status = "quota_exhausted"
        elif self.endpoint_failures or self.shape_failures:
            status = "partial" if quotes or self.stages["usable_discovery_responses"] else "failing"
        elif quotes and selected_rejections:
            status = "partial"
        elif quotes:
            status = "healthy"
        elif self.rejections["stale_or_unknown_source_time"]:
            status = "stale"
        elif not self.stages["eligible_candidates"]:
            status = "empty"
        else:
            status = "failing"
        known_source_times = [quote.source_at for quote in quotes if quote.source_at > 0]
        source_ages = [max(0.0, finished_at - value) for value in known_source_times]
        reasons = []
        if self.endpoint_failures:
            reasons.append("endpoint_request_failure")
        if self.shape_failures:
            reasons.append("unsupported_response_shape")
        if self.quota_exhausted:
            reasons.append("collection_bound_exhausted")
        if not quotes:
            reasons.append("no_normalized_quotes")
        if selected_rejections:
            reasons.append("selected_records_rejected")
        return {
            "schema": PROVIDER_DIAGNOSTICS_SCHEMA,
            "provider": "PumpPublicSource",
            "started_at": self.started_at,
            "finished_at": finished_at,
            "status": status,
            "bounds": dict(self.bounds),
            "stages": dict(self.stages),
            "rejections": {key: value for key, value in self.rejections.items() if value},
            "provenance": {
                "provider_hosts": ["frontend-api-v3.pump.fun"],
                "endpoint_classes": ["sol_usd_reference", "curve_discovery_listing"],
                "normalized_routes": ["curve"],
                "source_timestamp_field": "updated_at",
                "receipt_timestamp_field": "observed_at",
                "response_byte_basis": "canonical-decoded-json",
            },
            "readiness": {
                "ready": bool(quotes),
                "routes": sorted({quote.route for quote in quotes}),
                "reasons": reasons,
                "first_received_at": min((quote.observed_at for quote in quotes), default=None),
                "earliest_source_at": min(known_source_times, default=None),
                "latest_source_at": max(known_source_times, default=None),
                "maximum_source_age_seconds": max(source_ages, default=None),
            },
        }


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _https_url(value: object) -> str:
    """Keep display-only links bounded and limited to ordinary HTTPS URLs."""
    if not isinstance(value, str) or len(value) > 500:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return ""
    except ValueError:
        return ""
    return urllib.parse.urlunsplit(parsed)


def _pump_metadata(coin: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, limit in (("name", 120), ("description", 1000)):
        value = _bounded_text(coin.get(key), limit)
        if value:
            result[key] = value
    creator = str(coin.get("creator") or "")
    if MINT.fullmatch(creator):
        result["creator"] = creator
    for key in ("website", "twitter", "telegram"):
        value = _https_url(coin.get(key))
        if value:
            result[key] = value
    image = _https_url(coin.get("image_uri"))
    if image:
        result["image_url"] = image
    if isinstance(coin.get("verified"), bool):
        result["verified"] = coin["verified"]
    try:
        _timestamp_seconds(coin["updated_at"])
    except (KeyError, TypeError, ValueError):
        pass
    else:
        result["source_time_basis"] = "provider"
    result["source"] = "Pump.fun"
    return result


def _pair_metadata(pair: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"source": "PumpSwap"}
    name = _bounded_text(base.get("name"), 120)
    if name:
        result["name"] = name
    info = pair.get("info")
    if not isinstance(info, dict):
        return result
    image = _https_url(info.get("imageUrl"))
    if image:
        result["image_url"] = image
    websites = info.get("websites")
    if isinstance(websites, list):
        for item in websites:
            if isinstance(item, dict):
                url = _https_url(item.get("url"))
                if url:
                    result["website"] = url
                    break
    socials = info.get("socials")
    if isinstance(socials, list):
        for item in socials:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").lower()
            key = "twitter" if kind in ("twitter", "x") else "telegram" if kind == "telegram" else ""
            url = _https_url(item.get("url"))
            if key and url and key not in result:
                result[key] = url
    return result


def _timestamp_seconds(value: object) -> float:
    """Normalize the Pump API's millisecond timestamps without guessing dates."""
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("Invalid timestamp")
    return timestamp / 1000 if timestamp > 10_000_000_000 else timestamp


def _supported_price(value: object) -> float:
    """Round once to the replay contract's 1e-18 quote/token price scale."""
    try:
        amount = Decimal(str(value)).quantize(
            Decimal("0.000000000000000001"), rounding=ROUND_HALF_EVEN,
        )
    except (InvalidOperation, ValueError):
        raise MarketNormalizationError(
            "invalid_identity_or_units", "Quote price is outside supported precision",
        ) from None
    result = float(amount)
    if not math.isfinite(result) or result <= 0:
        raise MarketNormalizationError(
            "invalid_identity_or_units", "Quote price is outside supported precision",
        )
    return result


class MarketSource(Protocol):
    def collect(self, minimum_cap_usd: float, maximum_cap_usd: float,
                limit: int, priority: set[str] | None = None) -> list[MarketQuote]: ...


class PumpPublicSource:
    """Bounded direct reads from Pump's public index API."""

    def __init__(self, *, timeout_seconds: float = 8, max_workers: int = 4,
                 maximum_http_requests: int = 100,
                 maximum_elapsed_seconds: float = 60,
                 maximum_response_bytes: int = 12_000_000):
        if not 0 < timeout_seconds <= 60:
            raise ValueError("Market-data timeout must be between 0 and 60 seconds")
        if isinstance(max_workers, bool) or not 1 <= max_workers <= 4:
            raise ValueError("Market-data concurrency must be between 1 and 4")
        if (isinstance(maximum_http_requests, bool)
                or not 1 <= maximum_http_requests <= 100):
            raise ValueError("Market-data request bound must be between 1 and 100")
        if not 0 < maximum_elapsed_seconds <= 600:
            raise ValueError("Market-data elapsed bound must be at most ten minutes")
        if (isinstance(maximum_response_bytes, bool)
                or not 1_024 <= maximum_response_bytes <= 64_000_000):
            raise ValueError("Market-data response-byte bound is invalid")
        self.timeout_seconds = float(timeout_seconds)
        self.max_workers = max_workers
        self.maximum_http_requests = maximum_http_requests
        self.maximum_elapsed_seconds = float(maximum_elapsed_seconds)
        self.maximum_response_bytes = maximum_response_bytes
        self._response_byte_limit: int | None = None
        self.last_diagnostics: dict[str, Any] | None = None

    def _get_json(self, path: str) -> Any:
        allowed = path == "/sol-price" or path.startswith("/coins?") or (
            path.startswith("/coins/") and MINT.fullmatch(path.rsplit("/", 1)[-1]))
        if not allowed:
            raise ValueError("Refused non-market-data path")
        request = urllib.request.Request(PUMP_PUBLIC_BASE + path, headers={
            "Accept": "application/json", "Cache-Control": "no-cache",
            "User-Agent": "SolanaMarketWizard/0.2",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if response.geturl().split("/", 3)[2] != "frontend-api-v3.pump.fun":
                    raise MarketDataError("Unexpected market-data redirect")
                byte_limit = min(4_000_000, self._response_byte_limit or 4_000_000)
                body = response.read(byte_limit + 1)
                if len(body) > byte_limit:
                    raise MarketCollectionBoundError("Market-data response-byte bound was exhausted")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            raise MarketDataError(f"Market-data HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise MarketDataError("Market-data request failed") from exc

    def sol_usd_quote(self) -> tuple[float, float, float]:
        """Return price, provider source time and local receipt time."""
        received_at = time.time()
        raw = self._get_json("/sol-price")
        try:
            price = float(raw["solPrice"])
            source_at = float(raw["asOfTimestamp"]) / 1000
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketDataError("SOL/USD price response was invalid") from exc
        if (not isinstance(raw, dict) or raw.get("stale", True) or not math.isfinite(price) or price <= 0 or
                not math.isfinite(source_at) or not 0 <= received_at - source_at <= 60):
            raise MarketDataError("SOL/USD price is unavailable or stale")
        return price, source_at, received_at

    def sol_usd_price(self) -> float:
        """Return a fresh SOL/USD price from the bounded public Pump endpoint."""
        return self.sol_usd_quote()[0]

    def token_metadata(self, mints: set[str]) -> dict[str, dict[str, Any]]:
        """Resolve bounded display metadata directly from exact token mints."""
        selected = sorted(mint for mint in mints if MINT.fullmatch(mint))[:40]
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._get_json, "/coins/" + mint): mint for mint in selected}
            result: dict[str, dict[str, Any]] = {}
            for future in concurrent.futures.as_completed(futures):
                mint = futures[future]
                try:
                    coin = future.result()
                except MarketDataError:
                    continue
                if not isinstance(coin, dict) or coin.get("mint") != mint:
                    continue
                metadata = _pump_metadata(coin)
                symbol = _bounded_text(coin.get("symbol"), 80)
                if symbol:
                    metadata["symbol"] = symbol
                result[mint] = metadata
        return result

    def collect(self, minimum_cap_usd: float, maximum_cap_usd: float,
                limit: int, priority: set[str] | None = None) -> list[MarketQuote]:
        quotes, _diagnostics = self.collect_with_diagnostics(
            minimum_cap_usd, maximum_cap_usd, limit, priority,
        )
        return quotes

    def collect_with_diagnostics(self, minimum_cap_usd: float, maximum_cap_usd: float,
                                 limit: int, priority: set[str] | None = None,
                                 ) -> tuple[list[MarketQuote], dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("Market collection limit must be between 1 and 100")
        if (not math.isfinite(float(minimum_cap_usd))
                or not math.isfinite(float(maximum_cap_usd))
                or minimum_cap_usd < 0 or maximum_cap_usd <= minimum_cap_usd):
            raise ValueError("Market collection cap band is invalid")
        priority = set(priority or set())
        invalid_priority = sum(
            not isinstance(mint, str) or not MINT.fullmatch(mint) for mint in priority
        )
        priority = {
            mint for mint in priority if isinstance(mint, str) and MINT.fullmatch(mint)
        }
        diagnostics = _CollectionDiagnostics(
            maximum_http_requests=self.maximum_http_requests,
            maximum_elapsed_seconds=self.maximum_elapsed_seconds,
            maximum_response_bytes=self.maximum_response_bytes,
            maximum_concurrency=self.max_workers,
        )
        diagnostics.reject("invalid_identity_or_units", invalid_priority)
        now = time.time()
        quotes: list[MarketQuote] = []
        try:
            fx = diagnostics.request(self, "/sol-price", stage="fx")
            try:
                self._validate_fx(fx, now)
            except MarketNormalizationError as exc:
                diagnostics.reject(exc.category)
                raise MarketDataError(str(exc)) from None

            listings: dict[str, dict[str, Any]] = {}
            successful_listings = 0
            for sort in ("created_timestamp", "last_trade_timestamp"):
                try:
                    coins = diagnostics.request(
                        self,
                        f"/coins?offset=0&limit=70&sort={sort}&order=DESC&includeNsfw=false&complete=false",
                        stage="discovery",
                    )
                except MarketDataError:
                    continue
                if not isinstance(coins, list):
                    diagnostics.shape_failures += 1
                    diagnostics.reject("unsupported_shape")
                    continue
                successful_listings += 1
                diagnostics.stages["usable_discovery_responses"] += 1
                diagnostics.stages["listing_rows"] += len(coins)
                for coin in coins:
                    if not isinstance(coin, dict):
                        diagnostics.reject("unsupported_shape")
                        continue
                    mint = str(coin.get("mint", ""))
                    if not MINT.fullmatch(mint):
                        diagnostics.reject("invalid_identity_or_units")
                        continue
                    previous = listings.get(mint)
                    if previous is None or self._record_source_at(coin) >= self._record_source_at(previous):
                        listings[mint] = coin
            diagnostics.stages["unique_listings"] = len(listings)
            if not successful_listings:
                raise MarketDataError("All curve discovery listing requests failed")

            candidates: list[tuple[str, dict[str, Any], float]] = []
            for mint, coin in listings.items():
                rejection = self._discovery_rejection(
                    coin, now=now, minimum_cap_usd=minimum_cap_usd,
                    maximum_cap_usd=maximum_cap_usd, priority=mint in priority,
                )
                if rejection is not None:
                    diagnostics.reject(rejection)
                    continue
                candidates.append((mint, coin, self._record_source_at(coin)))
            missing_priority = priority - set(listings)
            diagnostics.reject("unavailable_route", len(missing_priority))
            diagnostics.stages["eligible_candidates"] = len(candidates)
            selected = sorted(
                candidates, key=lambda item: (item[0] not in priority, -item[2], item[0]),
            )[:limit]
            diagnostics.stages["selected_records"] = len(selected)
            for mint, raw, _source_at in selected:
                try:
                    quote = self._normalize(raw, fx, time.time())
                except MarketNormalizationError as exc:
                    diagnostics.reject(exc.category)
                    continue
                except (KeyError, TypeError, ValueError):
                    diagnostics.reject("unsupported_shape")
                    continue
                if quote.mint != mint:
                    diagnostics.reject("invalid_identity_or_units")
                    continue
                quotes.append(quote)
        except MarketDataError:
            self.last_diagnostics = diagnostics.finish(quotes)
            raise
        quotes = sorted(quotes, key=lambda quote: quote.mint)
        self.last_diagnostics = diagnostics.finish(quotes)
        return quotes, self.last_diagnostics

    @staticmethod
    def _record_source_at(coin: dict[str, Any]) -> float:
        try:
            return _timestamp_seconds(coin.get("updated_at"))
        except (TypeError, ValueError):
            return -1.0

    @staticmethod
    def _validate_fx(fx: Any, observed_at: float) -> None:
        if not isinstance(fx, dict):
            raise MarketNormalizationError("unsupported_shape", "SOL/USD response shape is unsupported")
        try:
            quote_usd = float(fx["solPrice"])
            # Pump's asOfTimestamp contract is milliseconds; unlike coin
            # timestamps it has no mixed seconds/milliseconds representation.
            fx_at = float(fx["asOfTimestamp"]) / 1000
        except (KeyError, TypeError, ValueError):
            raise MarketNormalizationError(
                "stale_or_unknown_source_time", "SOL/USD source time is unavailable",
            ) from None
        if (fx.get("stale", True) or not math.isfinite(quote_usd) or quote_usd <= 0
                or not math.isfinite(fx_at) or not 0 <= observed_at - fx_at <= 45):
            raise MarketNormalizationError(
                "stale_or_unknown_source_time", "SOL/USD price is unavailable or stale",
            )

    @staticmethod
    def _discovery_rejection(coin: dict[str, Any], *, now: float,
                             minimum_cap_usd: float, maximum_cap_usd: float,
                             priority: bool) -> str | None:
        mint = coin.get("mint")
        pool = coin.get("bonding_curve")
        if (not isinstance(mint, str) or not MINT.fullmatch(mint)
                or not isinstance(pool, str) or not MINT.fullmatch(pool)):
            return "invalid_identity_or_units"
        if (coin.get("complete") is not False or coin.get("program") != "pump"
                or coin.get("quote_mint") not in SOL_QUOTES):
            return "unavailable_route"
        try:
            cap = float(coin.get("market_cap_usd", coin.get("usd_market_cap")))
            updated = _timestamp_seconds(coin.get("updated_at"))
        except (TypeError, ValueError):
            return "stale_or_unknown_source_time"
        if not math.isfinite(cap) or cap <= 0:
            return "invalid_identity_or_units"
        if not 0 <= now - updated <= 45:
            return "stale_or_unknown_source_time"
        if not priority and not minimum_cap_usd * .5 <= cap <= maximum_cap_usd * 1.2:
            return "outside_requested_universe"
        return None

    @staticmethod
    def _normalize(coin: dict[str, Any], fx: dict[str, Any], observed_at: float) -> MarketQuote:
        mint = str(coin.get("mint", ""))
        if not MINT.fullmatch(mint):
            raise MarketNormalizationError("invalid_identity_or_units", "Invalid token mint")
        pool = coin.get("bonding_curve")
        if not isinstance(pool, str) or not MINT.fullmatch(pool):
            raise MarketNormalizationError("invalid_identity_or_units", "Invalid bonding-curve address")
        try:
            decimals = int(coin.get("base_decimals", 6))
            quote_decimals = int(coin.get("quote_decimals", 9))
        except (TypeError, ValueError):
            raise MarketNormalizationError(
                "invalid_identity_or_units", "Unsupported token decimals",
            ) from None
        if not 0 <= decimals <= 12 or quote_decimals != 9:
            raise MarketNormalizationError("invalid_identity_or_units", "Unsupported token decimals")
        quote_mint = coin.get("quote_mint")
        if quote_mint not in SOL_QUOTES or coin.get("program") != "pump":
            raise MarketNormalizationError("unavailable_route", "Unsupported quote asset or program")
        if coin.get("complete") is not False:
            raise MarketNormalizationError("unavailable_route", "Completed bonding curve is not an entry quote")
        try:
            source_at = _timestamp_seconds(coin["updated_at"])
            created_at = _timestamp_seconds(coin.get("created_timestamp", 0))
            fx_at = float(fx["asOfTimestamp"]) / 1000
            quote_usd = float(fx["solPrice"])
        except (KeyError, TypeError, ValueError):
            raise MarketNormalizationError(
                "stale_or_unknown_source_time", "Quote or FX source time is unavailable",
            ) from None
        try:
            y = float(coin.get("virtual_quote_reserves", coin.get("virtual_sol_reserves"))) / 1e9
            real_y = float(coin.get("real_quote_reserves", coin.get("real_sol_reserves"))) / 1e9
            x = float(coin["virtual_token_reserves"]) / 10 ** decimals
            real_x = float(coin["real_token_reserves"]) / 10 ** decimals
            raw_supply = coin["total_supply_str"] if coin.get("total_supply_str") is not None else coin["total_supply"]
            supply = float(raw_supply) / 10 ** decimals
            cap = float(coin.get("market_cap_usd", coin.get("usd_market_cap")))
        except (KeyError, TypeError, ValueError):
            raise MarketNormalizationError(
                "invalid_identity_or_units", "Quote reserves or valuation are invalid",
            ) from None
        numbers = (source_at, fx_at, quote_usd, x, y, real_x, real_y, supply, cap, observed_at, created_at)
        if (not all(math.isfinite(value) for value in numbers) or min(x, y, quote_usd, cap, supply) <= 0 or
                real_y < 0 or real_x < 0 or real_x > x):
            raise MarketNormalizationError(
                "invalid_identity_or_units", "Quote reserves or valuation are invalid",
            )
        if not 0 <= observed_at - source_at <= 45 or not 0 <= observed_at - fx_at <= 45:
            raise MarketNormalizationError(
                "stale_or_unknown_source_time", "Quote or FX source time is stale",
            )
        price_native = _supported_price(y / x)
        price_usd = _supported_price(price_native * quote_usd)
        return MarketQuote(mint=mint, symbol=str(coin.get("symbol") or mint[:8])[:80], observed_at=observed_at,
                           route="curve", pool=pool, market_cap_usd=cap,
                           price_usd=price_usd, price_native=price_native,
                           liquidity_usd=real_y * quote_usd, liquidity_native=real_y,
                           source="pump_public_index", quote_usd=quote_usd, base_reserve=x,
                           reserve_usd=y * quote_usd, real_token_reserve=real_x, supply=supply,
                           decimals=decimals, source_at=source_at, fx_at=fx_at, created_at=created_at,
                           metadata=_pump_metadata(coin))


class _MeteredReadOnlyRpc:
    """Count public RPC calls while retaining the underlying RPC allowlist."""

    def __init__(self, rpc: Any):
        self.rpc = rpc
        self.network = getattr(rpc, "network", None)
        self.calls = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self.calls += 1
        return self.rpc.call(method, params)


class CanonicalPumpSwapMarketSource:
    """Exact canonical PumpSwap state for a frozen, explicitly supplied mint set.

    This source performs no token discovery. It validates the current canonical
    pool through the same read-only route state used by trade preflight and turns
    its atomic reserves into an informational market observation. A later order
    must still repeat full route validation; this object is never an executable
    quote or signing authority.
    """

    MAX_MINTS = 20
    MAX_HTTP_REQUESTS = 42  # genesis + SOL/USD + two RPC snapshots per mint
    MAX_RESPONSE_BYTES = 64_000_000

    def __init__(self, *, rpc: Any | None = None, fx_source: Any | None = None,
                 clock=time.time, state_reader: Any | None = None):
        if rpc is None:
            from .transfers import TransferRpc

            class CanonicalReadRpc(TransferRpc):
                allowed_methods = frozenset({"getGenesisHash", "getMultipleAccounts"})

            rpc = CanonicalReadRpc("mainnet-beta")
        if getattr(rpc, "network", None) != "mainnet-beta":
            raise ValueError("Canonical PumpSwap observations require mainnet-beta")
        self.rpc = rpc
        self.fx_source = fx_source or PumpPublicSource()
        self.clock = clock
        if state_reader is None:
            from .pump_route import read_state

            state_reader = read_state
        self.state_reader = state_reader
        self._network_verified = False
        self.last_diagnostics: dict[str, Any] | None = None

    def collect(self, minimum_cap_usd: float, maximum_cap_usd: float,
                limit: int, priority: set[str] | None = None) -> list[MarketQuote]:
        if (isinstance(limit, bool) or not isinstance(limit, int)
                or not 1 <= limit <= self.MAX_MINTS):
            raise ValueError(f"Canonical PumpSwap limit must be 1–{self.MAX_MINTS}")
        if (not math.isfinite(float(minimum_cap_usd))
                or not math.isfinite(float(maximum_cap_usd))
                or minimum_cap_usd < 0 or maximum_cap_usd <= minimum_cap_usd):
            raise ValueError("Canonical PumpSwap market-cap band is invalid")
        requested = set(priority or set())
        invalid = sum(not isinstance(mint, str) or not MINT.fullmatch(mint)
                      for mint in requested)
        mints = sorted(
            mint for mint in requested
            if isinstance(mint, str) and MINT.fullmatch(mint)
        )
        if len(mints) > limit:
            raise ValueError("Frozen PumpSwap universe exceeds its source limit")
        started_at = float(self.clock())
        metered = _MeteredReadOnlyRpc(self.rpc)
        fx_attempts = 0
        rejections = {category: 0 for category in PROVIDER_REJECTION_CATEGORIES}
        rejections["invalid_identity_or_units"] = invalid
        reasons: list[str] = []
        quotes: list[MarketQuote] = []
        try:
            if not self._network_verified:
                from .transfers import GENESIS

                if metered.call("getGenesisHash", []) != GENESIS["mainnet-beta"]:
                    raise MarketDataError("Solana RPC network identity is not mainnet-beta")
                self._network_verified = True
            fx_attempts = 1
            if hasattr(self.fx_source, "sol_usd_quote"):
                quote_usd, fx_at, _fx_received_at = self.fx_source.sol_usd_quote()
            else:
                quote_usd = float(self.fx_source.sol_usd_price())
                fx_at = float(self.clock())
            fx_checked_at = float(self.clock())
            if (not math.isfinite(float(fx_at))
                    or not -2 <= fx_checked_at - float(fx_at) <= 120):
                rejections["stale_or_unknown_source_time"] += max(1, len(mints))
                reasons.append("stale_sol_usd_reference")
                self.last_diagnostics = self._diagnostics(
                    started_at, metered.calls + fx_attempts, [], rejections,
                    reasons, status="stale",
                )
                return []
            if not mints:
                rejections["empty_eligible_universe"] += 1
            for mint in mints:
                try:
                    state = self.state_reader(metered, None, mint, side="buy")
                    quote = self._quote(state, quote_usd=float(quote_usd),
                                        fx_at=float(fx_at), observed_at=float(self.clock()))
                except Exception as exc:
                    from .transfers import RpcError, TransferError

                    if isinstance(exc, RpcError) or (
                        isinstance(exc, TransferError)
                        and "connection unavailable" in str(exc).lower()
                    ):
                        raise MarketDataError("Canonical PumpSwap RPC read failed") from exc
                    if isinstance(exc, (TransferError, ValueError, KeyError, TypeError)):
                        rejections["unavailable_route"] += 1
                        if "canonical_route_unavailable" not in reasons:
                            reasons.append("canonical_route_unavailable")
                        continue
                    raise
                if not minimum_cap_usd <= quote.market_cap_usd <= maximum_cap_usd:
                    rejections["outside_requested_universe"] += 1
                    if "outside_market_cap_band" not in reasons:
                        reasons.append("outside_market_cap_band")
                    continue
                quotes.append(quote)
        except MarketDataError:
            rejections["request_failure"] += 1
            if "provider_request_failure" not in reasons:
                reasons.append("provider_request_failure")
            self.last_diagnostics = self._diagnostics(
                started_at, metered.calls + fx_attempts, [], rejections,
                reasons, status="failing",
            )
            raise
        status = "healthy" if quotes and not any(rejections.values()) else (
            "partial" if quotes else "empty"
        )
        self.last_diagnostics = self._diagnostics(
            started_at, metered.calls + fx_attempts, quotes, rejections, reasons,
            status=status,
        )
        return quotes

    @staticmethod
    def _quote(state: dict[str, Any], *, quote_usd: float, fx_at: float,
               observed_at: float) -> MarketQuote:
        from .pump_route import TOKEN_PROGRAM
        from .pump_swap_route import ROUTE

        if (state.get("route") != ROUTE or state.get("token_program") != TOKEN_PROGRAM
                or state.get("sender") is not None):
            raise ValueError("Only read-only canonical standard-SPL PumpSwap state is supported")
        decimals = state.get("decimals")
        reserves = state.get("amm_reserves")
        if (isinstance(decimals, bool) or not isinstance(decimals, int)
                or not 0 <= decimals <= 18 or not isinstance(reserves, dict)):
            raise ValueError("Canonical PumpSwap reserve units are unavailable")
        scale = 10 ** decimals
        base_atoms = int(reserves["base_reserves"])
        raw_quote_lamports = int(reserves["quote_reserves"])
        virtual_quote_lamports = int(reserves["virtual_quote_reserves"])
        effective_quote_lamports = int(reserves["effective_quote_reserves"])
        supply_atoms = int(reserves["token_total_supply"])
        if (min(base_atoms, raw_quote_lamports, effective_quote_lamports, supply_atoms) <= 0
                or raw_quote_lamports + virtual_quote_lamports != effective_quote_lamports
                or not all(math.isfinite(value) and value > 0
                           for value in (quote_usd, fx_at, observed_at))):
            raise ValueError("Canonical PumpSwap effective reserves are invalid")
        base_reserve = base_atoms / scale
        effective_quote = effective_quote_lamports / 1_000_000_000
        raw_quote = raw_quote_lamports / 1_000_000_000
        supply = supply_atoms / scale
        price_native = _supported_price(effective_quote / base_reserve)
        price_usd = _supported_price(price_native * quote_usd)
        market_cap = supply * price_usd
        values = (base_reserve, effective_quote, raw_quote, supply, market_cap)
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("Canonical PumpSwap quote is outside supported units")
        return MarketQuote(
            mint=str(state["mint"]), symbol=str(state["mint"])[:8],
            observed_at=observed_at, route="amm", pool=str(state["pool"]),
            market_cap_usd=market_cap, price_usd=price_usd,
            price_native=price_native, liquidity_usd=raw_quote * quote_usd,
            liquidity_native=raw_quote, source="canonical_pumpswap_rpc",
            quote_usd=quote_usd, base_reserve=base_reserve,
            reserve_usd=effective_quote * quote_usd,
            real_token_reserve=base_reserve, supply=supply, decimals=decimals,
            source_at=observed_at, fx_at=fx_at,
            metadata={
                "route_readiness": "canonical_pool_read_only",
                "execution_promise": False,
                "source_slot": int(state["slot"]),
                "source_time_basis": "local_confirmed_rpc_receipt",
                "base_reserves_atoms": base_atoms,
                "quote_reserves_lamports": raw_quote_lamports,
                "virtual_quote_reserves_lamports": virtual_quote_lamports,
                "effective_quote_reserves_lamports": effective_quote_lamports,
            },
        )

    def _diagnostics(self, started_at: float, http_requests: int,
                     quotes: list[MarketQuote], rejections: dict[str, int],
                     reasons: list[str], *, status: str) -> dict[str, Any]:
        finished_at = float(self.clock())
        source_times = [quote.source_at for quote in quotes]
        return {
            "schema": PROVIDER_DIAGNOSTICS_SCHEMA,
            "provider": type(self).__name__,
            "started_at": started_at,
            "finished_at": max(started_at, finished_at),
            "status": status,
            "bounds": {
                "maximum_http_requests": self.MAX_HTTP_REQUESTS,
                "maximum_elapsed_seconds": 60,
                "maximum_response_bytes": self.MAX_RESPONSE_BYTES,
                "maximum_concurrency": 1,
            },
            "stages": {
                "discovery_requests": 0,
                "listing_rows": 0,
                "unique_listings": 0,
                "eligible_candidates": len(quotes),
                "selected_records": len(quotes),
                "selected_detail_requests": max(0, http_requests - 1),
                "returned_details": len(quotes),
                "normalized_quotes": len(quotes),
                "http_requests": http_requests,
                # TransferRpc enforces per-response bounds but does not expose
                # wire byte counts. Preserve unknown instead of reporting zero.
                "response_bytes": None,
            },
            "rejections": {key: value for key, value in sorted(rejections.items()) if value},
            "provenance": {
                "provider_hosts": ["configured-solana-rpc", "frontend-api-v3.pump.fun"],
                "endpoint_classes": ["confirmed_account_snapshot", "sol_usd_reference"],
                "normalized_routes": ["amm"] if quotes else [],
                "source_timestamp_field": "local confirmed RPC receipt",
                "receipt_timestamp_field": "observed_at",
                "response_byte_basis": "unknown; transport enforces bounded responses",
            },
            "readiness": {
                "ready": bool(quotes),
                "routes": ["amm"] if quotes else [],
                "reasons": reasons,
                "first_received_at": min((quote.observed_at for quote in quotes), default=None),
                "earliest_source_at": min(source_times, default=None),
                "latest_source_at": max(source_times, default=None),
                "maximum_source_age_seconds": max(
                    (max(0.0, finished_at - value) for value in source_times), default=None,
                ),
            },
        }


class PumpSwapPairSource:
    """Read PumpSwap pair snapshots for already-held mints after graduation.

    This source deliberately does not discover AMM entry candidates. It supplies
    a local price/route history for positions the user already recorded, while
    route construction remains a later execution-layer responsibility.
    """

    def __init__(self, *, timeout_seconds: float = 8):
        self.timeout_seconds = timeout_seconds

    def collect(self, minimum_cap_usd: float, maximum_cap_usd: float,
                limit: int, priority: set[str] | None = None) -> list[MarketQuote]:
        if not 1 <= limit <= 100:
            raise ValueError("Market collection limit must be between 1 and 100")
        mints = sorted(mint for mint in priority or set() if MINT.fullmatch(mint))[:30]
        if not mints:
            return []
        path = "/tokens/v1/solana/" + urllib.parse.quote(",".join(mints), safe=",")
        raw_pairs = self._get_json(path)
        if not isinstance(raw_pairs, list):
            raise MarketDataError("Unexpected PumpSwap pair response")
        now = time.time()
        selected: dict[str, MarketQuote] = {}
        for pair in raw_pairs:
            try:
                quote = self._normalize_pair(pair, now)
            except (KeyError, TypeError, ValueError):
                continue
            if quote.mint not in mints:
                continue
            existing = selected.get(quote.mint)
            if existing is None or quote.liquidity_usd > existing.liquidity_usd:
                selected[quote.mint] = quote
        return sorted(selected.values(), key=lambda quote: quote.mint)[:limit]

    def token_metadata(self, mints: set[str]) -> dict[str, dict[str, Any]]:
        """Resolve display metadata for exact PumpSwap base-token mints."""
        selected = sorted(mint for mint in mints if MINT.fullmatch(mint))[:30]
        if not selected:
            return {}
        path = "/tokens/v1/solana/" + urllib.parse.quote(",".join(selected), safe=",")
        raw_pairs = self._get_json(path)
        if not isinstance(raw_pairs, list):
            raise MarketDataError("Unexpected PumpSwap pair response")
        result: dict[str, tuple[float, dict[str, Any]]] = {}
        for pair in raw_pairs:
            if not isinstance(pair, dict) or pair.get("chainId") != "solana" or pair.get("dexId") != "pumpswap":
                continue
            base = pair.get("baseToken")
            if not isinstance(base, dict) or base.get("address") not in selected:
                continue
            mint = str(base["address"])
            metadata = _pair_metadata(pair, base)
            symbol = _bounded_text(base.get("symbol"), 80)
            if symbol:
                metadata["symbol"] = symbol
            try:
                liquidity = float((pair.get("liquidity") or {}).get("usd", 0))
            except (TypeError, ValueError):
                liquidity = 0
            previous = result.get(mint)
            if previous is None or liquidity > previous[0]:
                result[mint] = (liquidity, metadata)
        return {mint: metadata for mint, (_, metadata) in result.items()}

    def _get_json(self, path: str) -> Any:
        prefix = "/tokens/v1/solana/"
        encoded = path[len(prefix):] if path.startswith(prefix) else ""
        if not encoded or any(not MINT.fullmatch(mint) for mint in urllib.parse.unquote(encoded).split(",")):
            raise ValueError("Refused non-market-data pair path")
        request = urllib.request.Request(DEXSCREENER_BASE + path, headers={
            "Accept": "application/json", "Cache-Control": "no-cache",
            "User-Agent": "SolanaMarketWizard/0.2",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if urllib.parse.urlsplit(response.geturl()).hostname != "api.dexscreener.com":
                    raise MarketDataError("Unexpected pair-data redirect")
                body = response.read(4_000_001)
                if len(body) > 4_000_000:
                    raise MarketDataError("Oversized pair-data response")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            raise MarketDataError(f"Pair-data HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise MarketDataError("Pair-data request failed") from exc

    @staticmethod
    def _normalize_pair(pair: Any, observed_at: float) -> MarketQuote:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana" or pair.get("dexId") != "pumpswap":
            raise ValueError("Unsupported pair")
        base = pair.get("baseToken")
        quote = pair.get("quoteToken")
        liquidity = pair.get("liquidity")
        if not isinstance(base, dict) or not isinstance(quote, dict) or not isinstance(liquidity, dict):
            raise ValueError("Incomplete pair")
        mint = base.get("address")
        pool = pair.get("pairAddress")
        if not isinstance(mint, str) or not MINT.fullmatch(mint) or not isinstance(pool, str) or not MINT.fullmatch(pool):
            raise ValueError("Invalid pair address")
        if quote.get("address") not in SOL_QUOTES:
            raise ValueError("Unsupported PumpSwap quote asset")
        price_usd = _supported_price(pair["priceUsd"])
        price_native = _supported_price(pair["priceNative"])
        liquidity_usd = float(liquidity["usd"])
        base_reserve = float(liquidity["base"])
        quote_reserve = float(liquidity["quote"])
        raw_market_cap = pair.get("marketCap")
        market_cap = float(raw_market_cap if raw_market_cap is not None else pair.get("fdv"))
        values = (price_usd, price_native, liquidity_usd, base_reserve, quote_reserve, market_cap)
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("Invalid pair valuation or liquidity")
        quote_usd = price_usd / price_native
        if not math.isfinite(quote_usd) or quote_usd <= 0:
            raise ValueError("Invalid pair quote price")
        return MarketQuote(
            mint=mint, symbol=str(base.get("symbol") or mint[:8])[:80], observed_at=observed_at,
            route="amm", pool=pool, market_cap_usd=market_cap, price_usd=price_usd,
            price_native=price_native, liquidity_usd=liquidity_usd, liquidity_native=quote_reserve,
            source="dexscreener_pumpswap_pair", quote_usd=quote_usd, base_reserve=base_reserve,
            reserve_usd=base_reserve * price_usd, real_token_reserve=base_reserve,
            supply=market_cap / price_usd, decimals=0, source_at=observed_at, fx_at=observed_at,
            metadata=_pair_metadata(pair, base),
        )


class JupiterPublicTokenSource:
    """Batch identity and market-quality context for exact held-token mints."""

    def __init__(self, *, timeout_seconds: float = 8):
        self.timeout_seconds = timeout_seconds

    def _get_json(self, mints: list[str]) -> Any:
        if not 1 <= len(mints) <= 40 or any(not MINT.fullmatch(mint) for mint in mints):
            raise ValueError("Token profile lookup requires 1–40 valid mints")
        query = urllib.parse.urlencode({"query": ",".join(mints)})
        request = urllib.request.Request(f"{JUPITER_LITE_BASE}/tokens/v2/search?{query}", headers={
            "Accept": "application/json", "Cache-Control": "no-cache",
            "User-Agent": "SolanaMarketWizard/0.2",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if urllib.parse.urlsplit(response.geturl()).hostname != "lite-api.jup.ag":
                    raise MarketDataError("Unexpected token-profile redirect")
                body = response.read(4_000_001)
                if len(body) > 4_000_000:
                    raise MarketDataError("Oversized token-profile response")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            raise MarketDataError(f"Token-profile HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise MarketDataError("Token-profile request failed") from exc

    @staticmethod
    def _number(value: object, *, positive: bool = False) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        result = float(value)
        if not math.isfinite(result) or (positive and result <= 0):
            return None
        return result

    @staticmethod
    def _created_at(value: object) -> float | None:
        if not isinstance(value, str) or len(value) > 80:
            return None
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
        return result if math.isfinite(result) and result > 0 else None

    def token_profiles(self, mints: set[str]) -> dict[str, dict[str, Any]]:
        selected = sorted(mint for mint in mints if MINT.fullmatch(mint))[:40]
        if not selected:
            return {}
        raw = self._get_json(selected)
        if not isinstance(raw, list) or len(raw) > 100:
            raise MarketDataError("Unexpected token-profile response")
        result: dict[str, dict[str, Any]] = {}
        for row in raw:
            if not isinstance(row, dict) or row.get("id") not in selected:
                continue
            mint = str(row["id"])
            metadata: dict[str, Any] = {}
            name = _bounded_text(row.get("name"), 120)
            symbol = _bounded_text(row.get("symbol"), 80)
            if name:
                metadata["name"] = name
            if symbol:
                metadata["symbol"] = symbol
            image = _https_url(row.get("icon"))
            website = _https_url(row.get("website"))
            if image:
                metadata["image_url"] = image
            if website:
                metadata["website"] = website
            launchpad = _bounded_text(row.get("launchpad"), 80)
            metadata["source"] = "Pump.fun" if launchpad.lower() == "pump.fun" else "Jupiter Tokens"
            if isinstance(row.get("isVerified"), bool):
                metadata["verified"] = row["isVerified"]
            stats = row.get("stats24h") if isinstance(row.get("stats24h"), dict) else {}
            audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
            block = row.get("priceBlockId")
            result[mint] = {
                "metadata": metadata,
                "price_usd": self._number(row.get("usdPrice"), positive=True),
                "price_block_id": int(block) if isinstance(block, int) and not isinstance(block, bool) and block > 0 else None,
                "liquidity_usd": self._number(row.get("liquidity")),
                "market_cap_usd": self._number(row.get("mcap")),
                "price_change_24h": self._number(stats.get("priceChange")),
                "volume_change_24h": self._number(stats.get("volumeChange")),
                "holder_count": int(row["holderCount"]) if isinstance(row.get("holderCount"), int) and row["holderCount"] >= 0 else None,
                "organic_score": self._number(row.get("organicScore")),
                "organic_score_label": _bounded_text(row.get("organicScoreLabel"), 20).lower() or None,
                "suspicious": audit.get("isSus") is True,
                "top_holders_percent": self._number(audit.get("topHoldersPercentage")),
                "created_at": self._created_at(row.get("createdAt")) or self._created_at((row.get("firstPool") or {}).get("createdAt") if isinstance(row.get("firstPool"), dict) else None),
            }
        return result


class CompositePumpMarketSource:
    """Curve discovery plus held-position PumpSwap continuity in one local feed."""

    def __init__(self, curve_source: MarketSource | None = None, amm_source: MarketSource | None = None,
                 profile_source=None):
        custom_sources = curve_source is not None or amm_source is not None
        self.curve_source = curve_source or PumpPublicSource()
        self.amm_source = amm_source or PumpSwapPairSource()
        self.profile_source = profile_source if profile_source is not None else (None if custom_sources else JupiterPublicTokenSource())
        self._profile_cache: dict[str, dict[str, Any]] = {}
        self._profile_loaded: set[str] = set()
        self.last_diagnostics: dict[str, Any] | None = None

    def collect(self, minimum_cap_usd: float, maximum_cap_usd: float,
                limit: int, priority: set[str] | None = None) -> list[MarketQuote]:
        curve_error: MarketDataError | None = None
        amm_error: MarketDataError | None = None
        try:
            curve_quotes = self.curve_source.collect(minimum_cap_usd, maximum_cap_usd, limit, priority)
        except MarketDataError as exc:
            curve_quotes = []
            curve_error = exc
        try:
            amm_quotes = self.amm_source.collect(minimum_cap_usd, maximum_cap_usd, limit, priority)
        except MarketDataError as exc:
            amm_quotes = []
            amm_error = exc
        curve_diagnostics = getattr(self.curve_source, "last_diagnostics", None)
        merged = {quote.mint: quote for quote in amm_quotes}
        # A still-live curve is the authoritative entry observation. The AMM
        # fallback is used only when the curve collector has no usable quote.
        merged.update({quote.mint: quote for quote in curve_quotes})
        quotes = sorted(merged.values(), key=lambda quote: quote.mint)
        self.last_diagnostics = self._combined_diagnostics(
            curve_diagnostics, quotes=quotes, curve_count=len(curve_quotes),
            amm_count=len(amm_quotes), curve_error=curve_error, amm_error=amm_error,
        )
        if not curve_quotes and not amm_quotes and curve_error is not None:
            raise curve_error
        if not curve_quotes and not amm_quotes and amm_error is not None:
            raise amm_error
        return quotes

    @staticmethod
    def _combined_diagnostics(curve_diagnostics: Any, *, quotes: list[MarketQuote],
                              curve_count: int, amm_count: int,
                              curve_error: Exception | None,
                              amm_error: Exception | None) -> dict[str, Any] | None:
        if not isinstance(curve_diagnostics, dict):
            return None
        # The diagnostic object is JSON-compatible by contract. Clone it so the
        # component source cannot mutate a recorder payload after collection.
        result = json.loads(json.dumps(curve_diagnostics, allow_nan=False))
        result["provider"] = "CompositePumpMarketSource"
        result["components"] = {
            "curve": {
                "provider": "PumpPublicSource",
                "status": curve_diagnostics.get("status"),
                "result_count": curve_count,
                "error": type(curve_error).__name__ if curve_error is not None else None,
                "source_time_basis": "provider-updated-at",
            },
            "amm": {
                "provider": "PumpSwapPairSource",
                "status": "failing" if amm_error is not None else ("healthy" if amm_count else "empty"),
                "result_count": amm_count,
                "error": type(amm_error).__name__ if amm_error is not None else None,
                "source_time_basis": "local-receipt-inference",
            },
        }
        result["stages"]["normalized_quotes"] = len(quotes)
        readiness = result["readiness"]
        readiness["ready"] = bool(quotes)
        readiness["routes"] = sorted({quote.route for quote in quotes})
        readiness["first_received_at"] = min(
            (quote.observed_at for quote in quotes), default=None,
        )
        known_sources = [quote.source_at for quote in quotes if quote.source_at > 0]
        readiness["earliest_source_at"] = min(known_sources, default=None)
        readiness["latest_source_at"] = max(known_sources, default=None)
        readiness["maximum_source_age_seconds"] = max(
            (max(0.0, float(result["finished_at"]) - value) for value in known_sources),
            default=None,
        )
        if curve_error is not None or amm_error is not None:
            result["status"] = "partial" if quotes else "failing"
            reasons = list(readiness.get("reasons") or [])
            if "component_request_failure" not in reasons:
                reasons.append("component_request_failure")
            readiness["reasons"] = reasons
        return result

    def sol_usd_price(self) -> float:
        """Use the bounded Pump SOL/USD reference shared by curve quotes."""
        return self.curve_source.sol_usd_price()  # type: ignore[attr-defined]

    def token_metadata(self, mints: set[str]) -> dict[str, dict[str, Any]]:
        """Resolve exact-mint display metadata, preferring Pump field by field."""
        result: dict[str, dict[str, Any]] = {}
        try:
            result.update(self.curve_source.token_metadata(mints))  # type: ignore[attr-defined]
        except (AttributeError, MarketDataError):
            pass
        # Pump records can be valid but incomplete (commonly a name without a
        # still-reachable image or project site). Ask the pair index for every
        # selected mint and fill only those gaps; Pump remains authoritative
        # wherever both sources provide a field.
        try:
            fallback = self.amm_source.token_metadata(mints)  # type: ignore[attr-defined]
        except (AttributeError, MarketDataError):
            fallback = {}
        profiles = self.token_profiles(mints)
        combined = {}
        for mint in set(result) | set(fallback) | set(profiles):
            metadata = {**profiles.get(mint, {}).get("metadata", {}), **fallback.get(mint, {}), **result.get(mint, {})}
            # Keep exact-mint alternatives when the preferred artwork is gone.
            images = list(dict.fromkeys(url for raw in (
                metadata.get("image_url"), fallback.get(mint, {}).get("image_url"),
                profiles.get(mint, {}).get("metadata", {}).get("image_url"),
            ) if (url := _https_url(raw))))
            if len(images) > 1:
                metadata["image_urls"] = images[:3]
            combined[mint] = metadata
        return combined

    def token_profiles(self, mints: set[str]) -> dict[str, dict[str, Any]]:
        missing = mints - self._profile_loaded
        if missing and self.profile_source is not None:
            try:
                self._profile_cache.update(self.profile_source.token_profiles(missing))
            except (AttributeError, MarketDataError):
                pass
            self._profile_loaded.update(missing)
        return {mint: self._profile_cache[mint] for mint in mints if mint in self._profile_cache}
