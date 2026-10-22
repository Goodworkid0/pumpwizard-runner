"""Single-owner background service engine and persistent read-only job state."""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Protocol

from . import __version__
from .library import StrategyLibrary
from .forward_campaigns import ForwardCampaignStore
from .evidence_alerts import EvidenceAlertStore
from .paper_research import PaperResearchStore, PaperResearchWorker
from .research_jobs import ResearchJobWorker, ResearchRegistry
from .scheduler import MonitorScheduler
from .service_recorder import ServiceEvidenceRecorder
from .transfers import GENESIS, TransferError, public_key


SCHEMA_VERSION = 1
SERVICE_PORT = 8781
JOB_ID = "strategy-monitor"


class ServiceSigner(Protocol):
    """Narrow Worker 2 injection boundary; no generic sign operation exists."""

    def state(self) -> dict[str, object]: ...
    def pause_entries(self, reason: str) -> dict[str, object]: ...
    def resume_entries(self, authorization: object) -> dict[str, object]: ...
    def revoke(self, reason: str) -> dict[str, object]: ...
    def lock(self, reason: str) -> dict[str, object]: ...
    def reconcile(self) -> dict[str, object]: ...
    def run_cycle(self) -> dict[str, object] | None: ...


class BlockedServiceSigner:
    """Production-safe default while the service custody slice is unaccepted."""

    def __init__(self):
        self._reason = "Production service signing has not passed funded acceptance"
        self._entries_paused = True

    def state(self) -> dict[str, object]:
        capability = {
            "available": False,
            "mode": "blocked",
            "blockers": [self._reason],
        }
        return {
            "schema": "pumpwizard.service-trading-state/v1",
            "custody_mode": "not-enrolled",
            "signer_state": "locked",
            "authorization_status": "blocked",
            "entries_paused": self._entries_paused,
            "recovery_required": False,
            "production_activation": "blocked",
            "activation_capability": capability,
            "blockers": capability["blockers"],
        }

    def pause_entries(self, reason: str) -> dict[str, object]:
        self._entries_paused = True
        self._reason = _reason(reason)
        return self.state()

    def resume_entries(self, authorization: object) -> dict[str, object]:
        del authorization
        raise ValueError("Fresh accepted service signing authorization is unavailable")

    def revoke(self, reason: str) -> dict[str, object]:
        self._entries_paused = True
        self._reason = _reason(reason)
        return self.state()

    def lock(self, reason: str) -> dict[str, object]:
        self._entries_paused = True
        self._reason = _reason(reason)
        return self.state()

    def reconcile(self) -> dict[str, object]:
        return {"status": "locked", "reconciled": 0}

    def run_cycle(self) -> dict[str, object] | None:
        return None


class ServiceOwnershipError(ValueError):
    pass


