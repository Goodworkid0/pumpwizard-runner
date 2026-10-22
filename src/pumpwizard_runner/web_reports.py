"""Sanitized browser views of replay, shadow, and unsigned-worker evidence.

The original reports stay in their local stores for Python-led review. Browser
views expose only status summaries; a report may contain route/RPC evidence
that is unnecessary for the browser and must not become a transaction-preview
or signing surface.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

from .evaluation import reports as evaluation_reports


def _read_only(path: Path):
    if not path.exists():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def evaluations(data_dir: Path, library) -> list[dict]:
    rows = []
    for strategy in library.rows():
        for report in evaluation_reports(data_dir, strategy["digest"]):
            # Evaluation reports retain their exact package and recording locally so
            # they can be reproduced by Python. The browser needs the immutable
            # evidence summary, not another copy of potentially large user input.
            rows.append({
                "strategy_digest": strategy["digest"],
                "strategy_label": strategy["label"],
                "report": {
                    "id": report["id"],
                    "completed_at": report["completed_at"],
                    "input_origin": report["input_origin"],
                    "period": report["period"],
                    "summary": report["summary"],
                    "limitations": report["limitations"],
                    "submission_enabled": report["submission_enabled"],
                    "funded_acceptance": report["funded_acceptance"],
                },
            })
    return sorted(rows, key=lambda item: item["report"].get("completed_at", 0), reverse=True)[:100]


def shadow(data_dir: Path, agent_id: str | None = None) -> list[dict]:
    db = _read_only(Path(data_dir) / "agent-risk.sqlite")
    if db is None:
        return []
    try:
        query, args = "SELECT * FROM shadow_reports", ()
        if agent_id:
            query += " WHERE agent_id=?"
            args = (agent_id,)
        query += " ORDER BY id DESC LIMIT 100"
        summaries = []
        for row in db.execute(query, args):
            report = json.loads(row["payload"])
            results = report.get("results", []) if isinstance(report, dict) else []
            summaries.append({
                "id": row["id"],
                "at": row["at"],
                "agent_id": row["agent_id"],
                "result_count": len(results) if isinstance(results, list) else 0,
                "submission_enabled": False,
            })
        return summaries
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()


def worker(data_dir: Path, agent_id: str | None = None) -> list[dict]:
    db = _read_only(Path(data_dir) / "execution-worker.sqlite")
    if db is None:
        return []
    try:
        runs = []
        for row in db.execute("SELECT * FROM worker_runs ORDER BY started_at DESC LIMIT 100"):
            config = json.loads(row["config"])
            if agent_id and config.get("agent_id") != agent_id:
                continue
            lease = db.execute("SELECT * FROM worker_leases WHERE scope=?", (row["scope"],)).fetchone()
            active = bool(lease and lease["owner"] == row["id"] and -90 <= time.time() - lease["heartbeat"] <= 90)
            # Keep detailed cycles in the local SQLite evidence store. This
            # route intentionally returns no config, sender, route/RPC input,
            # candidate proposal, or transaction-related data.
            runs.append({"id": row["id"], "status": row["status"],
                         "started_at": row["started_at"], "updated_at": row["updated_at"], "next_at": row["next_at"],
                         "failures": row["failures"], "cycles": row["cycles"], "active": active,
                         "mode": config.get("mode", "unsigned")})
        return runs
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()


def replays(data_dir: Path) -> list[dict]:
    db = _read_only(Path(data_dir) / "strategy-replays.sqlite")
    if db is None:
        return []
    try:
        return [dict(id=row["id"], checkpoint=json.loads(row["checkpoint"]))
                for row in db.execute("SELECT * FROM portfolio_runs ORDER BY id LIMIT 100")]
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()
