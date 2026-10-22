"""Registered experiments and durable, bounded research jobs."""
from __future__ import annotations

import hashlib
import importlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable

from .research_data import DATABASE_NAME, ResearchDataStore
from .strategies import canonical_json, parse_package


EXPERIMENT_SCHEMA = "pumpwizard.research-experiment/v1"
ATTEMPT_SCHEMA = "pumpwizard.research-attempt/v1"
JOB_SCHEMA = "pumpwizard.research-job/v1"
REPORT_SCHEMA = "pumpwizard.research-replay-report/v1"
INTEGRITY_SCHEMA = "pumpwizard.research-integrity/v1"
MAX_CANDIDATES = 64
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_ACTIVE_JOBS = 1
ACTIVE_STATES = ("queued", "running", "cancel_requested")
FINAL_STATES = ("failed", "cancelled", "completed")


class ResearchJobError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_research_job"):
        super().__init__(message)
        self.code = code


class ResearchRegistry:
    """Append-only experiment, attempt, holdout, and job registry."""

    def __init__(self, data_dir: str | Path, *, clock=time.time):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / DATABASE_NAME
        self.clock = clock
        self._lock = threading.RLock()
        # Ensure the datasets and shared schema exist first.
        bootstrap = ResearchDataStore(self.root, clock=clock)
        bootstrap.close()
        self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS research_experiments (
                    id TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL UNIQUE,
                    dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    source_session_id TEXT NOT NULL,
                    purpose TEXT NOT NULL CHECK(purpose IN ('exploratory','eligible')),
                    holdout_first_at REAL NOT NULL,
                    holdout_last_at REAL NOT NULL,
                    search_sha256 TEXT NOT NULL,
                    registered_at REAL NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS research_experiment_dataset
                    ON research_experiments(dataset_id,registered_at,id);
                CREATE TABLE IF NOT EXISTS research_attempts (
                    id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
                    ordinal INTEGER NOT NULL CHECK(ordinal > 0),
                    status TEXT NOT NULL CHECK(status IN
                        ('queued','running','failed','cancelled','completed')),
                    queued_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    restart_count INTEGER NOT NULL DEFAULT 0,
                    result_sha256 TEXT,
                    result_json TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    UNIQUE(experiment_id,ordinal)
                );
                CREATE INDEX IF NOT EXISTS research_attempt_history
                    ON research_attempts(experiment_id,ordinal);
                CREATE TABLE IF NOT EXISTS research_jobs (
                    id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
                    attempt_id TEXT NOT NULL UNIQUE REFERENCES research_attempts(id),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN
                        ('queued','running','cancel_requested','failed','cancelled','completed')),
                    progress REAL NOT NULL DEFAULT 0 CHECK(progress >= 0 AND progress <= 1),
                    cancellation_requested_at REAL,
                    worker_token TEXT,
                    queued_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    updated_at REAL NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    UNIQUE(experiment_id,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS research_job_queue
                    ON research_jobs(state,queued_at,id);
                CREATE TABLE IF NOT EXISTS research_holdout_ranges (
                    experiment_id TEXT PRIMARY KEY REFERENCES research_experiments(id),
                    source_session_id TEXT NOT NULL,
                    first_at REAL NOT NULL,
                    last_at REAL NOT NULL,
                    search_sha256 TEXT NOT NULL,
                    reserved_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS research_holdout_range_overlap
                    ON research_holdout_ranges(source_session_id,first_at,last_at);
                CREATE TABLE IF NOT EXISTS research_holdout_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    search_sha256 TEXT NOT NULL,
                    first_experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
                    reserved_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_holdout_exposure_audit (
                    experiment_id TEXT PRIMARY KEY REFERENCES research_experiments(id),
                    attempt_id TEXT,
                    exposure_source TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    inferred INTEGER NOT NULL CHECK(inferred IN (0,1)),
                    detected_at REAL NOT NULL,
                    report_id TEXT,
                    engine_version TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS research_holdout_exposure_reason
                    ON research_holdout_exposure_audit(inferred,reason,detected_at);
                CREATE TABLE IF NOT EXISTS research_integrity_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            self._add_column("research_attempts", "error_code", "TEXT NOT NULL DEFAULT ''")
            self._add_column("research_attempts", "error_json", "TEXT NOT NULL DEFAULT ''")
            self._add_column("research_jobs", "error_code", "TEXT NOT NULL DEFAULT ''")
            self._add_column("research_jobs", "error_json", "TEXT NOT NULL DEFAULT ''")
            installed = self.db.execute(
                "SELECT value FROM research_integrity_meta WHERE key='schema_version'"
            ).fetchone()
            if installed is None:
                now = float(self.clock())
                self._migrate_historical_exposure_locked(now)
                self.db.executemany(
                    "INSERT OR IGNORE INTO research_integrity_meta(key,value) VALUES(?,?)",
                    (("schema_version", "1"), ("installed_at", str(now))),
                )

    def _add_column(self, table: str, name: str, definition: str) -> None:
        columns = {str(row["name"]) for row in self.db.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            try:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError:
                # A second process may have completed the same additive
                # migration while this connection waited on SQLite's writer.
                current = {
                    str(row["name"])
                    for row in self.db.execute(f"PRAGMA table_info({table})")
                }
                if name not in current:
                    raise

    def _migrate_historical_exposure_locked(self, now: float) -> None:
        """Conservatively preserve holdout lineage from pre-integrity attempts.

        Old failed or interrupted evaluators may have read their holdout before they
        stopped.  A missing result therefore cannot be treated as proof that the
        evidence remained unseen.
        """
        rows = self.db.execute(
            """SELECT experiment.*,attempt.id AS attempt_id,attempt.status,
                      attempt.result_json
               FROM research_experiments AS experiment
               JOIN research_attempts AS attempt
                 ON attempt.experiment_id=experiment.id
               WHERE attempt.status IN ('running','failed','cancelled','completed')
               ORDER BY experiment.registered_at,attempt.ordinal"""
        ).fetchall()
        seen: set[str] = set()
        for row in rows:
            experiment_id = str(row["id"])
            if experiment_id in seen:
                continue
            seen.add(experiment_id)
            result = _loaded_object(row["result_json"])
            existing = self.db.execute(
                "SELECT 1 FROM research_holdout_ranges WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()
            if existing is not None:
                self._record_exposure_locked(
                    row, now, attempt_id=str(row["attempt_id"]),
                    source="pre_integrity_reservation",
                    reason="existing_holdout_reservation", inferred=False,
                    report=result,
                )
                continue
            if (row["status"] == "completed" and result is not None
                    and _report_explicitly_withheld(result)):
                continue
            if row["purpose"] != "exploratory":
                # An eligible pre-integrity attempt should have had a reservation.
                # Its absence is ambiguous, so fail closed just as for an old
                # exploratory evaluator.
                reason = "missing_eligible_reservation_assumed_exposed"
            elif result is not None and _report_has_holdout(result):
                reason = "legacy_report_contains_holdout_results"
            elif row["status"] == "completed":
                reason = "legacy_completed_report_assumed_exposed"
            else:
                reason = "legacy_attempt_may_have_read_holdout_before_termination"
            payload = json.loads(row["payload_json"])
            self._insert_holdout_lineage_locked(row, payload, now)
            self._record_exposure_locked(
                row, now, attempt_id=str(row["attempt_id"]),
                source="historical_migration", reason=reason, inferred=True,
                report=result,
            )

    def _record_exposure_locked(
        self, experiment: sqlite3.Row, now: float, *, attempt_id: str | None,
        source: str, reason: str, inferred: bool,
        report: dict[str, Any] | None = None,
    ) -> None:
        registered = _loaded_object(experiment["payload_json"]) or {}
        self.db.execute(
            """INSERT OR IGNORE INTO research_holdout_exposure_audit(
                   experiment_id,attempt_id,exposure_source,reason,inferred,
                   detected_at,report_id,engine_version
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                experiment["id"], attempt_id, source, reason, int(inferred), now,
                report.get("id") if report else None,
                str((report or {}).get("engine_version")
                    or registered.get("engine_version") or "unknown"),
            ),
        )

    def _insert_holdout_lineage_locked(
        self, experiment: sqlite3.Row, payload: dict[str, Any], now: float,
    ) -> None:
        self.db.execute(
            """INSERT OR IGNORE INTO research_holdout_ranges(
                   experiment_id,source_session_id,first_at,last_at,search_sha256,reserved_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                experiment["id"], experiment["source_session_id"],
                experiment["holdout_first_at"], experiment["holdout_last_at"],
                experiment["search_sha256"], now,
            ),
        )
        evidence_ids = payload.get("lineage", {}).get("holdout_evidence_ids", [])
        self.db.executemany(
            """INSERT OR IGNORE INTO research_holdout_evidence(
                   evidence_id,search_sha256,first_experiment_id,reserved_at
               ) VALUES(?,?,?,?)""",
            [(ident, experiment["search_sha256"], experiment["id"], now)
             for ident in evidence_ids],
        )

    def register(self, request: object) -> dict[str, Any]:
        normalized = self._normalize_request(request)
        request_sha = _sha256(normalized)
        experiment_id = "experiment-" + request_sha[:32]
        search_core = {key: value for key, value in normalized.items()
                       if key not in {"dataset_id", "lineage"}}
        search_sha = _sha256(search_core)
        now = float(self.clock())
        experiment = {
            "schema": EXPERIMENT_SCHEMA,
            "id": experiment_id,
            "request_sha256": request_sha,
            "search_sha256": search_sha,
            "registered_at": now,
            **normalized,
        }
        payload = canonical_json(experiment)
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT payload_json FROM research_experiments WHERE request_sha256=?",
                (request_sha,),
            ).fetchone()
            if existing is not None:
                return json.loads(existing["payload_json"])
            self.db.execute(
                """INSERT OR IGNORE INTO research_experiments(
                       id,request_sha256,dataset_id,source_session_id,purpose,
                       holdout_first_at,holdout_last_at,search_sha256,registered_at,payload_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    experiment_id, request_sha, normalized["dataset_id"],
                    normalized["lineage"]["source_session_id"], normalized["purpose"],
                    normalized["splits"]["validation_end_at"],
                    normalized["splits"]["holdout_end_at"], search_sha, now, payload,
                ),
            )
            row = self.db.execute(
                "SELECT payload_json FROM research_experiments WHERE id=?", (experiment_id,)
            ).fetchone()
            if row is None or row["payload_json"] != payload:
                raise ResearchJobError("Experiment identity collision", code="identity_conflict")
        return json.loads(payload)

    def _normalize_request(self, request: object) -> dict[str, Any]:
        required = {
            "dataset_id", "candidates", "cost_model", "capital", "splits",
            "embargo_seconds", "objective", "evidence_policy", "purpose",
            "engine_version", "code_version",
        }
        if not isinstance(request, dict) or set(request) != required:
            raise ResearchJobError(
                "Experiment registration requires the exact Batch 3 contract fields"
            )
        data = ResearchDataStore(self.root, clock=self.clock)
        try:
            dataset = data.dataset(_text(request["dataset_id"], "dataset_id"))
            export = data.export(dataset["id"])
        finally:
            data.close()
        purpose = request["purpose"]
        if purpose not in {"exploratory", "eligible"}:
            raise ResearchJobError("Experiment purpose must be exploratory or eligible")
        if purpose == "eligible" and not dataset["eligibility"]["eligible"]:
            raise ResearchJobError(
                "Dataset is not eligible for a holdout comparison: "
                + ", ".join(dataset["eligibility"]["reasons"]),
                code="ineligible_dataset",
            )
        candidates = request["candidates"]
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_CANDIDATES:
            raise ResearchJobError(f"Experiment requires 1–{MAX_CANDIDATES} candidates")
        normalized_candidates = []
        seen = set()
        for index, package in enumerate(candidates):
            if not isinstance(package, dict):
                raise ResearchJobError("Each experiment candidate must be a strategy package")
            parsed = parse_package(json.loads(canonical_json(package)))
            if parsed.digest in seen:
                raise ResearchJobError("Experiment candidates must have unique exact digests")
            seen.add(parsed.digest)
            normalized_candidates.append({
                "id": f"candidate-{index + 1}-{parsed.digest[:12]}",
                "package_sha256": parsed.digest,
                "package": json.loads(canonical_json(package)),
            })
        capital = request["capital"]
        if not isinstance(capital, dict) or set(capital) != {"amount", "unit"}:
            raise ResearchJobError("Experiment capital requires amount and unit")
        amount = _finite(capital["amount"], "capital amount")
        if not 0 < amount <= 1_000_000_000 or capital["unit"] != "USD":
            raise ResearchJobError("Experiment capital must be positive bounded USD")
        splits = request["splits"]
        if not isinstance(splits, dict) or set(splits) != {
            "train_end_at", "validation_end_at", "holdout_end_at",
        }:
            raise ResearchJobError("Experiment splits require train, validation and holdout ends")
        split_values = {key: _finite(value, key) for key, value in splits.items()}
        first = float(dataset["ranges"]["event"]["first"])
        last = float(dataset["ranges"]["event"]["last"])
        if not (first < split_values["train_end_at"]
                < split_values["validation_end_at"]
                < split_values["holdout_end_at"] <= last):
            raise ResearchJobError("Experiment splits must be chronological inside the dataset")
        embargo = _finite(request["embargo_seconds"], "embargo_seconds")
        if embargo < 0 or embargo > 30 * 24 * 60 * 60:
            raise ResearchJobError("Experiment embargo is outside the supported bound")
        if split_values["train_end_at"] + embargo >= split_values["validation_end_at"]:
            raise ResearchJobError("Experiment embargo consumes the validation interval")
        if split_values["validation_end_at"] + embargo >= split_values["holdout_end_at"]:
            raise ResearchJobError("Experiment embargo consumes the holdout interval")
        cost_model = _json_object(request["cost_model"], "cost_model")
        evidence_policy = _json_object(request["evidence_policy"], "evidence_policy")
        objective = request["objective"]
        if not isinstance(objective, (str, dict)):
            raise ResearchJobError("Experiment objective must be text or an object")
        objective = (_text(objective, "objective") if isinstance(objective, str)
                     else _json_object(objective, "objective"))
        engine_version = _text(request["engine_version"], "engine_version")
        code_version = _text(request["code_version"], "code_version")
        holdout_ids = sorted(
            row["evidence_id"] for row in export["records"]["observations"]
            if split_values["validation_end_at"] < row["times"]["event_at"]
            <= split_values["holdout_end_at"]
        )
        if purpose == "eligible" and not holdout_ids:
            raise ResearchJobError("Eligible experiment holdout contains no evidence")
        return {
            "dataset_id": dataset["id"],
            "lineage": {
                "source_session_id": dataset["source"]["session_id"],
                "dataset_content_sha256": dataset["content_sha256"],
                "holdout_evidence_ids": holdout_ids,
            },
            "candidates": normalized_candidates,
            "cost_model": cost_model,
            "capital": {"amount": amount, "unit": "USD"},
            "splits": split_values,
            "embargo_seconds": embargo,
            "objective": objective,
            "evidence_policy": evidence_policy,
            "purpose": purpose,
            "engine_version": engine_version,
            "code_version": code_version,
        }

    def experiment(self, experiment_id: str) -> dict[str, Any]:
        row = self._row("research_experiments", experiment_id, "experiment")
        return json.loads(row["payload_json"])

    def experiments(self, *, dataset_id: str | None = None,
                    limit: int = 100) -> list[dict[str, Any]]:
        _limit(limit)
        parameters: list[object] = []
        clause = ""
        if dataset_id is not None:
            clause = " WHERE dataset_id=?"
            parameters.append(_text(dataset_id, "dataset_id"))
        parameters.append(limit)
        with self._lock:
            rows = self.db.execute(
                "SELECT payload_json FROM research_experiments" + clause
                + " ORDER BY registered_at DESC,id LIMIT ?",
                parameters,
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def enqueue(self, experiment_id: str, *, idempotency_key: str) -> dict[str, Any]:
        experiment_id = _text(experiment_id, "experiment_id")
        key = _text(idempotency_key, "idempotency_key")
        if len(key) > 200:
            raise ResearchJobError("Research idempotency key is too long")
        now = float(self.clock())
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute(
                "SELECT 1 FROM research_experiments WHERE id=?", (experiment_id,)
            ).fetchone() is None:
                raise ResearchJobError("Unknown research experiment", code="not_found")
            existing = self.db.execute(
                "SELECT * FROM research_jobs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if existing["experiment_id"] != experiment_id:
                    raise ResearchJobError(
                        "Idempotency key belongs to another experiment", code="identity_conflict"
                    )
                return {**_job_row(existing), "reused": True}
            completed = self.db.execute(
                """SELECT job.* FROM research_jobs AS job
                   JOIN research_attempts AS attempt ON attempt.id=job.attempt_id
                   WHERE job.experiment_id=? AND attempt.status='completed'
                   ORDER BY attempt.ordinal LIMIT 1""",
                (experiment_id,),
            ).fetchone()
            if completed is not None:
                return {**_job_row(completed), "reused": True}
            active = self.db.execute(
                """SELECT * FROM research_jobs WHERE experiment_id=?
                   AND state IN ('queued','running','cancel_requested')
                   ORDER BY queued_at,id LIMIT 1""",
                (experiment_id,),
            ).fetchone()
            if active is not None:
                return {**_job_row(active), "reused": True}
            ordinal = int(self.db.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 FROM research_attempts WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()[0])
            attempt_id = uuid.uuid4().hex
            job_id = uuid.uuid4().hex
            self.db.execute(
                """INSERT INTO research_attempts(
                       id,experiment_id,ordinal,status,queued_at
                   ) VALUES(?,?,?,'queued',?)""",
                (attempt_id, experiment_id, ordinal, now),
            )
            self.db.execute(
                """INSERT INTO research_jobs(
                       id,experiment_id,attempt_id,idempotency_key,state,
                       queued_at,updated_at
                   ) VALUES(?,?,?,?,'queued',?,?)""",
                (job_id, experiment_id, attempt_id, key, now, now),
            )
            row = self.db.execute("SELECT * FROM research_jobs WHERE id=?", (job_id,)).fetchone()
        return {**_job_row(row), "reused": False}

    def cancel(self, job_id: str) -> dict[str, Any]:
        ident = _text(job_id, "job_id")
        now = float(self.clock())
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM research_jobs WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise ResearchJobError("Unknown research job", code="not_found")
            if row["state"] == "queued":
                self.db.execute(
                    """UPDATE research_jobs SET state='cancelled',progress=0,
                       cancellation_requested_at=?,finished_at=?,updated_at=? WHERE id=?""",
                    (now, now, now, ident),
                )
                self.db.execute(
                    """UPDATE research_attempts SET status='cancelled',finished_at=?,
                       error='Cancelled before replay started' WHERE id=?""",
                    (now, row["attempt_id"]),
                )
            elif row["state"] == "running":
                self.db.execute(
                    """UPDATE research_jobs SET state='cancel_requested',
                       cancellation_requested_at=?,updated_at=? WHERE id=?""",
                    (now, now, ident),
                )
            row = self.db.execute("SELECT * FROM research_jobs WHERE id=?", (ident,)).fetchone()
        return _job_row(row)

    def claim_next(self, *, worker_token: str | None = None) -> dict[str, Any] | None:
        token = worker_token or uuid.uuid4().hex
        now = float(self.clock())
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            running = self.db.execute(
                "SELECT COUNT(*) FROM research_jobs WHERE state IN ('running','cancel_requested')"
            ).fetchone()[0]
            if int(running) >= MAX_ACTIVE_JOBS:
                return None
            row = self.db.execute(
                """SELECT * FROM research_jobs WHERE state='queued'
                   ORDER BY queued_at,id LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            try:
                self._reserve_holdout_locked(
                    str(row["experiment_id"]), now, attempt_id=str(row["attempt_id"])
                )
            except ResearchJobError as exc:
                if exc.code != "holdout_conflict":
                    raise
                # The oldest selected row is failed inside the same transaction.
                # Never rediscover a job through a bounded, newest-first API list.
                self._fail_queued_locked(row, exc, now, phase="holdout_reservation")
                failed = self.db.execute(
                    "SELECT * FROM research_jobs WHERE id=?", (row["id"],)
                ).fetchone()
                return _job_row(failed)
            self.db.execute(
                """UPDATE research_jobs SET state='running',worker_token=?,
                   started_at=COALESCE(started_at,?),updated_at=? WHERE id=? AND state='queued'""",
                (token, now, now, row["id"]),
            )
            self.db.execute(
                """UPDATE research_attempts SET status='running',
                   started_at=COALESCE(started_at,?) WHERE id=?""",
                (now, row["attempt_id"]),
            )
            row = self.db.execute(
                "SELECT * FROM research_jobs WHERE id=?", (row["id"],)
            ).fetchone()
        return _job_row(row)

    def _reserve_holdout_locked(
        self, experiment_id: str, now: float, *, attempt_id: str | None = None,
    ) -> None:
        experiment = self.db.execute(
            "SELECT * FROM research_experiments WHERE id=?", (experiment_id,)
        ).fetchone()
        if experiment is None:
            raise ResearchJobError("Unknown research experiment", code="not_found")
        if experiment["purpose"] != "eligible":
            return
        existing = self.db.execute(
            "SELECT 1 FROM research_holdout_ranges WHERE experiment_id=?", (experiment_id,)
        ).fetchone()
        if existing is not None:
            return
        conflict = self.db.execute(
            """SELECT experiment_id FROM research_holdout_ranges
               WHERE source_session_id=? AND first_at<? AND last_at>?
                 AND experiment_id!=? LIMIT 1""",
            (
                experiment["source_session_id"], experiment["holdout_last_at"],
                experiment["holdout_first_at"], experiment_id,
            ),
        ).fetchone()
        payload = json.loads(experiment["payload_json"])
        evidence_ids = payload["lineage"]["holdout_evidence_ids"]
        evidence_conflict = None
        for offset in range(0, len(evidence_ids), 500):
            page = evidence_ids[offset:offset + 500]
            if not page:
                continue
            placeholders = ",".join("?" for _ in page)
            evidence_conflict = self.db.execute(
                f"""SELECT evidence_id FROM research_holdout_evidence
                    WHERE evidence_id IN ({placeholders})
                      AND first_experiment_id!=? LIMIT 1""",
                [*page, experiment_id],
            ).fetchone()
            if evidence_conflict is not None:
                break
        if conflict is not None or evidence_conflict is not None:
            raise ResearchJobError(
                "Overlapping holdout evidence was already exposed by another experiment",
                code="holdout_conflict",
            )
        self._insert_holdout_lineage_locked(experiment, payload, now)
        self._record_exposure_locked(
            experiment, now, attempt_id=attempt_id,
            source="claim_transaction", reason="eligible_holdout_reserved_before_replay",
            inferred=False,
        )

    def _fail_queued_locked(
        self, row: sqlite3.Row, error: object, now: float, *, phase: str,
    ) -> None:
        failure = _failure(error, phase=phase)
        encoded = canonical_json(failure)
        self.db.execute(
            """UPDATE research_attempts SET status='failed',finished_at=?,error=?,
                      error_code=?,error_json=?
               WHERE id=? AND status='queued'""",
            (now, failure["message"], failure["code"], encoded, row["attempt_id"]),
        )
        self.db.execute(
            """UPDATE research_jobs SET state='failed',finished_at=?,updated_at=?,
                      error=?,error_code=?,error_json=?,worker_token=NULL
               WHERE id=? AND state='queued'""",
            (
                now, now, failure["message"], failure["code"], encoded, row["id"],
            ),
        )

    def progress(self, job_id: str, value: float) -> None:
        progress = _finite(value, "job progress")
        if not 0 <= progress <= 1:
            raise ResearchJobError("Job progress must be between zero and one")
        with self._lock, self.db:
            self.db.execute(
                """UPDATE research_jobs SET progress=MAX(progress,?),updated_at=?
                   WHERE id=? AND state IN ('running','cancel_requested')""",
                (progress, float(self.clock()), _text(job_id, "job_id")),
            )

    def cancellation_requested(self, job_id: str) -> bool:
        with self._lock:
            row = self.db.execute(
                "SELECT state FROM research_jobs WHERE id=?", (_text(job_id, "job_id"),)
            ).fetchone()
        return row is None or row["state"] == "cancel_requested"

    def complete(self, job_id: str, result: object) -> dict[str, Any]:
        if not isinstance(result, dict) or result.get("schema") != REPORT_SCHEMA:
            raise ResearchJobError("Replay returned an unsupported or incomplete report")
        now = float(self.clock())
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM research_jobs WHERE id=?", (_text(job_id, "job_id"),)
            ).fetchone()
            if row is None:
                raise ResearchJobError("Unknown research job", code="not_found")
            experiment = json.loads(self.db.execute(
                "SELECT payload_json FROM research_experiments WHERE id=?",
                (row["experiment_id"],),
            ).fetchone()["payload_json"])
            normalized = json.loads(canonical_json(result))
            if normalized.get("experiment_id") != row["experiment_id"]:
                raise ResearchJobError("Replay result experiment identity is stale or mismatched",
                                       code="identity_conflict")
            if normalized.get("dataset_id") != experiment["dataset_id"]:
                raise ResearchJobError("Replay result dataset identity is stale or mismatched",
                                       code="identity_conflict")
            if experiment["purpose"] == "exploratory":
                if _report_has_holdout(normalized):
                    raise ResearchJobError(
                        "Exploratory replay returned holdout output; the result was rejected",
                        code="unexpected_holdout_exposure",
                    )
                normalized["holdout_state"] = "not_evaluated"
            report_core = {key: value for key, value in normalized.items()
                           if key not in {"id", "content_sha256", "created_at"}}
            content_sha = _sha256(report_core)
            normalized.setdefault("id", "report-" + content_sha[:32])
            normalized.setdefault("content_sha256", content_sha)
            normalized.setdefault("created_at", now)
            if (not isinstance(normalized["id"], str) or not normalized["id"]
                    or not isinstance(normalized["content_sha256"], str)
                    or len(normalized["content_sha256"]) != 64):
                raise ResearchJobError("Replay report identity is invalid",
                                       code="identity_conflict")
            payload = canonical_json(normalized)
            if len(payload.encode()) > MAX_RESULT_BYTES:
                raise ResearchJobError("Replay report exceeds the result byte cap")
            digest = hashlib.sha256(payload.encode()).hexdigest()
            if row["state"] == "cancel_requested":
                self._cancel_running_locked(row, now)
            elif row["state"] == "completed":
                attempt = self.db.execute(
                    "SELECT result_sha256 FROM research_attempts WHERE id=?",
                    (row["attempt_id"],),
                ).fetchone()
                if attempt["result_sha256"] != digest:
                    raise ResearchJobError("Completed job result is immutable",
                                           code="identity_conflict")
            elif row["state"] != "running":
                raise ResearchJobError("Only a running job can publish a result")
            else:
                self.db.execute(
                    """UPDATE research_attempts SET status='completed',finished_at=?,
                       result_sha256=?,result_json=?,error='',error_code='',error_json=''
                       WHERE id=?""",
                    (now, digest, payload, row["attempt_id"]),
                )
                self.db.execute(
                    """UPDATE research_jobs SET state='completed',progress=1,
                       finished_at=?,updated_at=?,error='',error_code='',error_json='',
                       worker_token=NULL WHERE id=?""",
                    (now, now, row["id"]),
                )
            row = self.db.execute(
                "SELECT * FROM research_jobs WHERE id=?", (row["id"],)
            ).fetchone()
        return _job_row(row)

    def fail(self, job_id: str, error: object) -> dict[str, Any]:
        failure = _failure(error, phase="replay")
        detail = failure["message"]
        encoded = canonical_json(failure)
        now = float(self.clock())
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM research_jobs WHERE id=?", (_text(job_id, "job_id"),)
            ).fetchone()
            if row is None:
                raise ResearchJobError("Unknown research job", code="not_found")
            if row["state"] == "cancel_requested":
                self._cancel_running_locked(row, now)
            elif row["state"] == "running":
                self.db.execute(
                    """UPDATE research_attempts SET status='failed',finished_at=?,error=?,
                              error_code=?,error_json=?
                       WHERE id=?""",
                    (now, detail, failure["code"], encoded, row["attempt_id"]),
                )
                self.db.execute(
                    """UPDATE research_jobs SET state='failed',finished_at=?,updated_at=?,
                       error=?,error_code=?,error_json=?,worker_token=NULL WHERE id=?""",
                    (now, now, detail, failure["code"], encoded, row["id"]),
                )
            row = self.db.execute(
                "SELECT * FROM research_jobs WHERE id=?", (row["id"],)
            ).fetchone()
        return _job_row(row)

    def _cancel_running_locked(self, row: sqlite3.Row, now: float) -> None:
        self.db.execute(
            """UPDATE research_attempts SET status='cancelled',finished_at=?,
               error='Cancelled during replay' WHERE id=?""",
            (now, row["attempt_id"]),
        )
        self.db.execute(
            """UPDATE research_jobs SET state='cancelled',finished_at=?,updated_at=?,
               error='Cancelled during replay',worker_token=NULL WHERE id=?""",
            (now, now, row["id"]),
        )

    def recover_interrupted(self) -> int:
        """Requeue the same durable attempt; never mint a replacement trial."""
        now = float(self.clock())
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT id,attempt_id FROM research_jobs WHERE state IN ('running','cancel_requested')"
            ).fetchall()
            for row in rows:
                job = self.db.execute(
                    "SELECT state FROM research_jobs WHERE id=?", (row["id"],)
                ).fetchone()
                if job["state"] == "cancel_requested":
                    full = self.db.execute(
                        "SELECT * FROM research_jobs WHERE id=?", (row["id"],)
                    ).fetchone()
                    self._cancel_running_locked(full, now)
                    continue
                self.db.execute(
                    """UPDATE research_jobs SET state='queued',worker_token=NULL,
                       progress=0,updated_at=?,error='Interrupted by service restart; safely requeued',
                       error_code='service_restart',error_json=''
                       WHERE id=?""",
                    (now, row["id"]),
                )
                self.db.execute(
                    """UPDATE research_attempts SET status='queued',restart_count=restart_count+1,
                       error='Interrupted by service restart; safely requeued',
                       error_code='service_restart',error_json='' WHERE id=?""",
                    (row["attempt_id"],),
                )
        return len(rows)

    def job(self, job_id: str) -> dict[str, Any]:
        return _job_row(self._row("research_jobs", job_id, "job"))

    def jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        _limit(limit)
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM research_jobs ORDER BY queued_at DESC,id LIMIT ?", (limit,)
            ).fetchall()
        return [_job_row(row) for row in rows]

    def attempts(self, experiment_id: str) -> list[dict[str, Any]]:
        ident = _text(experiment_id, "experiment_id")
        with self._lock:
            rows = self.db.execute(
                """SELECT * FROM research_attempts WHERE experiment_id=?
                   ORDER BY ordinal""",
                (ident,),
            ).fetchall()
        return [_attempt_row(row) for row in rows]

    def holdout_status(self, experiment_id: str) -> dict[str, Any]:
        """Return the conservative, read-only holdout state for an experiment."""
        ident = _text(experiment_id, "experiment_id")
        with self._lock:
            experiment = self.db.execute(
                "SELECT purpose FROM research_experiments WHERE id=?", (ident,)
            ).fetchone()
            if experiment is None:
                raise ResearchJobError("Unknown research experiment", code="not_found")
            audit = self.db.execute(
                "SELECT * FROM research_holdout_exposure_audit WHERE experiment_id=?",
                (ident,),
            ).fetchone()
            reserved = self.db.execute(
                "SELECT reserved_at FROM research_holdout_ranges WHERE experiment_id=?",
                (ident,),
            ).fetchone()
        if audit is not None or reserved is not None:
            return {
                "schema": INTEGRITY_SCHEMA,
                "state": "exposed",
                "reason": (audit["reason"] if audit is not None
                           else "holdout_reservation_exists"),
                "inferred": bool(audit["inferred"]) if audit is not None else False,
                "recorded_at": (audit["detected_at"] if audit is not None
                                else reserved["reserved_at"]),
            }
        if experiment["purpose"] == "exploratory":
            return {
                "schema": INTEGRITY_SCHEMA,
                "state": "not_evaluated",
                "reason": "exploratory_holdout_withheld",
                "inferred": False,
                "recorded_at": None,
            }
        return {
            "schema": INTEGRITY_SCHEMA,
            "state": "unseen",
            "reason": "eligible_holdout_not_yet_claimed",
            "inferred": False,
            "recorded_at": None,
        }

    def integrity_summary(self) -> dict[str, Any]:
        """Summarize historical exposure without changing the research database."""
        with self._lock:
            totals = self.db.execute(
                """SELECT COUNT(*) AS records,
                          COALESCE(SUM(inferred),0) AS inferred,
                          COUNT(report_id) AS reports
                   FROM research_holdout_exposure_audit"""
            ).fetchone()
            experiments = int(self.db.execute(
                "SELECT COUNT(*) FROM research_experiments"
            ).fetchone()[0])
            reasons = self.db.execute(
                """SELECT reason,COUNT(*) AS count
                   FROM research_holdout_exposure_audit
                   GROUP BY reason ORDER BY reason"""
            ).fetchall()
            affected = self.db.execute(
                """SELECT audit.experiment_id,audit.attempt_id,audit.report_id,
                          audit.reason,audit.inferred,audit.engine_version,
                          experiment.source_session_id
                   FROM research_holdout_exposure_audit AS audit
                   JOIN research_experiments AS experiment
                     ON experiment.id=audit.experiment_id
                   ORDER BY audit.detected_at,audit.experiment_id"""
            ).fetchall()
            installed = self.db.execute(
                "SELECT value FROM research_integrity_meta WHERE key='installed_at'"
            ).fetchone()
        return {
            "schema": INTEGRITY_SCHEMA,
            "policy": {
                "exploratory": "holdout_not_evaluated",
                "eligible": "reserve_before_replay",
                "historical_missing_record": "not_proof_unseen",
            },
            "installed_at": float(installed["value"]) if installed else None,
            "counts": {
                "experiments": experiments,
                "exposure_records": int(totals["records"]),
                "inferred_historical_exposures": int(totals["inferred"]),
                "reports_with_recorded_exposure": int(totals["reports"]),
            },
            "reasons": {str(row["reason"]): int(row["count"]) for row in reasons},
            "affected_lineage": [
                {
                    "experiment_id": row["experiment_id"],
                    "attempt_id": row["attempt_id"],
                    "report_id": row["report_id"],
                    "source_session_id": row["source_session_id"],
                    "reason": row["reason"],
                    "inferred": bool(row["inferred"]),
                    "engine_version": row["engine_version"],
                }
                for row in affected
            ],
        }

    def report_view(self, report: dict[str, Any]) -> dict[str, Any]:
        """Add current integrity context without rewriting the immutable artifact."""
        viewed = json.loads(canonical_json(report))
        experiment_id = viewed.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id:
            return viewed
        status = self.holdout_status(experiment_id)
        viewed["holdout_state"] = status["state"]
        viewed["integrity"] = status
        warnings: list[str] = []
        engine = str(viewed.get("engine_version", ""))
        if engine in {
            "pumpwizard-finite-capital-replay/2",
            "session-review-v1-compatibility-adapter",
        }:
            viewed["historical_eligible"] = bool(viewed.get("eligible"))
            viewed["eligible"] = False
            blockers = viewed.get("blockers")
            viewed["blockers"] = (
                [str(item) for item in blockers] if isinstance(blockers, list) else []
            ) + ["pre_batch4_engine_requires_revalidation"]
            warnings.append(
                "This report used a pre-Batch-4 engine; preserve it as historical "
                "evidence and do not treat it as newly eligible without revalidation."
            )
        if status["inferred"]:
            warnings.append(
                "Holdout exposure was conservatively inferred during the Batch 4 migration."
            )
        if warnings:
            existing = viewed.get("limitations")
            viewed["limitations"] = (
                [str(item) for item in existing] if isinstance(existing, list) else []
            ) + warnings
        return viewed

    def result_view(self, job_id: str) -> dict[str, Any]:
        return self.report_view(self.result(job_id))

    def result(self, job_id: str) -> dict[str, Any]:
        job = self._row("research_jobs", job_id, "job")
        if job["state"] != "completed":
            raise ResearchJobError("Research job has no completed immutable result",
                                   code="not_found")
        with self._lock:
            row = self.db.execute(
                "SELECT result_json,result_sha256 FROM research_attempts WHERE id=?",
                (job["attempt_id"],),
            ).fetchone()
        payload = str(row["result_json"])
        if hashlib.sha256(payload.encode()).hexdigest() != row["result_sha256"]:
            raise ResearchJobError("Stored research result failed integrity verification",
                                   code="identity_conflict")
        return json.loads(payload)

    def reports(self, *, source_session_id: str | None = None,
                limit: int = 100) -> list[dict[str, Any]]:
        _limit(limit)
        parameters: list[object] = []
        clause = "WHERE attempt.status='completed'"
        if source_session_id is not None:
            clause += " AND experiment.source_session_id=?"
            parameters.append(_text(source_session_id, "source_session_id"))
        parameters.append(limit)
        with self._lock:
            rows = self.db.execute(
                """SELECT attempt.result_json FROM research_attempts AS attempt
                   JOIN research_experiments AS experiment
                     ON experiment.id=attempt.experiment_id """
                + clause
                + " ORDER BY attempt.finished_at DESC,attempt.id LIMIT ?",
                parameters,
            ).fetchall()
        return [json.loads(row["result_json"]) for row in rows]

    def result_by_report_id(self, report_id: str) -> dict[str, Any]:
        ident = _text(report_id, "report_id")
        with self._lock:
            rows = self.db.execute(
                """SELECT result_json,result_sha256 FROM research_attempts
                   WHERE status='completed' AND result_json IS NOT NULL
                   ORDER BY finished_at DESC"""
            ).fetchall()
        for row in rows:
            result = json.loads(row["result_json"])
            if result.get("id") == ident:
                payload = canonical_json(result)
                if hashlib.sha256(payload.encode()).hexdigest() != row["result_sha256"]:
                    raise ResearchJobError(
                        "Stored research result failed integrity verification",
                        code="identity_conflict",
                    )
                return result
        raise ResearchJobError("Unknown completed research report", code="not_found")

    def report_view_by_id(self, report_id: str) -> dict[str, Any]:
        return self.report_view(self.result_by_report_id(report_id))

    def _row(self, table: str, ident: object, label: str) -> sqlite3.Row:
        if table not in {"research_experiments", "research_jobs"}:
            raise RuntimeError("Invalid internal research table")
        with self._lock:
            row = self.db.execute(
                f"SELECT * FROM {table} WHERE id=?", (_text(ident, f"{label}_id"),)
            ).fetchone()
        if row is None:
            raise ResearchJobError(f"Unknown research {label}", code="not_found")
        return row

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> "ResearchRegistry":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class ResearchJobWorker:
    """One service-owned worker; evaluator never receives service authority."""

    def __init__(self, data_dir: str | Path,
                 evaluator: Callable[..., dict[str, Any]] | None = None,
                 *, clock=time.time):
        self.root = Path(data_dir)
        self.clock = clock
        self.evaluator = evaluator or resolve_replay_evaluator(self.root)
        self.registry = ResearchRegistry(self.root, clock=clock)
        self.registry.recover_interrupted()

    def run_once(self) -> dict[str, Any] | None:
        job = self.registry.claim_next()
        if job is None:
            return None
        if job["state"] == "failed":
            return job
        data = ResearchDataStore(self.root, clock=self.clock)
        try:
            dataset = data.export(job["dataset_id"] if "dataset_id" in job else
                                  self.registry.experiment(job["experiment_id"])["dataset_id"])
            experiment = self.registry.experiment(job["experiment_id"])
            result = self.evaluator(
                dataset,
                experiment,
                lambda value: self.registry.progress(job["id"], value),
                lambda: self.registry.cancellation_requested(job["id"]),
            )
            return self.registry.complete(job["id"], result)
        except Exception as exc:
            return self.registry.fail(job["id"], exc)
        finally:
            data.close()

    def close(self) -> None:
        self.registry.close()


def resolve_replay_evaluator(data_dir: Path) -> Callable[..., dict[str, Any]]:
    """Prefer Worker 2's Batch 3 evaluator, retaining a v1 compatibility adapter."""
    for module_name, attribute in (
        ("pumpwizard_runner.research_replay", "evaluate_research_experiment"),
        ("pumpwizard_runner.portfolio_research", "evaluate_research_experiment"),
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        candidate = getattr(module, attribute, None)
        if callable(candidate):
            return candidate
    return _legacy_replay_evaluator(data_dir)


def _legacy_replay_evaluator(data_dir: Path) -> Callable[..., dict[str, Any]]:
    def evaluate(dataset, experiment, progress, cancelled):
        from .session_review import SessionReviewStore, compare_dataset, freeze_dataset

        progress(0.05)
        if cancelled():
            raise ResearchJobError("Research comparison cancelled")
        records = dataset["records"]
        source = dataset["dataset"]["source"]
        with SessionReviewStore(data_dir) as legacy:
            frozen = freeze_dataset(
                legacy,
                source_session_id=source["session_id"],
                strategy_package=source["strategy_package"],
                strategy_digest=source["strategy_sha256"],
                observations=[row["payload"] for row in records["observations"]],
                decisions=records["decisions"], outcomes=records["outcomes"],
                feature_records=records["features"],
                decision_capture_complete=dataset["dataset"]["eligibility"][
                    "decision_capture_complete"
                ],
                first_at=dataset["dataset"]["ranges"]["event"]["first"],
                last_at=dataset["dataset"]["ranges"]["event"]["last"],
            )
            progress(0.2)
            horizon = int(experiment["evidence_policy"].get("horizon_seconds", 1800))
            report = compare_dataset(
                legacy, frozen["id"], horizon_seconds=horizon, cancelled=cancelled,
                evaluate_holdout=experiment["purpose"] == "eligible",
            )
        progress(0.95)
        return {
            "schema": REPORT_SCHEMA,
            "experiment_id": experiment["id"],
            "dataset_id": experiment["dataset_id"],
            "engine_version": "session-review-v1-compatibility-adapter",
            "capital": experiment["capital"],
            "cost_model": experiment["cost_model"],
            "purpose": experiment["purpose"],
            "eligible": False,
            "holdout_state": (
                "not_evaluated" if experiment["purpose"] == "exploratory" else "exposed"
            ),
            "comparisons": report.get("results", []),
            "baselines": ["legacy-current-strategy", "legacy-no-entry"],
            "coverage": report.get("evidence", {}),
            "exclusions": report.get("limitations", []),
            "unknown_or_unpriced": sum(
                split["metrics"].get("unknown_outcomes", 0)
                for row in report.get("results", [])
                for split in row.get("splits", {}).values()
            ),
            "recommendation": report.get("recommendation"),
            "blockers": [
                "Compatibility replay is not the Batch 3 finite-capital Worker 2 engine; "
                "its output remains exploratory."
            ],
            "legacy_report": report,
        }

    return evaluate


def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    failure = _loaded_object(row["error_json"]) if row["error_json"] else None
    return {
        "schema": JOB_SCHEMA,
        "id": row["id"],
        "experiment_id": row["experiment_id"],
        "attempt_id": row["attempt_id"],
        "idempotency_key": row["idempotency_key"],
        "state": row["state"],
        "progress": row["progress"],
        "cancellation_requested_at": row["cancellation_requested_at"],
        "queued_at": row["queued_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "updated_at": row["updated_at"],
        "error": row["error"],
        "error_code": row["error_code"],
        "failure": failure,
        "resource_limits": {
            "concurrency": MAX_ACTIVE_JOBS,
            "candidates": MAX_CANDIDATES,
            "result_bytes": MAX_RESULT_BYTES,
        },
    }


def _attempt_row(row: sqlite3.Row) -> dict[str, Any]:
    failure = _loaded_object(row["error_json"]) if row["error_json"] else None
    return {
        "schema": ATTEMPT_SCHEMA,
        "id": row["id"], "experiment_id": row["experiment_id"],
        "ordinal": row["ordinal"], "status": row["status"],
        "queued_at": row["queued_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"], "restart_count": row["restart_count"],
        "result_sha256": row["result_sha256"], "error": row["error"],
        "error_code": row["error_code"], "failure": failure,
    }


def _json_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResearchJobError(f"Experiment {label} must be an object")
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ResearchJobError(f"Experiment {label} must be canonical JSON") from exc


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ResearchJobError(f"{label} must be non-empty bounded text")
    return value.strip()


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchJobError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ResearchJobError(f"{label} must be a finite number")
    return result


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _safe_error(value: object) -> str:
    text = str(value).strip().splitlines()[0] if str(value).strip() else type(value).__name__
    if "?" in text and "://" in text:
        text = text.split("?", 1)[0] + "?[redacted]"
    return text[:500]


def _failure(value: object, *, phase: str) -> dict[str, Any]:
    code = value.code if isinstance(value, ResearchJobError) else "replay_failed"
    return {
        "schema": "pumpwizard.research-failure/v1",
        "code": code,
        "message": _safe_error(value),
        "phase": phase,
        "retryable": code not in {
            "holdout_conflict", "identity_conflict", "unexpected_holdout_exposure",
        },
    }


def _loaded_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _report_explicitly_withheld(report: dict[str, Any]) -> bool:
    return (report.get("holdout_state") in {"not_evaluated", "withheld"}
            and not _report_has_holdout(report))


def _report_has_holdout(report: dict[str, Any]) -> bool:
    if report.get("holdout_state") in {"exposed", "reserved"}:
        return True
    comparison = report.get("detailed_comparison")
    if isinstance(comparison, dict) and comparison.get("holdout_results"):
        return True
    if report.get("holdout_results"):
        return True
    legacy = report.get("legacy_report")
    if isinstance(legacy, dict):
        for row in legacy.get("results", []):
            if isinstance(row, dict) and isinstance(row.get("splits"), dict):
                if row["splits"].get("holdout"):
                    return True
    return False


def _limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
        raise ResearchJobError("Research list limit must be between 1 and 500")
