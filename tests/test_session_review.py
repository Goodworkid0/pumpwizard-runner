from __future__ import annotations

import copy
import json

import pytest

from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.session_review import (
    DRAFT_SCHEMA,
    REPORT_SCHEMA,
    SessionReviewError,
    SessionReviewStore,
    capture_decision_features,
    compare_dataset,
    create_inactive_draft_request,
    freeze_dataset,
    generate_candidates,
)
from pumpwizard_runner.strategies import parse_package


SESSION_ID = "session-review-fixture"


def _package(**changes):
    configuration = copy.deepcopy(CONFIG)
    configuration.update({
        "observation_seconds": 60,
        "minimum_samples": 4,
        "minimum_price_changes": 2,
        "minimum_growth_fraction": 0.01,
        "maximum_peak_drawdown": 0.35,
        "max_sample_gap_seconds": 30,
        "quote_max_age_seconds": 30,
        "failed_attempt_probability": 0,
    })
    configuration.update(changes)
    return {
        "schema": "pumpwizard.strategy-handoff/v1",
        "strategy": {"id": "review-fixture", "label": "Review fixture"},
        "configuration": configuration,
    }


def _quote(mint, at, start, *, route="curve", pool=None, gap_price=0):
    elapsed = at - start
    price_native = 1 + elapsed / 3_000 + gap_price
    return {
        "instance_id": SESSION_ID,
        "mint": mint,
        "symbol": mint[-2:],
        "observed_at": at,
        "route": route,
        "pool": pool or f"pool-{mint}",
        "market_cap_usd": 15_000 * price_native,
        "price_usd": 100 * price_native,
        "price_native": price_native,
        "liquidity_usd": 10_000 + elapsed * 10,
        "liquidity_native": 100 + elapsed / 10,
        "source": "synthetic-test",
        "quote_usd": 100,
        "base_reserve": 1_000_000,
        "reserve_usd": 10_000 + elapsed * 10,
        "real_token_reserve": 500_000,
        "supply": 1_000_000_000,
        "decimals": 6,
        "source_at": at,
        "fx_at": at,
    }


def _evidence(*, route_change=False):
    observations = []
    # The 7,900 and 10,380 starts deliberately create signals whose fixed
    # horizons cross the deterministic train/validation boundaries.
    starts = (1_000, 2_600, 4_200, 5_800, 7_400, 7_900, 9_000, 10_380, 10_600)
    for index, start in enumerate(starts):
        mint = f"Mint{index:02d}"
        for at in range(start, start + 2_201, 20):
            route = "amm" if route_change and index == 3 and at >= start + 300 else "curve"
            pool = f"amm-{mint}" if route == "amm" else f"pool-{mint}"
            observations.append(_quote(mint, at, start, route=route, pool=pool))
    decisions = [{
        "instance_id": SESSION_ID,
        "decision_key": "decision-1",
        "at": 1_060,
        "kind": "entry_recommended",
        "mint": "Mint00",
        "reason": "strategy_entry_signal",
        "payload": {"route": "curve", "pool": "pool-Mint00"},
    }, {
        "instance_id": SESSION_ID,
        "decision_key": "decision-2",
        "at": 2_660,
        "kind": "entry_blocked",
        "mint": "Mint01",
        "reason": "Liquidity or modeled entry costs fail the screen",
        "payload": {},
    }]
    outcomes = [{
        "instance_id": SESSION_ID,
        "decision_key": "decision-1",
        "decision_at": 1_060,
        "status": "scored",
        "basis": "counterfactual_retained_quotes",
        "horizon_seconds": 300,
        "coverage": "continuous",
        "decision_return_fraction": 0.04,
    }, {
        "instance_id": SESSION_ID,
        "decision_key": "decision-2",
        "decision_at": 2_660,
        "status": "unknown",
        "basis": "counterfactual_retained_quotes",
        "horizon_seconds": 300,
        "coverage": "missing_horizon_quote",
    }]
    features = [{
        "schema": "pumpwizard.session-decision-features/v1",
        "instance_id": SESSION_ID,
        "decision_key": "decision-1",
        "captured_at_decision": True,
        "captured_at": 1_061,
        "decision_at": 1_060,
        "mint": "Mint00",
        "strategy_sha256": parse_package(_package()).digest,
        "engine_version": "fixture/1",
        "features": {"growth": 0.02, "samples": 4},
        "provenance": {
            "observation_times": [1_000, 1_020, 1_040, 1_060],
            "future_observations_included": False,
        },
    }]
    return observations, decisions, outcomes, features


def _freeze(store, *, package=None, route_change=False):
    package = package or _package()
    observations, decisions, outcomes, features = _evidence(route_change=route_change)
    return freeze_dataset(
        store,
        source_session_id=SESSION_ID,
        strategy_package=package,
        strategy_digest=parse_package(package).digest,
        observations=observations,
        decisions=decisions,
        outcomes=outcomes,
        feature_records=features,
    )


