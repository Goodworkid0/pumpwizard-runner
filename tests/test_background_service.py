import json
from pathlib import Path
import time

import pytest

from pumpwizard_runner.background_service import (
    BackgroundServiceEngine,
    BackgroundServiceStore,
    ServiceOwnershipError,
)
from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.forward_campaigns import START_CONFIRMATION
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.market import MarketDataError
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.transfers import GENESIS, TransferError
from test_forward_campaigns import request


def agent(data_dir: Path) -> str:
    package = json.loads(
        (Path(__file__).parents[1] / "examples/xyz-local-strategy.json").read_text()
    )
    strategy = parse_package(package)
    library = StrategyLibrary(data_dir)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        library.set_tracking(instance_id, True)
        return instance_id
    finally:
        library.close()


class Source:
    def __init__(self):
        self.calls = 0

    def collect(self, _low, _high, *, limit, priority):
        assert 1 <= limit <= 100
        self.calls += 1
        now = time.time()
        return [MarketQuote(
            "Mint", "TEST", now, "curve", "Pool", 15_000, 1, .01,
            1_000, 10, source="service-fixture", quote_usd=100,
            source_at=now - 1,
        )]


class CampaignSource(Source):
    pass


CampaignSource.__name__ = "FixtureSource"


class FailingSource:
    def __init__(self):
        self.calls = 0

    def collect(self, *_args, **_kwargs):
        self.calls += 1
        raise RuntimeError("fixture provider offline")


class RecoveringSource:
    def __init__(self):
        self.calls = 0
        self.offline = True
        self.observed_at = time.time()

    def collect(self, *_args, **_kwargs):
        self.calls += 1
        if self.offline:
            raise MarketDataError("fixture provider offline")
        return [MarketQuote(
            "Mint", "TEST", self.observed_at, "curve", "Pool", 15_000, 1, .01,
            1_000, 10, source="recovering-fixture", quote_usd=100,
            source_at=self.observed_at - 1,
        )]


class Signer:
    def __init__(self):
        self.active = True
        self.events = []

    def state(self):
        return {
            "schema": "pumpwizard.service-trading-state/v1",
            "authorization_status": "active" if self.active else "blocked",
            "signer_state": "unlocked" if self.active else "locked",
            "production_activation": "synthetic-only",
            "entries_paused": not self.active,
            "blockers": [],
        }

    def activate(self):
        self.active = True

    def pause_entries(self, reason):
        self.events.append(("pause", reason))
        self.active = False
        return self.state()

    def resume_entries(self, authorization):
        assert authorization == {"authorization_id": "fixture"}
        self.active = True
        return self.state()

    def revoke(self, reason):
        self.events.append(("revoke", reason))
        self.active = False
        return self.state()

    def lock(self, reason):
        self.events.append(("lock", reason))
        self.active = False
        return self.state()

    def reconcile(self):
        self.events.append(("reconcile", "original signatures"))
        return {"reconciled": 1}

    def run_cycle(self):
        self.events.append(("cycle", "synthetic"))
        return {"prepared": 1}


class AcceptanceSigner(Signer):
    def __init__(self, root):
        super().__init__()
        self.vault = type("Vault", (), {"root": root})()

    def enroll(self, *_args, **_kwargs):
        return {}

    def review(self, *_args, **_kwargs):
        return {}

    def authorize(self, *_args, **_kwargs):
        return {}

    def start(self, *_args, **_kwargs):
        return self.state()

    def allowances(self, sender, network):
        self.events.append(("allowances", sender, network))
        return [{"id": "allowance"}]

    def acceptance_request(self, **values):
        self.events.append(("acceptance-request", values))
        return {"message": "exact-message", "payload": {"allowance_id": "allowance"}}

    def install_acceptance(self, payload, signature):
        self.events.append(("acceptance-install", payload, signature))
        return self.state()

    def acceptance_evidence(self, approval_id):
        self.events.append(("acceptance-evidence", approval_id))
        return {
            "allowance": {"sender": "wallet", "genesis": GENESIS["devnet"]},
            "status": "reconciled",
        }


