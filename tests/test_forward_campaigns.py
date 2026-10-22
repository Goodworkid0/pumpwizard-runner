from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from pumpwizard_runner.forward_campaigns import (
    REQUEST_SCHEMA,
    START_CONFIRMATION,
    ForwardCampaignError,
    ForwardCampaignStore,
)
from pumpwizard_runner.paper_research import PaperResearchStore, PaperResearchWorker
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder
from pumpwizard_runner.scheduler import MonitorScheduler
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.strategies import parse_package


class Clock:
    def __init__(self, value=1_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value


def package(label: str, *, growth=0.01):
    configuration = copy.deepcopy(CONFIG)
    configuration.update({
        "observation_seconds": 20,
        "minimum_samples": 2,
        "minimum_price_changes": 1,
        "minimum_growth_fraction": growth,
        "quote_max_age_seconds": 120,
        "failed_attempt_probability": 0,
    })
    return {
        "schema": "pumpwizard.strategy-handoff/v1",
        "strategy": {"id": label.lower().replace(" ", "-"), "label": label},
        "configuration": configuration,
    }


def request(*, max_requests=10, campaign_key="trial-1", parent=None):
    first = package("Campaign A")
    second = package("Campaign B", growth=0.02)
    return {
        "schema": REQUEST_SCHEMA,
        "campaign_key": campaign_key,
        "supersedes_campaign_id": parent,
        "source": {
            "provider": "FixtureSource", "minimum_cap": 1_000,
            "maximum_cap": 50_000, "priority_mints": ["MintCampaign"],
            "limit": 24, "cadence_seconds": 5,
        },
        "arms": [{
            "id": "arm-a", "source_session_id": "session-a",
            "strategy_sha256": parse_package(first).digest,
            "strategy_package": first, "simulated_capital_usd": 100,
        }, {
            "id": "arm-b", "source_session_id": "session-b",
            "strategy_sha256": parse_package(second).digest,
            "strategy_package": second, "simulated_capital_usd": 100,
        }],
        "assumptions": {
            "cost_model": "strategy-declared round trip",
            "execution_model": "paper replay v3; no funded fills",
        },
        "coverage": {
            "max_source_age_seconds": 120, "minimum_independent_tokens": 1,
            "minimum_observations": 10, "maximum_gap_seconds": 60,
        },
        "duration_seconds": 600,
        "quotas": {
            "max_requests": max_requests, "max_active_runtime_seconds": 600,
            "max_storage_bytes": 10_000_000, "max_concurrency": 2,
        },
        "provider_cost": {
            "currency": "USD", "marginal_cost_per_request": None,
            "fixed_subscription_cost": None, "price_basis": "unknown",
        },
    }


def quote(at: float, *, price=1.0):
    return MarketQuote(
        "MintCampaign", "CMP", at, "curve", "PoolCampaign", 20_000,
        price, price / 100, 5_000, 50, source="FixtureSource", quote_usd=100,
        base_reserve=1_000_000, reserve_usd=50_000,
        real_token_reserve=500_000, supply=1_000_000_000, decimals=6,
        source_at=at, fx_at=at,
    )


class FixtureSource:
    def __init__(self):
        self.calls = 0
        self.requests = []

    def collect(self, minimum, maximum, *, limit, priority):
        self.calls += 1
        self.requests.append((minimum, maximum, limit, set(priority)))
        return [quote(10_000)]


class WrongSource(FixtureSource):
    pass


def test_scheduler_uses_one_shared_campaign_scan_and_rejects_provider_mismatch(tmp_path):
    recorder = ServiceEvidenceRecorder(tmp_path)
    campaigns = ForwardCampaignStore(tmp_path)
    library = StrategyLibrary(tmp_path)
    source = FixtureSource()
    try:
        campaign = campaigns.start(
            request(), idempotency_key="scheduler-shared",
            confirmation=START_CONFIRMATION,
        )
        scheduler = MonitorScheduler(
            tmp_path, source_factory=lambda: source,
            recorder=recorder, campaigns=campaigns,
        )
        assert scheduler.run_cycle(library) == 0
        assert source.calls == 1
        assert source.requests[0][2:] == (24, {"MintCampaign"})
        current = campaigns.campaign(campaign["id"], recorder)
        assert current["request_count"] == 1
        assert current["usage"]["requests"] == 1
        history = recorder.provider_history(campaign_id=campaign["id"])["records"]
        assert len(history) == 1
        assert history[0]["allocations"][0]["arm_count"] == 2

        mismatch_request = request(campaign_key="provider-mismatch")
        mismatch = campaigns.start(
            mismatch_request, idempotency_key="scheduler-mismatch",
            confirmation=START_CONFIRMATION,
        )
        library.release_monitor_lease(scheduler.owner)
        wrong = WrongSource()
        mismatched_scheduler = MonitorScheduler(
            tmp_path, source_factory=lambda: wrong,
            recorder=recorder, campaigns=campaigns,
        )
        assert mismatched_scheduler.run_cycle(library) == 0
        blocked = campaigns.campaign(mismatch["id"], recorder)
        assert blocked["state"] == "paused"
        assert blocked["blocker"]["code"] == "provider_mismatch"
        assert wrong.calls == 0
        assert blocked["request_count"] == 0
    finally:
        library.close()
        campaigns.close()
        recorder.close()


def test_campaign_start_is_atomic_idempotent_and_versioned(tmp_path):
    clock = Clock()
    with ForwardCampaignStore(tmp_path, clock=clock) as store:
        first = store.start(
            request(), idempotency_key="delivery-1", confirmation=START_CONFIRMATION,
        )
        repeated = store.start(
            request(), idempotency_key="delivery-1", confirmation=START_CONFIRMATION,
        )
        assert repeated["id"] == first["id"]
        assert first["version"] == 1
        assert len(first["arms"]) == 2
        assert {row["state"] for row in first["arms"]} == {"running"}
        assert first["authority"] == {
            "wallet_allocation": False, "signer": False, "orders": False,
            "submission": False, "provider_purchase": False,
        }

        changed = request(max_requests=11)
        with pytest.raises(ForwardCampaignError, match="idempotency"):
            store.start(changed, idempotency_key="delivery-1", confirmation=START_CONFIRMATION)

        successor = store.start(
            request(campaign_key="trial-2", parent=first["id"]),
            idempotency_key="delivery-2", confirmation=START_CONFIRMATION,
        )
        assert successor["version"] == 2
        assert successor["supersedes_campaign_id"] == first["id"]

    invalid = request(campaign_key="invalid")
    invalid["arms"][1]["strategy_sha256"] = "00" * 32
    with ForwardCampaignStore(tmp_path / "invalid", clock=clock) as store:
        with pytest.raises(ForwardCampaignError, match="identity changed"):
            store.start(invalid, idempotency_key="bad", confirmation=START_CONFIRMATION)
        assert store.campaigns() == []
        assert store.db.execute("SELECT COUNT(*) FROM paper_runs").fetchone()[0] == 0


def test_duplicate_concurrent_start_creates_one_campaign_and_complete_arm_set(tmp_path):
    for attempt in range(5):
        root = tmp_path / f"cold-start-{attempt}"

        def start_once():
            with ForwardCampaignStore(root) as store:
                return store.start(
                    request(), idempotency_key="concurrent-delivery",
                    confirmation=START_CONFIRMATION,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: start_once(), range(2)))
        assert results[0]["id"] == results[1]["id"]
        with ForwardCampaignStore(root) as store:
            assert len(store.campaigns()) == 1
            assert len(store.campaigns()[0]["arms"]) == 2
            assert store.db.execute(
                "SELECT COUNT(*) FROM forward_campaign_deliveries"
            ).fetchone()[0] == 1


