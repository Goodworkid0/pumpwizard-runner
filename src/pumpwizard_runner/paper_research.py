"""Prospective, service-owned paper observation with no execution authority."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable

from .decision import DecisionEngine, MarketQuote
from .service_recorder import ServiceEvidenceRecorder
from .strategies import canonical_json, parse_package


DATABASE_NAME = "paper-research.sqlite"
SCHEMA_VERSION = 3
RUN_SCHEMA = "pumpwizard.paper-run/v1"
OBSERVATION_SCHEMA = "pumpwizard.paper-observation/v1"
DECISION_SCHEMA = "pumpwizard.paper-decision/v1"
OUTCOME_SCHEMA = "pumpwizard.paper-outcome/v1"
START_CONFIRMATION = "START PAPER RESEARCH"
MAX_OBSERVATIONS_PER_CYCLE = 1_000
DEFAULT_OUTCOME_HORIZON = 1_800
MAX_ENGINE_RESTORE_OBSERVATIONS = 10_000
MAX_ENGINE_STATE_OBSERVATIONS = 10_000
MAX_RUN_RETRIES = 3


class PaperResearchError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_paper_research"):
        super().__init__(message)
        self.code = code


def monitoring_session(library, source_session_id: str) -> bool:
    """Use the stored brief and owning workspace, including older session briefs."""
    instance = library.instance(source_session_id)
    brief = library.session_brief(source_session_id)
    if (brief.get('session_mode') not in (None, 'monitoring')
            or brief.get('mode') not in ('observe-only', 'monitoring')):
        return False
    workspace_id = instance.get('workspace_id')
    return not workspace_id or library.session_workspace(workspace_id)['mode'] == 'monitoring'


def require_monitoring_session(library, source_session_id: str) -> None:
    if not monitoring_session(library, source_session_id):
        raise PaperResearchError('Paper research is available only in monitoring sessions.',
                                 code='monitoring_session_required')


class PaperResearchStore:
    def __init__(self, data_dir: str | Path, *, clock=time.time):
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / DATABASE_NAME
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA busy_timeout=15000")
            for attempt in range(8):
                try:
                    self.db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 7:
                        raise
                    time.sleep(0.01 * 2 ** attempt)
            self.db.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except Exception:
            self.db.close()
            raise

    def _migrate(self) -> None:
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            row = self.db.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is not None and int(row["value"]) > SCHEMA_VERSION:
                raise PaperResearchError(
                    "Paper research was created by a newer PumpWizard version",
                    code="unsupported_schema",
                )
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS paper_runs (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    source_session_id TEXT NOT NULL,
                    strategy_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('running','paused','stopped','interrupted')),
                    start_at REAL NOT NULL,
                    cursor_received_at REAL NOT NULL DEFAULT -1,
                    cursor_evidence_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    stopped_at REAL,
                    last_cycle_at REAL,
                    payload_json TEXT NOT NULL,
                    evaluator_checkpoint_json TEXT,
                    evaluator_report_json TEXT
                );
                CREATE INDEX IF NOT EXISTS paper_run_source
                    ON paper_runs(source_session_id,created_at,id);
                CREATE TABLE IF NOT EXISTS paper_observations (
                    run_id TEXT NOT NULL REFERENCES paper_runs(id),
                    evidence_id TEXT NOT NULL,
                    event_at REAL NOT NULL,
                    first_received_at REAL NOT NULL,
                    mint TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY(run_id,evidence_id)
                );
                CREATE INDEX IF NOT EXISTS paper_observation_time
                    ON paper_observations(run_id,first_received_at,evidence_id);
                CREATE TABLE IF NOT EXISTS paper_decisions (
                    run_id TEXT NOT NULL REFERENCES paper_runs(id),
                    decision_key TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    decided_at REAL NOT NULL,
                    mint TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY(run_id,decision_key)
                );
                CREATE INDEX IF NOT EXISTS paper_decision_time
                    ON paper_decisions(run_id,decided_at,decision_key);
                CREATE TABLE IF NOT EXISTS paper_outcomes (
                    run_id TEXT NOT NULL,
                    decision_key TEXT NOT NULL,
                    horizon_seconds INTEGER NOT NULL CHECK(horizon_seconds > 0),
                    scored_at REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('scored','unknown','unpriced')),
                    record_json TEXT NOT NULL,
                    PRIMARY KEY(run_id,decision_key,horizon_seconds),
                    FOREIGN KEY(run_id,decision_key)
                        REFERENCES paper_decisions(run_id,decision_key)
                );
                CREATE TABLE IF NOT EXISTS paper_gaps (
                    run_id TEXT NOT NULL REFERENCES paper_runs(id),
                    gap_id TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    reason TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY(run_id,gap_id)
                );
                CREATE TABLE IF NOT EXISTS paper_operating_intervals (
                    run_id TEXT NOT NULL REFERENCES paper_runs(id),
                    interval_id TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    reason TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY(run_id,interval_id)
                );
                CREATE INDEX IF NOT EXISTS paper_operating_interval_time
                    ON paper_operating_intervals(run_id,started_at,interval_id);
                CREATE TABLE IF NOT EXISTS paper_faults (
                    run_id TEXT NOT NULL REFERENCES paper_runs(id),
                    fault_id TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    evidence_id TEXT,
                    code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    retryable INTEGER NOT NULL,
                    retry_count INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY(run_id,fault_id)
                );
            """)
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(paper_runs)")}
            if "evaluator_checkpoint_json" not in columns:
                self.db.execute("ALTER TABLE paper_runs ADD COLUMN evaluator_checkpoint_json TEXT")
            if "evaluator_report_json" not in columns:
                self.db.execute("ALTER TABLE paper_runs ADD COLUMN evaluator_report_json TEXT")
            additions = {
                "request_sha256": "TEXT",
                "decision_not_before_at": "REAL",
                "paused_at": "REAL",
                "interrupted_at": "REAL",
                "retry_count": "INTEGER NOT NULL DEFAULT 0",
                "max_retries": f"INTEGER NOT NULL DEFAULT {MAX_RUN_RETRIES}",
                "fault_json": "TEXT",
                "next_retry_at": "REAL",
                "evaluator_checkpoint_received_at": "REAL NOT NULL DEFAULT -1",
                "evaluator_checkpoint_evidence_id": "TEXT NOT NULL DEFAULT ''",
                "decision_checkpoint_json": "TEXT",
                "decision_checkpoint_received_at": "REAL NOT NULL DEFAULT -1",
                "decision_checkpoint_evidence_id": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in additions.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE paper_runs ADD COLUMN {name} {definition}")
            observation_columns = {
                row["name"] for row in self.db.execute("PRAGMA table_info(paper_observations)")
            }
            if "quality_status" not in observation_columns:
                self.db.execute(
                    "ALTER TABLE paper_observations ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'eligible'"
                )
            if "exclusion_reason" not in observation_columns:
                self.db.execute(
                    "ALTER TABLE paper_observations ADD COLUMN exclusion_reason TEXT NOT NULL DEFAULT ''"
                )
            self.db.execute(
                "UPDATE paper_runs SET decision_not_before_at=start_at "
                "WHERE decision_not_before_at IS NULL"
            )
            # Campaign-created runs already open a purpose-specific interval in
            # the same creation transaction.  Older migrations also synthesized
            # a legacy interval for those runs, producing overlapping uptime.
            # Remove only that redundant synthetic row and retain every native
            # campaign/paper interval.
            self.db.execute(
                """DELETE FROM paper_operating_intervals AS legacy
                   WHERE legacy.interval_id='legacy-start:' || legacy.run_id
                     AND EXISTS (
                         SELECT 1 FROM paper_operating_intervals AS native
                         WHERE native.run_id=legacy.run_id
                           AND native.interval_id<>legacy.interval_id
                           AND native.started_at=legacy.started_at
                     )"""
            )
            self.db.execute(
                """INSERT OR IGNORE INTO paper_operating_intervals(
                       run_id,interval_id,started_at,ended_at,reason,record_json
                   ) SELECT run.id,'legacy-start:' || run.id,run.start_at,run.stopped_at,
                       'paper-run-start',
                       json_object('schema','pumpwizard.paper-operating-interval/v1',
                                   'run_id',run.id,
                                   'interval_id','legacy-start:' || run.id,
                                   'started_at',run.start_at,'ended_at',run.stopped_at,
                                   'reason','paper-run-start')
                     FROM paper_runs AS run
                    WHERE NOT EXISTS (
                        SELECT 1 FROM paper_operating_intervals AS interval
                        WHERE interval.run_id=run.id
                    )"""
            )

    def start(self, *, source_session_id: str, strategy_package: dict[str, Any],
              strategy_sha256: str, simulated_capital_usd: float,
              start_at: float | None, idempotency_key: str,
              confirmation: str) -> dict[str, Any]:
        if confirmation != START_CONFIRMATION:
            raise PaperResearchError(
                f"Starting paper research requires exact confirmation: {START_CONFIRMATION}"
            )
        session_id = _text(source_session_id, "source_session_id")
        strategy_digest = _text(strategy_sha256, "strategy_sha256")
        key = _text(idempotency_key, "idempotency_key")
        parsed = parse_package(json.loads(canonical_json(strategy_package)))
        if parsed.digest != strategy_digest:
            raise PaperResearchError("Paper strategy digest is stale or mismatched",
                                     code="identity_conflict")
        capital = _finite(simulated_capital_usd, "simulated_capital_usd")
        if not 0 < capital <= 1_000_000_000:
            raise PaperResearchError("Paper capital must be positive bounded USD")
        request_core = {
            "source_session_id": session_id,
            "strategy_sha256": strategy_digest,
            "strategy_package": json.loads(canonical_json(strategy_package)),
            "simulated_capital": {"amount": capital, "unit": "USD"},
            "evaluator": "pumpwizard-paper-decision-engine/v1",
            "outcome_horizon_seconds": DEFAULT_OUTCOME_HORIZON,
        }
        request_digest = _sha256(request_core)
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT * FROM paper_runs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                saved = json.loads(existing["payload_json"])
                saved_request = existing["request_sha256"] or _request_sha256(saved)
                if saved_request != request_digest:
                    raise PaperResearchError(
                        "Paper idempotency key belongs to different immutable settings",
                        code="identity_conflict",
                    )
                return self._overview_row(existing)
            # start_at is service-assigned metadata, not caller request identity.
            # It is read only after the idempotency lookup while holding the
            # write transaction, so a lost response can be retried at any later
            # wall-clock time without creating or conflicting with the run.
            now = float(self.clock())
            started = _finite(now if start_at is None else start_at, "start_at")
            if started < 0:
                raise PaperResearchError("Paper start time cannot be negative")
            content_digest = _sha256({**request_core, "start_at": started})
            run = {
                "schema": RUN_SCHEMA,
                "id": "paper-" + request_digest[:24] + "-" + uuid.uuid4().hex[:8],
                "request_sha256": request_digest,
                "content_sha256": content_digest,
                "state": "running",
                **request_core,
                "start_at": started,
                "authority": {
                    "wallet_allocation": False, "signer": False,
                    "orders": False, "paid_provider": False,
                },
            }
            payload = canonical_json(run)
            evaluator = _portfolio_evaluator(run)
            evaluator_checkpoint = canonical_json(evaluator.checkpoint())
            evaluator_report = canonical_json(evaluator.report())
            self.db.execute(
                """INSERT INTO paper_runs(
                       id,idempotency_key,source_session_id,strategy_sha256,state,
                       start_at,created_at,payload_json,evaluator_checkpoint_json,
                       evaluator_report_json,request_sha256,decision_not_before_at,
                       max_retries
                   ) VALUES(?,?,?,?,'running',?,?,?,?,?,?,?,?)""",
                (run["id"], key, session_id, strategy_digest, started, now, payload,
                 evaluator_checkpoint, evaluator_report, request_digest, started,
                 MAX_RUN_RETRIES),
            )
            self._open_interval_locked(
                run["id"], started, reason="paper-run-start",
                interval_id="start:" + run["id"],
            )
            row = self.db.execute("SELECT * FROM paper_runs WHERE id=?", (run["id"],)).fetchone()
        return self._overview_row(row)

    def stop(self, run_id: str, *, confirmation: str) -> dict[str, Any]:
        ident = _text(run_id, "run_id")
        if confirmation != ident:
            raise PaperResearchError("Stopping paper research requires the exact run ID")
        now = float(self.clock())
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            cursor = self.db.execute(
                """UPDATE paper_runs SET state='stopped',stopped_at=?
                   WHERE id=? AND state!='stopped'""",
                (now, ident),
            )
            self._close_interval_locked(ident, now)
            self._close_open_gaps_locked(ident, now)
            row = self.db.execute("SELECT * FROM paper_runs WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise PaperResearchError("Unknown paper run", code="not_found")
        if cursor.rowcount not in {0, 1}:
            raise PaperResearchError("Paper run stop failed")
        return self._overview_row(row)

    def command(self, run_id: str, *, action: str) -> dict[str, Any]:
        ident = _text(run_id, "run_id")
        if action not in {"pause", "resume"}:
            raise PaperResearchError("Paper command must be pause or resume")
        now = float(self.clock())
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM paper_runs WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise PaperResearchError("Unknown paper run", code="not_found")
            if action == "pause":
                if row["state"] != "running":
                    raise PaperResearchError("Paper run cannot pause from its current state")
                self.db.execute(
                    "UPDATE paper_runs SET state='paused',paused_at=? WHERE id=?",
                    (now, ident),
                )
                self._close_interval_locked(ident, now)
                self._upsert_gap_locked(
                    ident, gap_id="pause:" + _sha256([ident, now])[:24],
                    started_at=now, ended_at=None,
                    reason="Paper decisions paused by the user",
                )
            else:
                if row["state"] not in {"paused", "interrupted"}:
                    raise PaperResearchError("Paper run cannot resume from its current state")
                if int(row["retry_count"] or 0) >= int(row["max_retries"] or MAX_RUN_RETRIES):
                    raise PaperResearchError(
                        "Paper run reached its bounded retry limit; repair the evidence or start a new run",
                        code="retry_limit",
                    )
                self._close_open_gaps_locked(
                    ident, now,
                    prefixes=("pause:", "service-interruption:", "fault-gap:"),
                )
                self.db.execute(
                    """UPDATE paper_runs SET state='running',paused_at=NULL,
                       interrupted_at=NULL,decision_not_before_at=?,fault_json=NULL,
                       next_retry_at=NULL WHERE id=?""",
                    (now, ident),
                )
                self._open_interval_locked(ident, now, reason="paper-run-resume")
            row = self.db.execute("SELECT * FROM paper_runs WHERE id=?", (ident,)).fetchone()
        return self._overview_row(row)

    def recover_running(self) -> int:
        """Mark runs interrupted across a service restart without backfilling decisions."""
        now = float(self.clock())
        recovered = 0
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT * FROM paper_runs WHERE state='running' ORDER BY created_at,id"
            ).fetchall()
            for row in rows:
                started = float(row["last_cycle_at"] or row["created_at"] or row["start_at"])
                gap_id = "service-interruption:" + _sha256([row["id"], started])[:24]
                self._upsert_gap_locked(
                    str(row["id"]), gap_id=gap_id, started_at=started,
                    ended_at=None,
                    reason="Paper decision service was offline; missed decisions are not backfilled",
                )
                self._close_interval_locked(str(row["id"]), started)
                self.db.execute(
                    """UPDATE paper_runs SET state='interrupted',interrupted_at=?,
                       decision_not_before_at=? WHERE id=?""",
                    (now, now, row["id"]),
                )
                recovered += 1
        return recovered

    def run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM paper_runs WHERE id=?", (_text(run_id, "run_id"),)
            ).fetchone()
        if row is None:
            raise PaperResearchError("Unknown paper run", code="not_found")
        return self._overview_row(row)

    def runs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        _limit(limit)
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM paper_runs ORDER BY created_at DESC,id LIMIT ?", (limit,)
            ).fetchall()
        return [self._overview_row(row) for row in rows]

    def active_rows(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM paper_runs WHERE state='running' ORDER BY created_at,id"
            ).fetchall()

    def history(self, run_id: str, *, limit: int = 500) -> dict[str, Any]:
        _limit(limit, maximum=1000)
        run = self.run(run_id)
        ident = run["id"]
        with self._lock:
            observations = self.db.execute(
                """SELECT record_json FROM (
                       SELECT record_json,first_received_at,evidence_id
                       FROM paper_observations WHERE run_id=?
                       ORDER BY first_received_at DESC,evidence_id DESC LIMIT ?
                   ) ORDER BY first_received_at,evidence_id""",
                (ident, limit),
            ).fetchall()
            decisions = self.db.execute(
                """SELECT record_json FROM (
                       SELECT record_json,decided_at,decision_key
                       FROM paper_decisions WHERE run_id=?
                       ORDER BY decided_at DESC,decision_key DESC LIMIT ?
                   ) ORDER BY decided_at,decision_key""",
                (ident, limit),
            ).fetchall()
            outcomes = self.db.execute(
                """SELECT record_json FROM (
                       SELECT record_json,scored_at,decision_key
                       FROM paper_outcomes WHERE run_id=?
                       ORDER BY scored_at DESC,decision_key DESC LIMIT ?
                   ) ORDER BY scored_at,decision_key""",
                (ident, limit),
            ).fetchall()
            gaps = self.db.execute(
                """SELECT record_json FROM (
                       SELECT record_json,started_at,gap_id
                       FROM paper_gaps WHERE run_id=?
                       ORDER BY started_at DESC,gap_id DESC LIMIT ?
                   ) ORDER BY started_at,gap_id""",
                (ident, limit),
            ).fetchall()
            intervals = self.db.execute(
                """SELECT record_json FROM (
                       SELECT record_json,started_at,interval_id
                       FROM paper_operating_intervals WHERE run_id=?
                       ORDER BY started_at DESC,interval_id DESC LIMIT ?
                   ) ORDER BY started_at,interval_id""",
                (ident, limit),
            ).fetchall()
            faults = self.db.execute(
                """SELECT record_json FROM (
                       SELECT record_json,occurred_at,fault_id
                       FROM paper_faults WHERE run_id=?
                       ORDER BY occurred_at DESC,fault_id DESC LIMIT ?
                   ) ORDER BY occurred_at,fault_id""",
                (ident, limit),
            ).fetchall()
        return {
            "run": run,
            "observations": [json.loads(row["record_json"]) for row in observations],
            "decisions": [json.loads(row["record_json"]) for row in decisions],
            "outcomes": [json.loads(row["record_json"]) for row in outcomes],
            "gaps": [json.loads(row["record_json"]) for row in gaps],
            "operating_intervals": [json.loads(row["record_json"]) for row in intervals],
            "faults": [json.loads(row["record_json"]) for row in faults],
        }

    def restore_engine(self, run_id: str,
                       factory: Callable[[dict[str, Any]], Any]) -> Any:
        run = self.run(run_id)
        engine = factory(run["strategy_package"])
        with self._lock:
            state = self.db.execute(
                """SELECT decision_checkpoint_json,
                          decision_checkpoint_received_at,
                          decision_checkpoint_evidence_id
                   FROM paper_runs WHERE id=?""",
                (run_id,),
            ).fetchone()
            checkpoint = (
                json.loads(state["decision_checkpoint_json"])
                if state and state["decision_checkpoint_json"] else None
            )
            after = (-1.0, "")
            if checkpoint is not None:
                _restore_decision_checkpoint(engine, checkpoint)
                after = (
                    float(state["decision_checkpoint_received_at"]),
                    str(state["decision_checkpoint_evidence_id"]),
                )
            pending_count = self.db.execute(
                """SELECT COUNT(*) FROM paper_observations
                   WHERE run_id=? AND quality_status='eligible'
                     AND (first_received_at>? OR
                          (first_received_at=? AND evidence_id>?))""",
                (run_id, after[0], after[0], after[1]),
            ).fetchone()[0]
            if pending_count > MAX_ENGINE_RESTORE_OBSERVATIONS:
                raise PaperResearchError(
                    "Paper decision restore exceeds its bounded checkpoint tail; resume after repair",
                    code="restore_limit",
                )
            observations = self.db.execute(
                """SELECT record_json FROM paper_observations
                   WHERE run_id=? AND quality_status='eligible'
                     AND (first_received_at>? OR
                          (first_received_at=? AND evidence_id>?))
                   ORDER BY first_received_at,evidence_id""",
                (run_id, after[0], after[0], after[1]),
            ).fetchall()
            engine.restore(
                json.loads(row["record_json"])["payload"] for row in observations
            )
            decisions = self.db.execute(
                """SELECT decision.record_json FROM paper_decisions AS decision
                   JOIN paper_observations AS observation
                     ON observation.run_id=decision.run_id
                    AND observation.evidence_id=decision.evidence_id
                   WHERE decision.run_id=?
                     AND (observation.first_received_at>? OR
                          (observation.first_received_at=? AND observation.evidence_id>?))
                   ORDER BY decision.decided_at,decision.decision_key""",
                (run_id, after[0], after[0], after[1]),
            ).fetchall()
            engine.restore_decisions(
                json.loads(row["record_json"])["decision"] for row in decisions
            )
        return engine

    def restore_portfolio_evaluator(self, run_id: str):
        with self._lock:
            row = self.db.execute(
                """SELECT payload_json,evaluator_checkpoint_json,
                          evaluator_checkpoint_received_at,
                          evaluator_checkpoint_evidence_id
                   FROM paper_runs WHERE id=?""",
                (_text(run_id, "run_id"),),
            ).fetchone()
            if row is None:
                raise PaperResearchError("Unknown paper run", code="not_found")
            run = json.loads(row["payload_json"])
            checkpoint = (json.loads(row["evaluator_checkpoint_json"])
                          if row["evaluator_checkpoint_json"] else None)
            evaluator = _portfolio_evaluator(run, checkpoint)
            after = (
                float(row["evaluator_checkpoint_received_at"]),
                str(row["evaluator_checkpoint_evidence_id"]),
            )
            tail = self.db.execute(
                """SELECT record_json FROM paper_observations
                   WHERE run_id=? AND quality_status='eligible'
                     AND (first_received_at>? OR
                          (first_received_at=? AND evidence_id>?))
                   ORDER BY first_received_at,evidence_id
                   LIMIT ?""",
                (run_id, after[0], after[0], after[1],
                 MAX_ENGINE_RESTORE_OBSERVATIONS + 1),
            ).fetchall()
            if len(tail) > MAX_ENGINE_RESTORE_OBSERVATIONS:
                raise PaperResearchError(
                    "Paper portfolio restore exceeds its bounded checkpoint tail",
                    code="restore_limit",
                )
            for observation_row in tail:
                observation = json.loads(observation_row["record_json"])
                evaluator.advance(_portfolio_quote_event(observation))
                decisions = self.db.execute(
                    """SELECT decision_key,mint,kind FROM paper_decisions
                       WHERE run_id=? AND evidence_id=?
                       ORDER BY decided_at,decision_key""",
                    (run_id, observation["evidence_id"]),
                ).fetchall()
                for decision in decisions:
                    if decision["kind"] == "entry_recommended":
                        evaluator.advance(_portfolio_entry_event(
                            run_id, decision["decision_key"], decision["mint"],
                            observation["evidence_id"],
                            observation["first_received_at"],
                        ))
        return evaluator

    def save_portfolio_checkpoint(self, run_id: str, evaluator: Any) -> None:
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT cursor_received_at,cursor_evidence_id FROM paper_runs WHERE id=?",
                (_text(run_id, "run_id"),),
            ).fetchone()
            if row is None:
                raise PaperResearchError("Unknown paper run", code="not_found")
            self.db.execute(
                """UPDATE paper_runs SET evaluator_checkpoint_json=?,
                   evaluator_report_json=?,evaluator_checkpoint_received_at=?,
                   evaluator_checkpoint_evidence_id=? WHERE id=?""",
                (canonical_json(evaluator.checkpoint()), canonical_json(evaluator.report()),
                 row["cursor_received_at"], row["cursor_evidence_id"], run_id),
            )

    def append_evidence(self, run_id: str, evidence: dict[str, Any],
                        engine: Any, portfolio_evaluator: Any | None = None) -> int:
        ident = _text(run_id, "run_id")
        if not isinstance(evidence, dict) or not isinstance(evidence.get("payload"), dict):
            raise PaperResearchError("Paper evidence is invalid")
        evidence_id = _text(evidence.get("id"), "evidence_id")
        receipt = _finite(evidence.get("first_received_at"), "first_received_at")
        event_at = _finite(evidence.get("observed_at"), "observed_at")
        source_at = _finite(evidence.get("source_at"), "source_at")
        if receipt < 0 or event_at < 0 or source_at < 0:
            raise PaperResearchError("Paper evidence times cannot be negative")
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            run = self.db.execute("SELECT * FROM paper_runs WHERE id=?", (ident,)).fetchone()
            if run is None:
                raise PaperResearchError("Unknown paper run", code="not_found")
            if run["state"] != "running":
                return 0
            reasons: list[str] = []
            repair = ""
            quote: MarketQuote | None = None
            quote_event: dict[str, Any] | None = None
            try:
                quote = MarketQuote.from_record(evidence["payload"])
            except (TypeError, ValueError) as exc:
                reasons.append("invalid-market-quote")
                repair = f"Collector must emit a complete normalized quote: {exc}"
            if receipt < event_at or receipt < source_at:
                reasons.append("invalid-evidence-time-order")
                repair = "Collect a quote whose event and source times do not follow first receipt."
            source_basis = evidence.get("source_time_basis", "unverified")
            if source_basis not in {
                "independent", "provider", "inferred-observed-at", "legacy-event-at",
            }:
                reasons.append("unverified-market-freshness")
                repair = "Collect provider or independently timestamped market evidence."
            if receipt < float(run["start_at"]):
                reasons.append("before-paper-start")
                repair = "This evidence remains provenance only; collect a new quote after paper start."
            if receipt < float(run["decision_not_before_at"] or run["start_at"]):
                reasons.append("outside-operating-interval")
                repair = "Collect a new quote while the resumed paper run is operating."
            if quote is not None:
                if quote.observed_at != event_at or (
                    quote.source_at and quote.source_at != source_at
                ):
                    reasons.append("normalized-quote-time-mismatch")
                    repair = "Repair collector normalization so payload and evidence times agree."
                max_age = float(parse_package(
                    json.loads(run["payload_json"])["strategy_package"]
                ).effective_rules.get("quote_max_age_seconds", 120))
                market_age = receipt - source_at
                if market_age > max_age:
                    reasons.append("stale-market-evidence")
                    repair = "Refresh market evidence from the provider before its strategy age limit."
                if quote.route not in {"curve", "amm"}:
                    reasons.append("unsupported-market-route")
                    repair = "Collect a supported curve or AMM quote with stable pool identity."
                try:
                    quote_event = _portfolio_quote_event({
                        "evidence_id": evidence_id,
                        "event_at": event_at,
                        "first_received_at": receipt,
                        "source_at": source_at,
                        "source_time_basis": source_basis,
                        "mint": quote.mint,
                        "route": quote.route,
                        "pool": quote.pool,
                        "decimals": quote.decimals,
                        "payload": evidence["payload"],
                    })
                except (TypeError, ValueError) as exc:
                    message = str(exc)
                    reasons.append(
                        "unsupported-price-precision"
                        if "price" in message.lower() else "unsupported-ledger-precision"
                    )
                    repair = message
            reasons = sorted(set(reasons))
            payload = json.loads(canonical_json(evidence["payload"]))
            observation = {
                "schema": OBSERVATION_SCHEMA,
                "run_id": ident,
                "evidence_id": evidence_id,
                "mint": quote.mint if quote is not None else str(
                    evidence.get("mint") or payload.get("mint") or "unknown"
                ),
                "route": quote.route if quote is not None else str(
                    evidence.get("route") or payload.get("route") or "unknown"
                ),
                "pool": quote.pool if quote is not None else str(
                    evidence.get("pool") or payload.get("pool") or "unknown"
                ),
                "decimals": quote.decimals if quote is not None else payload.get("decimals"),
                "event_at": event_at,
                "source_at": source_at,
                "source_time_basis": evidence.get("source_time_basis", "unverified"),
                "first_received_at": receipt,
                "first_known_basis": evidence.get("first_known_basis", "recorder-first-receipt"),
                "source_age_seconds": receipt - source_at,
                "event_age_seconds": receipt - event_at,
                "run_ids": list(evidence.get("run_ids", [])),
                "payload": payload,
                "quality": {
                    "status": "excluded" if reasons else "eligible",
                    "reasons": reasons,
                    "decision_eligible": not reasons,
                    "repair": repair or None,
                },
            }
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO paper_observations(
                       run_id,evidence_id,event_at,first_received_at,mint,record_json,
                       quality_status,exclusion_reason
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (ident, evidence_id, event_at, receipt, observation["mint"],
                 canonical_json(observation), observation["quality"]["status"],
                 ",".join(reasons)),
            )
            self._advance_cursor_locked(run, receipt, evidence_id)
            if cursor.rowcount == 0:
                return 0
            if reasons:
                return 1
            assert quote is not None and quote_event is not None
            if portfolio_evaluator is not None:
                portfolio_evaluator.advance(quote_event)
            # The decision timestamp is first receipt, never provider event
            # time. The evaluator gets only this newly available quote plus its
            # already-restored past state.
            decisions = engine.evaluate([quote], receipt, allow_entries=True)
            for decision in decisions:
                record = decision.as_record()
                record["at"] = receipt
                decision_key = _sha256({
                    "run_id": ident, "evidence_id": observation["evidence_id"],
                    "decision": record,
                })
                saved = {
                    "schema": DECISION_SCHEMA,
                    "run_id": ident,
                    "decision_key": decision_key,
                    "evidence_id": observation["evidence_id"],
                    "decided_at": receipt,
                    "decision": record,
                }
                self.db.execute(
                    """INSERT OR IGNORE INTO paper_decisions(
                           run_id,decision_key,evidence_id,decided_at,mint,kind,record_json
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (ident, decision_key, observation["evidence_id"], receipt,
                     decision.mint, decision.kind, canonical_json(saved)),
                )
                if portfolio_evaluator is not None and decision.kind == "entry_recommended":
                    # The durable decision row is inserted before the pure
                    # evaluator sees the signal. A later quote can therefore
                    # never score an outcome whose decision was absent.
                    portfolio_evaluator.advance(_portfolio_entry_event(
                        ident, decision_key, decision.mint,
                        observation["evidence_id"], receipt,
                    ))
        return 1

    def _advance_cursor_locked(self, run: sqlite3.Row, receipt: float,
                               evidence_id: str) -> None:
        previous = (float(run["cursor_received_at"]), str(run["cursor_evidence_id"]))
        if (receipt, evidence_id) > previous:
            self.db.execute(
                """UPDATE paper_runs SET cursor_received_at=?,cursor_evidence_id=?,
                   last_cycle_at=? WHERE id=?""",
                (receipt, evidence_id, float(self.clock()), run["id"]),
            )

    def score_ready(self, run_id: str, *, horizon_seconds: int = DEFAULT_OUTCOME_HORIZON,
                    known_by: float | None = None) -> int:
        ident = _text(run_id, "run_id")
        if isinstance(horizon_seconds, bool) or not isinstance(horizon_seconds, int) \
                or not 1 <= horizon_seconds <= 30 * 24 * 60 * 60:
            raise PaperResearchError("Paper outcome horizon is invalid")
        cutoff = _finite(self.clock() if known_by is None else known_by, "known_by")
        saved = 0
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            decisions = self.db.execute(
                """SELECT * FROM paper_decisions AS decision
                   WHERE run_id=? AND kind='entry_recommended'
                     AND decided_at+?<=?
                     AND NOT EXISTS (
                         SELECT 1 FROM paper_outcomes AS outcome
                         WHERE outcome.run_id=decision.run_id
                           AND outcome.decision_key=decision.decision_key
                           AND outcome.horizon_seconds=?
                     ) ORDER BY decided_at,decision_key""",
                (ident, horizon_seconds, cutoff, horizon_seconds),
            ).fetchall()
            for decision in decisions:
                source = self.db.execute(
                    """SELECT record_json FROM paper_observations
                       WHERE run_id=? AND evidence_id=?""",
                    (ident, decision["evidence_id"]),
                ).fetchone()
                later = self.db.execute(
                    """SELECT record_json FROM paper_observations
                       WHERE run_id=? AND mint=? AND first_received_at>=?
                         AND first_received_at<=? AND quality_status='eligible'
                       ORDER BY first_received_at,evidence_id LIMIT 1""",
                    (ident, decision["mint"],
                     float(decision["decided_at"]) + horizon_seconds, cutoff),
                ).fetchone()
                if later is None:
                    continue
                first_observation = json.loads(source["record_json"])
                last_observation = json.loads(later["record_json"])
                first_price = _positive_or_none(first_observation["payload"].get("price_usd"))
                last_price = _positive_or_none(last_observation["payload"].get("price_usd"))
                status = "scored" if first_price is not None and last_price is not None else "unpriced"
                result = (last_price / first_price - 1
                          if status == "scored" else None)
                outcome = {
                    "schema": OUTCOME_SCHEMA,
                    "run_id": ident,
                    "decision_key": decision["decision_key"],
                    "horizon_seconds": horizon_seconds,
                    "decided_at": decision["decided_at"],
                    "scored_at": last_observation["first_received_at"],
                    "status": status,
                    "modeled_return_fraction": result,
                    "entry_evidence_id": decision["evidence_id"],
                    "outcome_evidence_id": last_observation["evidence_id"],
                }
                self.db.execute(
                    "INSERT INTO paper_outcomes VALUES(?,?,?,?,?,?)",
                    (ident, decision["decision_key"], horizon_seconds,
                     outcome["scored_at"], status, canonical_json(outcome)),
                )
                saved += 1
        return saved

    def record_gap(self, run_id: str, *, gap_id: str, started_at: float,
                   ended_at: float | None, reason: str) -> None:
        ident = _text(run_id, "run_id")
        start = _finite(started_at, "gap start")
        end = None if ended_at is None else _finite(ended_at, "gap end")
        if end is not None and end < start:
            raise PaperResearchError("Paper gap end cannot precede its start")
        detail = _text(reason, "gap reason")[:500]
        record = {
            "schema": "pumpwizard.paper-gap/v1", "run_id": ident,
            "gap_id": _text(gap_id, "gap_id"), "started_at": start,
            "ended_at": end, "reason": detail,
        }
        with self._lock, self.db:
            self._upsert_gap_locked(
                ident, gap_id=record["gap_id"], started_at=start,
                ended_at=end, reason=detail,
            )

    def close_gaps(self, run_id: str, *, prefix: str, ended_at: float) -> int:
        ident = _text(run_id, "run_id")
        end = _finite(ended_at, "gap end")
        with self._lock, self.db:
            rows = self.db.execute(
                """SELECT * FROM paper_gaps
                   WHERE run_id=? AND gap_id LIKE ? AND ended_at IS NULL""",
                (ident, prefix + "%"),
            ).fetchall()
            for row in rows:
                self._upsert_gap_locked(
                    ident, gap_id=row["gap_id"],
                    started_at=float(row["started_at"]),
                    ended_at=max(float(row["started_at"]), end),
                    reason=row["reason"],
                )
        return len(rows)

    def record_fault(self, run_id: str, *, code: str, message: str,
                     evidence_id: str | None = None, retryable: bool = True) -> dict[str, Any]:
        ident = _text(run_id, "run_id")
        detail = _text(str(message)[:500], "fault message")
        fault_code = _text(str(code)[:120], "fault code")
        now = float(self.clock())
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            run = self.db.execute("SELECT * FROM paper_runs WHERE id=?", (ident,)).fetchone()
            if run is None:
                raise PaperResearchError("Unknown paper run", code="not_found")
            retry_count = min(
                int(run["max_retries"] or MAX_RUN_RETRIES),
                int(run["retry_count"] or 0) + 1,
            )
            can_retry = bool(retryable and retry_count < int(run["max_retries"] or MAX_RUN_RETRIES))
            fault_id = "fault:" + _sha256([
                ident, evidence_id, fault_code, detail, retry_count,
            ])[:24]
            record = {
                "schema": "pumpwizard.paper-fault/v1",
                "run_id": ident,
                "fault_id": fault_id,
                "occurred_at": now,
                "evidence_id": evidence_id,
                "code": fault_code,
                "message": detail,
                "retryable": can_retry,
                "retry_count": retry_count,
                "max_retries": int(run["max_retries"] or MAX_RUN_RETRIES),
                "repair": (
                    "Repair or quarantine the named evidence, then resume this run."
                    if can_retry else
                    "Retry limit reached; preserve this run and start a new run after repair."
                ),
            }
            self.db.execute(
                """INSERT OR IGNORE INTO paper_faults(
                       run_id,fault_id,occurred_at,evidence_id,code,message,
                       retryable,retry_count,record_json
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (ident, fault_id, now, evidence_id, fault_code, detail,
                 int(can_retry), retry_count, canonical_json(record)),
            )
            self._close_interval_locked(ident, now)
            self._upsert_gap_locked(
                ident, gap_id="fault-gap:" + fault_id, started_at=now,
                ended_at=None, reason="Paper decision processing interrupted by a persisted fault",
            )
            self.db.execute(
                """UPDATE paper_runs SET state='interrupted',interrupted_at=?,
                   retry_count=?,fault_json=?,decision_not_before_at=? WHERE id=?""",
                (now, retry_count, canonical_json(record), now, ident),
            )
        return record

    def save_engine_checkpoint(self, run_id: str, engine: Any) -> None:
        checkpoint = _decision_checkpoint(engine)
        if checkpoint is None:
            return
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT cursor_received_at,cursor_evidence_id FROM paper_runs WHERE id=?",
                (_text(run_id, "run_id"),),
            ).fetchone()
            if row is None:
                raise PaperResearchError("Unknown paper run", code="not_found")
            self.db.execute(
                """UPDATE paper_runs SET decision_checkpoint_json=?,
                   decision_checkpoint_received_at=?,decision_checkpoint_evidence_id=?
                   WHERE id=?""",
                (canonical_json(checkpoint), row["cursor_received_at"],
                 row["cursor_evidence_id"], run_id),
            )

    def _open_interval_locked(self, run_id: str, started_at: float, *,
                              reason: str, interval_id: str | None = None) -> None:
        ident = interval_id or "interval:" + _sha256([run_id, started_at, reason])[:24]
        record = {
            "schema": "pumpwizard.paper-operating-interval/v1",
            "run_id": run_id, "interval_id": ident,
            "started_at": started_at, "ended_at": None, "reason": reason,
        }
        self.db.execute(
            """INSERT OR IGNORE INTO paper_operating_intervals(
                   run_id,interval_id,started_at,ended_at,reason,record_json
               ) VALUES(?,?,?,?,?,?)""",
            (run_id, ident, started_at, None, reason, canonical_json(record)),
        )

    def _close_interval_locked(self, run_id: str, ended_at: float) -> None:
        rows = self.db.execute(
            """SELECT * FROM paper_operating_intervals
               WHERE run_id=? AND ended_at IS NULL ORDER BY started_at,interval_id""",
            (run_id,),
        ).fetchall()
        for row in rows:
            end = max(float(row["started_at"]), ended_at)
            record = json.loads(row["record_json"])
            record["ended_at"] = end
            self.db.execute(
                """UPDATE paper_operating_intervals SET ended_at=?,record_json=?
                   WHERE run_id=? AND interval_id=?""",
                (end, canonical_json(record), run_id, row["interval_id"]),
            )

    def _upsert_gap_locked(self, run_id: str, *, gap_id: str, started_at: float,
                           ended_at: float | None, reason: str) -> None:
        record = {
            "schema": "pumpwizard.paper-gap/v1", "run_id": run_id,
            "gap_id": gap_id, "started_at": started_at,
            "ended_at": ended_at, "reason": reason,
        }
        self.db.execute(
            """INSERT INTO paper_gaps(run_id,gap_id,started_at,ended_at,reason,record_json)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(run_id,gap_id) DO UPDATE SET
                   ended_at=excluded.ended_at,reason=excluded.reason,
                   record_json=excluded.record_json""",
            (run_id, gap_id, started_at, ended_at, reason, canonical_json(record)),
        )

    def _close_open_gaps_locked(self, run_id: str, ended_at: float, *,
                                prefixes: tuple[str, ...] | None = None) -> None:
        rows = self.db.execute(
            "SELECT * FROM paper_gaps WHERE run_id=? AND ended_at IS NULL",
            (run_id,),
        ).fetchall()
        for row in rows:
            if prefixes is not None and not str(row["gap_id"]).startswith(prefixes):
                continue
            self._upsert_gap_locked(
                run_id, gap_id=row["gap_id"], started_at=float(row["started_at"]),
                ended_at=max(float(row["started_at"]), ended_at), reason=row["reason"],
            )

    def _overview_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        counts = self.db.execute(
            """SELECT
                (SELECT COUNT(*) FROM paper_observations WHERE run_id=?) observations,
                (SELECT COUNT(*) FROM paper_decisions WHERE run_id=?) decisions,
                (SELECT COUNT(*) FROM paper_outcomes WHERE run_id=?) outcomes,
                (SELECT COUNT(*) FROM paper_gaps WHERE run_id=?) gaps,
                (SELECT COUNT(*) FROM paper_operating_intervals WHERE run_id=?) operating_intervals,
                (SELECT COUNT(*) FROM paper_faults WHERE run_id=?) faults,
                (SELECT COUNT(*) FROM paper_observations WHERE run_id=? AND quality_status='excluded') exclusions""",
            (row["id"], row["id"], row["id"], row["id"], row["id"], row["id"], row["id"]),
        ).fetchone()
        return {
            **payload,
            "state": row["state"],
            "cursor": [row["cursor_received_at"], row["cursor_evidence_id"]],
            "created_at": row["created_at"], "stopped_at": row["stopped_at"],
            "last_cycle_at": row["last_cycle_at"],
            "counts": dict(counts),
            "limits": {"observations_per_cycle": MAX_OBSERVATIONS_PER_CYCLE},
            "decision_not_before_at": row["decision_not_before_at"],
            "retry": {
                "count": int(row["retry_count"] or 0),
                "maximum": int(row["max_retries"] or MAX_RUN_RETRIES),
                "next_at": row["next_retry_at"],
            },
            "fault": json.loads(row["fault_json"]) if row["fault_json"] else None,
            "evaluator_report": (
                json.loads(row["evaluator_report_json"])
                if row["evaluator_report_json"] else None
            ),
        }

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> "PaperResearchStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class PaperResearchWorker:
    """Ingest recorder evidence for active runs in bounded service cycles."""

    def __init__(self, data_dir: str | Path, recorder: ServiceEvidenceRecorder,
                 evaluator_factory: Callable[[dict[str, Any]], Any] | None = None,
                 *, clock=time.time):
        self.store = PaperResearchStore(data_dir, clock=clock)
        self.recorder = recorder
        self.clock = clock
        self.evaluator_factory = evaluator_factory or _decision_engine
        self.use_portfolio_evaluator = evaluator_factory is None

    def run_once(self, *, should_stop: Callable[[], bool] | None = None) -> dict[str, int]:
        cancelled = should_stop or (lambda: False)
        totals = {
            "runs": 0, "observations": 0, "outcomes": 0,
            "gaps": 0, "faults": 0,
        }
        now = float(self.clock())
        recent_coverage = self.recorder.coverage(500)
        for row in self.store.active_rows():
            if cancelled():
                break
            totals["runs"] += 1
            run_id = str(row["id"])
            current_evidence_id: str | None = None
            try:
                engine = self.store.restore_engine(run_id, self.evaluator_factory)
                portfolio_evaluator = (
                    self.store.restore_portfolio_evaluator(run_id)
                    if self.use_portfolio_evaluator else None
                )
                cursor = (float(row["cursor_received_at"]), str(row["cursor_evidence_id"]))
                run_payload = json.loads(row["payload_json"])
                page = self.recorder.evidence_page(
                    after=cursor if cursor[0] >= 0 else None,
                    limit=MAX_OBSERVATIONS_PER_CYCLE,
                    known_by=now,
                    campaign_id=run_payload.get("campaign_id"),
                )
                for evidence in page["records"]:
                    if cancelled():
                        break
                    current_evidence_id = str(evidence.get("id") or "")
                    totals["observations"] += self.store.append_evidence(
                        run_id, evidence, engine, portfolio_evaluator,
                    )
                self.store.save_engine_checkpoint(run_id, engine)
                if portfolio_evaluator is not None:
                    self.store.save_portfolio_checkpoint(run_id, portfolio_evaluator)
                if page["next_cursor"] is not None and not cancelled():
                    gap_id = "input-cap:" + _sha256({
                        "run_id": run_id, "cursor": page["next_cursor"], "known_by": now,
                    })[:24]
                    self.store.record_gap(
                        run_id, gap_id=gap_id, started_at=now, ended_at=None,
                        reason=(f"Input exceeded the {MAX_OBSERVATIONS_PER_CYCLE}-row cycle cap; "
                                "remaining evidence will resume next cycle"),
                    )
                    totals["gaps"] += 1
                elif not cancelled():
                    self.store.close_gaps(run_id, prefix="input-cap:", ended_at=now)
                for coverage in recent_coverage:
                    if (coverage["status"] == "gap"
                            and float(coverage["started_at"]) >= float(row["start_at"])):
                        self.store.record_gap(
                            run_id, gap_id="collection:" + str(coverage["id"]),
                            started_at=float(coverage["started_at"]),
                            ended_at=(float(coverage["received_at"])
                                      if coverage["received_at"] is not None else None),
                            reason=str(coverage["gap_reason"]),
                        )
                if self.use_portfolio_evaluator:
                    report = self.store.run(run_id).get("evaluator_report") or {}
                    totals["outcomes"] += sum(
                        item.get("outcome_status") == "closed"
                        for item in report.get("decisions", [])
                    )
                else:
                    totals["outcomes"] += self.store.score_ready(run_id, known_by=now)
            except Exception as exc:
                self.store.record_fault(
                    run_id, code="paper-cycle-fault", message=str(exc),
                    evidence_id=current_evidence_id or None, retryable=True,
                )
                totals["faults"] += 1
        return totals

    def close(self) -> None:
        self.store.close()


