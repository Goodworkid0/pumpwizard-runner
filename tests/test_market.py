import json
from pathlib import Path

import pytest

from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.market import (
    CanonicalPumpSwapMarketSource,
    CompositePumpMarketSource,
    JupiterPublicTokenSource,
    MarketDataError,
    MarketNormalizationError,
    PumpPublicSource,
    PumpSwapPairSource,
    _timestamp_seconds,
)
from pumpwizard_runner.pump_route import TOKEN_PROGRAM
from pumpwizard_runner.pump_swap_route import ROUTE as PUMPSWAP_ROUTE
from pumpwizard_runner.transfers import GENESIS
from pumpwizard_runner.portfolio_replay import quote_price_fields


MINT = "So11111111111111111111111111111111111111112"
SECOND_MINT = "11111111111111111111111111111111"
SANITIZED_LISTING = json.loads(
    (Path(__file__).parent / "fixtures/market/pump-curve-listing-sanitized.json").read_text()
)


def _coin() -> dict[str, object]:
    return {
        "mint": "So11111111111111111111111111111111111111112", "symbol": "ABC", "name": "Alpha Coin",
        "description": "A public token description.", "creator": SECOND_MINT,
        "website": "https://example.com/token", "twitter": "http://insecure.invalid/profile",
        "telegram": "https://t.me/alpha", "image_uri": "https://cdn.example.com/alpha.png",
        "verified": True,
        "base_decimals": 6, "quote_decimals": 9,
        "quote_mint": "So11111111111111111111111111111111111111112", "program": "pump",
        "complete": False, "updated_at": 95, "virtual_quote_reserves": 100_000_000_000,
        "real_quote_reserves": 50_000_000_000, "virtual_token_reserves": 1_000_000_000_000,
        "real_token_reserves": 500_000_000_000, "total_supply": 1_000_000_000_000,
        "market_cap_usd": 20_000, "bonding_curve": SECOND_MINT, "created_timestamp": 90_000,
    }


def test_normalized_market_quote_preserves_reserves_for_strategy_screen():
    quote = PumpPublicSource._normalize(_coin(), {"asOfTimestamp": 96_000, "solPrice": 100}, 100)
    assert quote.route == "curve"
    assert quote.reserve_usd == 10_000
    assert quote.real_token_reserve == 500_000
    assert quote.quote_usd == 100
    assert quote.created_at == 90_000
    assert quote.metadata == {
        "name": "Alpha Coin", "description": "A public token description.", "creator": SECOND_MINT,
        "website": "https://example.com/token", "telegram": "https://t.me/alpha",
        "image_url": "https://cdn.example.com/alpha.png",
        "verified": True, "source_time_basis": "provider", "source": "Pump.fun",
    }


def test_pump_millisecond_timestamp_is_normalized_to_seconds():
    assert _timestamp_seconds(1_700_000_000_000) == 1_700_000_000


def test_completed_curve_is_not_accepted_as_an_entry_quote():
    coin = _coin()
    coin["complete"] = True
    with pytest.raises(ValueError, match="Completed"):
        PumpPublicSource._normalize(coin, {"asOfTimestamp": 96_000, "solPrice": 100}, 100)


def test_public_source_returns_only_fresh_solana_usd_prices(monkeypatch):
    source = PumpPublicSource()
    monkeypatch.setattr(source, "_get_json", lambda _path: {
        "solPrice": 150.25, "asOfTimestamp": 1_000_000, "stale": False,
    })
    monkeypatch.setattr("pumpwizard_runner.market.time.time", lambda: 1_000.5)
    assert source.sol_usd_price() == 150.25
    monkeypatch.setattr(source, "_get_json", lambda _path: {
        "solPrice": 150.25, "asOfTimestamp": 1, "stale": True,
    })
    with pytest.raises(MarketDataError, match="unavailable"):
        source.sol_usd_price()


def test_exact_mint_metadata_does_not_require_an_executable_quote(monkeypatch):
    source = PumpPublicSource()
    complete = _coin()
    complete["complete"] = True
    monkeypatch.setattr(source, "_get_json", lambda path: complete if path == f"/coins/{MINT}" else None)
    metadata = source.token_metadata({MINT})[MINT]
    assert metadata["symbol"] == "ABC"
    assert metadata["name"] == "Alpha Coin"
    assert metadata["image_url"] == "https://cdn.example.com/alpha.png"
    assert metadata["website"] == "https://example.com/token"


