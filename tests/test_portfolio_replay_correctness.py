from __future__ import annotations

import copy

import pytest

from pumpwizard_runner.paper_evaluation import IncrementalPaperEvaluator, PaperEvaluationError
from pumpwizard_runner.portfolio_replay import (
    ENGINE_VERSION,
    MAX_TOKEN_ATOMS,
    PPM,
    PortfolioReplay,
    PortfolioReplayError,
    quote_amount_micros,
    quote_price_fields,
    quote_price_units,
    replay_portfolio,
)
from test_portfolio_replay import (
    clock,
    costs,
    execution,
    exit_signal,
    policy,
    quote,
    signal,
)


STRATEGY = "56" * 32


def sourced_quote(
    ident: str,
    event_at: float,
    first_known_at: float,
    *,
    source_at: float | None = None,
    basis: str = "independent",
    mint: str = "A",
    price: int = 1_000_000,
    cap: int = 10_000_000_000,
    decimals: int = 0,
    route: str = "curve",
    pool: str = "pool-a",
    migration: str | None = None,
) -> dict:
    event = quote(
        ident, event_at, mint, price=price, cap=cap, decimals=decimals,
        route=route, pool=pool, migration=migration,
    )
    event["first_known_at"] = first_known_at
    event["source_time_basis"] = basis
    if source_at is not None:
        event["source_at"] = source_at
    elif basis == "unknown":
        event.pop("source_at", None)
    else:
        event["source_at"] = event_at
    return event


def low_quote(
    ident: str,
    at: float,
    *,
    usd_price: str,
    mint: str = "A",
    decimals: int = 0,
) -> dict:
    event = quote(ident, at, mint, decimals=decimals)
    event.pop("price_micros_per_token")
    event.update(quote_price_fields(usd_price))
    event.update(source_at=at, source_time_basis="independent")
    return event


def test_received_stale_quote_cannot_fill_entry_or_existing_position_exit():
    stale_only = sourced_quote("stale", 0, 3_600, source_at=0)
    report = replay_portfolio(
        [
            stale_only, signal("entry", 3_600, evidence=["stale"]),
            sourced_quote("later-fresh", 3_601, 3_601),
        ],
        policy=policy(), costs=costs(), execution=execution(),
    )
    assert report["fills"] == []
    assert [row["reason"] for row in report["exclusions"]] == [
        "stale_quote", "entry_signal_has_no_fresh_executable_quote_evidence",
    ]
    assert report["capital"]["reserved_micros"] == 0

    events = [
        sourced_quote("entry-q", 0, 0),
        signal("entry", 0, evidence=["entry-q"]),
        sourced_quote("high", 5, 5, price=2_000_000),
        sourced_quote(
            "stale-high", 6, 3_600, source_at=0, price=9_000_000,
            route="amm", pool="pool-b", migration="pool-a",
        ),
        exit_signal("exit", 3_600),
    ]
    report = replay_portfolio(
        events, policy=policy(max_quote_age_seconds=10, stale_exit_seconds=20),
        costs=costs(), execution=execution(),
    )
    assert [fill["side"] for fill in report["fills"]] == ["entry"]
    position = report["open_positions"]["A"]
    assert position["peak_price_quote_units_per_token"] == 2 * 10 ** 18
    assert (position["route"], position["pool"]) == ("curve", "pool-a")
    assert position["valuation_status"] == "unknown_stale_or_unverified_quote"
    assert report["capital"]["ending_cash_micros"] == 100_000_000


@pytest.mark.parametrize(
    ("fill_at", "filled"),
    [(9.999, True), (10, False), (10.001, False)],
)
def test_entry_expiry_boundary_is_strict_and_releases_once(fill_at, filled):
    events = [
        sourced_quote("q0", 0, 0),
        signal("s0", 0, evidence=["q0"]),
        sourced_quote("candidate", fill_at, fill_at),
        clock("after", 20),
    ]
    report = replay_portfolio(
        events,
        policy=policy(submission_delay_seconds=2, entry_expiry_seconds=10),
        costs=costs(), execution=execution(),
    )
    assert bool(report["fills"]) is filled
    assert report["capital"]["reserved_micros"] == 0
    if filled:
        assert report["orders"][0]["status"] == "filled"
        assert report["capital"]["ending_cash_micros"] == 100_000_000
    else:
        assert report["orders"][0]["status"] == "expired_unfilled"
        assert report["capital"]["ending_cash_micros"] == 200_000_000
        assert report["cash_ledger"] == []


