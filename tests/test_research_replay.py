from __future__ import annotations

import copy

import pytest

from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.research_data import build_dataset_export
from pumpwizard_runner.research_jobs import resolve_replay_evaluator
from pumpwizard_runner.research_replay import (
    ADAPTER_VERSION,
    REPORT_SCHEMA,
    ResearchReplayError,
    _execution,
    _quote_events,
    evaluate_research_experiment,
)
from pumpwizard_runner.portfolio_replay import replay_portfolio
from test_portfolio_replay import costs, policy, quote, signal
from pumpwizard_runner.strategies import parse_package


def _package():
    configuration = copy.deepcopy(CONFIG)
    configuration.update({
        "observation_seconds": 30,
        "max_sample_gap_seconds": 20,
        "quote_max_age_seconds": 30,
        "fill_delay_seconds": 2,
        "entry_expiry_seconds": 20,
        "max_hold_seconds": 40,
        "quote_gap_exit_seconds": 30,
        "failed_attempt_probability": 0,
    })
    return {
        "schema": "pumpwizard.strategy-handoff/v1",
        "strategy": {"id": "research-replay-fixture", "label": "Research replay fixture"},
        "configuration": configuration,
    }


def _quote(at: int) -> dict:
    growth = 1 + at / 1_000
    return {
        "mint": "MintReplay",
        "symbol": "RPL",
        "observed_at": at,
        "route": "curve",
        "pool": "pool-replay",
        "market_cap_usd": 15_000 * growth,
        "price_usd": 100 * growth,
        "price_native": growth,
        "liquidity_usd": 10_000 + at * 10,
        "liquidity_native": 100 + at / 10,
        "source": "synthetic-test",
        "quote_usd": 100,
        "base_reserve": 1_000_000,
        "reserve_usd": 10_000 + at * 10,
        "real_token_reserve": 500_000,
        "supply": 1_000_000_000,
        "decimals": 6,
        "source_at": at,
        "fx_at": at,
    }


def _export():
    package = _package()
    observations = [_quote(at) for at in range(0, 301, 10)]
    snapshot = {
        "source_session_id": "research-replay-session",
        "strategy_digest": parse_package(package).digest,
        "strategy_package": package,
        "first_at": 0,
        "last_at": 300,
        "observations": observations,
        "decisions": [],
        "outcomes": [],
        "feature_records": [],
        "decision_capture_complete": True,
    }
    evidence = [{
        "id": f"evidence-{row['observed_at']}",
        "payload": row,
        "first_received_at": row["observed_at"],
        "source_at": row["source_at"],
        "source_time_basis": "independent",
        "run_ids": ["collection-1"],
        "route": row["route"],
        "pool": row["pool"],
    } for row in observations]
    recorder = {
        "schema": "pumpwizard.recorder-research-snapshot/v1",
        "evidence": evidence,
        "collection_runs": [{
            "id": "collection-1", "status": "complete",
            "started_at": 0, "received_at": 300,
        }],
    }
    return build_dataset_export(snapshot, recorder, network="localnet", caps={
        "evidence_rows": 20_000,
        "decisions": 5_000,
        "features": 5_000,
        "outcomes": 5_000,
        "collection_runs": 5_000,
        "canonical_bytes": 32 * 1024 * 1024,
    })


def _experiment(export, *, purpose="eligible"):
    package = _package()
    holdout_ids = sorted(
        row["evidence_id"] for row in export["records"]["observations"]
        if 200 < row["times"]["event_at"] <= 300
    )
    return {
        "schema": "pumpwizard.research-experiment/v1",
        "id": "experiment-replay-fixture",
        "request_sha256": "12" * 32,
        "search_sha256": "34" * 32,
        "registered_at": 1234.0,
        "dataset_id": export["dataset"]["id"],
        "lineage": {
            "source_session_id": "research-replay-session",
            "dataset_content_sha256": export["dataset"]["content_sha256"],
            "holdout_evidence_ids": holdout_ids,
        },
        "candidates": [{
            "id": "candidate-current",
            "package_sha256": parse_package(package).digest,
            "package": package,
        }],
        "cost_model": {
            "label": "fixture registered costs",
            "capital_unit": "USD",
            "variable_fee_fraction_each_side": 0,
            "slippage_fraction_each_side": 0,
            "fixed_cost_each_side": 0,
            "rent_each_entry": 0,
            "adverse_multiplier": 2,
        },
        "capital": {"amount": 100, "unit": "USD"},
        "splits": {
            "train_end_at": 100,
            "validation_end_at": 200,
            "holdout_end_at": 300,
        },
        "embargo_seconds": 5,
        "objective": "modeled net result after costs",
        "evidence_policy": {
            "minimum_independent_episodes": 1,
            "minimum_coverage_fraction": 1,
            "minimum_improvement_usd": 0,
            "maximum_drawdown_fraction": 1,
            "minimum_stability_groups": 1,
            "uncertainty_minimum_independent_episodes": 30,
            "require_nonnegative_adverse_difference": True,
        },
        "purpose": purpose,
        "engine_version": "registered-fixture-engine/1",
        "code_version": "fixture-code/1",
    }


