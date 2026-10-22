from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json

import pytest

from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.paper_research import (
    PaperResearchStore,
    PaperResearchWorker,
    START_CONFIRMATION,
)
from pumpwizard_runner.research_data import ResearchDataStore
from pumpwizard_runner.research_jobs import ResearchJobWorker, ResearchRegistry
from pumpwizard_runner.research_replay import evaluate_research_experiment
from pumpwizard_runner.strategies import canonical_json
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder
from pumpwizard_runner.service_backup import create_backup, restore_backup
from pumpwizard_runner.web_api import _research_report_artifact, payload as api_payload

from test_research_pipeline import (
    completed_report,
    dataset_export,
    package,
    quote,
    request,
    save_dataset,
)
from test_research_replay import _experiment, _export


def _registered_request(export, *, purpose: str) -> dict:
    fixture = _experiment(export, purpose=purpose)
    fields = {
        "dataset_id", "cost_model", "capital", "splits", "embargo_seconds",
        "objective", "evidence_policy", "purpose", "engine_version", "code_version",
    }
    return {
        **{key: fixture[key] for key in fields},
        "candidates": [row["package"] for row in fixture["candidates"]],
    }


def _save_export(root, export):
    with ResearchDataStore(root) as store:
        return store.save(export)


def _different_evidence_export(*, session: str):
    export = dataset_export(session=session)
    for row in export["records"]["observations"]:
        row["evidence_id"] = f"{session}:{row['evidence_id']}"
    export["dataset"]["evidence_identities"] = sorted(
        row["evidence_id"] for row in export["records"]["observations"]
    )
    return _rehash_export(export)


def _rehash_export(export):
    manifest = {
        key: value for key, value in export["dataset"].items()
        if key not in {"id", "content_sha256"}
    }
    digest = hashlib.sha256(canonical_json({
        "dataset": manifest, "records": export["records"],
    }).encode()).hexdigest()
    export["dataset"]["id"] = "research-" + digest[:32]
    export["dataset"]["content_sha256"] = digest
    return export


def test_exploratory_adapter_withholds_holdout_and_changed_eligible_can_claim(tmp_path):
    export = _export()
    _save_export(tmp_path, export)
    exploratory_request = _registered_request(export, purpose="exploratory")
    with ResearchRegistry(tmp_path) as registry:
        experiment = registry.register(exploratory_request)
        direct = evaluate_research_experiment(export, experiment)
        assert direct["holdout_state"] == "not_evaluated"
        assert direct["detailed_comparison"]["holdout_results"] == []
        assert direct["detailed_comparison"]["holdout_policy"] == {
            "selection_split": "validation",
            "holdout_used_for_selection": False,
            "evaluated_candidate_ids": [],
            "registry_must_prevent_reuse_as_unseen": True,
            "evaluated": False,
        }
        assert direct["period"]["last_at"] == experiment["splits"]["validation_end_at"]
        assert direct["detailed_comparison"]["input_lineage"]["holdout_evidence_ids"] == []
        assert all(
            row["holdout_evaluated"] is False
            and "holdout_signals" not in row
            and "holdout_blocked_reasons" not in row
            and "holdout_eligible_mints" not in row
            for row in direct["candidate_diagnostics"].values()
        )
        job = registry.enqueue(experiment["id"], idempotency_key="explore")
    worker = ResearchJobWorker(tmp_path)
    try:
        assert worker.run_once()["state"] == "completed"
        queued = worker.registry.result(job["id"])
        assert queued["holdout_state"] == "not_evaluated"
        assert queued["detailed_comparison"]["holdout_results"] == []
        assert worker.registry.holdout_status(experiment["id"])["state"] == "not_evaluated"

        eligible_request = copy.deepcopy(exploratory_request)
        eligible_request["purpose"] = "eligible"
        eligible_request["objective"] = "changed objective after validation-only exploration"
        eligible = worker.registry.register(eligible_request)
        eligible_job = worker.registry.enqueue(
            eligible["id"], idempotency_key="eligible-after-withheld"
        )
        claimed = worker.registry.claim_next()
        assert claimed["id"] == eligible_job["id"]
        assert claimed["state"] == "running"
        assert worker.registry.holdout_status(eligible["id"])["state"] == "exposed"
    finally:
        worker.close()


