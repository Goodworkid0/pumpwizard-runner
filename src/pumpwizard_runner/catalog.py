"""Offline strategy definitions and attributed, optional simulation snapshots.

No wallet or trading worker depends on this module's public-results endpoint.
Performance never participates in the identity of an imported configuration.
"""
from __future__ import annotations

import copy
from importlib.resources import files
import json
import math
from pathlib import Path
import urllib.request

from .strategies import parse_package


SOURCE = "https://pumpwizard.fun/api/public/summary"
SCHEMA = "solanamarket.strategy-catalog/v1"
PERIODS = {
    "All time": ("pnl_usd", "TOTAL"),
    "Last hour": ("pnl_1h_usd", "1H"),
    "Last 8 hours": ("pnl_8h_usd", "8H"),
    "Last 24 hours": ("pnl_24h_usd", "24H"),
}
REPORT_FIELDS = (
    "initial_usd", "equity_usd", "pnl_usd", "realized_net_pnl_usd", "unrealized_pnl_usd",
    "closed_count", "closed_wins", "closed_win_rate", "total_entries", "open_position_count",
    "started_at", "last_at", "pnl_1h_usd", "pnl_8h_usd", "pnl_24h_usd", "max_drawdown", "fees_usd", "failed_fees_usd",
)


def package_for(row: dict) -> dict:
    package = {
        "schema": "pumpwizard.strategy-handoff/v1",
        "strategy": {"id": row["id"], "label": row["label"]},
        "configuration": copy.deepcopy(row["config"]),
    }
    if row.get("trial"):
        package["trial"] = copy.deepcopy(row["trial"])
    return package


def number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def public_rows(document: dict) -> list[dict]:
    """Validate an entire publication before replacing any locally cached results."""
    if not isinstance(document, dict):
        raise ValueError("Invalid public snapshot")
    snapshot = document.get("_snapshot", {})
    if not isinstance(snapshot, dict):
        raise ValueError("Invalid snapshot metadata")
    at = snapshot.get("source_as_of")
    if not number(at) or at <= 0:
        raise ValueError("Simulation snapshot has no valid source timestamp")
    strategies = document.get("strategies")
    if not isinstance(strategies, list) or not 1 <= len(strategies) <= 5000:
        raise ValueError("Simulation snapshot has no bounded strategy list")
    result, seen = [], set()
    for item in strategies:
        report = item["report"]
        config = copy.deepcopy(report["config"])
        row = {
            "id": item["id"], "label": item["label"],
            "family": item.get("family") or config.get("entry_style", "momentum"),
            "description": item.get("description") or "PumpWizard forward simulation configuration.",
            "config": config, "trial": item.get("trial"),
            "report": {key: report.get(key) for key in REPORT_FIELDS},
            "source_as_of": at, "source_stale": bool(snapshot.get("stale")),
        }
        validate_row(row)
        if row["id"] in seen:
            raise ValueError("Duplicate strategy in simulation snapshot")
        seen.add(row["id"])
        result.append(row)
    return result


def validate_row(row: dict) -> None:
    parse_package(package_for(row))
    if not isinstance(row.get("family"), str) or len(row["family"]) > 80:
        raise ValueError("Invalid strategy family")
    if not isinstance(row.get("description"), str) or len(row["description"]) > 4000:
        raise ValueError("Invalid strategy description")
    report = row.get("report")
    if report is not None:
        if not isinstance(report, dict) or any(v is not None and not number(v) for v in report.values()):
            raise ValueError("Invalid simulation metrics")
        for field in ("initial_usd", "equity_usd", "pnl_usd", "closed_count", "total_entries"):
            if not number(report.get(field)):
                raise ValueError("Missing simulation metric: " + field)
        if report["initial_usd"] <= 0 or report["equity_usd"] < 0:
            raise ValueError("Invalid simulation balance")
        if not number(row.get("source_as_of")) or row["source_as_of"] <= 0:
            raise ValueError("Missing simulation timestamp")
        for field in ("closed_count", "total_entries", "closed_wins", "open_position_count"):
            value = report.get(field)
            if value is not None and (value < 0 or int(value) != value):
                raise ValueError("Invalid simulation count")
        for field in ("closed_win_rate", "max_drawdown"):
            value = report.get(field)
            if value is not None and not 0 <= value <= 1:
                raise ValueError("Invalid simulation fraction")


