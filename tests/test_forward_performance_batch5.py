from __future__ import annotations

import copy

import pytest

from pumpwizard_runner.forward_performance import (
    ACCOUNTING_INPUT_SCHEMA,
    INPUT_SCHEMA,
    READINESS_POLICY_SCHEMA,
    ForwardPerformanceError,
    build_forward_performance_report,
    performance_input_from_campaign,
    reconcile_accounting_snapshot,
)
from pumpwizard_runner.forward_campaigns import ForwardCampaignStore, START_CONFIRMATION
from pumpwizard_runner.paper_research import PaperResearchWorker
from pumpwizard_runner.portfolio_replay import ENGINE_VERSION, replay_portfolio
from pumpwizard_runner.receipt_evidence import normalize_retained_receipts
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder
from test_portfolio_replay import costs, execution, exit_signal, policy, quote, signal
from test_forward_campaigns import Clock, quote as market_quote, request as campaign_request


DIGEST = "ab" * 32
HYPOTHESIS = "cd" * 32
SOURCE = "ef" * 32


def replay(*, exit_price=900_000, stress=False):
    events = [
        quote("q0", 0),
        signal("signal", 0, evidence=["q0"]),
        quote("fill", 1),
        exit_signal("exit", 2),
        quote("exit-fill", 3, price=exit_price),
    ]
    selected_costs = costs(fee_ppm=50_000) if stress else costs()
    return replay_portfolio(
        events,
        policy=policy(),
        costs=selected_costs,
        execution=execution(),
    )


def readiness(**changes):
    result = {
        "schema": READINESS_POLICY_SCHEMA,
        "registered_at": 0,
        "minimum_independent_episodes": 1,
        "minimum_independent_tokens": 1,
        "minimum_observed_seconds": 5,
        "minimum_coverage_ppm": 1_000_000,
        "minimum_stability_groups": 1,
        "maximum_gap_seconds": 5,
        "uncertainty_minimum_independent_episodes": 3,
        "required_engine_version": ENGINE_VERSION,
    }
    result.update(changes)
    return result


def arm(ident, kind, report=None, *, end=10, stress_reports=None):
    result = {
        "id": ident,
        "kind": kind,
        "label": ident,
        "declared_capital_micros": 200_000_000,
        "operating_intervals": [{
            "id": "interval-" + ident,
            "started_at": 0,
            "ended_at": end,
            "reason": "fixture operation",
        }],
        "gaps": [],
        "matched_evidence_ids": ["q0", "fill", "exit-fill"],
        "independent_episode_returns_micros": [-10_000_000],
    }
    if kind not in {"cash"}:
        result.update(
            run_id="run-" + ident,
            strategy_sha256=DIGEST,
            report=report,
        )
    if stress_reports is not None:
        result["stress_reports"] = stress_reports
    return result


def performance_input(*, uneven=False, known_cost=False):
    losing = replay(exit_price=900_000)
    holding = replay(exit_price=950_000)
    stress = replay(exit_price=900_000, stress=True)
    arms = [
        arm("paper", "observed_paper", losing, stress_reports=[{
            "id": "higher-cost",
            "kind": "cost",
            "assumption_provenance": "preregistered configured estimate",
            "report": stress,
        }, {
            "id": "latency-boundary",
            "kind": "latency",
            "assumption_provenance": "fixture retained availability-time scenario",
            "report": losing,
        }]),
        arm("cash", "cash", end=9 if uneven else 10),
        arm("holding", "holding", holding),
    ]
    provider = {
        "status": "known" if known_cost else "unknown",
        "currency": "USD",
        "observed_charges_micros": 10_000 if known_cost else None,
        "configured_estimates_micros": 0 if known_cost else None,
        "shared_fixed_cost_micros": 0 if known_cost else None,
        "unknown_components": [] if known_cost else ["provider request price"],
        "allocation_by_arm_micros": {"paper": 10_000} if known_cost else {},
    }
    return {
        "schema": INPUT_SCHEMA,
        "campaign": {
            "id": "campaign-1",
            "manifest_sha256": DIGEST,
            "quote_currency": "USD",
        },
        "snapshot": {
            "as_of": 10,
            "window_start": 0,
            "window_end": 10,
            "revision": 1,
            "correction_of_report_id": None,
        },
        "lineage": {
            "source_snapshot_sha256": SOURCE,
            "selection_hypothesis_sha256": HYPOTHESIS,
            "experiment_id": "experiment-1",
            "prior_report_ids": [],
            "holdout_status": "not_applicable",
        },
        "readiness_policy": readiness(),
        "arms": arms,
        "provider_costs": provider,
    }