def wait_for(predicate, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("Timed out waiting for background service state")


def test_read_only_job_is_opt_in_persists_and_collects_without_a_browser(tmp_path: Path):
    agent(tmp_path)
    source = Source()
    engine = BackgroundServiceEngine(tmp_path, source_factory=lambda: source)
    started = engine.start()
    assert started["jobs"][0]["enabled"] is False
    time.sleep(.3)
    assert source.calls == 0

    job = engine.configure_job({"enabled": True, "interval_seconds": 5})
    assert job["enabled"] is True
    wait_for(lambda: source.calls == 1)
    wait_for(lambda: bool(engine.status()["coverage"]))
    status = engine.status()
    assert status["service"]["health"] == "healthy"
    assert status["coverage"][0]["source"] == "Source"
    assert status["coverage"][0]["status"] == "complete"
    stopped = engine.stop()
    assert stopped["stopped"] is True

    reopened = BackgroundServiceStore(tmp_path)
    try:
        assert reopened.job()["enabled"] is True
        assert reopened.status()["state"] == "stopped"
    finally:
        reopened.close()


def test_single_owner_and_restart_lock_signing_before_service_cycles(tmp_path: Path):
    signer = Signer()
    first = BackgroundServiceEngine(tmp_path, signer=signer)
    first.start()
    assert signer.events[0][0] == "lock"
    second = BackgroundServiceEngine(tmp_path)
    with pytest.raises(ServiceOwnershipError, match="owns this data directory"):
        second.start()

    signer.activate()
    wait_for(lambda: any(event[0] == "cycle" for event in signer.events))
    reconcile_index = next(index for index, event in enumerate(signer.events)
                           if event[0] == "reconcile")
    cycle_index = next(index for index, event in enumerate(signer.events)
                       if event[0] == "cycle")
    assert reconcile_index < cycle_index
    first.pause_entries()
    assert signer.state()["entries_paused"] is True
    assert first.stop()["stopped"] is True
    assert signer.events[-1][0] == "lock"

    restarted_signer = Signer()
    restarted = BackgroundServiceEngine(tmp_path, signer=restarted_signer)
    restarted.start()
    assert restarted_signer.state()["signer_state"] == "locked"
    restarted.stop()


def test_provider_failure_marks_service_degraded_and_uses_persisted_backoff(tmp_path: Path):
    agent(tmp_path)
    source = FailingSource()
    engine = BackgroundServiceEngine(tmp_path, source_factory=lambda: source)
    engine.start()
    try:
        engine.configure_job({"enabled": True, "interval_seconds": 5})
        wait_for(lambda: engine.status()["jobs"][0]["consecutive_failures"] == 1)
        wait_for(lambda: engine.status()["service"]["health"] == "degraded")
        status = engine.status()
        assert source.calls == 1
        assert status["service"]["health"] == "degraded"
        assert status["jobs"][0]["next_at"] > status["jobs"][0]["last_error_at"]
        assert status["coverage"][0]["status"] == "gap"
        assert status["coverage"][0]["gap_reason"] == "fixture provider offline"
        time.sleep(.5)
        assert source.calls == 1
    finally:
        engine.stop()


def test_campaign_lease_failure_uses_shared_backoff_without_retry_storm(tmp_path: Path):
    lease = StrategyLibrary(tmp_path)
    try:
        assert lease.claim_monitor_lease("foreign-scheduler", time.time(), ttl=180)
    finally:
        lease.close()
    source = CampaignSource()
    engine = BackgroundServiceEngine(tmp_path, source_factory=lambda: source)
    engine.start()
    try:
        campaign = engine.campaigns.start(
            request(max_requests=1), idempotency_key="foreign-lease",
            confirmation=START_CONFIRMATION,
        )
        engine.wake_event.set()
        wait_for(lambda: engine.status()["jobs"][0]["consecutive_failures"] == 1)
        time.sleep(.75)
        assert source.calls == 0
        assert engine.campaigns.campaign(campaign["id"])["request_count"] == 0
        assert engine.store.db.execute(
            "SELECT COUNT(*) FROM service_job_runs"
        ).fetchone()[0] == 1
    finally:
        engine.stop()


def test_service_restart_reclaims_its_unexpired_scheduler_lease(tmp_path: Path):
    first = BackgroundServiceEngine(tmp_path)
    first.start()
    owner = first.scheduler.owner
    first.stop()

    lease = StrategyLibrary(tmp_path)
    try:
        assert lease.claim_monitor_lease(owner, time.time(), ttl=180)
    finally:
        lease.close()

    source = CampaignSource()
    restarted = BackgroundServiceEngine(tmp_path, source_factory=lambda: source)
    restarted.start()
    try:
        assert restarted.scheduler.owner == owner
        campaign = restarted.campaigns.start(
            request(max_requests=1), idempotency_key="restart-lease",
            confirmation=START_CONFIRMATION,
        )
        restarted.wake_event.set()
        wait_for(
            lambda: restarted.campaigns.campaign(campaign["id"])["state"] == "capped"
        )
        assert source.calls == 1
    finally:
        restarted.stop()


def test_provider_recovery_and_restart_keep_the_gap_without_duplicate_evidence(tmp_path: Path):
    instance_id = agent(tmp_path)
    source = RecoveringSource()
    engine = BackgroundServiceEngine(tmp_path, source_factory=lambda: source)
    engine.start()
    try:
        engine.configure_job({"enabled": True, "interval_seconds": 5})
        wait_for(lambda: engine.status()["jobs"][0]["consecutive_failures"] == 1)
        source.offline = False
        prior_calls = source.calls
        with engine.store.db:
            engine.store.db.execute(
                "UPDATE service_jobs SET next_at=0 WHERE id='strategy-monitor'"
            )
        library = StrategyLibrary(tmp_path)
        try:
            with library._connection:
                library._connection.execute(
                    "UPDATE monitor_settings SET next_at=0 WHERE instance_id=?",
                    (instance_id,),
                )
        finally:
            library.close()
        engine.wake_event.set()
        wait_for(lambda: source.calls > prior_calls)
        wait_for(lambda: engine.status()["jobs"][0]["consecutive_failures"] == 0)
        wait_for(lambda: any(row["status"] == "complete" for row in engine.status()["coverage"]))
        coverage = engine.status()["coverage"]
        assert any(row["status"] == "gap" for row in coverage)
        assert any(row["status"] == "complete" for row in coverage)
        assert len(engine.recorder.raw_evidence()) == 1
    finally:
        engine.stop()

    restarted = BackgroundServiceEngine(tmp_path, source_factory=lambda: source)
    restarted.start()
    try:
        with restarted.store.db:
            restarted.store.db.execute(
                "UPDATE service_jobs SET next_at=0 WHERE id='strategy-monitor'"
            )
        library = StrategyLibrary(tmp_path)
        try:
            with library._connection:
                library._connection.execute(
                    "UPDATE monitor_settings SET next_at=0 WHERE instance_id=?",
                    (instance_id,),
                )
        finally:
            library.close()
        restarted.wake_event.set()
        prior_calls = source.calls
        wait_for(lambda: source.calls > prior_calls)
        wait_for(lambda: restarted.status()["jobs"][0]["consecutive_failures"] == 0)
        assert len(restarted.recorder.raw_evidence()) == 1
        assert any(row["status"] == "gap" for row in restarted.status()["coverage"])
    finally:
        restarted.stop()


def test_service_state_has_opaque_directory_identity_and_no_secret_fields(tmp_path: Path):
    engine = BackgroundServiceEngine(tmp_path)
    status = engine.start()
    try:
        encoded = json.dumps(status)
        assert str(tmp_path) not in encoded
        assert "token" not in encoded.lower()
        assert status["service"]["instance_id"]
        assert len(status["service"]["data_directory_id"]) == 64
        assert status["trading"]["production_activation"] == "blocked"
        assert status["trading"]["activation_capability"] == {
            "available": False,
            "mode": "blocked",
            "blockers": [
                "Live main-wallet service signing has no funded lifecycle acceptance",
                "This build exposes only an injected synthetic service runtime",
            ],
        }
        with pytest.raises(TransferError, match="locked"):
            engine.resume_entries({"authorization_id": "caller-cannot-enable"})
    finally:
        engine.stop()


def test_acceptance_controls_bind_authenticated_wallet_allowance_and_network(tmp_path: Path):
    signer = AcceptanceSigner(tmp_path)
    engine = BackgroundServiceEngine(tmp_path, signer=signer)
    request = engine.review_signing_acceptance({
        "sender": "wallet",
        "network": "devnet",
        "allowance_id": "allowance",
        "routes": ["route"],
        "duration_seconds": 300,
    })
    assert request["message"] == "exact-message"
    with pytest.raises(ValueError, match="does not belong"):
        engine.review_signing_acceptance({
            "sender": "wallet",
            "network": "devnet",
            "allowance_id": "another",
            "routes": ["route"],
            "duration_seconds": 300,
        })
    with pytest.raises(ValueError, match="another wallet or network"):
        engine.install_signing_acceptance({
            "sender": "wallet",
            "network": "devnet",
            "payload": {
                "sender": "attacker",
                "network": "devnet",
                "allowance_id": "allowance",
            },
            "signature": "signature",
        })
    state = engine.install_signing_acceptance({
        "sender": "wallet",
        "network": "devnet",
        "payload": {
            "sender": "wallet",
            "network": "devnet",
            "allowance_id": "allowance",
        },
        "signature": "signature",
    })
    assert state["signer_state"] == "unlocked"
    evidence = engine.signing_acceptance_evidence("wallet", "devnet", "approval")
    assert evidence["status"] == "reconciled"
    with pytest.raises(ValueError, match="another network"):
        engine.signing_acceptance_evidence("wallet", "mainnet-beta", "approval")


def test_restart_records_unclean_predecessor_and_preserves_job_configuration(tmp_path: Path):
    first = BackgroundServiceStore(tmp_path)
    prior = first.start_instance(port=8781)
    first.configure_job(enabled=True, interval_seconds=30)
    first.close()  # Simulate process loss: no stop_instance call is made.

    restarted = BackgroundServiceStore(tmp_path)
    try:
        current = restarted.start_instance(port=8781)
        previous = restarted.db.execute(
            "SELECT state,current_activity,stop_reason FROM service_instances WHERE instance_id=?",
            (prior["instance_id"],),
        ).fetchone()
        assert current["instance_revision"] == prior["instance_revision"] + 1
        assert dict(previous) == {
            "state": "stopped",
            "current_activity": "interrupted",
            "stop_reason": "Previous service ended without a clean shutdown",
        }
        assert restarted.job()["enabled"] is True
        assert restarted.job()["interval_seconds"] == 30
    finally:
        restarted.close()