def _decision_engine(package: dict[str, Any]) -> DecisionEngine:
    return DecisionEngine(parse_package(json.loads(canonical_json(package))))


def _portfolio_evaluator(run: dict[str, Any], checkpoint: dict[str, Any] | None = None):
    """Build Worker 2's pure evaluator without exposing any service object."""
    from .paper_evaluation import IncrementalPaperEvaluator
    from .research_replay import _costs, _execution, _policy

    strategy = parse_package(json.loads(canonical_json(run["strategy_package"])))
    experiment = {
        "id": run["id"],
        "capital": run["simulated_capital"],
        "cost_model": {},
    }
    policy = _policy(strategy, experiment)
    costs, _adverse, _stress = _costs(strategy, experiment)
    execution = _execution(strategy, experiment, [], [])
    return IncrementalPaperEvaluator(
        run_id=run["id"], strategy_sha256=run["strategy_sha256"],
        policy=policy, costs=costs, execution=execution, checkpoint=checkpoint,
    )


def _portfolio_quote_event(observation: dict[str, Any]) -> dict[str, Any]:
    from .portfolio_replay import quote_amount_micros, quote_price_fields

    quote = MarketQuote.from_record(observation["payload"])
    cap = quote_amount_micros(quote.market_cap_usd, "market_cap_usd")
    liquidity = quote_amount_micros(quote.liquidity_usd, "liquidity_usd")
    if cap < 1:
        raise PaperResearchError(
            "market_cap_usd is below the supported 1e-6 quote-currency ledger precision",
            code="unsupported_precision",
        )
    if liquidity < 1:
        raise PaperResearchError(
            "liquidity_usd is below the supported 1e-6 quote-currency ledger precision",
            code="unsupported_precision",
        )
    return {
        "id": observation["evidence_id"], "kind": "quote",
        "event_at": observation["event_at"],
        "first_known_at": observation["first_received_at"],
        "mint": observation["mint"], "route": observation["route"],
        "pool": observation["pool"], "decimals": observation["decimals"],
        "quote_currency": "USD",
        "market_cap_micros": cap,
        "liquidity_micros": liquidity,
        "source_at": observation["source_at"],
        "source_time_basis": observation["source_time_basis"],
        **quote_price_fields(quote.price_usd, "price_usd"),
    }


