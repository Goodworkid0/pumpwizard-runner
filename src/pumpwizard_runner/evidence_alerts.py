"""Bounded local inbox for actionable forward-evidence conditions.

The campaign service supplies retained campaign views. This module derives and
stores local-only operating alerts; it owns no scheduler, provider, signer,
wallet, order route, or external notification transport.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from .strategies import canonical_json


DATABASE_NAME = "evidence-alerts.sqlite"
SCHEMA_VERSION = 2
ALERT_SCHEMA = "pumpwizard.evidence-alert/v1"
INBOX_SCHEMA = "pumpwizard.evidence-alert-inbox/v1"
EXPORT_SCHEMA = "pumpwizard.evidence-alert-export/v1"
MAX_ALERTS = 1_000
MAX_MISMATCHES = 100


class EvidenceAlertError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_evidence_alert"):
        super().__init__(message)
        self.code = code


class EvidenceAlertStore:
    """Restart-safe local inbox with deterministic occurrence generations."""

    def __init__(
        self, data_dir: str | Path, *, clock=time.time,
        maximum_alerts: int = MAX_ALERTS,
    ):
        if (isinstance(maximum_alerts, bool) or not isinstance(maximum_alerts, int)
                or not 1 <= maximum_alerts <= 10_000):
            raise EvidenceAlertError("maximum_alerts must be between 1 and 10000")
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / DATABASE_NAME
        self.clock = clock
        self.maximum_alerts = maximum_alerts
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS alert_meta(
                    key TEXT PRIMARY KEY,value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_alerts(
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    condition_key TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK(generation > 0),
                    severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
                    state TEXT NOT NULL CHECK(state IN ('open','resolved')),
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    first_at REAL NOT NULL,
                    last_at REAL NOT NULL,
                    occurrence_count INTEGER NOT NULL CHECK(occurrence_count > 0),
                    acknowledged_at REAL,
                    resolved_at REAL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(campaign_id,condition_key,generation)
                );
                CREATE INDEX IF NOT EXISTS evidence_alert_recent
                    ON evidence_alerts(campaign_id,last_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS evidence_alert_evaluations(
                    campaign_id TEXT PRIMARY KEY,
                    observation_id TEXT NOT NULL,
                    evaluated_at REAL NOT NULL,
                    input_sha256 TEXT NOT NULL
                );
            """)
            row = self.db.execute(
                "SELECT value FROM alert_meta WHERE key='schema_version'"
            ).fetchone()
            if row is not None and int(row["value"]) > SCHEMA_VERSION:
                raise EvidenceAlertError("Evidence alerts were created by a newer version")
            self.db.execute(
                """INSERT INTO alert_meta(key,value) VALUES('schema_version',?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(SCHEMA_VERSION),),
            )

    def evaluate(
        self, campaign: dict[str, Any], *,
        accounting_mismatches: list[dict[str, Any]] | None = None,
        observation_id: str | None = None,
        evaluated_at: float | None = None,
    ) -> dict[str, int | bool]:
        """Evaluate one immutable campaign observation.

        Repeating the same observation is a no-op. An older or conflicting
        observation is rejected so reordered responses cannot resolve newer
        conditions.
        """
        campaign = _object(campaign, "campaign")
        campaign_id = _text(campaign.get("id"), "campaign_id")
        mismatches = [_object(row, "accounting mismatch") for row in (
            accounting_mismatches or []
        )]
        if len(mismatches) > MAX_MISMATCHES:
            raise EvidenceAlertError(
                f"Accounting mismatches exceed the {MAX_MISMATCHES}-row bound"
            )
        now = _number(self.clock() if evaluated_at is None else evaluated_at, "alert time")
        # Campaign views contain clock-derived progress fields. Those fields can
        # change on every read even when no new retained evidence arrived. Base
        # the implicit observation identity only on fields that can affect an
        # alert plus the durable campaign revision so polling cannot inflate an
        # occurrence count or drive resolution.
        evaluation_input = _evaluation_input(campaign, mismatches)
        input_sha256 = _digest(evaluation_input)
        observation = (
            _text(observation_id, "observation_id")
            if observation_id is not None else "campaign-observation-" + input_sha256[:32]
        )
        conditions = _conditions(campaign, mismatches)
        opened = updated = resolved = 0
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute(
                "SELECT * FROM evidence_alert_evaluations WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()
            if previous is not None:
                if previous["observation_id"] == observation:
                    if previous["input_sha256"] != input_sha256:
                        raise EvidenceAlertError(
                            "Alert observation identity was reused with different evidence",
                            code="identity_conflict",
                        )
                    return {"opened": 0, "updated": 0, "resolved": 0, "duplicate": True}
                if now < float(previous["evaluated_at"]):
                    raise EvidenceAlertError(
                        "Alert evaluation is older than the retained campaign observation",
                        code="stale_observation",
                    )
            current_rows = self.db.execute(
                """SELECT * FROM evidence_alerts
                   WHERE campaign_id=? AND state='open'""",
                (campaign_id,),
            ).fetchall()
            current = {str(row["condition_key"]): row for row in current_rows}

            # Close absent conditions before inserting new ones so a full inbox
            # can atomically replace an old resolved generation.
            for key, prior in current.items():
                if key in conditions:
                    continue
                record = json.loads(prior["payload_json"])
                record["state"] = "resolved"
                record["resolved_at"] = now
                self.db.execute(
                    """UPDATE evidence_alerts SET state='resolved',resolved_at=?,
                       payload_json=? WHERE id=?""",
                    (now, canonical_json(record), prior["id"]),
                )
                resolved += 1

            for key, condition in conditions.items():
                prior = current.get(key)
                if prior is None:
                    generation = int(self.db.execute(
                        """SELECT COALESCE(MAX(generation),0)+1 FROM evidence_alerts
                           WHERE campaign_id=? AND condition_key=?""",
                        (campaign_id, key),
                    ).fetchone()[0])
                    ident = "evidence-alert-" + _digest([
                        campaign_id, key, condition["source"]["type"],
                        condition["source"]["id"], generation,
                    ])[:32]
                    record = _record(
                        ident, campaign_id, key, generation, condition,
                        first_at=now, last_at=now, count=1,
                        acknowledged_at=None, resolved_at=None, state="open",
                    )
                    self._make_room_locked()
                    self.db.execute(
                        """INSERT INTO evidence_alerts(
                               id,campaign_id,condition_key,generation,severity,state,
                               source_type,source_id,first_at,last_at,occurrence_count,
                               acknowledged_at,resolved_at,payload_json
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        _values(record),
                    )
                    opened += 1
                else:
                    record = _record(
                        str(prior["id"]), campaign_id, key, int(prior["generation"]),
                        condition, first_at=float(prior["first_at"]), last_at=now,
                        count=int(prior["occurrence_count"]) + 1,
                        acknowledged_at=prior["acknowledged_at"], resolved_at=None,
                        state="open",
                    )
                    self.db.execute(
                        """UPDATE evidence_alerts SET severity=?,last_at=?,occurrence_count=?,
                           source_type=?,source_id=?,payload_json=? WHERE id=?""",
                        (record["severity"], now, record["occurrence_count"],
                         record["source"]["type"], record["source"]["id"],
                         canonical_json(record), record["id"]),
                    )
                    updated += 1
            self.db.execute(
                """INSERT INTO evidence_alert_evaluations(
                       campaign_id,observation_id,evaluated_at,input_sha256
                   ) VALUES(?,?,?,?)
                   ON CONFLICT(campaign_id) DO UPDATE SET
                       observation_id=excluded.observation_id,
                       evaluated_at=excluded.evaluated_at,
                       input_sha256=excluded.input_sha256""",
                (campaign_id, observation, now, input_sha256),
            )
        return {
            "opened": opened, "updated": updated, "resolved": resolved,
            "duplicate": False,
        }

    def acknowledge(
        self, alert_id: str, *, acknowledged_at: float | None = None,
    ) -> dict[str, Any]:
        ident = _text(alert_id, "alert_id")
        now = _number(
            self.clock() if acknowledged_at is None else acknowledged_at,
            "acknowledgment time",
        )
        with self._lock, self.db:
            row = self.db.execute(
                "SELECT * FROM evidence_alerts WHERE id=?", (ident,)
            ).fetchone()
            if row is None:
                raise EvidenceAlertError("Unknown evidence alert", code="not_found")
            record = json.loads(row["payload_json"])
            if record.get("acknowledged_at") is None:
                record["acknowledged_at"] = now
                self.db.execute(
                    "UPDATE evidence_alerts SET acknowledged_at=?,payload_json=? WHERE id=?",
                    (now, canonical_json(record), ident),
                )
        return record

    def get(self, alert_id: str) -> dict[str, Any] | None:
        ident = _text(alert_id, "alert_id")
        with self._lock:
            row = self.db.execute(
                "SELECT payload_json FROM evidence_alerts WHERE id=?", (ident,)
            ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def alerts(
        self, *, campaign_id: str | None = None, state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise EvidenceAlertError("Alert limit must be between 1 and 500")
        clauses = []
        parameters: list[Any] = []
        if campaign_id is not None:
            clauses.append("campaign_id=?")
            parameters.append(_text(campaign_id, "campaign_id"))
        if state is not None:
            state = "open" if state == "active" else state
            if state not in {"open", "resolved"}:
                raise EvidenceAlertError("Alert state must be open/active or resolved")
            clauses.append("state=?")
            parameters.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(limit)
        with self._lock:
            rows = self.db.execute(
                "SELECT payload_json FROM evidence_alerts" + where
                + " ORDER BY CASE severity WHEN 'critical' THEN 0 "
                + "WHEN 'warning' THEN 1 ELSE 2 END,last_at DESC,id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def inbox(
        self, campaign_id: str, *, state: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        campaign = _text(campaign_id, "campaign_id")
        rows = self.alerts(campaign_id=campaign, state=state, limit=limit)
        with self._lock:
            counts = self.db.execute(
                """SELECT COUNT(*) AS total,
                       SUM(CASE WHEN state='open' THEN 1 ELSE 0 END) AS active,
                       SUM(CASE WHEN state='open' AND acknowledged_at IS NULL THEN 1 ELSE 0 END)
                         AS unacknowledged_active
                   FROM evidence_alerts WHERE campaign_id=?""",
                (campaign,),
            ).fetchone()
        core = {
            "schema": INBOX_SCHEMA,
            "campaign_id": campaign,
            "alerts": rows,
            "counts": {
                "returned": len(rows),
                "total": int(counts["total"] or 0),
                "active": int(counts["active"] or 0),
                "unacknowledged_active": int(counts["unacknowledged_active"] or 0),
            },
            "local_only": True,
            "external_delivery": False,
            "buy_sell_alerts": False,
        }
        return {**core, "id": _digest(core)}

    def export(self, campaign_id: str) -> dict[str, Any]:
        campaign = _text(campaign_id, "campaign_id")
        with self._lock:
            rows = self.db.execute(
                """SELECT payload_json FROM evidence_alerts WHERE campaign_id=?
                   ORDER BY first_at,id""",
                (campaign,),
            ).fetchall()
        core = {
            "schema": EXPORT_SCHEMA,
            "campaign_id": campaign,
            "alerts": [json.loads(row["payload_json"]) for row in rows],
            "local_only": True,
            "external_delivery": False,
        }
        ident = _digest(core)
        return {**core, "id": ident, "reproducibility_sha256": ident}

    def _make_room_locked(self) -> None:
        count = int(self.db.execute("SELECT COUNT(*) FROM evidence_alerts").fetchone()[0])
        if count < self.maximum_alerts:
            return
        removable = self.db.execute(
            """SELECT id FROM evidence_alerts WHERE state='resolved'
               ORDER BY resolved_at,id LIMIT 1"""
        ).fetchone()
        if removable is None:
            raise EvidenceAlertError(
                "Local alert inbox is full of active conditions", code="alert_capacity"
            )
        self.db.execute("DELETE FROM evidence_alerts WHERE id=?", (removable["id"],))

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> "EvidenceAlertStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _conditions(
    campaign: dict[str, Any], mismatches: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    campaign_id = _text(campaign.get("id"), "campaign_id")
    campaign_href = f"/research/campaigns/{campaign_id}"
    blocker = campaign.get("blocker")
    if isinstance(blocker, dict):
        code = str(blocker.get("code") or "campaign-blocked")[:160]
        if code in {"request_quota", "runtime_quota", "storage_quota"}:
            kind = "provider-quota-exhausted"
            key = "provider:quota:" + code
            title = "Campaign resource quota is exhausted"
        elif code in {
            "provider_outage", "provider_error", "provider_timeout",
            "provider_failure", "provider_mismatch",
        }:
            kind = "provider-outage"
            key = "provider:outage"
            title = "Campaign provider is unavailable"
        else:
            kind = "campaign-blocked"
            key = "campaign:" + code
            title = str(blocker.get("message") or code)[:200]
        result[key] = _condition(
            kind=kind,
            severity="critical" if campaign.get("state") == "capped" else "warning",
            title=title,
            message=str(blocker.get("message") or title)[:1_000],
            repair=str(blocker.get("repair") or "Inspect the campaign blocker.")[:1_000],
            source_type="campaign" if kind == "campaign-blocked" else "provider",
            source_id=campaign_id,
            source_href=campaign_href,
        )
    service = campaign.get("service")
    if isinstance(service, dict) and service.get("status") in {"disconnected", "unknown"}:
        result["provider:service-unavailable"] = _condition(
            kind="provider-outage",
            severity="critical" if service.get("status") == "disconnected" else "warning",
            title="Campaign service is not connected",
            message="Current provider collection health cannot be established.",
            repair=str(service.get("repair") or "Inspect the local service, then explicitly resume if required.")[:1_000],
            source_type="service",
            source_id=campaign_id,
            source_href=campaign_href,
        )
    usage = campaign.get("usage") if isinstance(campaign.get("usage"), dict) else {}
    maximum_age = usage.get("maximum_source_age_seconds")
    coverage = campaign.get("coverage") if isinstance(campaign.get("coverage"), dict) else {}
    allowed_age = coverage.get("max_source_age_seconds")
    if (maximum_age is not None and allowed_age is not None
            and _number(maximum_age, "maximum source age")
            > _number(allowed_age, "allowed source age")):
        result["provider:stale"] = _condition(
            kind="stale-source-data",
            severity="warning",
            title="Provider evidence is stale",
            message=(f"Maximum retained source age is {float(maximum_age):g}s against "
                     f"the campaign limit of {float(allowed_age):g}s."),
            repair="Refresh the source; missed operating time remains a visible gap.",
            source_type="provider",
            source_id=campaign_id,
            source_href=campaign_href + "/provider",
        )
    for arm in campaign.get("arms", []):
        if not isinstance(arm, dict):
            raise EvidenceAlertError("Campaign arms must be objects")
        state = arm.get("state")
        fault = arm.get("fault")
        if fault is not None or state in {"interrupted", "stalled", "faulted"}:
            arm_id = _text(arm.get("id"), "arm id")
            fault_value = fault if isinstance(fault, dict) else {}
            result["arm:fault:" + arm_id] = _condition(
                kind="arm-faulted" if state == "faulted" or fault else "arm-stalled",
                severity="critical" if state == "faulted" or fault else "warning",
                title=f"Campaign arm {arm_id} is stalled or faulted",
                message=str(fault_value.get("message") or f"Arm state is {state}.")[:1_000],
                repair=str(fault_value.get("repair") or "Inspect the durable paper fault; healthy arms continue independently.")[:1_000],
                source_type="paper_run",
                source_id=str(arm.get("paper_run_id") or arm_id),
                source_href=campaign_href + "/arms/" + arm_id,
            )
    observations = [
        int(arm.get("counts", {}).get("observations", 0))
        for arm in campaign.get("arms", []) if isinstance(arm, dict)
    ]
    target = int(coverage.get("minimum_observations", 0))
    if observations and target > 0 and min(observations) >= target:
        result["milestone:minimum-observations"] = _condition(
            kind="evidence-milestone",
            severity="info",
            title="Preregistered observation milestone reached",
            message=(f"Every arm retained at least {target} observations; other "
                     "readiness gates still apply."),
            repair="Review the matched report; acknowledgment does not change readiness.",
            source_type="campaign",
            source_id=campaign_id,
            source_href=campaign_href + "/report",
        )
    for mismatch in mismatches:
        ident = _text(mismatch.get("id"), "accounting mismatch id")
        result["accounting:" + ident] = _condition(
            kind="accounting-mismatch",
            severity="critical",
            title="Accounting reconciliation mismatch",
            message=str(mismatch.get("reason") or "Receipt or balance evidence does not reconcile.")[:1_000],
            repair=str(mismatch.get("repair") or "Inspect source-linked receipt evidence.")[:1_000],
            source_type="accounting",
            source_id=ident,
            source_href=campaign_href + "/receipts/" + ident,
        )
    return result


def _evaluation_input(
    campaign: dict[str, Any], mismatches: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the alert-relevant, read-stable portion of a campaign view."""
    arms = []
    for arm in campaign.get("arms", []):
        if not isinstance(arm, dict):
            raise EvidenceAlertError("Campaign arms must be objects")
        arms.append({
            "id": arm.get("id"),
            "paper_run_id": arm.get("paper_run_id"),
            "state": arm.get("state"),
            "counts": arm.get("counts"),
            "fault": arm.get("fault"),
        })
    return {
        "campaign": {
            "id": campaign.get("id"),
            "state": campaign.get("state"),
            "updated_at": campaign.get("updated_at"),
            "blocker": campaign.get("blocker"),
            "service": campaign.get("service"),
            "coverage": campaign.get("coverage"),
            "usage": campaign.get("usage"),
            "arms": arms,
        },
        "accounting_mismatches": mismatches,
    }


def _condition(
    *, kind: str, severity: str, title: str, message: str, repair: str,
    source_type: str, source_id: str, source_href: str,
) -> dict[str, Any]:
    if severity not in {"info", "warning", "critical"}:
        raise EvidenceAlertError("Alert severity is unsupported")
    if not source_href.startswith("/") or source_href.startswith("//"):
        raise EvidenceAlertError("Alert source link must be a local absolute path")
    return {
        "kind": _text(kind, "alert kind"),
        "severity": severity,
        "title": _text(title, "alert title", maximum=200),
        "message": _text(message, "alert message", maximum=1_000),
        "repair": _text(repair, "alert repair", maximum=1_000),
        "source": {
            "type": _text(source_type, "alert source type"),
            "id": _text(source_id, "alert source id"),
            "href": source_href,
        },
    }


def _record(
    ident: str, campaign_id: str, key: str, generation: int,
    condition: dict[str, Any], *, first_at: float, last_at: float,
    count: int, acknowledged_at: float | None, resolved_at: float | None,
    state: str,
) -> dict[str, Any]:
    source = {**condition["source"], "kind": condition["source"]["type"]}
    return {
        "schema": ALERT_SCHEMA,
        "id": ident,
        "campaign_id": campaign_id,
        "condition_key": key,
        "kind": condition["kind"],
        "generation": generation,
        "occurrence_generation": generation,
        "recurrence_count": generation - 1,
        "severity": condition["severity"],
        "state": state,
        "title": condition["title"],
        "detail": condition["message"],
        "message": condition["message"],
        "repair": condition["repair"],
        "source": source,
        "first_at": first_at,
        "last_at": last_at,
        "first_occurred_at": first_at,
        "last_occurred_at": last_at,
        "occurrence_count": count,
        "acknowledged_at": acknowledged_at,
        "resolved_at": resolved_at,
        "acknowledgment_effect": (
            "records operator review only; it does not resolve the condition"
        ),
        "acknowledgment_changes_condition": False,
        "local_only": True,
        "external_delivery": False,
        "buy_sell_alert": False,
    }


def _values(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        record["id"], record["campaign_id"], record["condition_key"],
        record["generation"], record["severity"], record["state"],
        record["source"]["type"], record["source"]["id"],
        record["first_at"], record["last_at"], record["occurrence_count"],
        record["acknowledged_at"], record["resolved_at"], canonical_json(record),
    )


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceAlertError(f"{label} must be an object")
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise EvidenceAlertError(f"{label} must be canonical JSON") from exc


def _text(value: Any, label: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise EvidenceAlertError(f"{label} is invalid")
    return value.strip()


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceAlertError(f"{label} must be numeric")
    result = float(value)
    if result != result or result in {float("inf"), float("-inf")} or result < 0:
        raise EvidenceAlertError(f"{label} must be finite and nonnegative")
    return result


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