def test_composite_metadata_fills_missing_pump_fields_without_replacing_its_name():
    class Pump:
        def token_metadata(self, _mints):
            return {MINT: {"name": "Pump name", "symbol": "PUMP", "source": "Pump.fun"}}

    class Pair:
        def token_metadata(self, _mints):
            return {MINT: {
                "name": "Pair name", "image_url": "https://cdn.dexscreener.com/token.png",
                "website": "https://example.com", "source": "PumpSwap",
            }}

    metadata = CompositePumpMarketSource(Pump(), Pair()).token_metadata({MINT})[MINT]
    assert metadata == {
        "name": "Pump name", "symbol": "PUMP", "source": "Pump.fun",
        "image_url": "https://cdn.dexscreener.com/token.png", "website": "https://example.com",
    }


def test_composite_metadata_preserves_exact_mint_artwork_alternatives():
    class Pump:
        def token_metadata(self, _mints):
            return {MINT: {"name": "Pump name", "image_url": "https://pumpapi.io/primary.png"}}

    class Pair:
        def token_metadata(self, _mints):
            return {MINT: {"image_url": "https://cdn.dexscreener.com/alternative.png"}}

    class Profiles:
        def token_profiles(self, _mints):
            return {MINT: {"metadata": {"image_url": "https://ipfs.io/ipfs/alternate"}}}

    metadata = CompositePumpMarketSource(Pump(), Pair(), Profiles()).token_metadata({MINT})[MINT]
    assert metadata["name"] == "Pump name"
    assert metadata["image_url"] == "https://pumpapi.io/primary.png"
    assert metadata["image_urls"] == [
        "https://pumpapi.io/primary.png", "https://cdn.dexscreener.com/alternative.png", "https://ipfs.io/ipfs/alternate",
    ]


def test_jupiter_batch_profiles_keep_only_bounded_exact_mint_market_context(monkeypatch):
    source = JupiterPublicTokenSource()
    monkeypatch.setattr(source, "_get_json", lambda mints: [{
        "id": mints[0], "name": "Mayhem token", "symbol": "MAY", "icon": "https://ipfs.io/ipfs/icon",
        "launchpad": "pump.fun", "usdPrice": 0.0000005, "priceBlockId": 1234,
        "liquidity": 3500, "mcap": 500, "holderCount": 2, "organicScore": 0,
        "organicScoreLabel": "low", "stats24h": {"volumeChange": -100},
        "audit": {"topHoldersPercentage": 100}, "createdAt": "2026-09-25T11:18:06Z",
    }, {"id": "not-requested", "name": "Wrong"}])
    profile = source.token_profiles({MINT})[MINT]
    assert profile["metadata"] == {
        "name": "Mayhem token", "symbol": "MAY", "image_url": "https://ipfs.io/ipfs/icon",
        "source": "Pump.fun",
    }
    assert profile["price_usd"] == 0.0000005
    assert profile["price_block_id"] == 1234
    assert profile["price_change_24h"] is None
    assert profile["volume_change_24h"] == -100
    assert profile["organic_score_label"] == "low"
    assert profile["top_holders_percent"] == 100


def test_public_source_skips_partial_listing_without_stopping_the_scan(monkeypatch):
    """Public discovery records can arrive before all timestamp fields exist."""
    source = PumpPublicSource()
    valid = _coin()
    partial = dict(valid)
    partial.pop("updated_at")

    def get_json(path):
        if path == "/sol-price":
            return {"solPrice": 100, "asOfTimestamp": 100_000, "stale": False}
        if path.startswith("/coins?"):
            return [partial, valid]
        if path == "/coins/" + MINT:
            return valid
        raise AssertionError(path)

    monkeypatch.setattr(source, "_get_json", get_json)
    monkeypatch.setattr("pumpwizard_runner.market.time.time", lambda: 100)
    quotes = source.collect(10_000, 25_000, 10)
    assert [quote.mint for quote in quotes] == [MINT]


