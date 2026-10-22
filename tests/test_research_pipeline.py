from __future__ import annotations

import copy
from pathlib import Path
import time

import pytest

from pumpwizard_runner.decision import Decision, MarketQuote
from pumpwizard_runner.background_service import BackgroundServiceEngine
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.paper_research import (
    PaperResearchStore,
    PaperResearchWorker,
    START_CONFIRMATION,
)
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.portfolio_replay import ENGINE_VERSION as PORTFOLIO_REPLAY_ENGINE_VERSION
from pumpwizard_runner.research_data import (
    ResearchDataStore,
    build_dataset_export,
)
from pumpwizard_runner.research_jobs import (
    REPORT_SCHEMA,
    ResearchJobWorker,
    ResearchRegistry,
)
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder
from pumpwizard_runner.service_backup import create_backup, restore_backup
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.web_api import command, payload


SESSION = "research-session"


def package(*, order_usd=10):
    configuration = copy.deepcopy(CONFIG)
    configuration.update({
        "entry_style": "momentum",
        "order_usd": order_usd,
        "observation_seconds": 60,
        "quote_max_age_seconds": 120,
    })
    return {
        "schema": "pumpwizard.strategy-handoff/v1",
        "strategy": {"id": f"research-{order_usd}", "label": "Research fixture"},
        "configuration": configuration,
    }


def quote(at: float, *, mint="Mint", price=1.0, pool="Pool"):
    return MarketQuote(
        mint, mint[-4:], at, "curve", pool, 20_000 * price,
        price * 100, price, 5_000, 50, source="fixture-provider",
        quote_usd=100, base_reserve=1_000_000, reserve_usd=50_000,
        real_token_reserve=500_000, supply=1_000_000_000, decimals=6,
        source_at=at, fx_at=at,
    )


def dataset_export(*, extra=False, unknown_receipt=False, session=SESSION):
    selected = package()
    observations = []
    recorder_rows = []
    stop = 13 if extra else 12
    for index in range(stop):
        at = 100 + index * 100
        payload = quote(at, price=1 + index / 100).as_record()
        observations.append({**payload, "instance_id": session})
        recorder_rows.append({
            "id": f"evidence-{index}", "fingerprint": f"fingerprint-{index}",
            "mint": "Mint", "source": "fixture-provider", "route": "curve",
            "pool": "Pool", "observed_at": at, "source_at": at,
            "source_time_basis": "inferred-observed-at",
            "first_received_at": None if unknown_receipt and index == 0 else at + 10,
            "first_known_basis": ("unknown" if unknown_receipt and index == 0
                                  else "recorder-first-receipt"),
            "run_ids": ["run-1"], "payload": payload,
        })
    snapshot = {
        "source_session_id": session,
        "strategy_digest": parse_package(selected).digest,
        "strategy_package": selected,
        "first_at": 100.0,
        "last_at": float(100 + (stop - 1) * 100),
        "observations": observations,
        "decisions": [], "outcomes": [], "feature_records": [],
        "decision_capture_complete": True,
    }
    recorder = {
        "schema": "pumpwizard.recorder-research-snapshot/v1",
        "range": {"first_event_at": snapshot["first_at"],
                  "last_event_at": snapshot["last_at"]},
        "mints": ["Mint"], "evidence": recorder_rows,
        "collection_runs": [{
            "schema": "pumpwizard.collector-coverage/v1", "id": "run-1",
            "started_at": 90.0, "received_at": snapshot["last_at"] + 10,
            "status": "complete", "gap_reason": "",
        }],
        "canonical_bytes": 1,
    }
    return build_dataset_export(snapshot, recorder, network="devnet", caps={
        "evidence_rows": 20_000, "decisions": 5_000, "features": 5_000,
        "outcomes": 5_000, "collection_runs": 5_000,
        "canonical_bytes": 32 * 1024 * 1024,
    })


def save_dataset(root: Path, *, extra=False, unknown_receipt=False, session=SESSION):
    store = ResearchDataStore(root, clock=lambda: 5_000)
    try:
        return store.save(dataset_export(
            extra=extra, unknown_receipt=unknown_receipt, session=session,
        ))
    finally:
        store.close()