def read_catalog(raw: str) -> dict:
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError("Unknown strategy catalog format")
    rows = document.get("strategies")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError("Invalid strategy catalog size")
    seen = set()
    for row in rows:
        validate_row(row)
        if row["id"] in seen:
            raise ValueError("Duplicate catalog strategy")
        seen.add(row["id"])
    return document


def load_catalog(cache: Path | None = None) -> dict:
    bundled = read_catalog(files("pumpwizard_runner").joinpath("data/strategies.json").read_text(encoding="utf-8"))
    if cache and cache.exists():
        try:
            if cache.stat().st_size <= 12_000_000:
                cached = read_catalog(cache.read_text(encoding="utf-8"))
                rows = {row["id"]: row for row in bundled["strategies"]}
                for row in cached["strategies"]:
                    previous = rows.get(row["id"])
                    if previous is None or (row.get("source_as_of") or 0) > (previous.get("source_as_of") or 0):
                        rows[row["id"]] = row
                return dict(bundled, strategies=list(rows.values()))
        except (OSError, ValueError, KeyError, TypeError):
            pass  # A failed cache must not remove the bundled offline library.
    return bundled


def refresh_catalog(current: dict, cache: Path) -> dict:
    request = urllib.request.Request(SOURCE, headers={"Accept": "application/json", "User-Agent": "SolanaMarketWizard/1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read(12_000_001)
    if len(payload) > 12_000_000:
        raise ValueError("Simulation snapshot is too large")
    rows = public_rows(json.loads(payload))
    # Keep staged definitions and historical rows absent from a newer publication.
    # Each row keeps its own timestamp; never relabel old results with today's date.
    merged = {row["id"]: row for row in current["strategies"]}
    for row in rows:
        previous = merged.get(row["id"])
        if previous and (previous.get("source_as_of") or 0) > row["source_as_of"]:
            raise ValueError("Public snapshot is older than cached results")
        merged[row["id"]] = row
    document = dict(current, strategies=list(merged.values()))
    encoded = json.dumps(document, ensure_ascii=False, allow_nan=False)
    read_catalog(encoded)
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_suffix(".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(cache)
    return document


def period_performance(row: dict, period: str = "All time") -> tuple[float | None, float | None]:
    """Return period equity change and % of equity at the period's start."""
    field, _ = PERIODS[period]
    report = row.get("report") or {}
    pnl = report.get(field)
    if not number(pnl):
        return None, None
    baseline = report.get("initial_usd") if period == "All time" else report["equity_usd"] - pnl
    return pnl, pnl / baseline * 100 if number(baseline) and baseline > 0 else None


def select_rows(document: dict, query: str = "", family: str = "All families",
                cohort: str = "All versions", outcome: str = "All results",
                period: str = "All time") -> list[dict]:
    if period not in PERIODS:
        raise ValueError("Unknown performance period")
    rows = []
    for row in document["strategies"]:
        report = row.get("report")
        version = (row.get("trial") or {}).get("generation", "V2" if row.get("trial") else "Original")
        if query.lower() not in (row["id"] + " " + row["label"] + " " + row["family"]).lower():
            continue
        if family != "All families" and row["family"] != family:
            continue
        if cohort != "All versions" and cohort != version:
            continue
        if outcome == "Simulated" and report is None:
            continue
        if outcome == "Untested" and report is not None:
            continue
        pnl, _ = period_performance(row, period)
        if outcome == "Profitable" and (pnl is None or pnl <= 0):
            continue
        rows.append(row)
    def ranking(row):
        pnl, _ = period_performance(row, period)
        return pnl is None, -(pnl or 0), row["label"], row["id"]
    return sorted(rows, key=ranking)


def add_strategy(library, row: dict) -> tuple[str | None, bool]:
    """Pin exact rules; repeat clicks reuse the same local agent."""
    strategy = parse_package(package_for(row))
    created = library.import_strategy(strategy)
    if strategy.compatibility != "monitoring-ready":
        return None, created
    existing = next((x for x in library.instances() if x["strategy_digest"] == strategy.digest), None)
    if existing:
        return str(existing["id"]), False
    return library.create_instance(strategy.digest, entry_mode="disabled", exit_mode="manual"), True
