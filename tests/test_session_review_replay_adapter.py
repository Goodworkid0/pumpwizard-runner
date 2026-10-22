from __future__ import annotations

import copy

import pytest

from pumpwizard_runner.session_review import (
    REPORT_SCHEMA,
    REPLAY_REPORT_SCHEMA,
    REPLAY_REQUEST_SCHEMA,
    SessionReviewError,
    compare_portfolio_replay_request,
    create_inactive_draft_request,
    report_compatibility,
)
from pumpwizard_runner.strategies import parse_package
from test_portfolio_replay import comparison_candidate, permissive_evidence
from test_session_review import _package


def request():
    current_package = _package()
    variant_package = _package(minimum_growth_fraction=0.0125)
    current = comparison_candidate("current", "current", exit_price=1_050_000)
    current.update({
        "strategy_package": current_package,
        "strategy_sha256": parse_package(current_package).digest,
    })
    variant = comparison_candidate("variant", "variant", exit_price=1_200_000)
    variant.update({
        "strategy_package": variant_package,
        "strategy_sha256": parse_package(variant_package).digest,
    })
    return {
        "schema": REPLAY_REQUEST_SCHEMA,
        "input_lineage": {
            "dataset_id": "dataset-v2", "source_session_id": "source-session",
            "manifest_sha256": "34" * 32,
        },
        "evidence_policy": permissive_evidence(),
        "candidates": [current, variant],
    }


def test_v3_adapter_preserves_exact_packages_and_builds_zero_authority_draft():
    report = compare_portfolio_replay_request(request())
    assert report["schema"] == REPLAY_REPORT_SCHEMA
    assert report["model_label"] == "finite-capital event-ledger v3"
    assert report["recommendation"]["action"] == "eligible_inactive_draft"
    assert report["funded_performance"] is False
    assert report["submission_enabled"] is False
    draft = create_inactive_draft_request(report, candidate_id="variant", accepted=True)
    assert draft["source_dataset_id"] == "dataset-v2"
    assert draft["source_session_id"] == "source-session"
    assert draft["strategy_sha256"] == parse_package(_package(minimum_growth_fraction=0.0125)).digest
    assert draft["requested_session_state"] == {
        "status": "paused", "monitoring_enabled": False,
        "entry_mode": "disabled", "exit_mode": "manual",
        "wallet_allocation_lamports": "0", "copy_execution_authorization": False,
    }


def test_v3_adapter_rejects_changed_package_and_requires_explicit_acceptance():
    changed = request()
    changed["candidates"][1]["strategy_sha256"] = "00" * 32
    with pytest.raises(SessionReviewError, match="identity changed"):
        compare_portfolio_replay_request(changed)
    report = compare_portfolio_replay_request(request())
    with pytest.raises(SessionReviewError, match="explicit acceptance"):
        create_inactive_draft_request(report, candidate_id="variant", accepted=False)
    with pytest.raises(SessionReviewError, match="selected"):
        create_inactive_draft_request(report, candidate_id="current", accepted=True)


def test_report_compatibility_never_relabels_v1_as_portfolio_replay():
    legacy = report_compatibility({"schema": REPORT_SCHEMA})
    assert legacy == {
        "readable": True, "legacy": True,
        "model_label": "fixed-horizon per-signal modeled v1",
        "finite_capital": False,
        "notice": "Historical v1 evidence is preserved and is not a portfolio replay.",
    }
    modern = report_compatibility(compare_portfolio_replay_request(request()))
    assert modern["legacy"] is False and modern["finite_capital"] is True
    prior = report_compatibility({"schema": "pumpwizard.session-review-replay-report/v2"})
    assert prior["readable"] is True
    assert prior["legacy"] is True
    assert "predates corrected" in prior["notice"]


def test_request_schema_and_candidate_limit_remain_strict():
    invalid = request()
    invalid["extra"] = True
    with pytest.raises(SessionReviewError, match="fields"):
        compare_portfolio_replay_request(invalid)
    unsupported = request()
    unsupported["candidates"][1]["strategy_package"] = copy.deepcopy(_package())
    unsupported["candidates"][1]["strategy_package"]["configuration"]["extra_rule"] = 1
    unsupported["candidates"][1]["strategy_sha256"] = parse_package(_package()).digest
    with pytest.raises((SessionReviewError, ValueError)):
        compare_portfolio_replay_request(unsupported)
