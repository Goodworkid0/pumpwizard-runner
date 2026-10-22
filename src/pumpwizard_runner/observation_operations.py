"""Bounded preparation and operational accounting for observation campaigns."""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
import math
from typing import Any

from .forward_campaigns import (
    REQUEST_SCHEMA,
    RESOURCE_MODEL_SCHEMA,
    ForwardCampaignStore,
)
from .portfolio import CONFIG
from .service_recorder import (
    PROVIDER_REJECTION_CATEGORIES,
    ServiceEvidenceRecorder,
)
from .strategies import canonical_json, parse_package


SUMMARY_SCHEMA = "pumpwizard.observation-operational-summary/v1"
EXPORT_SCHEMA = "pumpwizard.observation-operational-export/v1"
PRESET_DURATION_SECONDS = 24 * 60 * 60
PRESET_CADENCE_SECONDS = 15 * 60
PRESET_PROVIDER_ATTEMPTS = PRESET_DURATION_SECONDS // PRESET_CADENCE_SECONDS
PUMPSWAP_72H_DURATION_SECONDS = 72 * 60 * 60
PUMPSWAP_72H_CADENCE_SECONDS = 15 * 60
PUMPSWAP_72H_PROVIDER_ATTEMPTS = (
    PUMPSWAP_72H_DURATION_SECONDS // PUMPSWAP_72H_CADENCE_SECONDS
)
PUMPSWAP_72H_MINT = "7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump"


def build_24_hour_request() -> dict[str, Any]:
    """Return the exact reviewed 24-hour manifest; this performs no network I/O."""
    packages = [
        _strategy("Batch 7 observed arm", growth=0.01),
        _strategy("Batch 7 strict comparator", growth=0.05),
    ]
    return {
        "schema": REQUEST_SCHEMA,
        "campaign_key": "batch7-24h-observation",
        "supersedes_campaign_id": None,
        "source": {
            "provider": "CompositePumpMarketSource",
            "minimum_cap": 1_000,
            "maximum_cap": 150_000,
            "priority_mints": [],
            "limit": 3,
            "cadence_seconds": PRESET_CADENCE_SECONDS,
        },
        "arms": [{
            "id": "observed" if index == 0 else "strict-comparator",
            "source_session_id": f"batch7-24h-session-{index + 1}",
            "strategy_sha256": parse_package(package).digest,
            "strategy_package": package,
            "simulated_capital_usd": 500,
        } for index, package in enumerate(packages)],
        "assumptions": {
            "cost_model": "exact frozen strategy package costs; provider billing unknown",
            "execution_model": "actual paper portfolio evaluator v3; no trading authority",
        },
        "coverage": {
            "max_source_age_seconds": 120,
            "minimum_independent_tokens": 2,
            "minimum_observations": 96,
            "maximum_gap_seconds": 30 * 60,
        },
        "duration_seconds": PRESET_DURATION_SECONDS,
        "quotas": {
            "max_requests": PRESET_PROVIDER_ATTEMPTS,
            "max_active_runtime_seconds": PRESET_DURATION_SECONDS,
            "max_storage_bytes": 64 * 1024 * 1024,
            "max_concurrency": 1,
        },
        "provider_cost": {
            "currency": "USD",
            "marginal_cost_per_request": None,
            "fixed_subscription_cost": None,
            "price_basis": "unknown",
        },
    }


def build_24_hour_resource_model() -> dict[str, Any]:
    """Reviewed bounds used by the zero-network preregistration estimate."""
    return {
        "schema": RESOURCE_MODEL_SCHEMA,
        "expected_http_reads_per_request": 3,
        "max_http_reads_per_request": 3,
        "estimated_provider_response_bytes_per_request": 256 * 1024,
        "max_provider_response_bytes_per_request": 3 * 1024 * 1024,
        "estimated_normalized_bytes_per_quote": 2 * 1024,
        "fixed_storage_overhead_bytes": 1024 * 1024,
    }


