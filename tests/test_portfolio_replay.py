from __future__ import annotations

import copy

import pytest

from pumpwizard_runner.portfolio_replay import (
    COST_SCHEMA,
    EVIDENCE_POLICY_SCHEMA,
    EXECUTION_SCHEMA,
    POLICY_SCHEMA,
    PortfolioReplayError,
    build_holding_benchmark,
    compare_portfolios,
    replay_portfolio,
    split_episodes,
)


def policy(**changes):
    value = {
        "schema": POLICY_SCHEMA,
        "family": "ladder-v1",
        "capital_unit": "quote-micros",
        "quote_currency": "USD",
        "starting_cash_micros": 200_000_000,
        "order_micros": 100_000_000,
        "max_positions": 2,
        "submission_delay_seconds": 0,
        "entry_expiry_seconds": 10,
        "max_quote_age_seconds": 10,
        "max_hold_seconds": 100,
        "stale_exit_seconds": 20,
        "stop_loss_ppm": -500_000,
        "trailing_drawdown_ppm": 500_000,
        "supported_routes": ["curve", "amm"],
        "targets": [{"market_cap_micros": 20_000_000_000, "initial_fraction_ppm": 400_000}],
    }
    value.update(changes)
    return value


def costs(**changes):
    value = {
        "schema": COST_SCHEMA,
        "fee_ppm": 0,
        "spread_ppm": 0,
        "slippage_ppm": 0,
        "impact_ppm_at_full_liquidity": 0,
        "maximum_impact_ppm": 0,
        "fixed_fee_micros": 0,
        "failed_entry_fee_micros": 5_000,
        "failed_exit_fee_micros": 7_000,
    }
    value.update(changes)
    return value


def execution(**changes):
    value = {
        "schema": EXECUTION_SCHEMA,
        "failed_entry_signal_ids": [],
        "unavailable_exit_quote_ids": [],
        "exit_fill_ppm_by_quote_id": {},
    }
    value.update(changes)
    return value


def quote(ident, at, mint="A", *, price=1_000_000, cap=10_000_000_000,
          route="curve", pool="pool-a", decimals=0, migration=None):
    value = {
        "id": ident, "kind": "quote", "event_at": at, "first_known_at": at,
        "mint": mint, "route": route, "pool": pool, "decimals": decimals,
        "quote_currency": "USD",
        "price_micros_per_token": price, "market_cap_micros": cap,
        "liquidity_micros": 1_000_000_000,
    }
    if migration is not None:
        value["migration_from_pool"] = migration
    return value


def signal(ident, at, mint="A", *, evidence=None, episode=None, group=None,
           split="validation"):
    return {
        "id": ident, "kind": "entry_signal", "event_at": at, "first_known_at": at,
        "mint": mint, "evidence_ids": evidence or [f"q-{mint}-{at}"],
        "episode_id": episode or f"episode-{ident}",
        "dependence_group": group or f"token-{mint}", "split": split,
    }


def exit_signal(ident, at, mint="A", fraction=1_000_000):
    return {
        "id": ident, "kind": "exit_signal", "event_at": at, "first_known_at": at,
        "mint": mint, "fraction_ppm": fraction, "reason": "fixture_exit",
    }


def clock(ident, at):
    return {"id": ident, "kind": "clock", "event_at": at, "first_known_at": at}


def round_trip(*, entry_price=1_000_000, exit_price=1_100_000, mint="A",
               split="validation", episode="episode-1", group="token-A"):
    return [
        quote("q0-" + mint, 0, mint, price=entry_price),
        signal("s-" + mint, 0, mint, evidence=["q0-" + mint], episode=episode,
               group=group, split=split),
        quote("q1-" + mint, 1, mint, price=entry_price),
        exit_signal("x-" + mint, 2, mint),
        quote("q2-" + mint, 3, mint, price=exit_price),
    ]


def test_shared_cash_equal_time_order_and_repeat_are_deterministic():
    events = [
        quote("q-A", 0, "A"), quote("q-B", 0, "B"),
        signal("s-A", 0, "A", evidence=["q-A"]),
        signal("s-B", 0, "B", evidence=["q-B"]),
    ]
    selected_policy = policy(starting_cash_micros=150_000_000)
    first = replay_portfolio(events, policy=selected_policy, costs=costs(), execution=execution())
    second = replay_portfolio(reversed(events), policy=selected_policy, costs=costs(), execution=execution())
    assert first == second
    assert first["capital"]["ending_cash_micros"] == 50_000_000
    assert first["open_positions"]["A"]["quantity_atoms"] == 100
    assert "B" not in first["open_positions"]
    assert [row["status"] for row in first["opportunities"]] == [
        "filled", "skipped_insufficient_cash"
    ]
    assert first["capital"]["reserved_micros"] == 0