def test_provider_telemetry_is_redacted_shared_and_campaign_scoped(tmp_path):
    clock = Clock(2_000)
    recorder = ServiceEvidenceRecorder(tmp_path, clock=clock)
    try:
        run_id = recorder.begin_scan(
            minimum_cap=1_000, maximum_cap=50_000, source="FixtureSource",
            priority=["MintCampaign"], limit=24,
            campaign_consumers={"campaign-a": 2, "campaign-b": 1},
        )
        clock.value = 2_001
        completed = recorder.complete_scan(run_id, [quote(2_000), quote(2_000)])
        assert completed["telemetry"]["duplicate_rows"] == 1
        assert completed["telemetry"]["response_bytes"] > 0
        history = recorder.provider_history(campaign_id="campaign-a")
        assert len(history["records"]) == 1
        record = history["records"][0]
        assert record["status"] == "success"
        assert {row["campaign_id"] for row in record["allocations"]} == {
            "campaign-a", "campaign-b",
        }
        assert sum(row["shared_request_fraction"] for row in record["allocations"]) == 1
        assert recorder.provider_usage("campaign-a")["requests"] == 1

        failed_id = recorder.begin_scan(
            minimum_cap=1_000, maximum_cap=50_000, source="FixtureSource",
            priority=["MintCampaign"], limit=24,
            campaign_consumers={"campaign-a": 2},
        )
        clock.value = 2_002
        failed = recorder.fail_scan(
            failed_id, TimeoutError("https://provider.invalid/data?api_key=secret timed out")
        )
        assert failed["gap_reason"] == "https://provider.invalid/data?[redacted]"
        assert failed["telemetry"]["error_class"] == "timeout"
        assert failed["next_retry_at"] > clock.value
        encoded = str(recorder.provider_history(campaign_id="campaign-a"))
        assert "api_key=secret" not in encoded
    finally:
        recorder.close()