def test_public_source_normalizes_supported_listing_without_detail_requests(monkeypatch):
    source = PumpPublicSource()
    row = dict(SANITIZED_LISTING)
    now = float(row["updated_at"]) + 2
    requested = []

    def get_json(path):
        requested.append(path)
        if path == "/sol-price":
            return {"solPrice": 118.09, "asOfTimestamp": (now - 1) * 1000, "stale": False}
        if path.startswith("/coins?"):
            return [row]
        raise AssertionError("The unavailable exact-mint detail route must not be requested")

    monkeypatch.setattr(source, "_get_json", get_json)
    monkeypatch.setattr("pumpwizard_runner.market.time.time", lambda: now)
    quotes, diagnostics = source.collect_with_diagnostics(1_000, 150_000, 3)
    assert len(quotes) == 1
    assert quotes[0].mint == row["mint"]
    assert quotes[0].pool == row["bonding_curve"]
    assert quotes[0].route == "curve"
    assert quotes[0].decimals == 6
    assert quotes[0].price_native == pytest.approx(
        row["virtual_quote_reserves"] / 1e9
        / (row["virtual_token_reserves"] / 1e6),
    )
    assert len(requested) == 3
    assert diagnostics["status"] == "healthy"
    assert diagnostics["stages"] == {
        "fx_requests": 1,
        "discovery_requests": 2,
        "listing_rows": 2,
        "unique_listings": 1,
        "eligible_candidates": 1,
        "selected_records": 1,
        "selected_detail_requests": 0,
        "returned_details": 0,
        "normalized_quotes": 1,
        "http_requests": 3,
        "usable_discovery_responses": 2,
        "response_bytes": diagnostics["stages"]["response_bytes"],
    }
    assert diagnostics["readiness"]["routes"] == ["curve"]


def test_public_source_diagnostics_distinguish_empty_partial_stale_and_failing(monkeypatch):
    row = dict(SANITIZED_LISTING)
    now = float(row["updated_at"]) + 2
    fx = {"solPrice": 118.09, "asOfTimestamp": (now - 1) * 1000, "stale": False}

    empty = PumpPublicSource()
    monkeypatch.setattr(empty, "_get_json", lambda path: fx if path == "/sol-price" else [])
    monkeypatch.setattr("pumpwizard_runner.market.time.time", lambda: now)
    assert empty.collect(1_000, 150_000, 3) == []
    assert empty.last_diagnostics["status"] == "empty"
    assert empty.last_diagnostics["rejections"]["empty_eligible_universe"] == 1

    partial = PumpPublicSource()

    def partial_json(path):
        if path == "/sol-price":
            return fx
        if "sort=created_timestamp" in path:
            raise MarketDataError("sanitized request failure")
        return [row]

    monkeypatch.setattr(partial, "_get_json", partial_json)
    assert len(partial.collect(1_000, 150_000, 3)) == 1
    assert partial.last_diagnostics["status"] == "partial"
    assert partial.last_diagnostics["rejections"]["request_failure"] == 1

    stale = PumpPublicSource()
    stale_row = {**row, "updated_at": now - 100}
    monkeypatch.setattr(
        stale, "_get_json", lambda path: fx if path == "/sol-price" else [stale_row],
    )
    assert stale.collect(1_000, 150_000, 3) == []
    assert stale.last_diagnostics["status"] == "stale"
    assert stale.last_diagnostics["rejections"]["stale_or_unknown_source_time"] == 1

    failing = PumpPublicSource()

    def failing_json(path):
        if path == "/sol-price":
            return fx
        raise MarketDataError("sanitized request failure")

    monkeypatch.setattr(failing, "_get_json", failing_json)
    with pytest.raises(MarketDataError, match="All curve discovery"):
        failing.collect(1_000, 150_000, 3)
    assert failing.last_diagnostics["status"] == "failing"
    assert failing.last_diagnostics["rejections"]["request_failure"] == 2


def test_public_source_rejects_stale_fx_and_invalid_listing_identity(monkeypatch):
    row = dict(SANITIZED_LISTING)
    now = float(row["updated_at"]) + 2
    stale_fx = PumpPublicSource()
    monkeypatch.setattr(
        stale_fx,
        "_get_json",
        lambda path: {
            "solPrice": 118.09, "asOfTimestamp": (now - 100) * 1000, "stale": False,
        },
    )
    monkeypatch.setattr("pumpwizard_runner.market.time.time", lambda: now)
    with pytest.raises(MarketDataError, match="stale"):
        stale_fx.collect(1_000, 150_000, 3)
    assert stale_fx.last_diagnostics["status"] == "stale"

    for field, value in (("mint", "bad"), ("bonding_curve", "bad"), ("base_decimals", 99)):
        invalid = {**row, field: value}
        with pytest.raises(MarketNormalizationError) as rejected:
            PumpPublicSource._normalize(
                invalid,
                {"solPrice": 118.09, "asOfTimestamp": (now - 1) * 1000},
                now,
            )
        assert rejected.value.category == "invalid_identity_or_units"


