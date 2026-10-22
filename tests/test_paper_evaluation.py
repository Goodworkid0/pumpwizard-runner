from __future__ import annotations

import copy

import pytest

from pumpwizard_runner.paper_evaluation import (
    IncrementalPaperEvaluator,
    PaperEvaluationError,
    evaluate_incrementally,
)
from pumpwizard_runner.portfolio_replay import replay_portfolio
from test_portfolio_replay import costs, execution, policy, round_trip


STRATEGY = "12" * 32


def evaluator(checkpoint=None):
    return IncrementalPaperEvaluator(
        run_id="paper-1", strategy_sha256=STRATEGY,
        policy=policy(), costs=costs(), execution=execution(), checkpoint=checkpoint,
    )


def test_incremental_evaluation_matches_batch_and_survives_every_event_restart():
    events = round_trip(exit_price=1_200_000)
    current = evaluator()
    for event in events:
        current.advance(event)
        current = evaluator(current.checkpoint())
    paper = current.report()
    batch = replay_portfolio(events, policy=policy(), costs=costs(), execution=execution())
    assert paper["replay"] == batch
    assert paper["decisions"][0]["recorded_before_outcome"] is True
    assert paper["decisions"][0]["outcome_status"] == "closed"
    assert paper["decisions"][0]["outcome_recorded_at"] > paper["decisions"][0]["decided_at"]
    assert paper["wallet_allocation_micros"] == 0
    assert paper["signing_authority"] is False
    assert paper["submission_enabled"] is False


def test_idempotent_observation_and_changed_duplicate_rejection():
    events = round_trip()
    current = evaluator()
    assert current.advance(events[0]) == []
    checkpoint = current.checkpoint()
    assert current.advance(copy.deepcopy(events[0])) == []
    assert current.checkpoint() == checkpoint
    changed = copy.deepcopy(events[0])
    changed["price_micros_per_token"] += 1
    with pytest.raises(PaperEvaluationError, match="reused with changed"):
        current.advance(changed)


def test_decision_cannot_reference_future_or_backfilled_evidence():
    events = round_trip()
    current = evaluator()
    with pytest.raises(PaperEvaluationError, match="not yet recorded"):
        current.advance(events[1])
    current.advance(events[0])
    later = copy.deepcopy(events[2])
    later["first_known_at"] = 5
    later["event_at"] = 1
    current.advance(later)
    backfill = copy.deepcopy(events[3])
    backfill["first_known_at"] = 4
    with pytest.raises(PaperEvaluationError, match="cannot be backfilled"):
        current.advance(backfill)


def test_gaps_are_durable_idempotent_and_never_repair_history():
    current = evaluator()
    current.record_gap(gap_id="gap-1", started_at=5, ended_at=10, reason="provider outage")
    current.record_gap(gap_id="gap-1", started_at=5, ended_at=10, reason="provider outage")
    restored = evaluator(current.checkpoint())
    assert restored.report()["gaps"] == [{
        "id": "gap-1", "started_at": 5, "ended_at": 10, "reason": "provider outage"
    }]
    assert restored.report()["backfill_repairs_history"] is False
    with pytest.raises(PaperEvaluationError, match="changed content"):
        restored.record_gap(gap_id="gap-1", started_at=5, ended_at=11, reason="provider outage")


def test_convenience_incremental_runner_is_reproducible():
    events = round_trip(exit_price=1_150_000)
    first = evaluate_incrementally(
        events, run_id="paper", strategy_sha256=STRATEGY,
        policy=policy(), costs=costs(), execution=execution(),
    )
    second = evaluate_incrementally(
        reversed(events), run_id="paper", strategy_sha256=STRATEGY,
        policy=policy(), costs=costs(), execution=execution(),
    )
    assert first == second