def test_freezes_immutable_identity_and_keeps_missing_features_incomplete(tmp_path):
    with SessionReviewStore(tmp_path) as store:
        first = _freeze(store)
        second = _freeze(store)

        assert first == second
        assert first["id"] == first["data_sha256"]
        assert first["evidence"] == {
            "observations": 999,
            "decisions": 2,
            "decisions_with_recorded_features": 1,
            "decisions_with_incomplete_features": 1,
            "eligible_decision_capture": "historical_transition_records_only",
            "scored_outcomes": 1,
            "unknown_outcomes": 1,
            "scope": "source_session_only",
            "outcome_basis": "counterfactual_retained_quotes_not_fills_or_net_returns",
        }
        missing = next(row for row in first["decisions"] if row["decision_key"] == "decision-2")
        assert missing["feature_provenance"]["status"] == "incomplete"
        assert "not_recorded_at_decision_time" in missing["feature_provenance"]["reason"]
        first["observations"][0]["price_usd"] = 0
        assert store.dataset(second["id"])["observations"][0]["price_usd"] > 0


def test_freeze_rejects_stale_digest_and_outside_session_evidence(tmp_path):
    package = _package()
    observations, decisions, outcomes, features = _evidence()
    with SessionReviewStore(tmp_path) as store:
        with pytest.raises(SessionReviewError, match="stale or mismatched"):
            freeze_dataset(
                store,
                source_session_id=SESSION_ID,
                strategy_package=package,
                strategy_digest="0" * 64,
                observations=observations,
                decisions=decisions,
                outcomes=outcomes,
                feature_records=features,
            )
        observations[0]["instance_id"] = "another-session"
        with pytest.raises(SessionReviewError, match="another session"):
            freeze_dataset(
                store,
                source_session_id=SESSION_ID,
                strategy_package=package,
                strategy_digest=parse_package(package).digest,
                observations=observations,
                decisions=decisions,
                outcomes=outcomes,
                feature_records=features,
            )


def test_candidate_search_is_bounded_and_uses_existing_package_parser():
    candidates = generate_candidates(_package())

    assert candidates[0]["id"] == "current"
    assert 2 <= len(candidates) <= 6
    assert len({row["package_sha256"] for row in candidates}) == len(candidates)
    for candidate in candidates:
        assert parse_package(candidate["package"]).digest == candidate["package_sha256"]


def test_feature_capture_is_at_decision_time_and_rejects_future_quotes():
    package = _package()
    observations = [_quote("Mint00", at, 1_000) for at in (1_000, 1_020, 1_040, 1_060)]
    decision = {
        "at": 1_060,
        "kind": "entry_recommended",
        "mint": "Mint00",
        "reason": "strategy_entry_signal",
        "payload": {},
    }

    captured = capture_decision_features(
        package,
        decision_key="decision-1",
        decision=decision,
        observations=observations,
        engine_version="decision-test/1",
        captured_at=1_061,
    )

    assert captured["captured_at_decision"] is True
    assert captured["feature_vector"]["sample_count"] == 4
    assert captured["feature_vector"]["growth_fraction"] == pytest.approx(0.02)
    assert captured["provenance"]["future_observations_included"] is False
    assert captured["strategy_sha256"] == parse_package(package).digest

    with pytest.raises(SessionReviewError, match="future observation"):
        capture_decision_features(
            package,
            decision_key="decision-1",
            decision=decision,
            observations=[*observations, _quote("Mint00", 1_080, 1_000)],
            engine_version="decision-test/1",
            captured_at=1_061,
        )


def test_comparison_is_deterministic_and_embargoes_split_crossing_signals(tmp_path):
    with SessionReviewStore(tmp_path) as store:
        dataset = _freeze(store)
        first = compare_dataset(store, dataset["id"], horizon_seconds=300)
        second = compare_dataset(store, dataset["id"], horizon_seconds=300)

        assert first == second
        assert first["schema"] == REPORT_SCHEMA
        assert first["funded_performance"] is False
        assert first["submission_enabled"] is False
        assert first["results"][0]["candidate_id"] == "baseline-no-entry"
        assert sum(row["embargoed_signals"] for row in first["results"][1:]) > 0
        boundaries = first["inputs"]["split_boundaries"]
        for result in first["results"][1:]:
            for outcome in result["splits"]["train"]["outcomes"]:
                assert outcome["due_at"] + 120 < boundaries["train_end_at"]
            for outcome in result["splits"]["validation"]["outcomes"]:
                assert outcome["at"] >= boundaries["train_end_at"]
                assert outcome["due_at"] + 120 < boundaries["validation_end_at"]
            for outcome in result["splits"]["holdout"]["outcomes"]:
                assert outcome["at"] >= boundaries["validation_end_at"]
                assert outcome["due_at"] + 120 <= boundaries["last_at"]