def request(dataset, *, candidate=None, purpose="eligible"):
    return {
        "dataset_id": dataset["dataset"]["id"],
        "candidates": [candidate or package()],
        "cost_model": {"fee_fraction": 0.01, "fixed_fee_usd": 0.10},
        "capital": {"amount": 500, "unit": "USD"},
        "splits": {
            "train_end_at": 400.0,
            "validation_end_at": 700.0,
            "holdout_end_at": 1_100.0,
        },
        "embargo_seconds": 10,
        "objective": "maximize finite-capital terminal equity with coverage",
        "evidence_policy": {"horizon_seconds": 300, "missing": "unknown"},
        "purpose": purpose,
        "engine_version": "fixture-replay/1",
        "code_version": "fixture-source",
    }


def completed_report(dataset, experiment, progress, cancelled):
    assert dataset["schema"] == "pumpwizard.research-export/v1"
    assert not cancelled()
    progress(0.5)
    return {
        "schema": REPORT_SCHEMA,
        "experiment_id": experiment["id"], "dataset_id": experiment["dataset_id"],
        "engine_version": "fixture-replay/1", "capital": experiment["capital"],
        "cost_model": experiment["cost_model"], "purpose": experiment["purpose"],
        "eligible": experiment["purpose"] == "eligible", "comparisons": [],
        "baselines": ["cash"], "coverage": {}, "exclusions": [],
        "unknown_or_unpriced": 0,
        "recommendation": {"action": "keep_current"}, "blockers": [],
    }


def test_dataset_is_deterministic_point_in_time_and_independent_of_recorder(tmp_path):
    first = dataset_export()
    second = dataset_export()
    assert first == second
    assert first["dataset"]["eligibility"] == {
        "eligible": True, "reasons": [], "supported_family": "momentum",
        "decision_capture_complete": True,
    }
    store = ResearchDataStore(tmp_path)
    try:
        saved = store.save(first)
        assert store.save(second) == saved
        page = store.point_in_time(saved["dataset"]["id"], known_by=510, limit=100)
        assert [row["times"]["first_received_at"] for row in page["records"]] == [
            110.0, 210.0, 310.0, 410.0, 510.0,
        ]
        before = store.export(saved["dataset"]["id"])
        recorder = ServiceEvidenceRecorder(tmp_path / "operational", clock=lambda: 10_000)
        try:
            run = recorder.begin_scan(
                minimum_cap=10_000, maximum_cap=30_000, source="fixture",
                limit=10,
            )
            recorder.complete_scan(run, [quote(100)], received_at=10_000)
            recorder.compact(now=20_000, retention_seconds=3_600,
                             max_evidence=100, max_runs=100)
        finally:
            recorder.close()
        assert store.export(saved["dataset"]["id"]) == before
    finally:
        store.close()


def test_unknown_receipt_is_not_backdated_or_point_in_time_visible(tmp_path):
    export = save_dataset(tmp_path, unknown_receipt=True)
    assert export["dataset"]["eligibility"]["reasons"] == ["unknown-first-receipt"]
    unknown = export["records"]["observations"][0]
    assert unknown["times"]["first_received_at"] is None
    assert unknown["times"]["first_known_basis"] == "unknown"
    store = ResearchDataStore(tmp_path)
    try:
        page = store.point_in_time(export["dataset"]["id"], known_by=10_000, limit=100)
        assert unknown["evidence_id"] not in {row["evidence_id"] for row in page["records"]}
    finally:
        store.close()