def test_exploratory_job_rejects_an_evaluator_that_returns_holdout(tmp_path):
    dataset = save_dataset(tmp_path)
    with ResearchRegistry(tmp_path) as registry:
        experiment = registry.register(request(dataset, purpose="exploratory"))
        job = registry.enqueue(experiment["id"], idempotency_key="bad-explorer")

    def leaking_evaluator(export, registered, progress, cancelled):
        report = completed_report(export, registered, progress, cancelled)
        report["holdout_state"] = "exposed"
        report["detailed_comparison"] = {"holdout_results": [{"candidate_id": "leak"}]}
        return report

    worker = ResearchJobWorker(tmp_path, evaluator=leaking_evaluator)
    try:
        failed = worker.run_once()
        assert failed["id"] == job["id"]
        assert failed["state"] == "failed"
        assert failed["error_code"] == "unexpected_holdout_exposure"
        assert failed["failure"]["phase"] == "replay"
        assert worker.registry.holdout_status(experiment["id"])["state"] == "not_evaluated"
    finally:
        worker.close()


def test_exploratory_adapter_does_not_inspect_holdout_payload_or_quality(tmp_path):
    export = copy.deepcopy(_export())
    fixture = _experiment(export, purpose="exploratory")
    boundary = fixture["splits"]["validation_end_at"]
    changed = 0
    for row in export["records"]["observations"]:
        if row["times"]["event_at"] > boundary:
            row["payload"] = {"private_holdout_value": "must-not-be-inspected"}
            row["quality_flags"] = ["holdout-only-quality-marker"]
            changed += 1
    assert changed > 0
    _rehash_export(export)
    _save_export(tmp_path, export)
    with ResearchRegistry(tmp_path) as registry:
        experiment = registry.register(_registered_request(export, purpose="exploratory"))
        report = evaluate_research_experiment(export, experiment)
    assert report["holdout_state"] == "not_evaluated"
    assert report["detailed_comparison"]["holdout_results"] == []
    assert "private_holdout_value" not in canonical_json(report)
    assert "holdout-only-quality-marker" not in canonical_json(report)


def test_historical_exploratory_report_is_preserved_and_fail_closed(tmp_path):
    dataset = save_dataset(tmp_path)
    with ResearchRegistry(tmp_path, clock=lambda: 1_000) as registry:
        experiment = registry.register(request(dataset, purpose="exploratory"))
        job = registry.enqueue(experiment["id"], idempotency_key="legacy")
        claimed = registry.claim_next()
        report = completed_report(dataset, experiment, lambda _value: None, lambda: False)
        report.update({
            "id": "report-legacy-exploratory",
            "holdout_state": "exposed",
            "detailed_comparison": {"holdout_results": [{"candidate_id": "current"}]},
        })
        payload = canonical_json(report)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        registry.db.execute(
            """UPDATE research_attempts SET status='completed',finished_at=1001,
                      result_sha256=?,result_json=? WHERE id=?""",
            (digest, payload, claimed["attempt_id"]),
        )
        registry.db.execute(
            """UPDATE research_jobs SET state='completed',progress=1,finished_at=1001,
                      updated_at=1001 WHERE id=?""",
            (job["id"],),
        )
        registry.db.execute("DELETE FROM research_integrity_meta")
        registry.db.execute("DELETE FROM research_holdout_exposure_audit")
        registry.db.commit()

    with ResearchRegistry(tmp_path, clock=lambda: 2_000) as migrated:
        stored = migrated.result(job["id"])
        assert stored == report
        viewed = migrated.result_view(job["id"])
        assert viewed["holdout_state"] == "exposed"
        assert viewed["integrity"]["inferred"] is True
        summary = migrated.integrity_summary()
        assert summary["counts"]["inferred_historical_exposures"] == 1
        assert summary["reasons"] == {"legacy_report_contains_holdout_results": 1}
        before = migrated.db.total_changes
        assert migrated.integrity_summary() == summary
        assert migrated.db.total_changes == before

        changed_request = request(dataset, purpose="eligible")
        changed_request["objective"] = "new objective must not regain historical holdout"
        changed = migrated.register(changed_request)
        blocked = migrated.enqueue(changed["id"], idempotency_key="after-migration")
        failed = migrated.claim_next()
        assert failed["id"] == blocked["id"]
        assert failed["state"] == "failed"
        assert failed["error_code"] == "holdout_conflict"

    library = StrategyLibrary(tmp_path)
    try:
        response = api_payload(
            library, f"/api/v1/research/jobs/{job['id']}/result", {}
        )
        assert response["result"]["holdout_state"] == "exposed"
        assert response["result"]["integrity"]["inferred"] is True
        assert response["result"]["eligible"] is False
    finally:
        library.close()
    content_type, filename, body = _research_report_artifact(
        tmp_path, "report-legacy-exploratory", "json"
    )
    exported = json.loads(body)
    assert content_type.startswith("application/json")
    assert filename == "report-legacy-exploratory.json"
    assert exported["holdout_state"] == "exposed"
    assert exported["integrity"]["reason"] == "legacy_report_contains_holdout_results"


