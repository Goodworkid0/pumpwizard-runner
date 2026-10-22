"""Deterministic matched-window performance snapshots for forward campaigns."""
from __future__ import annotations

from collections import defaultdict
import copy
import hashlib
import json
import math
import time
from typing import Any

from .forward_campaigns import ForwardCampaignError, ForwardCampaignStore
from .portfolio_replay import ENGINE_VERSION, quote_price_units
from .service_recorder import ServiceEvidenceRecorder
from .strategies import canonical_json


REPORT_SCHEMA = "pumpwizard.forward-performance-report/v1"
INPUT_SCHEMA = "pumpwizard.forward-performance-input/v1"
REPORT_ENGINE_VERSION = "pumpwizard-forward-performance/1"
MAX_REPORT_OBSERVATIONS_PER_ARM = 10_000
MAX_LEDGER_ROWS_PER_ARM = 500


def _build_campaign_report_legacy(
    campaigns: ForwardCampaignStore,
    recorder: ServiceEvidenceRecorder,
    campaign_id: str,
    *,
    previous_report_id: str | None = None,
    created_at: float | None = None,
) -> dict[str, Any]:
    """Build and persist one append-only report from already recorded evidence."""
    campaign = campaigns.campaign(campaign_id, recorder)
    now = float(time.time() if created_at is None else created_at)
    if not math.isfinite(now) or now < 0:
        raise ForwardCampaignError("Campaign report time is invalid")
    histories = {}
    for arm in campaign["arms"]:
        run_id = arm["paper_run_id"]
        count = int(campaigns.db.execute(
            "SELECT COUNT(*) FROM paper_observations WHERE run_id=?", (run_id,)
        ).fetchone()[0])
        if count > MAX_REPORT_OBSERVATIONS_PER_ARM:
            raise ForwardCampaignError(
                f"Campaign report arm exceeds {MAX_REPORT_OBSERVATIONS_PER_ARM} observations"
            )
        histories[arm["id"]] = _history(campaigns, run_id)
    evidence_sets = {
        arm_id: {row["evidence_id"] for row in history["observations"]}
        for arm_id, history in histories.items()
    }
    eligible_sets = {
        arm_id: {
            row["evidence_id"] for row in history["observations"]
            if row.get("quality", {}).get("status") == "eligible"
        }
        for arm_id, history in histories.items()
    }
    matched_ids = set.intersection(*evidence_sets.values()) if evidence_sets else set()
    matched_eligible_ids = set.intersection(*eligible_sets.values()) if eligible_sets else set()
    all_ids = set.union(*evidence_sets.values()) if evidence_sets else set()
    matched_rows = _matched_rows(histories, matched_ids)
    periods = [row["first_received_at"] for row in matched_rows]
    lineage = {
        "schema": INPUT_SCHEMA,
        "campaign_id": campaign["id"],
        "campaign_request_sha256": campaign["request_sha256"],
        "campaign_version": campaign["version"],
        "paper_runs": [{
            "arm_id": arm["id"], "paper_run_id": arm["paper_run_id"],
            "strategy_sha256": arm["strategy_sha256"],
            "evaluator_report_id": arm.get("evaluator_report_id"),
            "cursor": arm["cursor"],
        } for arm in campaign["arms"]],
        "matched_evidence_sha256": _digest(sorted(matched_ids)),
        "matched_evidence_count": len(matched_ids),
        "matched_eligible_evidence_count": len(matched_eligible_ids),
        "operating_intervals": copy.deepcopy(campaign["operating_intervals"]),
        "provider_request_count": int(campaign["usage"].get("requests") or 0),
        "as_of": now,
    }
    arms = [
        _arm_result(arm, histories[arm["id"]], matched_ids, campaign["provider_cost"])
        for arm in campaign["arms"]
    ]
    cash = _cash_benchmark(campaign["arms"], periods)
    holding = _holding_benchmark(campaign, matched_rows)
    identical_source_coverage = all(values == matched_ids for values in evidence_sets.values())
    identical_eligible_coverage = all(
        values == matched_eligible_ids for values in eligible_sets.values()
    )
    gaps = _gap_summary(histories)
    readiness = _readiness(
        campaign, matched_rows, matched_eligible_ids, gaps,
        identical_source_coverage, identical_eligible_coverage,
    )
    comparable = (
        identical_source_coverage and identical_eligible_coverage
        and all(row["valuation_complete"] for row in arms)
    )
    ranking = []
    if comparable:
        ranking = [row["id"] for row in sorted(
            [cash, holding, *arms],
            key=lambda row: (
                -int(row["net_result_micros"])
                if row.get("net_result_micros") is not None else 10**30,
                row["id"],
            ),
        ) if row.get("net_result_micros") is not None]
    provider_history = recorder.provider_history(campaign_id=campaign["id"], limit=100)
    core = {
        "schema": REPORT_SCHEMA,
        "engine_version": REPORT_ENGINE_VERSION,
        "replay_engine_version": ENGINE_VERSION,
        "campaign_id": campaign["id"],
        "campaign_request_sha256": campaign["request_sha256"],
        "previous_report_id": previous_report_id,
        "created_at": now,
        "period": {
            "first_known_at": min(periods) if periods else None,
            "last_known_at": max(periods) if periods else None,
            "actual_observed_seconds": (
                max(periods) - min(periods) if len(periods) > 1 else 0.0
            ),
        },
        "input_lineage": lineage,
        "matched_coverage": {
            "identical_source_evidence": identical_source_coverage,
            "identical_decision_eligible_evidence": identical_eligible_coverage,
            "union_evidence_count": len(all_ids),
            "matched_evidence_count": len(matched_ids),
            "matched_fraction": len(matched_ids) / len(all_ids) if all_ids else 0.0,
            "gaps": gaps,
        },
        "benchmarks": [cash, holding],
        "arms": arms,
        "ranking": {
            "comparable": comparable, "ordered_ids": ranking,
            "reason": (
                "Matched evidence and complete conservative valuations."
                if comparable else
                "Ranking withheld because arm coverage or valuation is not comparable."
            ),
        },
        "provider_cost": copy.deepcopy(campaign["provider_cost"]),
        "provider_requests": provider_history["records"],
        "daily": _daily(matched_rows),
        "readiness": readiness,
        "receipt_attribution": {
            "class": "recorded-execution-external-evidence",
            "records": [], "matched_to_paper": False,
            "notice": "No receipt source was requested for this campaign snapshot.",
        },
        "authority": copy.deepcopy(campaign["authority"]),
        "claims": {
            "prospective": True, "funded_performance": False,
            "profitability_established": False,
            "fully_costed": campaign["provider_cost"]["fully_costed_profitability_allowed"],
        },
        "limits": [
            "Paper fills are modeled and are not transaction receipts.",
            "Short or correlated evidence remains exploratory.",
            "Unknown provider or valuation costs prevent a fully-costed profitability claim.",
        ],
    }
    ident = "campaign-report-" + _digest(core)[:32]
    report = {**core, "id": ident, "reproducibility_sha256": _digest(core)}
    return campaigns.save_report(campaign["id"], report)


