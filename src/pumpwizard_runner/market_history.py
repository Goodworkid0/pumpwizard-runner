"""Automatic, bounded imports of public historical market candles.

The public source needs no account, credential, or browser configuration.
Imported candles are display and research records; they never become route,
quote, risk, or execution input.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from .library import StrategyLibrary


MINT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SOURCE = "geckoterminal-public"
RESOLUTION_SECONDS = 4 * 3600
PAGE_LIMIT = 1000
PUBLIC_API = "https://api.geckoterminal.com/api/v2"


class MarketHistoryError(ValueError):
    pass


class HistorySource(Protocol):
    label: str

    def fetch(self, mint: str, before: int, limit: int = PAGE_LIMIT) -> list[dict[str, object]]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MarketHistoryError("Public market history redirected unexpectedly")


class GeckoTerminalHistorySource:
    """Keyless 4-hour USD candles from a token's leading tracked pool."""

    label = "Public market history"

    def __init__(self, *, minimum_interval_seconds: float = 6.1):
        if (isinstance(minimum_interval_seconds, bool) or
                not isinstance(minimum_interval_seconds, (int, float)) or
                not math.isfinite(minimum_interval_seconds) or minimum_interval_seconds < 0):
            raise MarketHistoryError("Public history request interval is invalid")
        self._minimum_interval = float(minimum_interval_seconds)
        self._request_lock = threading.Lock()
        self._last_request_at = 0.0
        self._pools: dict[str, list[dict]] = {}
        self._discovered_at: dict[str, float] = {}

    def _request(self, path: str, query: dict[str, object]) -> object:
        url = f"{PUBLIC_API}{path}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={
            "Accept": "application/json;version=20230203",
            "Cache-Control": "no-cache",
            "User-Agent": "PumpWizard-Runner/0.2",
        })
        try:
            with self._request_lock:
                remaining = self._minimum_interval - (time.monotonic() - self._last_request_at)
                if remaining > 0:
                    time.sleep(remaining)
                try:
                    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=15) as response:
                        body = response.read(4_000_001)
                finally:
                    self._last_request_at = time.monotonic()
            if len(body) > 4_000_000:
                raise MarketHistoryError("Public market history response was too large")
            return json.loads(body)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise MarketHistoryError("No public price history was found for this token") from None
            if exc.code == 429:
                raise MarketHistoryError("Public price history is busy; try again shortly") from None
            raise MarketHistoryError(f"Public price history failed with HTTP {exc.code}") from None
        except MarketHistoryError:
            raise
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            raise MarketHistoryError("Public price history is temporarily unavailable") from None

    @staticmethod
    def _relationship_mint(row: object, side: str) -> str:
        try:
            identifier = row["relationships"][side]["data"]["id"]  # type: ignore[index]
        except (KeyError, TypeError):
            return ""
        prefix = "solana_"
        return identifier[len(prefix):] if isinstance(identifier, str) and identifier.startswith(prefix) else ""

    def discover(self, mint: str) -> list[dict]:
        if not MINT.fullmatch(mint):
            raise MarketHistoryError("Historical prices require a valid Solana mint")
        if mint in self._pools and time.time()-self._discovered_at.get(mint, 0)<300:
            return self._pools[mint]
        result = self._request(f"/networks/solana/tokens/{urllib.parse.quote(mint, safe='')}/pools", {"page": 1})
        rows = result.get('data') if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise MarketHistoryError('Public market history returned invalid pool data')
        pools = []
        for row in rows[:20]:
            if not isinstance(row, dict) or mint not in (self._relationship_mint(row, 'base_token'), self._relationship_mint(row, 'quote_token')):
                continue
            attrs = row.get('attributes') or {}
            address = attrs.get('address')
            dex = ((row.get('relationships') or {}).get('dex') or {}).get('data') or {}
            if isinstance(address, str) and MINT.fullmatch(address) and address not in [p['address'] for p in pools]:
                pools.append(dict(address=address, dex=str(dex.get('id', 'unknown'))[:80], created_at=str(attrs.get('pool_created_at') or '')[:80]))
        if not pools:
            raise MarketHistoryError('No tracked liquidity pool has public price history for this token')
        self._pools[mint] = pools[:5]
        self._discovered_at[mint] = time.time()
        return self._pools[mint]

    def _pool_for(self, mint: str) -> str:
        return self.discover(mint)[0]['address']

    def fetch(self, mint: str, before: int, limit: int = PAGE_LIMIT) -> list[dict[str, object]]:
        if not MINT.fullmatch(mint):
            raise MarketHistoryError("Historical prices require a valid Solana mint")
        if type(before) is not int or before <= 0 or not 1 <= limit <= PAGE_LIMIT:
            raise MarketHistoryError("Historical price cursor or page size is invalid")
        return self.fetch_pool(mint, self._pool_for(mint), before, limit)

    def fetch_pool(self, mint: str, pool: str, before: int, limit: int = PAGE_LIMIT):
        if not MINT.fullmatch(mint) or not MINT.fullmatch(pool) or type(before) is not int or before <= 0 or not 1 <= limit <= PAGE_LIMIT:
            raise MarketHistoryError("Invalid pool history request")
        result = self._request(
            f"/networks/solana/pools/{urllib.parse.quote(pool, safe='')}/ohlcv/hour",
            {
                "aggregate": 4,
                "before_timestamp": before,
                "limit": limit,
                "currency": "usd",
                "include_empty_intervals": "false",
                "token": mint,
            },
        )
        try:
            rows = result["data"]["attributes"]["ohlcv_list"]  # type: ignore[index]
            base_address = result["meta"]["base"]["address"]  # type: ignore[index]
        except (KeyError, TypeError):
            raise MarketHistoryError("Public market history returned an incomplete candle response") from None
        if base_address != mint:
            raise MarketHistoryError("Public price history did not match the selected token")
        if not isinstance(rows, list) or len(rows) > limit:
            raise MarketHistoryError("Public market history returned an invalid candle page")
        candles: list[dict[str, object]] = []
        seen: set[int] = set()
        for raw in rows:
            if not isinstance(raw, list) or len(raw) != 6:
                raise MarketHistoryError("Public market history returned an invalid candle")
            timestamp, opened, high, low, closed, volume = raw
            values = (opened, high, low, closed, volume)
            if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or
                    int(timestamp) != timestamp or not 0 < timestamp < before or
                    any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                        not math.isfinite(value) or value < 0 for value in values)):
                raise MarketHistoryError("Public market history returned an invalid candle value")
            timestamp = int(timestamp)
            opened, high, low, closed, volume = (float(value) for value in values)
            if min(opened, high, low, closed) <= 0 or high < max(opened, closed) or low > min(opened, closed):
                raise MarketHistoryError("Public market history returned inconsistent OHLC values")
            if timestamp in seen:
                continue
            seen.add(timestamp)
            candles.append({
                "opened_at": timestamp,
                "open_usd": str(opened),
                "high_usd": str(high),
                "low_usd": str(low),
                "close_usd": str(closed),
                "volume_usd": str(volume),
            })
        return sorted(candles, key=lambda row: int(row["opened_at"]), reverse=True)