def test_campaign_arms_share_only_attributed_evidence_and_pause_backlog(tmp_path):
    clock = Clock(3_000)
    with ForwardCampaignStore(tmp_path, clock=clock) as campaigns:
        campaign = campaigns.start(
            request(), idempotency_key="start", confirmation=START_CONFIRMATION,
        )
    recorder = ServiceEvidenceRecorder(tmp_path, clock=clock)
    worker = PaperResearchWorker(tmp_path, recorder, clock=clock)
    try:
        attributed = recorder.begin_scan(
            minimum_cap=1_000, maximum_cap=50_000, source="FixtureSource",
            priority=["MintCampaign"], limit=24,
            campaign_consumers={campaign["id"]: 2},
        )
        completed_attributed = recorder.complete_scan(
            attributed, [quote(3_000)], received_at=3_001,
        )
        with ForwardCampaignStore(tmp_path, clock=clock) as campaigns:
            campaigns.record_scan(
                [campaign["id"]], success=True,
                finished_at=float(completed_attributed["received_at"]),
            )
        unrelated = recorder.begin_scan(
            minimum_cap=1_000, maximum_cap=50_000, source="OtherSource", limit=24,
        )
        recorder.complete_scan(unrelated, [quote(3_000, price=2)], received_at=3_002)
        clock.value = 3_003
        totals = worker.run_once()
        assert totals["observations"] == 2
        clock.value = 3_004
        paused_scan = recorder.begin_scan(
            minimum_cap=1_000, maximum_cap=50_000, source="FixtureSource",
            priority=["MintCampaign"], limit=24,
            campaign_consumers={campaign["id"]: 2},
            requested_at=3_004, started_at=3_004,
        )
        clock.value = 3_004.1
        with ForwardCampaignStore(tmp_path, clock=clock) as campaigns:
            current = campaigns.campaign(campaign["id"], recorder)
            assert {arm["counts"]["observations"] for arm in current["arms"]} == {1}
            campaigns.command(campaign["id"], action="pause")

        completed = recorder.complete_scan(
            paused_scan, [quote(3_004, price=1.1)], received_at=3_005,
        )
        clock.value = 3_006
        with ForwardCampaignStore(tmp_path, clock=clock) as campaigns:
            campaigns.record_scan(
                [campaign["id"]], success=True,
                finished_at=float(completed["received_at"]),
            )
            paused = campaigns.campaign(campaign["id"], recorder)
            assert paused["state"] == "paused"
            assert paused["request_count"] == 2
            assert paused["usage"]["requests"] == 2
            campaigns.command(campaign["id"], action="resume")
        clock.value = 3_007
        worker.run_once()
        with PaperResearchStore(tmp_path, clock=clock) as paper:
            for arm in campaign["arms"]:
                observations = paper.history(arm["paper_run_id"])["observations"]
                paused = next(row for row in observations if row["first_received_at"] == 3_005)
                assert paused["quality"]["status"] == "excluded"
                assert "outside-operating-interval" in paused["quality"]["reasons"]
    finally:
        worker.close()
        recorder.close()


