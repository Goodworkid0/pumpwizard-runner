"""Durable bounded forward-observation campaigns with no trading authority."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Any

from .paper_research import (
    DEFAULT_OUTCOME_HORIZON,
    MAX_RUN_RETRIES,
    PaperResearchStore,
    RUN_SCHEMA,
    _portfolio_evaluator,
)
from .service_recorder import ServiceEvidenceRecorder
from .strategies import canonical_json, parse_package


SCHEMA_VERSION = 2
CAMPAIGN_SCHEMA = "pumpwizard.forward-campaign/v1"
REQUEST_SCHEMA = "pumpwizard.forward-campaign-request/v1"
EVENT_SCHEMA = "pumpwizard.forward-campaign-event/v1"
BLOCKER_SCHEMA = "pumpwizard.forward-campaign-blocker/v1"
PRESET_SCHEMA = "pumpwizard.forward-campaign-preset/v1"
RESOURCE_MODEL_SCHEMA = "pumpwizard.observation-resource-model/v1"
DRY_RUN_SCHEMA = "pumpwizard.observation-dry-run/v1"
START_CONFIRMATION = "START FORWARD CAMPAIGN"
MAX_ARMS = 3
MAX_SHARED_CONCURRENCY = 4
MAX_HISTORY_LIMIT = 500


class ForwardCampaignError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_forward_campaign"):
        super().__init__(message)
        self.code = code


class ForwardCampaignStore:
    """Campaign state and paper arms sharing one SQLite transaction."""

    def __init__(self, data_dir: str | Path, *, clock=time.time):
        self.root = Path(data_dir)
        self.clock = clock
        self.paper = PaperResearchStore(self.root, clock=clock)
        self.db = self.paper.db
        self._lock = self.paper._lock
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS forward_campaigns (
                    id TEXT PRIMARY KEY,
                    campaign_key TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version > 0),
                    supersedes_campaign_id TEXT REFERENCES forward_campaigns(id),
                    state TEXT NOT NULL CHECK(state IN (
                        'running','paused','degraded','capped','completed','stopped'
                    )),
                    start_at REAL NOT NULL,
                    deadline_at REAL NOT NULL,
                    next_scan_at REAL NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0 CHECK(request_count >= 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    stopped_at REAL,
                    payload_json TEXT NOT NULL,
                    blocker_json TEXT,
                    baseline_storage_bytes INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS forward_campaign_recent
                    ON forward_campaigns(created_at DESC,id DESC);
                CREATE INDEX IF NOT EXISTS forward_campaign_state
                    ON forward_campaigns(state,next_scan_at,id);
                CREATE TABLE IF NOT EXISTS forward_campaign_deliveries (
                    idempotency_key TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES forward_campaigns(id),
                    request_sha256 TEXT NOT NULL,
                    delivered_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forward_campaign_commands (
                    idempotency_key TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES forward_campaigns(id),
                    action TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forward_campaign_arms (
                    campaign_id TEXT NOT NULL REFERENCES forward_campaigns(id),
                    arm_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    paper_run_id TEXT NOT NULL UNIQUE REFERENCES paper_runs(id),
                    source_session_id TEXT NOT NULL,
                    strategy_sha256 TEXT NOT NULL,
                    simulated_capital_usd REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(campaign_id,arm_id),
                    UNIQUE(campaign_id,ordinal)
                );
                CREATE TABLE IF NOT EXISTS forward_campaign_events (
                    campaign_id TEXT NOT NULL REFERENCES forward_campaigns(id),
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    occurred_at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(campaign_id,sequence)
                );
                CREATE INDEX IF NOT EXISTS forward_campaign_event_time
                    ON forward_campaign_events(campaign_id,occurred_at,event_id);
                CREATE TABLE IF NOT EXISTS forward_campaign_intervals (
                    campaign_id TEXT NOT NULL REFERENCES forward_campaigns(id),
                    interval_id TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(campaign_id,interval_id)
                );
                CREATE TABLE IF NOT EXISTS forward_campaign_reports (
                    campaign_id TEXT NOT NULL REFERENCES forward_campaigns(id),
                    report_id TEXT NOT NULL UNIQUE,
                    previous_report_id TEXT,
                    created_at REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(campaign_id,report_id)
                );
                CREATE TABLE IF NOT EXISTS forward_observation_presets (
                    id TEXT PRIMARY KEY,
                    campaign_key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    identity_sha256 TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    prepared_at REAL NOT NULL,
                    request_json TEXT NOT NULL,
                    resource_model_json TEXT NOT NULL,
                    estimate_json TEXT NOT NULL,
                    UNIQUE(campaign_key,revision)
                );
                CREATE INDEX IF NOT EXISTS forward_observation_preset_identity
                    ON forward_observation_presets(campaign_key,identity_sha256);
                CREATE TABLE IF NOT EXISTS forward_observation_preset_deliveries (
                    idempotency_key TEXT PRIMARY KEY,
                    preset_id TEXT NOT NULL REFERENCES forward_observation_presets(id),
                    identity_sha256 TEXT NOT NULL,
                    delivered_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS forward_campaign_presets (
                    campaign_id TEXT PRIMARY KEY REFERENCES forward_campaigns(id),
                    preset_id TEXT NOT NULL REFERENCES forward_observation_presets(id)
                );
                CREATE TABLE IF NOT EXISTS forward_campaign_evaluation_cycles (
                    campaign_id TEXT NOT NULL REFERENCES forward_campaigns(id),
                    cycle_id TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL NOT NULL,
                    through_received_at REAL,
                    observations INTEGER NOT NULL CHECK(observations >= 0),
                    decisions INTEGER NOT NULL CHECK(decisions >= 0),
                    outcomes INTEGER NOT NULL CHECK(outcomes >= 0),
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(campaign_id,cycle_id)
                );
                CREATE INDEX IF NOT EXISTS forward_campaign_cycle_time
                    ON forward_campaign_evaluation_cycles(campaign_id,finished_at,cycle_id);
            """)
            campaign_columns = {
                item["name"] for item in self.db.execute("PRAGMA table_info(forward_campaigns)")
            }
            if "baseline_storage_bytes" not in campaign_columns:
                self.db.execute(
                    """ALTER TABLE forward_campaigns ADD COLUMN baseline_storage_bytes
                       INTEGER NOT NULL DEFAULT 0"""
                )
            row = self.db.execute(
                "SELECT value FROM meta WHERE key='forward_campaign_schema_version'"
            ).fetchone()
            if row is not None and int(row["value"]) > SCHEMA_VERSION:
                raise ForwardCampaignError(
                    "Forward campaigns were created by a newer PumpWizard version",
                    code="unsupported_schema",
                )
            self.db.execute(
                """INSERT INTO meta(key,value) VALUES('forward_campaign_schema_version',?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(SCHEMA_VERSION),),
            )

    def preregister(self, request: dict[str, Any], resource_model: dict[str, Any], *,
                    idempotency_key: str) -> dict[str, Any]:
        """Freeze a campaign manifest and its resource assumptions without I/O."""
        manifest = _manifest(request)
        normalized_request = {"schema": REQUEST_SCHEMA, **manifest}
        resources = _resource_model(resource_model)
        key = _text(idempotency_key, "idempotency_key", maximum=160)
        request_sha = _sha256(manifest)
        identity_sha = _sha256({"request": normalized_request, "resource_model": resources})
        now = _timestamp(self.clock(), "preset preparation time")
        estimate = _dry_run_estimate(manifest, resources, identity_sha)
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            delivered = self.db.execute(
                """SELECT preset.* FROM forward_observation_preset_deliveries AS delivery
                   JOIN forward_observation_presets AS preset ON preset.id=delivery.preset_id
                   WHERE delivery.idempotency_key=?""",
                (key,),
            ).fetchone()
            if delivered is not None:
                if delivered["identity_sha256"] != identity_sha:
                    raise ForwardCampaignError(
                        "Preset idempotency key belongs to different frozen inputs",
                        code="identity_conflict",
                    )
                return self._preset_row(delivered)
            existing = self.db.execute(
                """SELECT * FROM forward_observation_presets
                   WHERE campaign_key=? AND identity_sha256=?
                   ORDER BY revision DESC LIMIT 1""",
                (manifest["campaign_key"], identity_sha),
            ).fetchone()
            if existing is None:
                revision = int(self.db.execute(
                    """SELECT COALESCE(MAX(revision),0)+1
                       FROM forward_observation_presets WHERE campaign_key=?""",
                    (manifest["campaign_key"],),
                ).fetchone()[0])
                preset_id = "observation-preset-" + identity_sha[:32]
                existing_id = self.db.execute(
                    "SELECT identity_sha256 FROM forward_observation_presets WHERE id=?",
                    (preset_id,),
                ).fetchone()
                if existing_id is not None and existing_id["identity_sha256"] != identity_sha:
                    raise ForwardCampaignError("Observation preset identity collision")
                self.db.execute(
                    """INSERT INTO forward_observation_presets(
                           id,campaign_key,request_sha256,identity_sha256,revision,
                           prepared_at,request_json,resource_model_json,estimate_json
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (preset_id, manifest["campaign_key"], request_sha, identity_sha,
                     revision, now, canonical_json(normalized_request),
                     canonical_json(resources), canonical_json(estimate)),
                )
                existing = self.db.execute(
                    "SELECT * FROM forward_observation_presets WHERE id=?", (preset_id,)
                ).fetchone()
            self.db.execute(
                "INSERT INTO forward_observation_preset_deliveries VALUES(?,?,?,?)",
                (key, existing["id"], identity_sha, now),
            )
            return self._preset_row(existing)

    def preset(self, preset_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM forward_observation_presets WHERE id=?",
                (_text(preset_id, "preset_id"),),
            ).fetchone()
        if row is None:
            raise ForwardCampaignError("Unknown observation preset", code="not_found")
        return self._preset_row(row)

    def presets(self, *, campaign_key: str | None = None,
                limit: int = 100) -> list[dict[str, Any]]:
        _limit(limit)
        with self._lock:
            if campaign_key is None:
                rows = self.db.execute(
                    """SELECT * FROM forward_observation_presets
                       ORDER BY prepared_at DESC,id DESC LIMIT ?""", (limit,),
                ).fetchall()
            else:
                rows = self.db.execute(
                    """SELECT * FROM forward_observation_presets WHERE campaign_key=?
                       ORDER BY revision DESC LIMIT ?""",
                    (_text(campaign_key, "campaign_key"), limit),
                ).fetchall()
        return [self._preset_row(row) for row in rows]

    def start_preregistered(self, preset_id: str, *, idempotency_key: str,
                            confirmation: str) -> dict[str, Any]:
        preset = self.preset(preset_id)
        campaign = self.start(
            preset["request"], idempotency_key=idempotency_key,
            confirmation=confirmation,
        )
        with self._lock, self.db:
            prior = self.db.execute(
                "SELECT preset_id FROM forward_campaign_presets WHERE campaign_id=?",
                (campaign["id"],),
            ).fetchone()
            if prior is not None and prior["preset_id"] != preset["id"]:
                raise ForwardCampaignError(
                    "Campaign is already linked to a different resource preset",
                    code="identity_conflict",
                )
            self.db.execute(
                "INSERT OR IGNORE INTO forward_campaign_presets VALUES(?,?)",
                (campaign["id"], preset["id"]),
            )
        return self.campaign(campaign["id"])

    def record_evaluation_cycle(self, campaign_id: str, *, cycle_id: str,
                                started_at: float, finished_at: float,
                                through_received_at: float | None,
                                observations: int, decisions: int,
                                outcomes: int) -> dict[str, Any]:
        """Persist actual evaluator timing; callers must record real cycle boundaries."""
        ident = _text(campaign_id, "campaign_id")
        cycle = _text(cycle_id, "cycle_id")
        started = _timestamp(started_at, "evaluation start time")
        finished = _timestamp(finished_at, "evaluation finish time")
        if finished < started:
            raise ForwardCampaignError("Evaluation finish precedes its start")
        through = None if through_received_at is None else _timestamp(
            through_received_at, "evaluation receipt watermark"
        )
        counts = {
            "observations": _integer(observations, "evaluation observations", 0, 100_000_000),
            "decisions": _integer(decisions, "evaluation decisions", 0, 100_000_000),
            "outcomes": _integer(outcomes, "evaluation outcomes", 0, 100_000_000),
        }
        value = {
            "schema": "pumpwizard.forward-campaign-evaluation-cycle/v1",
            "campaign_id": ident, "id": cycle, "started_at": started,
            "finished_at": finished, "through_received_at": through, **counts,
            "processing_lag_seconds": (
                None if through is None else max(0.0, finished - through)
            ),
        }
        encoded = canonical_json(value)
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute(
                "SELECT 1 FROM forward_campaigns WHERE id=?", (ident,)
            ).fetchone() is None:
                raise ForwardCampaignError("Unknown forward campaign", code="not_found")
            prior = self.db.execute(
                """SELECT payload_json FROM forward_campaign_evaluation_cycles
                   WHERE campaign_id=? AND cycle_id=?""", (ident, cycle),
            ).fetchone()
            if prior is not None and prior["payload_json"] != encoded:
                raise ForwardCampaignError(
                    "Evaluation cycle identity belongs to different measurements",
                    code="identity_conflict",
                )
            self.db.execute(
                """INSERT OR IGNORE INTO forward_campaign_evaluation_cycles(
                       campaign_id,cycle_id,started_at,finished_at,through_received_at,
                       observations,decisions,outcomes,payload_json
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (ident, cycle, started, finished, through, counts["observations"],
                 counts["decisions"], counts["outcomes"], encoded),
            )
        return value

    def evaluation_snapshot(self, campaign_id: str) -> dict[str, Any]:
        """Return factual evaluator counters/watermark for service-cycle deltas."""
        ident = _text(campaign_id, "campaign_id")
        with self._lock:
            rows = self.db.execute(
                """SELECT arm.arm_id,run.cursor_received_at,run.evaluator_report_json,
                          (SELECT COUNT(*) FROM paper_observations
                           WHERE run_id=run.id) observations,
                          (SELECT COUNT(*) FROM paper_decisions
                           WHERE run_id=run.id) decisions,
                          (SELECT COUNT(*) FROM paper_outcomes
                           WHERE run_id=run.id) outcomes
                   FROM forward_campaign_arms AS arm
                   JOIN paper_runs AS run ON run.id=arm.paper_run_id
                   WHERE arm.campaign_id=? ORDER BY arm.ordinal""", (ident,),
            ).fetchall()
            exists = self.db.execute(
                "SELECT 1 FROM forward_campaigns WHERE id=?", (ident,)
            ).fetchone()
        if not rows and exists is None:
            raise ForwardCampaignError("Unknown forward campaign", code="not_found")
        arms = []
        for row in rows:
            report = json.loads(row["evaluator_report_json"] or "{}")
            portfolio_outcomes = sum(
                item.get("outcome_status") == "closed"
                for item in report.get("decisions", [])
            )
            arms.append({
                "id": row["arm_id"], "observations": int(row["observations"]),
                "decisions": int(row["decisions"]),
                "outcomes": max(int(row["outcomes"]), portfolio_outcomes),
                "through_received_at": (
                    None if float(row["cursor_received_at"]) < 0
                    else float(row["cursor_received_at"])
                ),
            })
        watermarks = [
            float(row["through_received_at"]) for row in arms
            if row["through_received_at"] is not None
        ]
        return {
            "campaign_id": ident,
            "observations": sum(row["observations"] for row in arms),
            "decisions": sum(row["decisions"] for row in arms),
            "outcomes": sum(row["outcomes"] for row in arms),
            "through_received_at": min(watermarks) if len(watermarks) == len(arms) else None,
            "arms": arms,
        }

    @staticmethod
    def _preset_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema": PRESET_SCHEMA, "id": row["id"],
            "campaign_key": row["campaign_key"], "revision": row["revision"],
            "prepared_at": row["prepared_at"],
            "request_sha256": row["request_sha256"],
            "identity_sha256": row["identity_sha256"],
            "request": json.loads(row["request_json"]),
            "resource_model": json.loads(row["resource_model_json"]),
            "dry_run": json.loads(row["estimate_json"]),
            "network_reads": 0,
        }

    def start(self, request: dict[str, Any], *, idempotency_key: str,
              confirmation: str) -> dict[str, Any]:
        if confirmation != START_CONFIRMATION:
            raise ForwardCampaignError(
                f"Starting a campaign requires exact confirmation: {START_CONFIRMATION}"
            )
        manifest = _manifest(request)
        key = _text(idempotency_key, "idempotency_key", maximum=160)
        digest = _sha256(manifest)
        now = _timestamp(self.clock(), "campaign start time")
        baseline_storage = _physical_storage_bytes(self.root)
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                """SELECT campaign.* FROM forward_campaign_deliveries AS delivery
                   JOIN forward_campaigns AS campaign ON campaign.id=delivery.campaign_id
                   WHERE delivery.idempotency_key=?""",
                (key,),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != digest:
                    raise ForwardCampaignError(
                        "Campaign idempotency key belongs to a different immutable manifest",
                        code="identity_conflict",
                    )
                return self._view_row(existing)
            parent_id = manifest["supersedes_campaign_id"]
            version = 1
            if parent_id is not None:
                parent = self.db.execute(
                    "SELECT version FROM forward_campaigns WHERE id=?", (parent_id,)
                ).fetchone()
                if parent is None:
                    raise ForwardCampaignError("Unknown superseded campaign", code="not_found")
                version = int(parent["version"]) + 1
            campaign_id = "campaign-" + digest[:32]
            collision = self.db.execute(
                "SELECT request_sha256 FROM forward_campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
            if collision is not None:
                if collision["request_sha256"] != digest:
                    raise ForwardCampaignError("Campaign identity collision")
                self.db.execute(
                    "INSERT INTO forward_campaign_deliveries VALUES(?,?,?,?)",
                    (key, campaign_id, digest, now),
                )
                return self._view_row(self.db.execute(
                    "SELECT * FROM forward_campaigns WHERE id=?", (campaign_id,)
                ).fetchone())
            duration = float(manifest["duration_seconds"])
            payload = {
                "schema": CAMPAIGN_SCHEMA,
                "id": campaign_id,
                "request_sha256": digest,
                "version": version,
                **manifest,
                "start_at": now,
                "deadline_at": now + duration,
                "authority": {
                    "wallet_allocation": False, "signer": False,
                    "orders": False, "submission": False,
                    "provider_purchase": False,
                },
            }
            self.db.execute(
                """INSERT INTO forward_campaigns(
                       id,campaign_key,idempotency_key,request_sha256,version,
                       supersedes_campaign_id,state,start_at,deadline_at,next_scan_at,
                       created_at,updated_at,payload_json,baseline_storage_bytes
                   ) VALUES(?,?,?,?,?,?,'running',?,?,?,?,?,?,?)""",
                (campaign_id, manifest["campaign_key"], key, digest, version, parent_id,
                 now, now + duration, now, now, now, canonical_json(payload), baseline_storage),
            )
            for ordinal, arm in enumerate(manifest["arms"], start=1):
                self._create_arm_locked(campaign_id, digest, arm, ordinal, now)
            self.db.execute(
                "INSERT INTO forward_campaign_deliveries VALUES(?,?,?,?)",
                (key, campaign_id, digest, now),
            )
            self._open_interval_locked(campaign_id, now, "campaign-start")
            self._event_locked(campaign_id, "started", now, {
                "request_sha256": digest, "arm_count": len(manifest["arms"]),
                "confirmation": START_CONFIRMATION,
            })
            row = self.db.execute(
                "SELECT * FROM forward_campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
        return self._view_row(row)

    def _create_arm_locked(self, campaign_id: str, manifest_sha: str,
                           arm: dict[str, Any], ordinal: int, now: float) -> None:
        request_core = {
            "source_session_id": arm["source_session_id"],
            "strategy_sha256": arm["strategy_sha256"],
            "strategy_package": arm["strategy_package"],
            "simulated_capital": {"amount": arm["simulated_capital_usd"], "unit": "USD"},
            "evaluator": "pumpwizard-paper-decision-engine/v1",
            "outcome_horizon_seconds": DEFAULT_OUTCOME_HORIZON,
            "campaign_id": campaign_id,
            "campaign_arm_id": arm["id"],
            "campaign_manifest_sha256": manifest_sha,
        }
        request_sha = _sha256(request_core)
        run = {
            "schema": RUN_SCHEMA,
            "id": "paper-" + request_sha[:24] + "-" + uuid.uuid4().hex[:8],
            "request_sha256": request_sha,
            "content_sha256": _sha256({**request_core, "start_at": now}),
            "state": "running", **request_core, "start_at": now,
            "authority": {
                "wallet_allocation": False, "signer": False,
                "orders": False, "paid_provider": False,
            },
        }
        evaluator = _portfolio_evaluator(run)
        self.db.execute(
            """INSERT INTO paper_runs(
                   id,idempotency_key,source_session_id,strategy_sha256,state,
                   start_at,created_at,payload_json,evaluator_checkpoint_json,
                   evaluator_report_json,request_sha256,decision_not_before_at,max_retries
               ) VALUES(?,?,?,?,'running',?,?,?,?,?,?,?,?)""",
            (run["id"], f"campaign:{campaign_id}:{arm['id']}",
             arm["source_session_id"], arm["strategy_sha256"], now, now,
             canonical_json(run), canonical_json(evaluator.checkpoint()),
             canonical_json(evaluator.report()), request_sha, now, MAX_RUN_RETRIES),
        )
        self.paper._open_interval_locked(
            run["id"], now, reason="forward-campaign-start",
            interval_id="campaign-start:" + campaign_id + ":" + arm["id"],
        )
        arm_payload = {
            "schema": "pumpwizard.forward-campaign-arm/v1",
            "campaign_id": campaign_id, "id": arm["id"], "ordinal": ordinal,
            "paper_run_id": run["id"], "source_session_id": arm["source_session_id"],
            "strategy_sha256": arm["strategy_sha256"],
            "simulated_capital_usd": arm["simulated_capital_usd"],
        }
        self.db.execute(
            "INSERT INTO forward_campaign_arms VALUES(?,?,?,?,?,?,?,?)",
            (campaign_id, arm["id"], ordinal, run["id"], arm["source_session_id"],
             arm["strategy_sha256"], arm["simulated_capital_usd"],
             canonical_json(arm_payload)),
        )

    def campaign(self, campaign_id: str,
                 recorder: ServiceEvidenceRecorder | None = None) -> dict[str, Any]:
        ident = _text(campaign_id, "campaign_id")
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM forward_campaigns WHERE id=?", (ident,)
            ).fetchone()
        if row is None:
            raise ForwardCampaignError("Unknown forward campaign", code="not_found")
        return self._view_row(row, recorder=recorder)

    def recover_running(self) -> int:
        """Pause campaigns after process restart; missed time is never backfilled."""
        now = _timestamp(self.clock(), "campaign recovery time")
        recovered = 0
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                """SELECT * FROM forward_campaigns
                   WHERE state IN ('running','degraded') ORDER BY created_at,id"""
            ).fetchall()
            for row in rows:
                campaign_id = str(row["id"])
                self._pause_arms_locked(
                    campaign_id, now, stopped=False,
                    gap_kind="service-interruption",
                    gap_reason="Service restart paused forward campaign decisions",
                )
                blocker = _blocker(
                    "service_restart", "Campaign was operating when the service restarted",
                    retryable=True,
                    repair="Explicitly resume the campaign; missed time remains a durable gap.",
                    source_id=campaign_id,
                )
                self._set_state_locked(campaign_id, "paused", now, blocker)
                self._event_locked(campaign_id, "service-interrupted", now, blocker)
                recovered += 1
        return recovered

    def campaigns(self, *, limit: int = 100,
                  recorder: ServiceEvidenceRecorder | None = None) -> list[dict[str, Any]]:
        _limit(limit)
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM forward_campaigns ORDER BY created_at DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._view_row(row, recorder=recorder) for row in rows]

    def history(self, campaign_id: str, *, after_sequence: int = 0,
                limit: int = 100) -> dict[str, Any]:
        ident = _text(campaign_id, "campaign_id")
        _limit(limit)
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) \
                or after_sequence < 0:
            raise ForwardCampaignError("Campaign history cursor is invalid")
        self.campaign(ident)
        with self._lock:
            rows = self.db.execute(
                """SELECT payload_json,sequence FROM forward_campaign_events
                   WHERE campaign_id=? AND sequence>? ORDER BY sequence LIMIT ?""",
                (ident, after_sequence, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        return {
            "schema": "pumpwizard.forward-campaign-history/v1",
            "campaign_id": ident,
            "events": [json.loads(row["payload_json"]) for row in selected],
            "next_cursor": int(selected[-1]["sequence"]) if len(rows) > limit else None,
        }

    def command(self, campaign_id: str, *, action: str,
                confirmation: str | None = None,
                idempotency_key: str | None = None) -> dict[str, Any]:
        ident = _text(campaign_id, "campaign_id")
        if action not in {"pause", "resume", "stop"}:
            raise ForwardCampaignError("Campaign command must be pause, resume or stop")
        if action == "stop" and confirmation != ident:
            raise ForwardCampaignError("Stopping a campaign requires the exact campaign ID")
        now = _timestamp(self.clock(), "campaign command time")
        delivery_key = (
            _text(idempotency_key, "idempotency_key")
            if idempotency_key is not None else None
        )
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if delivery_key is not None:
                prior = self.db.execute(
                    "SELECT * FROM forward_campaign_commands WHERE idempotency_key=?",
                    (delivery_key,),
                ).fetchone()
                if prior is not None:
                    if prior["campaign_id"] != ident or prior["action"] != action:
                        raise ForwardCampaignError(
                            "Campaign command idempotency key belongs to another command",
                            code="identity_conflict",
                        )
                    repeated = self.db.execute(
                        "SELECT * FROM forward_campaigns WHERE id=?", (ident,)
                    ).fetchone()
                    return self._view_row(repeated)
            row = self.db.execute(
                "SELECT * FROM forward_campaigns WHERE id=?", (ident,)
            ).fetchone()
            if row is None:
                raise ForwardCampaignError("Unknown forward campaign", code="not_found")
            current = str(row["state"])
            if action == "pause":
                if current == "paused":
                    updated = row
                    kind = "paused"
                elif current not in {"running", "degraded"}:
                    raise ForwardCampaignError("Campaign cannot pause from its current state")
                else:
                    self._pause_arms_locked(ident, now, stopped=False)
                    self._set_state_locked(ident, "paused", now, None)
                    kind = "paused"
            elif action == "resume":
                if current == "running":
                    updated = row
                    kind = "resumed"
                elif current not in {"paused", "degraded"}:
                    if current == "capped":
                        raise ForwardCampaignError(
                            "A capped campaign cannot widen its immutable ceilings; start a new version",
                            code="quota_exhausted",
                        )
                    raise ForwardCampaignError("Campaign cannot resume from its current state")
                elif now >= float(row["deadline_at"]):
                    raise ForwardCampaignError(
                        "Campaign observation duration has ended; start a new version",
                        code="duration_exhausted",
                    )
                else:
                    self._resume_arms_locked(ident, now)
                    self._set_state_locked(ident, "running", now, None)
                    self._open_interval_locked(ident, now, "campaign-resume")
                    kind = "resumed"
            else:
                if current != "stopped":
                    self._pause_arms_locked(ident, now, stopped=True)
                    self._set_state_locked(ident, "stopped", now, None, stopped_at=now)
                kind = "stopped"
            if current != self.db.execute(
                "SELECT state FROM forward_campaigns WHERE id=?", (ident,)
            ).fetchone()["state"] or action == "stop" and current != "stopped":
                self._event_locked(ident, kind, now, {"prior_state": current})
            if delivery_key is not None:
                self.db.execute(
                    "INSERT INTO forward_campaign_commands VALUES(?,?,?,?)",
                    (delivery_key, ident, action, now),
                )
            updated = self.db.execute(
                "SELECT * FROM forward_campaigns WHERE id=?", (ident,)
            ).fetchone()
        return self._view_row(updated)

    def scan_demands(self, *, now: float | None = None) -> list[dict[str, Any]]:
        at = _timestamp(self.clock() if now is None else now, "campaign scan time")
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                """SELECT * FROM forward_campaigns
                   WHERE state IN ('running','degraded') AND next_scan_at<=?
                   ORDER BY next_scan_at,created_at,id""",
                (at,),
            ).fetchall()
            demands = []
            for row in rows:
                payload = json.loads(row["payload_json"])
                if at >= float(row["deadline_at"]):
                    self._finish_locked(row, at, completed=True)
                    continue
                if int(row["request_count"]) >= int(payload["quotas"]["max_requests"]):
                    if _retain_through_deadline(payload):
                        self._hold_at_request_quota_locked(row, at)
                    else:
                        self._cap_locked(
                            row, at, "request_quota", "Provider request quota exhausted"
                        )
                    continue
                demands.append({
                    "campaign_id": row["id"], "arm_count": len(payload["arms"]),
                    **payload["source"],
                    "max_concurrency": payload["quotas"]["max_concurrency"],
                    "remaining_requests": (
                        int(payload["quotas"]["max_requests"])
                        - int(row["request_count"])
                    ),
                })
        return demands

    def record_scan(self, campaign_ids: list[str], *, success: bool,
                    finished_at: float, next_retry_at: float | None = None,
                    detail: str | None = None) -> None:
        at = _timestamp(finished_at, "provider finish time")
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for campaign_id in sorted(set(campaign_ids)):
                row = self.db.execute(
                    "SELECT * FROM forward_campaigns WHERE id=?", (campaign_id,)
                ).fetchone()
                if row is None:
                    continue
                payload = json.loads(row["payload_json"])
                count = int(row["request_count"]) + 1
                next_at = at + float(payload["source"]["cadence_seconds"])
                if next_retry_at is not None:
                    next_at = max(next_at, float(next_retry_at))
                operating = row["state"] in {"running", "degraded"}
                blocker = (
                    None if success else _blocker(
                        "provider_failure", detail or "Provider request failed",
                        retryable=True,
                        repair=("Wait for bounded backoff or pause and inspect "
                                "provider configuration."),
                        source_id=campaign_id,
                    )
                ) if operating else (
                    json.loads(row["blocker_json"]) if row["blocker_json"] else None
                )
                state = ("running" if success else "degraded") if operating else row["state"]
                self.db.execute(
                    """UPDATE forward_campaigns SET state=?,request_count=?,next_scan_at=?,
                       updated_at=?,blocker_json=? WHERE id=?""",
                    (state, count, next_at, at,
                     canonical_json(blocker) if blocker else None, campaign_id),
                )
                self._event_locked(campaign_id, "provider-request", at, {
                    "success": success, "request_count": count,
                    "next_scan_at": next_at, "detail": (detail or "")[:300],
                })
                if (count >= int(payload["quotas"]["max_requests"])
                        and row["state"] not in {"capped", "completed", "stopped"}):
                    refreshed = self.db.execute(
                        "SELECT * FROM forward_campaigns WHERE id=?", (campaign_id,)
                    ).fetchone()
                    if _retain_through_deadline(payload):
                        self._hold_at_request_quota_locked(refreshed, at)
                    else:
                        self._cap_locked(
                            refreshed, at, "request_quota", "Provider request quota exhausted"
                        )

    def block_provider_mismatch(self, campaign_id: str, *, actual_provider: str,
                                at: float | None = None) -> dict[str, Any]:
        """Pause a campaign before a request when its immutable provider is unavailable."""
        ident = _text(campaign_id, "campaign_id")
        actual = _text(actual_provider, "actual_provider", maximum=120)
        now = _timestamp(self.clock() if at is None else at, "provider mismatch time")
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM forward_campaigns WHERE id=?", (ident,)
            ).fetchone()
            if row is None:
                raise ForwardCampaignError("Unknown forward campaign", code="not_found")
            if row["state"] not in {"running", "degraded"}:
                return self._view_row(row)
            expected = json.loads(row["payload_json"])["source"]["provider"]
            blocker = _blocker(
                "provider_mismatch",
                f"Campaign requires {expected}; service provides {actual}",
                retryable=False,
                repair="Start a new campaign version bound to the configured provider.",
                source_id=ident,
            )
            self._pause_arms_locked(
                ident, now, stopped=False, gap_kind="provider-mismatch",
                gap_reason="Configured provider did not match the frozen campaign provider",
            )
            self._set_state_locked(ident, "paused", now, blocker)
            self._event_locked(ident, "provider-mismatch", now, blocker)
            updated = self.db.execute(
                "SELECT * FROM forward_campaigns WHERE id=?", (ident,)
            ).fetchone()
        return self._view_row(updated)

    def reconcile_limits(self, recorder: ServiceEvidenceRecorder) -> dict[str, int]:
        now = _timestamp(self.clock(), "campaign reconciliation time")
        totals = {"checked": 0, "capped": 0, "completed": 0}
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                """SELECT * FROM forward_campaigns
                   WHERE state IN ('running','degraded','paused') ORDER BY created_at,id"""
            ).fetchall()
            for row in rows:
                totals["checked"] += 1
                payload = json.loads(row["payload_json"])
                if now >= float(row["deadline_at"]):
                    self._finish_locked(row, now, completed=True)
                    totals["completed"] += 1
                    continue
                active = self._active_seconds_locked(str(row["id"]), now)
                usage = recorder.provider_usage(str(row["id"]))
                paper_bytes = self.db.execute(
                    """SELECT COALESCE(SUM(LENGTH(observation.record_json)),0)
                       FROM paper_observations AS observation
                       JOIN forward_campaign_arms AS arm
                         ON arm.paper_run_id=observation.run_id
                       WHERE arm.campaign_id=?""",
                    (row["id"],),
                ).fetchone()[0]
                attributable = int(usage["response_bytes"] or 0) + int(paper_bytes or 0)
                reason = None
                detail = None
                if active >= float(payload["quotas"]["max_active_runtime_seconds"]):
                    reason, detail = "runtime_quota", "Active runtime quota exhausted"
                elif attributable >= int(payload["quotas"]["max_storage_bytes"]):
                    reason, detail = "storage_quota", "Attributable storage quota exhausted"
                if reason:
                    self._cap_locked(row, now, reason, detail)
                    totals["capped"] += 1
        return totals

    def save_report(self, campaign_id: str, report: dict[str, Any]) -> dict[str, Any]:
        ident = _text(campaign_id, "campaign_id")
        value = json.loads(canonical_json(report))
        if value.get("campaign_id") != ident:
            raise ForwardCampaignError("Campaign report identity is mismatched")
        report_id = value.get("id") or "campaign-report-" + _sha256(value)[:32]
        value["id"] = report_id
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute(
                "SELECT 1 FROM forward_campaigns WHERE id=?", (ident,)
            ).fetchone() is None:
                raise ForwardCampaignError("Unknown forward campaign", code="not_found")
            existing = self.db.execute(
                "SELECT payload_json FROM forward_campaign_reports WHERE report_id=?",
                (report_id,),
            ).fetchone()
            encoded = canonical_json(value)
            if existing is not None and existing["payload_json"] != encoded:
                raise ForwardCampaignError("Campaign report identity collision")
            self.db.execute(
                """INSERT OR IGNORE INTO forward_campaign_reports(
                       campaign_id,report_id,previous_report_id,created_at,payload_json
                   ) VALUES(?,?,?,?,?)""",
                (ident, report_id, value.get("previous_report_id"),
                 float(value.get("created_at", self.clock())), encoded),
            )
        return value

    def reports(self, campaign_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        ident = _text(campaign_id, "campaign_id")
        _limit(limit)
        with self._lock:
            rows = self.db.execute(
                """SELECT payload_json FROM forward_campaign_reports
                   WHERE campaign_id=? ORDER BY created_at DESC,report_id DESC LIMIT ?""",
                (ident, limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def report(self, report_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute(
                "SELECT payload_json FROM forward_campaign_reports WHERE report_id=?",
                (_text(report_id, "report_id"),),
            ).fetchone()
        if row is None:
            raise ForwardCampaignError("Unknown campaign report", code="not_found")
        return json.loads(row["payload_json"])

    def _view_row(self, row: sqlite3.Row,
                  recorder: ServiceEvidenceRecorder | None = None) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        with self._lock:
            arms = self.db.execute(
                """SELECT arm.*,run.* FROM forward_campaign_arms AS arm
                   JOIN paper_runs AS run ON run.id=arm.paper_run_id
                   WHERE arm.campaign_id=? ORDER BY arm.ordinal""",
                (row["id"],),
            ).fetchall()
            intervals = self.db.execute(
                """SELECT payload_json FROM forward_campaign_intervals
                   WHERE campaign_id=? ORDER BY started_at,interval_id""",
                (row["id"],),
            ).fetchall()
            event_count = int(self.db.execute(
                "SELECT COUNT(*) FROM forward_campaign_events WHERE campaign_id=?",
                (row["id"],),
            ).fetchone()[0])
            report_count = int(self.db.execute(
                "SELECT COUNT(*) FROM forward_campaign_reports WHERE campaign_id=?",
                (row["id"],),
            ).fetchone()[0])
            preset_row = self.db.execute(
                "SELECT preset_id FROM forward_campaign_presets WHERE campaign_id=?",
                (row["id"],),
            ).fetchone()
            evaluation_cycle_count = int(self.db.execute(
                """SELECT COUNT(*) FROM forward_campaign_evaluation_cycles
                   WHERE campaign_id=?""", (row["id"],),
            ).fetchone()[0])
        arm_views = []
        for arm in arms:
            paper_row = self.db.execute(
                "SELECT * FROM paper_runs WHERE id=?", (arm["paper_run_id"],)
            ).fetchone()
            overview = self.paper._overview_row(paper_row)
            arm_payload = json.loads(arm["payload_json"])
            arm_views.append({**arm_payload, "state": overview["state"],
                              "cursor": overview["cursor"], "counts": overview["counts"],
                              "fault": overview["fault"], "retry": overview["retry"],
                              "evaluator_report_id": (overview.get("evaluator_report") or {}).get("id")})
        usage = recorder.provider_usage(str(row["id"])) if recorder is not None else {
            "requests": row["request_count"], "response_bytes": None,
            "result_rows": None, "duplicate_rows": None, "excluded_rows": None,
            "timeout_events": None, "rate_limit_events": None, "errors": None,
            "maximum_source_age_seconds": None, "last_request_at": None,
        }
        cost = _cost_view(payload["provider_cost"], usage)
        now = float(self.clock())
        active = self._active_seconds(str(row["id"]), now)
        wall_end = now
        if row["state"] in {"stopped", "completed"} and row["stopped_at"] is not None:
            wall_end = float(row["stopped_at"])
        elif row["state"] == "capped":
            wall_end = float(row["updated_at"])
        return {
            **payload, "state": row["state"], "updated_at": row["updated_at"],
            "stopped_at": row["stopped_at"], "next_scan_at": row["next_scan_at"],
            "request_count": row["request_count"],
            "preset_id": preset_row["preset_id"] if preset_row is not None else None,
            "storage": {
                "baseline_physical_bytes": int(row["baseline_storage_bytes"] or 0),
                "current_physical_bytes": _physical_storage_bytes(self.root),
                "scope": "shared campaign, paper, and recorder SQLite files",
            },
            "blocker": json.loads(row["blocker_json"]) if row["blocker_json"] else None,
            "arms": arm_views,
            "operating_intervals": [json.loads(item["payload_json"]) for item in intervals],
            "progress": {
                "planned_duration_seconds": payload["duration_seconds"],
                "active_seconds": active,
                "wall_seconds": max(
                    0.0, min(wall_end, float(row["deadline_at"])) - float(row["start_at"])
                ),
                "events": event_count, "reports": report_count,
                "evaluation_cycles": evaluation_cycle_count,
            },
            "usage": usage, "provider_cost": cost,
        }

    def _pause_arms_locked(self, campaign_id: str, at: float, *, stopped: bool,
                           gap_kind: str = "campaign-pause",
                           gap_reason: str = "Forward campaign decisions paused") -> None:
        rows = self.db.execute(
            """SELECT run.* FROM paper_runs AS run
               JOIN forward_campaign_arms AS arm ON arm.paper_run_id=run.id
               WHERE arm.campaign_id=?""",
            (campaign_id,),
        ).fetchall()
        for run in rows:
            target = "stopped" if stopped else "paused"
            self.db.execute(
                """UPDATE paper_runs SET state=?,paused_at=?,stopped_at=?
                   WHERE id=? AND state!='stopped'""",
                (target, None if stopped else at, at if stopped else None, run["id"]),
            )
            self.paper._close_interval_locked(str(run["id"]), at)
            if stopped:
                self.paper._close_open_gaps_locked(str(run["id"]), at)
            else:
                self.paper._upsert_gap_locked(
                    str(run["id"]),
                    gap_id=(gap_kind + ":" + campaign_id + ":"
                            + _sha256([campaign_id, run["id"], gap_kind, at])[:16]),
                    started_at=at, ended_at=None,
                    reason=gap_reason,
                )
        self._close_interval_locked(campaign_id, at)

    def _resume_arms_locked(self, campaign_id: str, at: float) -> None:
        rows = self.db.execute(
            """SELECT run.* FROM paper_runs AS run
               JOIN forward_campaign_arms AS arm ON arm.paper_run_id=run.id
               WHERE arm.campaign_id=?""",
            (campaign_id,),
        ).fetchall()
        for run in rows:
            if run["state"] == "stopped":
                raise ForwardCampaignError("Stopped campaign arms cannot resume")
            if int(run["retry_count"] or 0) >= int(run["max_retries"] or MAX_RUN_RETRIES):
                raise ForwardCampaignError(
                    "A campaign arm reached its bounded retry limit", code="retry_limit"
                )
            self.db.execute(
                """UPDATE paper_runs SET state='running',paused_at=NULL,interrupted_at=NULL,
                   decision_not_before_at=?,fault_json=NULL,next_retry_at=NULL WHERE id=?""",
                (at, run["id"]),
            )
            self.paper._close_open_gaps_locked(
                str(run["id"]), at,
                prefixes=(
                    "campaign-pause:", "service-interruption:",
                    "provider-mismatch:", "fault-gap:",
                ),
            )
            self.paper._open_interval_locked(
                str(run["id"]), at, reason="forward-campaign-resume"
            )

    def _cap_locked(self, row: sqlite3.Row, at: float, code: str, detail: str) -> None:
        blocker = _blocker(
            code, detail, retryable=False,
            repair="Create a new campaign version with reviewed ceilings; this trial remains immutable.",
            source_id=str(row["id"]),
        )
        self._pause_arms_locked(
            str(row["id"]), at, stopped=False, gap_kind="campaign-cap",
            gap_reason=detail,
        )
        self._set_state_locked(str(row["id"]), "capped", at, blocker)
        self._event_locked(str(row["id"]), "capped", at, blocker)

    def _hold_at_request_quota_locked(self, row: sqlite3.Row, at: float) -> None:
        """Stop provider reads while retaining the approved observation window."""
        detail = "Provider request quota exhausted; observing through the approved deadline"
        blocker = _blocker(
            "request_quota", detail, retryable=False,
            repair="Wait for the approved deadline; no additional provider reads are allowed.",
            source_id=str(row["id"]),
        )
        self.db.execute(
            """UPDATE forward_campaigns SET state='degraded',next_scan_at=?,updated_at=?,
               blocker_json=? WHERE id=?""",
            (
                float(row["deadline_at"]), at, canonical_json(blocker), str(row["id"]),
            ),
        )
        self._event_locked(str(row["id"]), "request-quota-reached", at, blocker)

    def _finish_locked(self, row: sqlite3.Row, at: float, *, completed: bool) -> None:
        self._pause_arms_locked(str(row["id"]), at, stopped=True)
        state = "completed" if completed else "stopped"
        self._set_state_locked(str(row["id"]), state, at, None, stopped_at=at)
        self._event_locked(str(row["id"]), state, at, {"planned_duration_reached": completed})

    def _set_state_locked(self, campaign_id: str, state: str, at: float,
                          blocker: dict[str, Any] | None,
                          stopped_at: float | None = None) -> None:
        self.db.execute(
            """UPDATE forward_campaigns SET state=?,updated_at=?,stopped_at=?,blocker_json=?
               WHERE id=?""",
            (state, at, stopped_at, canonical_json(blocker) if blocker else None, campaign_id),
        )

    def _event_locked(self, campaign_id: str, kind: str, at: float,
                      detail: dict[str, Any]) -> None:
        sequence = int(self.db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM forward_campaign_events WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()[0])
        event_id = "campaign-event-" + _sha256([campaign_id, sequence, kind, detail])[:32]
        event = {
            "schema": EVENT_SCHEMA, "id": event_id, "campaign_id": campaign_id,
            "sequence": sequence, "occurred_at": at, "kind": kind,
            "detail": json.loads(canonical_json(detail)),
        }
        self.db.execute(
            "INSERT INTO forward_campaign_events VALUES(?,?,?,?,?,?)",
            (campaign_id, sequence, event_id, at, kind, canonical_json(event)),
        )

    def _open_interval_locked(self, campaign_id: str, at: float, reason: str) -> None:
        interval_id = "campaign-interval-" + _sha256([campaign_id, at, reason])[:24]
        value = {
            "schema": "pumpwizard.forward-campaign-interval/v1",
            "campaign_id": campaign_id, "id": interval_id,
            "started_at": at, "ended_at": None, "reason": reason,
        }
        self.db.execute(
            "INSERT OR IGNORE INTO forward_campaign_intervals VALUES(?,?,?,?,?,?)",
            (campaign_id, interval_id, at, None, reason, canonical_json(value)),
        )

    def _close_interval_locked(self, campaign_id: str, at: float) -> None:
        rows = self.db.execute(
            """SELECT * FROM forward_campaign_intervals
               WHERE campaign_id=? AND ended_at IS NULL""",
            (campaign_id,),
        ).fetchall()
        for row in rows:
            end = max(float(row["started_at"]), at)
            value = json.loads(row["payload_json"])
            value["ended_at"] = end
            self.db.execute(
                """UPDATE forward_campaign_intervals SET ended_at=?,payload_json=?
                   WHERE campaign_id=? AND interval_id=?""",
                (end, canonical_json(value), campaign_id, row["interval_id"]),
            )

    def _active_seconds_locked(self, campaign_id: str, now: float) -> float:
        rows = self.db.execute(
            "SELECT started_at,ended_at FROM forward_campaign_intervals WHERE campaign_id=?",
            (campaign_id,),
        ).fetchall()
        return sum(max(0.0, float(row["ended_at"] or now) - float(row["started_at"]))
                   for row in rows)

    def _active_seconds(self, campaign_id: str, now: float) -> float:
        with self._lock:
            return self._active_seconds_locked(campaign_id, now)

    def close(self) -> None:
        self.paper.close()

    def __enter__(self) -> "ForwardCampaignStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _manifest(request: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(canonical_json(request))
    except (TypeError, ValueError) as exc:
        raise ForwardCampaignError("Campaign request must be canonical JSON") from exc
    expected = {
        "schema", "campaign_key", "supersedes_campaign_id", "source", "arms",
        "assumptions", "coverage", "duration_seconds", "quotas", "provider_cost",
    }
    if not isinstance(value, dict) or set(value) != expected or value.get("schema") != REQUEST_SCHEMA:
        raise ForwardCampaignError("Campaign request fields do not match the v1 contract")
    campaign_key = _text(value["campaign_key"], "campaign_key", maximum=160)
    parent = value["supersedes_campaign_id"]
    if parent is not None:
        parent = _text(parent, "supersedes_campaign_id")
    source = _source(value["source"])
    arms_raw = value["arms"]
    if not isinstance(arms_raw, list) or not 1 <= len(arms_raw) <= MAX_ARMS:
        raise ForwardCampaignError(f"Campaigns require one to {MAX_ARMS} strategy arms")
    arms = [_arm(row, index) for index, row in enumerate(arms_raw, start=1)]
    if len({row["id"] for row in arms}) != len(arms):
        raise ForwardCampaignError("Campaign arm IDs must be unique")
    assumptions = _object(value["assumptions"], "assumptions")
    if not {"cost_model", "execution_model"} <= set(assumptions):
        raise ForwardCampaignError("Campaign assumptions need cost and execution models")
    coverage = _coverage(value["coverage"])
    duration = _number(value["duration_seconds"], "duration_seconds", minimum=60,
                       maximum=366 * 24 * 60 * 60)
    quotas = _quotas(value["quotas"], duration)
    provider_cost = _provider_cost(value["provider_cost"])
    return {
        "campaign_key": campaign_key, "supersedes_campaign_id": parent,
        "source": source, "arms": arms, "assumptions": assumptions,
        "coverage": coverage, "duration_seconds": duration,
        "quotas": quotas, "provider_cost": provider_cost,
    }


def _source(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "provider", "minimum_cap", "maximum_cap", "priority_mints", "limit",
        "cadence_seconds",
    }:
        raise ForwardCampaignError("Campaign source universe is incomplete")
    provider = _text(value["provider"], "provider", maximum=120)
    lower = _number(value["minimum_cap"], "minimum_cap", minimum=0)
    upper = _number(value["maximum_cap"], "maximum_cap", minimum=0)
    if upper <= lower:
        raise ForwardCampaignError("Campaign maximum cap must exceed minimum cap")
    priority = value["priority_mints"]
    if not isinstance(priority, list) or len(priority) > 30:
        raise ForwardCampaignError("Campaign priority universe exceeds 30 mints")
    selected = sorted({_text(item, "priority mint", maximum=80) for item in priority})
    limit = _integer(value["limit"], "source limit", 1, 100)
    if len(selected) > limit:
        raise ForwardCampaignError(
            "Campaign priority universe cannot exceed its frozen source limit"
        )
    cadence = _number(value["cadence_seconds"], "cadence_seconds", minimum=1,
                      maximum=24 * 60 * 60)
    return {
        "provider": provider, "minimum_cap": lower, "maximum_cap": upper,
        "priority_mints": selected, "limit": limit, "cadence_seconds": cadence,
    }


def _arm(value: Any, ordinal: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "id", "source_session_id", "strategy_sha256", "strategy_package",
        "simulated_capital_usd",
    }:
        raise ForwardCampaignError(f"Campaign arm {ordinal} is incomplete")
    package = json.loads(canonical_json(value["strategy_package"]))
    parsed = parse_package(package)
    digest = _text(value["strategy_sha256"], "strategy_sha256")
    if parsed.digest != digest:
        raise ForwardCampaignError(
            f"Campaign arm {ordinal} strategy identity changed", code="identity_conflict"
        )
    return {
        "id": _text(value["id"], "arm id", maximum=80),
        "source_session_id": _text(value["source_session_id"], "source_session_id"),
        "strategy_sha256": digest, "strategy_package": package,
        "simulated_capital_usd": _number(
            value["simulated_capital_usd"], "simulated_capital_usd",
            minimum=0.000001, maximum=1_000_000_000,
        ),
    }


def _coverage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "max_source_age_seconds", "minimum_independent_tokens",
        "minimum_observations", "maximum_gap_seconds",
    }:
        raise ForwardCampaignError("Campaign coverage requirements are incomplete")
    return {
        "max_source_age_seconds": _number(
            value["max_source_age_seconds"], "max_source_age_seconds", minimum=0,
            maximum=30 * 24 * 60 * 60,
        ),
        "minimum_independent_tokens": _integer(
            value["minimum_independent_tokens"], "minimum_independent_tokens", 1, 1_000_000,
        ),
        "minimum_observations": _integer(
            value["minimum_observations"], "minimum_observations", 1, 10_000_000,
        ),
        "maximum_gap_seconds": _number(
            value["maximum_gap_seconds"], "maximum_gap_seconds", minimum=1,
            maximum=30 * 24 * 60 * 60,
        ),
    }


def _quotas(value: Any, duration: float) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "max_requests", "max_active_runtime_seconds", "max_storage_bytes",
        "max_concurrency",
    }:
        raise ForwardCampaignError("Campaign quotas are incomplete")
    runtime = _number(
        value["max_active_runtime_seconds"], "max_active_runtime_seconds",
        minimum=1, maximum=duration,
    )
    return {
        "max_requests": _integer(value["max_requests"], "max_requests", 1, 100_000),
        "max_active_runtime_seconds": runtime,
        "max_storage_bytes": _integer(
            value["max_storage_bytes"], "max_storage_bytes", 1_024, 2 * 1024 * 1024 * 1024,
        ),
        "max_concurrency": _integer(
            value["max_concurrency"], "max_concurrency", 1, MAX_SHARED_CONCURRENCY,
        ),
    }


def _provider_cost(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "currency", "marginal_cost_per_request", "fixed_subscription_cost", "price_basis",
    }:
        raise ForwardCampaignError("Provider cost declaration is incomplete")
    basis = value["price_basis"]
    if basis not in {"known", "mixed", "unknown"}:
        raise ForwardCampaignError("Provider price basis must be known, mixed or unknown")
    marginal = value["marginal_cost_per_request"]
    fixed = value["fixed_subscription_cost"]
    marginal = None if marginal is None else _number(
        marginal, "marginal_cost_per_request", minimum=0, maximum=1_000_000,
    )
    fixed = None if fixed is None else _number(
        fixed, "fixed_subscription_cost", minimum=0, maximum=1_000_000_000,
    )
    if basis == "known" and (marginal is None or fixed is None):
        raise ForwardCampaignError("Known provider pricing needs marginal and fixed costs")
    if basis == "unknown" and (marginal is not None or fixed is not None):
        raise ForwardCampaignError("Unknown provider pricing must not encode a zero estimate")
    return {
        "currency": _text(value["currency"], "provider cost currency", maximum=12),
        "marginal_cost_per_request": marginal,
        "fixed_subscription_cost": fixed, "price_basis": basis,
    }


def _resource_model(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "expected_http_reads_per_request", "max_http_reads_per_request",
        "estimated_provider_response_bytes_per_request",
        "max_provider_response_bytes_per_request",
        "estimated_normalized_bytes_per_quote", "fixed_storage_overhead_bytes",
    } or value.get("schema") != RESOURCE_MODEL_SCHEMA:
        raise ForwardCampaignError("Observation resource model fields do not match v1")
    expected_reads = _integer(
        value["expected_http_reads_per_request"], "expected HTTP reads", 0, 10_000
    )
    max_reads = _integer(value["max_http_reads_per_request"], "maximum HTTP reads", 0, 10_000)
    expected_bytes = _integer(
        value["estimated_provider_response_bytes_per_request"],
        "estimated provider response bytes", 0, 2 * 1024 * 1024 * 1024,
    )
    max_bytes = _integer(
        value["max_provider_response_bytes_per_request"],
        "maximum provider response bytes", 0, 2 * 1024 * 1024 * 1024,
    )
    if expected_reads > max_reads or expected_bytes > max_bytes:
        raise ForwardCampaignError("Expected resource use cannot exceed its declared maximum")
    return {
        "schema": RESOURCE_MODEL_SCHEMA,
        "expected_http_reads_per_request": expected_reads,
        "max_http_reads_per_request": max_reads,
        "estimated_provider_response_bytes_per_request": expected_bytes,
        "max_provider_response_bytes_per_request": max_bytes,
        "estimated_normalized_bytes_per_quote": _integer(
            value["estimated_normalized_bytes_per_quote"],
            "estimated normalized quote bytes", 1, 16 * 1024 * 1024,
        ),
        "fixed_storage_overhead_bytes": _integer(
            value["fixed_storage_overhead_bytes"], "fixed storage overhead bytes",
            0, 2 * 1024 * 1024 * 1024,
        ),
    }


def _dry_run_estimate(manifest: dict[str, Any], resources: dict[str, Any],
                      identity_sha: str) -> dict[str, Any]:
    cadence = float(manifest["source"]["cadence_seconds"])
    duration = float(manifest["duration_seconds"])
    scheduled = int(math.ceil(duration / cadence))
    attempts = min(scheduled, int(manifest["quotas"]["max_requests"]))
    source_limit = int(manifest["source"]["limit"])
    normalized = (
        int(resources["fixed_storage_overhead_bytes"])
        + attempts * source_limit * int(resources["estimated_normalized_bytes_per_quote"])
    )
    declared_cost = manifest["provider_cost"]
    marginal = declared_cost["marginal_cost_per_request"]
    fixed = declared_cost["fixed_subscription_cost"]
    fully_known = declared_cost["price_basis"] == "known"
    return {
        "schema": DRY_RUN_SCHEMA,
        "preset_identity_sha256": identity_sha,
        "network_reads": 0,
        "duration_seconds": duration,
        "cadence_seconds": cadence,
        "scheduled_intervals": scheduled,
        "bounded_provider_attempts": attempts,
        "expected_http_reads": attempts * int(resources["expected_http_reads_per_request"]),
        "maximum_http_reads": attempts * int(resources["max_http_reads_per_request"]),
        "estimated_provider_response_bytes": (
            attempts * int(resources["estimated_provider_response_bytes_per_request"])
        ),
        "maximum_provider_response_bytes": (
            attempts * int(resources["max_provider_response_bytes_per_request"])
        ),
        "estimated_attributable_storage_bytes": normalized,
        "maximum_attributable_storage_bytes": int(manifest["quotas"]["max_storage_bytes"]),
        "estimated_storage_headroom_bytes": (
            int(manifest["quotas"]["max_storage_bytes"]) - normalized
        ),
        "maximum_active_runtime_seconds": manifest["quotas"]["max_active_runtime_seconds"],
        "maximum_concurrency": manifest["quotas"]["max_concurrency"],
        "cost": {
            **declared_cost,
            "expected_marginal_cost": None if marginal is None else attempts * float(marginal),
            "expected_total_cost": (
                attempts * float(marginal) + float(fixed) if fully_known else None
            ),
            "fully_costed_profitability_allowed": fully_known,
        },
        "stop_conditions": [
            "duration", "provider_request_quota", "active_runtime_quota",
            "attributable_storage_quota", "operator_stop",
        ],
        "warnings": (["estimated attributable storage exceeds the frozen ceiling"]
                     if normalized > int(manifest["quotas"]["max_storage_bytes"]) else [])
                    + (["provider billing is unknown or partial; fully-costed profitability is blocked"]
                       if not fully_known else []),
    }


def _retain_through_deadline(payload: dict[str, Any]) -> bool:
    stop_policy = payload.get("assumptions", {}).get("stop_policy", {})
    return stop_policy.get("request_quota_lifecycle") == "retain-through-deadline"


def _cost_view(declared: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    requests = int(usage.get("requests") or 0)
    marginal = declared["marginal_cost_per_request"]
    known_marginal = None if marginal is None else requests * float(marginal)
    fully_known = declared["price_basis"] == "known"
    return {
        **declared, "observed_requests": requests,
        "known_marginal_cost": known_marginal,
        "known_fixed_cost": declared["fixed_subscription_cost"],
        "known_total_cost": (
            known_marginal + float(declared["fixed_subscription_cost"])
            if fully_known else None
        ),
        "cost_status": "known" if fully_known else "unknown_or_partial",
        "fully_costed_profitability_allowed": fully_known,
    }


def _blocker(code: str, message: str, *, retryable: bool,
             repair: str, source_id: str) -> dict[str, Any]:
    return {
        "schema": BLOCKER_SCHEMA, "code": code, "message": message,
        "retryable": retryable, "repair": repair, "source_id": source_id,
    }


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ForwardCampaignError(f"Campaign {label} must be an object")
    encoded = canonical_json(value)
    if len(encoded.encode()) > 32_000:
        raise ForwardCampaignError(f"Campaign {label} exceeds 32 KiB")
    return json.loads(encoded)


def _text(value: Any, label: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ForwardCampaignError(f"Campaign {label} is invalid")
    return value.strip()


def _number(value: Any, label: str, *, minimum: float,
            maximum: float = math.inf) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ForwardCampaignError(f"Campaign {label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ForwardCampaignError(f"Campaign {label} is outside its bounded range")
    return result


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ForwardCampaignError(f"Campaign {label} is outside its bounded range")
    return value


def _timestamp(value: Any, label: str) -> float:
    return _number(value, label, minimum=0)


def _limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_HISTORY_LIMIT:
        raise ForwardCampaignError(f"Campaign limit must be between 1 and {MAX_HISTORY_LIMIT}")


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _physical_storage_bytes(root: Path) -> int:
    total = 0
    for name in ("paper-research.sqlite", "service-evidence.sqlite"):
        for suffix in ("", "-wal", "-shm"):
            path = root / (name + suffix)
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                continue
    return total