def test_public_source_keeps_fine_price_precision_from_integer_reserves():
    row = dict(SANITIZED_LISTING)
    now = float(row["updated_at"]) + 2
    quote = PumpPublicSource._normalize(
        row, {"solPrice": 118.09, "asOfTimestamp": (now - 1) * 1000}, now,
    )
    expected_native = 35.178055554 / 915_059_104.122084
    assert quote.price_native == round(expected_native, 18)
    assert quote.price_native != round(quote.price_native, 10)
    assert quote_price_fields(quote.price_usd)["price_quote_units_per_token"] > 0


def test_public_source_enforces_request_and_decoded_byte_ceilings(monkeypatch):
    row = dict(SANITIZED_LISTING)
    now = float(row["updated_at"]) + 2
    fx = {"solPrice": 118.09, "asOfTimestamp": (now - 1) * 1000, "stale": False}
    monkeypatch.setattr("pumpwizard_runner.market.time.time", lambda: now)

    requests = []
    capped = PumpPublicSource(maximum_http_requests=2)

    def capped_json(path):
        requests.append(path)
        return fx if path == "/sol-price" else [row]

    monkeypatch.setattr(capped, "_get_json", capped_json)
    assert len(capped.collect(1_000, 150_000, 3)) == 1
    assert len(requests) == 2
    assert capped.last_diagnostics["status"] == "quota_exhausted"
    assert capped.last_diagnostics["rejections"]["exhausted_quota"] == 1

    byte_capped = PumpPublicSource(maximum_response_bytes=1_024)
    oversized = [{**row, "description": "x" * 2_000}]
    monkeypatch.setattr(
        byte_capped, "_get_json", lambda path: fx if path == "/sol-price" else oversized,
    )
    with pytest.raises(MarketDataError, match="All curve discovery"):
        byte_capped.collect(1_000, 150_000, 3)
    assert byte_capped.last_diagnostics["status"] == "quota_exhausted"
    assert byte_capped.last_diagnostics["stages"]["response_bytes"] == 1_024


def test_public_source_marks_malformed_listing_as_partial_when_other_page_is_usable(monkeypatch):
    row = dict(SANITIZED_LISTING)
    now = float(row["updated_at"]) + 2
    fx = {"solPrice": 118.09, "asOfTimestamp": (now - 1) * 1000, "stale": False}
    source = PumpPublicSource()

    def get_json(path):
        if path == "/sol-price":
            return fx
        return {} if "sort=created_timestamp" in path else [row]

    monkeypatch.setattr(source, "_get_json", get_json)
    monkeypatch.setattr("pumpwizard_runner.market.time.time", lambda: now)
    assert len(source.collect(1_000, 150_000, 3)) == 1
    assert source.last_diagnostics["status"] == "partial"
    assert source.last_diagnostics["rejections"]["unsupported_shape"] == 1


def _pair() -> dict[str, object]:
    return {
        "chainId": "solana", "dexId": "pumpswap", "pairAddress": SECOND_MINT,
        "baseToken": {"address": MINT, "symbol": "ABC"},
        "quoteToken": {"address": MINT}, "priceNative": "0.01", "priceUsd": "1.50",
        "liquidity": {"usd": 15_000, "base": 10_000, "quote": 100},
        "marketCap": None, "fdv": 150_000,
    }


def test_pumpswap_pair_quote_is_normalized_for_held_position_monitoring():
    quote = PumpSwapPairSource._normalize_pair(_pair(), 100)
    assert quote.route == "amm"
    assert quote.source == "dexscreener_pumpswap_pair"
    assert quote.quote_usd == 150
    assert quote.liquidity_native == 100
    assert quote.supply == 100_000