def build_pumpswap_72_hour_request() -> dict[str, Any]:
    """Freeze Session 1's exact read-only canonical PumpSwap campaign."""
    package = _pumpswap_strategy()
    return {
        "schema": REQUEST_SCHEMA,
        "campaign_key": "pumpswap-month-72h-canonical-standard-spl",
        "supersedes_campaign_id": None,
        "source": {
            "provider": "CanonicalPumpSwapMarketSource",
            "minimum_cap": 1,
            "maximum_cap": 1_000_000_000,
            "priority_mints": [PUMPSWAP_72H_MINT],
            "limit": 1,
            "cadence_seconds": PUMPSWAP_72H_CADENCE_SECONDS,
        },
        "arms": [{
            "id": "canonical-standard-spl-observed",
            "source_session_id": "pumpswap-month-session-1-72h",
            "strategy_sha256": parse_package(package).digest,
            "strategy_package": package,
            "simulated_capital_usd": 500,
        }],
        "assumptions": {
            "cost_model": (
                "exact frozen strategy costs; public RPC/provider billing remains unknown"
            ),
            "execution_model": (
                "existing paper evaluator v3 over canonical read-only PumpSwap state; "
                "no wallet, signer, orders or submission"
            ),
            "universe_enforcement": "priority-only",
            "route_scope": (
                "one canonical native-SOL standard-SPL PumpSwap mint; Token-2022, "
                "cashback, mayhem and noncanonical pools excluded"
            ),
            "source_contract": (
                "confirmed canonical pool account reserves plus a fresh Pump SOL/USD "
                "reference; every later executable quote requires new route validation"
            ),
            "stop_policy": {
                "maximum_consecutive_unusable_intervals": 4,
                "stop_on_unexpected_authority": True,
                "stop_on_provider_spend_above_approved_ceiling": True,
                "request_quota_lifecycle": "retain-through-deadline",
                "automatic_ceiling_stops": [
                    "duration", "active_runtime_quota", "attributable_storage_quota",
                ],
                "provider_read_ceiling": "provider_request_quota",
            },
        },
        "coverage": {
            "max_source_age_seconds": 120,
            "minimum_independent_tokens": 1,
            "minimum_observations": 276,
            "maximum_gap_seconds": 30 * 60,
        },
        "duration_seconds": PUMPSWAP_72H_DURATION_SECONDS,
        "quotas": {
            "max_requests": PUMPSWAP_72H_PROVIDER_ATTEMPTS,
            "max_active_runtime_seconds": PUMPSWAP_72H_DURATION_SECONDS,
            "max_storage_bytes": 64 * 1024 * 1024,
            "max_concurrency": 1,
        },
        "provider_cost": {
            "currency": "USD",
            "marginal_cost_per_request": None,
            "fixed_subscription_cost": None,
            "price_basis": "unknown",
        },
    }


def build_pumpswap_72_hour_resource_model() -> dict[str, Any]:
    """Bounded model for the exact-pool RPC and SOL/USD reads."""
    return {
        "schema": RESOURCE_MODEL_SCHEMA,
        "expected_http_reads_per_request": 3,
        "max_http_reads_per_request": 4,
        "estimated_provider_response_bytes_per_request": 512 * 1024,
        "max_provider_response_bytes_per_request": 7 * 1024 * 1024,
        "estimated_normalized_bytes_per_quote": 4 * 1024,
        "fixed_storage_overhead_bytes": 4 * 1024 * 1024,
    }