def test_registered_attempts_are_idempotent_and_interrupted_attempt_reuses_identity(tmp_path):
    dataset = save_dataset(tmp_path)
    registry = ResearchRegistry(tmp_path, clock=lambda: 2_000)
    try:
        experiment = registry.register(request(dataset))
        assert registry.register(request(dataset))["id"] == experiment["id"]
        job = registry.enqueue(experiment["id"], idempotency_key="first")
        claimed = registry.claim_next(worker_token="worker-one")
        assert claimed["id"] == job["id"] and claimed["state"] == "running"
        attempt_id = claimed["attempt_id"]
    finally:
        registry.close()

    restarted = ResearchRegistry(tmp_path, clock=lambda: 2_100)
    try:
        assert restarted.recover_interrupted() == 1
        claimed = restarted.claim_next(worker_token="worker-two")
        assert claimed["attempt_id"] == attempt_id
        assert restarted.attempts(experiment["id"])[0]["restart_count"] == 1
        restarted.complete(claimed["id"], completed_report(
            dataset, experiment, lambda _: None, lambda: False,
        ))
        reused = restarted.enqueue(experiment["id"], idempotency_key="second-key")
        assert reused["reused"] is True and reused["id"] == job["id"]
        assert len(restarted.attempts(experiment["id"])) == 1
    finally:
        restarted.close()


def test_service_job_calls_worker2_finite_capital_replay_without_authority(tmp_path):
    dataset = save_dataset(tmp_path)
    registry = ResearchRegistry(tmp_path, clock=lambda: 2_500)
    try:
        experiment = registry.register(request(dataset))
        job = registry.enqueue(experiment["id"], idempotency_key="worker2-replay")
    finally:
        registry.close()
    worker = ResearchJobWorker(tmp_path, clock=lambda: 2_600)
    try:
        completed = worker.run_once()
        assert completed["id"] == job["id"]
        assert completed["state"] == "completed"
        report = worker.registry.result(job["id"])
        assert report["schema"] == REPORT_SCHEMA
        assert report["engine_version"] == PORTFOLIO_REPLAY_ENGINE_VERSION
        assert report["comparisons"][0]["kind"] == "cash"
        assert report["funded_performance"] is False
        assert report["submission_enabled"] is False
        assert report["wallet_allocation_lamports"] == "0"
        assert report["signing_authority"] is False
    finally:
        worker.close()


def test_background_service_completes_research_without_a_browser(tmp_path):
    dataset = save_dataset(tmp_path)
    registry = ResearchRegistry(tmp_path)
    try:
        experiment = registry.register(request(dataset))
        job = registry.enqueue(experiment["id"], idempotency_key="browser-free")
    finally:
        registry.close()
    engine = BackgroundServiceEngine(tmp_path, port=8801)
    engine.start()
    try:
        deadline = time.monotonic() + 5
        state = "queued"
        while time.monotonic() < deadline:
            registry = ResearchRegistry(tmp_path)
            try:
                state = registry.job(job["id"])["state"]
            finally:
                registry.close()
            if state in {"completed", "failed"}:
                break
            time.sleep(0.05)
        assert state == "completed"
        status = engine.status()
        assert status["trading"]["production_activation"] == "blocked"
        assert status["research_jobs"][0]["state"] == "completed"
    finally:
        engine.stop()


def test_failure_cancellation_and_overlapping_holdout_never_reset_history(tmp_path):
    first_dataset = save_dataset(tmp_path)
    registry = ResearchRegistry(tmp_path, clock=lambda: 3_000)
    try:
        first = registry.register(request(first_dataset))
        failed = registry.enqueue(first["id"], idempotency_key="failed")
        registry.claim_next()
        registry.fail(failed["id"], "fixture replay failure")
        cancelled = registry.enqueue(first["id"], idempotency_key="cancelled")
        registry.cancel(cancelled["id"])
        completed = registry.enqueue(first["id"], idempotency_key="completed")
    finally:
        registry.close()
    worker = ResearchJobWorker(tmp_path, evaluator=completed_report, clock=lambda: 3_100)
    try:
        worker.run_once()
        assert worker.registry.job(completed["id"])["state"] == "completed"
        assert [row["status"] for row in worker.registry.attempts(first["id"])] == [
            "failed", "cancelled", "completed",
        ]
    finally:
        worker.close()

    second_dataset = save_dataset(tmp_path, extra=True)
    registry = ResearchRegistry(tmp_path, clock=lambda: 3_200)
    try:
        changed = registry.register(request(second_dataset, candidate=package(order_usd=12)))
        conflict = registry.enqueue(changed["id"], idempotency_key="overlap")
    finally:
        registry.close()
    worker = ResearchJobWorker(tmp_path, evaluator=completed_report, clock=lambda: 3_300)
    try:
        worker.run_once()
        assert worker.registry.job(conflict["id"])["state"] == "failed"
        assert worker.registry.attempts(changed["id"])[0]["status"] == "failed"
    finally:
        worker.close()


