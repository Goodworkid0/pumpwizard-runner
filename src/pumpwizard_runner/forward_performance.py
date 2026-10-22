"""Pure, reproducible forward-campaign performance and accounting reports.

Inputs are data snapshots supplied by the campaign owner.  This module has no
database, wallet, signer, RPC, scheduler, or order-submission dependency.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import math
import statistics
from typing import Any, Iterable

from .portfolio_replay import (
    ENGINE_VERSION as REPLAY_ENGINE_VERSION,
    MAX_LEDGER_MICROS,
    MAX_PRICE_UNITS_PER_TOKEN,
    MAX_TOKEN_ATOMS,
    MONEY_MICROS_PER_QUOTE,
    PPM,
    PRICE_UNITS_PER_QUOTE,
    REPORT_SCHEMA as REPLAY_REPORT_SCHEMA,
    build_holding_benchmark,
    quote_amount_micros,
    replay_portfolio,
)
from .strategies import canonical_json, content_hash


INPUT_SCHEMA = "pumpwizard.forward-performance-input/v1"
REPORT_SCHEMA = "pumpwizard.forward-performance-report/v1"
ACCOUNTING_INPUT_SCHEMA = "pumpwizard.accounting-snapshot-input/v1"
ACCOUNTING_REPORT_SCHEMA = "pumpwizard.accounting-reconciliation/v1"
READINESS_POLICY_SCHEMA = "pumpwizard.forward-evidence-readiness-policy/v1"
ENGINE_VERSION = "pumpwizard-forward-performance/1"
MAX_ARMS = 5
MAX_EVIDENCE_IDS = 10_000
MAX_DRILLDOWN_ROWS = 10_000
MAX_DAILY_ROWS = 366


class ForwardPerformanceError(ValueError):
    """A performance snapshot is ambiguous, inconsistent, or unbounded."""


def _canonical(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ForwardPerformanceError(f"{field} must be an object")
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ForwardPerformanceError(f"{field} must be canonical JSON data") from exc


def _text(value: object, field: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ForwardPerformanceError(f"{field} must be non-empty bounded text")
    return value.strip()


def _integer(
    value: object,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ForwardPerformanceError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ForwardPerformanceError(f"{field} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ForwardPerformanceError(f"{field} must be <= {maximum}")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ForwardPerformanceError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ForwardPerformanceError(f"{field} must be a finite number")
    return result


def _whole_number(
    value: object, field: str, *, minimum: int | None = None,
) -> int:
    """Accept an integral replay metric even when float time math emitted ``.0``."""
    number = _number(value, field)
    if not number.is_integer():
        raise ForwardPerformanceError(f"{field} must be an integer-valued number")
    result = int(number)
    if minimum is not None and result < minimum:
        raise ForwardPerformanceError(f"{field} must be >= {minimum}")
    return result


def _digest(value: object, field: str) -> str:
    text = _text(value, field, maximum=64)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ForwardPerformanceError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _intervals(
    values: object, field: str, *, window_start: float, window_end: float,
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        raise ForwardPerformanceError(f"{field} must be a list")
    if len(values) > MAX_DRILLDOWN_ROWS:
        raise ForwardPerformanceError(f"{field} exceeds the report row bound")
    result = []
    previous_end = window_start
    for index, raw in enumerate(values):
        row = _canonical(raw, f"{field}[{index}]")
        started = _number(row.get("started_at"), f"{field}[{index}].started_at")
        ended = _number(row.get("ended_at"), f"{field}[{index}].ended_at")
        if not window_start <= started < ended <= window_end:
            raise ForwardPerformanceError(f"{field} intervals must be within the report window")
        if started < previous_end:
            raise ForwardPerformanceError(f"{field} intervals must not overlap")
        previous_end = ended
        result.append({
            "id": _text(row.get("id"), f"{field}[{index}].id"),
            "started_at": started,
            "ended_at": ended,
            "reason": _text(row.get("reason", "campaign-operating"), f"{field}[{index}].reason"),
        })
    return result


def _gaps(
    values: object, field: str, *, window_start: float, window_end: float,
) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        raise ForwardPerformanceError(f"{field} must be a list")
    if len(values) > MAX_DRILLDOWN_ROWS:
        raise ForwardPerformanceError(f"{field} exceeds the report row bound")
    result = []
    for index, raw in enumerate(values):
        row = _canonical(raw, f"{field}[{index}]")
        started = _number(row.get("started_at"), f"{field}[{index}].started_at")
        ended_value = row.get("ended_at", window_end)
        ended = window_end if ended_value is None else _number(
            ended_value, f"{field}[{index}].ended_at"
        )
        if not window_start <= started <= ended <= window_end:
            raise ForwardPerformanceError(f"{field} gaps must be within the report window")
        result.append({
            "id": _text(row.get("id"), f"{field}[{index}].id"),
            "started_at": started,
            "ended_at": ended,
            "reason": _text(row.get("reason"), f"{field}[{index}].reason"),
        })
    ordered = sorted(result, key=lambda row: (row["started_at"], row["ended_at"], row["id"]))
    merged = []
    for row in ordered:
        if not merged or row["started_at"] > merged[-1]["ended_at"]:
            merged.append(row)
            continue
        previous = merged[-1]
        sources = sorted(set(previous.get("source_ids", [previous["id"]])) | {row["id"]})
        reasons = sorted(set(previous["reason"].split(" + ")) | {row["reason"]})
        previous.update(
            id="merged:" + content_hash(sources)[:24],
            ended_at=max(previous["ended_at"], row["ended_at"]),
            reason=" + ".join(reasons),
            source_ids=sources,
        )
    return merged


def _duration(rows: Iterable[dict[str, Any]]) -> float:
    return sum(float(row["ended_at"]) - float(row["started_at"]) for row in rows)


def _intersection(interval_sets: list[list[dict[str, Any]]]) -> list[dict[str, float]]:
    if not interval_sets:
        return []
    current = [
        {"started_at": float(row["started_at"]), "ended_at": float(row["ended_at"])}
        for row in interval_sets[0]
    ]
    for rows in interval_sets[1:]:
        following = []
        for left in current:
            for right in rows:
                start = max(left["started_at"], float(right["started_at"]))
                end = min(left["ended_at"], float(right["ended_at"]))
                if start < end:
                    following.append({"started_at": start, "ended_at": end})
        current = following
    return current


def _day(at: object, field: str) -> str:
    value = _number(at, field)
    try:
        return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise ForwardPerformanceError(f"{field} is outside supported UTC timestamps") from exc


def _replay_report(value: object, field: str) -> dict[str, Any]:
    report = _canonical(value, field)
    if report.get("schema") == "pumpwizard.incremental-paper-report/v2":
        report = _canonical(report.get("replay"), field + ".replay")
    if report.get("schema") != REPLAY_REPORT_SCHEMA:
        raise ForwardPerformanceError(f"{field} must contain a v3 portfolio replay report")
    return report


def _daily(replay: dict[str, Any]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}

    def item(day: str) -> dict[str, Any]:
        return rows.setdefault(day, {
            "day_utc": day,
            "cash_change_micros": 0,
            "realized_pnl_micros": 0,
            "cost_micros": 0,
            "fills": 0,
            "last_conservative_equity_micros": None,
            "valuation_complete": None,
        })

    for index, event in enumerate(replay.get("cash_ledger", [])):
        row = _canonical(event, f"cash_ledger[{index}]")
        item(_day(row.get("at"), f"cash_ledger[{index}].at"))["cash_change_micros"] += _integer(
            row.get("delta_micros"), f"cash_ledger[{index}].delta_micros"
        )
    for index, fill in enumerate(replay.get("fills", [])):
        row = _canonical(fill, f"fills[{index}]")
        target = item(_day(row.get("at"), f"fills[{index}].at"))
        target["fills"] += 1
        if row.get("side") == "exit":
            target["realized_pnl_micros"] += _integer(
                row.get("realized_pnl_micros"), f"fills[{index}].realized_pnl_micros"
            )
        costs = row.get("costs", {})
        if not isinstance(costs, dict):
            raise ForwardPerformanceError(f"fills[{index}].costs must be an object")
        target["cost_micros"] += sum(
            _integer(value, f"fills[{index}].costs.{name}", minimum=0)
            for name, value in costs.items()
        )
    for index, point in enumerate(replay.get("equity_curve", [])):
        row = _canonical(point, f"equity_curve[{index}]")
        target = item(_day(row.get("at"), f"equity_curve[{index}].at"))
        target["last_conservative_equity_micros"] = _integer(
            row.get("conservative_equity_micros"),
            f"equity_curve[{index}].conservative_equity_micros",
            minimum=0,
        )
        target["valuation_complete"] = bool(row.get("valuation_complete"))
    result = [rows[key] for key in sorted(rows)]
    if len(result) > MAX_DAILY_ROWS:
        raise ForwardPerformanceError(
            f"Daily report exceeds the {MAX_DAILY_ROWS}-day snapshot bound"
        )
    return result


def _uncertainty(
    values: object, *, independent_count: int, minimum: int,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(values, list):
        raise ForwardPerformanceError("independent_episode_returns_micros must be a list")
    returns = [
        _integer(value, "independent_episode_returns_micros value") for value in values
    ]
    if len(returns) != independent_count:
        return None, "return_count_does_not_match_independent_episode_count"
    if independent_count < minimum or len(returns) < 2:
        return None, "insufficient_independent_episodes"
    mean = statistics.fmean(returns)
    standard_error = statistics.stdev(returns) / math.sqrt(len(returns))
    return {
        "method": "normal-approximation-95-percent-interval",
        "assumption": "campaign preregistration identifies these episodes as independent",
        "episodes": len(returns),
        "mean_micros": round(mean),
        "lower_micros": round(mean - 1.96 * standard_error),
        "upper_micros": round(mean + 1.96 * standard_error),
    }, None


def _readiness_policy(value: object, *, window_start: float) -> dict[str, Any]:
    policy = _canonical(value, "readiness_policy")
    if policy.get("schema") != READINESS_POLICY_SCHEMA:
        raise ForwardPerformanceError(
            f"readiness_policy.schema must be {READINESS_POLICY_SCHEMA}"
        )
    registered = _number(policy.get("registered_at"), "readiness_policy.registered_at")
    if registered > window_start:
        raise ForwardPerformanceError("Readiness policy must be registered before observation")
    for name in (
        "minimum_independent_episodes",
        "minimum_independent_tokens",
        "minimum_observed_seconds",
        "minimum_coverage_ppm",
        "minimum_stability_groups",
        "maximum_gap_seconds",
        "uncertainty_minimum_independent_episodes",
    ):
        _integer(policy.get(name), f"readiness_policy.{name}", minimum=0)
    _text(policy.get("required_engine_version"), "readiness_policy.required_engine_version")
    return policy


def _arm_summary(
    raw: object,
    *,
    window_start: float,
    window_end: float,
    policy: dict[str, Any],
) -> dict[str, Any]:
    arm = _canonical(raw, "arm")
    ident = _text(arm.get("id"), "arm.id")
    kind = arm.get("kind")
    if kind not in {"observed_paper", "retrospective_scenario", "cash", "holding"}:
        raise ForwardPerformanceError(f"arm {ident} has an unsupported kind")
    capital = _integer(arm.get("declared_capital_micros"), "arm.declared_capital_micros", minimum=1)
    intervals = _intervals(
        arm.get("operating_intervals"),
        f"arm {ident} operating_intervals",
        window_start=window_start,
        window_end=window_end,
    )
    gaps = _gaps(
        arm.get("gaps", []),
        f"arm {ident} gaps",
        window_start=window_start,
        window_end=window_end,
    )
    evidence_ids = arm.get("matched_evidence_ids")
    if not isinstance(evidence_ids, list) or len(evidence_ids) > MAX_EVIDENCE_IDS:
        raise ForwardPerformanceError(f"arm {ident} matched_evidence_ids is invalid or unbounded")
    evidence_ids = [_text(value, f"arm {ident} evidence ID") for value in evidence_ids]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ForwardPerformanceError(f"arm {ident} evidence IDs must be unique")
    evidence_digest = content_hash(evidence_ids)
    active_seconds = _duration(intervals)
    total_window = window_end - window_start
    gap_seconds = _duration(gaps)
    longest_gap = max(
        (row["ended_at"] - row["started_at"] for row in gaps), default=0.0
    )

    unavailable_reason = arm.get("unavailable_reason")
    if unavailable_reason is not None:
        if kind != "holding":
            raise ForwardPerformanceError("Only the declared holding benchmark may be unavailable")
        unavailable_reason = _text(unavailable_reason, f"arm {ident} unavailable_reason")
        replay = None
    else:
        replay = None if kind == "cash" else _replay_report(arm.get("report"), f"arm {ident}.report")

    if kind == "cash":
        metrics = {
            "conservative_ending_equity_micros": capital,
            "net_modeled_pnl_micros": 0,
            "realized_pnl_micros": 0,
            "known_unrealized_pnl_micros": 0,
            "unknown_unpriced_basis_micros": 0,
            "valuation_complete": True,
            "maximum_drawdown_ppm": 0,
            "time_weighted_exposure_ppm": 0,
            "turnover_micros": 0,
            "opportunities": 0,
            "resolved_opportunities": 0,
            "independent_episode_count": 0,
            "independent_token_count": 0,
            "dependence_group_count": 0,
            "censored_positions": 0,
            "unpriced_positions": 0,
            "modeled_cost_micros": 0,
        }
        engine_version = ENGINE_VERSION
        daily = []
        curve = [
            {
                "at": window_start,
                "equity_usd": capital / MONEY_MICROS_PER_QUOTE,
                "segment_id": intervals[0]["id"] if intervals else None,
                "gap_reason": None if intervals else "not_operating",
            },
            {
                "at": window_end,
                "equity_usd": capital / MONEY_MICROS_PER_QUOTE,
                "segment_id": intervals[-1]["id"] if intervals else None,
                "gap_reason": None if intervals else "not_operating",
            },
        ]
        drilldown = {"fills": [], "cash_ledger": [], "open_positions": {}, "exclusions": []}
        report_id = None
    elif replay is None:
        metrics = None
        engine_version = None
        daily = []
        curve = []
        drilldown = {"fills": [], "cash_ledger": [], "open_positions": {}, "exclusions": []}
        report_id = None
    else:
        replay_capital = replay.get("capital", {})
        if replay_capital.get("unit") != "quote-micros":
            raise ForwardPerformanceError(f"arm {ident} report must use quote-micros")
        if replay_capital.get("starting_cash_micros") != capital:
            raise ForwardPerformanceError(f"arm {ident} declared capital differs from its report")
        source = replay.get("metrics")
        if not isinstance(source, dict):
            raise ForwardPerformanceError(f"arm {ident} report metrics are missing")
        fills = replay.get("fills", [])
        cash_ledger = replay.get("cash_ledger", [])
        if not isinstance(fills, list) or not isinstance(cash_ledger, list):
            raise ForwardPerformanceError(f"arm {ident} report ledgers must be lists")
        if len(fills) + len(cash_ledger) > MAX_DRILLDOWN_ROWS:
            raise ForwardPerformanceError(f"arm {ident} drill-down exceeds its row bound")
        turnover = sum(
            abs(_integer(fill.get("cash_delta_micros"), f"arm {ident} fill cash delta"))
            for fill in fills
        )
        costs = replay.get("cost_breakdown", {})
        if not isinstance(costs, dict):
            raise ForwardPerformanceError(f"arm {ident} cost_breakdown must be an object")
        modeled_cost = sum(
            _integer(value, f"arm {ident} cost {name}", minimum=0)
            for name, value in costs.items()
        )
        open_positions = replay.get("open_positions", {})
        opportunities = replay.get("opportunities", [])
        if not isinstance(open_positions, dict) or not isinstance(opportunities, list):
            raise ForwardPerformanceError(f"arm {ident} position/opportunity ledgers are invalid")
        censored_statuses = {"observed", "reserved", "filled"}
        censored = sum(row.get("status") in censored_statuses for row in opportunities)
        metrics = {
            "conservative_ending_equity_micros": _integer(
                source.get("conservative_ending_equity_micros"),
                f"arm {ident} ending equity",
                minimum=0,
            ),
            "net_modeled_pnl_micros": _integer(
                source.get("net_result_micros"), f"arm {ident} net result"
            ),
            "realized_pnl_micros": _integer(
                source.get("realized_pnl_micros"), f"arm {ident} realized pnl"
            ),
            "known_unrealized_pnl_micros": _integer(
                source.get("known_unrealized_pnl_micros"),
                f"arm {ident} unrealized pnl",
            ),
            "unknown_unpriced_basis_micros": _integer(
                source.get("unknown_unpriced_basis_micros"),
                f"arm {ident} unknown basis",
                minimum=0,
            ),
            "valuation_complete": bool(source.get("valuation_complete")),
            "maximum_drawdown_ppm": _integer(
                source.get("maximum_drawdown_ppm"), f"arm {ident} drawdown", minimum=0
            ),
            "time_weighted_exposure_ppm": _whole_number(
                source.get("time_weighted_exposure_ppm"),
                f"arm {ident} exposure",
                minimum=0,
            ),
            "turnover_micros": turnover,
            "opportunities": _integer(
                source.get("opportunities"), f"arm {ident} opportunities", minimum=0
            ),
            "resolved_opportunities": _integer(
                source.get("resolved_opportunities"),
                f"arm {ident} resolved opportunities",
                minimum=0,
            ),
            "independent_episode_count": _integer(
                source.get("independent_episode_count"),
                f"arm {ident} independent episodes",
                minimum=0,
            ),
            "independent_token_count": len({
                row.get("mint") for row in opportunities if isinstance(row.get("mint"), str)
            }),
            "dependence_group_count": _integer(
                source.get("dependence_group_count"),
                f"arm {ident} dependence groups",
                minimum=0,
            ),
            "censored_positions": censored,
            "unpriced_positions": sum(
                position.get("valuation_status") != "priced"
                for position in open_positions.values()
            ),
            "modeled_cost_micros": modeled_cost,
        }
        engine_version = _text(replay.get("engine_version"), f"arm {ident} engine_version")
        report_id = _text(replay.get("id"), f"arm {ident} report id")
        daily = _daily(replay)
        curve = [
            {
                "at": _number(point.get("at"), f"arm {ident} equity point time"),
                "equity_usd": (
                    _integer(
                        point.get("conservative_equity_micros"),
                        f"arm {ident} conservative equity",
                        minimum=0,
                    ) / MONEY_MICROS_PER_QUOTE
                ),
                "segment_id": next(
                    (
                        interval["id"] for interval in intervals
                        if interval["started_at"] <= float(point.get("at")) <= interval["ended_at"]
                    ),
                    None,
                ),
                "gap_reason": (
                    None if bool(point.get("valuation_complete"))
                    else "valuation_incomplete"
                ),
            }
            for point in replay.get("equity_curve", [])
        ]
        drilldown = {
            "fills": copy.deepcopy(fills),
            "cash_ledger": copy.deepcopy(cash_ledger),
            "open_positions": copy.deepcopy(open_positions),
            "exclusions": copy.deepcopy(replay.get("exclusions", [])),
        }

    blockers = []
    uncertainty = None
    uncertainty_reason = None
    if metrics is None:
        blockers.append("declared_holding_benchmark_unavailable")
    elif kind not in {"cash"}:
        checks = (
            (metrics["independent_episode_count"] < policy["minimum_independent_episodes"],
             "insufficient_independent_episodes"),
            (metrics["independent_token_count"] < policy["minimum_independent_tokens"],
             "insufficient_independent_tokens"),
            (metrics["dependence_group_count"] < policy["minimum_stability_groups"],
             "insufficient_stability_groups"),
            (active_seconds < policy["minimum_observed_seconds"],
             "insufficient_observed_duration"),
            (longest_gap > policy["maximum_gap_seconds"], "gap_limit_exceeded"),
            (metrics["valuation_complete"] is False, "valuation_incomplete"),
            (engine_version != policy["required_engine_version"], "engine_identity_changed"),
        )
        blockers.extend(reason for failed, reason in checks if failed)
        coverage_ppm = (
            metrics["resolved_opportunities"] * PPM // metrics["opportunities"]
            if metrics["opportunities"] else 0
        )
        if coverage_ppm < policy["minimum_coverage_ppm"]:
            blockers.append("insufficient_opportunity_coverage")
        uncertainty, uncertainty_reason = _uncertainty(
            arm.get("independent_episode_returns_micros", []),
            independent_count=metrics["independent_episode_count"],
            minimum=policy["uncertainty_minimum_independent_episodes"],
        )

    stress = []
    scenarios = arm.get("stress_reports", [])
    if not isinstance(scenarios, list) or len(scenarios) > 10:
        raise ForwardPerformanceError(f"arm {ident} stress_reports must be a bounded list")
    for index, raw_scenario in enumerate(scenarios):
        scenario = _canonical(raw_scenario, f"arm {ident} stress_reports[{index}]")
        scenario_id = _text(scenario.get("id"), f"arm {ident} stress id")
        scenario_kind = scenario.get("kind")
        if scenario_kind not in {"cost", "latency", "cost_and_latency"}:
            raise ForwardPerformanceError(f"arm {ident} stress kind is unsupported")
        stressed = _replay_report(scenario.get("report"), f"arm {ident} stress report")
        if stressed.get("capital", {}).get("starting_cash_micros") != capital:
            raise ForwardPerformanceError(f"arm {ident} stress changed declared capital")
        stressed_net = _integer(
            stressed.get("metrics", {}).get("net_result_micros"),
            f"arm {ident} stress net result",
        )
        base_net = metrics["net_modeled_pnl_micros"] if metrics is not None else 0
        stress.append({
            "id": scenario_id,
            "kind": scenario_kind,
            "assumption_provenance": _text(
                scenario.get("assumption_provenance"),
                f"arm {ident} stress assumption_provenance",
            ),
            "report_id": _text(stressed.get("id"), f"arm {ident} stress report id"),
            "net_modeled_pnl_micros": stressed_net,
            "difference_from_observed_history_micros": stressed_net - base_net,
            "history_mutated": False,
        })

    return {
        "id": ident,
        "kind": kind,
        "label": _text(arm.get("label", ident), f"arm {ident} label"),
        "run_id": arm.get("run_id") if isinstance(arm.get("run_id"), str) else None,
        "strategy_sha256": (
            _digest(arm["strategy_sha256"], f"arm {ident} strategy_sha256")
            if arm.get("strategy_sha256") is not None else None
        ),
        "report_id": report_id,
        "engine_version": engine_version,
        "declared_capital_micros": capital,
        "capital_unit": "quote-micros",
        "starting_capital_micros": capital,
        "ending_capital_micros": (
            None if metrics is None else metrics["conservative_ending_equity_micros"]
        ),
        "net_result_micros": (
            None if metrics is None else metrics["net_modeled_pnl_micros"]
        ),
        "valuation_complete": (
            False if metrics is None else metrics["valuation_complete"]
        ),
        "matched_evidence_count": len(evidence_ids),
        "operating_intervals": intervals,
        "gaps": gaps,
        "active_seconds": active_seconds,
        "gap_seconds": gap_seconds,
        "longest_gap_seconds": longest_gap,
        "uptime_ppm": round(active_seconds * PPM / total_window),
        "matched_evidence_ids": evidence_ids,
        "matched_evidence_sha256": evidence_digest,
        "metrics": metrics,
        "daily": daily,
        "curve": curve,
        "drilldown": drilldown,
        "stress": stress,
        "unavailable_reason": unavailable_reason,
        "readiness": {
            "status": "evidence_ready_for_review" if not blockers else "exploratory",
            "blockers": blockers,
            "uncertainty": uncertainty,
            "uncertainty_withheld_reason": uncertainty_reason,
            "live_or_execution_readiness": False,
        },
    }


def _provider_costs(value: object, *, arm_ids: set[str], quote_currency: str) -> dict[str, Any]:
    costs = _canonical(value, "provider_costs")
    status = costs.get("status")
    if status not in {"known", "partial", "unknown"}:
        raise ForwardPerformanceError("provider_costs.status is unsupported")
    if costs.get("currency") != quote_currency:
        raise ForwardPerformanceError("Provider costs need the campaign quote currency")
    amounts = {}
    for name in (
        "observed_charges_micros",
        "configured_estimates_micros",
        "shared_fixed_cost_micros",
    ):
        value = costs.get(name)
        amounts[name] = None if value is None else _integer(
            value, f"provider_costs.{name}", minimum=0
        )
    unknown = costs.get("unknown_components")
    if not isinstance(unknown, list):
        raise ForwardPerformanceError("provider_costs.unknown_components must be a list")
    unknown = [_text(item, "provider unknown component") for item in unknown]
    allocations = costs.get("allocation_by_arm_micros", {})
    if not isinstance(allocations, dict) or set(allocations) - arm_ids:
        raise ForwardPerformanceError("Provider cost allocation names an unknown arm")
    allocations = {
        key: _integer(value, f"provider cost allocation {key}", minimum=0)
        for key, value in allocations.items()
    }
    fully_known = status == "known" and not unknown and all(
        value is not None for value in amounts.values()
    )
    if status == "known" and not fully_known:
        raise ForwardPerformanceError("Known provider cost status requires every cost component")
    known_total = sum(value for value in amounts.values() if value is not None)
    if allocations and sum(allocations.values()) > known_total:
        raise ForwardPerformanceError("Provider arm allocations exceed known campaign cost")
    return {
        "status": status,
        **amounts,
        "known_total_micros": known_total,
        "unknown_components": unknown,
        "allocation_by_arm_micros": allocations,
        "fully_costed": fully_known,
        "unknown_is_zero": False,
        "shared_cost_counted_once": True,
    }


def build_forward_performance_report(value: dict[str, Any]) -> dict[str, Any]:
    """Build one bounded, content-addressed campaign performance snapshot."""
    report_input = _canonical(value, "performance input")
    if report_input.get("schema") != INPUT_SCHEMA:
        raise ForwardPerformanceError(f"performance input schema must be {INPUT_SCHEMA}")
    campaign = _canonical(report_input.get("campaign"), "campaign")
    campaign_id = _text(campaign.get("id"), "campaign.id")
    manifest_sha256 = _digest(campaign.get("manifest_sha256"), "campaign.manifest_sha256")
    quote_currency = _text(campaign.get("quote_currency"), "campaign.quote_currency")
    snapshot = _canonical(report_input.get("snapshot"), "snapshot")
    as_of = _number(snapshot.get("as_of"), "snapshot.as_of")
    created_at = _number(snapshot.get("created_at", as_of), "snapshot.created_at")
    if created_at < as_of:
        raise ForwardPerformanceError("snapshot.created_at cannot precede its as_of evidence")
    window_start = _number(snapshot.get("window_start"), "snapshot.window_start")
    window_end = _number(snapshot.get("window_end"), "snapshot.window_end")
    if not window_start < window_end <= as_of:
        raise ForwardPerformanceError("Snapshot window must end at or before as_of")
    revision = _integer(snapshot.get("revision"), "snapshot.revision", minimum=1)
    correction_of = snapshot.get("correction_of_report_id")
    if revision == 1 and correction_of is not None:
        raise ForwardPerformanceError("The first report revision cannot be a correction")
    if revision > 1:
        correction_of = _text(correction_of, "snapshot.correction_of_report_id")
    policy = _readiness_policy(report_input.get("readiness_policy"), window_start=window_start)
    lineage = _canonical(report_input.get("lineage"), "lineage")
    _digest(lineage.get("source_snapshot_sha256"), "lineage.source_snapshot_sha256")
    hypothesis = _digest(
        lineage.get("selection_hypothesis_sha256"),
        "lineage.selection_hypothesis_sha256",
    )
    experiment_id = _text(lineage.get("experiment_id"), "lineage.experiment_id")
    prior_reports = lineage.get("prior_report_ids", [])
    if not isinstance(prior_reports, list):
        raise ForwardPerformanceError("lineage.prior_report_ids must be a list")
    prior_reports = [_text(item, "lineage prior report ID") for item in prior_reports]
    previous_hypothesis = lineage.get("supersedes_hypothesis_sha256")
    if previous_hypothesis is not None:
        _digest(previous_hypothesis, "lineage.supersedes_hypothesis_sha256")
        if not prior_reports:
            raise ForwardPerformanceError(
                "A changed selection hypothesis must preserve prior report history"
            )
    holdout_status = lineage.get("holdout_status")
    if holdout_status not in {"not_applicable", "unseen", "opened"}:
        raise ForwardPerformanceError("lineage.holdout_status is unsupported")
    arms_raw = report_input.get("arms")
    if not isinstance(arms_raw, list) or not 2 <= len(arms_raw) <= MAX_ARMS:
        raise ForwardPerformanceError("Performance input needs two to five arms/benchmarks")
    arms = [
        _arm_summary(
            arm,
            window_start=window_start,
            window_end=window_end,
            policy=policy,
        )
        for arm in arms_raw
    ]
    ids = [arm["id"] for arm in arms]
    if len(ids) != len(set(ids)):
        raise ForwardPerformanceError("Arm IDs must be unique")
    if sum(arm["kind"] == "cash" for arm in arms) != 1:
        raise ForwardPerformanceError("Performance input needs exactly one cash benchmark")
    if sum(arm["kind"] == "holding" for arm in arms) != 1:
        raise ForwardPerformanceError("Performance input needs exactly one holding benchmark")
    if not any(arm["kind"] in {"observed_paper", "retrospective_scenario"} for arm in arms):
        raise ForwardPerformanceError("Performance input needs a strategy arm")
    capital_mismatch = len({arm["declared_capital_micros"] for arm in arms}) != 1
    provider_costs = _provider_costs(
        report_input.get("provider_costs"), arm_ids=set(ids), quote_currency=quote_currency
    )

    common_intervals = _intersection([arm["operating_intervals"] for arm in arms])
    interval_shapes = {
        tuple((row["started_at"], row["ended_at"]) for row in arm["operating_intervals"])
        for arm in arms
    }
    evidence_digests = {arm["matched_evidence_sha256"] for arm in arms}
    comparison_blockers = []
    if capital_mismatch:
        comparison_blockers.append("mismatched_declared_capital")
    if len(interval_shapes) != 1:
        comparison_blockers.append("mismatched_arm_operating_intervals")
    if len(evidence_digests) != 1:
        comparison_blockers.append("mismatched_arm_evidence")
    if any(arm["metrics"] is None for arm in arms):
        comparison_blockers.append("declared_holding_benchmark_unavailable")
    if any(arm["engine_version"] not in {ENGINE_VERSION, policy["required_engine_version"]}
           for arm in arms if arm["engine_version"] is not None):
        comparison_blockers.append("unstable_engine_identity")
    comparable = not comparison_blockers
    allocations = provider_costs["allocation_by_arm_micros"]
    ranked = []
    if comparable:
        for arm in arms:
            net = arm["metrics"]["net_modeled_pnl_micros"]
            allocated = allocations.get(arm["id"])
            ranked.append({
                "arm_id": arm["id"],
                "kind": arm["kind"],
                "net_modeled_pnl_micros": net,
                "allocated_provider_cost_micros": allocated,
                "net_after_allocated_provider_cost_micros": (
                    net - allocated if allocated is not None else None
                ),
            })
        ranked.sort(key=lambda row: (-row["net_modeled_pnl_micros"], row["arm_id"]))
    trading = [row for row in ranked if row["kind"] not in {"cash"}]
    if not comparable:
        recommendation = {
            "action": "withhold_ranking",
            "reasons": comparison_blockers,
            "arm_id": None,
        }
    elif trading and all(row["net_modeled_pnl_micros"] <= 0 for row in trading):
        recommendation = {
            "action": "prefer_cash",
            "reasons": ["all_matched_trading_arms_lost_to_cash"],
            "arm_id": next(arm["id"] for arm in arms if arm["kind"] == "cash"),
        }
    else:
        recommendation = {
            "action": "modeled_leader_only",
            "reasons": ["matched_modeled_result_ranked_without_execution_authority"],
            "arm_id": ranked[0]["arm_id"],
        }
    profitability = {
        "status": "fully_costed" if provider_costs["fully_costed"] else "not_fully_costed",
        "claim_supported": bool(provider_costs["fully_costed"] and comparable),
        "blockers": [] if provider_costs["fully_costed"] else ["provider_price_or_cost_unknown"],
    }

    receipt_report = report_input.get("receipt_lineage")
    receipt_lineage = None
    receipt_rows = []
    if receipt_report is not None:
        receipt_report = _canonical(receipt_report, "receipt_lineage")
        if receipt_report.get("schema") != "pumpwizard.receipt-evidence-report/v1":
            raise ForwardPerformanceError("receipt_lineage must be a receipt evidence report")
        receipt_lineage = {
            "report_id": _text(receipt_report.get("id"), "receipt_lineage.id"),
            "counts": copy.deepcopy(receipt_report.get("counts", {})),
            "evidence_class": "recorded_execution",
            "paper_fills_reclassified": False,
        }
        for index, raw_receipt in enumerate(receipt_report.get("receipts", [])):
            receipt = _canonical(raw_receipt, f"receipt_lineage.receipts[{index}]")
            amounts = receipt.get("amounts", {})
            if not isinstance(amounts, dict):
                raise ForwardPerformanceError("Receipt amounts must be an object")
            owner = receipt.get("ownership", {})
            if isinstance(owner, dict):
                ownership = str(owner.get("kind", "unknown"))
                if owner.get("agent_id"):
                    ownership += ":" + str(owner["agent_id"])
            else:
                ownership = "unknown"
            receipt_rows.append({
                "id": _text(receipt.get("id"), f"receipt_lineage.receipts[{index}].id"),
                "evidence_class": "recorded_execution",
                "classification": _text(
                    receipt.get("classification"),
                    f"receipt_lineage.receipts[{index}].classification",
                ),
                "record_kind": _text(
                    receipt.get("record_kind"),
                    f"receipt_lineage.receipts[{index}].record_kind",
                ),
                "source_label": _text(
                    receipt.get("source_label"),
                    f"receipt_lineage.receipts[{index}].source_label",
                ),
                "occurred_at": receipt.get("finalized_at") or receipt.get("created_at"),
                "ownership": ownership,
                "cash_delta": (
                    None if amounts.get("cash_delta_lamports") is None
                    else f"{amounts['cash_delta_lamports']} lamports"
                ),
                "token_delta": (
                    None if amounts.get("token_delta_atoms") is None
                    else f"{amounts['token_delta_atoms']} atoms"
                ),
                "fee": (
                    None if amounts.get("fee_lamports") is None
                    else f"{amounts['fee_lamports']} lamports"
                ),
                "amounts": copy.deepcopy(amounts),
                "units": copy.deepcopy(receipt.get("units", {})),
                "strategy_profit_treatment": (
                    "excluded_external_flow"
                    if receipt.get("record_kind") in {"withdrawal", "deposit", "transfer"}
                    else "fee_cost_only"
                    if receipt.get("classification") == "fee_only_failure"
                    else "unavailable_until_final"
                    if receipt.get("classification") in {"pending", "unresolved"}
                    else "requires_matched_basis_and_costs"
                ),
                "discrepancy": (
                    None if receipt.get("classification") in {
                        "finalized_success", "fee_only_failure"
                    } else str(receipt.get("receipt_status") or "unresolved receipt")
                ),
                "source_href": (
                    f"/research/campaigns/{campaign_id}/receipts/{receipt.get('id')}"
                ),
            })

    accounting = report_input.get("accounting_reconciliation")
    if accounting is not None:
        accounting = _canonical(accounting, "accounting_reconciliation")
        if accounting.get("schema") != ACCOUNTING_REPORT_SCHEMA:
            raise ForwardPerformanceError(
                "accounting_reconciliation must be a v1 accounting reconciliation"
            )
        conservation = accounting.get("conservation", {})
        accounting_performance = accounting.get("performance", {})
        if conservation.get("complete") and accounting_performance.get(
            "strategy_net_result_micros"
        ) is not None:
            reconciliation_status = "reconciled"
        elif not all(conservation.get(name) for name in ("cash", "token_atoms")):
            reconciliation_status = "mismatch"
        else:
            reconciliation_status = "unknown"
        external_cash = accounting.get("cash", {}).get("external_cash_flow_micros")
        reconciliation = {
            "status": reconciliation_status,
            "external_cash_flow": (
                None if external_cash is None
                else f"{external_cash} {quote_currency} micros"
            ),
            "unresolved_count": len(accounting.get("unpriced_holdings", []))
            + len(accounting.get("missing_holdings", [])),
            "reasons": copy.deepcopy(conservation.get("blockers", [])),
            "external_cash_flow_micros": external_cash,
            "external_cash_flow_currency": quote_currency,
            "strategy_profit_treatment": "excluded",
        }
    else:
        reconciliation = {
            "status": "unknown",
            "external_cash_flow": None,
            "unresolved_count": sum(
                row["classification"] in {"pending", "unresolved"} for row in receipt_rows
            ),
            "reasons": ["No complete quote-currency accounting snapshot was supplied"],
            "external_cash_flow_micros": None,
            "external_cash_flow_currency": quote_currency,
            "strategy_profit_treatment": "excluded",
        }

    input_sha256 = content_hash(report_input)
    presentation_comparisons = []
    first_strategy = True
    for arm in arms:
        metrics = arm["metrics"]
        provider_allocation = allocations.get(arm["id"])
        if arm["kind"] == "cash":
            presentation_kind = "cash"
        elif arm["kind"] == "holding":
            presentation_kind = "holding"
        elif first_strategy:
            presentation_kind = "current"
            first_strategy = False
        else:
            presentation_kind = "arm"
        presentation_comparisons.append({
            "id": "comparison-" + content_hash([campaign_id, arm["id"], input_sha256])[:24],
            "kind": presentation_kind,
            "label": arm["label"],
            "arm_id": None if presentation_kind in {"cash", "holding"} else arm["id"],
            "comparable": comparable and metrics is not None,
            "comparability_reason": (
                None if comparable and metrics is not None
                else "; ".join(comparison_blockers or arm["readiness"]["blockers"])
            ),
            "starting_capital_usd": arm["declared_capital_micros"] / MONEY_MICROS_PER_QUOTE,
            "ending_capital_usd": (
                None if metrics is None
                else metrics["conservative_ending_equity_micros"] / MONEY_MICROS_PER_QUOTE
            ),
            "modeled_net_pnl_usd": (
                None if metrics is None
                else metrics["net_modeled_pnl_micros"] / MONEY_MICROS_PER_QUOTE
            ),
            "modeled_realized_pnl_usd": (
                None if metrics is None
                else metrics["realized_pnl_micros"] / MONEY_MICROS_PER_QUOTE
            ),
            "modeled_unrealized_pnl_usd": (
                None if metrics is None
                else metrics["known_unrealized_pnl_micros"] / MONEY_MICROS_PER_QUOTE
            ),
            "maximum_drawdown_fraction": (
                None if metrics is None else metrics["maximum_drawdown_ppm"] / PPM
            ),
            "exposure_fraction": (
                None if metrics is None else metrics["time_weighted_exposure_ppm"] / PPM
            ),
            "turnover_usd": (
                None if metrics is None else metrics["turnover_micros"] / MONEY_MICROS_PER_QUOTE
            ),
            "opportunities": 0 if metrics is None else metrics["opportunities"],
            "independent_tokens": (
                0 if metrics is None else metrics["independent_token_count"]
            ),
            "coverage_fraction": (
                None if metrics is None or metrics["opportunities"] == 0
                else metrics["resolved_opportunities"] / metrics["opportunities"]
            ),
            "operating_seconds": arm["active_seconds"],
            "unpriced_positions": 0 if metrics is None else metrics["unpriced_positions"],
            "censored_positions": 0 if metrics is None else metrics["censored_positions"],
            "known_costs_usd": (
                0 if metrics is None
                else (metrics["modeled_cost_micros"] + (provider_allocation or 0))
                / MONEY_MICROS_PER_QUOTE
            ),
            "estimated_costs_usd": (
                None if provider_allocation is None
                else provider_allocation / MONEY_MICROS_PER_QUOTE
            ),
            "cost_status": (
                "known" if provider_costs["fully_costed"]
                else "estimated" if provider_allocation is not None else "unknown"
            ),
            "curve": arm["curve"],
        })

    readiness_reasons = sorted({
        *comparison_blockers,
        *(reason for arm in arms if arm["kind"] not in {"cash", "holding"}
          for reason in arm["readiness"]["blockers"]),
    })
    if comparison_blockers:
        readiness_state = "not_ready"
    elif readiness_reasons:
        readiness_state = "exploratory"
    else:
        readiness_state = "ready"
    action = recommendation["action"]
    ranking_label = {
        "prefer_cash": "Cash remains the matched benchmark leader",
        "withhold_ranking": "Ranking withheld",
        "modeled_leader_only": "Modeled leader; not execution evidence",
    }[action]
    ranking_reason = "; ".join(recommendation["reasons"])
    ranking = {
        "comparison_id": next(
            (
                row["id"] for arm, row in zip(arms, presentation_comparisons, strict=True)
                if arm["id"] == recommendation["arm_id"]
            ),
            None,
        ),
        "label": ranking_label,
        "reason": ranking_reason,
        "fully_costed": provider_costs["fully_costed"],
    }

    presentation_gaps = []
    seen_gaps = set()
    for arm in arms:
        for gap in arm["gaps"]:
            key = (gap["id"], gap["started_at"], gap["ended_at"])
            if key in seen_gaps:
                continue
            seen_gaps.add(key)
            presentation_gaps.append({
                **gap,
                "detail": f"Attributed to {arm['label']}; excluded from matched uptime",
            })
    day_map: dict[str, set[str]] = {}
    for arm in arms:
        for day in arm["daily"]:
            day_map.setdefault(day["day_utc"], set()).add(arm["id"])
    presentation_daily = []
    for day_name in sorted(day_map):
        day_start = datetime.fromisoformat(day_name).replace(tzinfo=timezone.utc).timestamp()
        day_end = day_start + 86_400
        presentation_daily.append({
            "id": f"day:{day_name}",
            "label": day_name,
            "first_at": max(window_start, day_start),
            "last_at": min(window_end, day_end),
            "comparison_ids": sorted(day_map[day_name]),
            "gaps": sum(
                gap["started_at"] < day_end and gap["ended_at"] > day_start
                for gap in presentation_gaps
            ),
        })

    engine_versions = sorted({
        arm["engine_version"] for arm in arms if arm["engine_version"] is not None
    } | {ENGINE_VERSION})
    limitations = [
        "Paper and retrospective fills are modeled; recorded receipts remain a separate class.",
        "The adapter does not query arbitrary external wallet history.",
        "Unknown costs, marks, FX, transferred basis, or unmatched uptime are never treated as zero.",
    ]

    core = {
        "schema": REPORT_SCHEMA,
        "campaign_id": campaign_id,
        "manifest_digest": manifest_sha256,
        "previous_report_id": correction_of,
        "created_at": created_at,
        "as_of": as_of,
        "actual_period": {
            "first_at": window_start,
            "last_at": window_end,
            "operating_seconds": _duration(common_intervals),
        },
        "model_label": "prospective paper replay with separate recorded-execution evidence",
        "engine_versions": engine_versions,
        "readiness": {"state": readiness_state, "reasons": readiness_reasons},
        "ranking": ranking,
        "comparisons": presentation_comparisons,
        "daily": presentation_daily,
        "gaps": presentation_gaps,
        "limitations": limitations,
        "receipts": receipt_rows,
        "reconciliation": reconciliation,
        "engine_version": ENGINE_VERSION,
        "replay_engine_version": REPLAY_ENGINE_VERSION,
        "input_sha256": input_sha256,
        "campaign": {
            "id": campaign_id,
            "manifest_sha256": manifest_sha256,
            "quote_currency": quote_currency,
        },
        "snapshot": {
            "as_of": as_of,
            "window_start": window_start,
            "window_end": window_end,
            "revision": revision,
            "correction_of_report_id": correction_of,
            "append_only_required": True,
        },
        "lineage": {
            **lineage,
            "experiment_id": experiment_id,
            "selection_hypothesis_sha256": hypothesis,
            "prior_report_ids": prior_reports,
            "viewing_creates_new_trial": False,
        },
        "units": {
            "cash": "quote-micros (1e-6 quote currency)",
            "price": "integer 1e-18 quote currency per whole token",
            "token": "integer atoms at declared decimals",
            "implicit_fx_conversion": False,
        },
        "arms": arms,
        "matched_comparison": {
            "status": "comparable" if comparable else "withheld",
            "blockers": comparison_blockers,
            "common_operating_intervals": common_intervals,
            "common_operating_seconds": _duration(common_intervals),
            "ranked": ranked,
            "recommendation": recommendation,
            "cash_and_holding_use_same_evidence": len(evidence_digests) == 1,
        },
        "provider_costs": provider_costs,
        "profitability": profitability,
        "receipt_lineage": receipt_lineage,
        "metric_provenance": {
            "pnl_drawdown_exposure": "immutable portfolio replay report metrics",
            "turnover": "sum of absolute replay fill cash deltas",
            "opportunity_coverage": "replay opportunity ledger",
            "uptime_and_gaps": "campaign operating intervals and durable gaps",
            "provider_cost": "campaign provider telemetry/cost declaration",
            "receipt_attribution": "separate read-only receipt evidence report",
        },
        "authority": {
            "wallet_allocation": False,
            "signer": False,
            "orders": False,
            "submission": False,
        },
    }
    ident = content_hash(core)
    return {**core, "id": ident, "reproducibility_sha256": ident}


def _closed_interval_rows(
    rows: object, *, as_of: float, field: str,
) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise ForwardPerformanceError(f"{field} must be a list")
    result = []
    for index, raw in enumerate(rows):
        row = _canonical(raw, f"{field}[{index}]")
        started = _number(row.get("started_at"), f"{field}[{index}].started_at")
        ended_raw = row.get("ended_at")
        ended = as_of if ended_raw is None else min(
            as_of, _number(ended_raw, f"{field}[{index}].ended_at")
        )
        if started >= as_of or ended <= started:
            continue
        result.append({
            "id": _text(row.get("id") or row.get("interval_id") or row.get("gap_id"),
                        f"{field}[{index}].id"),
            "started_at": started,
            "ended_at": ended,
            "reason": _text(row.get("reason"), f"{field}[{index}].reason"),
        })
    ordered = sorted(result, key=lambda row: (row["started_at"], row["ended_at"], row["id"]))
    merged = []
    for row in ordered:
        if not merged or row["started_at"] > merged[-1]["ended_at"]:
            merged.append(row)
            continue
        previous = merged[-1]
        sources = sorted(set(previous.get("source_ids", [previous["id"]])) | {row["id"]})
        reasons = sorted(set(previous["reason"].split(" + ")) | {row["reason"]})
        previous.update(
            id="merged:" + content_hash(sources)[:24],
            ended_at=max(previous["ended_at"], row["ended_at"]),
            reason=" + ".join(reasons),
            source_ids=sources,
        )
    return merged


def _episode_returns(report: dict[str, Any]) -> list[int]:
    by_episode: dict[str, int] = {}
    closed = set()
    open_positions = {
        position["id"] for position in report.get("open_positions", {}).values()
    }
    for fill in report.get("fills", []):
        if fill.get("side") != "exit" or not isinstance(fill.get("episode_id"), str):
            continue
        episode = fill["episode_id"]
        by_episode[episode] = by_episode.get(episode, 0) + _integer(
            fill.get("realized_pnl_micros"), "fill.realized_pnl_micros"
        )
        if fill.get("position_id") not in open_positions:
            closed.add(episode)
    return [by_episode[episode] for episode in sorted(closed)]


def performance_input_from_campaign(
    campaign: dict[str, Any],
    arm_histories: dict[str, dict[str, Any]],
    *,
    as_of: int | float,
    prior_report_ids: Iterable[str] = (),
    correction_of_report_id: str | None = None,
    receipt_report: dict[str, Any] | None = None,
    accounting_reconciliation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Adapt a real campaign view and paper histories to the pure v1 input.

    ``arm_histories`` is keyed by paper-run ID and is normally supplied by
    ``PaperResearchStore.history``.  The function performs no reads itself.
    """
    value = _canonical(campaign, "campaign")
    if value.get("schema") != "pumpwizard.forward-campaign/v1":
        raise ForwardPerformanceError("campaign must be a forward-campaign/v1 view")
    as_of_value = _number(as_of, "as_of")
    started = _number(value.get("start_at"), "campaign.start_at")
    if as_of_value <= started:
        raise ForwardPerformanceError("Campaign reporting needs evidence time after Start")
    manifest = _digest(
        value.get("request_sha256") or value.get("manifest_digest"),
        "campaign manifest digest",
    )
    campaign_intervals = _closed_interval_rows(
        value.get("operating_intervals", []),
        as_of=as_of_value,
        field="campaign.operating_intervals",
    )
    arm_inputs = []
    eligible_by_run: dict[str, list[dict[str, Any]]] = {}
    reports = []
    for index, raw_arm in enumerate(value.get("arms", [])):
        arm = _canonical(raw_arm, f"campaign.arms[{index}]")
        run_id = _text(arm.get("paper_run_id"), f"campaign.arms[{index}].paper_run_id")
        history_raw = arm_histories.get(run_id)
        if history_raw is None:
            raise ForwardPerformanceError(f"Paper history is missing for campaign arm {run_id}")
        history = _canonical(history_raw, f"arm history {run_id}")
        run = _canonical(history.get("run"), f"arm history {run_id}.run")
        paper_report = _canonical(
            run.get("evaluator_report"), f"arm history {run_id}.evaluator_report"
        )
        replay = _replay_report(paper_report, f"arm history {run_id}.evaluator_report")
        reports.append(replay)
        observations = history.get("observations", [])
        if not isinstance(observations, list):
            raise ForwardPerformanceError(f"arm history {run_id}.observations must be a list")
        eligible = [
            _canonical(row, f"arm history {run_id} observation")
            for row in observations
            if isinstance(row, dict)
            and isinstance(row.get("quality"), dict)
            and row["quality"].get("decision_eligible") is True
            and float(row.get("first_received_at", -1)) <= as_of_value
        ]
        eligible.sort(key=lambda row: (row["first_received_at"], row["evidence_id"]))
        eligible_by_run[run_id] = eligible
        intervals = _closed_interval_rows(
            history.get("operating_intervals", []),
            as_of=as_of_value,
            field=f"arm history {run_id}.operating_intervals",
        )
        gaps = _closed_interval_rows(
            history.get("gaps", []),
            as_of=as_of_value,
            field=f"arm history {run_id}.gaps",
        )
        arm_inputs.append({
            "id": _text(arm.get("id"), f"campaign.arms[{index}].id"),
            "kind": "observed_paper",
            "label": str(arm.get("label") or arm.get("id")),
            "run_id": run_id,
            "strategy_sha256": _digest(
                arm.get("strategy_sha256"), f"campaign.arms[{index}].strategy_sha256"
            ),
            "declared_capital_micros": quote_amount_micros(
                arm.get("simulated_capital_usd"),
                f"campaign.arms[{index}].simulated_capital_usd",
                minimum=1,
            ),
            "operating_intervals": intervals,
            "gaps": gaps,
            "matched_evidence_ids": [row["evidence_id"] for row in eligible],
            "independent_episode_returns_micros": _episode_returns(replay),
            "report": paper_report,
        })
    if not arm_inputs:
        raise ForwardPerformanceError("Campaign performance needs at least one paper arm")
    common_ids = set(arm_inputs[0]["matched_evidence_ids"])
    for arm in arm_inputs[1:]:
        common_ids.intersection_update(arm["matched_evidence_ids"])
    common_rows = [
        row for row in eligible_by_run[arm_inputs[0]["run_id"]]
        if row["evidence_id"] in common_ids
    ]
    try:
        from .paper_research import _portfolio_quote_event

        quote_events = [_portfolio_quote_event(row) for row in common_rows]
        holding_definition = build_holding_benchmark(quote_events)
        holding_report = replay_portfolio(
            holding_definition["events"],
            policy=reports[0]["policy"],
            costs=reports[0]["costs_assumption"],
            execution=reports[0]["execution_assumption"],
        )
        holding_unavailable = None
        if not holding_definition["eligible_mints"]:
            holding_unavailable = "No mint has two consistent attributed quotes"
    except (KeyError, TypeError, ValueError) as exc:
        holding_report = None
        holding_unavailable = f"Holding benchmark could not be built: {exc}"
    benchmark_capital = arm_inputs[0]["declared_capital_micros"]
    common_evidence = [row["evidence_id"] for row in common_rows]
    cash = {
        "id": "cash",
        "kind": "cash",
        "label": "Cash",
        "declared_capital_micros": benchmark_capital,
        "operating_intervals": campaign_intervals,
        "gaps": [],
        "matched_evidence_ids": common_evidence,
        "independent_episode_returns_micros": [],
    }
    holding = {
        "id": "holding",
        "kind": "holding",
        "label": "Declared holding benchmark",
        "declared_capital_micros": benchmark_capital,
        "operating_intervals": campaign_intervals,
        "gaps": [],
        "matched_evidence_ids": common_evidence,
        "independent_episode_returns_micros": (
            _episode_returns(holding_report) if holding_report is not None else []
        ),
    }
    if holding_unavailable is not None:
        holding["unavailable_reason"] = holding_unavailable
    else:
        holding["report"] = holding_report
    coverage = _canonical(value.get("coverage"), "campaign.coverage")
    provider = _canonical(value.get("provider_cost"), "campaign.provider_cost")
    cost_status = provider.get("cost_status")
    known = cost_status == "known"
    currency = _text(provider.get("currency"), "campaign.provider_cost.currency")
    observed_cost = provider.get("known_marginal_cost")
    fixed_cost = provider.get("known_fixed_cost")
    provider_costs = {
        "status": "known" if known else "unknown",
        "currency": currency,
        "observed_charges_micros": (
            quote_amount_micros(observed_cost, "known_marginal_cost")
            if observed_cost is not None else None
        ),
        "configured_estimates_micros": 0 if known else None,
        "shared_fixed_cost_micros": (
            quote_amount_micros(fixed_cost, "known_fixed_cost")
            if fixed_cost is not None else None
        ),
        "unknown_components": [] if known else ["provider price is unknown or partial"],
        "allocation_by_arm_micros": {},
    }
    priors = [_text(item, "prior_report_id") for item in prior_report_ids]
    revision = 2 if correction_of_report_id is not None else 1
    result = {
        "schema": INPUT_SCHEMA,
        "campaign": {
            "id": _text(value.get("id"), "campaign.id"),
            "manifest_sha256": manifest,
            "quote_currency": currency,
        },
        "snapshot": {
            "as_of": as_of_value,
            "created_at": as_of_value,
            "window_start": started,
            "window_end": as_of_value,
            "revision": revision,
            "correction_of_report_id": correction_of_report_id,
        },
        "lineage": {
            "source_snapshot_sha256": content_hash({
                run_id: [row["evidence_id"] for row in rows]
                for run_id, rows in sorted(eligible_by_run.items())
            }),
            "selection_hypothesis_sha256": manifest,
            "experiment_id": _text(value.get("id"), "campaign.id"),
            "prior_report_ids": priors,
            "holdout_status": "not_applicable",
        },
        "readiness_policy": {
            "schema": READINESS_POLICY_SCHEMA,
            "registered_at": started,
            "minimum_independent_episodes": _integer(
                coverage.get("minimum_independent_tokens"),
                "campaign.coverage.minimum_independent_tokens",
                minimum=1,
            ),
            "minimum_independent_tokens": coverage["minimum_independent_tokens"],
            "minimum_observed_seconds": _whole_number(
                value.get("duration_seconds"), "campaign.duration_seconds", minimum=1
            ),
            "minimum_coverage_ppm": PPM,
            "minimum_stability_groups": coverage["minimum_independent_tokens"],
            "maximum_gap_seconds": _whole_number(
                coverage.get("maximum_gap_seconds"),
                "campaign.coverage.maximum_gap_seconds",
                minimum=0,
            ),
            "uncertainty_minimum_independent_episodes": 30,
            "required_engine_version": REPLAY_ENGINE_VERSION,
        },
        "arms": [*arm_inputs, cash, holding],
        "provider_costs": provider_costs,
    }
    if receipt_report is not None:
        result["receipt_lineage"] = receipt_report
    if accounting_reconciliation is not None:
        result["accounting_reconciliation"] = accounting_reconciliation
    return result