def _portfolio_entry_event(run_id: str, decision_key: str, mint: str,
                           evidence_id: str, decided_at: float) -> dict[str, Any]:
    return {
        "id": decision_key,
        "kind": "entry_signal",
        "event_at": decided_at,
        "first_known_at": decided_at,
        "mint": mint,
        "evidence_ids": [evidence_id],
        "episode_id": f"paper:{run_id}:{decision_key}",
        "dependence_group": f"mint:{mint}",
        "split": "paper",
    }


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise PaperResearchError(f"{label} must be non-empty bounded text")
    return value.strip()


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaperResearchError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise PaperResearchError(f"{label} must be a finite number")
    return result


def _positive_or_none(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _request_sha256(run: dict[str, Any]) -> str:
    return _sha256({
        "source_session_id": run["source_session_id"],
        "strategy_sha256": run["strategy_sha256"],
        "strategy_package": run["strategy_package"],
        "simulated_capital": run["simulated_capital"],
        "evaluator": run.get("evaluator", "pumpwizard-paper-decision-engine/v1"),
        "outcome_horizon_seconds": run.get(
            "outcome_horizon_seconds", DEFAULT_OUTCOME_HORIZON,
        ),
    })


def _decision_checkpoint(engine: Any) -> dict[str, Any] | None:
    checkpoint = getattr(engine, "checkpoint", None)
    histories = getattr(engine, "histories", None)
    if not callable(checkpoint) or not isinstance(histories, dict):
        return None
    rows = [
        quote.as_record()
        for mint in sorted(histories)
        for quote in histories[mint]
    ]
    if len(rows) > MAX_ENGINE_STATE_OBSERVATIONS:
        raise PaperResearchError(
            f"Paper decision state exceeded {MAX_ENGINE_STATE_OBSERVATIONS} retained quotes",
            code="engine_state_limit",
        )
    return {
        "schema": "pumpwizard.paper-decision-checkpoint/v1",
        "engine": checkpoint(),
        "history": rows,
        "history_count": len(rows),
        "limit": MAX_ENGINE_STATE_OBSERVATIONS,
    }


def _restore_decision_checkpoint(engine: Any, value: dict[str, Any]) -> None:
    if (not isinstance(value, dict)
            or value.get("schema") != "pumpwizard.paper-decision-checkpoint/v1"
            or not isinstance(value.get("engine"), dict)
            or not isinstance(value.get("history"), list)
            or len(value["history"]) > MAX_ENGINE_STATE_OBSERVATIONS):
        raise PaperResearchError(
            "Paper decision checkpoint is invalid or exceeds its resource bound",
            code="invalid_checkpoint",
        )
    restore_checkpoint = getattr(engine, "restore_checkpoint", None)
    if not callable(restore_checkpoint):
        raise PaperResearchError(
            "Paper decision engine cannot restore its durable checkpoint",
            code="invalid_checkpoint",
        )
    restore_checkpoint(value["engine"])
    engine.restore(value["history"])


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _limit(value: int, *, maximum: int = 500) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise PaperResearchError(f"Paper list limit must be between 1 and {maximum}")