def test_checkpoint_resume_expires_before_quote_fill_and_is_idempotent():
    selected = policy(submission_delay_seconds=2, entry_expiry_seconds=10)
    current = PortfolioReplay(selected, costs(), execution())
    current.apply(sourced_quote("q0", 0, 0))
    current.apply(signal("s0", 0, evidence=["q0"]))
    restored = PortfolioReplay(selected, costs(), execution(), checkpoint=current.checkpoint())
    transitions = restored.apply(sourced_quote("at-expiry", 10, 10))
    assert transitions == [{
        "action": "cancel_entry", "mint": "A", "reason": "expired_unfilled",
        "signal_id": "s0",
    }]
    assert restored.report()["capital"]["reserved_micros"] == 0
    checkpoint = restored.checkpoint()
    assert restored.apply(sourced_quote("at-expiry", 10, 10)) == []
    assert restored.checkpoint() == checkpoint


def test_source_time_basis_and_freshness_boundary_are_explicit():
    at_boundary = sourced_quote("boundary", 0, 10, source_at=0)
    accepted = replay_portfolio(
        [at_boundary, signal("s", 10, evidence=["boundary"])],
        policy=policy(max_quote_age_seconds=10), costs=costs(), execution=execution(),
    )
    assert [row["side"] for row in accepted["fills"]] == ["entry"]

    unknown = sourced_quote("unknown", 0, 0, basis="unknown")
    excluded = replay_portfolio(
        [unknown], policy=policy(), costs=costs(), execution=execution(),
    )
    assert excluded["exclusions"][0]["reason"] == "unknown_or_unverified_source_time"

    invalid = sourced_quote("bad", 0, 0, source_at=0, basis="made-up")
    with pytest.raises(PortfolioReplayError, match="source_time_basis"):
        replay_portfolio([invalid], policy=policy(), costs=costs(), execution=execution())

    future_source = sourced_quote("future-source", 0, 1, source_at=2)
    with pytest.raises(PortfolioReplayError, match="source time cannot be after"):
        replay_portfolio(
            [future_source], policy=policy(), costs=costs(), execution=execution(),
        )


def test_late_quote_is_excluded_and_clocked_hold_waits_for_a_fresh_fill_quote():
    events = [
        sourced_quote("q0", 0, 0), signal("s0", 0, evidence=["q0"]),
        sourced_quote("newer", 5, 5, price=2_000_000),
        sourced_quote("late", 4, 6, source_at=4, price=8_000_000),
        clock("hold", 100),
        sourced_quote("fresh-exit", 101, 101, price=2_000_000),
    ]
    report = replay_portfolio(
        events,
        policy=policy(max_quote_age_seconds=10, max_hold_seconds=100,
                      stale_exit_seconds=1_000, targets=[]),
        costs=costs(), execution=execution(),
    )
    assert any(row["reason"] == "late_or_replayed_quote" for row in report["exclusions"])
    assert report["fills"][-1]["reason"] == "max_hold"
    assert report["fills"][-1]["price_micros_per_token"] == 2_000_000
    assert report["capital"]["ending_cash_micros"] == 300_000_000
    assert report["metrics"]["realized_pnl_micros"] == 100_000_000


def test_fresh_quote_can_trigger_stop_while_stale_quote_cannot_trigger_or_mark():
    stale = sourced_quote("stale-stop", 1, 20, source_at=1, price=100_000)
    report = replay_portfolio(
        [quote("q0", 0), signal("s0", 0, evidence=["q0"]), stale],
        policy=policy(max_quote_age_seconds=10, targets=[]),
        costs=costs(), execution=execution(),
    )
    assert [row["side"] for row in report["fills"]] == ["entry"]
    assert report["open_positions"]["A"]["quantity_atoms"] == 100

    fresh = copy.deepcopy(stale)
    fresh.update(id="fresh-stop", event_at=1, first_known_at=1, source_at=1)
    report = replay_portfolio(
        [quote("q0", 0), signal("s0", 0, evidence=["q0"]), fresh],
        policy=policy(max_quote_age_seconds=10, targets=[]),
        costs=costs(), execution=execution(),
    )
    assert [row["side"] for row in report["fills"]] == ["entry", "exit"]
    assert report["fills"][-1]["reason"] == "stop_loss"


@pytest.mark.parametrize("decimals", [0, 6, 9, 18])
def test_one_nanodollar_price_round_trip_is_exact_for_supported_decimals(decimals):
    events = [
        low_quote("q0", 0, usd_price="0.000000001", decimals=decimals),
        signal("s0", 0, evidence=["q0"]),
        low_quote("q1", 1, usd_price="0.000000001", decimals=decimals),
        exit_signal("x", 1),
    ]
    report = replay_portfolio(
        events, policy=policy(), costs=costs(), execution=execution(),
    )
    scale = 10 ** decimals
    assert [fill["quantity_atoms"] for fill in report["fills"]] == [
        100_000_000_000 * scale,
        100_000_000_000 * scale,
    ]
    assert report["capital"]["ending_cash_micros"] == 200_000_000
    assert report["metrics"]["realized_pnl_micros"] == 0
    assert not report["open_positions"]