def build_operational_summary(campaigns: ForwardCampaignStore,
                              recorder: ServiceEvidenceRecorder,
                              campaign_id: str, *,
                              as_of: float | None = None) -> dict[str, Any]:
    campaign = campaigns.campaign(campaign_id, recorder)
    now = float(campaigns.clock() if as_of is None else as_of)
    records = _provider_records(recorder, campaign_id)
    requirements = campaign["coverage"]
    classified = [_interval_class(record, requirements) for record in records]
    status_counts = Counter(classified)
    start = float(campaign["start_at"])
    terminal = campaign["state"] in {"stopped", "completed", "capped"}
    end = min(
        float(campaign.get("stopped_at") or campaign.get("updated_at") or now)
        if terminal else now,
        float(campaign["deadline_at"]),
    )
    cadence = float(campaign["source"]["cadence_seconds"])
    total_expected = int(math.ceil(float(campaign["duration_seconds"]) / cadence))
    elapsed = max(0.0, end - start)
    expected_so_far = min(total_expected, int(math.floor(elapsed / cadence)) + 1)
    attempted = len(records)
    usable_records = [
        record for record, status in zip(records, classified) if status == "usable"
    ]
    receipt_times = sorted(
        float(record["finished_at"]) for record in records
        if record.get("finished_at") is not None
    )
    usable_receipts = sorted(
        float(record["finished_at"]) for record in usable_records
        if record.get("finished_at") is not None
    )
    rejection_counts: Counter[str] = Counter()
    diagnostic_statuses: Counter[str] = Counter()
    for record in records:
        diagnostics = record.get("diagnostics") or {}
        if diagnostics.get("status"):
            diagnostic_statuses[str(diagnostics["status"])] += 1
        for category, count in (diagnostics.get("rejections") or {}).items():
            rejection_counts[str(category)] += int(count)

    usage = campaign["usage"]
    http_unknown = int(usage.get("unknown_http_read_requests") or 0)
    byte_unknown = int(usage.get("unknown_provider_response_byte_requests") or 0)
    normalized_bytes = int(usage.get("response_bytes") or 0)
    arms, aligned = _arm_summaries(campaigns, campaign, end, usage)
    paper_bytes = sum(int(arm["resources"]["paper_record_bytes"]) for arm in arms)
    physical = campaign["storage"]
    cycles = _evaluation_cycles(campaigns, campaign_id)
    lags = [
        float(item["processing_lag_seconds"]) for item in cycles
        if item.get("processing_lag_seconds") is not None
    ]
    summary = {
        "schema": SUMMARY_SCHEMA,
        "campaign_id": campaign_id,
        "generated_at": now,
        "manifest_sha256": campaign["request_sha256"],
        "preset_id": campaign.get("preset_id"),
        "state": campaign["state"],
        "blocker": campaign.get("blocker"),
        "intervals": {
            "planned": total_expected,
            "expected_so_far": expected_so_far,
            "attempted": attempted,
            "missing": max(0, expected_so_far - attempted),
            "usable": status_counts["usable"],
            "sparse": status_counts["sparse"],
            "empty": status_counts["empty"],
            "stale": status_counts["stale"],
            "outage": status_counts["outage"],
            "quota_exhausted": status_counts["quota_exhausted"],
            "usable_fraction_of_expected": (
                status_counts["usable"] / expected_so_far if expected_so_far else None
            ),
            "maximum_attempt_gap_seconds": _maximum_gap(receipt_times),
            "maximum_usable_gap_seconds": _maximum_gap(usable_receipts),
            "classification_order": [
                "quota_exhausted", "outage", "stale", "empty", "sparse", "usable",
            ],
        },
        "coverage": {
            "first_receipt_at": receipt_times[0] if receipt_times else None,
            "last_receipt_at": receipt_times[-1] if receipt_times else None,
            "first_usable_receipt_at": usable_receipts[0] if usable_receipts else None,
            "last_usable_receipt_at": usable_receipts[-1] if usable_receipts else None,
            "usable_source_span_seconds": (
                usable_receipts[-1] - usable_receipts[0]
                if len(usable_receipts) >= 2 else (0.0 if usable_receipts else None)
            ),
            "continuous_coverage_seconds": None,
            "continuous_coverage_note": (
                "Discrete source points do not establish continuous market coverage."
            ),
            "requirements": requirements,
        },
        "time": {
            **campaign["progress"],
            "inactive_wall_seconds": max(
                0.0,
                float(campaign["progress"]["wall_seconds"])
                - float(campaign["progress"]["active_seconds"]),
            ),
        },
        "resources": {
            "logical_provider_attempts": int(usage.get("requests") or 0),
            "allocated_provider_request_equivalents": float(
                usage.get("allocated_request_equivalents") or 0
            ),
            "actual_http_reads": None if http_unknown else int(usage.get("http_reads") or 0),
            "measured_http_reads_lower_bound": int(usage.get("http_reads") or 0),
            "http_read_measurements_unknown": http_unknown,
            "provider_response_bytes": (
                None if byte_unknown else int(usage.get("provider_response_bytes") or 0)
            ),
            "measured_provider_response_bytes_lower_bound": int(
                usage.get("provider_response_bytes") or 0
            ),
            "provider_byte_measurements_unknown": byte_unknown,
            "normalized_evidence_bytes": normalized_bytes,
            "paper_record_bytes": paper_bytes,
            "attributable_storage_bytes": normalized_bytes + paper_bytes,
            "physical_storage": {
                **physical,
                "change_bytes": (
                    int(physical["current_physical_bytes"])
                    - int(physical["baseline_physical_bytes"])
                ),
                "attribution": (
                    "shared physical growth is not assigned solely to this campaign"
                ),
            },
            "duplicate_rows": int(usage.get("duplicate_rows") or 0),
            "excluded_rows": int(usage.get("excluded_rows") or 0),
            "result_rows": int(usage.get("result_rows") or 0),
        },
        "provider_quality": {
            "diagnostic_statuses": dict(sorted(diagnostic_statuses.items())),
            "rejections": {
                category: rejection_counts.get(category, 0)
                for category in sorted(PROVIDER_REJECTION_CATEGORIES)
            },
            "timeouts": int(usage.get("timeout_events") or 0),
            "rate_limits": int(usage.get("rate_limit_events") or 0),
            "errors": int(usage.get("errors") or 0),
            "maximum_source_age_seconds": usage.get("maximum_source_age_seconds"),
        },
        "evaluation": {
            "cycles": len(cycles),
            "processing_lag_seconds": {
                "maximum": max(lags) if lags else None,
                "mean": sum(lags) / len(lags) if lags else None,
                "measurement_status": "measured" if lags else "unavailable",
            },
            "cycle_observations": sum(int(item["observations"]) for item in cycles),
            "cycle_decisions": sum(int(item["decisions"]) for item in cycles),
            "cycle_outcomes": sum(int(item["outcomes"]) for item in cycles),
        },
        "alignment": aligned,
        "arms": arms,
        "provider_cost": campaign["provider_cost"],
        "profitability": {
            "fully_costed_allowed": bool(
                campaign["provider_cost"]["fully_costed_profitability_allowed"]
            ),
            "blocker": (
                None if campaign["provider_cost"]["fully_costed_profitability_allowed"]
                else "Provider billing is unknown or partial."
            ),
        },
    }
    summary["id"] = "observation-summary-" + _sha256(summary)[:32]
    return summary