class ServiceOwnerLock:
    """Linux advisory lock; the PID text is diagnostic and never authority."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "background-service.lock"
        self.file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise ServiceOwnershipError(
                "Another PumpWizard background service owns this data directory"
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        os.fsync(handle.fileno())
        self.file = handle

    def release(self) -> None:
        if self.file is None:
            return
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


class BackgroundServiceStore:
    def __init__(self, data_dir: Path, *, clock=time.time):
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir / "background-service.sqlite"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            row = self.db.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is not None and int(row["value"]) > SCHEMA_VERSION:
                raise ValueError("Background service state was created by a newer PumpWizard version")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS service_instances (
                    instance_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL UNIQUE CHECK(revision > 0),
                    version TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    port INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('starting','running','stopping','stopped','degraded')),
                    started_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    stopped_at REAL,
                    current_activity TEXT NOT NULL,
                    last_success_at REAL,
                    stop_reason TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS service_instances_recent
                    ON service_instances(revision DESC);
                CREATE TABLE IF NOT EXISTS service_jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    interval_seconds REAL NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    next_at REAL NOT NULL,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    current_activity TEXT NOT NULL,
                    last_started_at REAL,
                    last_success_at REAL,
                    last_error_at REAL,
                    last_error TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS service_job_runs (
                    id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES service_jobs(id),
                    service_instance_id TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','interrupted')),
                    detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS service_job_runs_recent
                    ON service_job_runs(job_id,started_at DESC,id DESC);
            """)
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self.db.execute(
                """INSERT OR IGNORE INTO service_jobs(
                       id,kind,enabled,interval_seconds,revision,next_at,current_activity,last_error
                   ) VALUES(?, 'read-only-monitor', 0, 15, 1, 0, 'idle', '')""",
                (JOB_ID,),
            )
            self.db.execute(
                """UPDATE service_job_runs SET status='interrupted',finished_at=?,
                   detail='Service stopped before this job run completed'
                   WHERE status='running'""",
                (float(self.clock()),),
            )

    @property
    def data_directory_id(self) -> str:
        return hashlib.sha256(str(self.data_dir).encode()).hexdigest()

    def start_instance(self, *, port: int, instance_id: str | None = None) -> dict[str, object]:
        at = float(self.clock())
        ident = instance_id or uuid.uuid4().hex
        with self._lock, self.db:
            self.db.execute(
                """UPDATE service_instances SET state='stopped',stopped_at=?,
                   current_activity='interrupted',stop_reason=?
                   WHERE state!='stopped'""",
                (at, "Previous service ended without a clean shutdown"),
            )
            revision = int(self.db.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM service_instances"
            ).fetchone()[0])
            self.db.execute(
                """INSERT INTO service_instances(
                       instance_id,revision,version,pid,port,state,started_at,heartbeat_at,
                       current_activity,stop_reason
                   ) VALUES(?,?,?,?,?,'running',?,?, 'starting','')""",
                (ident, revision, __version__, os.getpid(), port, at, at),
            )
        return self.status(ident)

    def heartbeat(self, instance_id: str, *, activity: str = "idle",
                  success: bool = False, state: str = "running") -> None:
        if state not in {"running", "stopping", "degraded"}:
            raise ValueError("Invalid active service state")
        at = float(self.clock())
        with self._lock, self.db:
            cursor = self.db.execute(
                """UPDATE service_instances SET heartbeat_at=?,current_activity=?,state=?,
                   last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END
                   WHERE instance_id=? AND state!='stopped'""",
                (at, str(activity)[:160], state, int(success), at, instance_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown active service instance")

    def stop_instance(self, instance_id: str, reason: str) -> None:
        at = float(self.clock())
        with self._lock, self.db:
            self.db.execute(
                """UPDATE service_instances SET state='stopped',heartbeat_at=?,stopped_at=?,
                   current_activity='stopped',stop_reason=? WHERE instance_id=?""",
                (at, at, _reason(reason), instance_id),
            )
            self.db.execute(
                """UPDATE service_job_runs SET status='interrupted',finished_at=?,
                   detail='Service stopped before this job run completed'
                   WHERE service_instance_id=? AND status='running'""",
                (at, instance_id),
            )

    def status(self, instance_id: str | None = None) -> dict[str, object]:
        with self._lock:
            if instance_id is None:
                row = self.db.execute(
                    "SELECT * FROM service_instances ORDER BY revision DESC LIMIT 1"
                ).fetchone()
            else:
                row = self.db.execute(
                    "SELECT * FROM service_instances WHERE instance_id=?", (instance_id,)
                ).fetchone()
        if row is None:
            return {
                "schema": "pumpwizard.background-service-status/v1",
                "instance_id": None, "instance_revision": 0,
                "data_directory_id": self.data_directory_id, "version": __version__,
                "state": "stopped", "started_at": None, "heartbeat_at": None,
                "current_activity": "stopped", "last_success_at": None,
                "health": "unknown", "stop_reason": "No service instance has started",
                "production_trading": "blocked",
            }
        result = {
            "schema": "pumpwizard.background-service-status/v1",
            "instance_id": row["instance_id"], "instance_revision": row["revision"],
            "data_directory_id": self.data_directory_id, "version": row["version"],
            "state": row["state"], "started_at": row["started_at"],
            "heartbeat_at": row["heartbeat_at"], "current_activity": row["current_activity"],
            "last_success_at": row["last_success_at"], "stop_reason": row["stop_reason"],
            "production_trading": "blocked",
        }
        age = max(0.0, float(self.clock()) - float(row["heartbeat_at"]))
        if row["state"] == "stopped":
            result["health"] = "stopped"
        elif age > 30:
            result["health"] = "stale"
        elif row["state"] == "degraded":
            result["health"] = "degraded"
        else:
            result["health"] = "healthy"
        return result

    def configure_job(self, *, enabled: bool, interval_seconds: float) -> dict[str, object]:
        if not isinstance(enabled, bool):
            raise ValueError("Service job enabled must be true or false")
        if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)):
            raise ValueError("Service job interval must be a number")
        interval = float(interval_seconds)
        if not 5 <= interval <= 3600:
            raise ValueError("Service job interval must be between 5 and 3600 seconds")
        at = float(self.clock())
        with self._lock, self.db:
            self.db.execute(
                """UPDATE service_jobs SET enabled=?,interval_seconds=?,revision=revision+1,
                   next_at=CASE WHEN ? THEN ? ELSE next_at END,current_activity='idle',last_error=''
                   WHERE id=?""",
                (int(enabled), interval, int(enabled), at, JOB_ID),
            )
        return self.job(JOB_ID)

    def job(self, job_id: str = JOB_ID) -> dict[str, object]:
        with self._lock:
            row = self.db.execute("SELECT * FROM service_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown service job")
        return _job_row(row)

    def jobs(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM service_jobs ORDER BY id").fetchall()
        return [_job_row(row) for row in rows]

    def job_due(self, at: float | None = None) -> bool:
        current = float(self.clock() if at is None else at)
        row = self.job(JOB_ID)
        return bool(row["enabled"] and float(row["next_at"]) <= current)

    def begin_job(self, instance_id: str) -> int:
        at = float(self.clock())
        with self._lock, self.db:
            self.db.execute(
                "UPDATE service_jobs SET current_activity='collecting',last_started_at=? WHERE id=?",
                (at, JOB_ID),
            )
            cursor = self.db.execute(
                """INSERT INTO service_job_runs(
                       job_id,service_instance_id,started_at,status,detail
                   ) VALUES(?,?,?,'running','')""",
                (JOB_ID, instance_id, at),
            )
        return int(cursor.lastrowid)

    def finish_job(self, run_id: int, *, success: bool, detail: str = "") -> None:
        at = float(self.clock())
        with self._lock, self.db:
            job = self.db.execute("SELECT * FROM service_jobs WHERE id=?", (JOB_ID,)).fetchone()
            if job is None:
                raise ValueError("Unknown service job")
            if success:
                failures = 0
                next_at = at + float(job["interval_seconds"])
                self.db.execute(
                    """UPDATE service_jobs SET consecutive_failures=0,current_activity='idle',
                       last_success_at=?,next_at=?,last_error='' WHERE id=?""",
                    (at, next_at, JOB_ID),
                )
            else:
                failures = int(job["consecutive_failures"]) + 1
                next_at = at + min(300, 5 * (2 ** min(failures - 1, 8)))
                self.db.execute(
                    """UPDATE service_jobs SET consecutive_failures=?,current_activity='degraded',
                       last_error_at=?,next_at=?,last_error=? WHERE id=?""",
                    (failures, at, next_at, _safe_detail(detail), JOB_ID),
                )
            self.db.execute(
                """UPDATE service_job_runs SET finished_at=?,status=?,detail=?
                   WHERE id=? AND status='running'""",
                (at, "completed" if success else "failed", _safe_detail(detail), run_id),
            )

    def close(self) -> None:
        with self._lock:
            self.db.close()


@dataclass
class BackgroundServiceEngine:
    data_dir: Path
    port: int = SERVICE_PORT
    source_factory: object | None = None
    signer: ServiceSigner | None = None
    clock: object = time.time
    monotonic: object = time.monotonic

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).resolve()
        self._blocked_signer = BlockedServiceSigner()
        self.owner_lock = ServiceOwnerLock(self.data_dir)
        self.store: BackgroundServiceStore | None = None
        self.recorder: ServiceEvidenceRecorder | None = None
        self.scheduler: MonitorScheduler | None = None
        self.research_worker: ResearchJobWorker | None = None
        self.paper_worker: PaperResearchWorker | None = None
        self.campaigns: ForwardCampaignStore | None = None
        self.alerts: EvidenceAlertStore | None = None
        self.instance_id: str | None = None
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.research_thread: threading.Thread | None = None
        self.funding_thread: threading.Thread | None = None
        self.stop_reason = "Service stopped"
        self._started = False
        self._last_wall = float(self.clock())
        self._last_monotonic = float(self.monotonic())
        self._research_faults: dict[str, dict[str, object] | None] = {
            "research_jobs": None,
            "paper_research": None,
        }

    def start(self) -> dict[str, object]:
        if self._started:
            return self.status()
        self.owner_lock.acquire()
        try:
            if self.signer is None:
                # Import and recover custody only after this process proves it
                # owns the data directory. A losing competitor must not lock or
                # mutate the real service signer's durable state.
                from .service_signing import ServiceSigningController

                self.signer = ServiceSigningController(self.data_dir)
            self.store = BackgroundServiceStore(self.data_dir, clock=self.clock)
            self.recorder = ServiceEvidenceRecorder(self.data_dir, clock=self.clock)
            self.recorder.recover_incomplete()
            self.research_worker = ResearchJobWorker(self.data_dir, clock=self.clock)
            self.paper_worker = PaperResearchWorker(
                self.data_dir, self.recorder, clock=self.clock
            )
            self.campaigns = ForwardCampaignStore(self.data_dir, clock=self.clock)
            self.campaigns.recover_running()
            self.alerts = EvidenceAlertStore(self.data_dir, clock=self.clock)
            self.signer.lock("Service start requires fresh trading unlock and authorization")
            kwargs = {
                "recorder": self.recorder,
                "campaigns": self.campaigns,
                # The process owner lock proves that a prior background service
                # is gone. Reuse its scheduler identity so an abrupt restart can
                # reclaim the durable monitor lease without waiting for its TTL.
                "owner": "background-service:" + self.store.data_directory_id,
            }
            if self.source_factory is not None:
                kwargs["source_factory"] = self.source_factory
            self.scheduler = MonitorScheduler(self.data_dir, **kwargs)
            self.instance_id = str(self.store.start_instance(port=self.port)["instance_id"])
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self._run, name="pumpwizard-background-service", daemon=True
            )
            self.research_thread = threading.Thread(
                target=self._run_research,
                name="pumpwizard-research-service",
                daemon=True,
            )
            self._started = True
            self.funding_thread = threading.Thread(
                target=self._run_funding, name='pumpwizard-wallet-funds', daemon=True,
            )
            self.thread.start()
            self.research_thread.start()
            self.funding_thread.start()
            return self.status()
        except Exception:
            if self.alerts is not None:
                self.alerts.close()
            if self.campaigns is not None:
                self.campaigns.close()
            if self.paper_worker is not None:
                self.paper_worker.close()
            if self.research_worker is not None:
                self.research_worker.close()
            if self.recorder is not None:
                self.recorder.close()
            if self.store is not None:
                self.store.close()
            self.owner_lock.release()
            raise

    def _run_funding(self) -> None:
        """Check funded wallets even with collection disabled or browsers closed."""
        from .session_funding import CHECK_INTERVAL, check
        from .manual_trades import TradeJournal
        library = StrategyLibrary(self.data_dir)
        journal = TradeJournal(self.data_dir)
        try:
            while not self.stop_event.is_set():
                held = {r[0] for r in journal.db.execute("SELECT workspace_id FROM session_workspace_allocations WHERE status='held'")}
                wallets = {(w['wallet_sender'], w['wallet_network']) for w in library.session_workspaces()
                           if w['mode'] == 'trade' and w['wallet_sender'] and w['id'] in held}
                for sender, network in wallets:
                    if self.stop_event.is_set():
                        break
                    try:
                        check(library, sender, network)
                    except (sqlite3.Error, TransferError):
                        continue
                self.stop_event.wait(CHECK_INTERVAL)
        finally:
            journal.close()
            library.close()

    def _run_research(self) -> None:
        """Keep bounded research and paper work off the collector/signing loop."""
        assert self.research_worker is not None and self.paper_worker is not None
        while not self.stop_event.is_set():
            try:
                self.research_worker.run_once()
                self._research_faults["research_jobs"] = None
            except Exception as exc:
                self._research_faults["research_jobs"] = {
                    "at": float(self.clock()), "message": _safe_detail(exc),
                }
            try:
                self.paper_worker.run_once(should_stop=self.stop_event.is_set)
                if self.campaigns is not None and self.recorder is not None:
                    self.campaigns.reconcile_limits(self.recorder)
                    if self.alerts is not None:
                        for campaign in self.campaigns.campaigns(
                            limit=500, recorder=self.recorder,
                        ):
                            self.alerts.evaluate(campaign)
                self._research_faults["paper_research"] = None
            except Exception as exc:
                # The two workers are isolated: a paper-cycle fault cannot
                # starve queued comparison work (and vice versa).
                self._research_faults["paper_research"] = {
                    "at": float(self.clock()), "message": _safe_detail(exc),
                }
            self.stop_event.wait(0.25)

    def _run(self) -> None:
        assert self.store is not None and self.scheduler is not None and self.instance_id is not None
        library = StrategyLibrary(self.data_dir)
        try:
            while not self.stop_event.is_set():
                try:
                    self._detect_resume()
                    success = False
                    now = float(self.clock())
                    job = self.store.job()
                    campaign_due = bool(
                        self.campaigns is not None
                        and self.campaigns.scan_demands(now=now)
                    )
                    campaign_backoff = bool(
                        job["consecutive_failures"]
                        and float(job["next_at"]) > now
                    )
                    if self.store.job_due(now) or campaign_due and not campaign_backoff:
                        run_id = self.store.begin_job(self.instance_id)
                        self.store.heartbeat(self.instance_id, activity="collecting")
                        try:
                            completed = self.scheduler.run_cycle(library)
                            failures = int(self.scheduler.last_cycle["failures"])
                            self.store.finish_job(
                                run_id, success=failures == 0,
                                detail=(f"{completed} strategy monitor cycle(s) completed"
                                        if failures == 0 else
                                        f"{failures} monitor scan group(s) failed; provider backoff active"),
                            )
                            success = failures == 0
                        except Exception as exc:
                            self.store.finish_job(
                                run_id, success=False, detail=_safe_detail(exc)
                            )
                    self._run_signer_cycle()
                    state = (
                        "degraded"
                        if self.store.job()["consecutive_failures"] else "running"
                    )
                    activity = "idle" if state == "running" else "collector backoff"
                    self.store.heartbeat(
                        self.instance_id, activity=activity, success=success, state=state
                    )
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                    # A concurrent local commit is transient. Keep the sole
                    # service worker alive and retry after the ordinary wake
                    # interval rather than requiring a process restart.
                    try:
                        self.store.heartbeat(
                            self.instance_id,
                            activity="local database busy; retrying",
                            state="degraded",
                        )
                    except sqlite3.OperationalError:
                        pass
                self.wake_event.wait(0.25)
                self.wake_event.clear()
        except Exception as exc:
            self.stop_reason = "Service worker failed: " + _safe_detail(exc)
            self.signer.lock("Service worker failure")
            self.store.heartbeat(self.instance_id, activity="worker failed", state="degraded")
        finally:
            self.scheduler.stop()
            library.release_monitor_lease(self.scheduler.owner)
            library.close()

    def _run_signer_cycle(self) -> None:
        assert self.signer is not None
        # Receipt reconciliation needs no private key.  Keep resolving original
        # signatures after restart while new signing authority remains locked.
        self.signer.reconcile()
        state = self.signer.state()
        if state.get("authorization_status") != "active":
            return
        self.signer.run_cycle()

    def _detect_resume(self) -> None:
        wall = float(self.clock())
        monotonic = float(self.monotonic())
        wall_elapsed = wall - self._last_wall
        monotonic_elapsed = monotonic - self._last_monotonic
        self._last_wall, self._last_monotonic = wall, monotonic
        if wall_elapsed > 30 or abs(wall_elapsed - monotonic_elapsed) > 30:
            assert self.signer is not None
            self.signer.lock("Sleep/resume or clock discontinuity requires fresh trading unlock")

    def configure_job(self, body: object) -> dict[str, object]:
        if not isinstance(body, dict) or set(body) != {"enabled", "interval_seconds"}:
            raise ValueError("Service job requires exactly enabled and interval_seconds")
        assert self.store is not None
        result = self.store.configure_job(
            enabled=body["enabled"], interval_seconds=body["interval_seconds"]
        )
        self.wake_event.set()
        return result

    def pause_entries(self) -> dict[str, object]:
        assert self.signer is not None
        return self.signer.pause_entries("Entries paused from authenticated dashboard")

    def resume_entries(self, body: object) -> dict[str, object]:
        assert self.signer is not None
        return self.signer.resume_entries(body)

    def revoke_trading(self) -> dict[str, object]:
        assert self.signer is not None
        return self.signer.revoke("Trading revoked from authenticated dashboard")

    def signing_overview(self, sender: str, network: str) -> dict[str, object]:
        public_key(sender)
        signer = self._signing_controller()
        enrolled = None
        enrollment_path = signer.vault.root / f"{sender}.json"
        if enrollment_path.exists():
            # A present but unreadable/tampered enrollment must remain a visible
            # fault, not be collapsed into the benign not-enrolled state.
            enrolled = signer.vault.metadata(sender)
        return {
            "trading": signer.state(),
            "enrollment": enrolled,
            "allowances": signer.allowances(sender, network),
        }

    def enroll_signer(self, body: object) -> dict[str, object]:
        if not isinstance(body, dict) or set(body) != {
            "sender", "network", "envelope", "service_unlock_secret",
        }:
            raise ValueError(
                "Service enrollment requires sender, network, envelope, and unlock secret"
            )
        secret = body["service_unlock_secret"]
        if not isinstance(secret, str):
            raise ValueError("Service enrollment unlock secret must be text")
        return self._signing_controller().enroll(
            body["envelope"], secret,
            expected_address=body["sender"], expected_network=body["network"],
        )

    def review_signing(self, library: StrategyLibrary, body: object) -> dict[str, object]:
        if not isinstance(body, dict) or set(body) != {
            "sender", "network", "session_id", "plan",
        }:
            raise ValueError("Service allowance review requires exact wallet, session, and plan")
        return self._signing_controller().review(
            library, sender=body["sender"], network=body["network"],
            session_id=body["session_id"], plan=body["plan"],
        )

    def authorize_signing(self, library: StrategyLibrary, body: object) -> dict[str, object]:
        if not isinstance(body, dict) or set(body) != {
            "sender", "network", "session_id", "review_id", "confirmation",
        }:
            raise ValueError("Service authorization requires the exact reviewed allowance")
        return self._signing_controller().authorize(
            library, sender=body["sender"], network=body["network"],
            session_id=body["session_id"], review_id=body["review_id"],
            confirmation=body["confirmation"],
        )

    def start_signing(self, body: object) -> dict[str, object]:
        if not isinstance(body, dict) or set(body) != {
            "sender", "network", "allowance_id", "service_unlock_secret", "confirmation",
        }:
            raise ValueError("Service signing start requires exact wallet, allowance, and unlock")
        if self.store is None:
            raise ValueError("Start the background service before unlocking auto trading")
        signer = self._signing_controller()
        allowance_id = body["allowance_id"]
        if not isinstance(allowance_id, str) or allowance_id not in {
            row["id"] for row in signer.allowances(body["sender"], body["network"])
        }:
            raise ValueError("Service allowance does not belong to this authenticated wallet")
        result = signer.start(
            allowance_id=allowance_id,
            unlock_secret=body["service_unlock_secret"],
            confirmation=body["confirmation"],
        )
        # Starting automatic execution also starts its source observations.
        # The signer remains bounded by the already approved session/allowance.
        try:
            self.configure_job({"enabled": True, "interval_seconds": self.store.job()["interval_seconds"]})
        except Exception:
            signer.lock("Monitoring startup failed; authorize auto trading again")
            raise
        return result

    def review_signing_acceptance(self, body: object) -> dict[str, object]:
        if not isinstance(body, dict) or set(body) != {
            "sender", "network", "allowance_id", "routes", "duration_seconds",
        }:
            raise ValueError(
                "Service acceptance review requires exact wallet, allowance, routes, and duration"
            )
        signer = self._signing_controller()
        allowance_id = body["allowance_id"]
        if not isinstance(allowance_id, str) or allowance_id not in {
            row["id"] for row in signer.allowances(body["sender"], body["network"])
        }:
            raise ValueError("Service allowance does not belong to this authenticated wallet")
        if not hasattr(signer, "acceptance_request"):
            raise ValueError("Service acceptance is unavailable in this runtime")
        return signer.acceptance_request(
            allowance_id=allowance_id,
            routes=body["routes"],
            duration_seconds=body["duration_seconds"],
        )

    def install_signing_acceptance(self, body: object) -> dict[str, object]:
        if not isinstance(body, dict) or set(body) != {
            "sender", "network", "payload", "signature",
        }:
            raise ValueError(
                "Service acceptance activation requires exact wallet, payload, and signature"
            )
        approval = body["payload"]
        if (
            not isinstance(approval, dict)
            or approval.get("sender") != body["sender"]
            or approval.get("network") != body["network"]
        ):
            raise ValueError("Service acceptance approval belongs to another wallet or network")
        signer = self._signing_controller()
        allowance_id = approval.get("allowance_id")
        if not isinstance(allowance_id, str) or allowance_id not in {
            row["id"] for row in signer.allowances(body["sender"], body["network"])
        }:
            raise ValueError("Service allowance does not belong to this authenticated wallet")
        if not hasattr(signer, "install_acceptance"):
            raise ValueError("Service acceptance is unavailable in this runtime")
        return signer.install_acceptance(approval, body["signature"])

    def signing_acceptance_evidence(
        self,
        sender: str,
        network: str,
        approval_id: str,
    ) -> dict[str, object]:
        signer = self._signing_controller()
        if not hasattr(signer, "acceptance_evidence"):
            raise ValueError("Service acceptance evidence is unavailable in this runtime")
        evidence = signer.acceptance_evidence(approval_id)
        allowance = evidence.get("allowance") if isinstance(evidence, dict) else None
        if not isinstance(allowance, dict) or allowance.get("sender") != sender:
            raise ValueError("Service acceptance evidence belongs to another wallet")
        if network not in GENESIS or allowance.get("genesis") != GENESIS[network]:
            raise ValueError("Service acceptance evidence belongs to another network")
        return evidence

    def _signing_controller(self):
        if self.signer is None or not all(hasattr(self.signer, name) for name in (
            "enroll", "review", "authorize", "start", "allowances", "vault",
        )):
            raise ValueError("Service wallet enrollment is unavailable in this runtime")
        return self.signer

    def request_shutdown(self, reason: str = "Authenticated service shutdown requested") -> dict[str, object]:
        assert self.signer is not None
        self.signer.lock(reason)
        self.stop_reason = _reason(reason)
        self.stop_event.set()
        self.wake_event.set()
        return {
            "requested": True,
            "notice": "Signing was locked before worker shutdown. Already submitted transactions were not cancelled.",
        }

    def stop(self, *, timeout: float = 5) -> dict[str, object]:
        if not self._started:
            return {"stopped": True, "detail": "Service was not running"}
        self.request_shutdown(self.stop_reason)
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=timeout)
        if (self.research_thread is not None
                and self.research_thread is not threading.current_thread()):
            self.research_thread.join(timeout=timeout)
        if self.funding_thread is not None and self.funding_thread is not threading.current_thread():
            self.funding_thread.join(timeout=timeout)
        clean = ((self.thread is None or not self.thread.is_alive())
                 and (self.research_thread is None or not self.research_thread.is_alive())
                 and (self.funding_thread is None or not self.funding_thread.is_alive()))
        assert self.store is not None and self.instance_id is not None
        if clean:
            self.store.stop_instance(self.instance_id, self.stop_reason)
        else:
            self.store.heartbeat(
                self.instance_id, activity="worker stop timed out", state="degraded"
            )
        status = self.store.status(self.instance_id)
        if clean:
            assert self.recorder is not None
            assert self.paper_worker is not None and self.research_worker is not None
            if self.alerts is not None:
                self.alerts.close()
            if self.campaigns is not None:
                self.campaigns.close()
            self.paper_worker.close()
            self.research_worker.close()
            self.recorder.compact()
            self.recorder.close()
            self.store.close()
            self.owner_lock.release()
            self._started = False
            self.store = None
            self.recorder = None
            self.scheduler = None
            self.paper_worker = None
            self.campaigns = None
            self.alerts = None
            self.research_worker = None
            self.research_thread = None
            self.funding_thread = None
        return {"stopped": clean, "service": status,
                "detail": "Service stopped cleanly" if clean else "Service worker did not stop in time"}

    def status(self) -> dict[str, object]:
        if self.store is None:
            store = BackgroundServiceStore(self.data_dir, clock=self.clock)
            registry = ResearchRegistry(self.data_dir, clock=self.clock)
            paper = PaperResearchStore(self.data_dir, clock=self.clock)
            campaigns = ForwardCampaignStore(self.data_dir, clock=self.clock)
            recorder = ServiceEvidenceRecorder(self.data_dir, clock=self.clock)
            alerts = EvidenceAlertStore(self.data_dir, clock=self.clock)
            try:
                signer = self.signer or self._blocked_signer
                return {"service": store.status(), "jobs": store.jobs(),
                        "trading": signer.state(),
                        "coverage": recorder.coverage(50),
                        "research_jobs": registry.jobs(), "paper_runs": paper.runs(),
                        "campaigns": campaigns.campaigns(recorder=recorder),
                        "alerts": alerts.alerts(state="open"),
                        "research_integrity": registry.integrity_summary(),
                        "research_worker": {"state": "stopped", "faults": {}}}
            finally:
                alerts.close()
                recorder.close()
                campaigns.close()
                paper.close()
                registry.close()
                store.close()
        assert self.recorder is not None
        paper_runs = self.paper_worker.store.runs() if self.paper_worker else []
        stalled = bool(
            self._started and self.research_thread is not None
            and not self.research_thread.is_alive() and not self.stop_event.is_set()
        )
        degraded = any(self._research_faults.values()) or any(
            row.get("state") == "interrupted" for row in paper_runs
        )
        return {
            "service": self.store.status(self.instance_id),
            "jobs": self.store.jobs(),
            "trading": (self.signer or self._blocked_signer).state(),
            "coverage": self.recorder.coverage(50),
            "research_jobs": (
                self.research_worker.registry.jobs() if self.research_worker else []
            ),
            "paper_runs": paper_runs,
            "campaigns": (
                self.campaigns.campaigns(recorder=self.recorder)
                if self.campaigns is not None else []
            ),
            "alerts": self.alerts.alerts(state="open") if self.alerts is not None else [],
            "research_integrity": (
                self.research_worker.registry.integrity_summary()
                if self.research_worker else None
            ),
            "research_worker": {
                "state": "stalled" if stalled else "degraded" if degraded else "healthy",
                "faults": {key: value for key, value in self._research_faults.items()
                           if value is not None},
            },
        }


def _job_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "schema": "pumpwizard.service-job/v1",
        "id": row["id"], "kind": row["kind"], "enabled": bool(row["enabled"]),
        "interval_seconds": row["interval_seconds"], "revision": row["revision"],
        "next_at": row["next_at"], "consecutive_failures": row["consecutive_failures"],
        "current_activity": row["current_activity"],
        "last_started_at": row["last_started_at"],
        "last_success_at": row["last_success_at"],
        "last_error_at": row["last_error_at"], "last_error": row["last_error"],
    }


def _reason(value: object) -> str:
    text = str(value).strip()
    return (text or "Service signing locked")[:300]


def _safe_detail(value: object) -> str:
    text = str(value).strip().splitlines()[0] if str(value).strip() else type(value).__name__
    if "?" in text and "://" in text:
        text = text.split("?", 1)[0] + "?[redacted]"
    return text[:300]