class MarketHistoryService:
    """Always-available keyless source plus resumable one-page imports."""

    def __init__(self, source: HistorySource | None = None):
        self._source = source or GeckoTerminalHistorySource()
        self._lock = threading.Lock()

    def import_page(self, library: StrategyLibrary, mint: str, *, now: float | None = None) -> dict[str, object]:
        if not MINT.fullmatch(mint):
            raise MarketHistoryError("Historical prices require a valid Solana mint")
        current = time.time() if now is None else now
        if not math.isfinite(current) or current <= 0:
            raise MarketHistoryError("Historical price clock is invalid")
        with self._lock:
            source = self._source
            if hasattr(source, "discover") and hasattr(source, "fetch_pool"):
                return self._import_pool_page(library, mint, current)
            job = library.market_history_job(mint, SOURCE, RESOLUTION_SECONDS)
            if job and job["status"] == "complete":
                return job
            before = int(job["before_at"]) if job and job.get("before_at") else int(current) + 1
            library.update_market_history_job(
                mint, SOURCE, RESOLUTION_SECONDS, status="running", before_at=before,
                error="", coverage=str(job.get("coverage") or "") if job else "", updated_at=current,
            )
            try:
                candles = source.fetch(mint, before, PAGE_LIMIT)
            except MarketHistoryError as exc:
                library.update_market_history_job(
                    mint, SOURCE, RESOLUTION_SECONDS, status="error", before_at=before,
                    error=str(exc), coverage=str(job.get("coverage") or "") if job else "", updated_at=current,
                )
                raise
            observed = library.coin_history(mint, 1)
            created_at = float(observed[0].get("created_at") or 0) if observed else 0
            earliest = min((int(row["opened_at"]) for row in candles), default=before)
            complete = not candles or len(candles) < PAGE_LIMIT or (created_at > 0 and earliest <= created_at)
            coverage = (
                "Available public history has been loaded. Coverage can begin after launch or change when the active pool changes."
                if complete else
                "Older public candles are loading automatically while this token page remains open."
            )
            library.record_market_history_page(
                mint, SOURCE, RESOLUTION_SECONDS, candles,
                status="complete" if complete else "ready", before_at=earliest,
                error="", coverage=coverage, updated_at=current,
            )
            result = library.market_history_job(mint, SOURCE, RESOLUTION_SECONDS)
            if result is None:
                raise MarketHistoryError("Historical price state was not retained")
            return result

    def _import_pool_page(self, library, mint, now):
        db = library._connection
        db.execute('CREATE TABLE IF NOT EXISTS history_pools (mint TEXT, address TEXT, metadata TEXT NOT NULL, PRIMARY KEY(mint,address))')
        try:
            pools = self._source.discover(mint)
            with db:
                for pool in pools:
                    db.execute('INSERT OR REPLACE INTO history_pools VALUES (?,?,?)', (mint, pool['address'], json.dumps(pool)))
            jobs = [(pool, library.market_history_job(mint, SOURCE+':'+pool['address'], RESOLUTION_SECONDS)) for pool in pools]
            # Finish one page per request. Pool cursors survive process restarts independently.
            pending = [(pool, job) for pool, job in jobs if not job or job['status'] != 'complete' or now-job['updated_at']>=300]
            if pending:
                pool, job = pending[0]
                name = SOURCE+':'+pool['address']
                before = int(job['before_at']) if job and job['status'] != 'complete' else int(now)+1
                candles = self._source.fetch_pool(mint, pool['address'], before, PAGE_LIMIT)
                earliest = min((int(c['opened_at']) for c in candles), default=before)
                library.record_market_history_page(mint, name, RESOLUTION_SECONDS, candles,
                    status='complete' if len(candles)<PAGE_LIMIT else 'ready', before_at=earliest,
                    error='', coverage='Provider pool candles only; omitted intervals are not zero volume. Launch/curve coverage and canonical onchain history remain unverified.', updated_at=now)
            jobs = [library.market_history_job(mint, SOURCE+':'+pool['address'], RESOLUTION_SECONDS) for pool in pools]
            done = all(job and job['status']=='complete' for job in jobs)
            library.update_market_history_job(mint, SOURCE, RESOLUTION_SECONDS, status='complete' if done else 'ready', before_at=int(now)+1,
                error='', coverage=f'{len(pools)} discovered pools tracked separately (maximum 5). Pool discovery covers one provider page; it is not complete lifecycle history.', updated_at=now)
            with db:
                db.execute('UPDATE market_history_jobs SET row_count=?,earliest_at=?,latest_at=? WHERE mint=? AND source=? AND resolution_seconds=?',
                    (sum(job['row_count'] for job in jobs if job), min((job['earliest_at'] for job in jobs if job and job['earliest_at']), default=None), max((job['latest_at'] for job in jobs if job and job['latest_at']), default=None), mint, SOURCE, RESOLUTION_SECONDS))
        except MarketHistoryError as exc:
            library.update_market_history_job(mint, SOURCE, RESOLUTION_SECONDS, status='error', before_at=int(now)+1,
                error=str(exc), coverage='Pool history import is incomplete; retry resumes the saved pool cursors.', updated_at=now)
            raise
        return library.market_history_job(mint, SOURCE, RESOLUTION_SECONDS)


def pool_histories(library, mint):
    db = library._connection
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='history_pools'").fetchone():
        return []
    result = []
    for row in db.execute('SELECT metadata FROM history_pools WHERE mint=? ORDER BY address LIMIT 5', (mint,)):
        pool = json.loads(row[0])
        source = SOURCE+':'+pool['address']
        candles = library.market_history_candles(mint, 500, source=source)
        ordered = list(reversed(candles))
        gaps = [dict(after=a['opened_at'], before=b['opened_at']) for a,b in zip(ordered, ordered[1:]) if b['opened_at']-a['opened_at']>RESOLUTION_SECONDS]
        result.append(dict(**pool, source=source, candles=candles, gaps=gaps, job=library.market_history_job(mint, source, RESOLUTION_SECONDS)))
    return result