class PaperEvaluator:
    def __init__(self, _package):
        self.seen = set()

    def restore(self, rows):
        self.seen.update(row["mint"] for row in rows)

    def restore_decisions(self, _rows):
        pass

    def evaluate(self, quotes, at, *, allow_entries):
        decisions = []
        for selected in quotes:
            if selected.mint not in self.seen:
                decisions.append(Decision(
                    "entry_recommended", selected.mint, at,
                    "fixture prospective decision", {"price_usd": selected.price_usd},
                ))
            self.seen.add(selected.mint)
        return decisions


def test_paper_decision_precedes_outcome_and_survives_restart_without_duplicates(tmp_path):
    clock = [100.0]
    recorder = ServiceEvidenceRecorder(tmp_path, clock=lambda: clock[0])
    selected = package()
    digest = parse_package(selected).digest
    paper = PaperResearchStore(tmp_path, clock=lambda: clock[0])
    try:
        run = paper.start(
            source_session_id=SESSION, strategy_package=selected,
            strategy_sha256=digest, simulated_capital_usd=500, start_at=0,
            idempotency_key="paper-one", confirmation=START_CONFIRMATION,
        )
        assert paper.start(
            source_session_id=SESSION, strategy_package=selected,
            strategy_sha256=digest, simulated_capital_usd=500, start_at=0,
            idempotency_key="paper-one", confirmation=START_CONFIRMATION,
        )["id"] == run["id"]
    finally:
        paper.close()
    first_scan = recorder.begin_scan(
        minimum_cap=1, maximum_cap=100_000, source="fixture", limit=10,
    )
    recorder.complete_scan(first_scan, [quote(90, price=1)], received_at=100)

    worker = PaperResearchWorker(
        tmp_path, recorder, evaluator_factory=PaperEvaluator, clock=lambda: clock[0]
    )
    try:
        result = worker.run_once()
        assert result["observations"] == 1 and result["outcomes"] == 0
        history = worker.store.history(run["id"])
        assert len(history["decisions"]) == 1
        assert history["decisions"][0]["decided_at"] == 100
    finally:
        worker.close()

    clock[0] = 150
    failed_scan = recorder.begin_scan(
        minimum_cap=1, maximum_cap=100_000, source="fixture", limit=10,
    )
    recorder.fail_scan(failed_scan, "fixture collection outage", received_at=150)
    clock[0] = 2_000
    second_scan = recorder.begin_scan(
        minimum_cap=1, maximum_cap=100_000, source="fixture", limit=10,
    )
    recorder.complete_scan(second_scan, [quote(1_950, price=0.8)], received_at=2_000)
    restarted = PaperResearchWorker(
        tmp_path, recorder, evaluator_factory=PaperEvaluator, clock=lambda: clock[0]
    )
    try:
        restarted.run_once()
        restarted.run_once()
        history = restarted.store.history(run["id"])
        assert len(history["observations"]) == 2
        assert len(history["decisions"]) == 1
        assert len(history["outcomes"]) == 1
        assert [gap["reason"] for gap in history["gaps"]] == ["fixture collection outage"]
        outcome = history["outcomes"][0]
        assert outcome["decided_at"] < outcome["scored_at"]
        assert outcome["modeled_return_fraction"] == pytest.approx(-0.2)
    finally:
        restarted.close()
        recorder.close()


