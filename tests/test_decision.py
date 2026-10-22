from pumpwizard_runner.decision import DecisionEngine, MarketQuote, PositionView, summarize
from pumpwizard_runner.strategies import parse_package


def _rules(entry_policy: str = "simulation_unverified") -> dict[str, object]:
    return {
        "version": "early-ladder-v1", "mode": "paper_only", "entry_policy": entry_policy,
        "entry_style": "momentum", "budget_usd": 500, "order_usd": 10, "max_positions": 3,
        "entry_cap_min": 10000, "entry_cap_max": 25000, "observation_seconds": 60,
        "minimum_samples": 4, "minimum_price_changes": 2, "minimum_growth_fraction": 0.01,
        "minimum_reserve_growth_usd": 5, "maximum_peak_drawdown": 0.35,
        "max_sample_gap_seconds": 30, "quote_max_age_seconds": 30, "entry_expiry_seconds": 30,
        "fill_delay_seconds": 2, "ownership_ttl_seconds": 120, "min_curve_liquidity_usd": 1000,
        "min_amm_liquidity_usd": 10000, "max_entry_reserve_fraction": 0.01,
        "max_roundtrip_loss_fraction": 0.15, "fee_fraction": 0.025, "adverse_fraction": 0.01,
        "fixed_fee_usd": 0.1, "entry_rent_usd": 0.35, "failed_attempt_probability": 0.05,
        "stop_loss_fraction": -0.3, "trailing_drawdown_fraction": 0.35,
        "quote_gap_exit_seconds": 600, "max_hold_seconds": 864000,
        "targets": [{"market_cap_usd": 30000, "initial_fraction": 0.4},
                    {"market_cap_usd": 50000, "initial_fraction": 0.3}],
    }


def _strategy(entry_policy: str = "simulation_unverified"):
    return parse_package({
        "schema": "pumpwizard.strategy-handoff/v1", "mode": "simulation_only",
        "strategy": {"id": "core-frequency", "label": "Core frequency"},
        "configuration": _rules(entry_policy),
    })


def _quote(at: float, price: float, cap: float = 15000) -> MarketQuote:
    return MarketQuote(mint="MintABC", symbol="ABC", observed_at=at, route="curve", pool="CurveABC",
                       market_cap_usd=cap, price_usd=price * 100, price_native=price,
                       liquidity_usd=(100 + at / 20) * 100, liquidity_native=100 + at / 20,
                       quote_usd=100, base_reserve=1_000_000, reserve_usd=10_000,
                       real_token_reserve=500_000, supply=1_000_000_000, decimals=6)


def test_ladder_engine_emits_entry_after_local_observation_window():
    engine = DecisionEngine(_strategy())
    for at, price in ((0, 1.0), (20, 1.01), (40, 1.02)):
        engine.evaluate([_quote(at, price)], at)
    decisions = engine.evaluate([_quote(60, 1.03)], 60)
    assert [(decision.kind, decision.reason) for decision in decisions] == [
        ("entry_recommended", "strategy_entry_signal"),
    ]


def test_ladder_engine_requires_fresh_ownership_for_strict_entry():
    engine = DecisionEngine(_strategy("strict"))
    for at, price in ((0, 1.0), (20, 1.01), (40, 1.02)):
        engine.evaluate([_quote(at, price)], at)
    final_quote = _quote(60, 1.03)
    engine.evaluate([final_quote], 60)
    assert engine.entry_reason(final_quote, {}, 60) == "Ownership flagged or unverified"


def test_target_recommendation_uses_user_recorded_position_state():
    engine = DecisionEngine(_strategy())
    position = PositionView(position_id="position-1", mint="MintABC", symbol="ABC", entry_at=0,
                            initial_quantity=100, quantity=100, remaining_cost_usd=.5,
                            total_cost_usd=.5, peak_price_usd=1, last_usable_at=0, entry_pool="CurveABC")
    decisions = engine.evaluate([_quote(100, 0.2, cap=31_000)], 100, positions=(position,))
    exit_decision = next(decision for decision in decisions if decision.kind == "partial_exit_recommended")
    assert exit_decision.reason == "market_cap_target"
    assert exit_decision.payload["fraction_of_original"] == 0.4


def test_persisted_curve_quote_keeps_its_pool_identity():
    original = _quote(60, 1.03)
    restored = MarketQuote.from_record(original.as_record())
    assert restored.route == "curve"
    assert restored.pool == "CurveABC"
    assert restored.base_reserve == original.base_reserve


def test_research_summary_uses_the_frozen_strategy_gap_instead_of_three_minutes():
    points = [_quote(index * 900, 1 + index / 100) for index in range(6)]
    assert summarize(
        points, 4_500, points[-1].observed_at, maximum_gap_seconds=1_800,
    ) is not None
    assert summarize(
        points, 4_500, points[-1].observed_at, maximum_gap_seconds=899,
    ) is None