def test_registered_research_adapter_is_deterministic_and_emits_finite_capital_report(tmp_path):
    export = _export()
    experiment = _experiment(export)
    progress = []
    first = evaluate_research_experiment(export, experiment, progress.append, lambda: False)
    second = evaluate_research_experiment(export, experiment)

    assert first == second
    assert first["schema"] == REPORT_SCHEMA
    assert first["adapter_version"] == ADAPTER_VERSION
    assert first["funded_performance"] is False
    assert first["submission_enabled"] is False
    assert first["signing_authority"] is False
    assert [row["kind"] for row in first["comparisons"]] == [
        "cash", "current", "holding",
    ]
    current = first["detailed_comparison"]["validation_results"][0]["base"]
    assert current["orders"] and current["fills"]
    assert current["capital"]["starting_cash_micros"] == 100_000_000
    assert current["metrics"]["coverage_ppm"] == 1_000_000
    assert first["detailed_comparison"]["holdout_policy"]["holdout_used_for_selection"] is False
    assert progress == sorted(progress) and progress[-1] == 0.98
    assert resolve_replay_evaluator(tmp_path) is evaluate_research_experiment


def test_exploratory_run_cannot_recommend_a_successor():
    export = _export()
    report = evaluate_research_experiment(export, _experiment(export, purpose="exploratory"))
    assert report["eligible"] is False
    assert report["recommendation"]["action"] != "create_inactive_draft"
    assert "experiment_purpose_is_exploratory" in report["blockers"]


def test_dataset_and_holdout_lineage_tampering_fail_closed():
    export = _export()
    experiment = _experiment(export)
    changed = copy.deepcopy(export)
    changed["records"]["observations"][0]["payload"]["price_usd"] += 1
    with pytest.raises(ResearchReplayError, match="identity changed"):
        evaluate_research_experiment(changed, experiment)

    changed_experiment = copy.deepcopy(experiment)
    changed_experiment["lineage"]["holdout_evidence_ids"] = []
    with pytest.raises(ResearchReplayError, match="holdout evidence lineage"):
        evaluate_research_experiment(export, changed_experiment)


def test_cancellation_returns_no_partial_report():
    export = _export()
    with pytest.raises(ResearchReplayError, match="cancelled"):
        evaluate_research_experiment(export, _experiment(export), cancelled=lambda: True)


def test_adapter_preserves_nanodollar_price_and_declared_source_time():
    export = _export()
    row = export["records"]["observations"][0]
    row["payload"]["price_usd"] = 0.000000001
    events, _quotes, exclusions = _quote_events(export, lambda: False)
    event = next(item for item in events if item["id"] == row["evidence_id"])
    assert event["price_quote_units_per_token"] == 1_000_000_000
    assert "price_micros_per_token" not in event
    assert event["source_at"] == row["times"]["source_at"]
    assert event["source_time_basis"] == "independent"
    assert exclusions == []


def test_adapter_excludes_unverified_source_time_instead_of_making_it_executable():
    export = _export()
    row = export["records"]["observations"][0]
    row["times"]["source_time_basis"] = "unverified"
    events, _quotes, exclusions = _quote_events(export, lambda: False)
    assert row["evidence_id"] not in {event["id"] for event in events}
    exclusion = next(item for item in exclusions if item["evidence_id"] == row["evidence_id"])
    assert "unknown or unverified source time" in exclusion["reason"]


def test_empty_at_construction_execution_scenario_still_applies_to_future_signal_ids():
    export = _export()
    experiment = _experiment(export)
    experiment["cost_model"]["execution_assumption"] = {
        "failed_entry_probability": 1,
        "failed_entry_seed": "stable-paper-seed",
    }
    scenario = _execution(
        parse_package(_package()), experiment, [], [],
    )
    assert scenario["failed_entry_signal_ids"] == []
    assert scenario["entry_failure_model"] == {
        "method": "sha256-threshold-v1",
        "seed": "stable-paper-seed",
        "failure_ppm": 1_000_000,
    }
    report = replay_portfolio(
        [quote("q-future", 0), signal("not-known-at-construction", 0, evidence=["q-future"])],
        policy=policy(), costs=costs(), execution=scenario,
    )
    assert report["fills"] == []
    assert report["opportunities"][0]["status"] == "failed_entry"