def _history(campaigns: ForwardCampaignStore, run_id: str) -> dict[str, Any]:
    observations = campaigns.db.execute(
        """SELECT record_json FROM paper_observations WHERE run_id=?
           ORDER BY first_received_at,evidence_id""",
        (run_id,),
    ).fetchall()
    decisions = campaigns.db.execute(
        """SELECT record_json FROM paper_decisions WHERE run_id=?
           ORDER BY decided_at,decision_key LIMIT ?""",
        (run_id, MAX_LEDGER_ROWS_PER_ARM),
    ).fetchall()
    gaps = campaigns.db.execute(
        """SELECT record_json FROM paper_gaps WHERE run_id=?
           ORDER BY started_at,gap_id LIMIT ?""",
        (run_id, MAX_LEDGER_ROWS_PER_ARM),
    ).fetchall()
    row = campaigns.db.execute(
        "SELECT evaluator_report_json FROM paper_runs WHERE id=?", (run_id,)
    ).fetchone()
    return {
        "observations": [json.loads(item["record_json"]) for item in observations],
        "decisions": [json.loads(item["record_json"]) for item in decisions],
        "gaps": [json.loads(item["record_json"]) for item in gaps],
        "evaluator_report": json.loads(row["evaluator_report_json"]),
    }


def _matched_rows(histories: dict[str, dict[str, Any]],
                  matched_ids: set[str]) -> list[dict[str, Any]]:
    if not histories:
        return []
    first = next(iter(histories.values()))["observations"]
    return [row for row in first if row["evidence_id"] in matched_ids]


