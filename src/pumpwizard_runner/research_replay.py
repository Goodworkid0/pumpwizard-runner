"""Pure adapter from registered Batch 3 research evidence to portfolio replay.

The job worker passes this module immutable JSON plus progress/cancellation
callbacks.  It deliberately has no database, service, wallet, signer, RPC or
order dependency.  Exact strategy packages generate point-in-time entry
signals; the finite-capital engine owns all modeled fills and exits.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
import math
from typing import Any, Callable, Iterable

from .decision import DecisionEngine, MarketQuote
from .portfolio_replay import (
    COMPARISON_SCHEMA,
    COST_SCHEMA,
    ENGINE_VERSION,
    EVIDENCE_POLICY_SCHEMA,
    EXECUTION_SCHEMA,
    POLICY_SCHEMA,
    PPM,
    PRICE_UNITS_PER_QUOTE,
    PortfolioReplayError,
    build_holding_benchmark,
    compare_portfolios,
    default_evidence_policy,
    quote_amount_micros,
    quote_price_fields,
)
from .session_review import generate_candidates
from .strategies import canonical_json, content_hash, parse_package


EXPORT_SCHEMA = "pumpwizard.research-export/v1"
DATASET_SCHEMA = "pumpwizard.research-dataset/v1"
EXPERIMENT_SCHEMA = "pumpwizard.research-experiment/v1"
REPORT_SCHEMA = "pumpwizard.research-replay-report/v1"
ADAPTER_VERSION = "pumpwizard-research-replay-adapter/2"
MAX_CANDIDATES = 6

_HARD_EXCLUSION_FLAGS = frozenset({
    "unknown-first-receipt",
    "invalid-time-order",
    "missing-price",
    "missing-unit-metadata",
    "ambiguous-pool",
    "route-pool-mismatch",
    "duplicate-source-evidence",
    "dead-or-illiquid",
})


class ResearchReplayError(PortfolioReplayError):
    """The registered research inputs cannot be replayed without guessing."""


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResearchReplayError(f"{label} must be an object")
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ResearchReplayError(f"{label} must be canonical JSON") from exc


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchReplayError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ResearchReplayError(f"{label} must be a finite number")
    return result


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchReplayError(f"{label} must be non-empty text")
    return value.strip()


def _usd_micros(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ResearchReplayError(f"{label} must be a decimal USD amount")
    try:
        decimal = Decimal(str(value))
    except InvalidOperation as exc:
        raise ResearchReplayError(f"{label} must be a decimal USD amount") from exc
    if not decimal.is_finite():
        raise ResearchReplayError(f"{label} must be finite")
    result = int((decimal * 1_000_000).to_integral_value(rounding=ROUND_HALF_EVEN))
    if result < minimum:
        raise ResearchReplayError(f"{label} is below the supported micro-USD precision")
    return result


def _fraction_ppm(value: object, label: str, *, allow_negative: bool = False) -> int:
    amount = _number(value, label)
    if allow_negative:
        valid = -1 < amount < 0
    else:
        valid = 0 <= amount <= 1
    if not valid:
        raise ResearchReplayError(f"{label} must be between {-1 if allow_negative else 0} and 1")
    result = round(amount * PPM)
    if allow_negative and result >= 0:
        raise ResearchReplayError(f"{label} must remain negative at ppm precision")
    return result


def _validate_inputs(dataset: object, experiment: object) -> tuple[dict[str, Any], dict[str, Any]]:
    export = _object(dataset, "dataset export")
    registered = _object(experiment, "experiment")
    if export.get("schema") != EXPORT_SCHEMA:
        raise ResearchReplayError(f"dataset export schema must be {EXPORT_SCHEMA}")
    manifest = export.get("dataset")
    records = export.get("records")
    if (not isinstance(manifest, dict) or manifest.get("schema") != DATASET_SCHEMA
            or not isinstance(records, dict) or not isinstance(records.get("observations"), list)):
        raise ResearchReplayError("Research dataset export is incomplete")
    core_manifest = {
        key: value for key, value in manifest.items() if key not in {"id", "content_sha256"}
    }
    actual_digest = content_hash({"dataset": core_manifest, "records": records})
    if (manifest.get("content_sha256") != actual_digest
            or manifest.get("id") != "research-" + actual_digest[:32]):
        raise ResearchReplayError("Research dataset content identity changed")
    if registered.get("schema") != EXPERIMENT_SCHEMA:
        raise ResearchReplayError(f"experiment schema must be {EXPERIMENT_SCHEMA}")
    if registered.get("dataset_id") != manifest["id"]:
        raise ResearchReplayError("Experiment references a different dataset")
    lineage = registered.get("lineage")
    source = manifest.get("source")
    if not isinstance(lineage, dict) or not isinstance(source, dict):
        raise ResearchReplayError("Experiment or dataset lineage is incomplete")
    if (lineage.get("dataset_content_sha256") != actual_digest
            or lineage.get("source_session_id") != source.get("session_id")):
        raise ResearchReplayError("Experiment lineage does not match the dataset")
    splits = registered.get("splits")
    if not isinstance(splits, dict):
        raise ResearchReplayError("Experiment split contract is missing")
    train = _number(splits.get("train_end_at"), "train_end_at")
    validation = _number(splits.get("validation_end_at"), "validation_end_at")
    holdout = _number(splits.get("holdout_end_at"), "holdout_end_at")
    embargo = _number(registered.get("embargo_seconds"), "embargo_seconds")
    if not (train < validation < holdout and 0 <= embargo
            and train + embargo < validation and validation + embargo < holdout):
        raise ResearchReplayError("Experiment split boundaries or embargo are invalid")
    expected_holdout = sorted(
        str(row["evidence_id"])
        for row in records["observations"]
        if (isinstance(row, dict) and isinstance(row.get("times"), dict)
            and validation < _number(row["times"].get("event_at"), "evidence event_at")
            <= holdout)
    )
    if lineage.get("holdout_evidence_ids") != expected_holdout:
        raise ResearchReplayError("Registered holdout evidence lineage changed")
    return export, registered


def _validated_candidates(export: dict[str, Any], experiment: dict[str, Any]) -> list[dict[str, Any]]:
    manifest = export["dataset"]
    source = manifest["source"]
    source_package = source["strategy_package"]
    source_strategy = parse_package(copy.deepcopy(source_package))
    if (source_strategy.profile != "ladder-v1"
            or source_strategy.digest != source.get("strategy_sha256")):
        raise ResearchReplayError("Dataset source is not an exact supported ladder-v1 package")
    raw = experiment.get("candidates")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_CANDIDATES:
        raise ResearchReplayError("Research replay requires current plus at most five variants")
    generated = {row["package_sha256"]: row for row in generate_candidates(source_package)}
    result = []
    seen = set()
    for index, candidate in enumerate(raw):
        if not isinstance(candidate, dict):
            raise ResearchReplayError("Registered candidate is not an object")
        ident = _text(candidate.get("id"), "candidate.id")
        parsed = parse_package(copy.deepcopy(candidate.get("package")))
        digest = candidate.get("package_sha256")
        if parsed.profile != "ladder-v1" or digest != parsed.digest:
            raise ResearchReplayError("Registered candidate identity or family changed")
        if digest in seen:
            raise ResearchReplayError("Registered candidate digests must be unique")
        seen.add(digest)
        if index == 0:
            if digest != source_strategy.digest:
                raise ResearchReplayError("The first registered candidate must be current rules")
            kind, label = "current", "Current strategy"
        else:
            allowed = generated.get(digest)
            if allowed is None or digest == source_strategy.digest:
                raise ResearchReplayError("Candidate is not one of the five bounded variants")
            kind, label = "variant", allowed["label"]
        result.append({
            "id": ident,
            "kind": kind,
            "label": label,
            "package_sha256": digest,
            "package": parsed.package,
            "strategy": parsed,
        })
    return result


def _quote_events(
    export: dict[str, Any], cancelled: Callable[[], bool], *,
    maximum_event_at: float | None = None,
) -> tuple[
        list[dict[str, Any]], dict[str, MarketQuote], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    quotes: dict[str, MarketQuote] = {}
    exclusions: list[dict[str, Any]] = []
    seen = set()
    for index, row in enumerate(export["records"]["observations"]):
        if index % 100 == 0 and cancelled():
            raise ResearchReplayError("Research comparison cancelled")
        if not isinstance(row, dict):
            raise ResearchReplayError("Research observations must be objects")
        times = row.get("times")
        if maximum_event_at is not None:
            if not isinstance(times, dict):
                raise ResearchReplayError("Research evidence times are incomplete")
            if _number(times.get("event_at"), "event_at") > maximum_event_at:
                # The export digest and registered lineage were checked above,
                # but exploratory evaluation must not inspect holdout payloads,
                # quality flags, prices, signals or outcomes.
                continue
        ident = _text(row.get("evidence_id"), "evidence_id")
        mint = str(row.get("mint") or "")
        flags = row.get("quality_flags")
        if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
            raise ResearchReplayError("Research quality flags are invalid")
        reasons = sorted(_HARD_EXCLUSION_FLAGS.intersection(flags))
        if ident in seen:
            reasons.append("duplicate-evidence-identity")
        seen.add(ident)
        payload = row.get("payload")
        if not isinstance(times, dict) or not isinstance(payload, dict):
            reasons.append("incomplete-evidence-record")
        receipt = times.get("first_received_at") if isinstance(times, dict) else None
        if receipt is None:
            reasons.append("unknown-first-receipt")
        if reasons:
            exclusions.append({
                "evidence_id": ident,
                "mint": mint,
                "reason": ",".join(sorted(set(reasons))),
                "action": "excluded_from_point_in_time_replay",
            })
            continue
        try:
            quote = MarketQuote.from_record(payload)
            event_at = _number(times["event_at"], "event_at")
            known_at = _number(receipt, "first_received_at")
            source_at = _number(times["source_at"], "source_at")
            source_time_basis = _text(
                times.get("source_time_basis"), "source_time_basis",
            )
            if source_time_basis in ("unknown", "unverified"):
                raise ResearchReplayError("unknown or unverified source time")
            if source_time_basis not in (
                "independent", "provider", "inferred-observed-at",
            ):
                raise ResearchReplayError("unsupported source time basis")
            if known_at < event_at or source_at > known_at:
                raise ResearchReplayError("invalid evidence time order")
            if (quote.mint != mint or quote.route != row.get("route")
                    or quote.pool != row.get("pool") or quote.decimals != row.get("decimals")
                    or quote.observed_at != event_at):
                raise ResearchReplayError("normalized quote metadata changed")
            cap = quote_amount_micros(
                quote.market_cap_usd, "market_cap_usd", minimum=1,
            )
            liquidity = quote_amount_micros(
                quote.liquidity_usd, "liquidity_usd", minimum=1,
            )
            event = {
                "id": ident,
                "kind": "quote",
                "event_at": event_at,
                "first_known_at": known_at,
                "mint": mint,
                "route": quote.route,
                "pool": quote.pool,
                "decimals": quote.decimals,
                "quote_currency": "USD",
                "market_cap_micros": cap,
                "liquidity_micros": liquidity,
                "source_at": source_at,
                "source_time_basis": source_time_basis,
                **quote_price_fields(quote.price_usd, "price_usd"),
            }
            migration = quote.metadata.get("migration_from_pool")
            if isinstance(migration, str) and migration:
                event["migration_from_pool"] = migration
        except (KeyError, TypeError, ValueError, PortfolioReplayError) as exc:
            exclusions.append({
                "evidence_id": ident,
                "mint": mint,
                "reason": f"invalid-or-unsupported-quote: {exc}",
                "action": "excluded_from_point_in_time_replay",
            })
            continue
        events.append(event)
        quotes[ident] = quote
    events.sort(key=lambda row: (
        row["first_known_at"], row["event_at"], row["mint"], row["id"]
    ))
    return events, quotes, exclusions


def _duration(value: object, label: str, *, minimum: int = 0) -> int:
    result = math.ceil(_number(value, label))
    if result < minimum:
        raise ResearchReplayError(f"{label} must be at least {minimum}")
    return result


def _policy(strategy: Any, experiment: dict[str, Any]) -> dict[str, Any]:
    rules = strategy.effective_rules
    capital = experiment.get("capital")
    if not isinstance(capital, dict) or capital.get("unit") != "USD":
        raise ResearchReplayError("Research capital must use explicit USD units")
    targets = [{
        "market_cap_micros": _usd_micros(
            target.get("market_cap_usd"), "target market cap", minimum=1,
        ),
        "initial_fraction_ppm": _fraction_ppm(
            target.get("initial_fraction"), "target initial fraction",
        ),
    } for target in rules["targets"]]
    return {
        "schema": POLICY_SCHEMA,
        "family": "ladder-v1",
        "capital_unit": "quote-micros",
        "quote_currency": "USD",
        "starting_cash_micros": _usd_micros(
            capital.get("amount"), "starting capital", minimum=1,
        ),
        "order_micros": _usd_micros(rules["order_usd"], "order size", minimum=1),
        "max_positions": int(rules["max_positions"]),
        "submission_delay_seconds": _duration(
            rules["fill_delay_seconds"], "fill delay",
        ),
        "entry_expiry_seconds": _duration(
            rules["entry_expiry_seconds"], "entry expiry", minimum=1,
        ),
        "max_quote_age_seconds": _duration(
            rules["quote_max_age_seconds"], "quote maximum age",
        ),
        "max_hold_seconds": _duration(rules["max_hold_seconds"], "maximum hold", minimum=1),
        "stale_exit_seconds": _duration(
            rules["quote_gap_exit_seconds"], "quote gap exit", minimum=1,
        ),
        "stop_loss_ppm": _fraction_ppm(
            rules["stop_loss_fraction"], "stop loss", allow_negative=True,
        ),
        "trailing_drawdown_ppm": _fraction_ppm(
            rules["trailing_drawdown_fraction"], "trailing drawdown",
        ),
        "supported_routes": ["curve", "amm"],
        "targets": targets,
    }


def _cost_value(model: dict[str, Any], aliases: Iterable[str], default: object) -> object:
    for alias in aliases:
        if alias in model:
            return model[alias]
    return default


def _costs(strategy: Any, experiment: dict[str, Any]) -> tuple[
        dict[str, Any], dict[str, Any], dict[str, Any]]:
    model = _object(experiment.get("cost_model"), "cost model")
    rules = strategy.effective_rules
    if model.get("schema") == COST_SCHEMA:
        base = copy.deepcopy(model)
        base.setdefault("entry_rent_micros", 0)
        adverse = copy.deepcopy(base)
        stress = {"basis": "registered-v2-costs", "multiplier": 1.0}
        return base, adverse, stress

    def fraction(aliases: tuple[str, ...], default: object, label: str) -> int:
        return _fraction_ppm(_cost_value(model, aliases, default), label)

    fee = fraction(
        ("variable_fee_fraction_each_side", "fee_fraction"),
        rules.get("fee_fraction", 0), "variable fee",
    )
    spread = fraction(
        ("spread_fraction_each_side", "spread_fraction"), 0, "spread",
    )
    slippage = fraction(
        ("slippage_fraction_each_side", "slippage_fraction", "adverse_fraction"),
        rules.get("adverse_fraction", 0), "slippage",
    )
    impact = fraction(
        ("impact_fraction_at_full_liquidity",), 0, "size impact",
    )
    maximum_impact = fraction(
        ("maximum_impact_fraction",), 0.25 if impact else 0, "maximum size impact",
    )
    fixed = _usd_micros(_cost_value(
        model, ("fixed_cost_each_side", "fixed_fee_usd"),
        rules.get("fixed_fee_usd", 0),
    ), "fixed cost")
    rent = _usd_micros(_cost_value(
        model, ("rent_each_entry", "entry_rent_usd"), rules.get("entry_rent_usd", 0),
    ), "entry rent")
    failed_entry = _usd_micros(_cost_value(
        model, ("failed_entry_fee_usd",), rules.get("fixed_fee_usd", 0),
    ), "failed entry fee")
    failed_exit = _usd_micros(_cost_value(
        model, ("failed_exit_fee_usd",), rules.get("fixed_fee_usd", 0),
    ), "failed exit fee")
    base = {
        "schema": COST_SCHEMA,
        "fee_ppm": fee,
        "spread_ppm": spread,
        "slippage_ppm": slippage,
        "impact_ppm_at_full_liquidity": impact,
        "maximum_impact_ppm": maximum_impact,
        "fixed_fee_micros": fixed,
        "entry_rent_micros": rent,
        "failed_entry_fee_micros": failed_entry,
        "failed_exit_fee_micros": failed_exit,
    }
    multiplier = _number(model.get("adverse_multiplier", 2), "adverse cost multiplier")
    if not 1 <= multiplier <= 10:
        raise ResearchReplayError("adverse cost multiplier must be between 1 and 10")
    adverse = copy.deepcopy(base)
    for field in ("spread_ppm", "slippage_ppm", "impact_ppm_at_full_liquidity"):
        adverse[field] = min(PPM - 1, round(adverse[field] * multiplier))
    if adverse["impact_ppm_at_full_liquidity"]:
        adverse["maximum_impact_ppm"] = min(
            PPM - 1, max(adverse["maximum_impact_ppm"],
                         adverse["impact_ppm_at_full_liquidity"]),
        )
    stress = {
        "basis": "registered base costs; spread, slippage and size impact stressed",
        "multiplier": multiplier,
        "fees_and_fixed_charges_unchanged": True,
    }
    return base, adverse, stress


def _adverse_policy(policy: dict[str, Any], experiment: dict[str, Any]) -> tuple[
        dict[str, Any], dict[str, Any]]:
    model = experiment["cost_model"]
    additional = _duration(
        model.get("adverse_latency_seconds", policy["submission_delay_seconds"]),
        "adverse latency",
    )
    result = copy.deepcopy(policy)
    requested = policy["submission_delay_seconds"] + additional
    result["submission_delay_seconds"] = min(
        requested, result["entry_expiry_seconds"] - 1,
    )
    return result, {
        "base_submission_delay_seconds": policy["submission_delay_seconds"],
        "additional_adverse_latency_seconds": additional,
        "effective_adverse_submission_delay_seconds": result["submission_delay_seconds"],
        "capped_by_entry_expiry": result["submission_delay_seconds"] != requested,
    }


def _evidence_policy(experiment: dict[str, Any]) -> dict[str, Any]:
    registered = _object(experiment.get("evidence_policy"), "evidence policy")
    if registered.get("schema") == EVIDENCE_POLICY_SCHEMA:
        return registered
    policy = default_evidence_policy()
    integer_aliases = {
        "minimum_independent_episodes": ("minimum_independent_episodes",),
        "minimum_stability_groups": ("minimum_stability_groups",),
        "uncertainty_minimum_independent_episodes": (
            "uncertainty_minimum_independent_episodes",),
    }
    for target, aliases in integer_aliases.items():
        value = _cost_value(registered, aliases, policy[target])
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ResearchReplayError(f"evidence policy {target} must be a nonnegative integer")
        policy[target] = value
    if "minimum_coverage_fraction" in registered:
        policy["minimum_coverage_ppm"] = _fraction_ppm(
            registered["minimum_coverage_fraction"], "minimum coverage",
        )
    elif "minimum_coverage_ppm" in registered:
        value = registered["minimum_coverage_ppm"]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= PPM:
            raise ResearchReplayError("minimum coverage ppm is invalid")
        policy["minimum_coverage_ppm"] = value
    if "minimum_improvement_usd" in registered:
        policy["minimum_improvement_micros"] = _usd_micros(
            registered["minimum_improvement_usd"], "minimum improvement",
        )
    elif "minimum_improvement_micros" in registered:
        value = registered["minimum_improvement_micros"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ResearchReplayError("minimum improvement micros is invalid")
        policy["minimum_improvement_micros"] = value
    if "maximum_drawdown_fraction" in registered:
        policy["maximum_drawdown_ppm"] = _fraction_ppm(
            registered["maximum_drawdown_fraction"], "maximum drawdown",
        )
    elif "maximum_drawdown_ppm" in registered:
        value = registered["maximum_drawdown_ppm"]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= PPM:
            raise ResearchReplayError("maximum drawdown ppm is invalid")
        policy["maximum_drawdown_ppm"] = value
    if "require_nonnegative_adverse_difference" in registered:
        value = registered["require_nonnegative_adverse_difference"]
        if not isinstance(value, bool):
            raise ResearchReplayError("adverse evidence requirement must be boolean")
        policy["require_nonnegative_adverse_difference"] = value
    policy["registered_policy"] = registered
    return policy


def _signals_for_interval(
    candidate: dict[str, Any], quote_events: list[dict[str, Any]],
    quote_objects: dict[str, MarketQuote], *, lower: float, upper: float,
    embargo: float, split: str, cancelled: Callable[[], bool],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    engine = DecisionEngine(candidate["strategy"])
    batches: dict[float, list[dict[str, Any]]] = {}
    for event in quote_events:
        if event["first_known_at"] <= upper:
            batches.setdefault(float(event["first_known_at"]), []).append(event)
    signals = []
    blocked: dict[str, int] = {}
    sequence = 0
    for batch_index, known_at in enumerate(sorted(batches)):
        if batch_index % 100 == 0 and cancelled():
            raise ResearchReplayError("Research comparison cancelled")
        rows = sorted(batches[known_at], key=lambda row: (
            row["event_at"], row["mint"], row["id"]
        ))
        opportunities: list[dict[str, Any]] = []
        allow = (
            known_at <= upper - embargo
            and any(lower < row["event_at"] <= upper - embargo for row in rows)
        )
        decisions = engine.evaluate(
            [quote_objects[row["id"]] for row in rows], known_at,
            allow_entries=allow, opportunities=opportunities if allow else None,
        )
        for opportunity in opportunities:
            if opportunity["kind"] == "entry_blocked":
                reason = str(opportunity["reason"])
                blocked[reason] = blocked.get(reason, 0) + 1
        if not allow:
            continue
        newest = {}
        for row in rows:
            newest[row["mint"]] = row
        for decision in decisions:
            if decision.kind != "entry_recommended":
                continue
            evidence = newest.get(decision.mint)
            if evidence is None:
                raise ResearchReplayError("Decision did not retain its first-known quote")
            if not lower < evidence["event_at"] <= upper - embargo:
                continue
            sequence += 1
            signal_id = (
                f"signal:{candidate['id']}:{split}:{sequence}:"
                f"{hashlib.sha256(evidence['id'].encode()).hexdigest()[:12]}"
            )
            signals.append({
                "id": signal_id,
                "kind": "entry_signal",
                "event_at": known_at,
                "first_known_at": known_at,
                "mint": decision.mint,
                "evidence_ids": [evidence["id"]],
                "episode_id": f"episode:{signal_id}",
                "dependence_group": f"mint:{decision.mint}",
                "split": split,
            })
    return signals, blocked


def _interval_events(
    quote_events: list[dict[str, Any]], signals: list[dict[str, Any]],
    *, lower: float, upper: float, candidate_id: str, split: str,
) -> list[dict[str, Any]]:
    rows = [
        copy.deepcopy(row) for row in quote_events
        if lower < row["event_at"] <= upper and row["first_known_at"] <= upper
    ]
    rows.extend(copy.deepcopy(signals))
    rows.append({
        "id": f"clock:{candidate_id}:{split}:{upper}",
        "kind": "clock",
        "event_at": upper,
        "first_known_at": upper,
    })
    return rows


def _execution(
    strategy: Any, experiment: dict[str, Any],
    validation_events: list[dict[str, Any]], holdout_events: list[dict[str, Any]],
) -> dict[str, Any]:
    registered = experiment["cost_model"].get("execution_assumption", {})
    if not isinstance(registered, dict):
        raise ResearchReplayError("execution assumption must be an object")
    explicit_failed = registered.get("failed_entry_signal_ids")
    configured_model = registered.get("entry_failure_model")
    if configured_model is not None:
        if not isinstance(configured_model, dict):
            raise ResearchReplayError("entry failure model must be an object")
        failure_model = copy.deepcopy(configured_model)
        if explicit_failed is None:
            explicit_failed = []
    elif explicit_failed is None:
        probability = _number(
            registered.get(
                "failed_entry_probability",
                strategy.effective_rules.get("failed_attempt_probability", 0),
            ),
            "failed entry probability",
        )
        if not 0 <= probability <= 1:
            raise ResearchReplayError("failed entry probability must be between 0 and 1")
        failure_model = {
            "method": "sha256-threshold-v1",
            "seed": str(registered.get(
                "failed_entry_seed", f"{experiment['id']}:failed-entry",
            )),
            "failure_ppm": round(probability * PPM),
        }
        explicit_failed = []
    else:
        failure_model = {"method": "none"}
    unavailable = registered.get("unavailable_exit_quote_ids", [])
    partial = registered.get("exit_fill_ppm_by_quote_id", {})
    if (not isinstance(explicit_failed, list)
            or any(not isinstance(value, str) or not value for value in explicit_failed)):
        raise ResearchReplayError("failed entry signal IDs must be a list of IDs")
    if (not isinstance(unavailable, list)
            or any(not isinstance(value, str) or not value for value in unavailable)):
        raise ResearchReplayError("unavailable exit quote IDs must be a list of IDs")
    if not isinstance(partial, dict):
        raise ResearchReplayError("partial exit fill assumptions must be an object")
    return {
        "schema": EXECUTION_SCHEMA,
        "failed_entry_signal_ids": sorted(set(explicit_failed)),
        "entry_failure_model": failure_model,
        "unavailable_exit_quote_ids": sorted(set(unavailable)),
        "exit_fill_ppm_by_quote_id": dict(sorted(partial.items())),
    }


def _holding_events(
    quote_events: list[dict[str, Any]], *, lower: float, upper: float,
    split: str, delay: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = [
        copy.deepcopy(row) for row in quote_events
        if lower < row["event_at"] <= upper and row["first_known_at"] <= upper
    ]
    benchmark = build_holding_benchmark(selected)
    events = benchmark["events"]
    by_mint: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if event["kind"] == "quote":
            by_mint.setdefault(event["mint"], []).append(event)
    for event in events:
        if event["kind"] == "entry_signal":
            event["split"] = split
        elif event["kind"] == "exit_signal" and delay:
            quotes = sorted(by_mint[event["mint"]], key=lambda row: row["first_known_at"])
            scheduled = max(quotes[0]["first_known_at"], quotes[-1]["first_known_at"] - delay)
            event["event_at"] = scheduled
            event["first_known_at"] = scheduled
    events.append({
        "id": f"clock:holding:{split}:{upper}",
        "kind": "clock", "event_at": upper, "first_known_at": upper,
    })
    return events, benchmark


def _candidate_rows(
    candidates: list[dict[str, Any]], quote_events: list[dict[str, Any]],
    quote_objects: dict[str, MarketQuote], experiment: dict[str, Any],
    progress: Callable[[float], None], cancelled: Callable[[], bool], *,
    evaluate_holdout: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    splits = experiment["splits"]
    train = float(splits["train_end_at"])
    validation = float(splits["validation_end_at"])
    holdout = float(splits["holdout_end_at"])
    embargo = float(experiment["embargo_seconds"])
    rows = []
    diagnostics: dict[str, Any] = {}
    for index, candidate in enumerate(candidates):
        if cancelled():
            raise ResearchReplayError("Research comparison cancelled")
        policy = _policy(candidate["strategy"], experiment)
        adverse_policy, latency = _adverse_policy(policy, experiment)
        base_costs, adverse_costs, stress = _costs(candidate["strategy"], experiment)
        validation_signals, validation_blocked = _signals_for_interval(
            candidate, quote_events, quote_objects, lower=train, upper=validation,
            embargo=embargo, split="validation", cancelled=cancelled,
        )
        if evaluate_holdout:
            holdout_signals, holdout_blocked = _signals_for_interval(
                candidate, quote_events, quote_objects, lower=validation, upper=holdout,
                embargo=embargo, split="holdout", cancelled=cancelled,
            )
        else:
            holdout_signals, holdout_blocked = [], {}
        validation_events = _interval_events(
            quote_events, validation_signals, lower=train, upper=validation,
            candidate_id=candidate["id"], split="validation",
        )
        holdout_events = _interval_events(
            quote_events, holdout_signals, lower=validation, upper=holdout,
            candidate_id=candidate["id"], split="holdout",
        )
        execution = _execution(
            candidate["strategy"], experiment, validation_events, holdout_events,
        )
        rows.append({
            "id": candidate["id"],
            "kind": candidate["kind"],
            "label": candidate["label"],
            "policy": policy,
            "adverse_policy": adverse_policy,
            "base_costs": base_costs,
            "adverse_costs": adverse_costs,
            "execution": execution,
            "events_validation": validation_events,
            "events_holdout": holdout_events,
            "episode_returns_validation_micros": [],
        })
        diagnostics[candidate["id"]] = {
            "package_sha256": candidate["package_sha256"],
            "validation_signals": len(validation_signals),
            "validation_blocked_reasons": validation_blocked,
            "cost_stress": stress,
            "latency_stress": latency,
            "holdout_evaluated": evaluate_holdout,
        }
        if evaluate_holdout:
            diagnostics[candidate["id"]].update({
                "holdout_signals": len(holdout_signals),
                "holdout_blocked_reasons": holdout_blocked,
            })
        progress(0.2 + 0.35 * (index + 1) / len(candidates))

    current = rows[0]
    validation_holding, validation_definition = _holding_events(
        quote_events, lower=train, upper=validation, split="validation",
        delay=current["policy"]["submission_delay_seconds"],
    )
    if evaluate_holdout:
        holdout_holding, holdout_definition = _holding_events(
            quote_events, lower=validation, upper=holdout, split="holdout",
            delay=current["policy"]["submission_delay_seconds"],
        )
    else:
        holdout_holding, holdout_definition = [], None
    holding_strategy = candidates[0]["strategy"]
    holding_execution = _execution(
        holding_strategy, experiment, validation_holding, holdout_holding,
    )
    rows.append({
        "id": "holding",
        "kind": "holding",
        "label": "Simple first-to-last quote holding benchmark",
        "policy": copy.deepcopy(current["policy"]),
        "adverse_policy": copy.deepcopy(current["adverse_policy"]),
        "base_costs": copy.deepcopy(current["base_costs"]),
        "adverse_costs": copy.deepcopy(current["adverse_costs"]),
        "execution": holding_execution,
        "events_validation": validation_holding,
        "events_holdout": holdout_holding,
        "episode_returns_validation_micros": [],
    })
    diagnostics["holding"] = {
        "package_sha256": None,
        "definition": validation_definition["definition"],
        "validation_eligible_mints": validation_definition["eligible_mints"],
        "validation_excluded_mints": validation_definition["excluded_mints"],
        "holdout_evaluated": evaluate_holdout,
        "cost_stress": diagnostics[candidates[0]["id"]]["cost_stress"],
        "latency_stress": diagnostics[candidates[0]["id"]]["latency_stress"],
    }
    if holdout_definition is not None:
        diagnostics["holding"].update({
            "holdout_eligible_mints": holdout_definition["eligible_mints"],
            "holdout_excluded_mints": holdout_definition["excluded_mints"],
        })
    return rows, diagnostics


def _usd(micros: int | None) -> float | None:
    return None if micros is None else micros / 1_000_000


def _comparison_summary(
    comparison: dict[str, Any], candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    packages = {row["id"]: row["package_sha256"] for row in candidates}
    starting = candidates and comparison["validation_results"][0]["base"]["policy"][
        "starting_cash_micros"
    ]
    result = [{
        "id": "cash", "kind": "cash", "label": "Cash / no-trade baseline",
        "package_sha256": None, "starting_capital": _usd(starting),
        "ending_capital": _usd(starting), "capital_unit": "USD",
        "modeled_net_pnl": 0.0, "modeled_realized_pnl": 0.0,
        "modeled_unrealized_pnl": 0.0, "maximum_drawdown_fraction": 0.0,
        "coverage_fraction": 1.0, "independent_observations": 0,
        "modeled_costs": 0.0, "uncertainty": None,
        "equity": [],
    }]
    for row in comparison["validation_results"]:
        report = row["base"]
        metrics = report["metrics"]
        result.append({
            "id": row["candidate_id"],
            "kind": "candidate" if row["kind"] == "variant" else row["kind"],
            "label": row["label"],
            "package_sha256": packages.get(row["candidate_id"]),
            "starting_capital": _usd(report["policy"]["starting_cash_micros"]),
            "ending_capital": (
                _usd(metrics["conservative_ending_equity_micros"])
                if metrics["valuation_complete"] else None
            ),
            "capital_unit": "USD",
            "modeled_net_pnl": _usd(metrics["net_result_micros"]),
            "modeled_realized_pnl": _usd(metrics["realized_pnl_micros"]),
            "modeled_unrealized_pnl": _usd(metrics["known_unrealized_pnl_micros"]),
            "maximum_drawdown_fraction": metrics["maximum_drawdown_ppm"] / PPM,
            "coverage_fraction": metrics["coverage_ppm"] / PPM,
            "independent_observations": metrics["independent_episode_count"],
            "modeled_costs": _usd(sum(report["cost_breakdown"].values())),
            "uncertainty": (
                None if row["uncertainty"] is None
                else canonical_json(row["uncertainty"])
            ),
            "equity": [{
                "at": point["at"],
                "equity": _usd(point["conservative_equity_micros"]),
                "gap_reason": (
                    None if point["valuation_complete"] else "unknown_or_unpriced_exit"
                ),
            } for point in report["equity_curve"]],
        })
    return result


def _ledger_summary(comparison: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for candidate in comparison["validation_results"]:
        for fill in candidate["base"]["fills"]:
            costs = sum(fill["costs"].values())
            value = -fill["cash_delta_micros"] if fill["side"] == "entry" else fill["cash_delta_micros"]
            rows.append({
                "id": fill["id"],
                "comparison_id": candidate["candidate_id"],
                "opened_at": fill["at"] if fill["side"] == "entry" else None,
                "closed_at": fill["at"] if fill["side"] == "exit" else None,
                "symbol": fill["mint"],
                "side": fill["side"],
                "quantity": fill["quantity_atoms"],
                "price": (
                    fill["price_quote_units_per_token"] / PRICE_UNITS_PER_QUOTE
                    if fill.get("price_quote_units_per_token") is not None else None
                ),
                "value": _usd(value),
                "cost": _usd(costs),
                "pnl": _usd(fill.get("realized_pnl_micros")),
                "state": "modeled",
                "explanation": "Modeled token atoms; see detailed comparison for units and assumptions.",
            })
    return rows


def _exclusion_summary(
    adapter_exclusions: list[dict[str, Any]], comparison: dict[str, Any],
) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = {}
    for row in adapter_exclusions:
        key = (row["reason"], row["action"])
        counts[key] = counts.get(key, 0) + 1
    for candidate in comparison["validation_results"]:
        for row in candidate["base"]["exclusions"]:
            key = (str(row["reason"]), "excluded_or_left_unpriced_by_portfolio_replay")
            counts[key] = counts.get(key, 0) + 1
    return [
        {"reason": reason, "count": count, "action": action}
        for (reason, action), count in sorted(counts.items())
    ]


def _recommendation(
    comparison: dict[str, Any], experiment: dict[str, Any],
    dataset_eligible: bool,
) -> tuple[dict[str, Any], bool, list[str]]:
    source = comparison["recommendation"]
    blockers = list(source.get("reasons", []))
    if not dataset_eligible:
        blockers.append("dataset_not_eligible")
    if experiment["purpose"] != "eligible":
        blockers.append("experiment_purpose_is_exploratory")
    eligible = (
        dataset_eligible
        and experiment["purpose"] == "eligible"
        and source["action"] == "eligible_inactive_draft"
    )
    if eligible:
        action = "create_inactive_draft"
        candidate_id = source["candidate_id"]
    elif source["action"] in {"prefer_cash", "keep_current"}:
        action = source["action"]
        candidate_id = source.get("candidate_id")
    else:
        action = "insufficient_evidence"
        candidate_id = None
    if experiment["purpose"] != "eligible" and action == "create_inactive_draft":
        action, candidate_id = "insufficient_evidence", None
    blockers = sorted(set(blockers))
    return {
        "action": action,
        "candidate_id": candidate_id,
        "reason": "; ".join(blockers) if blockers else "predeclared evidence policy passed",
        "blockers": blockers,
    }, eligible, blockers


def evaluate_research_experiment(
    dataset: object,
    experiment: object,
    progress: Callable[[float], None] = lambda _value: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Evaluate one verified export and registered experiment without authority.

    The result is deterministic for identical JSON inputs and evaluator version.
    Progress is advisory; cancellation never returns a partial report.
    """
    progress(0.02)
    export, registered = _validate_inputs(dataset, experiment)
    if cancelled():
        raise ResearchReplayError("Research comparison cancelled")
    candidates = _validated_candidates(export, registered)
    progress(0.08)
    evaluate_holdout = registered["purpose"] == "eligible"
    quote_events, quote_objects, adapter_exclusions = _quote_events(
        export, cancelled,
        maximum_event_at=(None if evaluate_holdout
                          else float(registered["splits"]["validation_end_at"])),
    )
    if not quote_events:
        raise ResearchReplayError("Dataset contains no point-in-time replayable quotes")
    progress(0.18)
    rows, diagnostics = _candidate_rows(
        candidates, quote_events, quote_objects, registered, progress, cancelled,
        evaluate_holdout=evaluate_holdout,
    )
    progress(0.62)
    evidence_policy = _evidence_policy(registered)
    comparison = compare_portfolios(
        rows,
        evidence_policy=evidence_policy,
        input_lineage={
            "dataset_id": export["dataset"]["id"],
            "dataset_content_sha256": export["dataset"]["content_sha256"],
            "source_session_id": export["dataset"]["source"]["session_id"],
            "experiment_id": registered["id"],
            "experiment_request_sha256": registered["request_sha256"],
            "holdout_evidence_ids": (
                registered["lineage"]["holdout_evidence_ids"] if evaluate_holdout else []
            ),
        },
        cancelled=cancelled,
        evaluate_holdout=evaluate_holdout,
    )
    if comparison.get("schema") != COMPARISON_SCHEMA:
        raise ResearchReplayError("Portfolio comparison returned an unsupported report")
    progress(0.9)
    recommendation, eligible, blockers = _recommendation(
        comparison, registered, bool(export["dataset"]["eligibility"]["eligible"]),
    )
    summaries = _comparison_summary(comparison, candidates)
    unknown = sum(
        len(result["base"]["open_positions"])
        for result in comparison["validation_results"]
    ) + sum(
        len(result["base"]["open_positions"])
        for result in comparison["holdout_results"]
    )
    exclusions = _exclusion_summary(adapter_exclusions, comparison)
    cost_stress = [{
        "id": f"stress:{row['candidate_id']}",
        "label": "Adverse registered cost and latency scenario",
        "comparison_id": row["candidate_id"],
        "modeled_net_pnl": _usd(row["adverse"]["metrics"]["net_result_micros"]),
        "costs": _usd(sum(row["adverse"]["cost_breakdown"].values())),
        "changed_recommendation": (
            row["base"]["metrics"]["net_result_micros"] >= 0
            and row["adverse"]["metrics"]["net_result_micros"] < 0
        ),
    } for row in comparison["validation_results"]]
    limitations = [
        "Offline modeled fills are not funded performance or executable quotes.",
        "Unknown and unavailable exits remain open and reduce valuation coverage.",
        "Seeded SHA-256 failed entries are deterministic registered scenarios, not service receipts.",
        "Validation selects; the registry controls one-time holdout exposure across datasets.",
        "No wallet allocation, signing authority, order request or production activation is present.",
    ]
    core = {
        "schema": REPORT_SCHEMA,
        "adapter_version": ADAPTER_VERSION,
        "engine_version": ENGINE_VERSION,
        "registered_engine_version": registered["engine_version"],
        "code_version": registered["code_version"],
        "experiment_id": registered["id"],
        "dataset_id": registered["dataset_id"],
        "created_at": registered["registered_at"],
        "period": {
            "first_at": export["dataset"]["ranges"]["event"]["first"],
            "last_at": registered["splits"][
                "holdout_end_at" if evaluate_holdout else "validation_end_at"
            ],
        },
        "capital": registered["capital"],
        "capital_unit": registered["capital"]["unit"],
        "cost_model": registered["cost_model"],
        "purpose": registered["purpose"],
        "eligible": eligible,
        "comparisons": summaries,
        "ledger": _ledger_summary(comparison),
        "cost_stress": cost_stress,
        "baselines": ["cash-no-trade", "current-rules", "simple-first-to-last-holding"],
        "coverage": {
            "dataset_quality": export["dataset"]["quality"],
            "comparison_count": len(summaries),
            "excluded_evidence_count": len(adapter_exclusions),
            "unknown_or_unpriced": unknown,
        },
        "exclusions": exclusions,
        "unknown_or_unpriced": unknown,
        "recommendation": recommendation,
        "blockers": blockers,
        "holdout_state": (
            "exposed" if registered["purpose"] == "eligible" else "not_evaluated"
        ),
        "limitations": limitations,
        "candidate_diagnostics": diagnostics,
        "adapter_exclusions": adapter_exclusions,
        "detailed_comparison": comparison,
        "funded_performance": False,
        "submission_enabled": False,
        "wallet_allocation_lamports": "0",
        "signing_authority": False,
    }
    ident = content_hash(core)
    progress(0.98)
    return {**core, "id": ident, "reproducibility_sha256": ident}