def test_matched_losing_arms_prefer_cash_and_unknown_provider_cost_blocks_claim():
    source = performance_input()
    report = build_forward_performance_report(source)
    assert report == build_forward_performance_report(copy.deepcopy(source))
    comparison = report["matched_comparison"]
    assert comparison["status"] == "comparable"
    assert comparison["common_operating_seconds"] == 10
    assert comparison["recommendation"] == {
        "action": "prefer_cash",
        "reasons": ["all_matched_trading_arms_lost_to_cash"],
        "arm_id": "cash",
    }
    assert report["profitability"] == {
        "status": "not_fully_costed",
        "claim_supported": False,
        "blockers": ["provider_price_or_cost_unknown"],
    }
    paper = next(row for row in report["arms"] if row["id"] == "paper")
    assert paper["metrics"]["net_modeled_pnl_micros"] == -10_000_000
    assert paper["metrics"]["turnover_micros"] == 190_000_000
    assert paper["stress"][0]["history_mutated"] is False
    assert paper["stress"][0]["difference_from_observed_history_micros"] < 0
    assert paper["stress"][1]["kind"] == "latency"
    assert paper["stress"][1]["difference_from_observed_history_micros"] == 0
    assert paper["readiness"]["status"] == "evidence_ready_for_review"
    assert paper["readiness"]["uncertainty"] is None
    assert paper["readiness"]["uncertainty_withheld_reason"] == (
        "insufficient_independent_episodes"
    )
    assert report["authority"] == {
        "wallet_allocation": False,
        "signer": False,
        "orders": False,
        "submission": False,
    }


def test_uneven_uptime_withholds_ranking_instead_of_using_full_history():
    report = build_forward_performance_report(performance_input(uneven=True))
    assert report["matched_comparison"]["status"] == "withheld"
    assert "mismatched_arm_operating_intervals" in report["matched_comparison"]["blockers"]
    assert report["matched_comparison"]["ranked"] == []
    assert report["matched_comparison"]["recommendation"]["action"] == "withhold_ranking"
    assert report["matched_comparison"]["common_operating_seconds"] == 9


def test_cost_provenance_correction_and_hypothesis_history_are_enforced():
    source = performance_input(known_cost=True)
    report = build_forward_performance_report(source)
    assert report["provider_costs"]["fully_costed"] is True
    assert report["profitability"]["claim_supported"] is True
    paper = next(row for row in report["matched_comparison"]["ranked"] if row["arm_id"] == "paper")
    assert paper["net_after_allocated_provider_cost_micros"] == -10_010_000

    changed = copy.deepcopy(source)
    changed["lineage"]["selection_hypothesis_sha256"] = "12" * 32
    changed["lineage"]["supersedes_hypothesis_sha256"] = HYPOTHESIS
    with pytest.raises(ForwardPerformanceError, match="preserve prior report history"):
        build_forward_performance_report(changed)

    changed["lineage"]["prior_report_ids"] = [report["id"]]
    changed["snapshot"].update(revision=2, correction_of_report_id=report["id"])
    corrected = build_forward_performance_report(changed)
    assert corrected["id"] != report["id"]
    assert corrected["snapshot"]["correction_of_report_id"] == report["id"]


