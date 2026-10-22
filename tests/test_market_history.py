import json

import pytest

from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.market_history import (
    GeckoTerminalHistorySource,
    MarketHistoryError,
    MarketHistoryService,
    RESOLUTION_SECONDS,
    SOURCE,
)
from pumpwizard_runner.web_api import command, payload


MINT = "So11111111111111111111111111111111111111112"


class FakeSource:
    label = "Test history"

    def __init__(self):
        self.calls = []

    def fetch(self, mint, before, limit=1000):
        self.calls.append((mint, before, limit))
        count = 1000 if len(self.calls) == 1 else 2
        return [{
            "opened_at": before - (index + 1) * RESOLUTION_SECONDS,
            "open_usd": "1", "high_usd": "2", "low_usd": ".5",
            "close_usd": "1.5", "volume_usd": "100",
        } for index in range(count)]


def test_resumable_history_service_retains_attributed_pages_and_coverage(tmp_path):
    source = FakeSource()
    service = MarketHistoryService(source)
    library = StrategyLibrary(tmp_path)
    now = 1_800_000_000.0
    try:
        library.record_market_watch_quote(MarketQuote(
            mint=MINT, symbol="HIST", observed_at=now, route="curve", pool="pool",
            market_cap_usd=10_000, price_usd=1.6, price_native=.01,
            liquidity_usd=5_000, liquidity_native=30, created_at=0,
        ))
        first = service.import_page(library, MINT, now=now)
        assert first["status"] == "ready" and first["row_count"] == 1000
        first_cursor = first["before_at"]
        second = service.import_page(library, MINT, now=now + 1)
        assert second["status"] == "complete" and second["row_count"] == 1002
        assert source.calls[1][1] == first_cursor
        latest = library.market_history_candles(MINT, 2)
        assert len(latest) == 2 and all(row["source"] == SOURCE for row in latest)
        older = library.market_history_candles(MINT, 2, before=latest[-1]["opened_at"])
        assert older and older[0]["opened_at"] < latest[-1]["opened_at"]

        detail = payload(library, f"/api/v1/coins/{MINT}", {}, history_service=service)
        assert "history_provider" not in detail
        assert detail["history_backfill"]["row_count"] == 1002
        assert len(detail["candles"]) == 500 and detail["next_candle_before"] is not None
        assert "api" not in json.dumps(detail).lower()
    finally:
        library.close()


def test_automatic_history_import_command_needs_no_setup(tmp_path):
    service = MarketHistoryService(FakeSource())
    library = StrategyLibrary(tmp_path)
    try:
        result = command(
            library, f"/api/v1/coins/{MINT}/history/backfill", {},
            history_service=service,
        )
        assert result["job"]["row_count"] == 1000
        assert set(result) == {"schema", "version", "job"}
        with pytest.raises(ValueError, match="Unknown read endpoint"):
            payload(library, "/api/v1/history/provider", {}, history_service=service)
        with pytest.raises(ValueError, match="Unknown browser command"):
            command(library, "/api/v1/history/provider/setup", {}, history_service=service)
    finally:
        library.close()


def test_keyless_source_discovers_pool_and_validates_mint_attribution_and_ohlc(monkeypatch):
    pool_response = {"data": [{
        "attributes": {"address": MINT},
        "relationships": {
            "base_token": {"data": {"id": f"solana_{MINT}"}},
            "quote_token": {"data": {"id": "solana_11111111111111111111111111111111"}},
        },
    }]}
    candle_response = {
        "data": {"attributes": {"ohlcv_list": [[100, 1.0, 1.3, .8, 1.1, 50.0]]}},
        "meta": {"base": {"address": MINT}},
    }
    requests = []

    class Reply:
        def __init__(self, response):
            self.response = response

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(self.response).encode()

    class Opener:
        def open(self, request, timeout):
            assert timeout == 15
            assert not any("api-key" in key.lower() for key in request.headers)
            requests.append(request)
            return Reply(pool_response if "/tokens/" in request.full_url else candle_response)

    monkeypatch.setattr("urllib.request.build_opener", lambda *_handlers: Opener())
    source = GeckoTerminalHistorySource(minimum_interval_seconds=0)
    assert source.fetch(MINT, 200)[0]["close_usd"] == "1.1"
    assert "/tokens/" in requests[0].full_url
    assert "aggregate=4" in requests[1].full_url
    assert f"token={MINT}" in requests[1].full_url

    candle_response["meta"]["base"]["address"] = "11111111111111111111111111111111"
    with pytest.raises(MarketHistoryError, match="selected token"):
        source.fetch(MINT, 200)


def test_pool_cursors_overlap_and_gaps_remain_separate_after_restart(tmp_path):
    from pumpwizard_runner.market_history import pool_histories

    pools = [dict(address=MINT, dex='pump', created_at=''),
             dict(address='11111111111111111111111111111111', dex='pumpswap', created_at='')]
    class MultiPool:
        def discover(self, mint): return pools
        def fetch_pool(self, mint, pool, before, limit):
            prices = ('1', '2') if pool == MINT else ('3', '4')
            return [dict(opened_at=1_800_000_000-i*RESOLUTION_SECONDS*2, open_usd=p,
                         high_usd=p, low_usd=p, close_usd=p, volume_usd='10') for i,p in enumerate(prices)]
    library = StrategyLibrary(tmp_path)
    try:
        first = MarketHistoryService(MultiPool()).import_page(library, MINT, now=1_800_000_001)
        assert first['status'] == 'ready' and first['row_count'] == 2
        # A new service resumes the second pool; equal timestamps do not overwrite.
        second = MarketHistoryService(MultiPool()).import_page(library, MINT, now=1_800_000_002)
        assert second['status'] == 'complete' and second['row_count'] == 4
        series = pool_histories(library, MINT)
        assert len(series) == 2 and all(len(pool['gaps']) == 1 for pool in series)
        assert {pool['candles'][0]['close_usd'] for pool in series} == {'1','3'}
        assert library.market_history_candles(MINT, source=SOURCE) == []
    finally:
        library.close()