def build_operational_export(summary: dict[str, Any]) -> dict[str, Any]:
    """Wrap a summary in a canonical, digest-verifiable transfer object."""
    if not isinstance(summary, dict) or summary.get("schema") != SUMMARY_SCHEMA:
        raise ValueError("Observation operational summary schema is invalid")
    payload = json.loads(canonical_json(summary))
    value = {
        "schema": EXPORT_SCHEMA,
        "campaign_id": payload["campaign_id"],
        "manifest_sha256": payload["manifest_sha256"],
        "summary": payload,
        "summary_sha256": _sha256(payload),
    }
    value["id"] = "observation-export-" + _sha256(value)[:32]
    return value


def _strategy(label: str, *, growth: float) -> dict[str, Any]:
    configuration = copy.deepcopy(CONFIG)
    configuration.update({
        "observation_seconds": 45 * 60,
        "minimum_samples": 4,
        "minimum_price_changes": 2,
        "minimum_growth_fraction": growth,
        "minimum_reserve_growth_usd": 10,
        "entry_cap_max": 20_000,
        "max_sample_gap_seconds": 30 * 60,
        "quote_max_age_seconds": 120,
        "entry_expiry_seconds": 30 * 60,
        "fill_delay_seconds": 2,
        "failed_attempt_probability": 0,
        "max_positions": 2,
        "quote_gap_exit_seconds": 30 * 60,
        "max_hold_seconds": 4 * 60 * 60,
        "targets": [
            {"market_cap_usd": 22_000, "initial_fraction": 0.4},
            {"market_cap_usd": 30_000, "initial_fraction": 0.3},
        ],
    })
    return {
        "schema": "pumpwizard.strategy-handoff/v1",
        "strategy": {"id": label.lower().replace(" ", "-"), "label": label},
        "configuration": configuration,
    }