def accounting_input(*, transferred_basis=None, low_price=False):
    ending_cash = 1_115_000
    mark = {
        "status": "priced",
        "mint": "B",
        "decimals": 0 if not low_price else 18,
        "quote_currency": "USD",
        "price_quote_units_per_token": 2 * 10**18 if not low_price else 1,
        "as_of": 100,
    }
    quantity = 10 if not low_price else 1
    transfer = {
        "id": "transfer-in",
        "kind": "transfer_in",
        "mint": "B",
        "decimals": mark["decimals"],
        "quantity_delta_atoms": quantity,
    }
    if transferred_basis is not None:
        transfer["basis_delta_micros"] = transferred_basis
    return {
        "schema": ACCOUNTING_INPUT_SCHEMA,
        "account_id": "fixture-account",
        "as_of": 100,
        "quote_currency": "USD",
        "starting_cash_micros": 1_000_000,
        "ending_cash_micros": ending_cash,
        "fills": [
            {
                "id": "entry",
                "side": "entry",
                "mint": "A",
                "decimals": 0,
                "quantity_delta_atoms": 100,
                "cash_delta_micros": -400_000,
                "basis_delta_micros": 400_000,
                "cost_micros": 10_000,
            },
            {
                "id": "partial-exit",
                "side": "exit",
                "mint": "A",
                "decimals": 0,
                "quantity_delta_atoms": -40,
                "cash_delta_micros": 200_000,
                "basis_delta_micros": -160_000,
                "cost_micros": 5_000,
            },
            {
                "id": "final-exit",
                "side": "exit",
                "mint": "A",
                "decimals": 0,
                "quantity_delta_atoms": -60,
                "cash_delta_micros": 270_000,
                "basis_delta_micros": -240_000,
                "cost_micros": 5_000,
            },
            {
                "id": "failed-fee",
                "side": "failed_fee",
                "quantity_delta_atoms": 0,
                "cash_delta_micros": -5_000,
                "cost_micros": 5_000,
            },
        ],
        "external_flows": [
            {"id": "deposit", "kind": "deposit", "cash_delta_micros": 100_000},
            {"id": "withdrawal", "kind": "withdrawal", "cash_delta_micros": -50_000},
            transfer,
        ],
        "ending_holdings": [
            {
                "mint": "A",
                "decimals": 0,
                "quantity_atoms": 0,
                "cost_basis_micros": 0,
            },
            {
                "mint": "B",
                "decimals": mark["decimals"],
                "quantity_atoms": quantity,
                "cost_basis_micros": transferred_basis,
                "mark": mark,
            },
        ],
    }


def test_hand_calculated_partial_final_failed_fee_and_external_flow_accounting():
    report = reconcile_accounting_snapshot(accounting_input())
    assert report["cash"] == {
        "starting_cash_micros": 1_000_000,
        "strategy_cash_delta_micros": 65_000,
        "external_cash_flow_micros": 50_000,
        "expected_ending_cash_micros": 1_115_000,
        "reported_ending_cash_micros": 1_115_000,
        "conserved": True,
    }
    assert report["performance"]["realized_pnl_micros"] == 65_000
    assert report["performance"]["partial_exits"] == 1
    assert report["performance"]["final_exits"] == 1
    assert report["performance"]["strategy_cost_micros"] == 25_000
    assert report["performance"]["external_flows_count_as_strategy_return"] is False
    assert report["performance"]["strategy_net_result_micros"] is None
    assert report["conservation"]["cash"] is True
    assert report["conservation"]["token_atoms"] is True
    assert report["conservation"]["cost_basis"] is False
    assert "cost_basis_missing_or_inconsistent" in report["conservation"]["blockers"]


def test_presentation_retains_receipt_units_and_external_flow_profit_treatment():
    source = performance_input(known_cost=True)
    source["accounting_reconciliation"] = reconcile_accounting_snapshot(
        accounting_input(transferred_basis=15_000_000)
    )
    source["receipt_lineage"] = normalize_retained_receipts([{
        "id": "failed-fee", "genesis": "fixture-genesis", "sender": "wallet",
        "signature": "failed-fee-signature", "status": "failed", "created_at": 9,
        "payload": {
            "network": "mainnet-beta", "side": "buy", "mint": "MintA",
            "decimals": 6,
        },
        "receipt": {
            "err": {"InstructionError": [0, "Custom"]}, "block_time": 9,
            "fee_lamports": 5000, "wallet_delta": -5000, "token_delta": 0,
        },
    }], as_of=10)
    report = build_forward_performance_report(source)
    receipt = report["receipts"][0]
    assert receipt["classification"] == "fee_only_failure"
    assert receipt["amounts"] == {
        "fee_lamports": 5000,
        "cash_delta_lamports": -5000,
        "token_delta_atoms": 0,
    }
    assert receipt["units"]["cash"] == "lamports"
    assert receipt["units"]["fee"] == "lamports"
    assert receipt["strategy_profit_treatment"] == "fee_cost_only"
    assert report["reconciliation"]["external_cash_flow_micros"] == 50_000
    assert report["reconciliation"]["external_cash_flow_currency"] == "USD"
    assert report["reconciliation"]["strategy_profit_treatment"] == "excluded"