def test_exact_oldest_queue_failure_survives_more_than_public_history_limit(tmp_path):
    dataset = save_dataset(tmp_path)
    now = [1_000.0]
    with ResearchRegistry(tmp_path, clock=lambda: now[0]) as registry:
        for index in range(105):
            historical_request = request(dataset, purpose="exploratory")
            historical_request["objective"] = f"cancelled history {index}"
            historical = registry.register(historical_request)
            old = registry.enqueue(historical["id"], idempotency_key=f"history-{index}")
            registry.cancel(old["id"])
            now[0] += 1

        exposed = registry.register(request(dataset))
        first = registry.enqueue(exposed["id"], idempotency_key="initial-exposure")
        registry.claim_next()
        registry.fail(first["id"], "fixture failure after reservation")
        now[0] += 1

        conflicting_request = request(dataset, candidate=package(order_usd=12))
        conflicting = registry.register(conflicting_request)
        blocked = registry.enqueue(conflicting["id"], idempotency_key="blocked-oldest")
        now[0] += 1
        valid = registry.register(request(dataset, purpose="exploratory"))
        valid_job = registry.enqueue(valid["id"], idempotency_key="valid-newest")

    worker = ResearchJobWorker(tmp_path, evaluator=completed_report, clock=lambda: now[0])
    try:
        failed = worker.run_once()
        assert failed["id"] == blocked["id"]
        assert failed["state"] == "failed"
        assert failed["failure"] == {
            "schema": "pumpwizard.research-failure/v1",
            "code": "holdout_conflict",
            "message": "Overlapping holdout evidence was already exposed by another experiment",
            "phase": "holdout_reservation",
            "retryable": False,
        }
        assert worker.registry.job(valid_job["id"])["state"] == "queued"
        completed = worker.run_once()
        assert completed["id"] == valid_job["id"]
        assert completed["state"] == "completed"
    finally:
        worker.close()


def test_two_claimers_cannot_claim_the_same_or_a_second_concurrent_job(tmp_path):
    dataset = save_dataset(tmp_path)
    with ResearchRegistry(tmp_path) as setup:
        first = setup.register(request(dataset))
        first_job = setup.enqueue(first["id"], idempotency_key="first")
        second_request = request(dataset, purpose="exploratory")
        second_request["objective"] = "second queued job"
        second = setup.register(second_request)
        setup.enqueue(second["id"], idempotency_key="second")

    one = ResearchRegistry(tmp_path)
    two = ResearchRegistry(tmp_path)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda registry: registry.claim_next(), (one, two)))
        claimed = [row for row in results if row is not None]
        assert len(claimed) == 1
        assert claimed[0]["id"] == first_job["id"]
        assert claimed[0]["state"] == "running"
    finally:
        one.close()
        two.close()