def test_exact_40_percent_partial_exit_then_remaining_sale_conserves_atoms_and_cash():
    events = [
        quote("q0", 0), signal("s0", 0, evidence=["q0"]),
        quote("q1", 1),
        quote("q-target", 2, cap=25_000_000_000),
        exit_signal("x-final", 3), quote("q-final", 4),
    ]
    report = replay_portfolio(events, policy=policy(), costs=costs(), execution=execution())
    exits = [fill for fill in report["fills"] if fill["side"] == "exit"]
    assert [fill["quantity_atoms"] for fill in exits] == [40, 60]
    assert not report["open_positions"]
    assert report["capital"]["ending_cash_micros"] == 200_000_000
    assert sum(fill["quantity_atoms"] for fill in exits) == report["fills"][0]["quantity_atoms"]
    assert report["metrics"]["completed_position_exits"] == 1
    assert report["metrics"]["coverage_ppm"] == 1_000_000


def test_entry_rent_is_charged_once_and_never_reapplied_on_exit():
    report = replay_portfolio(
        round_trip(entry_price=1_000_000, exit_price=1_000_000),
        policy=policy(), costs=costs(entry_rent_micros=1_000_000),
        execution=execution(),
    )
    assert report["capital"]["ending_cash_micros"] == 199_000_000
    assert report["cost_breakdown"]["rent_micros"] == 1_000_000
    assert report["fills"][0]["costs"]["rent_micros"] == 1_000_000
    assert report["fills"][-1]["costs"]["rent_micros"] == 0


def test_large_equity_curve_is_deterministically_bounded_without_changing_metrics():
    report = replay_portfolio(
        [clock(f"clock-{at}", at) for at in range(300)],
        policy=policy(), costs=costs(), execution=execution(),
    )
    assert len(report["equity_curve"]) == 256
    assert report["equity_curve"][0]["at"] == 0
    assert report["equity_curve"][-1]["at"] == 299
    assert report["equity_curve_sampling"] == {
        "method": "deterministic-even-with-first-last-extrema",
        "metrics_use_all_points": True,
        "total_points": 300,
        "retained_points": 256,
    }
    assert report["metrics"]["maximum_drawdown_ppm"] == 0


def test_delayed_failed_entry_charges_only_declared_fee_and_releases_reservation():
    events = [
        quote("q0", 0), signal("s0", 0, evidence=["q0"]),
        quote("q-too-soon", 1), quote("q-due", 2),
    ]
    report = replay_portfolio(
        events, policy=policy(submission_delay_seconds=2), costs=costs(),
        execution=execution(failed_entry_signal_ids=["s0"]),
    )
    assert report["capital"] == {
        "unit": "quote-micros", "quote_currency": "USD",
        "starting_cash_micros": 200_000_000,
        "ending_cash_micros": 199_995_000, "reserved_micros": 0,
    }
    assert report["cost_breakdown"]["failed_micros"] == 5_000
    assert not report["fills"] and not report["open_positions"]
    assert report["opportunities"][0]["status"] == "failed_entry"


def test_unavailable_stale_exit_keeps_censored_unpriced_position():
    events = [
        quote("q0", 0), signal("s0", 0, evidence=["q0"]), quote("q1", 1),
        exit_signal("x0", 2), quote("q-unavailable", 3), clock("offline", 30),
    ]
    report = replay_portfolio(
        events, policy=policy(), costs=costs(),
        execution=execution(unavailable_exit_quote_ids=["q-unavailable"]),
    )
    assert report["open_positions"]["A"]["quantity_atoms"] == 100
    assert report["open_positions"]["A"]["valuation_status"] == "unknown_stale_or_unavailable_exit"
    assert report["metrics"]["valuation_complete"] is False
    assert report["metrics"]["unknown_unpriced_basis_micros"] == 100_000_000
    assert report["metrics"]["conservative_ending_equity_micros"] == 99_993_000
    assert report["metrics"]["at_cost_upper_equity_micros"] == 199_993_000
    assert report["cost_breakdown"]["failed_micros"] == 7_000


def test_pool_migration_requires_explicit_lineage_and_decimal_match():
    events = [
        quote("q0", 0), signal("s0", 0, evidence=["q0"]), quote("q1", 1),
        quote("bad-pool", 2, route="amm", pool="pool-b"),
        quote("migrated", 3, route="amm", pool="pool-b", migration="pool-a"),
        quote("bad-decimals", 4, route="amm", pool="pool-b", decimals=6),
    ]
    report = replay_portfolio(events, policy=policy(), costs=costs(), execution=execution())
    assert report["open_positions"]["A"]["route"] == "amm"
    assert report["open_positions"]["A"]["pool"] == "pool-b"
    assert report["open_positions"]["A"]["quantity_atoms"] == 100
    assert [row["reason"] for row in report["exclusions"]] == [
        "unsupported_route_or_pool_change", "route_or_decimal_mismatch"
    ]
    assert any(row["action"] == "route_migration" for row in report["position_ledger"])