def test_exploratory_comparison_never_reads_or_locks_holdout(tmp_path, monkeypatch):
    import pumpwizard_runner.session_review as review

    seen_last_times = []
    original_signals = review._signals

    def recording_signals(package, observations, cancelled):
        seen_last_times.append(max(row["observed_at"] for row in observations))
        return original_signals(package, observations, cancelled)

    monkeypatch.setattr(review, "_signals", recording_signals)
    with SessionReviewStore(tmp_path) as store:
        dataset = _freeze(store)
        report = compare_dataset(
            store, dataset["id"], horizon_seconds=300, evaluate_holdout=False,
        )
        boundary = report["inputs"]["split_boundaries"]["validation_end_at"]

        assert report["holdout_state"] == "not_evaluated"
        assert report["inputs"]["holdout_evaluated"] is False
        assert all("holdout" not in row["splits"] for row in report["results"])
        assert "not evaluated or exported" in report["limitations"][-1]
        assert seen_last_times and all(value < boundary for value in seen_last_times)
        assert store.reports(SESSION_ID) == []

        # Validation-only searches do not consume or lock the dataset holdout.
        changed = compare_dataset(
            store, dataset["id"], horizon_seconds=600, evaluate_holdout=False,
        )
        assert changed["holdout_state"] == "not_evaluated"

        exposed = compare_dataset(store, dataset["id"], horizon_seconds=300)
        assert exposed["holdout_state"] == "exposed"
        with pytest.raises(SessionReviewError, match="holdout was already exposed"):
            compare_dataset(store, dataset["id"], horizon_seconds=600)

        # Historical v1 bytes stay untouched while reads gain the truthful
        # exposure label that older payloads did not contain.
        historical = copy.deepcopy(exposed)
        historical.pop("holdout_state")
        raw = json.dumps(historical, sort_keys=True, separators=(",", ":"))
        store._connection.execute(
            "UPDATE reports SET payload_json=? WHERE id=?", (raw, exposed["id"])
        )
        store._connection.commit()
        assert store.report(exposed["id"])["holdout_state"] == "exposed"
        saved = store._connection.execute(
            "SELECT payload_json FROM reports WHERE id=?", (exposed["id"],)
        ).fetchone()
        assert "holdout_state" not in json.loads(saved["payload_json"])


def test_future_holdout_prices_cannot_change_validation_selection(tmp_path):
    package = _package()
    with SessionReviewStore(tmp_path) as store:
        original = _freeze(store, package=package)
        first = compare_dataset(store, original["id"], horizon_seconds=300)
        holdout_at = first["inputs"]["split_boundaries"]["validation_end_at"]

        observations, decisions, outcomes, features = _evidence()
        for row in observations:
            if row["observed_at"] >= holdout_at:
                row["price_usd"] *= 0.5
                row["price_native"] *= 0.5
                row["market_cap_usd"] *= 0.5
        changed = freeze_dataset(
            store,
            source_session_id=SESSION_ID,
            strategy_package=package,
            strategy_digest=parse_package(package).digest,
            observations=observations,
            decisions=decisions,
            outcomes=outcomes,
            feature_records=features,
        )
        second = compare_dataset(store, changed["id"], horizon_seconds=300)

        assert second["recommendation"] == first["recommendation"]
        assert [row["splits"]["validation"] for row in second["results"]] == [
            row["splits"]["validation"] for row in first["results"]
        ]


def test_explicit_costs_penalize_the_smaller_order_more(tmp_path):
    with SessionReviewStore(tmp_path) as store:
        dataset = _freeze(store)
        report = compare_dataset(store, dataset["id"], horizon_seconds=300)

        current = next(row for row in report["results"] if row["candidate_id"] == "current")
        smaller = next(row for row in report["results"]
                       if row["label"] == "Smaller modeled order")
        assert smaller["cost_model"]["fixed_cost_usd_each_side"] == 0.1
        compared = 0
        for name in ("train", "validation", "holdout"):
            current_mean = current["splits"][name]["metrics"]["mean_modeled_return_fraction"]
            smaller_mean = smaller["splits"][name]["metrics"]["mean_modeled_return_fraction"]
            if current_mean is not None and smaller_mean is not None:
                compared += 1
                assert smaller_mean < current_mean
        assert compared > 0