def test_hard_request_quota_caps_all_arms_without_retry_storm(tmp_path):
    clock = Clock(4_000)
    recorder = ServiceEvidenceRecorder(tmp_path, clock=clock)
    try:
        with ForwardCampaignStore(tmp_path, clock=clock) as store:
            campaign = store.start(
                request(max_requests=1), idempotency_key="quota",
                confirmation=START_CONFIRMATION,
            )
            store.record_scan([campaign["id"]], success=True, finished_at=4_001)
            capped = store.campaign(campaign["id"], recorder)
            assert capped["state"] == "capped"
            assert capped["blocker"]["code"] == "request_quota"
            assert {row["state"] for row in capped["arms"]} == {"paused"}
            assert store.scan_demands(now=4_010) == []
            with pytest.raises(ForwardCampaignError, match="cannot widen"):
                store.command(campaign["id"], action="resume")
            stopped = store.command(
                campaign["id"], action="stop", confirmation=campaign["id"],
            )
            assert stopped["state"] == "stopped"
            for arm in stopped["arms"]:
                gaps = store.paper.history(arm["paper_run_id"])["gaps"]
                assert gaps[0]["ended_at"] == 4_001
    finally:
        recorder.close()


def test_runtime_and_storage_ceilings_cap_with_explicit_reasons(tmp_path):
    runtime_clock = Clock(5_000)
    runtime_recorder = ServiceEvidenceRecorder(tmp_path / "runtime", clock=runtime_clock)
    try:
        runtime_request = request(campaign_key="runtime-ceiling")
        runtime_request["quotas"]["max_active_runtime_seconds"] = 10
        with ForwardCampaignStore(tmp_path / "runtime", clock=runtime_clock) as store:
            campaign = store.start(
                runtime_request, idempotency_key="runtime", confirmation=START_CONFIRMATION,
            )
            runtime_clock.value += 11
            assert store.reconcile_limits(runtime_recorder)["capped"] == 1
            capped = store.campaign(campaign["id"], runtime_recorder)
            assert capped["state"] == "capped"
            assert capped["blocker"]["code"] == "runtime_quota"
    finally:
        runtime_recorder.close()

    storage_clock = Clock(6_000)
    storage_dir = tmp_path / "storage"
    storage_recorder = ServiceEvidenceRecorder(storage_dir, clock=storage_clock)
    worker = PaperResearchWorker(storage_dir, storage_recorder, clock=storage_clock)
    try:
        storage_request = request(campaign_key="storage-ceiling")
        storage_request["quotas"]["max_storage_bytes"] = 1_024
        with ForwardCampaignStore(storage_dir, clock=storage_clock) as store:
            campaign = store.start(
                storage_request, idempotency_key="storage", confirmation=START_CONFIRMATION,
            )
            run_id = storage_recorder.begin_scan(
                minimum_cap=1_000, maximum_cap=50_000, source="FixtureSource",
                priority=["MintCampaign"], limit=24,
                campaign_consumers={campaign["id"]: 2},
            )
            storage_recorder.complete_scan(run_id, [quote(storage_clock.value)])
            worker.run_once()
            assert store.reconcile_limits(storage_recorder)["capped"] == 1
            capped = store.campaign(campaign["id"], storage_recorder)
            assert capped["state"] == "capped"
            assert capped["blocker"]["code"] == "storage_quota"
    finally:
        worker.close()
        storage_recorder.close()