def test_integrity_schema_install_is_safe_under_competing_openers(tmp_path):
    def open_registry(_index):
        with ResearchRegistry(tmp_path) as registry:
            return registry.integrity_summary()["counts"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        summaries = list(pool.map(open_registry, range(8)))
    assert summaries == [{
        "experiments": 0,
        "exposure_records": 0,
        "inferred_historical_exposures": 0,
        "reports_with_recorded_exposure": 0,
    }] * 8


def test_overlap_policy_covers_all_search_inputs_and_global_evidence_identity(tmp_path):
    dataset = save_dataset(tmp_path)
    with ResearchRegistry(tmp_path) as registry:
        baseline = registry.register(request(dataset))
        initial = registry.enqueue(baseline["id"], idempotency_key="baseline")
        registry.claim_next()
        registry.fail(initial["id"], "preserve reservation")

        variants = []
        candidate_change = request(dataset, candidate=package(order_usd=12))
        variants.append(candidate_change)
        cost_change = request(dataset)
        cost_change["cost_model"]["fee_fraction"] = 0.02
        variants.append(cost_change)
        objective_change = request(dataset)
        objective_change["objective"] = "minimize drawdown"
        variants.append(objective_change)
        evidence_change = request(dataset)
        evidence_change["evidence_policy"]["horizon_seconds"] = 600
        variants.append(evidence_change)
        for index, changed_request in enumerate(variants):
            changed = registry.register(changed_request)
            queued = registry.enqueue(changed["id"], idempotency_key=f"variant-{index}")
            failed = registry.claim_next()
            assert failed["id"] == queued["id"]
            assert failed["error_code"] == "holdout_conflict"

        same_ids_other_session = save_dataset(tmp_path, session="other-session")
        cross_session = registry.register(request(same_ids_other_session))
        cross_job = registry.enqueue(cross_session["id"], idempotency_key="same-global-evidence")
        assert registry.claim_next()["id"] == cross_job["id"]
        assert registry.job(cross_job["id"])["error_code"] == "holdout_conflict"

        distinct = _save_export(
            tmp_path, _different_evidence_export(session="independent-session")
        )
        independent = registry.register(request(distinct))
        independent_job = registry.enqueue(
            independent["id"], idempotency_key="different-global-evidence"
        )
        claimed = registry.claim_next()
        assert claimed["id"] == independent_job["id"]
        assert claimed["state"] == "running"


def test_cancel_retry_failure_restart_and_completed_result_are_durable(tmp_path):
    dataset = save_dataset(tmp_path)
    now = [5_000.0]
    with ResearchRegistry(tmp_path, clock=lambda: now[0]) as registry:
        experiment = registry.register(request(dataset, purpose="exploratory"))
        before = registry.enqueue(experiment["id"], idempotency_key="cancel-before")
        assert registry.cancel(before["id"])["state"] == "cancelled"

        after = registry.enqueue(experiment["id"], idempotency_key="cancel-after")
        claimed = registry.claim_next()
        assert claimed["id"] == after["id"]
        assert registry.cancel(after["id"])["state"] == "cancel_requested"
        report = completed_report(dataset, experiment, lambda _value: None, lambda: False)
        assert registry.complete(after["id"], report)["state"] == "cancelled"

        interrupted = registry.enqueue(experiment["id"], idempotency_key="restart")
        assert registry.claim_next()["id"] == interrupted["id"]

    with ResearchRegistry(tmp_path, clock=lambda: now[0]) as restarted:
        assert restarted.recover_interrupted() == 1
        assert restarted.job(interrupted["id"])["state"] == "queued"
        assert restarted.attempts(experiment["id"])[-1]["restart_count"] == 1
        restarted.fail(restarted.claim_next()["id"], RuntimeError("transient evaluator fault"))
        retry = restarted.enqueue(experiment["id"], idempotency_key="retry-after-fault")

    worker = ResearchJobWorker(tmp_path, evaluator=completed_report, clock=lambda: now[0])
    try:
        completed = worker.run_once()
        assert completed["id"] == retry["id"]
        assert completed["state"] == "completed"
        stored = worker.registry.result(retry["id"])
        assert worker.registry.complete(retry["id"], report)["state"] == "completed"
        changed = copy.deepcopy(report)
        changed["blockers"] = ["changed-after-completion"]
        with pytest.raises(ValueError, match="immutable"):
            worker.registry.complete(retry["id"], changed)
        assert worker.registry.result(retry["id"]) == stored
        assert [row["status"] for row in worker.registry.attempts(experiment["id"])] == [
            "cancelled", "cancelled", "failed", "completed",
        ]
    finally:
        worker.close()


def test_bad_paper_run_is_faulted_without_starving_healthy_run_and_retries_are_bounded(
    tmp_path,
):
    now = [100.0]
    bad_package = package(order_usd=10)
    good_package = package(order_usd=12)
    with PaperResearchStore(tmp_path, clock=lambda: now[0]) as paper:
        bad = paper.start(
            source_session_id="bad-session",
            strategy_package=bad_package,
            strategy_sha256=parse_package(bad_package).digest,
            simulated_capital_usd=500,
            start_at=0,
            idempotency_key="bad-paper-run",
            confirmation=START_CONFIRMATION,
        )
        good = paper.start(
            source_session_id="good-session",
            strategy_package=good_package,
            strategy_sha256=parse_package(good_package).digest,
            simulated_capital_usd=500,
            start_at=0,
            idempotency_key="good-paper-run",
            confirmation=START_CONFIRMATION,
        )
    recorder = ServiceEvidenceRecorder(tmp_path, clock=lambda: now[0])
    scan = recorder.begin_scan(minimum_cap=1, maximum_cap=100_000, source="fixture", limit=10)
    recorder.complete_scan(scan, [quote(90)], received_at=100)

    class HealthyEvaluator:
        def restore(self, _rows):
            pass

        def restore_decisions(self, _rows):
            pass

        def snapshot(self):
            return {"seen": []}

        def evaluate(self, _quotes, _at, *, allow_entries):
            assert allow_entries is True
            return []

    def evaluator_factory(selected):
        if selected["configuration"]["order_usd"] == 10:
            raise RuntimeError("fixture malformed strategy state")
        return HealthyEvaluator()

    worker = PaperResearchWorker(
        tmp_path, recorder, evaluator_factory=evaluator_factory, clock=lambda: now[0]
    )
    try:
        totals = worker.run_once()
        assert totals["runs"] == 2
        assert totals["faults"] == 1
        bad_state = worker.store.run(bad["id"])
        assert bad_state["state"] == "interrupted"
        assert bad_state["fault"]["code"] == "paper-cycle-fault"
        assert bad_state["fault"]["retryable"] is True
        assert worker.store.run(good["id"])["counts"]["observations"] == 1

        for expected_count in (2, 3):
            now[0] += 1
            worker.store.command(bad["id"], action="resume")
            worker.run_once()
            faulted = worker.store.run(bad["id"])
            assert faulted["retry"]["count"] == expected_count
        assert faulted["fault"]["retryable"] is False
        with pytest.raises(ValueError, match="retry limit"):
            worker.store.command(bad["id"], action="resume")
        assert len(worker.store.history(bad["id"])["faults"]) == 3
        assert worker.store.run(good["id"])["state"] == "running"
    finally:
        worker.close()
        recorder.close()


def test_backup_restore_preserves_integrity_migration_and_paper_faults(tmp_path):
    source = tmp_path / "source"
    dataset = save_dataset(source)
    with ResearchRegistry(source, clock=lambda: 4_000) as registry:
        experiment = registry.register(request(dataset))
        job = registry.enqueue(experiment["id"], idempotency_key="backup-exposure")
        registry.claim_next()
        registry.fail(job["id"], "fixture failure after exposure")
        expected_integrity = registry.integrity_summary()

    selected = package()
    with PaperResearchStore(source, clock=lambda: 4_000) as paper:
        run = paper.start(
            source_session_id="backup-session",
            strategy_package=selected,
            strategy_sha256=parse_package(selected).digest,
            simulated_capital_usd=500,
            start_at=4_000,
            idempotency_key="backup-paper",
            confirmation=START_CONFIRMATION,
        )
        paper.record_fault(
            run["id"], code="fixture-quality-fault",
            message="fixture evidence needs repair", retryable=True,
        )

    backup = tmp_path / "backup"
    restored = tmp_path / "restored"
    manifest = create_backup(source, backup)
    result = restore_backup(backup, restored)
    assert {entry["path"] for entry in manifest["entries"]} >= {
        "research.sqlite", "paper-research.sqlite",
    }
    assert result["restored"] is True

    with ResearchRegistry(restored) as registry:
        assert registry.integrity_summary() == expected_integrity
        assert registry.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    with PaperResearchStore(restored) as paper:
        restored_run = paper.run(run["id"])
        assert restored_run["state"] == "interrupted"
        assert restored_run["fault"]["code"] == "fixture-quality-fault"
        assert paper.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