def test_exact_price_units_value_inventory_and_low_value_remains_unknown():
    complete = reconcile_accounting_snapshot(accounting_input(transferred_basis=15_000_000))
    holding = next(row for row in complete["holdings"] if row["mint"] == "B")
    assert holding["value_micros"] == 20_000_000
    assert complete["conservation"]["complete"] is True
    assert complete["performance"]["strategy_net_result_micros"] == 5_065_000

    low = reconcile_accounting_snapshot(accounting_input(transferred_basis=0, low_price=True))
    assert low["unpriced_holdings"] == [{
        "mint": "B",
        "reason": "value_below_quote-micro_precision",
    }]
    assert low["performance"]["strategy_net_result_micros"] is None
    assert "ending_valuation_incomplete" in low["conservation"]["blockers"]


def test_transferred_out_inventory_removes_atoms_and_known_external_basis():
    source = accounting_input(transferred_basis=15_000_000)
    source["external_flows"].append({
        "id": "transfer-out",
        "kind": "transfer_out",
        "mint": "B",
        "decimals": 0,
        "quantity_delta_atoms": -4,
        "basis_delta_micros": -6_000_000,
    })
    source["ending_holdings"][1].update(
        quantity_atoms=6,
        cost_basis_micros=9_000_000,
    )
    report = reconcile_accounting_snapshot(source)
    holding = next(row for row in report["holdings"] if row["mint"] == "B")
    assert holding["quantity_atoms"] == holding["expected_quantity_atoms"] == 6
    assert holding["value_micros"] == 12_000_000
    assert report["performance"]["external_token_basis_flow_micros"] == 9_000_000
    assert report["performance"]["strategy_net_result_micros"] == 3_065_000
    assert report["conservation"]["complete"] is True


def test_stale_mark_and_cash_mismatch_never_become_zero_or_profit():
    source = accounting_input(transferred_basis=1)
    source["ending_cash_micros"] += 1
    source["ending_holdings"][1]["mark"] = {
        **source["ending_holdings"][1]["mark"],
        "status": "stale",
        "reason": "stale_mark",
    }
    report = reconcile_accounting_snapshot(source)
    assert report["cash"]["conserved"] is False
    assert report["unpriced_holdings"] == [{"mint": "B", "reason": "stale_mark"}]
    assert report["performance"]["strategy_net_result_micros"] is None
    assert report["evidence_semantics"]["unknown_values_are_zero"] is False


def test_real_campaign_histories_build_save_and_restore_dashboard_report(tmp_path):
    clock = Clock(1_000)
    with ForwardCampaignStore(tmp_path, clock=clock) as campaigns:
        campaign = campaigns.start(
            campaign_request(),
            idempotency_key="performance-campaign",
            confirmation=START_CONFIRMATION,
        )
    recorder = ServiceEvidenceRecorder(tmp_path, clock=clock)
    worker = PaperResearchWorker(tmp_path, recorder, clock=clock)
    try:
        for observed_at, received_at, price in (
            (1_000, 1_001, 1.00),
            (1_010, 1_011, 1.01),
            (1_025, 1_026, 1.05),
        ):
            scan_id = recorder.begin_scan(
                minimum_cap=1_000,
                maximum_cap=50_000,
                source="FixtureSource",
                priority=["MintCampaign"],
                limit=24,
                campaign_consumers={campaign["id"]: 2},
                requested_at=observed_at,
                started_at=observed_at,
            )
            recorder.complete_scan(
                scan_id,
                [market_quote(observed_at, price=price)],
                received_at=received_at,
            )
            clock.value = received_at + 0.5
            worker.run_once()
        as_of = 1_030
        clock.value = as_of
        with ForwardCampaignStore(tmp_path, clock=clock) as campaigns:
            current = campaigns.campaign(campaign["id"], recorder)
            histories = {
                arm["paper_run_id"]: campaigns.paper.history(arm["paper_run_id"])
                for arm in current["arms"]
            }
            report_input = performance_input_from_campaign(
                current, histories, as_of=as_of
            )
            report = build_forward_performance_report(report_input)
            saved = campaigns.save_report(campaign["id"], report)
            assert saved["campaign_id"] == campaign["id"]
            assert saved["manifest_digest"] == campaign["request_sha256"]
            assert saved["comparisons"]
            assert {row["kind"] for row in saved["comparisons"]} == {
                "current", "arm", "cash", "holding",
            }
            assert saved["authority"]["submission"] is False
            report_id = saved["id"]
    finally:
        worker.close()
        recorder.close()

    with ForwardCampaignStore(tmp_path, clock=clock) as reopened:
        restored = reopened.report(report_id)
        assert restored == report
        assert reopened.campaign(campaign["id"])["progress"]["reports"] == 1