def _pumpswap_strategy() -> dict[str, Any]:
    configuration = copy.deepcopy(CONFIG)
    configuration.update({
        "entry_style": "post-grad",
        "entry_cap_min": 1,
        "entry_cap_max": 1_000_000_000,
        # Six 15-minute samples need a 75-minute inclusive window.
        "observation_seconds": 75 * 60,
        "minimum_samples": 6,
        "minimum_price_changes": 3,
        "minimum_growth_fraction": 0.01,
        "minimum_reserve_growth_usd": 10,
        "max_sample_gap_seconds": 30 * 60,
        "quote_max_age_seconds": 120,
        "entry_expiry_seconds": 30 * 60,
        "failed_attempt_probability": 0,
        "max_positions": 1,
        "quote_gap_exit_seconds": 30 * 60,
        "max_hold_seconds": PUMPSWAP_72H_DURATION_SECONDS,
        "targets": [
            {"market_cap_usd": 20_000_000, "initial_fraction": 0.4},
            {"market_cap_usd": 50_000_000, "initial_fraction": 0.3},
        ],
    })
    return {
        "schema": "pumpwizard.strategy-handoff/v1",
        "strategy": {
            "id": "pumpswap-month-post-grad-observed",
            "label": "PumpSwap month post-graduation observation",
        },
        "configuration": configuration,
    }


