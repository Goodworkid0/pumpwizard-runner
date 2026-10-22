#!/usr/bin/env python3
"""Bounded keyless live observation through the existing campaign/evaluator path."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import tempfile
import time

from pumpwizard_runner.campaign_performance import build_campaign_report
from pumpwizard_runner.forward_campaigns import (
    REQUEST_SCHEMA,
    START_CONFIRMATION,
    ForwardCampaignStore,
)
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.observation_operations import (
    build_24_hour_resource_model,
    build_operational_export,
    build_operational_summary,
)
from pumpwizard_runner.paper_research import PaperResearchWorker
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.scheduler import MonitorScheduler
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder
from pumpwizard_runner.strategies import parse_package


SCHEMA = "pumpwizard.forward-live-campaign-acceptance/v2"
MAX_PROVIDER_REQUESTS = 5
MAX_HTTP_READS_PER_PROVIDER_REQUEST = 3
MAX_TOTAL_HTTP_READS = MAX_PROVIDER_REQUESTS * MAX_HTTP_READS_PER_PROVIDER_REQUEST
MAX_WALL_SECONDS = 60


def _strategy(label: str, growth: float) -> dict[str, object]:
    configuration = copy.deepcopy(CONFIG)
    configuration.update({
        "observation_seconds": 15,
        "minimum_samples": 4,
        "minimum_price_changes": 2,
        "minimum_growth_fraction": growth,
        "minimum_reserve_growth_usd": 10,
        "quote_max_age_seconds": 45,
        "fill_delay_seconds": 2,
        "failed_attempt_probability": 0,
        "max_positions": 2,
    })
    return {
        "schema": "pumpwizard.strategy-handoff/v1",
        "strategy": {"id": label.lower().replace(" ", "-"), "label": label},
        "configuration": configuration,
    }


def _manifest() -> dict[str, object]:
    packages = [
        _strategy("Batch 6 live arm A", 0.01),
        _strategy("Batch 6 live arm B", 0.02),
    ]
    return {
        "schema": REQUEST_SCHEMA,
        "campaign_key": "batch6-bounded-live-market-coverage",
        "supersedes_campaign_id": None,
        "source": {
            "provider": "CompositePumpMarketSource",
            "minimum_cap": 1_000,
            "maximum_cap": 150_000,
            "priority_mints": [],
            "limit": 3,
            "cadence_seconds": 1,
        },
        "arms": [{
            "id": f"arm-{index + 1}",
            "source_session_id": f"batch6-live-session-{index + 1}",
            "strategy_sha256": parse_package(package).digest,
            "strategy_package": package,
            "simulated_capital_usd": 500,
        } for index, package in enumerate(packages)],
        "assumptions": {
            "cost_model": "exact frozen strategy package costs",
            "execution_model": "actual paper portfolio replay v3",
        },
        "coverage": {
            "max_source_age_seconds": 45,
            "minimum_independent_tokens": 2,
            "minimum_observations": 10,
            "maximum_gap_seconds": 10,
        },
        "duration_seconds": MAX_WALL_SECONDS,
        "quotas": {
            # The acceptance loop stops one request before the immutable
            # campaign ceiling so the evaluator consumes the last arrival.
            "max_requests": MAX_PROVIDER_REQUESTS + 1,
            "max_active_runtime_seconds": MAX_WALL_SECONDS,
            "max_storage_bytes": 32 * 1024 * 1024,
            "max_concurrency": 1,
        },
        "provider_cost": {
            "currency": "USD",
            "marginal_cost_per_request": None,
            "fixed_subscription_cost": None,
            "price_basis": "unknown",
        },
    }


def run(data_dir: Path) -> dict[str, object]:
    wall_started = time.time()
    manifest = _manifest()
    library = StrategyLibrary(data_dir)
    recorder = ServiceEvidenceRecorder(data_dir)
    campaigns = ForwardCampaignStore(data_dir)
    worker = PaperResearchWorker(data_dir, recorder)
    preset = campaigns.preregister(
        manifest, build_24_hour_resource_model(),
        idempotency_key="batch7-bounded-live-prepare",
    )
    campaign = campaigns.start_preregistered(
        preset["id"],
        idempotency_key="batch6-bounded-live-start",
        confirmation=START_CONFIRMATION,
    )
    campaign_id = str(campaign["id"])
    scheduler = MonitorScheduler(
        data_dir,
        recorder=recorder,
        campaigns=campaigns,
        owner="batch6-bounded-live-worker",
    )
    cycles = []
    maximum_processing_lag = 0.0
    try:
        for sequence in range(1, MAX_PROVIDER_REQUESTS + 1):
            if time.time() - wall_started >= MAX_WALL_SECONDS:
                break
            before = campaigns.campaign(campaign_id, recorder)
            evaluation_before = campaigns.evaluation_snapshot(campaign_id)
            before_counts = {
                arm["id"]: dict(arm["counts"]) for arm in before["arms"]
            }
            cycle_started = time.time()
            scheduler.run_cycle(library)
            provider_records = recorder.provider_history(
                campaign_id=campaign_id, limit=MAX_PROVIDER_REQUESTS,
            )["records"]
            new_records = provider_records[int(before["request_count"]):]
            worker_totals = worker.run_once()
            worker_finished = time.time()
            after = campaigns.campaign(campaign_id, recorder)
            evaluation_after = campaigns.evaluation_snapshot(campaign_id)
            processing_lags = [
                max(0.0, worker_finished - float(record["finished_at"]))
                for record in new_records if record["finished_at"] is not None
            ]
            maximum_processing_lag = max(
                maximum_processing_lag, max(processing_lags, default=0.0),
            )
            deltas = {
                arm["id"]: {
                    key: int(arm["counts"][key]) - int(before_counts[arm["id"]][key])
                    for key in (
                        "observations", "decisions", "outcomes", "exclusions", "gaps", "faults",
                    )
                }
                for arm in after["arms"]
            }
            through_received_at = evaluation_after["through_received_at"]
            campaigns.record_evaluation_cycle(
                campaign_id, cycle_id=f"batch7-live-cycle-{sequence:03d}",
                started_at=cycle_started, finished_at=worker_finished,
                through_received_at=through_received_at,
                observations=sum(delta["observations"] for delta in deltas.values()),
                decisions=sum(delta["decisions"] for delta in deltas.values()),
                outcomes=(
                    evaluation_after["outcomes"] - evaluation_before["outcomes"]
                ),
            )
            cycles.append({
                "sequence": sequence,
                "started_at": cycle_started,
                "finished_at": worker_finished,
                "provider_requests": len(new_records),
                "provider_statuses": [
                    (record.get("diagnostics") or {}).get("status") for record in new_records
                ],
                "normalized_quotes": sum(
                    int((record.get("diagnostics") or {}).get("stages", {}).get(
                        "normalized_quotes", 0,
                    ))
                    for record in new_records
                ),
                "maximum_processing_lag_seconds": max(processing_lags, default=None),
                "worker": worker_totals,
                "arm_deltas": deltas,
                "no_opportunity": all(
                    delta["observations"] > 0 and delta["decisions"] == 0
                    for delta in deltas.values()
                ),
            })
            if after["state"] not in {"running", "degraded"}:
                break
            delay = max(0.0, float(after["next_scan_at"]) - time.time())
            if delay:
                time.sleep(min(delay + 0.02, 2.0))

        stopped = campaigns.command(
            campaign_id,
            action="stop",
            confirmation=campaign_id,
            idempotency_key="batch6-bounded-live-stop",
        )
        worker.run_once()
        final = campaigns.campaign(campaign_id, recorder)
        provider_records = recorder.provider_history(
            campaign_id=campaign_id, limit=MAX_PROVIDER_REQUESTS,
        )["records"]
        report = build_campaign_report(
            campaigns, recorder, campaign_id, created_at=time.time(),
        )
        histories = {
            arm["id"]: campaigns.paper.history(arm["paper_run_id"])
            for arm in final["arms"]
        }
        all_diagnostics = [
            record["diagnostics"] for record in provider_records if record["diagnostics"]
        ]
        observed_http_reads = sum(
            int(diagnostic["stages"]["http_requests"]) for diagnostic in all_diagnostics
        )
        provider_payload_bytes = sum(
            int(diagnostic["stages"]["response_bytes"]) for diagnostic in all_diagnostics
        )
        receipts = [
            float(record["finished_at"])
            for record in provider_records if record["finished_at"] is not None
        ]
        operational_summary = build_operational_summary(
            campaigns, recorder, campaign_id, as_of=time.time(),
        )
        operational_export = build_operational_export(operational_summary)
        result = {
            "schema": SCHEMA,
            "campaign_id": campaign_id,
            "campaign_request_sha256": final["request_sha256"],
            "preset_identity_sha256": preset["identity_sha256"],
            "dry_run": preset["dry_run"],
            "manifest": manifest,
            "bounds": {
                "maximum_provider_requests": MAX_PROVIDER_REQUESTS,
                "maximum_http_reads": MAX_TOTAL_HTTP_READS,
                "maximum_wall_seconds": MAX_WALL_SECONDS,
                "maximum_concurrency": 1,
            },
            "started_at": wall_started,
            "finished_at": time.time(),
            "actual_observed_seconds": max(receipts) - min(receipts) if len(receipts) > 1 else 0,
            "campaign_state": final["state"],
            "stop_command_state": stopped["state"],
            "provider_requests": len(provider_records),
            "observed_http_reads": observed_http_reads,
            "provider_payload_bytes": provider_payload_bytes,
            "provider_usage": final["usage"],
            "provider_cost": final["provider_cost"],
            "provider_statuses": [diagnostic["status"] for diagnostic in all_diagnostics],
            "maximum_source_age_seconds": max((
                float(diagnostic["readiness"]["maximum_source_age_seconds"])
                for diagnostic in all_diagnostics
                if diagnostic["readiness"]["maximum_source_age_seconds"] is not None
            ), default=None),
            "maximum_processing_lag_seconds": maximum_processing_lag,
            "cycles": cycles,
            "no_opportunity_intervals": sum(cycle["no_opportunity"] for cycle in cycles),
            "arm_counts": {arm["id"]: arm["counts"] for arm in final["arms"]},
            "arm_decision_kinds": {
                arm_id: sorted({
                    row["decision"]["kind"] for row in history["decisions"]
                })
                for arm_id, history in histories.items()
            },
            "arm_gaps": {
                arm_id: history["gaps"] for arm_id, history in histories.items()
            },
            "report": {
                "id": report["id"],
                "reproducibility_sha256": report["reproducibility_sha256"],
                "readiness": report["readiness"],
                "claims": report["claims"],
            },
            "operational_summary": operational_summary,
            "operational_export": operational_export,
            "authority": final["authority"],
            "isolated_data_dir": str(data_dir),
        }
        result["passed"] = (
            result["campaign_state"] == "stopped"
            and 1 <= result["provider_requests"] <= MAX_PROVIDER_REQUESTS
            and result["observed_http_reads"] <= MAX_TOTAL_HTTP_READS
            and any(status in {"healthy", "partial"} for status in result["provider_statuses"])
            and all(counts["observations"] > 0 for counts in result["arm_counts"].values())
            and result["operational_summary"]["state"] == "stopped"
            and not any(result["authority"].values())
        )
        return result
    finally:
        worker.close()
        campaigns.close()
        recorder.close()
        library.release_monitor_lease(scheduler.owner)
        library.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if args.data_dir is not None:
        args.data_dir.mkdir(parents=True, exist_ok=True)
        result = run(args.data_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="pumpwizard-batch6-live-") as temporary:
            result = run(Path(temporary))
    encoded = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