def _arm_result(arm: dict[str, Any], history: dict[str, Any], matched_ids: set[str],
                provider_cost: dict[str, Any]) -> dict[str, Any]:
    report = history["evaluator_report"]
    replay = report.get("replay", {})
    metrics = replay.get("metrics", {})
    starting = int(replay.get("capital", {}).get(
        "starting_cash_micros", round(float(arm["simulated_capital_usd"]) * 1_000_000)
    ))
    costs = replay.get("cost_breakdown", {})
    total_costs = sum(int(value) for value in costs.values()
                      if isinstance(value, int) and not isinstance(value, bool))
    arm_evidence = {row["evidence_id"] for row in history["observations"]}
    return {
        "id": arm["id"], "kind": "strategy_arm", "paper_run_id": arm["paper_run_id"],
        "strategy_sha256": arm["strategy_sha256"], "capital_unit": "quote-micros",
        "starting_capital_micros": starting,
        "ending_capital_micros": metrics.get("conservative_ending_equity_micros", starting),
        "net_result_micros": metrics.get("net_result_micros", 0),
        "realized_pnl_micros": metrics.get("realized_pnl_micros", 0),
        "known_unrealized_pnl_micros": metrics.get("known_unrealized_pnl_micros", 0),
        "unknown_unpriced_basis_micros": metrics.get("unknown_unpriced_basis_micros", 0),
        "valuation_complete": bool(metrics.get("valuation_complete", True)),
        "maximum_drawdown_ppm": metrics.get("maximum_drawdown_ppm", 0),
        "time_weighted_exposure_ppm": metrics.get("time_weighted_exposure_ppm", 0),
        "opportunities": metrics.get("opportunities", 0),
        "independent_episode_count": metrics.get("independent_episode_count", 0),
        "coverage_ppm": metrics.get("coverage_ppm", 0),
        "modeled_costs_micros": total_costs, "cost_breakdown": costs,
        "provider_cost_status": provider_cost["cost_status"],
        "matched_evidence_count": len(arm_evidence & matched_ids),
        "observation_count": len(arm_evidence),
        "decisions": history["decisions"][:MAX_LEDGER_ROWS_PER_ARM],
        "fills": replay.get("fills", [])[:MAX_LEDGER_ROWS_PER_ARM],
        "open_positions": list(replay.get("open_positions", {}).values())[:100],
        "equity_curve": replay.get("equity_curve", [])[:MAX_LEDGER_ROWS_PER_ARM],
    }


def _cash_benchmark(arms: list[dict[str, Any]], periods: list[float]) -> dict[str, Any]:
    starting = int(round(float(arms[0]["simulated_capital_usd"]) * 1_000_000)) if arms else 0
    return {
        "id": "cash", "kind": "cash", "capital_unit": "quote-micros",
        "starting_capital_micros": starting, "ending_capital_micros": starting,
        "net_result_micros": 0, "valuation_complete": True,
        "maximum_drawdown_ppm": 0, "time_weighted_exposure_ppm": 0,
        "matched_evidence_count": len(periods),
    }


