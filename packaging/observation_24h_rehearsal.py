#!/usr/bin/env python3
"""Deterministic 24-hour mechanics rehearsal; it is not elapsed live evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time

from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.forward_campaigns import START_CONFIRMATION, ForwardCampaignStore
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.observation_operations import (
    PRESET_CADENCE_SECONDS,
    PRESET_PROVIDER_ATTEMPTS,
    build_24_hour_request,
    build_24_hour_resource_model,
    build_operational_export,
    build_operational_summary,
)
from pumpwizard_runner.paper_research import PaperResearchWorker
from pumpwizard_runner.scheduler import MonitorScheduler
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder


SCHEMA = "pumpwizard.observation-24h-rehearsal/v1"


class Clock:
    def __init__(self, value: float = 2_000_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class FixtureController:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.attempt = 0
        self.statuses: list[str] = []


class CompositePumpMarketSource:
    """Bounded deterministic source with the production provider identity."""

    def __init__(self, controller: FixtureController):
        self.controller = controller
        self.last_diagnostics: dict[str, object] | None = None

    def collect(self, minimum: float, maximum: float, *, limit: int,
                priority: set[str]) -> list[MarketQuote]:
        del minimum, maximum, priority
        sequence = self.controller.attempt
        self.controller.attempt += 1
        at = self.controller.clock.value
        if sequence == 19:
            self.last_diagnostics = _diagnostics(at, "failing", quotes=0)
            self.controller.statuses.append("failing")
            raise TimeoutError("deterministic provider outage")
        if sequence == 17:
            status, quotes = "empty", []
        elif sequence == 18:
            status = "stale"
            quotes = [_quote(at, "StaleFixture", sequence, source_at=at - 600)]
        elif sequence == 16:
            status = "partial"
            quotes = [_quote(at, "ObservedEntry", sequence)]
        else:
            status = "healthy"
            quotes = [
                _quote(at, "ObservedEntry", sequence),
                _quote(at, "IndependentControl", sequence, growing=False),
            ]
        quotes = quotes[:limit]
        self.last_diagnostics = _diagnostics(at, status, quotes=len(quotes))
        self.controller.statuses.append(status)
        return quotes


def _quote(at: float, mint: str, sequence: int, *, growing: bool = True,
           source_at: float | None = None) -> MarketQuote:
    growth = 1 + (0.006 * sequence if growing else 0)
    cap = 15_000 * growth if growing else 18_000
    price = growth if growing else 1.0
    observed_source = at if source_at is None else source_at
    return MarketQuote(
        mint, mint[-8:], at, "curve", "Pool-" + mint, cap,
        price, price / 100, 5_000 + sequence, 50 + sequence / 10,
        source="CompositePumpMarketSource", quote_usd=100,
        base_reserve=1_000_000, reserve_usd=50_000 + cap,
        real_token_reserve=500_000, supply=1_000_000_000, decimals=6,
        source_at=observed_source, fx_at=observed_source,
    )


def _diagnostics(at: float, status: str, *, quotes: int) -> dict[str, object]:
    stale = status == "stale"
    return {
        "schema": "pumpwizard.provider-diagnostics/v1",
        "provider": "CompositePumpMarketSource",
        "started_at": at, "finished_at": at + 0.1, "status": status,
        "bounds": {
            "maximum_http_requests": 3, "maximum_elapsed_seconds": 60,
            "maximum_response_bytes": 3 * 1024 * 1024, "maximum_concurrency": 1,
        },
        "stages": {
            "discovery_requests": 2, "listing_rows": max(2, quotes),
            "unique_listings": max(2, quotes), "eligible_candidates": quotes,
            "selected_records": quotes, "selected_detail_requests": 1,
            "returned_details": quotes, "normalized_quotes": quotes,
            "http_requests": 3, "response_bytes": 2_048 + 512 * quotes,
        },
        "rejections": ({"stale_or_unknown_source_time": 1} if stale else {}),
        "provenance": {
            "provider_hosts": ["deterministic-fixture"],
            "endpoint_classes": ["fixture-discovery", "fixture-detail"],
        },
        "readiness": {
            "ready": status in {"healthy", "partial"}, "routes": ["curve"],
            "reasons": [] if status == "healthy" else [status],
            "first_received_at": at + 0.1,
            "earliest_source_at": at - 600 if stale else at,
            "latest_source_at": at - 600 if stale else at,
            "maximum_source_age_seconds": 600 if stale else 0.1,
        },
    }


def run(data_dir: Path) -> dict[str, object]:
    real_started = time.perf_counter()
    clock = Clock()
    simulated_start = clock.value
    recorder = ServiceEvidenceRecorder(data_dir, clock=clock)
    campaigns = ForwardCampaignStore(data_dir, clock=clock)
    worker = PaperResearchWorker(data_dir, recorder, clock=clock)
    library = StrategyLibrary(data_dir)
    controller = FixtureController(clock)
    scheduler = MonitorScheduler(
        data_dir, source_factory=lambda: CompositePumpMarketSource(controller),
        recorder=recorder, campaigns=campaigns,
        owner="batch7-observation-rehearsal", clock=clock,
    )
    campaign_id: str | None = None
    browser_absent_cycles = 0
    restart_paused = False
    restart_demand_suppressed = False
    manual_pause_suppressed = False
    evaluation_cycles = 0
    try:
        request = build_24_hour_request()
        resource_model = build_24_hour_resource_model()
        preset = campaigns.preregister(
            request, resource_model, idempotency_key="batch7-24h-prepare",
        )
        repeated_preset = campaigns.preregister(
            request, resource_model, idempotency_key="batch7-24h-prepare",
        )
        campaign = campaigns.start_preregistered(
            preset["id"], idempotency_key="batch7-24h-start",
            confirmation=START_CONFIRMATION,
        )
        duplicate = campaigns.start_preregistered(
            preset["id"], idempotency_key="batch7-24h-start",
            confirmation=START_CONFIRMATION,
        )
        campaign_id = str(campaign["id"])

        for sequence in range(PRESET_PROVIDER_ATTEMPTS):
            planned_at = simulated_start + sequence * PRESET_CADENCE_SECONDS
            clock.value = planned_at
            if sequence == 30:
                clock.value = planned_at - 120
                campaigns.command(
                    campaign_id, action="pause",
                    idempotency_key="batch7-manual-pause",
                )
                clock.value = planned_at
                before = controller.attempt
                scheduler.run_cycle(library)
                manual_pause_suppressed = controller.attempt == before
                campaigns.command(
                    campaign_id, action="resume",
                    idempotency_key="batch7-manual-resume",
                )
            if sequence == 48:
                clock.value = planned_at - 120
                restart_paused = campaigns.recover_running() == 1
                clock.value = planned_at
                before = controller.attempt
                scheduler.run_cycle(library)
                restart_demand_suppressed = controller.attempt == before
                campaigns.command(
                    campaign_id, action="resume",
                    idempotency_key="batch7-restart-resume",
                )
            evaluation_before = campaigns.evaluation_snapshot(campaign_id)
            scheduler.run_cycle(library)
            cycle_started = clock.value
            worker.run_once()
            clock.value += 0.25
            evaluation_after = campaigns.evaluation_snapshot(campaign_id)
            observation_delta = (
                evaluation_after["observations"] - evaluation_before["observations"]
            )
            decision_delta = evaluation_after["decisions"] - evaluation_before["decisions"]
            outcome_delta = evaluation_after["outcomes"] - evaluation_before["outcomes"]
            through = evaluation_after["through_received_at"]
            campaigns.record_evaluation_cycle(
                campaign_id, cycle_id=f"cycle-{sequence + 1:03d}",
                started_at=cycle_started, finished_at=clock.value,
                through_received_at=through,
                observations=observation_delta,
                decisions=decision_delta,
                outcomes=outcome_delta,
            )
            evaluation_cycles += 1
            if 5 <= sequence < 15:
                browser_absent_cycles += 1

        capped_before_stop = campaigns.campaign(campaign_id, recorder)
        stopped = campaigns.command(
            campaign_id, action="stop", confirmation=campaign_id,
            idempotency_key="batch7-24h-stop",
        )
        final = campaigns.campaign(campaign_id, recorder)
        summary = build_operational_summary(campaigns, recorder, campaign_id)
        export = build_operational_export(summary)
        decision_kinds: dict[str, dict[str, int]] = {}
        for arm in summary["arms"]:
            decision_kinds[str(arm["id"])] = dict(arm["decisions"])
        positive_arms = sorted(
            arm_id for arm_id, counts in decision_kinds.items()
            if int(counts.get("entry_recommended", 0)) > 0
        )
        zero_entry_arms = sorted(
            arm_id for arm_id, counts in decision_kinds.items()
            if int(counts.get("entry_recommended", 0)) == 0
        )
        result = {
            "schema": SCHEMA,
            "label": "deterministic simulated-time mechanics; not elapsed live observation",
            "campaign_id": campaign_id,
            "manifest_sha256": final["request_sha256"],
            "manifest": request,
            "resource_model": resource_model,
            "preset_identity_sha256": preset["identity_sha256"],
            "preset_revision": preset["revision"],
            "dry_run": preset["dry_run"],
            "duplicate_prepare_same_identity": repeated_preset["id"] == preset["id"],
            "duplicate_start_same_campaign": duplicate["id"] == campaign_id,
            "simulated_seconds": PRESET_PROVIDER_ATTEMPTS * PRESET_CADENCE_SECONDS,
            "real_elapsed_seconds": time.perf_counter() - real_started,
            "browser_absent_scheduler_cycles": browser_absent_cycles,
            "manual_pause_suppressed_collection": manual_pause_suppressed,
            "restart_paused_campaign": restart_paused,
            "restart_suppressed_collection_until_resume": restart_demand_suppressed,
            "provider_statuses": dict(sorted({
                status: controller.statuses.count(status) for status in set(controller.statuses)
            }.items())),
            "capped_before_stop": {
                "state": capped_before_stop["state"],
                "blocker": capped_before_stop["blocker"],
            },
            "final_state": stopped["state"],
            "evaluation_cycles": evaluation_cycles,
            "positive_entry_arms": positive_arms,
            "zero_entry_arms": zero_entry_arms,
            "operational_summary": summary,
            "operational_export": export,
            "isolated_data_dir": str(data_dir),
            "authority": final["authority"],
        }
        result["passed"] = (
            result["simulated_seconds"] == 24 * 60 * 60
            and result["duplicate_prepare_same_identity"]
            and result["duplicate_start_same_campaign"]
            and result["manual_pause_suppressed_collection"]
            and result["restart_paused_campaign"]
            and result["restart_suppressed_collection_until_resume"]
            and result["browser_absent_scheduler_cycles"] == 10
            and result["provider_statuses"].get("failing") == 1
            and result["provider_statuses"].get("empty") == 1
            and result["provider_statuses"].get("stale") == 1
            and result["capped_before_stop"]["state"] == "capped"
            and result["final_state"] == "stopped"
            and bool(positive_arms) and bool(zero_entry_arms)
            and not any(result["authority"].values())
        )
        return result
    finally:
        if campaign_id is not None:
            try:
                current = campaigns.campaign(campaign_id)
                if current["state"] != "stopped":
                    campaigns.command(
                        campaign_id, action="stop", confirmation=campaign_id,
                        idempotency_key="batch7-24h-finally-stop",
                    )
            except Exception:
                pass
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
        with tempfile.TemporaryDirectory(prefix="pumpwizard-batch7-observation-") as temporary:
            result = run(Path(temporary))
    encoded = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        args.output.with_suffix(args.output.suffix + ".sha256").write_text(
            f"{digest}  {args.output.name}\n", encoding="utf-8",
        )
    print(encoded, end="")
    print("sha256=" + digest)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