def _provider_records(recorder: ServiceEvidenceRecorder,
                      campaign_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    cursor: tuple[float, str] | None = None
    while True:
        page = recorder.provider_history(campaign_id=campaign_id, after=cursor, limit=500)
        records.extend(page["records"])
        value = page["next_cursor"]
        if value is None:
            return records
        cursor = (float(value[0]), str(value[1]))


def _interval_class(record: dict[str, Any], requirements: dict[str, Any]) -> str:
    diagnostics = record.get("diagnostics") or {}
    diagnostic_status = diagnostics.get("status")
    rejections = diagnostics.get("rejections") or {}
    if diagnostic_status == "quota_exhausted" or int(rejections.get("exhausted_quota", 0)):
        return "quota_exhausted"
    if record.get("status") != "success" or diagnostic_status == "failing":
        return "outage"
    age = record.get("maximum_source_age_seconds")
    if (diagnostic_status == "stale"
            or (age is not None and float(age) > float(requirements["max_source_age_seconds"]))):
        return "stale"
    results = int(record.get("result_count") or 0)
    if results == 0 or diagnostic_status == "empty":
        return "empty"
    if (results < int(requirements["minimum_independent_tokens"])
            or diagnostic_status == "partial"):
        return "sparse"
    return "usable"


def _arm_summaries(campaigns: ForwardCampaignStore, campaign: dict[str, Any],
                   end: float, usage: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result: list[dict[str, Any]] = []
    evidence_sets: list[set[str]] = []
    arm_count = max(1, len(campaign["arms"]))
    request_share = float(usage.get("allocated_request_equivalents") or 0) / arm_count
    normalized_share = int(usage.get("response_bytes") or 0) / arm_count
    with campaigns._lock:
        for arm in campaign["arms"]:
            run_id = arm["paper_run_id"]
            observations = campaigns.db.execute(
                """SELECT evidence_id,mint,quality_status,exclusion_reason,
                          first_received_at,LENGTH(record_json) record_bytes
                   FROM paper_observations WHERE run_id=?
                   ORDER BY first_received_at,evidence_id""", (run_id,),
            ).fetchall()
            eligible = {str(row["evidence_id"]) for row in observations
                        if row["quality_status"] == "eligible"}
            evidence_sets.append(eligible)
            exclusion_reasons = Counter(
                str(row["exclusion_reason"] or "unspecified") for row in observations
                if row["quality_status"] == "excluded"
            )
            decision_rows = campaigns.db.execute(
                """SELECT kind,COUNT(*) count FROM paper_decisions
                   WHERE run_id=? GROUP BY kind ORDER BY kind""", (run_id,),
            ).fetchall()
            outcome_rows = campaigns.db.execute(
                """SELECT status,COUNT(*) count FROM paper_outcomes
                   WHERE run_id=? GROUP BY status ORDER BY status""", (run_id,),
            ).fetchall()
            run_row = campaigns.db.execute(
                "SELECT evaluator_report_json FROM paper_runs WHERE id=?", (run_id,),
            ).fetchone()
            evaluator_report = (
                json.loads(run_row["evaluator_report_json"])
                if run_row is not None and run_row["evaluator_report_json"] else {}
            )
            portfolio_outcomes = Counter(
                str(item.get("outcome_status") or "unknown")
                for item in evaluator_report.get("decisions", [])
            )
            replay = evaluator_report.get("replay") or {}
            replay_metrics = replay.get("metrics") or {}
            intervals = campaigns.db.execute(
                """SELECT started_at,ended_at FROM paper_operating_intervals
                   WHERE run_id=?""", (run_id,),
            ).fetchall()
            gaps = campaigns.db.execute(
                "SELECT started_at,ended_at FROM paper_gaps WHERE run_id=?", (run_id,),
            ).fetchall()
            faults = int(campaigns.db.execute(
                "SELECT COUNT(*) FROM paper_faults WHERE run_id=?", (run_id,),
            ).fetchone()[0])
            exposure = sum(max(0.0, float(row["ended_at"] or end) - float(row["started_at"]))
                           for row in intervals)
            gap_seconds = sum(max(0.0, float(row["ended_at"] or end) - float(row["started_at"]))
                              for row in gaps)
            paper_bytes = sum(int(row["record_bytes"] or 0) for row in observations)
            result.append({
                "id": arm["id"], "paper_run_id": run_id,
                "strategy_sha256": arm["strategy_sha256"], "state": arm["state"],
                "observations": {
                    "total": len(observations), "eligible": len(eligible),
                    "excluded": len(observations) - len(eligible),
                    "independent_tokens": len({str(row["mint"]) for row in observations
                                                if row["quality_status"] == "eligible"}),
                    "exclusion_reasons": dict(sorted(exclusion_reasons.items())),
                },
                "decisions": {str(row["kind"]): int(row["count"]) for row in decision_rows},
                "outcomes": {str(row["status"]): int(row["count"]) for row in outcome_rows},
                "portfolio_outcomes": dict(sorted(portfolio_outcomes.items())),
                "portfolio_facts": {
                    "fills": len(replay.get("fills") or []),
                    "completed_position_exits": int(
                        replay_metrics.get("completed_position_exits") or 0
                    ),
                    "valuation_complete": replay_metrics.get("valuation_complete"),
                    "reproducibility_sha256": replay.get("reproducibility_sha256"),
                },
                "evaluator": {
                    "exposure_seconds": exposure,
                    "gap_seconds": gap_seconds,
                    "faults": faults,
                },
                "resources": {
                    "allocated_provider_request_equivalents": request_share,
                    "allocated_normalized_evidence_bytes": normalized_share,
                    "paper_record_bytes": paper_bytes,
                    "allocation_basis": (
                        "campaign's shared allocation divided equally across frozen arms; "
                        "provider attempts are not summed once per arm"
                    ),
                },
            })
    aligned = set.intersection(*evidence_sets) if evidence_sets else set()
    union = set.union(*evidence_sets) if evidence_sets else set()
    return result, {
        "eligible_evidence_intersection": len(aligned),
        "eligible_evidence_union": len(union),
        "all_arms_aligned_fraction": len(aligned) / len(union) if union else None,
        "basis": "distinct evidence IDs, not per-arm row sums",
    }


def _evaluation_cycles(campaigns: ForwardCampaignStore,
                       campaign_id: str) -> list[dict[str, Any]]:
    with campaigns._lock:
        rows = campaigns.db.execute(
            """SELECT payload_json FROM forward_campaign_evaluation_cycles
               WHERE campaign_id=? ORDER BY finished_at,cycle_id""", (campaign_id,),
        ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def _maximum_gap(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return max(right - left for left, right in zip(values, values[1:]))


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