def test_no_signals_remains_cash_and_future_information_is_rejected():
    report = replay_portfolio(
        [quote("q0", 0), clock("done", 10)],
        policy=policy(), costs=costs(), execution=execution(),
    )
    assert report["metrics"]["net_result_micros"] == 0
    assert report["capital"]["ending_cash_micros"] == 200_000_000
    bad = signal("s0", 0, evidence=["future"])
    with pytest.raises(PortfolioReplayError, match="not yet first-known"):
        replay_portfolio([bad], policy=policy(), costs=costs(), execution=execution())
    future = quote("future", 10)
    future["first_known_at"] = 9
    with pytest.raises(PortfolioReplayError, match="known before"):
        replay_portfolio([future], policy=policy(), costs=costs(), execution=execution())


def test_holding_benchmark_is_explicit_and_rejects_ambiguous_pool_history():
    fixture = build_holding_benchmark([
        quote("a0", 0, "A"), quote("a1", 5, "A", price=1_100_000),
        quote("b0", 0, "B"), quote("b1", 5, "B", route="amm", pool="pool-b"),
        quote("c0", 0, "C"),
    ])
    assert fixture["eligible_mints"] == ["A"]
    assert fixture["excluded_mints"] == [
        {"mint": "B", "reason": "pool_change_without_migration_lineage"},
        {"mint": "C", "reason": "needs_at_least_two_quotes"},
    ]
    report = replay_portfolio(
        fixture["events"], policy=policy(targets=[]), costs=costs(), execution=execution(),
    )
    assert report["metrics"]["net_result_micros"] == 10_000_000
    assert not report["open_positions"]


def test_partial_execution_and_cancellation_do_not_borrow_future_proceeds():
    events = round_trip(exit_price=1_200_000)
    result = replay_portfolio(
        events, policy=policy(), costs=costs(),
        execution=execution(exit_fill_ppm_by_quote_id={"q2-A": 500_000}),
    )
    assert result["open_positions"]["A"]["quantity_atoms"] == 50
    assert result["capital"]["ending_cash_micros"] == 160_000_000
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 2

    with pytest.raises(PortfolioReplayError, match="cancelled"):
        replay_portfolio(events, policy=policy(), costs=costs(), execution=execution(),
                         cancelled=cancelled)


def test_embargo_and_censoring_are_chronological_and_explicit():
    rows = split_episodes([
        {"id": "train", "mint": "A", "entry_at": 1, "outcome_at": 7},
        {"id": "cross", "mint": "A", "entry_at": 8, "outcome_at": 11},
        {"id": "validation", "mint": "B", "entry_at": 11, "outcome_at": 17},
        {"id": "holdout", "mint": "C", "entry_at": 21, "outcome_at": 29},
        {"id": "censored", "mint": "D", "entry_at": 22, "outcome_at": None},
    ], train_end_at=10, validation_end_at=20, holdout_end_at=30, embargo_seconds=2)
    assert [row["split"] for row in rows] == [
        "train", "embargoed", "validation", "holdout", "censored"
    ]
    assert rows[0]["dependence_group"] == "A"


def comparison_candidate(ident, kind, *, exit_price, base=None, adverse=None,
                         episodes=1, groups=1):
    validation = []
    holdout = []
    for index in range(episodes):
        mint = f"{ident}-{index}"
        validation.extend(round_trip(
            mint=mint, exit_price=exit_price, episode=f"{ident}-episode-{index}",
            group=f"{ident}-group-{index % groups}", split="validation",
        ))
        holdout.extend(round_trip(
            mint="h-" + mint, exit_price=exit_price,
            episode=f"h-{ident}-episode-{index}", group=f"h-{ident}-group-{index % groups}",
            split="holdout",
        ))
    return {
        "id": ident, "kind": kind, "label": ident,
        "events_validation": validation, "events_holdout": holdout,
        "policy": policy(starting_cash_micros=max(200_000_000, episodes * 100_000_000),
                         max_positions=max(2, episodes)),
        "base_costs": base or costs(), "adverse_costs": adverse or costs(),
        "execution": execution(), "episode_returns_validation_micros": [],
    }


