"""Durable, bounded evidence for service-owned public market scans."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Iterable

from .decision import MarketQuote


SCHEMA_VERSION = 3
DEFAULT_RETENTION_SECONDS = 30 * 24 * 60 * 60
DEFAULT_MAX_EVIDENCE = 50_000
DEFAULT_MAX_RUNS = 5_000
RESEARCH_MAX_EVIDENCE = 20_000
RESEARCH_MAX_RUNS = 5_000
RESEARCH_MAX_BYTES = 32 * 1024 * 1024
PROVIDER_DIAGNOSTICS_SCHEMA = "pumpwizard.provider-diagnostics/v1"
PROVIDER_DIAGNOSTIC_STATUSES = {
    "healthy", "empty", "partial", "stale", "failing", "quota_exhausted",
}
PROVIDER_REJECTION_CATEGORIES = {
    "empty_eligible_universe", "unsupported_shape", "invalid_identity_or_units",
    "stale_or_unknown_source_time", "unavailable_route", "request_failure",
    "exhausted_quota", "outside_requested_universe", "quality_gate",
}


class ServiceEvidenceRecorder:
    """Record provider attempts and normalized raw evidence without credentials."""

    def __init__(self, data_dir: Path, *, clock=time.time):
        self.path = Path(data_dir) / "service-evidence.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            row = self.db.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is not None and int(row["value"]) > SCHEMA_VERSION:
                raise ValueError("Service evidence was created by a newer PumpWizard version")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS collection_runs (
                    id TEXT PRIMARY KEY,
                    scan_key TEXT NOT NULL,
                    source TEXT NOT NULL,
                    minimum_cap REAL NOT NULL,
                    maximum_cap REAL NOT NULL,
                    requested_at REAL NOT NULL,
                    started_at REAL NOT NULL,
                    received_at REAL,
                    status TEXT NOT NULL CHECK(status IN ('collecting','complete','gap')),
                    quote_count INTEGER NOT NULL DEFAULT 0 CHECK(quote_count >= 0),
                    earliest_event_at REAL,
                    latest_event_at REAL,
                    earliest_source_at REAL,
                    latest_source_at REAL,
                    routes_json TEXT NOT NULL DEFAULT '[]',
                    pools_json TEXT NOT NULL DEFAULT '[]',
                    gap_reason TEXT NOT NULL DEFAULT '',
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL
                );
                CREATE INDEX IF NOT EXISTS collection_runs_recent
                    ON collection_runs(received_at DESC,started_at DESC,id);
                CREATE INDEX IF NOT EXISTS collection_runs_scan
                    ON collection_runs(scan_key,started_at DESC,id);
                CREATE TABLE IF NOT EXISTS raw_evidence (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    mint TEXT NOT NULL,
                    source TEXT NOT NULL,
                    route TEXT NOT NULL,
                    pool TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    source_at REAL NOT NULL,
                    first_received_at REAL NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS raw_evidence_recent
                    ON raw_evidence(first_received_at DESC,id);
                CREATE TABLE IF NOT EXISTS collection_run_evidence (
                    run_id TEXT NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
                    evidence_id TEXT NOT NULL REFERENCES raw_evidence(id) ON DELETE CASCADE,
                    PRIMARY KEY(run_id,evidence_id)
                );
                CREATE TABLE IF NOT EXISTS provider_request_consumers (
                    run_id TEXT NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
                    campaign_id TEXT NOT NULL,
                    arm_count INTEGER NOT NULL CHECK(arm_count > 0),
                    allocation_json TEXT NOT NULL,
                    PRIMARY KEY(run_id,campaign_id)
                );
                CREATE INDEX IF NOT EXISTS provider_consumer_campaign
                    ON provider_request_consumers(campaign_id,run_id);
            """)
            columns = {
                row["name"] for row in self.db.execute("PRAGMA table_info(collection_runs)")
            }
            additions = {
                "duration_ms": "REAL",
                "response_bytes": "INTEGER",
                "duplicate_rows": "INTEGER NOT NULL DEFAULT 0",
                "excluded_rows": "INTEGER NOT NULL DEFAULT 0",
                "error_class": "TEXT NOT NULL DEFAULT ''",
                "timeout_events": "INTEGER NOT NULL DEFAULT 0",
                "rate_limit_events": "INTEGER NOT NULL DEFAULT 0",
                "maximum_source_age_seconds": "REAL",
                "provider_response_bytes": "INTEGER",
                "diagnostics_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for name, definition in additions.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE collection_runs ADD COLUMN {name} {definition}")
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def recover_incomplete(self, *, at: float | None = None) -> int:
        """Make a crash gap explicit instead of implying continuous coverage."""
        recovered_at = _timestamp(self.clock() if at is None else at, "Recovery time")
        with self._lock, self.db:
            rows = self.db.execute(
                "SELECT id,scan_key FROM collection_runs WHERE status='collecting'"
            ).fetchall()
            for row in rows:
                failures = self._prior_failures(str(row["scan_key"])) + 1
                self.db.execute(
                    """UPDATE collection_runs SET status='gap',received_at=?,gap_reason=?,
                       consecutive_failures=?,next_retry_at=? WHERE id=?""",
                    (
                        recovered_at,
                        "Service stopped before this provider scan completed",
                        failures,
                        recovered_at + _backoff_seconds(failures),
                        row["id"],
                    ),
                )
        return len(rows)

    def begin_scan(self, *, minimum_cap: float, maximum_cap: float,
                   source: str, priority: Iterable[str] = (), limit: int,
                   requested_at: float | None = None,
                   started_at: float | None = None,
                   campaign_consumers: dict[str, int] | None = None) -> str:
        lower = _finite(minimum_cap, "Minimum market cap")
        upper = _finite(maximum_cap, "Maximum market cap")
        if lower < 0 or upper <= lower:
            raise ValueError("Collector market-cap band is invalid")
        if not isinstance(source, str) or not source or len(source) > 120:
            raise ValueError("Collector source identity is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Collector limit must be between 1 and 100")
        selected = sorted({str(item) for item in priority if item})
        if len(selected) > 30:
            raise ValueError("Collector priority universe exceeds 30 mints")
        requested = _timestamp(self.clock() if requested_at is None else requested_at,
                               "Collector request time")
        started = _timestamp(requested if started_at is None else started_at,
                             "Collector start time")
        if started < requested:
            raise ValueError("Collector start cannot precede its request")
        scan_key = _scan_key(lower, upper, source, selected, limit)
        consumers = _campaign_consumers(campaign_consumers)
        run_id = uuid.uuid4().hex
        with self._lock, self.db:
            self.db.execute(
                """INSERT INTO collection_runs(
                       id,scan_key,source,minimum_cap,maximum_cap,requested_at,started_at,status
                   ) VALUES(?,?,?,?,?,?,?,'collecting')""",
                (run_id, scan_key, source[:120], lower, upper, requested, started),
            )
            allocation = 1 / len(consumers) if consumers else 0.0
            for campaign_id, arm_count in consumers.items():
                detail = {
                    "schema": "pumpwizard.provider-allocation/v1",
                    "run_id": run_id, "campaign_id": campaign_id,
                    "arm_count": arm_count,
                    "shared_request_fraction": allocation,
                    "basis": "one provider request shared equally across consuming campaigns; "
                             "all arms inside a campaign reuse its evidence",
                }
                self.db.execute(
                    "INSERT INTO provider_request_consumers VALUES(?,?,?,?)",
                    (run_id, campaign_id, arm_count, _json(detail)),
                )
        return run_id

    def complete_scan(self, run_id: str, quotes: Iterable[MarketQuote], *,
                      received_at: float | None = None,
                      diagnostics: dict[str, object] | None = None) -> dict[str, object]:
        received = _timestamp(self.clock() if received_at is None else received_at,
                              "Collector receipt time")
        provider_diagnostics = _provider_diagnostics(diagnostics)
        diagnostic_stages = provider_diagnostics.get("stages", {})
        diagnostic_rejections = provider_diagnostics.get("rejections", {})
        measured_response_bytes = diagnostic_stages.get("response_bytes")
        provider_response_bytes = (
            int(measured_response_bytes)
            if provider_diagnostics and measured_response_bytes is not None else None
        )
        excluded_rows = sum(
            int(diagnostic_rejections.get(category, 0))
            for category in (
                "unsupported_shape", "invalid_identity_or_units",
                "stale_or_unknown_source_time", "unavailable_route", "quality_gate",
            )
        )
        records = []
        for quote in quotes:
            if not isinstance(quote, MarketQuote):
                raise ValueError("Collector evidence must contain MarketQuote records")
            record = quote.as_record()
            observed = _timestamp(record["observed_at"], "Observation event time")
            source_at = _timestamp(record.get("source_at") or observed, "Observation source time")
            canonical = json.dumps(record, allow_nan=False, ensure_ascii=False,
                                   sort_keys=True, separators=(",", ":"))
            fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
            records.append((quote, record, canonical, fingerprint, observed, source_at))
        with self._lock, self.db:
            run = self._collecting(run_id)
            if received < float(run["started_at"]):
                raise ValueError("Collector receipt cannot precede its start")
            evidence_ids = set()
            inserted = 0
            for quote, _record, canonical, fingerprint, observed, source_at in records:
                evidence_id = fingerprint
                cursor = self.db.execute(
                    """INSERT OR IGNORE INTO raw_evidence(
                           id,fingerprint,mint,source,route,pool,observed_at,source_at,
                           first_received_at,payload_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (evidence_id, fingerprint, quote.mint, quote.source, quote.route, quote.pool,
                     observed, source_at, received, canonical),
                )
                inserted += cursor.rowcount
                self.db.execute(
                    "INSERT OR IGNORE INTO collection_run_evidence(run_id,evidence_id) VALUES(?,?)",
                    (run_id, evidence_id),
                )
                evidence_ids.add(evidence_id)
            event_times = [item[4] for item in records]
            source_times = [item[5] for item in records]
            routes = sorted({item[0].route for item in records})
            pools = sorted({item[0].pool for item in records})
            response_bytes = sum(len(item[2].encode()) for item in records)
            maximum_source_age = max(
                (max(0.0, received - item[5]) for item in records), default=None,
            )
            self.db.execute(
                """UPDATE collection_runs SET status='complete',received_at=?,quote_count=?,
                   earliest_event_at=?,latest_event_at=?,earliest_source_at=?,latest_source_at=?,
                   routes_json=?,pools_json=?,gap_reason='',consecutive_failures=0,next_retry_at=NULL,
                   duration_ms=?,response_bytes=?,duplicate_rows=?,excluded_rows=?,error_class='',
                   timeout_events=0,rate_limit_events=0,maximum_source_age_seconds=?,
                   provider_response_bytes=?,diagnostics_json=?
                   WHERE id=?""",
                (received, len(evidence_ids), min(event_times, default=None),
                 max(event_times, default=None), min(source_times, default=None),
                 max(source_times, default=None), _json(routes), _json(pools),
                 max(0.0, (received - float(run["started_at"])) * 1000),
                 response_bytes, max(0, len(records) - inserted), excluded_rows,
                 maximum_source_age,
                 provider_response_bytes, _json(provider_diagnostics), run_id),
            )
        return self.run(run_id)

    def fail_scan(self, run_id: str, error: object, *,
                  received_at: float | None = None,
                  diagnostics: dict[str, object] | None = None) -> dict[str, object]:
        received = _timestamp(self.clock() if received_at is None else received_at,
                              "Collector failure time")
        detail = _safe_error(error)
        error_class = _error_class(error)
        provider_diagnostics = _provider_diagnostics(diagnostics)
        diagnostic_stages = provider_diagnostics.get("stages", {})
        diagnostic_rejections = provider_diagnostics.get("rejections", {})
        measured_response_bytes = diagnostic_stages.get("response_bytes")
        provider_response_bytes = (
            int(measured_response_bytes)
            if provider_diagnostics and measured_response_bytes is not None else None
        )
        excluded_rows = sum(
            int(diagnostic_rejections.get(category, 0))
            for category in (
                "unsupported_shape", "invalid_identity_or_units",
                "stale_or_unknown_source_time", "unavailable_route", "quality_gate",
            )
        )
        with self._lock, self.db:
            run = self._collecting(run_id)
            failures = self._prior_failures(str(run["scan_key"])) + 1
            self.db.execute(
                """UPDATE collection_runs SET status='gap',received_at=?,gap_reason=?,
                   consecutive_failures=?,next_retry_at=?,duration_ms=?,response_bytes=0,
                   duplicate_rows=0,excluded_rows=?,error_class=?,timeout_events=?,
                   rate_limit_events=?,provider_response_bytes=?,diagnostics_json=? WHERE id=?""",
                (received, detail, failures, received + _backoff_seconds(failures),
                 max(0.0, (received - float(run["started_at"])) * 1000), excluded_rows,
                 error_class, int(error_class == "timeout"), int(error_class == "rate_limit"),
                 provider_response_bytes, _json(provider_diagnostics), run_id),
            )
        return self.run(run_id)

    def provider_history(self, *, campaign_id: str | None = None,
                         after: tuple[float, str] | None = None,
                         limit: int = 100) -> dict[str, object]:
        """Return bounded, cursor-paginated and credential-free request telemetry."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("Provider history limit must be between 1 and 500")
        cursor_time, cursor_id = (-1.0, "")
        if after is not None:
            if (not isinstance(after, tuple) or len(after) != 2
                    or not isinstance(after[1], str)):
                raise ValueError("Provider history cursor is invalid")
            cursor_time = _timestamp(after[0], "Provider history cursor time")
            cursor_id = after[1]
        parameters: list[object] = [cursor_time, cursor_time, cursor_id]
        join = ""
        campaign_clause = ""
        if campaign_id is not None:
            ident = _identifier(campaign_id, "campaign_id")
            join = " JOIN provider_request_consumers AS consumer ON consumer.run_id=run.id"
            campaign_clause = " AND consumer.campaign_id=?"
            parameters.append(ident)
        parameters.append(limit + 1)
        with self._lock:
            rows = self.db.execute(
                "SELECT run.* FROM collection_runs AS run" + join
                + " WHERE (COALESCE(run.received_at,run.started_at)>? OR "
                  "(COALESCE(run.received_at,run.started_at)=? AND run.id>?))"
                + campaign_clause
                + " ORDER BY COALESCE(run.received_at,run.started_at),run.id LIMIT ?",
                parameters,
            ).fetchall()
            selected = rows[:limit]
            allocations = self._consumer_allocations([str(row["id"]) for row in selected])
        records = [_provider_row(row, allocations.get(str(row["id"]), []))
                   for row in selected]
        next_cursor = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_cursor = [float(last["received_at"] or last["started_at"]), str(last["id"])]
        return {
            "schema": "pumpwizard.provider-history/v1",
            "campaign_id": campaign_id,
            "records": records,
            "next_cursor": next_cursor,
        }

    def provider_usage(self, campaign_id: str) -> dict[str, object]:
        ident = _identifier(campaign_id, "campaign_id")
        with self._lock:
            row = self.db.execute(
                """SELECT COUNT(*) requests,
                          COALESCE(SUM(CAST(json_extract(
                              consumer.allocation_json,'$.shared_request_fraction'
                          ) AS REAL)),0) allocated_request_equivalents,
                          COALESCE(SUM(CAST(json_extract(
                              run.diagnostics_json,'$.stages.http_requests'
                          ) AS INTEGER)),0) http_reads,
                          COALESCE(SUM(json_extract(
                              run.diagnostics_json,'$.stages.http_requests'
                          ) IS NULL),0) unknown_http_read_requests,
                          COALESCE(SUM(run.response_bytes),0) response_bytes,
                          COALESCE(SUM(run.provider_response_bytes),0) provider_response_bytes,
                          COALESCE(SUM(run.provider_response_bytes IS NULL),0)
                              unknown_provider_response_byte_requests,
                          COALESCE(SUM(run.quote_count),0) result_rows,
                          COALESCE(SUM(run.duplicate_rows),0) duplicate_rows,
                          COALESCE(SUM(run.excluded_rows),0) excluded_rows,
                          COALESCE(SUM(run.timeout_events),0) timeout_events,
                          COALESCE(SUM(run.rate_limit_events),0) rate_limit_events,
                          COALESCE(SUM(run.status='gap'),0) errors,
                          MAX(run.maximum_source_age_seconds) maximum_source_age_seconds,
                          MAX(COALESCE(run.received_at,run.started_at)) last_request_at
                   FROM collection_runs AS run
                   JOIN provider_request_consumers AS consumer ON consumer.run_id=run.id
                   WHERE consumer.campaign_id=?""",
                (ident,),
            ).fetchone()
        return {key: row[key] for key in row.keys()}

    def next_scan_at(self, *, minimum_cap: float, maximum_cap: float, source: str,
                     priority: Iterable[str] = (), limit: int) -> float | None:
        """Return durable provider backoff for the exact normalized scan request."""
        key = _scan_key(minimum_cap, maximum_cap, source, priority, limit)
        with self._lock:
            row = self.db.execute(
                """SELECT next_retry_at FROM collection_runs
                   WHERE scan_key=? ORDER BY COALESCE(received_at,started_at) DESC,id DESC
                   LIMIT 1""",
                (key,),
            ).fetchone()
        return None if row is None or row["next_retry_at"] is None else float(row["next_retry_at"])

    def _consumer_allocations(self, run_ids: list[str]) -> dict[str, list[dict[str, object]]]:
        result: dict[str, list[dict[str, object]]] = {ident: [] for ident in run_ids}
        if not run_ids:
            return result
        for offset in range(0, len(run_ids), 500):
            page = run_ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in page)
            rows = self.db.execute(
                f"""SELECT run_id,allocation_json FROM provider_request_consumers
                    WHERE run_id IN ({placeholders}) ORDER BY run_id,campaign_id""",
                page,
            )
            for row in rows:
                result[str(row["run_id"])].append(json.loads(row["allocation_json"]))
        return result

    def run(self, run_id: str) -> dict[str, object]:
        with self._lock:
            row = self.db.execute("SELECT * FROM collection_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown collector run")
        return _coverage_row(row)

    def coverage(self, limit: int = 100) -> list[dict[str, object]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("Coverage limit must be between 1 and 500")
        with self._lock:
            rows = self.db.execute(
                """SELECT * FROM collection_runs
                   ORDER BY COALESCE(received_at,started_at) DESC,id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [_coverage_row(row) for row in rows]

    def raw_evidence(self, limit: int = 100) -> list[dict[str, object]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Evidence limit must be between 1 and 1000")
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM raw_evidence ORDER BY first_received_at DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{
            "id": row["id"], "mint": row["mint"], "source": row["source"],
            "route": row["route"], "pool": row["pool"],
            "observed_at": row["observed_at"], "source_at": row["source_at"],
            "first_received_at": row["first_received_at"],
            "payload": json.loads(row["payload_json"]),
        } for row in rows]

    def evidence_page(self, *, after: tuple[float, str] | None = None,
                      limit: int = 500,
                      known_by: float | None = None,
                      campaign_id: str | None = None) -> dict[str, object]:
        """Read a stable first-receipt page for research and paper consumers.

        The cursor is the last ``(first_received_at, id)`` pair.  Unknown
        historical receipt times never enter this recorder; adapters represent
        unmatched library observations explicitly with a null receipt time.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Evidence page limit must be between 1 and 1000")
        receipt_limit = None if known_by is None else _timestamp(known_by, "Known-by time")
        cursor_time, cursor_id = (-1.0, "")
        if after is not None:
            if (not isinstance(after, tuple) or len(after) != 2
                    or not isinstance(after[1], str)):
                raise ValueError("Evidence cursor must contain receipt time and identity")
            cursor_time = _timestamp(after[0], "Evidence cursor time")
            cursor_id = after[1]
        parameters: list[object] = [cursor_time, cursor_time, cursor_id]
        known_clause = ""
        if receipt_limit is not None:
            known_clause = " AND evidence.first_received_at<=?"
            parameters.append(receipt_limit)
        joins = ""
        campaign_clause = ""
        if campaign_id is not None:
            joins = (
                " JOIN collection_run_evidence AS linked ON linked.evidence_id=evidence.id"
                " JOIN provider_request_consumers AS consumer ON consumer.run_id=linked.run_id"
            )
            campaign_clause = " AND consumer.campaign_id=?"
            parameters.append(_identifier(campaign_id, "campaign_id"))
        parameters.append(limit + 1)
        with self._lock:
            rows = self.db.execute(
                "SELECT DISTINCT evidence.* FROM raw_evidence AS evidence" + joins
                + " WHERE (evidence.first_received_at>? OR "
                  "(evidence.first_received_at=? AND evidence.id>?))"
                + known_clause
                + campaign_clause
                + " ORDER BY evidence.first_received_at,evidence.id LIMIT ?",
                parameters,
            ).fetchall()
            selected = rows[:limit]
            run_map = self._run_ids([str(row["id"]) for row in selected])
        records = [self._research_evidence_row(row, run_map) for row in selected]
        next_cursor = None
        if len(rows) > limit and selected:
            next_cursor = [selected[-1]["first_received_at"], selected[-1]["id"]]
        return {
            "schema": "pumpwizard.recorder-evidence-page/v1",
            "records": records,
            "next_cursor": next_cursor,
        }

    def research_snapshot(self, *, mints: Iterable[str], first_event_at: float,
                          last_event_at: float,
                          max_evidence: int = RESEARCH_MAX_EVIDENCE,
                          max_runs: int = RESEARCH_MAX_RUNS,
                          max_bytes: int = RESEARCH_MAX_BYTES) -> dict[str, object]:
        """Copy one bounded recorder range while retention is locked out.

        This is deliberately a transfer object rather than a live view.  Once
        returned, compaction may delete recorder rows without changing the
        caller's canonical copy.
        """
        start = _timestamp(first_event_at, "Research range start")
        end = _timestamp(last_event_at, "Research range end")
        if end < start:
            raise ValueError("Research range end cannot precede its start")
        selected_mints = sorted({str(mint) for mint in mints if isinstance(mint, str) and mint})
        if not selected_mints or len(selected_mints) > 2_000:
            raise ValueError("Research snapshot requires 1–2000 exact mints")
        if (isinstance(max_evidence, bool) or not isinstance(max_evidence, int)
                or not 1 <= max_evidence <= RESEARCH_MAX_EVIDENCE):
            raise ValueError("Research evidence cap is invalid")
        if (isinstance(max_runs, bool) or not isinstance(max_runs, int)
                or not 1 <= max_runs <= RESEARCH_MAX_RUNS):
            raise ValueError("Research collection-run cap is invalid")
        if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
                or not 1024 <= max_bytes <= RESEARCH_MAX_BYTES):
            raise ValueError("Research byte cap is invalid")
        placeholders = ",".join("?" for _ in selected_mints)
        with self._lock:
            rows = self.db.execute(
                f"""SELECT * FROM raw_evidence
                    WHERE observed_at BETWEEN ? AND ? AND mint IN ({placeholders})
                    ORDER BY observed_at,mint,first_received_at,id LIMIT ?""",
                [start, end, *selected_mints, max_evidence + 1],
            ).fetchall()
            if len(rows) > max_evidence:
                raise ValueError(
                    f"Research snapshot exceeds the {max_evidence}-evidence-row cap"
                )
            run_map = self._run_ids([str(row["id"]) for row in rows])
            run_ids = sorted({ident for values in run_map.values() for ident in values})
            if len(run_ids) > max_runs:
                raise ValueError(
                    f"Research snapshot exceeds the {max_runs}-collection-run cap"
                )
            linked_runs = []
            if run_ids:
                run_placeholders = ",".join("?" for _ in run_ids)
                linked_runs = self.db.execute(
                    f"""SELECT * FROM collection_runs WHERE id IN ({run_placeholders})
                        ORDER BY started_at,id""",
                    run_ids,
                ).fetchall()
            # Gaps have no linked evidence. Preserve every overlapping attempt
            # for the selected cap bands so absence remains visible.
            gap_rows = self.db.execute(
                """SELECT * FROM collection_runs
                   WHERE status='gap' AND started_at<=?
                     AND COALESCE(received_at,started_at)>=?
                   ORDER BY started_at,id LIMIT ?""",
                (end, start, max_runs + 1),
            ).fetchall()
            combined = {str(row["id"]): row for row in [*linked_runs, *gap_rows]}
            if len(combined) > max_runs or len(gap_rows) > max_runs:
                raise ValueError(
                    f"Research snapshot exceeds the {max_runs}-collection-run cap"
                )
            evidence = [self._research_evidence_row(row, run_map) for row in rows]
            runs = [_coverage_row(combined[key]) for key in sorted(
                combined, key=lambda item: (float(combined[item]["started_at"]), item)
            )]
            transfer = {
                "schema": "pumpwizard.recorder-research-snapshot/v1",
                "range": {"first_event_at": start, "last_event_at": end},
                "mints": selected_mints,
                "evidence": evidence,
                "collection_runs": runs,
            }
            encoded = _json(transfer).encode()
            if len(encoded) > max_bytes:
                raise ValueError(
                    f"Research snapshot exceeds the {max_bytes}-byte canonical cap"
                )
            transfer["canonical_bytes"] = len(encoded)
            return transfer

    def _run_ids(self, evidence_ids: list[str]) -> dict[str, list[str]]:
        if not evidence_ids:
            return {}
        result = {ident: [] for ident in evidence_ids}
        # SQLite builds commonly permit at least 999 parameters; page this
        # internal join so the public 20k-row cap stays portable.
        for offset in range(0, len(evidence_ids), 500):
            page = evidence_ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in page)
            rows = self.db.execute(
                f"""SELECT evidence_id,run_id FROM collection_run_evidence
                    WHERE evidence_id IN ({placeholders}) ORDER BY evidence_id,run_id""",
                page,
            )
            for row in rows:
                result[str(row["evidence_id"])].append(str(row["run_id"]))
        return result

    @staticmethod
    def _research_evidence_row(row: sqlite3.Row,
                               run_map: dict[str, list[str]]) -> dict[str, object]:
        payload = json.loads(row["payload_json"])
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if metadata.get("source_time_basis") == "provider":
            source_time_basis = "provider"
        elif metadata.get("source_time_independent") is True:
            source_time_basis = "independent"
        elif float(row["source_at"]) == float(row["observed_at"]):
            source_time_basis = "inferred-observed-at"
        else:
            source_time_basis = "unverified"
        return {
            "id": row["id"],
            "fingerprint": row["fingerprint"],
            "mint": row["mint"],
            "source": row["source"],
            "route": row["route"],
            "pool": row["pool"],
            "observed_at": row["observed_at"],
            "source_at": row["source_at"],
            "source_time_basis": source_time_basis,
            "first_received_at": row["first_received_at"],
            "first_known_basis": "recorder-first-receipt",
            "run_ids": list(run_map.get(str(row["id"]), [])),
            "payload": payload,
        }

    def compact(self, *, now: float | None = None,
                retention_seconds: int = DEFAULT_RETENTION_SECONDS,
                max_evidence: int = DEFAULT_MAX_EVIDENCE,
                max_runs: int = DEFAULT_MAX_RUNS) -> dict[str, int]:
        current = _timestamp(self.clock() if now is None else now, "Compaction time")
        if not 3600 <= retention_seconds <= 366 * 24 * 60 * 60:
            raise ValueError("Evidence retention must be between one hour and 366 days")
        if not 100 <= max_evidence <= 1_000_000 or not 100 <= max_runs <= 100_000:
            raise ValueError("Evidence retention row limits are invalid")
        cutoff = current - retention_seconds
        with self._lock, self.db:
            before_runs = self.db.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
            before_evidence = self.db.execute("SELECT COUNT(*) FROM raw_evidence").fetchone()[0]
            self.db.execute(
                "DELETE FROM collection_runs WHERE status!='collecting' AND received_at<?", (cutoff,)
            )
            self.db.execute("""
                DELETE FROM collection_runs WHERE id IN (
                    SELECT id FROM collection_runs WHERE status!='collecting'
                    ORDER BY COALESCE(received_at,started_at) DESC,id DESC LIMIT -1 OFFSET ?
                )
            """, (max_runs,))
            self.db.execute(
                "DELETE FROM raw_evidence WHERE id NOT IN (SELECT evidence_id FROM collection_run_evidence)"
            )
            self.db.execute("""
                DELETE FROM raw_evidence WHERE id IN (
                    SELECT id FROM raw_evidence ORDER BY first_received_at DESC,id DESC LIMIT -1 OFFSET ?
                )
            """, (max_evidence,))
            after_runs = self.db.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
            after_evidence = self.db.execute("SELECT COUNT(*) FROM raw_evidence").fetchone()[0]
        return {"runs_removed": before_runs - after_runs,
                "evidence_removed": before_evidence - after_evidence}

    def _collecting(self, run_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM collection_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown collector run")
        if row["status"] != "collecting":
            raise ValueError("Collector run is already final")
        return row

    def _prior_failures(self, scan_key: str) -> int:
        row = self.db.execute(
            """SELECT consecutive_failures FROM collection_runs
               WHERE scan_key=? AND status!='collecting'
               ORDER BY COALESCE(received_at,started_at) DESC,id DESC LIMIT 1""",
            (scan_key,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def close(self) -> None:
        with self._lock:
            self.db.close()


def _coverage_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "schema": "pumpwizard.collector-coverage/v1",
        "id": row["id"], "scan_key": row["scan_key"], "source": row["source"],
        "minimum_cap": row["minimum_cap"], "maximum_cap": row["maximum_cap"],
        "requested_at": row["requested_at"], "started_at": row["started_at"],
        "received_at": row["received_at"], "status": row["status"],
        "quote_count": row["quote_count"], "earliest_event_at": row["earliest_event_at"],
        "latest_event_at": row["latest_event_at"],
        "earliest_source_at": row["earliest_source_at"],
        "latest_source_at": row["latest_source_at"],
        "routes": json.loads(row["routes_json"]), "pools": json.loads(row["pools_json"]),
        "gap_count": 1 if row["status"] == "gap" else 0,
        "gap_reason": row["gap_reason"],
        "consecutive_failures": row["consecutive_failures"],
        "next_retry_at": row["next_retry_at"],
        "telemetry": {
            "duration_ms": row["duration_ms"],
            "response_bytes": row["response_bytes"],
            "provider_response_bytes": row["provider_response_bytes"],
            "duplicate_rows": row["duplicate_rows"],
            "excluded_rows": row["excluded_rows"],
            "error_class": row["error_class"] or None,
            "timeout_events": row["timeout_events"],
            "rate_limit_events": row["rate_limit_events"],
            "maximum_source_age_seconds": row["maximum_source_age_seconds"],
        },
        "provider_diagnostics": _stored_diagnostics(row["diagnostics_json"]),
    }


def _provider_row(row: sqlite3.Row,
                  allocations: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": "pumpwizard.provider-request/v1",
        "id": row["id"], "scan_key": row["scan_key"], "source": row["source"],
        "requested_at": row["requested_at"], "started_at": row["started_at"],
        "finished_at": row["received_at"], "duration_ms": row["duration_ms"],
        "status": "success" if row["status"] == "complete" else row["status"],
        "result_count": row["quote_count"], "response_bytes": row["response_bytes"],
        "provider_response_bytes": row["provider_response_bytes"],
        "duplicate_rows": row["duplicate_rows"], "excluded_rows": row["excluded_rows"],
        "error_class": row["error_class"] or None,
        "error": row["gap_reason"] or None,
        "retry_count": row["consecutive_failures"], "next_retry_at": row["next_retry_at"],
        "timeout_events": row["timeout_events"],
        "rate_limit_events": row["rate_limit_events"],
        "maximum_source_age_seconds": row["maximum_source_age_seconds"],
        "first_received_at": row["received_at"],
        "allocations": allocations,
        "diagnostics": _stored_diagnostics(row["diagnostics_json"]),
    }


def _provider_diagnostics(value: dict[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict) or value.get("schema") != PROVIDER_DIAGNOSTICS_SCHEMA:
        raise ValueError("Provider diagnostics schema is invalid")
    provider = value.get("provider")
    status = value.get("status")
    if (not isinstance(provider, str) or not provider or len(provider) > 120
            or status not in PROVIDER_DIAGNOSTIC_STATUSES):
        raise ValueError("Provider diagnostics identity or status is invalid")
    started = _timestamp(value.get("started_at"), "Provider diagnostic start")
    finished = _timestamp(value.get("finished_at"), "Provider diagnostic finish")
    if finished < started:
        raise ValueError("Provider diagnostic finish precedes its start")
    bounds = value.get("bounds")
    stages = value.get("stages")
    rejections = value.get("rejections")
    provenance = value.get("provenance")
    readiness = value.get("readiness")
    if not all(isinstance(item, dict) for item in (
            bounds, stages, rejections, provenance, readiness)):
        raise ValueError("Provider diagnostics sections are invalid")
    numeric_bounds = {
        "maximum_http_requests": (1, 100),
        "maximum_elapsed_seconds": (0, 600),
        "maximum_response_bytes": (1_024, 64_000_000),
        "maximum_concurrency": (1, 4),
    }
    checked_bounds: dict[str, float] = {}
    for key, (minimum, maximum) in numeric_bounds.items():
        raw = bounds.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("Provider diagnostic bound is invalid")
        number = float(raw)
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise ValueError("Provider diagnostic bound is invalid")
        checked_bounds[key] = number
    required_stages = {
        "discovery_requests", "listing_rows", "unique_listings",
        "eligible_candidates", "selected_records", "selected_detail_requests",
        "returned_details", "normalized_quotes", "http_requests", "response_bytes",
    }
    for key in required_stages:
        raw = stages.get(key)
        if key == "response_bytes" and raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 10_000_000:
            raise ValueError("Provider diagnostic stage count is invalid")
    if (stages["http_requests"] > checked_bounds["maximum_http_requests"]
            or (stages["response_bytes"] is not None
                and stages["response_bytes"] > checked_bounds["maximum_response_bytes"])):
        raise ValueError("Provider diagnostics exceed their declared bounds")
    for key, raw in rejections.items():
        if (key not in PROVIDER_REJECTION_CATEGORIES or isinstance(raw, bool)
                or not isinstance(raw, int) or not 0 <= raw <= 10_000_000):
            raise ValueError("Provider diagnostic rejection count is invalid")
    if not isinstance(readiness.get("ready"), bool):
        raise ValueError("Provider readiness is invalid")
    routes = readiness.get("routes")
    reasons = readiness.get("reasons")
    if (not isinstance(routes, list) or len(routes) > 4
            or any(route not in ("curve", "amm") for route in routes)
            or not isinstance(reasons, list) or len(reasons) > 20
            or any(not isinstance(reason, str) or len(reason) > 100 for reason in reasons)):
        raise ValueError("Provider readiness detail is invalid")
    encoded = _json(value)
    if len(encoded.encode()) > 32_768 or "://" in encoded:
        raise ValueError("Provider diagnostics are oversized or contain a URL")
    # Round-trip to detach mutable caller-owned dictionaries before persistence.
    return json.loads(encoded)


def _stored_diagnostics(value: object) -> dict[str, object] | None:
    if not isinstance(value, str) or value == "{}":
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _scan_key(minimum_cap: object, maximum_cap: object, source: object,
              priority: Iterable[str], limit: object) -> str:
    lower = _finite(minimum_cap, "Minimum market cap")
    upper = _finite(maximum_cap, "Maximum market cap")
    selected = sorted({str(item) for item in priority if item})
    return _digest({
        "minimum_cap": lower, "maximum_cap": upper, "source": str(source),
        "priority": selected, "limit": int(limit),
    })


def _campaign_consumers(value: dict[str, int] | None) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 100:
        raise ValueError("Provider request campaign allocation is invalid")
    result = {}
    for campaign_id, arm_count in value.items():
        ident = _identifier(campaign_id, "campaign_id")
        if isinstance(arm_count, bool) or not isinstance(arm_count, int) or not 1 <= arm_count <= 3:
            raise ValueError("Provider request campaign arm count is invalid")
        result[ident] = arm_count
    return dict(sorted(result.items()))


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 160:
        raise ValueError(f"{label} is invalid")
    return value.strip()


def _error_class(error: object) -> str:
    text = (type(error).__name__ + " " + str(error)).lower()
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "rate_limit"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "network" in text or "connection" in text or "dns" in text:
        return "network"
    return "provider_error"


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _timestamp(value: object, label: str) -> float:
    result = _finite(value, label)
    if result < 0:
        raise ValueError(f"{label} cannot be negative")
    return result


def _backoff_seconds(failures: int) -> float:
    return float(min(300, 5 * (2 ** max(0, min(failures - 1, 8)))))


def _safe_error(error: object) -> str:
    text = str(error).strip() or type(error).__name__
    # Provider errors are persisted for operators, but keep one bounded line and
    # avoid accidentally recording URL query credentials.
    text = text.splitlines()[0]
    if "?" in text and "://" in text:
        text = text.split("?", 1)[0] + "?[redacted]"
    return text[:300]