def _holding_benchmark(campaign: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    starting = int(round(float(campaign["arms"][0]["simulated_capital_usd"]) * 1_000_000))
    priority = campaign["source"].get("priority_mints", [])
    mint = priority[0] if priority else (rows[0]["mint"] if rows else None)
    quotes = [row for row in rows if row.get("mint") == mint
              and row.get("quality", {}).get("status") == "eligible"]
    try:
        first = quote_price_units(quotes[0]["payload"]["price_usd"], "holding first price")
        last = quote_price_units(quotes[-1]["payload"]["price_usd"], "holding last price")
        ending = starting * last // first
        status = "priced"
    except (IndexError, KeyError, TypeError, ValueError):
        first = last = ending = None
        status = "unpriced"
    return {
        "id": "holding", "kind": "holding", "mint": mint,
        "capital_unit": "quote-micros", "starting_capital_micros": starting,
        "ending_capital_micros": ending,
        "net_result_micros": ending - starting if ending is not None else None,
        "valuation_complete": ending is not None, "valuation_status": status,
        "first_price_quote_units_per_token": first,
        "last_price_quote_units_per_token": last,
        "matched_evidence_count": len(quotes),
        "assumption": "Buy-and-hold benchmark over the same first-known evidence; no live fill implied.",
    }


def _gap_summary(histories: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for arm_id, history in histories.items():
        for gap in history["gaps"]:
            key = str(gap["gap_id"])
            entry = grouped.setdefault(key, {
                "id": key, "started_at": gap["started_at"], "ended_at": gap["ended_at"],
                "reason": gap["reason"], "arm_ids": [],
            })
            entry["arm_ids"].append(arm_id)
    return [grouped[key] for key in sorted(grouped)]


def _readiness(campaign: dict[str, Any], rows: list[dict[str, Any]],
               eligible_ids: set[str], gaps: list[dict[str, Any]],
               identical: bool, eligible_identical: bool) -> dict[str, Any]:
    policy = campaign["coverage"]
    tokens = {row["mint"] for row in rows if row["evidence_id"] in eligible_ids}
    duration = 0.0
    receipts = [float(row["first_received_at"]) for row in rows]
    if len(receipts) > 1:
        duration = max(receipts) - min(receipts)
    reasons = []
    if len(eligible_ids) < int(policy["minimum_observations"]):
        reasons.append("insufficient_matched_observations")
    if len(tokens) < int(policy["minimum_independent_tokens"]):
        reasons.append("insufficient_independent_tokens")
    if duration < float(campaign["duration_seconds"]):
        reasons.append("planned_observation_duration_not_reached")
    if gaps:
        reasons.append("operating_or_provider_gaps_present")
    if not identical or not eligible_identical:
        reasons.append("arm_coverage_mismatch")
    if campaign["provider_cost"]["cost_status"] != "known":
        reasons.append("provider_cost_unknown_or_partial")
    return {
        "state": "ready_for_review" if not reasons else "exploratory",
        "reasons": reasons, "matched_observations": len(eligible_ids),
        "independent_tokens": len(tokens), "actual_duration_seconds": duration,
        "planned_duration_seconds": campaign["duration_seconds"],
        "uncertainty": (
            "No inferential interval is reported; independence and sample-size assumptions are unmet."
            if reasons else
            "Readiness gates passed, but observed paper evidence still does not prove future returns."
        ),
    }


def _daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[int(float(row["first_received_at"]) // 86_400)].append(row)
    return [{
        "day_start_at": day * 86_400,
        "observation_count": len(values),
        "independent_tokens": len({row["mint"] for row in values}),
        "first_known_at": min(float(row["first_received_at"]) for row in values),
        "last_known_at": max(float(row["first_received_at"]) for row in values),
    } for day, values in sorted(groups.items())]


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


# The campaign owner originally landed the narrow integration above while the
# full accounting engine was still arriving.  Keep its compatibility fields for
# callers, but route new snapshots through the pure, exact-unit report engine.
def build_campaign_report(
    campaigns: ForwardCampaignStore,
    recorder: ServiceEvidenceRecorder,
    campaign_id: str,
    *,
    previous_report_id: str | None = None,
    created_at: float | None = None,
) -> dict[str, Any]:
    from .forward_performance import (
        build_forward_performance_report,
        performance_input_from_campaign,
    )
    from .receipt_evidence import ReceiptEvidenceError, read_trade_journal_receipts

    campaign = campaigns.campaign(campaign_id, recorder)
    now = float(time.time() if created_at is None else created_at)
    if not math.isfinite(now) or now <= float(campaign["start_at"]):
        raise ForwardCampaignError("Campaign report time must follow campaign Start")
    histories = {
        arm["paper_run_id"]: campaigns.paper.history(arm["paper_run_id"])
        for arm in campaign["arms"]
    }
    try:
        receipts = read_trade_journal_receipts(campaigns.root, as_of=now)
    except ReceiptEvidenceError:
        receipts = None
    report_input = performance_input_from_campaign(
        campaign,
        histories,
        as_of=now,
        prior_report_ids=[previous_report_id] if previous_report_id else [],
        correction_of_report_id=previous_report_id,
        receipt_report=receipts,
    )
    report = build_forward_performance_report(report_input)

    evidence_sets = {
        arm["id"]: {
            row["evidence_id"] for row in histories[arm["paper_run_id"]]["observations"]
        }
        for arm in campaign["arms"]
    }
    eligible_sets = {
        arm["id"]: {
            row["evidence_id"]
            for row in histories[arm["paper_run_id"]]["observations"]
            if row.get("quality", {}).get("decision_eligible") is True
        }
        for arm in campaign["arms"]
    }
    matched = set.intersection(*evidence_sets.values()) if evidence_sets else set()
    matched_eligible = set.intersection(*eligible_sets.values()) if eligible_sets else set()
    union = set.union(*evidence_sets.values()) if evidence_sets else set()
    first_history = histories[campaign["arms"][0]["paper_run_id"]]
    matched_rows = [
        row for row in first_history["observations"] if row["evidence_id"] in matched
    ]
    simple_holding = _holding_benchmark(campaign, matched_rows)
    holding_arm = next(row for row in report["arms"] if row["kind"] == "holding")
    if holding_arm["metrics"] is not None:
        simple_holding.update(
            ending_capital_micros=holding_arm["metrics"][
                "conservative_ending_equity_micros"
            ],
            net_result_micros=holding_arm["metrics"]["net_modeled_pnl_micros"],
            valuation_complete=holding_arm["metrics"]["valuation_complete"],
            valuation_status=(
                "priced" if holding_arm["metrics"]["valuation_complete"]
                else "unpriced"
            ),
            assumption=(
                "Compatibility projection of the exact holding replay over matched "
                "first-known evidence; no live fill implied."
            ),
        )
    cash_arm = next(row for row in report["arms"] if row["kind"] == "cash")
    cash_metrics = cash_arm["metrics"]
    cash = {
        "id": "cash", "kind": "cash", "capital_unit": "quote-micros",
        "starting_capital_micros": cash_arm["declared_capital_micros"],
        "ending_capital_micros": cash_metrics["conservative_ending_equity_micros"],
        "net_result_micros": 0, "valuation_complete": True,
        "maximum_drawdown_ppm": 0, "time_weighted_exposure_ppm": 0,
        "matched_evidence_count": len(matched),
    }
    comparable = report["matched_comparison"]["status"] == "comparable"
    ordered = [row["arm_id"] for row in report["matched_comparison"]["ranked"]]
    provider_history = recorder.provider_history(campaign_id=campaign["id"], limit=100)
    compatibility = {
        "campaign_request_sha256": campaign["request_sha256"],
        "period": {
            "first_known_at": min(
                (row["first_received_at"] for row in matched_rows), default=None
            ),
            "last_known_at": max(
                (row["first_received_at"] for row in matched_rows), default=None
            ),
            "actual_observed_seconds": (
                max(row["first_received_at"] for row in matched_rows)
                - min(row["first_received_at"] for row in matched_rows)
                if len(matched_rows) > 1 else 0
            ),
        },
        "input_lineage": report_input["lineage"],
        "matched_coverage": {
            "identical_source_evidence": all(rows == matched for rows in evidence_sets.values()),
            "identical_decision_eligible_evidence": all(
                rows == matched_eligible for rows in eligible_sets.values()
            ),
            "union_evidence_count": len(union),
            "matched_evidence_count": len(matched),
            "matched_fraction": len(matched) / len(union) if union else 0,
            "gaps": copy.deepcopy(report["gaps"]),
        },
        "benchmarks": [cash, simple_holding],
        "provider_cost": copy.deepcopy(campaign["provider_cost"]),
        "provider_requests": provider_history["records"],
        "receipt_attribution": {
            "class": "recorded-execution-external-evidence",
            "records": copy.deepcopy(report["receipts"]),
            "matched_to_paper": False,
            "notice": "Receipt evidence remains separate from modeled campaign fills.",
        },
        "claims": {
            "prospective": True,
            "funded_performance": False,
            "profitability_established": False,
            "fully_costed": report["provider_costs"]["fully_costed"],
        },
    }
    readiness = copy.deepcopy(report["readiness"])
    if not all(rows == matched for rows in evidence_sets.values()) or not all(
        rows == matched_eligible for rows in eligible_sets.values()
    ):
        readiness["reasons"] = sorted(set(readiness["reasons"]) | {"arm_coverage_mismatch"})
    core = {
        **report,
        **compatibility,
        "readiness": readiness,
        "ranking": {
            **report["ranking"],
            "comparable": comparable,
            "ordered_ids": ordered,
        },
    }
    core.pop("id", None)
    core.pop("reproducibility_sha256", None)
    digest = _digest(core)
    final = {
        **core,
        "id": "campaign-report-" + digest[:32],
        "reproducibility_sha256": digest,
    }
    return campaigns.save_report(campaign["id"], final)