def test_workspace_routes_use_exact_commands_without_trading_authority(tmp_path):
    library = StrategyLibrary(tmp_path)
    selected = parse_package(package())
    library.import_strategy(selected)
    session = library.create_instance(selected.digest, "Research API fixture")
    fresh = payload(library, f"/api/v1/research/sessions/{session}", {})
    assert fresh["source_strategy_sha256"] == selected.digest
    assert fresh["datasets"] == []
    assert fresh["paper_runs"] == []
    saved = save_dataset(tmp_path, session=session)
    try:
        workspace = payload(
            library, f"/api/v1/research/sessions/{session}", {},
        )
        assert workspace["schema"] == "pumpwizard.research-workspace/v1"
        assert workspace["datasets"][0]["id"] == saved["dataset"]["id"]
        assert workspace["datasets"][0]["counts"]["observations"] == 12
        assert workspace["paper_runs"] == []

        comparison = command(library, "/api/v1/research/jobs", {
            "schema": "pumpwizard.research-comparison-request/v1",
            "dataset_id": saved["dataset"]["id"],
            "settings": {
                "starting_capital": 500, "capital_unit": "USD", "purpose": "eligible",
                "variable_fee_bps_each_side": 100,
                "slippage_bps_each_side": 100,
                "fixed_cost_each_side": 0.10, "rent_each_entry": 0,
                "embargo_seconds": 10,
            },
            "idempotency_key": "browser-tab-one",
        })
        assert comparison["job"]["state"] == "queued"
        duplicate = command(library, "/api/v1/research/jobs", {
            "schema": "pumpwizard.research-comparison-request/v1",
            "dataset_id": saved["dataset"]["id"],
            "settings": {
                "starting_capital": 500, "capital_unit": "USD", "purpose": "eligible",
                "variable_fee_bps_each_side": 100,
                "slippage_bps_each_side": 100,
                "fixed_cost_each_side": 0.10, "rent_each_entry": 0,
                "embargo_seconds": 10,
            },
            "idempotency_key": "browser-tab-one",
        })
        assert duplicate["job"]["id"] == comparison["job"]["id"]

        run = command(library, "/api/v1/research/paper-runs", {
            "schema": "pumpwizard.paper-start-request/v1",
            "source_session_id": session, "strategy_sha256": selected.digest,
            "starting_capital_usd": 500, "confirmation": START_CONFIRMATION,
            "idempotency_key": "paper-browser-one",
        })["paper_run"]
        assert run["state"] == "running"
        assert run["simulated_capital_usd"] == 500
        assert run["strategy_sha256"] == selected.digest
        assert run.get("wallet") is None and run.get("signer") is None
        paused = command(
            library, f"/api/v1/research/paper-runs/{run['id']}/pause", {
                "schema": "pumpwizard.paper-command/v1", "run_id": run["id"],
                "command": "pause", "idempotency_key": "pause-one",
            },
        )["paper_run"]
        assert paused["state"] == "paused"
        with pytest.raises(ValueError, match="schema"):
            command(library, "/api/v1/research/datasets/freeze", {
                "schema": "pumpwizard.research-freeze-request/v1",
                "source_session_id": session, "idempotency_key": "freeze",
                "activate_trading": True,
            })
    finally:
        library.close()


def test_backup_restore_includes_research_and_paper_without_authority(tmp_path):
    source = tmp_path / "source"
    dataset = save_dataset(source)
    registry = ResearchRegistry(source, clock=lambda: 4_000)
    try:
        experiment = registry.register(request(dataset))
        registry.enqueue(experiment["id"], idempotency_key="backup-job")
    finally:
        registry.close()
    selected = package()
    paper = PaperResearchStore(source, clock=lambda: 4_000)
    try:
        run = paper.start(
            source_session_id=SESSION, strategy_package=selected,
            strategy_sha256=parse_package(selected).digest,
            simulated_capital_usd=500, start_at=4_000,
            idempotency_key="backup-paper", confirmation=START_CONFIRMATION,
        )
    finally:
        paper.close()

    backup = tmp_path / "backup"
    restored = tmp_path / "restored"
    create_backup(source, backup)
    restore_backup(backup, restored)
    data = ResearchDataStore(restored)
    registry = ResearchRegistry(restored)
    paper = PaperResearchStore(restored)
    try:
        assert data.dataset(dataset["dataset"]["id"])["content_sha256"] == \
            dataset["dataset"]["content_sha256"]
        assert registry.jobs()[0]["state"] == "queued"
        restored_run = paper.run(run["id"])
        assert restored_run["state"] == "running"
        assert restored_run["authority"] == {
            "wallet_allocation": False, "signer": False,
            "orders": False, "paid_provider": False,
        }
    finally:
        paper.close()
        registry.close()
        data.close()