def permissive_evidence(**changes):
    value = {
        "schema": EVIDENCE_POLICY_SCHEMA,
        "minimum_independent_episodes": 1,
        "minimum_coverage_ppm": 1_000_000,
        "minimum_improvement_micros": 1,
        "maximum_drawdown_ppm": 900_000,
        "minimum_stability_groups": 1,
        "require_nonnegative_adverse_difference": True,
        "uncertainty_minimum_independent_episodes": 30,
        "purpose": "fixture draft eligibility only",
    }
    value.update(changes)
    return value


def test_cash_is_selected_when_every_tradable_alternative_loses():
    report = compare_portfolios([
        comparison_candidate("current", "current", exit_price=900_000),
        comparison_candidate("variant", "variant", exit_price=950_000),
    ], evidence_policy=permissive_evidence(), input_lineage={"dataset_id": "same"})
    assert report["recommendation"] == {
        "action": "prefer_cash", "candidate_id": "cash",
        "reasons": ["all_evidence_measurable_trading_alternatives_lost_to_cash"],
    }
    assert report["baselines"][0]["eligible_outcome"] is True


def test_costs_can_reverse_a_gross_winner_and_adverse_stress_blocks_draft():
    high_cost = costs(fee_ppm=60_000)
    report = compare_portfolios([
        comparison_candidate("current", "current", exit_price=1_010_000,
                             base=high_cost, adverse=high_cost),
    ], evidence_policy=permissive_evidence(), input_lineage={"dataset_id": "fees"})
    current = report["validation_results"][0]
    assert current["base"]["fills"][-1]["price_micros_per_token"] > 1_000_000
    assert current["base"]["metrics"]["net_result_micros"] < 0
    assert report["recommendation"]["action"] == "prefer_cash"
    assert "adverse_scenario_loses_to_cash" in current["eligibility_blockers"]


def test_variant_selection_uses_validation_then_reports_bounded_holdout():
    report = compare_portfolios([
        comparison_candidate("current", "current", exit_price=1_050_000),
        comparison_candidate("variant", "variant", exit_price=1_200_000),
        comparison_candidate("holding", "holding", exit_price=1_100_000),
    ], evidence_policy=permissive_evidence(), input_lineage={"dataset_id": "selection"})
    assert report["recommendation"]["action"] == "eligible_inactive_draft"
    assert report["recommendation"]["candidate_id"] == "variant"
    assert {row["candidate_id"] for row in report["holdout_results"]} == {
        "current", "variant", "holding"
    }
    assert report["holdout_policy"]["holdout_used_for_selection"] is False
    assert report["submission_enabled"] is False


def test_holding_is_a_selection_hurdle_never_a_draft_candidate():
    report = compare_portfolios([
        comparison_candidate("current", "current", exit_price=1_050_000),
        comparison_candidate("variant", "variant", exit_price=1_200_000),
        comparison_candidate("holding", "holding", exit_price=1_300_000),
    ], evidence_policy=permissive_evidence(), input_lineage={"dataset_id": "holding-hurdle"})
    assert report["recommendation"] == {
        "action": "keep_current", "candidate_id": "current",
        "reasons": ["no_bounded_variant_cleared_current_cash_and_holding_benchmarks"],
    }


def test_default_policy_keeps_tiny_synthetic_samples_exploratory():
    report = compare_portfolios([
        comparison_candidate("current", "current", exit_price=1_200_000),
    ], input_lineage={"dataset_id": "tiny"})
    assert report["recommendation"]["action"] == "insufficient_evidence"
    assert "insufficient_independent_episodes" in report["validation_results"][0]["eligibility_blockers"]
    assert report["validation_results"][0]["uncertainty"] is None


def test_comparison_limits_variants_and_rejects_changed_units():
    candidates = [comparison_candidate("current", "current", exit_price=1_100_000)]
    candidates.extend(
        comparison_candidate(f"v{index}", "variant", exit_price=1_100_000)
        for index in range(6)
    )
    with pytest.raises(PortfolioReplayError, match="at most five"):
        compare_portfolios(candidates, input_lineage={"dataset": "x"})
    bad = policy()
    bad["capital_unit"] = "usd-float"
    with pytest.raises(PortfolioReplayError, match="quote-micros"):
        replay_portfolio([], policy=bad, costs=costs(), execution=execution())


def test_report_changes_when_assumptions_change_but_repeat_is_exact():
    events = round_trip(exit_price=1_100_000)
    first = replay_portfolio(events, policy=policy(), costs=costs(), execution=execution())
    assert first == replay_portfolio(
        copy.deepcopy(events), policy=copy.deepcopy(policy()),
        costs=copy.deepcopy(costs()), execution=copy.deepcopy(execution()),
    )
    changed = replay_portfolio(
        events, policy=policy(), costs=costs(spread_ppm=1), execution=execution(),
    )
    assert changed["id"] != first["id"]
    assert changed["costs_assumption"] != first["costs_assumption"]
