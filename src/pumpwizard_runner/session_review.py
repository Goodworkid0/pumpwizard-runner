"""Deterministic, offline session evidence review.

This module deliberately has no wallet, RPC, signer, order, scheduler, or web
imports.  It freezes caller-supplied session evidence in a separate store and
replays one supported strategy family without weakening the production replay
eligibility checks in :mod:`evaluation`.
"""

from __future__ import annotations

from collections import defaultdict
import copy
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable

from .decision import DecisionEngine, MarketQuote, summarize
from .portfolio_replay import (
    COMPARISON_SCHEMA,
    compare_portfolios,
)
from .strategies import canonical_json, content_hash, parse_package


DATASET_SCHEMA = "pumpwizard.session-review-dataset/v1"
REPORT_SCHEMA = "pumpwizard.session-review-report/v1"
DRAFT_SCHEMA = "pumpwizard.session-review-draft/v1"
FEATURE_SCHEMA = "pumpwizard.session-decision-features/v1"
ENGINE_VERSION = "pumpwizard-session-review/1"
REPLAY_REQUEST_SCHEMA = "pumpwizard.session-review-replay-request/v3"
REPLAY_REPORT_SCHEMA = "pumpwizard.session-review-replay-report/v3"
LEGACY_REPLAY_REPORT_SCHEMA = "pumpwizard.session-review-replay-report/v2"
DATABASE_NAME = "session-reviews.sqlite"
DEFAULT_HORIZON_SECONDS = 1_800
ENDPOINT_GRACE_SECONDS = 120
MINIMUM_VALIDATION_SAMPLES = 3
MINIMUM_COVERAGE_FRACTION = 0.70


class SessionReviewError(ValueError):
    """The requested review would be unsupported, ambiguous, or non-reproducible."""