def test_canonical_pumpswap_source_uses_exact_signed_reserves_and_records_read_counts():
    class Rpc:
        network = "mainnet-beta"

        def call(self, method, _params):
            return GENESIS["mainnet-beta"] if method == "getGenesisHash" else {}

    class Fx:
        @staticmethod
        def sol_usd_quote():
            return 100.0, 99.0, 100.0

    def state_reader(rpc, sender, mint, *, side):
        assert sender is None and side == "buy"
        rpc.call("getMultipleAccounts", [])
        rpc.call("getMultipleAccounts", [])
        return {
            "route": PUMPSWAP_ROUTE,
            "token_program": TOKEN_PROGRAM,
            "sender": None,
            "mint": mint,
            "pool": SECOND_MINT,
            "slot": 123,
            "decimals": 6,
            "amm_reserves": {
                "base_reserves": 500_000_000_000,
                "quote_reserves": 60_000_000_000,
                "virtual_quote_reserves": -10_000_000_000,
                "effective_quote_reserves": 50_000_000_000,
                "token_total_supply": 1_000_000_000_000_000,
            },
        }

    source = CanonicalPumpSwapMarketSource(
        rpc=Rpc(), fx_source=Fx(), clock=lambda: 100.0,
        state_reader=state_reader,
    )
    quotes = source.collect(1, 20_000_000, 1, {MINT})
    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.route == "amm" and quote.pool == SECOND_MINT
    assert quote.decimals == 6 and quote.base_reserve == 500_000
    assert quote.price_native == .0001 and quote.market_cap_usd == 10_000_000
    assert quote.reserve_usd == 5_000 and quote.liquidity_usd == 6_000
    assert quote.metadata["effective_quote_reserves_lamports"] == 50_000_000_000
    assert quote.metadata["execution_promise"] is False
    assert source.last_diagnostics["stages"]["http_requests"] == 4
    assert source.last_diagnostics["stages"]["response_bytes"] is None


def test_canonical_pumpswap_source_rejects_stale_fx_before_pool_reads():
    methods = []

    class Rpc:
        network = "mainnet-beta"

        def call(self, method, _params):
            methods.append(method)
            return GENESIS["mainnet-beta"]

    class Fx:
        @staticmethod
        def sol_usd_quote():
            return 100.0, 100.0, 100.0

    source = CanonicalPumpSwapMarketSource(
        rpc=Rpc(), fx_source=Fx(), clock=lambda: 300.0,
        state_reader=lambda *_args, **_kwargs: pytest.fail("stale FX must stop pool reads"),
    )
    assert source.collect(1, 20_000_000, 1, {MINT}) == []
    assert methods == ["getGenesisHash"]
    assert source.last_diagnostics["status"] == "stale"
    assert source.last_diagnostics["rejections"] == {
        "stale_or_unknown_source_time": 1,
    }
    assert source.last_diagnostics["readiness"]["reasons"] == [
        "stale_sol_usd_reference",
    ]


def test_pumpswap_pair_rejects_non_sol_quote_asset():
    pair = _pair()
    pair["quoteToken"] = {"address": "NotSol"}
    with pytest.raises(ValueError, match="quote asset"):
        PumpSwapPairSource._normalize_pair(pair, 100)


class _StaticSource:
    def __init__(self, quotes: list[MarketQuote] | None = None, error: str | None = None):
        self.quotes = quotes or []
        self.error = error

    def collect(self, minimum_cap_usd: float, maximum_cap_usd: float, limit: int,
                priority: set[str] | None = None) -> list[MarketQuote]:
        if self.error:
            raise MarketDataError(self.error)
        return self.quotes


def test_composite_keeps_curve_quote_and_falls_back_to_amm_for_missing_held_mint():
    curve = PumpPublicSource._normalize(_coin(), {"asOfTimestamp": 96_000, "solPrice": 100}, 100)
    amm = PumpSwapPairSource._normalize_pair(_pair(), 100)
    composite = CompositePumpMarketSource(_StaticSource([curve]), _StaticSource([amm]))
    assert composite.collect(10_000, 25_000, 10, {MINT}) == [curve]


def test_composite_returns_amm_quote_when_curve_source_is_temporarily_unavailable():
    amm = PumpSwapPairSource._normalize_pair(_pair(), 100)
    composite = CompositePumpMarketSource(_StaticSource(error="curve unavailable"), _StaticSource([amm]))
    assert composite.collect(10_000, 25_000, 10, {MINT}) == [amm]
