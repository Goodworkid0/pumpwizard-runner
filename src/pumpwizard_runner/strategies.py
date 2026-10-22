"""Versioned, data-only strategy package parsing.

The importer deliberately preserves unsupported strategy information rather than
converting it into a similar live policy. Strategy content is data, never code.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any


class StrategyImportError(ValueError):
    """A local strategy package could not be safely identified."""


_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}(?:[-+][A-Za-z0-9.-]+)?$")
_REQUIRED_NATIVE_RULES = {
    "entry_style", "entry_cap_min_usd", "entry_cap_max_usd",
    "minimum_growth_fraction", "minimum_reserve_growth_usd",
    "take_profit_fraction", "stop_loss_fraction", "max_hold_seconds",
}
_LADDER_RULES = {
    "entry_style", "entry_cap_min", "entry_cap_max", "observation_seconds",
    "minimum_samples", "minimum_price_changes", "minimum_growth_fraction",
    "minimum_reserve_growth_usd", "maximum_peak_drawdown", "max_sample_gap_seconds",
    "ownership_ttl_seconds", "max_positions", "stop_loss_fraction",
    "trailing_drawdown_fraction", "quote_gap_exit_seconds", "max_hold_seconds", "targets",
}
_LADDER_REQUIRED_NUMBERS = {
    "budget_usd", "order_usd", "entry_cap_min", "entry_cap_max", "observation_seconds",
    "minimum_samples", "minimum_price_changes", "minimum_growth_fraction",
    "minimum_reserve_growth_usd", "maximum_peak_drawdown", "max_sample_gap_seconds",
    "quote_max_age_seconds", "entry_expiry_seconds", "fill_delay_seconds",
    "ownership_ttl_seconds", "min_curve_liquidity_usd", "min_amm_liquidity_usd",
    "max_entry_reserve_fraction", "max_roundtrip_loss_fraction", "fee_fraction",
    "adverse_fraction", "fixed_fee_usd", "entry_rent_usd", "failed_attempt_probability",
    "stop_loss_fraction",
    "trailing_drawdown_fraction", "quote_gap_exit_seconds", "max_hold_seconds",
}
_SUPPORTED_STYLES = {
    "momentum", "breakout", "pullback_reclaim", "slow_burn",
    "persistent", "higher-low", "multi-window", "squeeze", "retest", "post-grad",
    "liquidity", "relative", "second-wave", "recovery",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ImportedStrategy:
    schema: str
    strategy_id: str
    label: str
    version: str | None
    engine: str | None
    compatibility: str
    reason: str
    package: dict[str, Any]
    digest: str
    effective_rules: dict[str, Any]
    profile: str


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StrategyImportError(f"{field} must be an object")
    return value


def _text(value: Any, field: str, maximum: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise StrategyImportError(f"{field} must be non-empty text no longer than {maximum} characters")
    return value.strip()


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StrategyImportError(f"{field} must be a number")
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise StrategyImportError(f"{field} must be finite")
    return result


def parse_package(raw: str | bytes | dict[str, Any]) -> ImportedStrategy:
    """Parse native v2 packages and inspect legacy public v1 handoffs."""
    if isinstance(raw, (str, bytes)):
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise StrategyImportError("Strategy file must contain one JSON object") from exc
    else:
        value = raw
    package = _object(value, "strategy package")
    schema = _text(package.get("schema"), "schema", 80)
    if schema == "pumpwizard.strategy/v2":
        return _parse_native(package)
    if schema == "pumpwizard.strategy-handoff/v1":
        return _parse_legacy_handoff(package)
    raise StrategyImportError(f"Unsupported strategy schema: {schema}")


def _parse_native(package: dict[str, Any]) -> ImportedStrategy:
    strategy = _object(package.get("strategy"), "strategy")
    strategy_id = _text(strategy.get("id"), "strategy.id", 64)
    if not _ID.fullmatch(strategy_id):
        raise StrategyImportError("strategy.id must contain lowercase letters, digits, and hyphens")
    label = _text(strategy.get("label"), "strategy.label")
    version = _text(strategy.get("version"), "strategy.version", 40)
    if not _VERSION.fullmatch(version):
        raise StrategyImportError("strategy.version is not a supported version string")
    engine = _text(strategy.get("engine"), "strategy.engine", 80)
    market = _object(package.get("market"), "market")
    if market.get("chain") != "solana":
        raise StrategyImportError("Only the solana market contract is supported")
    _finite_number(market.get("observation_seconds"), "market.observation_seconds")
    rules = _object(package.get("rules"), "rules")
    missing = sorted(_REQUIRED_NATIVE_RULES - set(rules))
    if missing:
        raise StrategyImportError("rules is missing: " + ", ".join(missing))
    for name in _REQUIRED_NATIVE_RULES - {"entry_style"}:
        _finite_number(rules[name], "rules." + name)
    if not isinstance(rules["entry_style"], str) or not rules["entry_style"].strip():
        raise StrategyImportError("rules.entry_style must be non-empty text")
    if engine == "pumpwizard-core/1":
        compatibility = "monitoring-ready"
        reason = ("The package is ready for local monitoring with the bundled decision engine. "
                  "Monitoring compatibility does not establish reviewed execution eligibility.")
    else:
        compatibility = "recognized-not-runnable"
        reason = "The package is structurally valid but needs an engine version that is not installed."
    return ImportedStrategy(schema="pumpwizard.strategy/v2", strategy_id=strategy_id, label=label,
                            version=version, engine=engine, compatibility=compatibility,
                            reason=reason, package=package, digest=content_hash(package),
                            effective_rules=dict(rules), profile="native-v2")


def _parse_legacy_handoff(package: dict[str, Any]) -> ImportedStrategy:
    strategy = _object(package.get("strategy"), "strategy")
    strategy_id = _text(strategy.get("id"), "strategy.id", 64)
    if not _ID.fullmatch(strategy_id):
        raise StrategyImportError("strategy.id must contain lowercase letters, digits, and hyphens")
    label = _text(strategy.get("label"), "strategy.label")
    configuration = _object(package.get("configuration"), "configuration")
    trial = package.get("trial") or strategy.get("trial") or configuration.get("trial")
    if trial:
        reason = ("Full trial configuration saved. V2/V3 trial overlays are not implemented "
                  "by the local engine; starting it as its parent would change its decisions.")
        compatibility = "legacy-inspection-only"
        profile = "unknown"
    elif _is_ladder_contract(configuration):
        reason = ("Legacy PumpWizard rules mapped to the bundled local decision engine. "
                  "The app collects its own observations. Reviewed execution requires a separate exact-configuration check.")
        compatibility = "monitoring-ready"
        profile = "ladder-v1"
    else:
        # Public handoffs can name a strategy whose data semantics and engine
        # were never exported. Preserve it for inspection rather than guessing.
        reason = ("Legacy simulation handoff stored for inspection. It lacks the complete local "
                  "engine and market-data contract required for execution.")
        compatibility = "legacy-inspection-only"
        profile = "unknown"
    return ImportedStrategy(schema="pumpwizard.strategy-handoff/v1", strategy_id=strategy_id,
                            label=label, version=None, engine="pumpwizard-core/1" if profile != "unknown" else None,
                            compatibility=compatibility, reason=reason, package=package,
                            digest=content_hash(package), effective_rules=dict(configuration), profile=profile)


def _is_ladder_contract(configuration: dict[str, Any]) -> bool:
    """Identify the public early-ladder rules without treating arbitrary JSON as one."""
    if configuration.get("version") != "early-ladder-v1":
        return False
    if configuration.get("mode") != "paper_only":
        return False
    if configuration.get("entry_style") not in _SUPPORTED_STYLES:
        return False
    if configuration.get("entry_policy") not in ("strict", "simulation_unverified"):
        return False
    known = _LADDER_RULES | _LADDER_REQUIRED_NUMBERS | {
        "version", "mode", "entry_policy", "breakout_fraction", "pullback_min_fraction",
        "reclaim_fraction",
    }
    if set(configuration) - known:
        return False  # Never silently discard an additional decision rule.
    if not (_LADDER_RULES | _LADDER_REQUIRED_NUMBERS) <= set(configuration):
        return False
    targets = configuration.get("targets")
    if not isinstance(targets, list) or not targets:
        return False
    try:
        for key in (_LADDER_RULES | _LADDER_REQUIRED_NUMBERS) - {"entry_style", "targets"}:
            _finite_number(configuration[key], "configuration." + key)
        for key in _LADDER_REQUIRED_NUMBERS - {"entry_rent_usd", "failed_attempt_probability", "stop_loss_fraction"}:
            if configuration[key] <= 0:
                return False
        if configuration["entry_rent_usd"] < 0 or configuration["failed_attempt_probability"] < 0:
            return False
        if configuration["budget_usd"] < configuration["order_usd"]:
            return False
        if not 0 < configuration["entry_cap_min"] < configuration["entry_cap_max"]:
            return False
        if any(int(configuration[key]) != configuration[key] or configuration[key] < 1
               for key in ("max_positions", "minimum_samples", "minimum_price_changes")):
            return False
        if not -1 < configuration["stop_loss_fraction"] < 0:
            return False
        if configuration["entry_expiry_seconds"] <= configuration["fill_delay_seconds"]:
            return False
        for key in ("fee_fraction", "adverse_fraction", "maximum_peak_drawdown",
                    "max_entry_reserve_fraction", "max_roundtrip_loss_fraction",
                    "trailing_drawdown_fraction", "failed_attempt_probability"):
            if not 0 <= configuration[key] < 1:
                return False
        for key in ("breakout_fraction", "pullback_min_fraction", "reclaim_fraction"):
            if key in configuration and not 0 < _finite_number(configuration[key], "configuration." + key) < 1:
                return False
        for target in targets:
            target = _object(target, "configuration.targets item")
            _finite_number(target.get("market_cap_usd"), "configuration.targets.market_cap_usd")
            _finite_number(target.get("initial_fraction"), "configuration.targets.initial_fraction")
    except StrategyImportError:
        return False
    previous_cap = configuration["entry_cap_max"]
    total_fraction = 0.0
    for target in targets:
        if target["market_cap_usd"] <= previous_cap or not 0 < target["initial_fraction"] <= 1:
            return False
        previous_cap = target["market_cap_usd"]
        total_fraction += target["initial_fraction"]
    if total_fraction > 1 + 1e-9:
        return False
    return True