def test_campaigns_share_only_an_exact_frozen_scan_identity(tmp_path):
    recorder = ServiceEvidenceRecorder(tmp_path)
    campaigns = ForwardCampaignStore(tmp_path)
    library = StrategyLibrary(tmp_path)
    source = FixtureSource()
    try:
        first_request = request(max_requests=1, campaign_key="chunk-a")
        first_request["source"]["priority_mints"] = [
            f"A-priority-{index:02d}" for index in range(30)
        ]
        first_request["source"]["limit"] = 30
        second_request = request(max_requests=1, campaign_key="chunk-b")
        second_request["source"] = copy.deepcopy(first_request["source"])
        first = campaigns.start(
            first_request, idempotency_key="chunk-a", confirmation=START_CONFIRMATION,
        )
        second = campaigns.start(
            second_request, idempotency_key="chunk-b", confirmation=START_CONFIRMATION,
        )
        scheduler = MonitorScheduler(
            tmp_path, source_factory=lambda: source,
            recorder=recorder, campaigns=campaigns,
        )

        assert scheduler.run_cycle(library) == 0
        assert source.calls == 1
        assert source.requests[0][2:] == (
            30, set(first_request["source"]["priority_mints"]),
        )
        for campaign in (first, second):
            view = campaigns.campaign(campaign["id"], recorder)
            assert view["state"] == "capped"
            assert view["request_count"] == 1
            assert view["usage"]["requests"] == 1
    finally:
        library.close()
        campaigns.close()
        recorder.close()


def test_distinct_campaign_universes_are_not_silently_broadened_or_shared(tmp_path):
    recorder = ServiceEvidenceRecorder(tmp_path)
    campaigns = ForwardCampaignStore(tmp_path)
    library = StrategyLibrary(tmp_path)
    source = FixtureSource()
    try:
        first_request = request(max_requests=1, campaign_key="frozen-a")
        first_request["source"]["limit"] = 3
        second_request = request(max_requests=1, campaign_key="frozen-b")
        second_request["source"]["limit"] = 4
        first = campaigns.start(
            first_request, idempotency_key="frozen-a", confirmation=START_CONFIRMATION,
        )
        second = campaigns.start(
            second_request, idempotency_key="frozen-b", confirmation=START_CONFIRMATION,
        )
        scheduler = MonitorScheduler(
            tmp_path, source_factory=lambda: source,
            recorder=recorder, campaigns=campaigns,
        )

        assert scheduler.run_cycle(library) == 0
        assert source.calls == 2
        assert sorted(request[2] for request in source.requests) == [3, 4]
        assert all(request[3] == {"MintCampaign"} for request in source.requests)
        for campaign in (first, second):
            view = campaigns.campaign(campaign["id"], recorder)
            assert view["request_count"] == 1
            assert view["usage"]["requests"] == 1
    finally:
        library.close()
        campaigns.close()
        recorder.close()


def test_campaign_rejects_priority_universe_larger_than_frozen_limit(tmp_path):
    value = request(campaign_key="invalid-universe")
    value["source"]["priority_mints"] = ["MintA", "MintB"]
    value["source"]["limit"] = 1
    with ForwardCampaignStore(tmp_path) as campaigns:
        with pytest.raises(ForwardCampaignError, match="priority universe"):
            campaigns.start(
                value, idempotency_key="invalid-universe",
                confirmation=START_CONFIRMATION,
            )


def test_running_campaign_recovery_requires_explicit_resume(tmp_path):
    clock = Clock(5_000)
    with ForwardCampaignStore(tmp_path, clock=clock) as store:
        campaign = store.start(
            request(), idempotency_key="restart", confirmation=START_CONFIRMATION,
        )
    clock.value = 5_100
    with ForwardCampaignStore(tmp_path, clock=clock) as restarted:
        assert restarted.recover_running() == 1
        recovered = restarted.campaign(campaign["id"])
        assert recovered["state"] == "paused"
        assert recovered["blocker"]["code"] == "service_restart"
        assert {row["state"] for row in recovered["arms"]} == {"paused"}
        resumed = restarted.command(campaign["id"], action="resume")
        assert resumed["state"] == "running"
        assert {row["state"] for row in resumed["arms"]} == {"running"}
