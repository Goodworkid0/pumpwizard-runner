import json
from pathlib import Path

import pytest

from pumpwizard_runner.strategies import StrategyImportError, parse_package


ROOT = Path(__file__).parents[1]


def test_native_example_is_ready_for_local_monitoring():
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    assert strategy.schema == "pumpwizard.strategy/v2"
    assert strategy.strategy_id == "xyz-momentum-example"
    assert strategy.compatibility == "monitoring-ready"
    assert strategy.profile == "native-v2"
    assert len(strategy.digest) == 64


def test_legacy_handoff_is_inspection_only():
    handoff = {
        "schema": "pumpwizard.strategy-handoff/v1",
        "mode": "simulation_only",
        "strategy": {"id": "xyz", "label": "XYZ"},
        "configuration": {"entry_style": "momentum"},
    }
    strategy = parse_package(handoff)
    assert strategy.compatibility == "legacy-inspection-only"
    assert "simulation" in strategy.reason.lower()


def test_complete_early_ladder_handoff_maps_to_local_engine():
    handoff = {
        "schema": "pumpwizard.strategy-handoff/v1",
        "mode": "simulation_only",
        "strategy": {"id": "core-frequency", "label": "Core frequency"},
        "configuration": {
            "version": "early-ladder-v1", "mode": "paper_only", "entry_policy": "simulation_unverified",
            "entry_style": "momentum", "budget_usd": 500, "order_usd": 10, "max_positions": 20,
            "entry_cap_min": 10000, "entry_cap_max": 25000, "observation_seconds": 30,
            "minimum_samples": 4, "minimum_price_changes": 2, "minimum_growth_fraction": 0.01,
            "minimum_reserve_growth_usd": 10, "maximum_peak_drawdown": 0.35,
            "max_sample_gap_seconds": 20, "quote_max_age_seconds": 30, "entry_expiry_seconds": 30,
            "fill_delay_seconds": 2, "ownership_ttl_seconds": 120, "min_curve_liquidity_usd": 1000,
            "min_amm_liquidity_usd": 10000, "max_entry_reserve_fraction": 0.01,
            "max_roundtrip_loss_fraction": 0.15, "fee_fraction": 0.025, "adverse_fraction": 0.01,
            "fixed_fee_usd": 0.10, "entry_rent_usd": 0.35, "failed_attempt_probability": 0.05,
            "stop_loss_fraction": -0.30, "trailing_drawdown_fraction": 0.35,
            "quote_gap_exit_seconds": 600, "max_hold_seconds": 864000,
            "targets": [{"market_cap_usd": 30000, "initial_fraction": 0.4}],
        },
    }
    strategy = parse_package(handoff)
    assert strategy.compatibility == "monitoring-ready"
    assert strategy.profile == "ladder-v1"
    assert strategy.engine == "pumpwizard-core/1"


def test_missing_native_rule_is_rejected():
    package = json.loads((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    del package["rules"]["max_hold_seconds"]
    with pytest.raises(StrategyImportError, match="max_hold_seconds"):
        parse_package(package)
