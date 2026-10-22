from pathlib import Path

import pytest

from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.orders import OrderIntentError, cancel_order, prepare_order, simulate_order
from pumpwizard_runner.strategies import parse_package


ROOT = Path(__file__).parents[1]
MINT = "MintABC"


def _quote(at: float = 100) -> MarketQuote:
    return MarketQuote(mint=MINT, symbol="ABC", observed_at=at, route="curve", pool="CurveABC",
                       market_cap_usd=15_000, price_usd=2, price_native=.02,
                       liquidity_usd=10_000, liquidity_native=100, quote_usd=100,
                       base_reserve=500_000, reserve_usd=10_000,
                       real_token_reserve=250_000, supply=1_000_000_000, decimals=6)


def _library(tmp_path: Path) -> tuple[StrategyLibrary, str]:
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    library.import_strategy(strategy)
    instance_id = library.create_instance(strategy.digest)
    library.set_executor_config(instance_id, mode="dry-run", max_order_usd=20,
                                max_slippage_bps=500, decision_ttl_seconds=45, updated_at=90)
    library.append_observations(instance_id, [_quote().as_record()])
    return library, instance_id


def test_dry_run_buy_is_bounded_by_config_and_has_a_durable_lifecycle(tmp_path: Path):
    library, instance_id = _library(tmp_path)
    try:
        library.record_decisions(instance_id, [{
            "kind": "entry_recommended", "mint": MINT, "at": 100,
            "reason": "strategy_entry_signal", "payload": {"symbol": "ABC", "route": "curve"},
        }])
        order = prepare_order(library, instance_id, MINT, spend_usd=10, now=110)
        assert order["status"] == "draft"
        assert order["side"] == "buy"
        assert order["max_input_usd"] == "10"
        assert order["minimum_output_amount"] == "4.75"
        simulated = simulate_order(library, order["id"], now=115)
        assert simulated["status"] == "simulated"
        events = library.order_events(order["id"])
        assert [event["kind"] for event in events] == ["dry_run_simulated", "prepared"]
        cancelled = cancel_order(library, order["id"], now=116)
        assert cancelled["status"] == "cancelled"
        assert library.summary()["orders"] == 1
    finally:
        library.close()


def test_dry_run_rejects_expensive_or_stale_entry_decisions(tmp_path: Path):
    library, instance_id = _library(tmp_path)
    try:
        library.record_decisions(instance_id, [{
            "kind": "entry_recommended", "mint": MINT, "at": 100,
            "reason": "strategy_entry_signal", "payload": {"symbol": "ABC", "route": "curve"},
        }])
        with pytest.raises(OrderIntentError, match="maximum"):
            prepare_order(library, instance_id, MINT, spend_usd=21, now=110)
        with pytest.raises(OrderIntentError, match="time limit"):
            prepare_order(library, instance_id, MINT, spend_usd=10, now=146)
    finally:
        library.close()


def test_dry_run_partial_exit_uses_the_recorded_position_quantity(tmp_path: Path):
    library, instance_id = _library(tmp_path)
    try:
        position_id = library.record_position(instance_id, MINT, "ABC", quantity=100,
                                              total_cost_usd=10, opened_at=90)
        library.record_decisions(instance_id, [{
            "kind": "partial_exit_recommended", "mint": MINT, "at": 100,
            "reason": "market_cap_target", "payload": {
                "position_id": position_id, "symbol": "ABC", "fraction_of_original": .4,
            },
        }])
        order = prepare_order(library, instance_id, MINT, spend_usd=None, now=110)
        assert order["side"] == "sell"
        assert order["token_amount"] == "40"
        assert order["minimum_output_amount"] == "76"
    finally:
        library.close()