def test_low_price_costed_round_trip_and_partial_exit_conserve_cash_and_atoms():
    selected_costs = costs(fee_ppm=10_000)
    events = [
        low_quote("q0", 0, usd_price="0.000000001"),
        signal("s0", 0, evidence=["q0"]),
        exit_signal("half", 1, fraction=500_000),
        low_quote("q-half", 2, usd_price="0.000000002"),
        exit_signal("rest", 3),
        low_quote("q-rest", 4, usd_price="0.000000002"),
    ]
    report = replay_portfolio(
        events, policy=policy(targets=[]), costs=selected_costs, execution=execution(),
    )
    entry, first_exit, second_exit = report["fills"]
    assert entry["quantity_atoms"] == 99_000_000_000
    assert [first_exit["quantity_atoms"], second_exit["quantity_atoms"]] == [
        49_500_000_000, 49_500_000_000,
    ]
    assert [first_exit["gross_micros"], second_exit["gross_micros"]] == [
        99_000_000, 99_000_000,
    ]
    assert [first_exit["cash_delta_micros"], second_exit["cash_delta_micros"]] == [
        98_010_000, 98_010_000,
    ]
    assert report["cost_breakdown"]["fees_micros"] == 2_980_000
    assert report["capital"]["ending_cash_micros"] == 296_020_000
    assert report["metrics"]["realized_pnl_micros"] == 96_020_000
    assert not report["open_positions"]


def test_price_validation_insufficient_size_overflow_route_and_fx_fail_closed():
    assert quote_price_units("0.000000001") == 1_000_000_000
    assert quote_price_units("1000000000000000000000000") == 10 ** 42
    assert quote_amount_micros("1000000000000000000") == 10 ** 24
    with pytest.raises(PortfolioReplayError):
        quote_price_units("1000000000000000000000001")
    with pytest.raises(PortfolioReplayError):
        quote_amount_micros("1000000000000000001")
    for value in (0, -1, "nan", "inf", "0.0000000000000000001"):
        with pytest.raises(PortfolioReplayError):
            quote_price_units(value)

    too_expensive = low_quote("expensive", 0, usd_price="1000000000000")
    no_atoms = replay_portfolio(
        [too_expensive, signal("no-atoms", 0, evidence=["expensive"])],
        policy=policy(), costs=costs(), execution=execution(),
    )
    assert no_atoms["fills"] == []
    assert no_atoms["orders"][0]["status"] == "rejected_zero_atoms"

    tiny = low_quote("tiny", 0, usd_price="0.000000000000000001", decimals=18)
    overflow = replay_portfolio(
        [tiny, signal("overflow", 0, evidence=["tiny"])],
        policy=policy(
            starting_cash_micros=10 ** 24, order_micros=10 ** 24,
        ),
        costs=costs(), execution=execution(),
    )
    assert overflow["fills"] == []
    assert overflow["orders"][0]["status"] == "rejected_quantity_overflow"
    assert overflow["capital"]["reserved_micros"] == 0
    assert MAX_TOKEN_ATOMS < 10 ** 54

    wrong_currency = low_quote("eur", 0, usd_price="0.000000001")
    wrong_currency["quote_currency"] = "EUR"
    fx = replay_portfolio(
        [wrong_currency], policy=policy(), costs=costs(), execution=execution(),
    )
    assert fx["exclusions"][0]["reason"] == "quote_currency_or_fx_basis_mismatch"

    events = [
        low_quote("start", 0, usd_price="0.000000001"),
        signal("s", 0, evidence=["start"]),
        {
            **low_quote("migration", 1, usd_price="0.000000001"),
            "route": "amm", "pool": "pool-b", "migration_from_pool": "pool-a",
        },
    ]
    migrated = replay_portfolio(
        events, policy=policy(), costs=costs(), execution=execution(),
    )
    assert migrated["open_positions"]["A"]["pool"] == "pool-b"


def test_exit_below_cash_ledger_precision_retains_atoms_and_unknown_value():
    entry = quote("entry", 0, price=100_000_000)
    tiny_exit = low_quote("tiny-exit", 1, usd_price="0.000000001")
    report = replay_portfolio(
        [entry, signal("s", 0, evidence=["entry"]), tiny_exit],
        policy=policy(targets=[]), costs=costs(), execution=execution(),
    )
    assert report["open_positions"]["A"]["quantity_atoms"] == 1
    assert report["open_positions"]["A"]["valuation_status"] == "unknown_below_currency_precision"
    assert report["fills"] == [report["fills"][0]]
    assert report["fills"][0]["side"] == "entry"
    assert report["exclusions"][-1]["reason"] == "exit_value_below_quote-micro_precision"
    assert report["metrics"]["valuation_complete"] is False