def _asset_key(mint: object, decimals: object, field: str) -> tuple[str, int]:
    return (
        _text(mint, field + ".mint"),
        _integer(decimals, field + ".decimals", minimum=0, maximum=18),
    )


def reconcile_accounting_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    """Check cash/token/basis conservation while excluding external flows."""
    source = _canonical(value, "accounting input")
    if source.get("schema") != ACCOUNTING_INPUT_SCHEMA:
        raise ForwardPerformanceError(
            f"accounting input schema must be {ACCOUNTING_INPUT_SCHEMA}"
        )
    account_id = _text(source.get("account_id"), "account_id")
    as_of = _number(source.get("as_of"), "as_of")
    quote_currency = _text(source.get("quote_currency"), "quote_currency")
    starting_cash = _integer(
        source.get("starting_cash_micros"), "starting_cash_micros",
        minimum=0, maximum=MAX_LEDGER_MICROS,
    )
    ending_cash = _integer(
        source.get("ending_cash_micros"), "ending_cash_micros",
        minimum=0, maximum=MAX_LEDGER_MICROS,
    )
    fills = source.get("fills")
    external = source.get("external_flows")
    holdings = source.get("ending_holdings")
    if not all(isinstance(rows, list) for rows in (fills, external, holdings)):
        raise ForwardPerformanceError("Accounting fills, external flows and holdings must be lists")
    if len(fills) + len(external) + len(holdings) > MAX_DRILLDOWN_ROWS:
        raise ForwardPerformanceError("Accounting reconciliation exceeds its row bound")

    cash_delta = 0
    realized = 0
    strategy_cost = 0
    tokens: dict[tuple[str, int], int] = {}
    basis: dict[tuple[str, int], int] = {}
    missing_basis_assets: set[tuple[str, int]] = set()
    fill_rows = []
    position_quantity: dict[tuple[str, int], int] = {}
    partial_exits = 0
    final_exits = 0
    seen = set()
    for index, raw in enumerate(fills):
        row = _canonical(raw, f"fills[{index}]")
        ident = _text(row.get("id"), f"fills[{index}].id")
        if ident in seen:
            raise ForwardPerformanceError("Accounting event IDs must be unique")
        seen.add(ident)
        side = row.get("side")
        if side not in {"entry", "exit", "failed_fee"}:
            raise ForwardPerformanceError("Accounting fill side is unsupported")
        event_cash = _integer(row.get("cash_delta_micros"), f"fills[{index}].cash_delta_micros")
        cost = _integer(row.get("cost_micros", 0), f"fills[{index}].cost_micros", minimum=0)
        if abs(event_cash) > MAX_LEDGER_MICROS or cost > MAX_LEDGER_MICROS:
            raise ForwardPerformanceError("Accounting fill exceeds the supported cash ledger bound")
        cash_delta += event_cash
        strategy_cost += cost
        key = None
        quantity_delta = 0
        basis_delta = 0
        fill_realized = 0
        if side == "failed_fee":
            if event_cash != -cost or row.get("quantity_delta_atoms", 0) != 0:
                raise ForwardPerformanceError("Failed fill must be a fee-only cash debit")
            realized += event_cash
            fill_realized = event_cash
        else:
            key = _asset_key(row.get("mint"), row.get("decimals"), f"fills[{index}]")
            quantity_delta = _integer(
                row.get("quantity_delta_atoms"), f"fills[{index}].quantity_delta_atoms"
            )
            basis_delta = _integer(
                row.get("basis_delta_micros"), f"fills[{index}].basis_delta_micros"
            )
            if abs(quantity_delta) > MAX_TOKEN_ATOMS or abs(basis_delta) > MAX_LEDGER_MICROS:
                raise ForwardPerformanceError("Accounting fill exceeds token or basis bounds")
            if side == "entry" and not (quantity_delta > 0 and basis_delta > 0 and event_cash < 0):
                raise ForwardPerformanceError("Entry accounting signs are inconsistent")
            if side == "exit" and not (quantity_delta < 0 and basis_delta < 0 and event_cash >= 0):
                raise ForwardPerformanceError("Exit accounting signs are inconsistent")
            previous = position_quantity.get(key, 0)
            following = previous + quantity_delta
            if following < 0:
                raise ForwardPerformanceError("An exit exceeds accounted token inventory")
            if side == "exit":
                fill_realized = event_cash + basis_delta
                realized += fill_realized
                if following == 0:
                    final_exits += 1
                else:
                    partial_exits += 1
            position_quantity[key] = following
            tokens[key] = tokens.get(key, 0) + quantity_delta
            basis[key] = basis.get(key, 0) + basis_delta
            if basis[key] < 0:
                raise ForwardPerformanceError("Allocated basis exceeds accounted position basis")
        fill_rows.append({
            **row,
            "cash_delta_micros": event_cash,
            "quantity_delta_atoms": quantity_delta,
            "basis_delta_micros": basis_delta,
            "realized_pnl_micros": fill_realized,
        })

    external_cash = 0
    external_basis = 0
    external_basis_complete = True
    external_rows = []
    for index, raw in enumerate(external):
        row = _canonical(raw, f"external_flows[{index}]")
        ident = _text(row.get("id"), f"external_flows[{index}].id")
        if ident in seen:
            raise ForwardPerformanceError("Accounting event IDs must be unique")
        seen.add(ident)
        kind = row.get("kind")
        if kind not in {"deposit", "withdrawal", "transfer_in", "transfer_out"}:
            raise ForwardPerformanceError("External accounting flow kind is unsupported")
        if kind in {"deposit", "withdrawal"}:
            delta = _integer(row.get("cash_delta_micros"), f"external_flows[{index}].cash_delta_micros")
            if abs(delta) > MAX_LEDGER_MICROS:
                raise ForwardPerformanceError("External cash flow exceeds the ledger bound")
            if (kind == "deposit" and delta <= 0) or (kind == "withdrawal" and delta >= 0):
                raise ForwardPerformanceError("External cash flow signs are inconsistent")
            cash_delta += delta
            external_cash += delta
            normalized = {**row, "cash_delta_micros": delta}
        else:
            key = _asset_key(row.get("mint"), row.get("decimals"), f"external_flows[{index}]")
            quantity = _integer(
                row.get("quantity_delta_atoms"),
                f"external_flows[{index}].quantity_delta_atoms",
            )
            if abs(quantity) > MAX_TOKEN_ATOMS:
                raise ForwardPerformanceError("External token flow exceeds the atom bound")
            if (kind == "transfer_in" and quantity <= 0) or (kind == "transfer_out" and quantity >= 0):
                raise ForwardPerformanceError("External token flow signs are inconsistent")
            tokens[key] = tokens.get(key, 0) + quantity
            position_quantity[key] = position_quantity.get(key, 0) + quantity
            if tokens[key] < 0:
                raise ForwardPerformanceError("External transfer exceeds accounted token inventory")
            flow_basis = row.get("basis_delta_micros")
            if flow_basis is None:
                missing_basis_assets.add(key)
                external_basis_complete = False
            else:
                flow_basis = _integer(
                    flow_basis, f"external_flows[{index}].basis_delta_micros"
                )
                if abs(flow_basis) > MAX_LEDGER_MICROS:
                    raise ForwardPerformanceError("External token basis exceeds the ledger bound")
                if (kind == "transfer_in" and flow_basis < 0) or (
                    kind == "transfer_out" and flow_basis > 0
                ):
                    raise ForwardPerformanceError("External token basis signs are inconsistent")
                basis[key] = basis.get(key, 0) + flow_basis
                external_basis += flow_basis
                if basis[key] < 0:
                    raise ForwardPerformanceError("External transfer removes excess basis")
            normalized = {
                **row,
                "quantity_delta_atoms": quantity,
                "basis_delta_micros": flow_basis,
            }
        external_rows.append(normalized)

    expected_cash = starting_cash + cash_delta
    cash_conserved = expected_cash == ending_cash
    holding_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    known_holding_value = 0
    unpriced = []
    holding_rows = []
    for index, raw in enumerate(holdings):
        row = _canonical(raw, f"ending_holdings[{index}]")
        key = _asset_key(row.get("mint"), row.get("decimals"), f"ending_holdings[{index}]")
        if key in holding_by_key:
            raise ForwardPerformanceError("Ending holdings must be unique by mint and decimals")
        quantity = _integer(
            row.get("quantity_atoms"), f"ending_holdings[{index}].quantity_atoms",
            minimum=0, maximum=MAX_TOKEN_ATOMS,
        )
        reported_basis = row.get("cost_basis_micros")
        if reported_basis is not None:
            reported_basis = _integer(
                reported_basis, f"ending_holdings[{index}].cost_basis_micros",
                minimum=0, maximum=MAX_LEDGER_MICROS,
            )
        if key in missing_basis_assets:
            reported_basis = None
        expected_quantity = tokens.get(key, 0)
        quantity_matches = expected_quantity == quantity
        basis_matches = reported_basis is not None and reported_basis == basis.get(key, 0)
        mark = row.get("mark")
        value_micros = None
        valuation_reason = "mark_missing"
        if mark is not None:
            mark = _canonical(mark, f"ending_holdings[{index}].mark")
            if mark.get("mint") != key[0] or mark.get("decimals") != key[1]:
                raise ForwardPerformanceError("Holding mark asset identity differs from the holding")
            if mark.get("quote_currency") != quote_currency:
                valuation_reason = "missing_exact_fx_or_quote_currency"
            elif mark.get("status") != "priced":
                valuation_reason = _text(
                    mark.get("reason", "mark_not_priced"),
                    f"ending_holdings[{index}].mark.reason",
                )
            else:
                price = _integer(
                    mark.get("price_quote_units_per_token"),
                    f"ending_holdings[{index}].mark.price_quote_units_per_token",
                    minimum=1,
                    maximum=MAX_PRICE_UNITS_PER_TOKEN,
                )
                mark_as_of = _number(mark.get("as_of"), f"ending_holdings[{index}].mark.as_of")
                if mark_as_of > as_of:
                    raise ForwardPerformanceError("A holding mark cannot be from the future")
                numerator = quantity * price * MONEY_MICROS_PER_QUOTE
                denominator = (10 ** key[1]) * PRICE_UNITS_PER_QUOTE
                value_micros = numerator // denominator
                if quantity > 0 and value_micros == 0:
                    value_micros = None
                    valuation_reason = "value_below_quote-micro_precision"
                else:
                    valuation_reason = "priced"
        if value_micros is None and quantity > 0:
            unpriced.append({"mint": key[0], "reason": valuation_reason})
        else:
            known_holding_value += value_micros or 0
        normalized = {
            "mint": key[0],
            "decimals": key[1],
            "quantity_atoms": quantity,
            "expected_quantity_atoms": expected_quantity,
            "quantity_conserved": quantity_matches,
            "cost_basis_micros": reported_basis,
            "expected_cost_basis_micros": None if key in missing_basis_assets else basis.get(key, 0),
            "basis_conserved": basis_matches,
            "value_micros": value_micros,
            "valuation_reason": valuation_reason,
        }
        holding_by_key[key] = normalized
        holding_rows.append(normalized)
    missing_holding_rows = [
        {"mint": key[0], "decimals": key[1], "expected_quantity_atoms": quantity}
        for key, quantity in tokens.items()
        if quantity != 0 and key not in holding_by_key
    ]
    token_conserved = not missing_holding_rows and all(
        row["quantity_conserved"] for row in holding_rows
    )
    basis_conserved = external_basis_complete and all(
        row["basis_conserved"] for row in holding_rows if row["quantity_atoms"] > 0
    )
    complete_valuation = not unpriced and not missing_holding_rows
    net_result = None
    if complete_valuation and external_basis_complete:
        net_result = (
            ending_cash + known_holding_value - starting_cash - external_cash - external_basis
        )
    blockers = []
    if not cash_conserved:
        blockers.append("cash_does_not_conserve")
    if not token_conserved:
        blockers.append("token_atoms_do_not_conserve")
    if not basis_conserved:
        blockers.append("cost_basis_missing_or_inconsistent")
    if not complete_valuation:
        blockers.append("ending_valuation_incomplete")
    core = {
        "schema": ACCOUNTING_REPORT_SCHEMA,
        "engine_version": ENGINE_VERSION,
        "account_id": account_id,
        "as_of": as_of,
        "quote_currency": quote_currency,
        "units": {
            "cash": "quote-micros",
            "price": "integer 1e-18 quote currency per whole token",
            "tokens": "integer atoms at declared decimals",
            "implicit_fx_conversion": False,
        },
        "cash": {
            "starting_cash_micros": starting_cash,
            "strategy_cash_delta_micros": cash_delta - external_cash,
            "external_cash_flow_micros": external_cash,
            "expected_ending_cash_micros": expected_cash,
            "reported_ending_cash_micros": ending_cash,
            "conserved": cash_conserved,
        },
        "performance": {
            "realized_pnl_micros": realized,
            "known_ending_holding_value_micros": known_holding_value,
            "external_token_basis_flow_micros": (
                external_basis if external_basis_complete else None
            ),
            "strategy_net_result_micros": net_result,
            "external_flows_count_as_strategy_return": False,
            "strategy_cost_micros": strategy_cost,
            "partial_exits": partial_exits,
            "final_exits": final_exits,
        },
        "holdings": holding_rows,
        "missing_holdings": missing_holding_rows,
        "unpriced_holdings": unpriced,
        "fills": fill_rows,
        "external_flows": external_rows,
        "conservation": {
            "cash": cash_conserved,
            "token_atoms": token_conserved,
            "cost_basis": basis_conserved,
            "complete": cash_conserved and token_conserved and basis_conserved,
            "blockers": blockers,
        },
        "evidence_semantics": {
            "ledger_is_execution_authority": False,
            "paper_fill_is_on_chain_receipt": False,
            "unknown_values_are_zero": False,
        },
    }
    ident = content_hash(core)
    return {**core, "id": ident, "reproducibility_sha256": ident}
