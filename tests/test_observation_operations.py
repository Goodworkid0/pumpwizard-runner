from __future__ import annotations

import copy
from pathlib import Path
import runpy

import pytest

from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.forward_campaigns import (
    START_CONFIRMATION,
    ForwardCampaignError,
    ForwardCampaignStore,
)
from pumpwizard_runner.observation_operations import (
    build_24_hour_request,
    build_24_hour_resource_model,
    build_operational_export,
    build_operational_summary,
    build_pumpswap_72_hour_request,
    build_pumpswap_72_hour_resource_model,
)
from pumpwizard_runner.paper_research import PaperResearchWorker
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder


class Clock:
    def __init__(self, value: float = 10_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


def diagnostics(at: float, *, status: str, http_reads: int = 3,
                response_bytes: int = 2_048,
                rejections: dict[str, int] | None = None) -> dict[str, object]:
    return {
        "schema": "pumpwizard.provider-diagnostics/v1",
        "provider": "CompositePumpMarketSource",
        "started_at": at,
        "finished_at": at + 0.2,
        "status": status,
        "bounds": {
            "maximum_http_requests": 3,
            "maximum_elapsed_seconds": 60,
            "maximum_response_bytes": 3 * 1024 * 1024,
            "maximum_concurrency": 1,
        },
        "stages": {
            "discovery_requests": min(http_reads, 2), "listing_rows": 2,
            "unique_listings": 2, "eligible_candidates": 2,
            "selected_records": 2, "selected_detail_requests": max(0, http_reads - 2),
            "returned_details": max(0, http_reads - 2), "normalized_quotes": 2,
            "http_requests": http_reads, "response_bytes": response_bytes,
        },
        "rejections": rejections or {},
        "provenance": {"provider_hosts": ["fixture"], "endpoint_classes": ["fixture"]},
        "readiness": {
            "ready": status in {"healthy", "partial"}, "routes": ["curve"],
            "reasons": [], "first_received_at": at + 0.2,
            "earliest_source_at": at, "latest_source_at": at,
            "maximum_source_age_seconds": 0.2,
        },
    }


def quote(at: float, mint: str, *, source_at: float | None = None,
          price: float = 1.0) -> MarketQuote:
    return MarketQuote(
        mint, mint[-4:], at, "curve", "Pool-" + mint, 20_000,
        price, price / 100, 5_000, 50, source="CompositePumpMarketSource",
        quote_usd=100, base_reserve=1_000_000, reserve_usd=50_000,
        real_token_reserve=500_000, supply=1_000_000_000, decimals=6,
        source_at=at if source_at is None else source_at,
        fx_at=at if source_at is None else source_at,
    )


def test_preregister_is_zero_network_revisioned_and_start_is_linked(tmp_path):
    clock = Clock()
    store = ForwardCampaignStore(tmp_path, clock=clock)
    try:
        request = build_24_hour_request()
        resources = build_24_hour_resource_model()
        first = store.preregister(request, resources, idempotency_key="prepare-1")
        repeated = store.preregister(request, resources, idempotency_key="prepare-1")
        assert repeated == first
        assert first["revision"] == 1
        assert first["network_reads"] == 0
        assert first["dry_run"]["scheduled_intervals"] == 96
        assert first["dry_run"]["maximum_http_reads"] == 288
        assert not first["dry_run"]["cost"]["fully_costed_profitability_allowed"]

        changed = copy.deepcopy(resources)
        changed["estimated_provider_response_bytes_per_request"] += 1
        with pytest.raises(ForwardCampaignError, match="different frozen inputs"):
            store.preregister(request, changed, idempotency_key="prepare-1")
        second = store.preregister(request, changed, idempotency_key="prepare-2")
        assert second["revision"] == 2
        assert second["identity_sha256"] != first["identity_sha256"]

        campaign = store.start_preregistered(
            first["id"], idempotency_key="start-1", confirmation=START_CONFIRMATION,
        )
        duplicate = store.start_preregistered(
            first["id"], idempotency_key="start-1", confirmation=START_CONFIRMATION,
        )
        assert duplicate["id"] == campaign["id"]
        assert duplicate["preset_id"] == first["id"]
        clock.value += 10
        store.command(campaign["id"], action="pause")
        clock.value += 5
        store.command(campaign["id"], action="resume")
        clock.value += 10
        store.command(campaign["id"], action="pause")
        clock.value += 5
        store.command(campaign["id"], action="resume")
        clock.value += 10
        stopped = store.command(
            campaign["id"], action="stop", confirmation=campaign["id"],
        )
        wall = stopped["progress"]["wall_seconds"]
        assert stopped["progress"]["active_seconds"] == 30
        gaps = store.paper.history(stopped["arms"][0]["paper_run_id"])["gaps"]
        assert len(gaps) == 2
        assert sum(row["ended_at"] - row["started_at"] for row in gaps) == 10
        clock.value += 1_000
        assert store.campaign(campaign["id"])["progress"]["wall_seconds"] == wall
    finally:
        store.close()


def test_pumpswap_72h_manifest_freezes_exact_pool_universe_and_hard_ceilings(tmp_path):
    store = ForwardCampaignStore(tmp_path, clock=Clock())
    try:
        request = build_pumpswap_72_hour_request()
        preset = store.preregister(
            request, build_pumpswap_72_hour_resource_model(),
            idempotency_key="pumpswap-72h-prepare",
        )
        assert preset["network_reads"] == 0
        assert preset["request"]["source"] == request["source"]
        assert request["source"]["provider"] == "CanonicalPumpSwapMarketSource"
        assert request["assumptions"]["universe_enforcement"] == "priority-only"
        assert request["assumptions"]["stop_policy"]["request_quota_lifecycle"] == (
            "retain-through-deadline"
        )
        assert preset["dry_run"]["duration_seconds"] == 72 * 60 * 60
        assert preset["dry_run"]["scheduled_intervals"] == 288
        assert preset["dry_run"]["bounded_provider_attempts"] == 288
        assert preset["dry_run"]["maximum_http_reads"] == 1_152
        assert preset["dry_run"]["estimated_storage_headroom_bytes"] > 0
        assert not preset["dry_run"]["cost"]["fully_costed_profitability_allowed"]
    finally:
        store.close()


def test_pumpswap_request_quota_stops_reads_but_lifecycle_reaches_72h(tmp_path):
    clock = Clock()
    recorder = ServiceEvidenceRecorder(tmp_path, clock=clock)
    store = ForwardCampaignStore(tmp_path, clock=clock)
    try:
        preset = store.preregister(
            build_pumpswap_72_hour_request(), build_pumpswap_72_hour_resource_model(),
            idempotency_key="pumpswap-fast-clock-prepare",
        )
        campaign = store.start_preregistered(
            preset["id"], idempotency_key="pumpswap-fast-clock-start",
            confirmation=START_CONFIRMATION,
        )
        started = clock.value
        for index in range(288):
            assert store.scan_demands(now=clock.value)
            clock.value += 0.2
            store.record_scan(
                [campaign["id"]], success=True, finished_at=clock.value,
            )
            if index < 287:
                clock.value += 900

        quota_reached = store.campaign(campaign["id"])
        assert quota_reached["state"] == "degraded"
        assert quota_reached["blocker"]["code"] == "request_quota"
        assert quota_reached["request_count"] == 288
        assert clock.value - started == pytest.approx(258_357.6)
        assert store.scan_demands(now=clock.value + 100) == []

        # An operator interruption remains resumable without widening the quota.
        store.command(campaign["id"], action="pause")
        clock.value += 300
        resumed = store.command(campaign["id"], action="resume")
        assert resumed["state"] == "running"
        assert store.scan_demands(now=resumed["deadline_at"] - 0.1) == []

        clock.value = float(resumed["deadline_at"])
        assert store.scan_demands(now=clock.value) == []
        completed = store.campaign(campaign["id"], recorder)
        assert completed["state"] == "completed"
        assert completed["request_count"] == 288
        assert completed["progress"]["wall_seconds"] == 72 * 60 * 60
        assert completed["progress"]["active_seconds"] < 72 * 60 * 60
    finally:
        store.close()
        recorder.close()


def test_pumpswap_delayed_reads_complete_at_deadline_without_exceeding_quota(tmp_path):
    clock = Clock()
    recorder = ServiceEvidenceRecorder(tmp_path, clock=clock)
    store = ForwardCampaignStore(tmp_path, clock=clock)
    try:
        campaign = store.start(
            build_pumpswap_72_hour_request(),
            idempotency_key="pumpswap-delayed-clock-start",
            confirmation=START_CONFIRMATION,
        )
        while True:
            demands = store.scan_demands(now=clock.value)
            if not demands:
                break
            clock.value += 60
            store.record_scan(
                [campaign["id"]], success=True, finished_at=clock.value,
            )
            clock.value += 900

        completed = store.campaign(campaign["id"], recorder)
        assert completed["state"] == "completed"
        assert completed["request_count"] < 288
        assert completed["progress"]["wall_seconds"] == 72 * 60 * 60
    finally:
        store.close()
        recorder.close()


def test_pumpswap_operator_resume_reuses_exact_retained_approval(tmp_path):
    operator = runpy.run_path(
        str(Path(__file__).parents[1] / "packaging" / "pumpswap_72h_observation.py")
    )
    retain = operator["_retain_approval"]
    first = {
        "schema": "pumpwizard.pumpswap-72h-operating-approval/v1",
        "recorded_at": 10_000.0,
        "approval_reference": "operator-ticket-42",
        "provider_spend_ceiling": {"amount": 12.5, "currency": "USD"},
        "preset_id": "preset-1",
        "preset_identity_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "scope": "foreground read-only canonical PumpSwap observation; no trading authority",
    }
    assert retain(tmp_path, first) == first

    resumed = copy.deepcopy(first)
    resumed["recorded_at"] = 20_000.0
    assert retain(tmp_path, resumed) == first

    changed = copy.deepcopy(resumed)
    changed["provider_spend_ceiling"]["amount"] = 13.0
    with pytest.raises(ValueError, match="different operating approval"):
        retain(tmp_path, changed)


def test_operational_summary_classifies_interruptions_resources_and_alignment(tmp_path):
    clock = Clock()
    recorder = ServiceEvidenceRecorder(tmp_path, clock=clock)
    campaigns = ForwardCampaignStore(tmp_path, clock=clock)
    worker = PaperResearchWorker(tmp_path, recorder, clock=clock)
    try:
        preset = campaigns.preregister(
            build_24_hour_request(), build_24_hour_resource_model(),
            idempotency_key="prepare",
        )
        campaign = campaigns.start_preregistered(
            preset["id"], idempotency_key="start", confirmation=START_CONFIRMATION,
        )
        campaign_id = campaign["id"]

        def complete(status: str, quotes: list[MarketQuote], *,
                     rejections: dict[str, int] | None = None) -> None:
            started = clock.value
            run_id = recorder.begin_scan(
                minimum_cap=1_000, maximum_cap=150_000,
                source="CompositePumpMarketSource", limit=3,
                campaign_consumers={campaign_id: 2},
            )
            clock.value += 0.2
            row = recorder.complete_scan(
                run_id, quotes, diagnostics=diagnostics(
                    started, status=status, rejections=rejections,
                ),
            )
            campaigns.record_scan(
                [campaign_id], success=True, finished_at=float(row["received_at"]),
            )

        complete("healthy", [
            quote(clock.value, "HealthyA"), quote(clock.value, "HealthyB"),
        ], rejections={"stale_or_unknown_source_time": 3})
        clock.value += 900
        complete("partial", [quote(clock.value, "Sparse")])
        clock.value += 900
        complete("empty", [])
        clock.value += 900
        complete("stale", [
            quote(clock.value, "Stale", source_at=clock.value - 500),
        ], rejections={"stale_or_unknown_source_time": 1})
        clock.value += 900
        started = clock.value
        failed_id = recorder.begin_scan(
            minimum_cap=1_000, maximum_cap=150_000,
            source="CompositePumpMarketSource", limit=3,
            campaign_consumers={campaign_id: 2},
        )
        clock.value += 0.2
        failed = recorder.fail_scan(
            failed_id, TimeoutError("fixture outage"),
            diagnostics=diagnostics(started, status="failing"),
        )
        campaigns.record_scan(
            [campaign_id], success=False, finished_at=float(failed["received_at"]),
            next_retry_at=float(failed["next_retry_at"]), detail="fixture outage",
        )

        cycle_start = clock.value
        totals = worker.run_once()
        cycle_finish = clock.value + 0.5
        campaigns.record_evaluation_cycle(
            campaign_id, cycle_id="cycle-1", started_at=cycle_start,
            finished_at=cycle_finish, through_received_at=clock.value,
            observations=totals["observations"], decisions=0,
            outcomes=totals["outcomes"],
        )
        summary = build_operational_summary(
            campaigns, recorder, campaign_id, as_of=cycle_finish,
        )
        assert summary["intervals"]["usable"] == 1
        assert summary["intervals"]["sparse"] == 1
        assert summary["intervals"]["empty"] == 1
        assert summary["intervals"]["stale"] == 1
        assert summary["intervals"]["outage"] == 1
        assert summary["resources"]["actual_http_reads"] == 15
        assert summary["resources"]["provider_response_bytes"] == 5 * 2_048
        assert summary["resources"]["logical_provider_attempts"] == 5
        assert summary["resources"]["allocated_provider_request_equivalents"] == 5
        assert summary["alignment"]["eligible_evidence_intersection"] > 0
        assert summary["evaluation"]["processing_lag_seconds"]["maximum"] == 0.5
        assert not summary["profitability"]["fully_costed_allowed"]
        exported = build_operational_export(summary)
        assert exported["summary"]["id"] == summary["id"]
        assert len(exported["summary_sha256"]) == 64
    finally:
        campaigns.command(
            campaign["id"], action="stop", confirmation=campaign["id"],
        )
        worker.store.close()
        campaigns.close()
        recorder.close()