def test_entry_atom_rounding_keeps_unused_reserved_budget_as_cash():
    report = replay_portfolio(
        [quote("sixty", 0, price=60_000_000), signal("s", 0, evidence=["sixty"])],
        policy=policy(targets=[]), costs=costs(), execution=execution(),
    )
    fill = report["fills"][0]
    assert fill["quantity_atoms"] == 1
    assert fill["reserved_budget_micros"] == 100_000_000
    assert fill["fair_value_micros"] == 60_000_000
    assert fill["cash_delta_micros"] == -60_000_000
    assert fill["unused_budget_micros"] == 40_000_000
    assert report["capital"]["ending_cash_micros"] == 140_000_000
    assert report["open_positions"]["A"]["remaining_basis_micros"] == 60_000_000


def test_seeded_failure_model_applies_to_future_ids_and_is_restart_stable():
    scenario = execution(entry_failure_model={
        "method": "sha256-threshold-v1", "seed": "paper-run-1", "failure_ppm": PPM,
    })
    events = [quote("q", 0), signal("future-signal", 0, evidence=["q"])]
    batch = replay_portfolio(
        events, policy=policy(), costs=costs(), execution=scenario,
    )
    assert batch["fills"] == []
    assert batch["opportunities"][0]["status"] == "failed_entry"
    assert batch["cost_breakdown"]["failed_micros"] == 5_000

    current = IncrementalPaperEvaluator(
        run_id="paper-run-1", strategy_sha256=STRATEGY,
        policy=policy(), costs=costs(), execution=scenario,
    )
    current.advance(events[0])
    current = IncrementalPaperEvaluator(
        run_id="paper-run-1", strategy_sha256=STRATEGY,
        policy=policy(), costs=costs(), execution=scenario,
        checkpoint=current.checkpoint(),
    )
    current.advance(events[1])
    assert current.report()["replay"] == batch
    assert current.report()["decisions"][0]["outcome_status"] == "failed_entry"


def test_complex_batch_incremental_checkpoint_parity_includes_ledgers_and_exclusions():
    scenario = execution(
        failed_entry_signal_ids=["fail"],
        unavailable_exit_quote_ids=["unavailable"],
        exit_fill_ppm_by_quote_id={"partial": 500_000},
    )
    events = [
        quote("qa", 0, "A"), signal("fail", 0, "A", evidence=["qa"]),
        quote("qb", 1, "B"), signal("enter-b", 1, "B", evidence=["qb"]),
        exit_signal("exit-b", 2, "B"),
        quote("unavailable", 3, "B"), quote("partial", 4, "B"), quote("finish", 5, "B"),
        sourced_quote("received-stale", 6, 100, source_at=0, mint="B"),
    ]
    batch = replay_portfolio(
        events, policy=policy(targets=[]), costs=costs(), execution=scenario,
    )
    current = IncrementalPaperEvaluator(
        run_id="parity", strategy_sha256=STRATEGY,
        policy=policy(targets=[]), costs=costs(), execution=scenario,
    )
    for event in events:
        current.advance(copy.deepcopy(event))
        current = IncrementalPaperEvaluator(
            run_id="parity", strategy_sha256=STRATEGY,
            policy=policy(targets=[]), costs=costs(), execution=scenario,
            checkpoint=current.checkpoint(),
        )
    current.record_gap(gap_id="pause", started_at=101, ended_at=120, reason="paused")
    paper = current.report()
    assert paper["replay"] == batch
    assert paper["replay"]["cash_ledger"] == batch["cash_ledger"]
    assert paper["replay"]["position_ledger"] == batch["position_ledger"]
    assert paper["replay"]["exclusions"] == batch["exclusions"]
    assert paper["gaps"][-1]["id"] == "pause"
    assert paper["backfill_repairs_history"] is False


def test_v2_checkpoint_is_rejected_after_corrected_engine_version():
    current = PortfolioReplay(policy(), costs(), execution())
    checkpoint = current.checkpoint()
    checkpoint["schema"] = "pumpwizard.portfolio-replay-input/v2"
    checkpoint["engine_version"] = "pumpwizard-finite-capital-replay/2"
    with pytest.raises(PortfolioReplayError, match="Unsupported replay checkpoint"):
        PortfolioReplay(policy(), costs(), execution(), checkpoint=checkpoint)
    assert ENGINE_VERSION == "pumpwizard-finite-capital-replay/3"

    paper = IncrementalPaperEvaluator(
        run_id="old-paper", strategy_sha256=STRATEGY,
        policy=policy(), costs=costs(), execution=execution(),
    ).checkpoint()
    paper["schema"] = "pumpwizard.incremental-paper-state/v1"
    with pytest.raises(PaperEvaluationError, match="Unsupported paper checkpoint"):
        IncrementalPaperEvaluator(
            run_id="old-paper", strategy_sha256=STRATEGY,
            policy=policy(), costs=costs(), execution=execution(), checkpoint=paper,
        )
