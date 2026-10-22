"""Immutable, provenance-preserving research datasets.

The adapter copies library and service-recorder evidence into an independent
SQLite store.  Research remains reproducible after the operational recorder is
compacted and this module has no wallet, signer, order, or RPC dependency.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterable

from .decision import MarketQuote, RESEARCH_STYLES
from .library import StrategyLibrary
from .service_recorder import (
    RESEARCH_MAX_BYTES,
    RESEARCH_MAX_EVIDENCE,
    RESEARCH_MAX_RUNS,
    ServiceEvidenceRecorder,
)
from .strategies import canonical_json, parse_package


DATABASE_NAME = "research.sqlite"
SCHEMA_VERSION = 1
DATASET_SCHEMA = "pumpwizard.research-dataset/v1"
EXPORT_SCHEMA = "pumpwizard.research-export/v1"
DEFAULT_MAX_DECISIONS = 5_000
DEFAULT_MAX_FEATURES = 5_000
DEFAULT_MAX_OUTCOMES = 5_000
KNOWN_NETWORKS = frozenset({"mainnet-beta", "devnet", "localnet", "unknown"})


class ResearchDataError(ValueError):
    """Invalid, incomplete, conflicting, or unknown research data."""

    def __init__(self, message: str, *, code: str = "invalid_research_data"):
        super().__init__(message)
        self.code = code


class ResearchDataStore:
    """Append-only dataset storage with deterministic exports."""

    def __init__(self, data_dir: str | Path, *, clock=time.time):
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / DATABASE_NAME
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=15000")
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
                raise ResearchDataError(
                    "Research data was created by a newer PumpWizard version",
                    code="unsupported_schema",
                )
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL UNIQUE,
                    source_session_id TEXT NOT NULL,
                    strategy_sha256 TEXT NOT NULL,
                    first_event_at REAL NOT NULL,
                    last_event_at REAL NOT NULL,
                    first_known_at REAL,
                    last_known_at REAL,
                    eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
                    stored_at REAL NOT NULL,
                    manifest_json TEXT NOT NULL,
                    export_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS research_dataset_source
                    ON datasets(source_session_id,first_event_at,last_event_at,id);
                CREATE TABLE IF NOT EXISTS dataset_evidence (
                    dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    evidence_id TEXT NOT NULL,
                    mint TEXT NOT NULL,
                    event_at REAL NOT NULL,
                    first_received_at REAL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY(dataset_id,evidence_id)
                );
                CREATE INDEX IF NOT EXISTS research_dataset_evidence_time
                    ON dataset_evidence(dataset_id,event_at,evidence_id);
                CREATE INDEX IF NOT EXISTS research_evidence_lineage
                    ON dataset_evidence(evidence_id,dataset_id);
            """)
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def save(self, export: dict[str, Any]) -> dict[str, Any]:
        _validate_export(export)
        dataset = export["dataset"]
        payload = canonical_json(export)
        manifest_payload = canonical_json(dataset)
        records = export["records"]["observations"]
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                """INSERT OR IGNORE INTO datasets(
                       id,content_sha256,source_session_id,strategy_sha256,
                       first_event_at,last_event_at,first_known_at,last_known_at,
                       eligible,stored_at,manifest_json,export_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    dataset["id"], dataset["content_sha256"],
                    dataset["source"]["session_id"], dataset["source"]["strategy_sha256"],
                    dataset["ranges"]["event"]["first"],
                    dataset["ranges"]["event"]["last"],
                    dataset["ranges"]["first_known"]["first"],
                    dataset["ranges"]["first_known"]["last"],
                    int(dataset["eligibility"]["eligible"]), float(self.clock()),
                    manifest_payload, payload,
                ),
            )
            saved = self.db.execute(
                "SELECT export_json FROM datasets WHERE id=?", (dataset["id"],)
            ).fetchone()
            if saved is None or saved["export_json"] != payload:
                raise ResearchDataError("Research dataset identity collision", code="identity_conflict")
            for record in records:
                self.db.execute(
                    """INSERT OR IGNORE INTO dataset_evidence(
                           dataset_id,evidence_id,mint,event_at,first_received_at,record_json
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        dataset["id"], record["evidence_id"], record["mint"],
                        record["times"]["event_at"], record["times"]["first_received_at"],
                        canonical_json(record),
                    ),
                )
                collision = self.db.execute(
                    """SELECT record_json FROM dataset_evidence
                       WHERE dataset_id=? AND evidence_id=?""",
                    (dataset["id"], record["evidence_id"]),
                ).fetchone()
                if collision is None or collision["record_json"] != canonical_json(record):
                    raise ResearchDataError(
                        "Research evidence identity collision", code="identity_conflict"
                    )
        return json.loads(payload)

    def dataset(self, dataset_id: str) -> dict[str, Any]:
        ident = _identity(dataset_id, "dataset_id")
        with self._lock:
            row = self.db.execute(
                "SELECT manifest_json FROM datasets WHERE id=?", (ident,)
            ).fetchone()
        if row is None:
            raise ResearchDataError("Unknown research dataset", code="not_found")
        return json.loads(row["manifest_json"])

    def export(self, dataset_id: str) -> dict[str, Any]:
        ident = _identity(dataset_id, "dataset_id")
        with self._lock:
            row = self.db.execute(
                "SELECT export_json FROM datasets WHERE id=?", (ident,)
            ).fetchone()
        if row is None:
            raise ResearchDataError("Unknown research dataset", code="not_found")
        result = json.loads(row["export_json"])
        _validate_export(result)
        return result

    def list(self, *, source_session_id: str | None = None,
             limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ResearchDataError("Dataset list limit must be between 1 and 500")
        parameters: list[object] = []
        clause = ""
        if source_session_id is not None:
            clause = " WHERE source_session_id=?"
            parameters.append(_identity(source_session_id, "source_session_id"))
        parameters.append(limit)
        with self._lock:
            rows = self.db.execute(
                "SELECT manifest_json FROM datasets" + clause
                + " ORDER BY last_event_at DESC,id LIMIT ?",
                parameters,
            ).fetchall()
        return [json.loads(row["manifest_json"]) for row in rows]

    def point_in_time(self, dataset_id: str, *, known_by: float,
                      after: tuple[float, str] | None = None,
                      limit: int = 500) -> dict[str, Any]:
        """Return only evidence actually known by the requested timestamp."""
        ident = _identity(dataset_id, "dataset_id")
        cutoff = _finite(known_by, "known_by")
        if cutoff < 0:
            raise ResearchDataError("known_by cannot be negative")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ResearchDataError("Point-in-time limit must be between 1 and 1000")
        cursor_at, cursor_id = (-1.0, "")
        if after is not None:
            if not isinstance(after, tuple) or len(after) != 2 or not isinstance(after[1], str):
                raise ResearchDataError("Point-in-time cursor is invalid")
            cursor_at = _finite(after[0], "cursor event time")
            cursor_id = after[1]
        with self._lock:
            if self.db.execute("SELECT 1 FROM datasets WHERE id=?", (ident,)).fetchone() is None:
                raise ResearchDataError("Unknown research dataset", code="not_found")
            rows = self.db.execute(
                """SELECT event_at,evidence_id,record_json FROM dataset_evidence
                   WHERE dataset_id=? AND first_received_at IS NOT NULL
                     AND first_received_at<=?
                     AND (event_at>? OR (event_at=? AND evidence_id>?))
                   ORDER BY event_at,evidence_id LIMIT ?""",
                (ident, cutoff, cursor_at, cursor_at, cursor_id, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        return {
            "schema": "pumpwizard.research-point-in-time/v1",
            "dataset_id": ident,
            "known_by": cutoff,
            "records": [json.loads(row["record_json"]) for row in selected],
            "next_cursor": (
                [selected[-1]["event_at"], selected[-1]["evidence_id"]]
                if len(rows) > limit and selected else None
            ),
            "unknown_first_receipt_excluded": True,
        }

    def evidence_lineage(self, dataset_id: str, *, first_at: float,
                         last_at: float) -> list[str]:
        start, end = _finite(first_at, "lineage start"), _finite(last_at, "lineage end")
        if end < start:
            raise ResearchDataError("Lineage end cannot precede its start")
        with self._lock:
            rows = self.db.execute(
                """SELECT evidence_id FROM dataset_evidence
                   WHERE dataset_id=? AND event_at BETWEEN ? AND ?
                   ORDER BY evidence_id""",
                (_identity(dataset_id, "dataset_id"), start, end),
            ).fetchall()
        return [str(row["evidence_id"]) for row in rows]

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> "ResearchDataStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def freeze_service_dataset(
    library: StrategyLibrary,
    recorder: ServiceEvidenceRecorder,
    store: ResearchDataStore,
    *,
    source_session_id: str,
    network: str = "unknown",
    max_evidence: int = RESEARCH_MAX_EVIDENCE,
    max_runs: int = RESEARCH_MAX_RUNS,
    max_bytes: int = RESEARCH_MAX_BYTES,
    max_decisions: int = DEFAULT_MAX_DECISIONS,
    max_features: int = DEFAULT_MAX_FEATURES,
    max_outcomes: int = DEFAULT_MAX_OUTCOMES,
) -> dict[str, Any]:
    """Join one source session to recorder provenance and freeze it."""
    session_id = _identity(source_session_id, "source_session_id")
    if network not in KNOWN_NETWORKS:
        raise ResearchDataError("Research network is unsupported")
    for value, maximum, label in (
        (max_decisions, DEFAULT_MAX_DECISIONS, "decision"),
        (max_features, DEFAULT_MAX_FEATURES, "feature"),
        (max_outcomes, DEFAULT_MAX_OUTCOMES, "outcome"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ResearchDataError(f"Research {label} cap is invalid")

    snapshot = library.session_review_snapshot(
        session_id, observation_limit=max_evidence, decision_limit=max_decisions
    )
    counts = snapshot.get("source_counts")
    if not isinstance(counts, dict):
        raise ResearchDataError("Source snapshot does not declare completeness")
    expected = {
        "observations": len(snapshot["observations"]),
        "decisions": len(snapshot["decisions"]),
        "features": len(snapshot["feature_records"]),
        "outcomes": len(snapshot["outcomes"]),
    }
    for field, actual in expected.items():
        if int(counts.get(field, -1)) != actual:
            raise ResearchDataError(
                f"Research source exceeds the bounded {field} export; refusing truncation",
                code="source_too_large",
            )
    if expected["features"] > max_features or expected["outcomes"] > max_outcomes:
        raise ResearchDataError(
            "Research source exceeds the feature or outcome cap", code="source_too_large"
        )

    universe = sorted({str(row.get("mint")) for row in snapshot["observations"] if row.get("mint")})
    recorder_snapshot = recorder.research_snapshot(
        mints=universe,
        first_event_at=float(snapshot["first_at"]),
        last_event_at=float(snapshot["last_at"]),
        max_evidence=max_evidence,
        max_runs=max_runs,
        max_bytes=max_bytes,
    )
    export = build_dataset_export(snapshot, recorder_snapshot, network=network, caps={
        "evidence_rows": max_evidence,
        "decisions": max_decisions,
        "features": max_features,
        "outcomes": max_outcomes,
        "collection_runs": max_runs,
        "canonical_bytes": max_bytes,
    })
    if len(canonical_json(export).encode()) > max_bytes:
        raise ResearchDataError(
            f"Research export exceeds the {max_bytes}-byte canonical cap",
            code="source_too_large",
        )
    return store.save(export)


def build_dataset_export(snapshot: dict[str, Any], recorder_snapshot: dict[str, Any],
                         *, network: str,
                         caps: dict[str, int]) -> dict[str, Any]:
    """Pure deterministic adapter used by fixtures and the live store."""
    required = {
        "source_session_id", "strategy_digest", "strategy_package", "first_at", "last_at",
        "observations", "decisions", "outcomes", "feature_records",
        "decision_capture_complete",
    }
    if not isinstance(snapshot, dict) or not required <= set(snapshot):
        raise ResearchDataError("Source snapshot schema is incomplete")
    if recorder_snapshot.get("schema") != "pumpwizard.recorder-research-snapshot/v1":
        raise ResearchDataError("Unsupported recorder research snapshot", code="unsupported_schema")
    package = snapshot["strategy_package"]
    parsed = parse_package(package)
    if parsed.digest != snapshot["strategy_digest"]:
        raise ResearchDataError("Source strategy digest is stale or mismatched", code="identity_conflict")
    if network not in KNOWN_NETWORKS:
        raise ResearchDataError("Research network is unsupported")

    recorder_rows = recorder_snapshot.get("evidence")
    collection_runs = recorder_snapshot.get("collection_runs")
    if not isinstance(recorder_rows, list) or not isinstance(collection_runs, list):
        raise ResearchDataError("Recorder snapshot rows are invalid")
    exact: dict[str, list[dict[str, Any]]] = {}
    temporal: dict[tuple[str, float], list[dict[str, Any]]] = {}
    for raw in recorder_rows:
        if not isinstance(raw, dict) or not isinstance(raw.get("payload"), dict):
            raise ResearchDataError("Recorder evidence is invalid")
        normalized = _normalized_quote(raw["payload"])
        digest = _sha256(normalized)
        exact.setdefault(digest, []).append(raw)
        temporal.setdefault((normalized["mint"], normalized["observed_at"]), []).append(raw)

    observations = []
    duplicate_count = 0
    seen_identities: set[str] = set()
    ambiguous_pool_count = 0
    route_mismatch_count = 0
    stale_count = 0
    late_count = 0
    backfilled_count = 0
    invalid_time_count = 0
    missing_price_count = 0
    missing_unit_count = 0
    dead_or_illiquid_count = 0
    unknown_receipt_count = 0
    for source_row in snapshot["observations"]:
        if not isinstance(source_row, dict):
            raise ResearchDataError("Session observation must be an object")
        row = _normalized_quote(source_row)
        row_digest = _sha256(row)
        candidates = exact.get(row_digest, [])
        approximate = temporal.get((row["mint"], row["observed_at"]), [])
        match = candidates[0] if len(candidates) == 1 else None
        flags: list[str] = []
        if len(candidates) > 1:
            flags.append("duplicate-recorder-payload")
        pools = {(str(item.get("route")), str(item.get("pool"))) for item in approximate}
        if len(pools) > 1:
            flags.append("ambiguous-pool")
            ambiguous_pool_count += 1
        if approximate and (row["route"], row["pool"]) not in pools:
            flags.append("route-pool-mismatch")
            route_mismatch_count += 1

        evidence_id = str(match["id"]) if match is not None else "session:" + _sha256({
            "session_id": snapshot["source_session_id"], "observation": row,
        })
        if evidence_id in seen_identities:
            flags.append("duplicate-source-evidence")
            duplicate_count += 1
        seen_identities.add(evidence_id)
        receipt = match.get("first_received_at") if match is not None else None
        source_at = float(match.get("source_at")) if match is not None else float(row["source_at"])
        if receipt is None:
            flags.append("unknown-first-receipt")
            unknown_receipt_count += 1
        else:
            receipt = _finite(receipt, "first receipt")
            age = receipt - float(row["observed_at"])
            if age > 120:
                flags.append("stale-on-first-receipt")
                stale_count += 1
            if age > 300:
                flags.append("late-evidence")
                late_count += 1
            if age > 3600:
                flags.append("backfilled-evidence")
                backfilled_count += 1
            if receipt < float(row["observed_at"]) or source_at > receipt:
                flags.append("invalid-time-order")
                invalid_time_count += 1
        if row["price_usd"] <= 0 or row["price_native"] <= 0:
            flags.append("missing-price")
            missing_price_count += 1
        if "decimals" not in source_row or not isinstance(row["decimals"], int):
            flags.append("missing-unit-metadata")
            missing_unit_count += 1
        if row["liquidity_usd"] <= 0:
            flags.append("dead-or-illiquid")
            dead_or_illiquid_count += 1
        source_basis = (
            str(match.get("source_time_basis")) if match is not None
            else "unverified"
        )
        observations.append({
            "schema": "pumpwizard.research-evidence/v1",
            "evidence_id": evidence_id,
            "mint": row["mint"],
            "network": network,
            "route": row["route"],
            "pool": row["pool"],
            "decimals": row["decimals"],
            "units": {
                "price_usd": "USD/token", "price_native": "SOL/token",
                "market_cap_usd": "USD", "liquidity_usd": "USD",
                "liquidity_native": "SOL", "base_reserve": "token",
                "reserve_usd": "USD", "real_token_reserve": "token",
                "supply": "token",
            },
            "times": {
                "event_at": row["observed_at"], "source_at": source_at,
                "source_time_basis": source_basis,
                "first_received_at": receipt,
                "first_known_basis": (
                    "recorder-first-receipt" if receipt is not None else "unknown"
                ),
            },
            "provenance": {
                "source_session_id": snapshot["source_session_id"],
                "recorder_evidence_id": match.get("id") if match is not None else None,
                "collection_run_ids": list(match.get("run_ids", [])) if match else [],
                "provider": row["source"],
            },
            "quality_flags": sorted(set(flags)),
            "payload": row,
        })
    observations.sort(key=lambda item: (
        item["times"]["event_at"], item["mint"], item["evidence_id"]
    ))

    event_times = [float(row["times"]["event_at"]) for row in observations]
    known_times = [float(row["times"]["first_received_at"]) for row in observations
                   if row["times"]["first_received_at"] is not None]
    supported_family = str(parsed.effective_rules.get("entry_style", "momentum"))
    eligibility_reasons = []
    if supported_family not in RESEARCH_STYLES and supported_family not in {
        "momentum", "breakout", "pullback_reclaim", "slow_burn",
    }:
        eligibility_reasons.append("unsupported-strategy-family")
    if unknown_receipt_count:
        eligibility_reasons.append("unknown-first-receipt")
    if invalid_time_count:
        eligibility_reasons.append("invalid-time-order")
    if stale_count or late_count or backfilled_count:
        eligibility_reasons.append("stale-late-or-backfilled-evidence")
    if missing_price_count or missing_unit_count:
        eligibility_reasons.append("missing-price-or-unit")
    if ambiguous_pool_count or route_mismatch_count:
        eligibility_reasons.append("ambiguous-route-or-pool")
    if not snapshot["decision_capture_complete"]:
        eligibility_reasons.append("incomplete-decision-capture")
    if any(run.get("status") == "gap" for run in collection_runs):
        eligibility_reasons.append("collection-outage")

    quality_counts = {
        "observations": len(observations),
        "decisions": len(snapshot["decisions"]),
        "outcomes": len(snapshot["outcomes"]),
        "duplicates": duplicate_count,
        "stale": stale_count,
        "late": late_count,
        "backfilled": backfilled_count,
        "invalid_time_order": invalid_time_count,
        "missing_price": missing_price_count,
        "missing_unit": missing_unit_count,
        "ambiguous_pool": ambiguous_pool_count,
        "route_mismatch": route_mismatch_count,
        "dead_or_illiquid": dead_or_illiquid_count,
        "unknown_first_receipt": unknown_receipt_count,
        "collection_gaps": sum(run.get("status") == "gap" for run in collection_runs),
    }
    strategy_package = json.loads(canonical_json(package))
    record_bundle = {
        "observations": observations,
        "decisions": _canonical_rows(snapshot["decisions"], "decisions"),
        "features": _canonical_rows(snapshot["feature_records"], "features"),
        "outcomes": _canonical_rows(snapshot["outcomes"], "outcomes"),
        "collection_runs": _canonical_rows(collection_runs, "collection runs"),
    }
    manifest = {
        "schema": DATASET_SCHEMA,
        "source": {
            "session_id": snapshot["source_session_id"],
            "strategy_sha256": snapshot["strategy_digest"],
            "strategy_package": strategy_package,
            "chain": "solana",
            "network": network,
        },
        "ranges": {
            "event": {"first": min(event_times), "last": max(event_times)},
            "source": {
                "first": min(float(row["times"]["source_at"]) for row in observations),
                "last": max(float(row["times"]["source_at"]) for row in observations),
            },
            "first_known": {
                "first": min(known_times) if known_times else None,
                "last": max(known_times) if known_times else None,
            },
            "collection": {
                "first": min((float(run["started_at"]) for run in collection_runs), default=None),
                "last": max((float(run.get("received_at") or run["started_at"])
                             for run in collection_runs), default=None),
            },
        },
        "universe": sorted({row["mint"] for row in observations}),
        "evidence_identities": sorted(row["evidence_id"] for row in observations),
        "collection_run_identities": sorted(str(run["id"]) for run in collection_runs),
        "quality": {
            "counts": quality_counts,
            "gaps": [run for run in collection_runs if run.get("status") == "gap"],
        },
        "eligibility": {
            "eligible": not eligibility_reasons,
            "reasons": sorted(set(eligibility_reasons)),
            "supported_family": supported_family,
            "decision_capture_complete": bool(snapshot["decision_capture_complete"]),
        },
        "limits": dict(sorted(caps.items())),
    }
    core = {"dataset": manifest, "records": record_bundle}
    digest = _sha256(core)
    completed_manifest = {
        **manifest,
        "id": "research-" + digest[:32],
        "content_sha256": digest,
    }
    return {
        "schema": EXPORT_SCHEMA,
        "dataset": completed_manifest,
        "records": record_bundle,
    }


def _validate_export(value: dict[str, Any]) -> None:
    if not isinstance(value, dict) or value.get("schema") != EXPORT_SCHEMA:
        raise ResearchDataError("Unsupported research export schema", code="unsupported_schema")
    dataset, records = value.get("dataset"), value.get("records")
    if (not isinstance(dataset, dict) or dataset.get("schema") != DATASET_SCHEMA
            or not isinstance(records, dict) or not isinstance(records.get("observations"), list)):
        raise ResearchDataError("Research export is incomplete")
    provided = dataset.get("content_sha256")
    core_dataset = {key: item for key, item in dataset.items()
                    if key not in {"id", "content_sha256"}}
    actual = _sha256({"dataset": core_dataset, "records": records})
    if provided != actual or dataset.get("id") != "research-" + actual[:32]:
        raise ResearchDataError("Research export digest does not match its contents",
                                code="identity_conflict")


def _normalized_quote(record: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(canonical_json(MarketQuote.from_record(record).as_record()))
    except (TypeError, ValueError) as exc:
        raise ResearchDataError(f"Research observation is invalid: {exc}") from exc


def _canonical_rows(rows: Iterable[object], label: str) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ResearchDataError(f"Research {label} must contain objects")
        try:
            result.append(json.loads(canonical_json(row)))
        except (TypeError, ValueError) as exc:
            raise ResearchDataError(f"Research {label} contains non-canonical data") from exc
    return result


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ResearchDataError(f"{label} must be non-empty bounded text")
    return value.strip()


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchDataError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ResearchDataError(f"{label} must be a finite number")
    return result


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