def _finite(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SessionReviewError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise SessionReviewError(f"{field} must be {'positive' if positive else 'finite'}")
    return result


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SessionReviewError(f"{field} must be non-empty text")
    return value.strip()


def _cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise SessionReviewError("Session review cancelled; no completed comparison saved")


def _assert_source(row: dict[str, Any], source_session_id: str, field: str) -> None:
    retained = row.get("instance_id", row.get("session_id"))
    if retained is not None and retained != source_session_id:
        raise SessionReviewError(f"{field} contains evidence from another session")


def _canonical_rows(rows: Iterable[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SessionReviewError(f"{field} item {index + 1} must be an object")
        # Canonical round-trip both deep-copies and rejects NaN/Infinity.
        try:
            result.append(json.loads(canonical_json(row)))
        except (TypeError, ValueError) as exc:
            raise SessionReviewError(f"{field} item {index + 1} is not canonical JSON") from exc
    return result


class SessionReviewStore:
    """Append-only storage for frozen datasets and completed comparisons."""

    def __init__(self, data_dir: str | Path):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / DATABASE_NAME
        self._connection = sqlite3.connect(self.path, timeout=10)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=10000")
        with self._connection:
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY,
                    source_session_id TEXT NOT NULL,
                    strategy_sha256 TEXT NOT NULL,
                    first_at REAL NOT NULL,
                    last_at REAL NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS session_review_dataset_source
                    ON datasets(source_session_id,first_at,last_at,id);
                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    source_strategy_sha256 TEXT NOT NULL,
                    search_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS session_review_report_dataset
                    ON reports(dataset_id,id);
                CREATE TABLE IF NOT EXISTS holdout_locks (
                    dataset_id TEXT PRIMARY KEY REFERENCES datasets(id),
                    search_sha256 TEXT NOT NULL,
                    report_id TEXT NOT NULL REFERENCES reports(id)
                );
            """)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SessionReviewStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def save_dataset(self, dataset: dict[str, Any]) -> dict[str, Any]:
        payload = canonical_json(dataset)
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO datasets VALUES (?,?,?,?,?,?)",
                (dataset["id"], dataset["source_session_id"], dataset["strategy_sha256"],
                 dataset["period"]["first_at"], dataset["period"]["last_at"], payload),
            )
            saved = self._connection.execute(
                "SELECT payload_json FROM datasets WHERE id=?", (dataset["id"],)
            ).fetchone()
        if saved is None or saved["payload_json"] != payload:
            raise SessionReviewError("Frozen dataset identity collision")
        return json.loads(saved["payload_json"])

    def dataset(self, dataset_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload_json FROM datasets WHERE id=?", (dataset_id,)
        ).fetchone()
        if row is None:
            raise SessionReviewError("Unknown frozen session-review dataset")
        return json.loads(row["payload_json"])

    def report(self, report_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT payload_json FROM reports WHERE id=?", (report_id,)
        ).fetchone()
        if row is None:
            raise SessionReviewError("Unknown completed session review")
        report = json.loads(row["payload_json"])
        if report.get("schema") == REPORT_SCHEMA:
            # Every persisted v1 report was produced by the exposed path and
            # sealed in holdout_locks. Annotate older artifacts on read without
            # rewriting their stored payload.
            report.setdefault("holdout_state", "exposed")
        return report

    def reports(self, source_session_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute("""
            SELECT report.payload_json FROM reports AS report
            JOIN datasets AS dataset ON dataset.id=report.dataset_id
            WHERE dataset.source_session_id=? ORDER BY dataset.last_at DESC,report.id
        """, (source_session_id,))
        reports = [json.loads(row["payload_json"]) for row in rows]
        for report in reports:
            if report.get("schema") == REPORT_SCHEMA:
                report.setdefault("holdout_state", "exposed")
        return reports

    def locked_report(self, dataset_id: str, search_sha256: str) -> dict[str, Any] | None:
        """Reject a changed search before evaluating an already exposed holdout."""
        row = self._connection.execute(
            "SELECT search_sha256,report_id FROM holdout_locks WHERE dataset_id=?",
            (dataset_id,),
        ).fetchone()
        if row is None:
            return None
        if row["search_sha256"] != search_sha256:
            raise SessionReviewError(
                "This dataset's holdout was already exposed by a different search; "
                "freeze new future evidence before tuning again"
            )
        return self.report(row["report_id"])

    def save_report(self, report: dict[str, Any]) -> dict[str, Any]:
        """Atomically publish a complete report and seal its holdout search."""
        payload = canonical_json(report)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            locked = self._connection.execute(
                "SELECT search_sha256,report_id FROM holdout_locks WHERE dataset_id=?",
                (report["dataset_id"],),
            ).fetchone()
            if locked is not None and locked["search_sha256"] != report["search_sha256"]:
                raise SessionReviewError(
                    "This dataset's holdout was already exposed by a different search; "
                    "freeze new future evidence before tuning again"
                )
            self._connection.execute(
                "INSERT OR IGNORE INTO reports VALUES (?,?,?,?,?)",
                (report["id"], report["dataset_id"], report["source_strategy_sha256"],
                 report["search_sha256"], payload),
            )
            saved = self._connection.execute(
                "SELECT payload_json FROM reports WHERE id=?", (report["id"],)
            ).fetchone()
            if saved is None or saved["payload_json"] != payload:
                raise SessionReviewError("Completed report identity collision")
            self._connection.execute(
                "INSERT OR IGNORE INTO holdout_locks VALUES (?,?,?)",
                (report["dataset_id"], report["search_sha256"], report["id"]),
            )
        return json.loads(saved["payload_json"])


def freeze_dataset(
    store: SessionReviewStore,
    *,
    source_session_id: str,
    strategy_package: dict[str, Any],
    strategy_digest: str,
    observations: Iterable[dict[str, Any]],
    decisions: Iterable[dict[str, Any]],
    outcomes: Iterable[dict[str, Any]],
    feature_records: Iterable[dict[str, Any]] = (),
    decision_capture_complete: bool = False,
    first_at: float | None = None,
    last_at: float | None = None,
) -> dict[str, Any]:
    """Freeze one session's supplied evidence without inferring missing history.

    The caller is responsible for reading the source session under one database
    snapshot.  Session 3's integration hook performs that shared-library read;
    this module validates the resulting identity and owns only its separate store.
    """
    source_session_id = _text(source_session_id, "source_session_id")
    if not isinstance(decision_capture_complete, bool):
        raise SessionReviewError("decision_capture_complete must be true or false")
    parsed = parse_package(copy.deepcopy(strategy_package))
    if parsed.digest != _text(strategy_digest, "strategy_digest"):
        raise SessionReviewError("Source session strategy digest is stale or mismatched")
    observation_rows = _canonical_rows(observations, "observations")
    decision_rows = _canonical_rows(decisions, "decisions")
    outcome_rows = _canonical_rows(outcomes, "outcomes")
    feature_rows = _canonical_rows(feature_records, "feature_records")
    if not observation_rows:
        raise SessionReviewError("A review dataset needs at least one retained observation")

    parsed_observations: list[dict[str, Any]] = []
    for row in observation_rows:
        _assert_source(row, source_session_id, "observations")
        quote = MarketQuote.from_record(row)
        parsed_observations.append(quote.as_record())
    parsed_observations.sort(key=lambda row: (row["observed_at"], row["mint"], canonical_json(row)))
    if len({(row["mint"], row["observed_at"]) for row in parsed_observations}) != len(parsed_observations):
        raise SessionReviewError("Observation identity must be unique by mint and timestamp")

    inferred_first = parsed_observations[0]["observed_at"]
    inferred_last = parsed_observations[-1]["observed_at"]
    start = inferred_first if first_at is None else _finite(first_at, "first_at")
    end = inferred_last if last_at is None else _finite(last_at, "last_at")
    if start > inferred_first or end < inferred_last or start >= end:
        raise SessionReviewError("Dataset period must contain every observation and span positive time")

    features_by_key: dict[str, dict[str, Any]] = {}
    for row in feature_rows:
        _assert_source(row, source_session_id, "feature_records")
        key = _text(row.get("decision_key"), "feature_records.decision_key")
        if key in features_by_key:
            raise SessionReviewError("Feature records must be unique by decision key")
        if row.get("schema") != FEATURE_SCHEMA:
            raise SessionReviewError(f"Feature records must use {FEATURE_SCHEMA}")
        if row.get("captured_at_decision") is not True:
            raise SessionReviewError("Feature records must state captured_at_decision=true")
        if row.get("strategy_sha256") != parsed.digest:
            raise SessionReviewError("Feature record strategy digest is stale or mismatched")
        if (row.get("provenance") or {}).get("future_observations_included") is not False:
            raise SessionReviewError("Feature record must explicitly exclude future observations")
        features_by_key[key] = row

    frozen_decisions = []
    decision_keys: set[str] = set()
    for row in decision_rows:
        _assert_source(row, source_session_id, "decisions")
        key = _text(row.get("decision_key"), "decisions.decision_key")
        at = _finite(row.get("at"), "decisions.at")
        if not start <= at <= end:
            raise SessionReviewError("Decision falls outside the frozen period")
        if key in decision_keys:
            raise SessionReviewError("Decision keys must be unique")
        decision_keys.add(key)
        feature = features_by_key.get(key)
        if feature is not None and (
            _finite(feature.get("decision_at"), "feature_records.decision_at") != at or
            feature.get("mint") != row.get("mint")
        ):
            raise SessionReviewError("Feature record identity differs from its frozen decision")
        frozen_decisions.append({
            "decision_key": key,
            "at": at,
            "kind": _text(row.get("kind"), "decisions.kind"),
            "mint": _text(row.get("mint"), "decisions.mint"),
            "reason": _text(row.get("reason"), "decisions.reason"),
            "payload": copy.deepcopy(row.get("payload") or {}),
            "feature_provenance": copy.deepcopy(feature) if feature is not None else {
                "status": "incomplete",
                "reason": "feature_snapshot_not_recorded_at_decision_time",
            },
        })
    frozen_decisions.sort(key=lambda row: (row["at"], row["decision_key"]))

    frozen_outcomes = []
    scored = unknown = 0
    for row in outcome_rows:
        _assert_source(row, source_session_id, "outcomes")
        key = _text(row.get("decision_key"), "outcomes.decision_key")
        if key not in decision_keys:
            raise SessionReviewError("Outcome does not link to a frozen decision")
        status = row.get("status")
        if status not in ("scored", "unknown"):
            raise SessionReviewError("Outcome status must be scored or unknown")
        if row.get("basis") != "counterfactual_retained_quotes":
            raise SessionReviewError("Only counterfactual retained-quote outcomes belong in this dataset")
        scored += status == "scored"
        unknown += status == "unknown"
        frozen_outcomes.append(row)
    frozen_outcomes.sort(key=lambda row: (
        row.get("decision_key", ""), row.get("horizon_seconds", 0), canonical_json(row)
    ))

    unused_features = sorted(set(features_by_key) - decision_keys)
    if unused_features:
        raise SessionReviewError("Feature record does not link to a frozen decision")
    core = {
        "schema": DATASET_SCHEMA,
        "capture_engine_version": ENGINE_VERSION,
        "source_session_id": source_session_id,
        "strategy_sha256": parsed.digest,
        "strategy_package": parsed.package,
        "strategy_profile": parsed.profile,
        "period": {"first_at": start, "last_at": end},
        "observations": parsed_observations,
        "decisions": frozen_decisions,
        "outcomes": frozen_outcomes,
        "evidence": {
            "observations": len(parsed_observations),
            "decisions": len(frozen_decisions),
            "decisions_with_recorded_features": len(features_by_key),
            "decisions_with_incomplete_features": len(frozen_decisions) - len(features_by_key),
            "eligible_decision_capture": (
                "complete" if decision_capture_complete else "historical_transition_records_only"
            ),
            "scored_outcomes": scored,
            "unknown_outcomes": unknown,
            "scope": "source_session_only",
            "outcome_basis": "counterfactual_retained_quotes_not_fills_or_net_returns",
        },
    }
    dataset_id = content_hash(core)
    return store.save_dataset({**core, "id": dataset_id, "data_sha256": dataset_id})


def capture_decision_features(
    strategy_package: dict[str, Any],
    *,
    decision_key: str,
    decision: dict[str, Any],
    observations: Iterable[dict[str, Any]],
    engine_version: str,
    captured_at: float,
) -> dict[str, Any]:
    """Capture reproducible inputs at decision time for future append-only storage.

    This helper rejects any observation later than the decision. It must be
    called by the live decision producer, not as a historical backfill.
    """
    strategy = parse_package(copy.deepcopy(strategy_package))
    if strategy.profile != "ladder-v1":
        raise SessionReviewError("Feature capture currently supports the ladder-v1 family only")
    decision_key = _text(decision_key, "decision_key")
    engine_version = _text(engine_version, "engine_version")
    decision = json.loads(canonical_json(decision))
    at = _finite(decision.get("at"), "decision.at")
    captured_at = _finite(captured_at, "captured_at")
    if captured_at < at or captured_at - at > ENDPOINT_GRACE_SECONDS:
        raise SessionReviewError("Feature capture must occur during the decision cycle")
    mint = _text(decision.get("mint"), "decision.mint")
    rows = _canonical_rows(observations, "observations")
    quotes = []
    for row in rows:
        quote = MarketQuote.from_record(row)
        if quote.observed_at > at:
            raise SessionReviewError("Feature provenance cannot include a future observation")
        quotes.append(quote)
    quotes.sort(key=lambda quote: (quote.observed_at, quote.mint, canonical_json(quote.as_record())))
    local = sorted((quote for quote in quotes if quote.mint == mint),
                   key=lambda quote: quote.observed_at)
    if not local:
        raise SessionReviewError("Feature capture needs the decision mint's retained observations")
    latest = local[-1]
    rules = strategy.effective_rules
    window_seconds = float(rules.get("observation_seconds", 300))
    prior = [index for index, quote in enumerate(local)
             if quote.observed_at <= latest.observed_at - window_seconds]
    window = local[prior[-1]:] if prior else local
    values = [quote.price_native if quote.route == "curve" else quote.price_usd for quote in window]
    reserve_values = [quote.liquidity_native if quote.route == "curve" else quote.liquidity_usd
                      for quote in window]
    shared = summarize(local, window_seconds, at)
    if shared is not None and strategy.effective_rules.get("entry_style") == "relative":
        low = float(rules["entry_cap_min"])
        high = float(rules["entry_cap_max"])
        peer_growth = []
        for peer_mint in sorted({quote.mint for quote in quotes} - {mint}):
            peer_history = sorted((quote for quote in quotes if quote.mint == peer_mint),
                                  key=lambda quote: quote.observed_at)
            if not peer_history or not low <= peer_history[-1].market_cap_usd < high:
                continue
            peer = summarize(peer_history, window_seconds, at)
            if peer is not None and peer["pool"] == peer_history[-1].pool:
                peer_growth.append(float(peer["growth"]))
        shared["peer_count"] = len(peer_growth) + 1
        if peer_growth:
            shared["percentile"] = sum(value < float(shared["growth"])
                                       for value in peer_growth) / len(peer_growth)
    feature_vector = {
        "quote_age_seconds": at - latest.observed_at,
        "market_cap_usd": latest.market_cap_usd,
        "price_usd": latest.price_usd,
        "price_native": latest.price_native,
        "liquidity_usd": latest.liquidity_usd,
        "liquidity_native": latest.liquidity_native,
        "sample_count": len(window),
        "price_change_count": sum(abs(right / left - 1) > 1e-8
                                  for left, right in zip(values, values[1:])),
        "growth_fraction": values[-1] / values[0] - 1 if len(values) > 1 else None,
        "reserve_growth": reserve_values[-1] - reserve_values[0] if len(reserve_values) > 1 else None,
        "market_cap_peak_drawdown": (
            1 - latest.market_cap_usd / max(quote.market_cap_usd for quote in local)
        ),
        "research_summary": shared,
    }
    threshold_names = (
        "entry_style", "entry_cap_min", "entry_cap_max", "observation_seconds",
        "minimum_samples", "minimum_price_changes", "minimum_growth_fraction",
        "minimum_reserve_growth_usd", "maximum_peak_drawdown",
        "min_curve_liquidity_usd", "min_amm_liquidity_usd",
    )
    inputs = [quote.as_record() for quote in quotes]
    return {
        "schema": FEATURE_SCHEMA,
        "decision_key": decision_key,
        "captured_at_decision": True,
        "captured_at": captured_at,
        "decision_at": at,
        "mint": mint,
        "strategy_sha256": strategy.digest,
        "engine_version": engine_version,
        "route": latest.route,
        "pool": latest.pool,
        "source": latest.source,
        "feature_vector": feature_vector,
        "thresholds": {name: copy.deepcopy(rules[name]) for name in threshold_names if name in rules},
        "decision": decision,
        "provenance": {
            "observation_count": len(inputs),
            "observation_sha256": content_hash(inputs),
            "latest_observed_at": latest.observed_at,
            "future_observations_included": False,
        },
    }


def _changed_package(package: dict[str, Any], changes: dict[str, float]) -> dict[str, Any]:
    candidate = copy.deepcopy(package)
    configuration = candidate.get("configuration")
    if not isinstance(configuration, dict):
        raise SessionReviewError("Candidate search supports the ladder-v1 handoff family only")
    configuration.update(changes)
    # This is intentionally the public parser, not an internal shortcut.
    return parse_package(candidate).package


def generate_candidates(strategy_package: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the current ladder strategy plus at most five bounded variants."""
    current = parse_package(copy.deepcopy(strategy_package))
    if current.profile != "ladder-v1":
        raise SessionReviewError(
            "Offline candidate comparison currently supports only the complete ladder-v1 family"
        )
    rules = current.effective_rules
    growth = float(rules["minimum_growth_fraction"])
    window = float(rules["observation_seconds"])
    drawdown = float(rules["maximum_peak_drawdown"])
    order = float(rules["order_usd"])
    variants = [
        ("lower growth threshold", {"minimum_growth_fraction": max(0.000001, growth * 0.8)}),
        ("higher growth threshold", {"minimum_growth_fraction": min(0.95, growth * 1.25)}),
        ("shorter observation window", {"observation_seconds": max(1.0, round(window * 0.75, 6))}),
        ("tighter peak drawdown", {"maximum_peak_drawdown": max(0.0, drawdown * 0.8)}),
        ("smaller modeled order", {"order_usd": max(0.000001, min(order * 0.75, float(rules["budget_usd"])))}),
    ]
    result = [{
        "id": "current",
        "kind": "current",
        "label": "Current strategy",
        "changes": {},
        "package_sha256": current.digest,
        "package": current.package,
    }]
    seen = {current.digest}
    for label, changes in variants:
        package = _changed_package(current.package, changes)
        parsed = parse_package(package)
        if parsed.digest in seen:
            continue
        seen.add(parsed.digest)
        result.append({
            "id": f"candidate-{parsed.digest[:12]}",
            "kind": "candidate",
            "label": label.capitalize(),
            "changes": changes,
            "package_sha256": parsed.digest,
            "package": parsed.package,
        })
    return result


def _split_boundaries(observations: list[dict[str, Any]]) -> dict[str, float]:
    times = sorted({float(row["observed_at"]) for row in observations})
    if len(times) < 5:
        raise SessionReviewError("At least five distinct observation times are required")
    train_index = min(len(times) - 2, max(1, math.ceil(len(times) * 0.6)))
    validation_index = min(len(times) - 1, max(train_index + 1, math.ceil(len(times) * 0.8)))
    return {
        "first_at": times[0],
        "train_end_at": times[train_index],
        "validation_end_at": times[validation_index],
        "last_at": times[-1],
    }


def _split_for(at: float, evidence_end_at: float, boundaries: dict[str, float]) -> str:
    if at < boundaries["train_end_at"]:
        return "train" if evidence_end_at < boundaries["train_end_at"] else "embargoed"
    if at < boundaries["validation_end_at"]:
        return "validation" if evidence_end_at < boundaries["validation_end_at"] else "embargoed"
    return "holdout" if evidence_end_at <= boundaries["last_at"] else "embargoed"


def _cost_model(rules: dict[str, Any]) -> dict[str, float | str]:
    return {
        "schema": "pumpwizard.session-review-costs/v1",
        "variable_fee_fraction_each_side": float(rules.get("fee_fraction", 0)),
        "slippage_fraction_each_side": float(rules.get("adverse_fraction", 0)),
        "fixed_cost_usd_each_side": float(rules.get("fixed_fee_usd", 0)),
        "entry_rent_usd": float(rules.get("entry_rent_usd", 0)),
        "basis": "deterministic modeled round trip; not an executable quote or fill",
    }


def _modeled_return(raw_return: float, rules: dict[str, Any]) -> float:
    order = float(rules["order_usd"])
    fee = float(rules.get("fee_fraction", 0))
    slip = float(rules.get("adverse_fraction", 0))
    fixed = float(rules.get("fixed_fee_usd", 0))
    rent = float(rules.get("entry_rent_usd", 0))
    entry_capital = max(0.0, order - fixed - rent)
    if order <= 0 or entry_capital <= 0:
        return -1.0
    acquired = entry_capital * (1 - fee) * (1 - slip)
    exit_value = acquired * (1 + raw_return) * (1 - fee) * (1 - slip) - fixed
    return exit_value / order - 1


def _outcome_for_signal(
    signal: dict[str, Any],
    history: list[dict[str, Any]],
    horizon_seconds: int,
    rules: dict[str, Any],
) -> dict[str, Any]:
    start_at = float(signal["at"])
    due_at = start_at + horizon_seconds
    baseline = min(
        (row for row in history if abs(float(row["observed_at"]) - start_at) <= ENDPOINT_GRACE_SECONDS),
        key=lambda row: (abs(float(row["observed_at"]) - start_at), -float(row["observed_at"])),
        default=None,
    )
    endpoint = min(
        (row for row in history if abs(float(row["observed_at"]) - due_at) <= ENDPOINT_GRACE_SECONDS),
        key=lambda row: (abs(float(row["observed_at"]) - due_at), -float(row["observed_at"])),
        default=None,
    )
    common = {"at": start_at, "due_at": due_at, "mint": signal["mint"]}
    if baseline is None or endpoint is None:
        return {**common, "status": "unknown", "coverage": (
            "missing_baseline_quote" if baseline is None else "missing_horizon_quote"
        )}
    identity = (baseline["route"], baseline["pool"])
    rows = [row for row in history if float(baseline["observed_at"]) <= float(row["observed_at"])
            <= float(endpoint["observed_at"])]
    if any((row["route"], row["pool"]) != identity for row in rows):
        return {**common, "status": "unknown", "coverage": "route_or_pool_changed"}
    maximum_gap = float(rules.get("max_sample_gap_seconds", 180))
    if len(rows) < 2 or any(float(right["observed_at"]) - float(left["observed_at"]) > maximum_gap
                            for left, right in zip(rows, rows[1:])):
        return {**common, "status": "unknown", "coverage": "discontinuous_quotes",
                "sample_count": len(rows)}
    minimum_liquidity = float(rules[
        "min_curve_liquidity_usd" if baseline["route"] == "curve" else "min_amm_liquidity_usd"
    ])
    if any(float(row["liquidity_usd"]) < minimum_liquidity for row in rows):
        return {**common, "status": "unknown", "coverage": "liquidity_below_candidate_floor",
                "sample_count": len(rows)}
    raw = float(endpoint["price_usd"]) / float(baseline["price_usd"]) - 1
    modeled = _modeled_return(raw, rules)
    return {
        **common,
        "status": "scored",
        "coverage": "continuous_same_route_with_modeled_costs",
        "baseline_observed_at": baseline["observed_at"],
        "outcome_observed_at": endpoint["observed_at"],
        "raw_price_return_fraction": raw,
        "modeled_return_fraction": modeled,
        "sample_count": len(rows),
    }


def _signals(package: dict[str, Any], observations: list[dict[str, Any]],
             cancelled: Callable[[], bool]) -> list[dict[str, Any]]:
    strategy = parse_package(copy.deepcopy(package))
    engine = DecisionEngine(strategy)
    batches: dict[float, list[MarketQuote]] = defaultdict(list)
    for row in observations:
        quote = MarketQuote.from_record(row)
        batches[quote.observed_at].append(quote)
    signals = []
    for at in sorted(batches):
        _cancelled(cancelled)
        decisions = engine.evaluate(sorted(batches[at], key=lambda quote: quote.mint), at)
        signals.extend({"at": decision.at, "mint": decision.mint, "reason": decision.reason}
                       for decision in decisions if decision.kind == "entry_recommended")
    return signals


def _union_seconds(intervals: list[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    total = 0.0
    start, end = sorted(intervals)[0]
    for next_start, next_end in sorted(intervals)[1:]:
        if next_start > end:
            total += end - start
            start, end = next_start, next_end
        else:
            end = max(end, next_end)
    return total + end - start


def _metrics(outcomes: list[dict[str, Any]], span: float) -> dict[str, Any]:
    scored = [row for row in outcomes if row["status"] == "scored"]
    returns = [float(row["modeled_return_fraction"]) for row in scored]
    equity = peak = 1.0
    max_drawdown = 0.0
    for result in returns:
        equity *= max(0.0, 1 + result)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak if peak else 0.0)
    exposure = _union_seconds([(row["at"], row["due_at"]) for row in scored])
    return {
        "signals": len(outcomes),
        "scored_samples": len(scored),
        "unknown_samples": len(outcomes) - len(scored),
        "coverage_fraction": len(scored) / len(outcomes) if outcomes else 0.0,
        "mean_modeled_return_fraction": sum(returns) / len(returns) if returns else None,
        "compounded_modeled_result_fraction": equity - 1 if returns else None,
        "maximum_trade_drawdown_fraction": max_drawdown if returns else None,
        "loss_tail_fraction": min(returns) if returns else None,
        "positive_fraction": sum(value > 0 for value in returns) / len(returns) if returns else None,
        "exposure_fraction": min(1.0, exposure / span) if span > 0 else 0.0,
    }


def _objective(result: dict[str, Any]) -> float:
    metrics = result["splits"]["validation"]["metrics"]
    mean = metrics["mean_modeled_return_fraction"]
    drawdown = metrics["maximum_trade_drawdown_fraction"]
    return float(mean) - float(drawdown) if mean is not None and drawdown is not None else -math.inf


def _rank_key(result: dict[str, Any]) -> tuple[float, float, float, int, str]:
    metrics = result["splits"]["validation"]["metrics"]
    return (-_objective(result), float(metrics["maximum_trade_drawdown_fraction"] or 0),
            -float(metrics["coverage_fraction"]), 0 if result["kind"] == "current" else 1,
            result["candidate_id"])


def compare_dataset(
    store: SessionReviewStore,
    dataset_id: str,
    *,
    horizon_seconds: int = DEFAULT_HORIZON_SECONDS,
    cancelled: Callable[[], bool] = lambda: False,
    evaluate_holdout: bool = True,
) -> dict[str, Any]:
    """Compare a frozen dataset without reading holdout unless explicitly requested.

    The legacy Session Review UI requests holdout and atomically seals the
    completed report in ``holdout_locks``.  Research compatibility runs can be
    exploratory; those stop at validation and let the research registry own
    persistence instead of creating a misleading legacy holdout lock.
    """
    _cancelled(cancelled)
    if not isinstance(evaluate_holdout, bool):
        raise SessionReviewError("Holdout evaluation must be explicitly enabled or disabled")
    if isinstance(horizon_seconds, bool) or not isinstance(horizon_seconds, int) or horizon_seconds <= 0:
        raise SessionReviewError("Comparison horizon must be a positive whole number of seconds")
    dataset = store.dataset(dataset_id)
    if content_hash({key: value for key, value in dataset.items() if key not in ("id", "data_sha256")}) != dataset["id"]:
        raise SessionReviewError("Frozen dataset content no longer matches its identity")
    candidates = generate_candidates(dataset["strategy_package"])
    boundaries = _split_boundaries(dataset["observations"])
    source_rules = parse_package(dataset["strategy_package"]).effective_rules
    search = {
        "engine_version": ENGINE_VERSION,
        "dataset_id": dataset["id"],
        "horizon_seconds": horizon_seconds,
        "endpoint_grace_seconds": ENDPOINT_GRACE_SECONDS,
        "split_boundaries": boundaries,
        "candidate_package_sha256": [candidate["package_sha256"] for candidate in candidates],
        "cost_model": _cost_model(source_rules),
        "ranking": {
            "selection_split": "validation",
            "objective": "mean modeled return minus maximum sequential trade drawdown",
            "minimum_samples": MINIMUM_VALIDATION_SAMPLES,
            "minimum_coverage_fraction": MINIMUM_COVERAGE_FRACTION,
            "tie_breakers": "lower drawdown, higher coverage, current package, lexical candidate id",
        },
    }
    if not evaluate_holdout:
        # Preserve the historical identity of ordinary Session Review searches
        # while giving validation-only compatibility reports a distinct identity.
        search["holdout_evaluated"] = False
    search_sha256 = content_hash(search)
    report_id = content_hash({"schema": REPORT_SCHEMA, "dataset_id": dataset["id"],
                              "search_sha256": search_sha256})
    if evaluate_holdout:
        locked = store.locked_report(dataset["id"], search_sha256)
        if locked is not None:
            return locked
    try:
        existing = store.report(report_id)
    except SessionReviewError:
        existing = None
    if existing is not None:
        return existing

    evaluation_observations = dataset["observations"]
    if not evaluate_holdout:
        evaluation_observations = [
            row for row in evaluation_observations
            if float(row["observed_at"]) < boundaries["validation_end_at"]
        ]
    histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evaluation_observations:
        histories[row["mint"]].append(row)
    split_ranges = [
        ("train", boundaries["first_at"], boundaries["train_end_at"]),
        ("validation", boundaries["train_end_at"], boundaries["validation_end_at"]),
    ]
    if evaluate_holdout:
        split_ranges.append(
            ("holdout", boundaries["validation_end_at"], boundaries["last_at"])
        )
    results = [{
        "candidate_id": "baseline-no-entry",
        "kind": "baseline",
        "label": "No-entry baseline",
        "package_sha256": None,
        "changes": {},
        "cost_model": None,
        "splits": {
            name: {"outcomes": [], "metrics": _metrics([], max(1.0, right - left))}
            for name, left, right in split_ranges
        },
        "embargoed_signals": 0,
    }]
    for candidate in candidates:
        _cancelled(cancelled)
        rules = parse_package(candidate["package"]).effective_rules
        split_outcomes: dict[str, list[dict[str, Any]]] = {
            name: [] for name, _left, _right in split_ranges
        }
        embargoed = 0
        for signal in _signals(candidate["package"], evaluation_observations, cancelled):
            outcome = _outcome_for_signal(signal, histories[signal["mint"]], horizon_seconds, rules)
            # Endpoint lookup may use the grace window after the nominal due
            # time, so embargo that whole window rather than leaking a quote
            # across a chronological boundary.
            evidence_end_at = float(outcome["due_at"]) + ENDPOINT_GRACE_SECONDS
            split = _split_for(float(signal["at"]), evidence_end_at, boundaries)
            if split == "embargoed":
                embargoed += 1
            else:
                split_outcomes[split].append(outcome)
        spans = {name: right - left for name, left, right in split_ranges}
        results.append({
            "candidate_id": candidate["id"],
            "kind": candidate["kind"],
            "label": candidate["label"],
            "package_sha256": candidate["package_sha256"],
            "changes": candidate["changes"],
            "cost_model": _cost_model(rules),
            "splits": {name: {"outcomes": rows, "metrics": _metrics(rows, spans[name])}
                       for name, rows in split_outcomes.items()},
            "embargoed_signals": embargoed,
        })

    comparable = []
    for result in results:
        if result["kind"] == "baseline":
            continue
        validation = result["splits"]["validation"]["metrics"]
        if (validation["scored_samples"] >= MINIMUM_VALIDATION_SAMPLES and
                validation["coverage_fraction"] >= MINIMUM_COVERAGE_FRACTION):
            comparable.append(result)
    recommendation: dict[str, Any]
    if dataset["evidence"].get("eligible_decision_capture") != "complete":
        recommendation = {
            "action": "collect_more_data",
            "candidate_id": None,
            "reason": "Historical evidence contains transition records, not every eligible rejection.",
        }
    elif not comparable:
        recommendation = {
            "action": "collect_more_data",
            "candidate_id": None,
            "reason": "Validation evidence does not meet the fixed sample and coverage gates.",
        }
    else:
        selected = sorted(comparable, key=_rank_key)[0]
        current = next(result for result in comparable if result["candidate_id"] == "current") if any(
            result["candidate_id"] == "current" for result in comparable
        ) else None
        if current is None:
            recommendation = {
                "action": "collect_more_data",
                "candidate_id": None,
                "reason": "The current strategy does not meet the validation sample and coverage gates.",
            }
        elif (selected["candidate_id"] == "current" or
              _objective(selected) <= _objective(current) + 1e-12):
            recommendation = {
                "action": "keep_current",
                "candidate_id": "current",
                "reason": "Validation-only ranking did not identify a bounded variant ahead of current.",
            }
        else:
            recommendation = {
                "action": "create_inactive_draft",
                "candidate_id": selected["candidate_id"],
                "reason": (
                    "This bounded variant ranked first on validation; holdout is shown for review only."
                    if evaluate_holdout else
                    "This bounded variant ranked first on validation; holdout remains unexamined."
                ),
            }
    _cancelled(cancelled)
    report = {
        "schema": REPORT_SCHEMA,
        "id": report_id,
        "dataset_id": dataset["id"],
        "source_session_id": dataset["source_session_id"],
        "source_strategy_sha256": dataset["strategy_sha256"],
        "engine_version": ENGINE_VERSION,
        "search_sha256": search_sha256,
        "inputs": search,
        "candidate_packages": candidates,
        "results": results,
        "recommendation": recommendation,
        "holdout_state": "exposed" if evaluate_holdout else "not_evaluated",
        "holdout_policy": (
            "Candidates were frozen before evaluation; selection used validation only. "
            "This dataset is locked against a different search after holdout exposure."
            if evaluate_holdout else
            "Exploratory comparison stopped at validation; holdout was not evaluated or exported."
        ),
        "funded_performance": False,
        "submission_enabled": False,
        "limitations": [
            "Results are modeled fixed-horizon quote replays, not fills or net funded returns.",
            "Missing endpoints, quote gaps, route changes and liquidity-floor failures remain unknown.",
            "Historical feature snapshots absent at decision time remain explicitly incomplete.",
            "Only the ladder-v1 family and retained source-session observations are supported.",
        ] + ([] if evaluate_holdout else [
            "Holdout evidence was not evaluated or exported by this exploratory comparison."
        ]),
    }
    _cancelled(cancelled)
    if evaluate_holdout:
        return store.save_report(report)
    return json.loads(canonical_json(report))


def create_inactive_draft_request(
    report: dict[str, Any], *, candidate_id: str, accepted: bool
) -> dict[str, Any]:
    """Build a safe integration request after an explicit user acceptance."""
    if accepted is not True:
        raise SessionReviewError("Creating a successor draft requires explicit acceptance")
    if report.get("schema") == REPLAY_REPORT_SCHEMA:
        return _portfolio_draft_request(report, candidate_id)
    if report.get("schema") != REPORT_SCHEMA:
        raise SessionReviewError("Choose a completed session-review report")
    recommendation = report.get("recommendation")
    if not isinstance(recommendation, dict) or recommendation.get("action") != "create_inactive_draft":
        raise SessionReviewError("This report does not recommend creating a successor draft")
    if recommendation.get("candidate_id") != candidate_id:
        raise SessionReviewError("Choose the candidate selected by this report")
    candidate = next((row for row in report.get("candidate_packages", [])
                      if row.get("id") == candidate_id and row.get("kind") == "candidate"), None)
    if candidate is None:
        raise SessionReviewError("Choose a bounded candidate from this report")
    package = parse_package(copy.deepcopy(candidate["package"]))
    if package.digest != candidate.get("package_sha256"):
        raise SessionReviewError("Candidate package identity changed after comparison")
    return {
        "schema": DRAFT_SCHEMA,
        "source_report_id": report["id"],
        "source_dataset_id": report["dataset_id"],
        "source_session_id": report["source_session_id"],
        "strategy_package": package.package,
        "strategy_sha256": package.digest,
        "requested_session_state": {
            "status": "paused",
            "monitoring_enabled": False,
            "entry_mode": "disabled",
            "exit_mode": "manual",
            "wallet_allocation_lamports": "0",
            "copy_execution_authorization": False,
        },
        "accepted": True,
        "note": "Import and create through existing validation; never mutate the source session.",
    }


def compare_portfolio_replay_request(
    request: dict[str, Any], *, cancelled: Callable[[], bool] = lambda: False
) -> dict[str, Any]:
    """Adapt a registered Batch-3 request to the pure finite-capital engine.

    Worker 1 owns persistence, holdout reservations, jobs and API authentication.
    This adapter validates unchanged strategy packages and returns a typed result
    that Worker 3 can render without recalculating financial values.
    """
    value = _canonical_rows([request], "request")[0]
    if value.get("schema") != REPLAY_REQUEST_SCHEMA:
        raise SessionReviewError(f"Replay request must use {REPLAY_REQUEST_SCHEMA}")
    if set(value) != {"schema", "input_lineage", "evidence_policy", "candidates"}:
        raise SessionReviewError("Replay request fields do not match the v3 contract")
    rows = value["candidates"]
    if not isinstance(rows, list) or not rows:
        raise SessionReviewError("Replay request needs registered candidates")
    engine_candidates = []
    packages = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SessionReviewError("Replay candidates must be objects")
        kind = row.get("kind")
        common = {
            key: copy.deepcopy(row[key]) for key in (
                "id", "kind", "label", "events_validation", "events_holdout", "policy",
                "base_costs", "adverse_costs", "execution",
                "episode_returns_validation_micros",
            ) if key in row
        }
        if kind in ("current", "variant"):
            if "strategy_package" not in row or "strategy_sha256" not in row:
                raise SessionReviewError("Trading candidates need an exact strategy package identity")
            parsed = parse_package(copy.deepcopy(row["strategy_package"]))
            if parsed.profile != "ladder-v1":
                raise SessionReviewError("Finite-capital comparison supports ladder-v1 packages only")
            if parsed.digest != row["strategy_sha256"]:
                raise SessionReviewError("Replay candidate package identity changed after registration")
            packages.append({
                "id": row.get("id"), "kind": kind, "label": row.get("label"),
                "strategy_sha256": parsed.digest, "strategy_package": parsed.package,
            })
        elif kind == "holding":
            packages.append({
                "id": row.get("id"), "kind": "holding", "label": row.get("label"),
                "strategy_sha256": None, "strategy_package": None,
            })
        else:
            raise SessionReviewError(f"Replay candidate {index + 1} has an unsupported kind")
        engine_candidates.append(common)
    current_packages = [row for row in packages if row["kind"] == "current"]
    if len(current_packages) != 1:
        raise SessionReviewError("Replay request needs exactly one current strategy package")
    allowed_variants = {
        row["package_sha256"] for row in generate_candidates(
            current_packages[0]["strategy_package"]
        ) if row["kind"] == "candidate"
    }
    if any(
        row["strategy_sha256"] not in allowed_variants
        for row in packages if row["kind"] == "variant"
    ):
        raise SessionReviewError("Replay variant is outside the five predeclared bounded variants")
    try:
        comparison = compare_portfolios(
            engine_candidates, evidence_policy=value["evidence_policy"],
            input_lineage=value["input_lineage"], cancelled=cancelled,
        )
    except ValueError as exc:
        raise SessionReviewError(str(exc)) from None
    if comparison.get("schema") != COMPARISON_SCHEMA:
        raise SessionReviewError("Finite-capital replay returned an unexpected schema")
    core = {
        "schema": REPLAY_REPORT_SCHEMA,
        "comparison": comparison,
        "candidate_packages": packages,
        "recommendation": copy.deepcopy(comparison["recommendation"]),
        "legacy_report": False,
        "model_label": "finite-capital event-ledger v3",
        "funded_performance": False,
        "submission_enabled": False,
    }
    ident = content_hash(core)
    return {**core, "id": ident, "reproducibility_sha256": ident}


def report_compatibility(report: dict[str, Any]) -> dict[str, Any]:
    """Describe report semantics without relabeling historical v1 evidence."""
    schema = report.get("schema") if isinstance(report, dict) else None
    if schema == REPORT_SCHEMA:
        return {
            "readable": True, "legacy": True,
            "model_label": "fixed-horizon per-signal modeled v1",
            "finite_capital": False,
            "notice": "Historical v1 evidence is preserved and is not a portfolio replay.",
        }
    if schema == LEGACY_REPLAY_REPORT_SCHEMA:
        return {
            "readable": True, "legacy": True,
            "model_label": "finite-capital event-ledger v2",
            "finite_capital": True,
            "notice": (
                "Historical v2 portfolio evidence is preserved; it predates corrected "
                "source-time freshness, inclusive expiry and 1e-18 price precision."
            ),
        }
    if schema == REPLAY_REPORT_SCHEMA:
        return {
            "readable": True, "legacy": False,
            "model_label": "finite-capital event-ledger v3",
            "finite_capital": True,
            "notice": "Modeled research evidence only; no funded or signing authority.",
        }
    raise SessionReviewError("Unknown session-review report schema")


def _portfolio_draft_request(report: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    recommendation = report.get("recommendation")
    if not isinstance(recommendation, dict) or recommendation.get("action") != "eligible_inactive_draft":
        raise SessionReviewError("This replay does not recommend an eligible inactive draft")
    if recommendation.get("candidate_id") != candidate_id:
        raise SessionReviewError("Choose the candidate selected by this replay")
    candidate = next((
        row for row in report.get("candidate_packages", [])
        if row.get("id") == candidate_id and row.get("kind") == "variant"
    ), None)
    if candidate is None:
        raise SessionReviewError("Choose a bounded strategy variant from this replay")
    parsed = parse_package(copy.deepcopy(candidate["strategy_package"]))
    if parsed.digest != candidate.get("strategy_sha256"):
        raise SessionReviewError("Replay candidate package identity changed after comparison")
    return {
        "schema": DRAFT_SCHEMA,
        "source_report_id": report["id"],
        "source_dataset_id": report["comparison"]["input_lineage"].get("dataset_id"),
        "source_session_id": report["comparison"]["input_lineage"].get("source_session_id"),
        "strategy_package": parsed.package,
        "strategy_sha256": parsed.digest,
        "requested_session_state": {
            "status": "paused", "monitoring_enabled": False,
            "entry_mode": "disabled", "exit_mode": "manual",
            "wallet_allocation_lamports": "0", "copy_execution_authorization": False,
        },
        "accepted": True,
        "note": "Create through existing validation; replay and paper evidence grant no authority.",
    }