def test_route_change_stays_unknown_in_modeled_replay(tmp_path):
    with SessionReviewStore(tmp_path) as store:
        dataset = _freeze(store, route_change=True)
        report = compare_dataset(store, dataset["id"], horizon_seconds=300)

        current = next(row for row in report["results"] if row["candidate_id"] == "current")
        all_outcomes = [outcome for split in current["splits"].values()
                        for outcome in split["outcomes"]]
        changed = [row for row in all_outcomes if row["mint"] == "Mint03"]
        assert changed
        assert {row["status"] for row in changed} == {"unknown"}
        assert {row["coverage"] for row in changed} == {"route_or_pool_changed"}


def test_missing_horizon_quotes_stay_unknown_in_modeled_replay(tmp_path):
    package = _package()
    observations, decisions, outcomes, features = _evidence()
    observations = [row for row in observations if not (
        row["mint"] == "Mint02" and 4_440 <= row["observed_at"] <= 4_680
    )]
    with SessionReviewStore(tmp_path) as store:
        dataset = freeze_dataset(
            store,
            source_session_id=SESSION_ID,
            strategy_package=package,
            strategy_digest=parse_package(package).digest,
            observations=observations,
            decisions=decisions,
            outcomes=outcomes,
            feature_records=features,
        )
        report = compare_dataset(store, dataset["id"], horizon_seconds=300)

        current = next(row for row in report["results"] if row["candidate_id"] == "current")
        all_outcomes = [outcome for split in current["splits"].values()
                        for outcome in split["outcomes"]]
        missing = [row for row in all_outcomes if row["mint"] == "Mint02"]
        assert missing
        unknown = [row for row in missing if row["status"] == "unknown"]
        assert unknown
        assert {row["coverage"] for row in unknown} == {"missing_horizon_quote"}


def test_cancelled_comparison_does_not_publish_report_and_can_restart(tmp_path):
    calls = 0

    def cancel_after_work():
        nonlocal calls
        calls += 1
        return calls > 20

    with SessionReviewStore(tmp_path) as store:
        dataset = _freeze(store)
        with pytest.raises(SessionReviewError, match="cancelled"):
            compare_dataset(store, dataset["id"], horizon_seconds=300,
                            cancelled=cancel_after_work)
        assert store.reports(SESSION_ID) == []

        completed = compare_dataset(store, dataset["id"], horizon_seconds=300)
        assert store.reports(SESSION_ID) == [completed]


def test_exposed_holdout_rejects_a_different_search_on_same_dataset(tmp_path):
    with SessionReviewStore(tmp_path) as store:
        dataset = _freeze(store)
        compare_dataset(store, dataset["id"], horizon_seconds=300)

        with pytest.raises(SessionReviewError, match="holdout was already exposed"):
            compare_dataset(store, dataset["id"], horizon_seconds=600)


def test_explicit_acceptance_produces_only_an_inactive_unfunded_draft_request(tmp_path):
    with SessionReviewStore(tmp_path) as store:
        dataset = _freeze(store)
        report = compare_dataset(store, dataset["id"], horizon_seconds=300)
        candidate = next(row for row in report["candidate_packages"]
                         if row["kind"] == "candidate")

        with pytest.raises(SessionReviewError, match="explicit acceptance"):
            create_inactive_draft_request(report, candidate_id=candidate["id"], accepted=False)
        with pytest.raises(SessionReviewError, match="does not recommend"):
            create_inactive_draft_request(report, candidate_id=candidate["id"], accepted=True)
        report["recommendation"] = {
            "action": "create_inactive_draft",
            "candidate_id": candidate["id"],
            "reason": "Synthetic selected-candidate contract fixture.",
        }
        request = create_inactive_draft_request(
            report, candidate_id=candidate["id"], accepted=True
        )

        assert request["schema"] == DRAFT_SCHEMA
        assert request["source_session_id"] == SESSION_ID
        assert request["strategy_sha256"] == candidate["package_sha256"]
        assert request["requested_session_state"] == {
            "status": "paused",
            "monitoring_enabled": False,
            "entry_mode": "disabled",
            "exit_mode": "manual",
            "wallet_allocation_lamports": "0",
            "copy_execution_authorization": False,
        }


def test_unsupported_strategy_family_has_a_clear_limit(tmp_path):
    package = {
        "schema": "pumpwizard.strategy/v2",
        "strategy": {
            "id": "native-review",
            "label": "Native review",
            "version": "1.0.0",
            "engine": "pumpwizard-core/1",
        },
        "market": {"chain": "solana", "observation_seconds": 60},
        "rules": {
            "entry_style": "momentum",
            "entry_cap_min_usd": 10_000,
            "entry_cap_max_usd": 25_000,
            "minimum_growth_fraction": 0.01,
            "minimum_reserve_growth_usd": 10,
            "take_profit_fraction": 0.2,
            "stop_loss_fraction": -0.1,
            "max_hold_seconds": 3_600,
        },
    }
    with pytest.raises(SessionReviewError, match="ladder-v1"):
        generate_candidates(package)
