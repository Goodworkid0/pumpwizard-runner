"""Deterministic finite-capital research replay with fixed-precision ledgers.

This module is deliberately pure: it imports no wallet, signer, RPC, service, or
database code.  Observed quote/signal events are distinct from modeled execution
assumptions. Cash ledgers use integer quote micros, prices use independent
integer 1e-18 quote units, and token balances use integer atoms; callers must
name the quote currency, source-time basis and token decimals.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import math
import statistics
from typing import Any, Callable, Iterable

from .strategies import canonical_json, content_hash


INPUT_SCHEMA = "pumpwizard.portfolio-replay-input/v3"
POLICY_SCHEMA = "pumpwizard.portfolio-replay-policy/v2"
COST_SCHEMA = "pumpwizard.portfolio-replay-costs/v2"
EXECUTION_SCHEMA = "pumpwizard.portfolio-replay-execution/v3"
REPORT_SCHEMA = "pumpwizard.portfolio-replay-report/v3"
COMPARISON_SCHEMA = "pumpwizard.portfolio-comparison/v3"
EVIDENCE_POLICY_SCHEMA = "pumpwizard.replay-evidence-policy/v1"
ENGINE_VERSION = "pumpwizard-finite-capital-replay/3"
PPM = 1_000_000
MONEY_MICROS_PER_QUOTE = 1_000_000
PRICE_UNITS_PER_QUOTE = 10 ** 18
PRICE_UNITS_PER_MICRO = PRICE_UNITS_PER_QUOTE // MONEY_MICROS_PER_QUOTE
MAX_LEDGER_MICROS = 10 ** 24
MAX_PRICE_UNITS_PER_TOKEN = 10 ** 42
MAX_TOKEN_ATOMS = 10 ** 48
MAX_REPORTED_EQUITY_POINTS = 256

_EXECUTABLE_SOURCE_TIME_BASES = frozenset({
    "independent", "provider", "inferred-observed-at", "legacy-event-at",
})
_SOURCE_TIME_BASES = _EXECUTABLE_SOURCE_TIME_BASES | {"unverified", "unknown"}


class PortfolioReplayError(ValueError):
    """The replay input is ambiguous, unsupported, or internally inconsistent."""


def _integer(
    value: Any, field: str, *, minimum: int = 0, maximum: int | None = None,
) -> int:
    if (isinstance(value, bool) or not isinstance(value, int) or value < minimum
            or (maximum is not None and value > maximum)):
        suffix = f" and <= {maximum}" if maximum is not None else ""
        raise PortfolioReplayError(f"{field} must be an integer >= {minimum}{suffix}")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PortfolioReplayError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise PortfolioReplayError(f"{field} must be a finite number")
    return result


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortfolioReplayError(f"{field} must be non-empty text")
    return value.strip()


def _canonical(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PortfolioReplayError(f"{field} must be an object")
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise PortfolioReplayError(f"{field} must be canonical JSON") from exc


def quote_price_units(value: object, field: str = "price") -> int:
    """Convert a positive decimal quote/token price to exact 1e-18 quote units.

    Price precision is deliberately independent of the quote-micro cash ledger.
    Values below the scale, or values that need rounding at this scale, fail
    closed instead of being raised to a tradable minimum.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise PortfolioReplayError(f"{field} must be a positive decimal amount")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise PortfolioReplayError(f"{field} must be a positive decimal amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise PortfolioReplayError(f"{field} must be finite and positive")
    with localcontext() as context:
        context.prec = 100
        scaled = amount * PRICE_UNITS_PER_QUOTE
        integral = scaled.to_integral_value()
    if scaled != integral:
        raise PortfolioReplayError(
            f"{field} is below the supported 1e-18 quote-per-token precision"
        )
    return _integer(
        int(integral), field, minimum=1, maximum=MAX_PRICE_UNITS_PER_TOKEN,
    )


def quote_amount_micros(
    value: object, field: str = "amount", *, minimum: int = 0,
) -> int:
    """Round a nonnegative decimal quote amount half-even to ledger micros."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise PortfolioReplayError(f"{field} must be a nonnegative decimal amount")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise PortfolioReplayError(f"{field} must be a nonnegative decimal amount") from exc
    if not amount.is_finite() or amount < 0:
        raise PortfolioReplayError(f"{field} must be finite and nonnegative")
    with localcontext() as context:
        context.prec = 100
        scaled = amount * MONEY_MICROS_PER_QUOTE
        integral = int(scaled.to_integral_value(rounding=ROUND_HALF_EVEN))
    return _integer(
        integral, field, minimum=minimum, maximum=MAX_LEDGER_MICROS,
    )


def quote_price_fields(value: object, field: str = "price") -> dict[str, int]:
    """Return canonical price fields, plus the legacy field only when exact."""
    units = quote_price_units(value, field)
    result = {"price_quote_units_per_token": units}
    if units % PRICE_UNITS_PER_MICRO == 0:
        result["price_micros_per_token"] = units // PRICE_UNITS_PER_MICRO
    return result


def default_evidence_policy() -> dict[str, Any]:
    """Conservative defaults; small fixture samples remain exploratory."""
    return {
        "schema": EVIDENCE_POLICY_SCHEMA,
        "minimum_independent_episodes": 20,
        "minimum_coverage_ppm": 800_000,
        "minimum_improvement_micros": 1,
        "maximum_drawdown_ppm": 300_000,
        "minimum_stability_groups": 3,
        "require_nonnegative_adverse_difference": True,
        "uncertainty_minimum_independent_episodes": 30,
        "purpose": "draft eligibility only; never live-readiness or profitability proof",
    }


def validate_policy(policy: dict[str, Any]) -> dict[str, Any]:
    value = _canonical(policy, "policy")
    if value.get("schema") != POLICY_SCHEMA:
        raise PortfolioReplayError(f"policy.schema must be {POLICY_SCHEMA}")
    if value.get("family") != "ladder-v1":
        raise PortfolioReplayError("Only the complete ladder-v1 replay family is supported")
    if value.get("capital_unit") != "quote-micros":
        raise PortfolioReplayError("Replay capital_unit must be quote-micros")
    _identifier(value.get("quote_currency"), "policy.quote_currency")
    for field, minimum in (
        ("starting_cash_micros", 1), ("order_micros", 1), ("max_positions", 1),
        ("submission_delay_seconds", 0), ("entry_expiry_seconds", 1),
        ("max_quote_age_seconds", 0), ("max_hold_seconds", 1),
        ("stale_exit_seconds", 1), ("trailing_drawdown_ppm", 0),
    ):
        maximum = MAX_LEDGER_MICROS if field in (
            "starting_cash_micros", "order_micros",
        ) else None
        _integer(value.get(field), f"policy.{field}", minimum=minimum, maximum=maximum)
    if value["entry_expiry_seconds"] <= value["submission_delay_seconds"]:
        raise PortfolioReplayError("entry expiry must exceed submission delay")
    stop = value.get("stop_loss_ppm")
    if isinstance(stop, bool) or not isinstance(stop, int) or not -PPM < stop < 0:
        raise PortfolioReplayError("policy.stop_loss_ppm must be between -1000000 and 0")
    if value["trailing_drawdown_ppm"] >= PPM:
        raise PortfolioReplayError("policy.trailing_drawdown_ppm must be below 1000000")
    routes = value.get("supported_routes")
    if not isinstance(routes, list) or not routes or any(route not in ("curve", "amm") for route in routes):
        raise PortfolioReplayError("policy.supported_routes must contain curve and/or amm")
    if len(routes) != len(set(routes)):
        raise PortfolioReplayError("policy.supported_routes must be unique")
    targets = value.get("targets")
    if not isinstance(targets, list):
        raise PortfolioReplayError("policy.targets must be a list")
    previous = 0
    total = 0
    for index, target in enumerate(targets):
        if not isinstance(target, dict) or set(target) != {
            "market_cap_micros", "initial_fraction_ppm"
        }:
            raise PortfolioReplayError("Each target needs market_cap_micros and initial_fraction_ppm")
        cap = _integer(target["market_cap_micros"], f"policy.targets[{index}].market_cap_micros", minimum=1)
        fraction = _integer(target["initial_fraction_ppm"], f"policy.targets[{index}].initial_fraction_ppm", minimum=1)
        if cap <= previous or fraction > PPM:
            raise PortfolioReplayError("Targets must have rising caps and bounded fractions")
        previous = cap
        total += fraction
    if total > PPM:
        raise PortfolioReplayError("Target fractions cannot exceed the initial position")
    return value


def validate_costs(costs: dict[str, Any]) -> dict[str, Any]:
    value = _canonical(costs, "costs")
    if value.get("schema") != COST_SCHEMA:
        raise PortfolioReplayError(f"costs.schema must be {COST_SCHEMA}")
    for field in ("fee_ppm", "spread_ppm", "slippage_ppm", "maximum_impact_ppm"):
        amount = _integer(value.get(field), f"costs.{field}")
        if amount >= PPM:
            raise PortfolioReplayError(f"costs.{field} must be below {PPM}")
    _integer(value.get("impact_ppm_at_full_liquidity"), "costs.impact_ppm_at_full_liquidity")
    value.setdefault("entry_rent_micros", 0)
    for field in (
        "fixed_fee_micros", "entry_rent_micros",
        "failed_entry_fee_micros", "failed_exit_fee_micros",
    ):
        _integer(value.get(field), f"costs.{field}", maximum=MAX_LEDGER_MICROS)
    return value


def validate_execution(execution: dict[str, Any]) -> dict[str, Any]:
    value = _canonical(execution, "execution")
    if value.get("schema") != EXECUTION_SCHEMA:
        raise PortfolioReplayError(f"execution.schema must be {EXECUTION_SCHEMA}")
    for field in ("failed_entry_signal_ids", "unavailable_exit_quote_ids"):
        rows = value.get(field)
        if not isinstance(rows, list) or any(not isinstance(item, str) or not item for item in rows):
            raise PortfolioReplayError(f"execution.{field} must be a list of IDs")
        if len(rows) != len(set(rows)):
            raise PortfolioReplayError(f"execution.{field} must not contain duplicates")
    fills = value.get("exit_fill_ppm_by_quote_id")
    if not isinstance(fills, dict):
        raise PortfolioReplayError("execution.exit_fill_ppm_by_quote_id must be an object")
    for quote_id, fraction in fills.items():
        _identifier(quote_id, "execution.exit_fill_ppm_by_quote_id key")
        amount = _integer(fraction, "execution.exit_fill_ppm_by_quote_id value", minimum=1)
        if amount > PPM:
            raise PortfolioReplayError("Exit fill fractions cannot exceed 1000000 ppm")
    model = value.get("entry_failure_model", {"method": "none"})
    if not isinstance(model, dict) or model.get("method") not in (
        "none", "sha256-threshold-v1",
    ):
        raise PortfolioReplayError("execution.entry_failure_model is invalid")
    if model["method"] == "none":
        if set(model) != {"method"}:
            raise PortfolioReplayError("The none entry failure model accepts no parameters")
    else:
        if set(model) != {"method", "seed", "failure_ppm"}:
            raise PortfolioReplayError("The SHA-256 entry failure model is incomplete")
        _identifier(model.get("seed"), "execution.entry_failure_model.seed")
        failure_ppm = _integer(
            model.get("failure_ppm"),
            "execution.entry_failure_model.failure_ppm",
        )
        if failure_ppm > PPM:
            raise PortfolioReplayError("Entry failure probability cannot exceed 1000000 ppm")
    value["entry_failure_model"] = model
    return value


def _event(value: dict[str, Any]) -> dict[str, Any]:
    event = _canonical(value, "event")
    ident = _identifier(event.get("id"), "event.id")
    kind = event.get("kind")
    if kind not in ("quote", "entry_signal", "exit_signal", "clock"):
        raise PortfolioReplayError(f"Unsupported event kind for {ident}")
    occurred = _number(event.get("event_at"), "event.event_at")
    known = _number(event.get("first_known_at"), "event.first_known_at")
    if known < occurred:
        raise PortfolioReplayError("Event cannot be known before it occurred")
    if kind == "clock":
        return event
    _identifier(event.get("mint"), "event.mint")
    if kind == "quote":
        _identifier(event.get("quote_currency"), "quote.quote_currency")
        if event.get("route") not in ("curve", "amm"):
            raise PortfolioReplayError("Quote route must be curve or amm")
        _identifier(event.get("pool"), "quote.pool")
        _integer(event.get("decimals"), "quote.decimals")
        if event["decimals"] > 18:
            raise PortfolioReplayError("Quote decimals above 18 are unsupported")
        price_units = event.get("price_quote_units_per_token")
        legacy_price = event.get("price_micros_per_token")
        if price_units is None and legacy_price is None:
            raise PortfolioReplayError("Quote requires an explicit token price")
        if price_units is None:
            legacy_price = _integer(
                legacy_price, "quote.price_micros_per_token", minimum=1,
                maximum=MAX_LEDGER_MICROS,
            )
            price_units = legacy_price * PRICE_UNITS_PER_MICRO
            event["price_quote_units_per_token"] = price_units
        else:
            price_units = _integer(
                price_units, "quote.price_quote_units_per_token", minimum=1,
                maximum=MAX_PRICE_UNITS_PER_TOKEN,
            )
            if legacy_price is not None:
                legacy_price = _integer(
                    legacy_price, "quote.price_micros_per_token", minimum=1,
                    maximum=MAX_LEDGER_MICROS,
                )
                if legacy_price * PRICE_UNITS_PER_MICRO != price_units:
                    raise PortfolioReplayError("Quote price fields disagree")
        for field in ("market_cap_micros", "liquidity_micros"):
            _integer(
                event.get(field), f"quote.{field}", minimum=1,
                maximum=MAX_LEDGER_MICROS,
            )
        basis = event.get("source_time_basis")
        source = event.get("source_at")
        if basis is None and source is None:
            basis = "legacy-event-at"
            source = occurred
            event["source_time_basis"] = basis
            event["source_at"] = source
        elif basis not in _SOURCE_TIME_BASES:
            raise PortfolioReplayError("Quote source_time_basis is invalid")
        if basis == "unknown":
            if source is not None:
                raise PortfolioReplayError("Unknown quote source time cannot carry source_at")
        else:
            source = _number(source, "quote.source_at")
            if source > known:
                raise PortfolioReplayError("Quote source time cannot be after first-known time")
            event["source_at"] = source
        migration = event.get("migration_from_pool")
        if migration is not None:
            _identifier(migration, "quote.migration_from_pool")
    elif kind == "entry_signal":
        evidence = event.get("evidence_ids")
        if not isinstance(evidence, list) or not evidence:
            raise PortfolioReplayError("Entry signals require first-known evidence IDs")
        if any(not isinstance(item, str) or not item for item in evidence):
            raise PortfolioReplayError("Entry signal evidence IDs must be text")
        _identifier(event.get("episode_id"), "entry_signal.episode_id")
        _identifier(event.get("dependence_group"), "entry_signal.dependence_group")
        if event.get("split") not in ("train", "validation", "holdout", "paper"):
            raise PortfolioReplayError("Entry signal split is invalid")
    else:
        fraction = _integer(event.get("fraction_ppm", PPM), "exit_signal.fraction_ppm", minimum=1)
        if fraction > PPM:
            raise PortfolioReplayError("Exit signal fraction cannot exceed 1000000 ppm")
        _identifier(event.get("reason", "explicit_exit"), "exit_signal.reason")
    return event


def _event_key(event: dict[str, Any]) -> tuple[float, float, int, str, str]:
    priority = {"quote": 0, "exit_signal": 1, "entry_signal": 2, "clock": 3}
    return (
        float(event["first_known_at"]), float(event["event_at"]), priority[event["kind"]],
        str(event.get("mint", "")), event["id"],
    )


def _cost_line(amount: int, ppm: int) -> int:
    return amount * ppm // PPM


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _execution_fails_entry(execution: dict[str, Any], signal_id: str) -> bool:
    if signal_id in execution["failed_entry_signal_ids"]:
        return True
    model = execution["entry_failure_model"]
    if model["method"] == "none" or model["failure_ppm"] == 0:
        return False
    digest = hashlib.sha256(
        f"{model['seed']}\0{signal_id}".encode("utf-8")
    ).digest()
    sample = int.from_bytes(digest, "big")
    return sample * PPM < model["failure_ppm"] * (1 << 256)


def _bounded_equity_curve(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep an exact, deterministic display curve while metrics use all points."""
    if len(points) <= MAX_REPORTED_EQUITY_POINTS:
        return copy.deepcopy(points)
    mandatory = {
        0,
        len(points) - 1,
        min(range(len(points)), key=lambda index: points[index]["conservative_equity_micros"]),
        max(range(len(points)), key=lambda index: points[index]["conservative_equity_micros"]),
        max(range(len(points)), key=lambda index: points[index]["unknown_basis_micros"]),
    }
    remaining = [index for index in range(len(points)) if index not in mandatory]
    needed = MAX_REPORTED_EQUITY_POINTS - len(mandatory)
    if needed == 1:
        mandatory.add(remaining[len(remaining) // 2])
    elif needed > 1:
        mandatory.update(
            remaining[offset * (len(remaining) - 1) // (needed - 1)]
            for offset in range(needed)
        )
    return [copy.deepcopy(points[index]) for index in sorted(mandatory)]


class PortfolioReplay:
    """Incremental event-ordered ledger used by both batch and paper evaluation."""

    def __init__(
        self,
        policy: dict[str, Any],
        costs: dict[str, Any],
        execution: dict[str, Any],
        *,
        checkpoint: dict[str, Any] | None = None,
    ):
        self.policy = validate_policy(policy)
        self.costs = validate_costs(costs)
        self.execution = validate_execution(execution)
        if checkpoint is None:
            self.state = {
                "cash_micros": self.policy["starting_cash_micros"],
                "reserved_micros": 0,
                "positions": {}, "pending_entries": {}, "pending_exits": {}, "quotes": {},
                "processed": {}, "evidence_ids": [], "quote_evidence": {},
                "executable_quote_ids": [], "orders": [], "fills": [],
                "cash_ledger": [], "position_ledger": [], "equity_curve": [],
                "exclusions": [], "opportunities": [], "costs": {
                    "fees_micros": 0, "spread_micros": 0, "slippage_micros": 0,
                    "impact_micros": 0, "fixed_micros": 0, "rent_micros": 0,
                    "failed_micros": 0,
                },
                "realized_pnl_micros": 0, "exposure_micros_seconds": 0,
                "last_known_at": None, "peak_conservative_equity_micros": self.policy["starting_cash_micros"],
                "maximum_drawdown_ppm": 0, "cancelled": False,
            }
        else:
            value = _canonical(checkpoint, "checkpoint")
            if value.get("schema") != INPUT_SCHEMA or value.get("engine_version") != ENGINE_VERSION:
                raise PortfolioReplayError("Unsupported replay checkpoint")
            checkpoint_costs = value.get("costs")
            if isinstance(checkpoint_costs, dict):
                checkpoint_costs.setdefault("entry_rent_micros", 0)
            if (value.get("policy") != self.policy or checkpoint_costs != self.costs
                    or value.get("execution") != self.execution):
                raise PortfolioReplayError("Replay checkpoint assumptions changed")
            self.state = copy.deepcopy(value["state"])
            self.state["costs"].setdefault("rent_micros", 0)
            self._assert_invariants()

    def checkpoint(self) -> dict[str, Any]:
        return {
            "schema": INPUT_SCHEMA,
            "engine_version": ENGINE_VERSION,
            "policy": copy.deepcopy(self.policy),
            "costs": copy.deepcopy(self.costs),
            "execution": copy.deepcopy(self.execution),
            "state": copy.deepcopy(self.state),
        }

    def apply(self, raw_event: dict[str, Any]) -> list[dict[str, Any]]:
        event = _event(raw_event)
        digest = content_hash(event)
        prior = self.state["processed"].get(event["id"])
        if prior is not None:
            if prior != digest:
                raise PortfolioReplayError("An observation ID was reused with changed content")
            return []
        known_at = float(event["first_known_at"])
        last_at = self.state["last_known_at"]
        if last_at is not None and known_at < float(last_at):
            raise PortfolioReplayError("Incremental replay cannot borrow future-known information")
        before = copy.deepcopy(self.state)
        output: list[dict[str, Any]] = []
        try:
            self._accrue_exposure(known_at)
            # Intent is valid strictly before expires_at.  Expiry is processed
            # before every event that could expose a quote to a pending order.
            self._expire_entries(known_at, output)
            kind = event["kind"]
            if kind == "quote":
                self.state["quote_evidence"][event["id"]] = copy.deepcopy(event)
                output.extend(self._quote(event))
            elif kind == "entry_signal":
                output.extend(self._entry_signal(event))
            elif kind == "exit_signal":
                output.extend(self._exit_signal(event))
            else:
                output.extend(self._clock(event))
            self.state["processed"][event["id"]] = digest
            if kind == "quote":
                self.state["evidence_ids"].append(event["id"])
            self.state["last_known_at"] = known_at
            self._record_equity(known_at)
            self._assert_invariants()
        except Exception:
            self.state = before
            raise
        return output

    def _accrue_exposure(self, known_at: float) -> None:
        previous = self.state["last_known_at"]
        if previous is None:
            return
        elapsed = max(0.0, known_at - float(previous))
        deployed = sum(position["remaining_basis_micros"] for position in self.state["positions"].values())
        self.state["exposure_micros_seconds"] += round(deployed * elapsed)

    def _cash(self, at: float, reason: str, delta: int, *, reference: str) -> None:
        if self.state["cash_micros"] + delta < 0:
            raise PortfolioReplayError("Cash ledger would become negative")
        self.state["cash_micros"] += delta
        self.state["cash_ledger"].append({
            "at": at, "reason": reason, "delta_micros": delta,
            "cash_micros": self.state["cash_micros"],
            "reserved_micros": self.state["reserved_micros"],
            "spendable_micros": self.state["cash_micros"] - self.state["reserved_micros"],
            "reference": reference,
        })

    def _quote(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        mint = event["mint"]
        now = float(event["first_known_at"])
        if event["quote_currency"] != self.policy["quote_currency"]:
            self.state["exclusions"].append({
                "event_id": event["id"], "mint": mint,
                "reason": "quote_currency_or_fx_basis_mismatch",
            })
            return []
        fresh, freshness_reason, market_age = self._quote_freshness(event, now)
        if not fresh:
            self.state["exclusions"].append({
                "event_id": event["id"], "mint": mint,
                "reason": freshness_reason,
                "source_time_basis": event["source_time_basis"],
                "market_age_seconds": market_age,
            })
            position = self.state["positions"].get(mint)
            previous = self.state["quotes"].get(mint)
            if (position is not None and
                    (previous is None or not self._fresh(previous, now))):
                position["valuation_status"] = "unknown_stale_or_unverified_quote"
            return []
        previous = self.state["quotes"].get(mint)
        if previous is not None and (
            event["source_at"], event["event_at"], event["id"]
        ) <= (previous["source_at"], previous["event_at"], previous["id"]):
            self.state["exclusions"].append({
                "event_id": event["id"], "mint": mint, "reason": "late_or_replayed_quote",
            })
            return []
        position = self.state["positions"].get(mint)
        if position is not None and event["decimals"] != position["decimals"]:
            self.state["exclusions"].append({
                "event_id": event["id"], "mint": mint, "reason": "route_or_decimal_mismatch",
            })
            position["valuation_status"] = "unknown_decimal_mismatch"
            return []
        if position is not None and (
            event["route"], event["pool"]
        ) != (position["route"], position["pool"]):
            if event.get("migration_from_pool") != position["pool"]:
                self.state["exclusions"].append({
                    "event_id": event["id"], "mint": mint,
                    "reason": "unsupported_route_or_pool_change",
                })
                position["valuation_status"] = "unknown_route_change"
                return []
            position["route"] = event["route"]
            position["pool"] = event["pool"]
            self.state["position_ledger"].append({
                "at": event["first_known_at"], "mint": mint, "action": "route_migration",
                "route": event["route"], "pool": event["pool"], "quote_id": event["id"],
            })
        self.state["quotes"][mint] = copy.deepcopy(event)
        self.state["executable_quote_ids"].append(event["id"])
        if position is not None:
            position["peak_price_quote_units_per_token"] = max(
                position["peak_price_quote_units_per_token"],
                event["price_quote_units_per_token"],
            )
            if "peak_price_micros_per_token" in position and "price_micros_per_token" in event:
                position["peak_price_micros_per_token"] = max(
                    position["peak_price_micros_per_token"],
                    event["price_micros_per_token"],
                )
            position["valuation_status"] = "priced"
        output = []
        output.extend(self._fill_entry(mint, event, now))
        output.extend(self._fill_exit(mint, event, now))
        output.extend(self._automatic_exit(mint, event))
        return output

    def _entry_signal(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        evidence = set(event["evidence_ids"])
        if not evidence <= set(self.state["evidence_ids"]):
            raise PortfolioReplayError("Entry decision references evidence not yet first-known")
        mint = event["mint"]
        opportunity = {
            "signal_id": event["id"], "mint": mint, "episode_id": event["episode_id"],
            "dependence_group": event["dependence_group"], "split": event["split"],
            "at": event["first_known_at"], "status": "observed",
        }
        self.state["opportunities"].append(opportunity)
        evidence_quotes = [
            self.state["quote_evidence"].get(ident) for ident in event["evidence_ids"]
        ]
        executable_ids = set(self.state["executable_quote_ids"])
        if not any(
            quote is not None
            and quote["id"] in executable_ids
            and quote["mint"] == mint
            and self._fresh(quote, float(event["first_known_at"]))
            for quote in evidence_quotes
        ):
            opportunity["status"] = "skipped_unexecutable_evidence"
            self.state["exclusions"].append({
                "event_id": event["id"], "mint": mint,
                "reason": "entry_signal_has_no_fresh_executable_quote_evidence",
            })
            return []
        if mint in self.state["positions"] or mint in self.state["pending_entries"]:
            opportunity["status"] = "skipped_existing_exposure"
            return []
        if len(self.state["positions"]) + len(self.state["pending_entries"]) >= self.policy["max_positions"]:
            opportunity["status"] = "skipped_capacity"
            return []
        amount = self.policy["order_micros"]
        if self.state["cash_micros"] - self.state["reserved_micros"] < amount:
            opportunity["status"] = "skipped_insufficient_cash"
            return []
        order = {
            "id": f"entry:{event['id']}", "side": "entry", "mint": mint,
            "signal_id": event["id"], "created_at": event["first_known_at"],
            "eligible_at": event["first_known_at"] + self.policy["submission_delay_seconds"],
            "expires_at": event["first_known_at"] + self.policy["entry_expiry_seconds"],
            "reserved_micros": amount, "status": "pending", "episode_id": event["episode_id"],
            "dependence_group": event["dependence_group"], "split": event["split"],
        }
        self.state["reserved_micros"] += amount
        self.state["pending_entries"][mint] = order
        self.state["orders"].append(copy.deepcopy(order))
        opportunity["status"] = "reserved"
        quote = self.state["quotes"].get(mint)
        return self._fill_entry(mint, quote, float(event["first_known_at"])) if quote is not None else []

    def _entry_costs(self, amount: int, quote: dict[str, Any]) -> dict[str, int]:
        fixed = min(amount, self.costs["fixed_fee_micros"])
        rent = min(max(0, amount - fixed), self.costs["entry_rent_micros"])
        variable_base = max(0, amount - fixed - rent)
        liquidity = quote["liquidity_micros"]
        impact = min(
            self.costs["maximum_impact_ppm"],
            self.costs["impact_ppm_at_full_liquidity"] * variable_base // max(1, liquidity),
        )
        return {
            "fees_micros": _cost_line(variable_base, self.costs["fee_ppm"]),
            "spread_micros": _cost_line(variable_base, self.costs["spread_ppm"]),
            "slippage_micros": _cost_line(variable_base, self.costs["slippage_ppm"]),
            "impact_micros": _cost_line(variable_base, impact),
            "fixed_micros": fixed,
            "rent_micros": rent,
        }

    def _fill_entry(
        self, mint: str, quote: dict[str, Any] | None, now: float,
    ) -> list[dict[str, Any]]:
        order = self.state["pending_entries"].get(mint)
        if order is None or quote is None:
            return []
        if now >= order["expires_at"]:
            output: list[dict[str, Any]] = []
            self._expire_entries(now, output)
            return output
        if now < order["eligible_at"] or not self._fresh(quote, now):
            return []
        self.state["reserved_micros"] -= order["reserved_micros"]
        del self.state["pending_entries"][mint]
        if quote["route"] not in self.policy["supported_routes"]:
            order["status"] = "rejected_unsupported_route"
            self._update_order(order)
            return []
        if _execution_fails_entry(self.execution, order["signal_id"]):
            fee = min(self.state["cash_micros"], self.costs["failed_entry_fee_micros"])
            self._cash(now, "failed_entry_fee", -fee, reference=order["id"])
            self.state["costs"]["failed_micros"] += fee
            order["status"] = "failed_fee_only"
            self._update_order(order)
            self._opportunity(order["signal_id"], "failed_entry")
            return [{
                "action": "failed_entry", "mint": mint, "fee_micros": fee,
                "signal_id": order["signal_id"],
            }]
        amount = order["reserved_micros"]
        costs = self._entry_costs(amount, quote)
        acquired_value = amount - sum(costs.values())
        atoms = (
            acquired_value * PRICE_UNITS_PER_MICRO * (10 ** quote["decimals"])
            // quote["price_quote_units_per_token"]
        )
        if atoms <= 0:
            order["status"] = "rejected_zero_atoms"
            self._update_order(order)
            self._opportunity(order["signal_id"], "skipped_unexecutable")
            return []
        if atoms > MAX_TOKEN_ATOMS:
            order["status"] = "rejected_quantity_overflow"
            self._update_order(order)
            self._opportunity(order["signal_id"], "skipped_unexecutable")
            self.state["exclusions"].append({
                "event_id": order["signal_id"], "mint": mint,
                "reason": "entry_quantity_exceeds_supported_bound",
            })
            return []
        quote_value = _ceil_div(
            atoms * quote["price_quote_units_per_token"],
            (10 ** quote["decimals"]) * PRICE_UNITS_PER_MICRO,
        )
        debit = quote_value + sum(costs.values())
        if debit > amount:
            raise PortfolioReplayError("Rounded entry debit exceeds its reserved budget")
        self._cash(now, "modeled_entry", -debit, reference=order["id"])
        self._add_costs(costs)
        position_id = f"position:{order['signal_id']}"
        position = {
            "id": position_id, "mint": mint, "decimals": quote["decimals"],
            "quantity_atoms": atoms, "initial_atoms": atoms,
            "remaining_basis_micros": debit, "entry_at": now,
            "entry_quote_id": quote["id"],
            "entry_price_quote_units_per_token": quote["price_quote_units_per_token"],
            "route": quote["route"], "pool": quote["pool"],
            "peak_price_quote_units_per_token": quote["price_quote_units_per_token"],
            "sold_target_indexes": [], "scheduled_target_indexes": [],
            "realized_pnl_micros": 0, "valuation_status": "priced",
            "episode_id": order["episode_id"], "dependence_group": order["dependence_group"],
            "split": order["split"], "signal_id": order["signal_id"],
        }
        if "price_micros_per_token" in quote:
            position["entry_price_micros_per_token"] = quote["price_micros_per_token"]
            position["peak_price_micros_per_token"] = quote["price_micros_per_token"]
        self.state["positions"][mint] = position
        order["status"] = "filled"
        order["fill_quote_id"] = quote["id"]
        self._update_order(order)
        self._opportunity(order["signal_id"], "filled")
        fill = {
            "id": f"fill:{order['id']}:{quote['id']}", "side": "entry", "at": now,
            "mint": mint, "position_id": position_id, "quantity_atoms": atoms,
            "cash_delta_micros": -debit, "fair_value_micros": quote_value,
            "reserved_budget_micros": amount,
            "unused_budget_micros": amount - debit,
            "costs": costs, "quote_id": quote["id"], "modeled": True,
            "price_quote_units_per_token": quote["price_quote_units_per_token"],
            "episode_id": order["episode_id"], "split": order["split"],
        }
        if "price_micros_per_token" in quote:
            fill["price_micros_per_token"] = quote["price_micros_per_token"]
        self.state["fills"].append(fill)
        self.state["position_ledger"].append({
            "at": now, "mint": mint, "action": "opened", "quantity_delta_atoms": atoms,
            "quantity_atoms": atoms, "basis_micros": debit, "position_id": position_id,
        })
        return [{"action": "fill_entry", **copy.deepcopy(fill)}]

    def _exit_signal(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        position = self.state["positions"].get(event["mint"])
        if position is None:
            self.state["exclusions"].append({
                "event_id": event["id"], "mint": event["mint"], "reason": "exit_without_position",
            })
            return []
        quantity = max(1, position["quantity_atoms"] * event.get("fraction_ppm", PPM) // PPM)
        self._schedule_exit(position, quantity, event["first_known_at"], event.get("reason", "explicit_exit"), event["id"])
        quote = self.state["quotes"].get(event["mint"])
        # A contemporaneous quote may fill an exit on the same observation.
        # Otherwise an exit intent waits for the next quote rather than
        # pretending that an older displayed price remained executable.
        if quote is None or float(quote["first_known_at"]) != float(event["first_known_at"]):
            return []
        return self._fill_exit(
            event["mint"], quote, float(event["first_known_at"]),
        )

    def _schedule_exit(
        self, position: dict[str, Any], quantity: int, at: float, reason: str,
        reference: str, *, target_index: int | None = None,
    ) -> None:
        if position["mint"] in self.state["pending_exits"]:
            return
        order = {
            "id": f"exit:{position['id']}:{reference}", "side": "exit", "mint": position["mint"],
            "position_id": position["id"], "created_at": at,
            "eligible_at": at + self.policy["submission_delay_seconds"],
            "quantity_atoms": min(quantity, position["quantity_atoms"]), "status": "pending",
            "reason": reason, "target_index": target_index,
        }
        self.state["pending_exits"][position["mint"]] = order
        self.state["orders"].append(copy.deepcopy(order))

    def _automatic_exit(self, mint: str, quote: dict[str, Any]) -> list[dict[str, Any]]:
        position = self.state["positions"].get(mint)
        if position is None or mint in self.state["pending_exits"]:
            return []
        now = float(quote["first_known_at"])
        estimate = self._exit_estimate(position["quantity_atoms"], quote)
        change_ppm = (
            (estimate["proceeds_micros"] - position["remaining_basis_micros"]) * PPM
            // max(1, position["remaining_basis_micros"])
        )
        reason = None
        if now - position["entry_at"] >= self.policy["max_hold_seconds"]:
            reason = "max_hold"
        elif change_ppm <= self.policy["stop_loss_ppm"]:
            reason = "stop_loss"
        elif position["sold_target_indexes"] and quote["price_quote_units_per_token"] * PPM <= (
            position["peak_price_quote_units_per_token"]
            * (PPM - self.policy["trailing_drawdown_ppm"])
        ):
            reason = "trailing_exit"
        if reason is not None:
            self._schedule_exit(position, position["quantity_atoms"], now, reason, quote["id"])
            return self._fill_exit(mint, quote, now)
        for index, target in enumerate(self.policy["targets"]):
            if index in position["sold_target_indexes"] or index in position["scheduled_target_indexes"]:
                continue
            if quote["market_cap_micros"] >= target["market_cap_micros"]:
                quantity = max(1, position["initial_atoms"] * target["initial_fraction_ppm"] // PPM)
                position["scheduled_target_indexes"].append(index)
                self._schedule_exit(position, quantity, now, "target", quote["id"], target_index=index)
                break
        return self._fill_exit(mint, quote, now)

    def _exit_estimate(self, quantity: int, quote: dict[str, Any]) -> dict[str, Any]:
        gross = (
            quantity * quote["price_quote_units_per_token"]
            // ((10 ** quote["decimals"]) * PRICE_UNITS_PER_MICRO)
        )
        impact_ppm = min(
            self.costs["maximum_impact_ppm"],
            self.costs["impact_ppm_at_full_liquidity"] * gross // max(1, quote["liquidity_micros"]),
        )
        costs = {
            "fees_micros": _cost_line(gross, self.costs["fee_ppm"]),
            "spread_micros": _cost_line(gross, self.costs["spread_ppm"]),
            "slippage_micros": _cost_line(gross, self.costs["slippage_ppm"]),
            "impact_micros": _cost_line(gross, impact_ppm),
            "fixed_micros": min(gross, self.costs["fixed_fee_micros"]),
            "rent_micros": 0,
        }
        return {"gross_micros": gross, "proceeds_micros": max(0, gross - sum(costs.values())), "costs": costs}

    def _fill_exit(
        self, mint: str, quote: dict[str, Any] | None, now: float,
    ) -> list[dict[str, Any]]:
        order = self.state["pending_exits"].get(mint)
        position = self.state["positions"].get(mint)
        if order is None or position is None or quote is None:
            return []
        if now < order["eligible_at"] or not self._fresh(quote, now):
            return []
        if quote["id"] in self.execution["unavailable_exit_quote_ids"]:
            fee = min(self.state["cash_micros"], self.costs["failed_exit_fee_micros"])
            self._cash(now, "failed_exit_fee", -fee, reference=order["id"])
            self.state["costs"]["failed_micros"] += fee
            self.state["position_ledger"].append({
                "at": now, "mint": mint, "action": "exit_unavailable", "quote_id": quote["id"],
                "quantity_atoms": position["quantity_atoms"], "position_id": position["id"],
            })
            return [{"action": "exit_unavailable", "mint": mint, "fee_micros": fee}]
        requested = min(order["quantity_atoms"], position["quantity_atoms"])
        fraction = self.execution["exit_fill_ppm_by_quote_id"].get(quote["id"], PPM)
        sold = max(1, requested * fraction // PPM)
        sold = min(sold, requested)
        estimate = self._exit_estimate(sold, quote)
        if estimate["gross_micros"] <= 0:
            self.state["exclusions"].append({
                "event_id": quote["id"], "mint": mint,
                "reason": "exit_value_below_quote-micro_precision",
                "quantity_atoms": sold,
            })
            position["valuation_status"] = "unknown_below_currency_precision"
            return []
        before_quantity = position["quantity_atoms"]
        if sold == before_quantity:
            basis = position["remaining_basis_micros"]
        else:
            basis = position["remaining_basis_micros"] * sold // before_quantity
        proceeds = estimate["proceeds_micros"]
        self._cash(now, "modeled_exit", proceeds, reference=order["id"])
        self._add_costs(estimate["costs"])
        pnl = proceeds - basis
        self.state["realized_pnl_micros"] += pnl
        position["quantity_atoms"] -= sold
        position["remaining_basis_micros"] -= basis
        position["realized_pnl_micros"] += pnl
        if order["target_index"] is not None and sold == requested:
            index = order["target_index"]
            if index not in position["sold_target_indexes"]:
                position["sold_target_indexes"].append(index)
        fill = {
            "id": f"fill:{order['id']}:{quote['id']}:{before_quantity}", "side": "exit",
            "at": now, "mint": mint, "position_id": position["id"],
            "quantity_atoms": sold, "cash_delta_micros": proceeds,
            "gross_micros": estimate["gross_micros"], "basis_micros": basis,
            "realized_pnl_micros": pnl, "costs": estimate["costs"],
            "quote_id": quote["id"],
            "price_quote_units_per_token": quote["price_quote_units_per_token"],
            "reason": order["reason"], "modeled": True,
            "episode_id": position["episode_id"], "split": position["split"],
        }
        if "price_micros_per_token" in quote:
            fill["price_micros_per_token"] = quote["price_micros_per_token"]
        self.state["fills"].append(fill)
        self.state["position_ledger"].append({
            "at": now, "mint": mint, "action": "closed" if position["quantity_atoms"] == 0 else "reduced",
            "quantity_delta_atoms": -sold, "quantity_atoms": position["quantity_atoms"],
            "basis_micros": position["remaining_basis_micros"], "position_id": position["id"],
        })
        if sold == requested:
            del self.state["pending_exits"][mint]
        else:
            order["quantity_atoms"] -= sold
            order["status"] = "partially_filled"
            self._update_order(order)
        if position["quantity_atoms"] == 0:
            del self.state["positions"][mint]
            self.state["pending_exits"].pop(mint, None)
        return [{"action": "fill_exit", **copy.deepcopy(fill)}]

    def _clock(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        now = float(event["first_known_at"])
        for position in self.state["positions"].values():
            quote = self.state["quotes"].get(position["mint"])
            age = math.inf if quote is None else self._market_age(quote, now)
            if age >= self.policy["stale_exit_seconds"]:
                position["valuation_status"] = "unknown_stale_or_unavailable_exit"
                if position["mint"] not in self.state["pending_exits"]:
                    self._schedule_exit(
                        position, position["quantity_atoms"], now, "stale_exit",
                        event["id"],
                    )
            elif now - position["entry_at"] >= self.policy["max_hold_seconds"]:
                self._schedule_exit(position, position["quantity_atoms"], now, "max_hold", event["id"])
        return []

    @staticmethod
    def _market_age(quote: dict[str, Any], now: float) -> float:
        source = quote.get("source_at")
        return math.inf if source is None else now - float(source)

    def _quote_freshness(
        self, quote: dict[str, Any], now: float,
    ) -> tuple[bool, str | None, float | None]:
        if float(quote["first_known_at"]) > now:
            return False, "quote_not_yet_available", None
        if quote["source_time_basis"] not in _EXECUTABLE_SOURCE_TIME_BASES:
            return False, "unknown_or_unverified_source_time", None
        age = self._market_age(quote, now)
        if age < 0:
            return False, "quote_source_time_is_in_the_future", age
        if age > self.policy["max_quote_age_seconds"]:
            return False, "stale_quote", age
        return True, None, age

    def _fresh(self, quote: dict[str, Any], now: float) -> bool:
        return self._quote_freshness(quote, now)[0]

    def _expire_entries(self, now: float, output: list[dict[str, Any]]) -> None:
        for mint, order in list(self.state["pending_entries"].items()):
            if now < order["expires_at"]:
                continue
            self.state["reserved_micros"] -= order["reserved_micros"]
            del self.state["pending_entries"][mint]
            order["status"] = "expired_unfilled"
            self._update_order(order)
            self._opportunity(order["signal_id"], "skipped_expired")
            output.append({
                "action": "cancel_entry", "mint": mint,
                "reason": "expired_unfilled", "signal_id": order["signal_id"],
            })

    def _update_order(self, order: dict[str, Any]) -> None:
        for index in range(len(self.state["orders"]) - 1, -1, -1):
            if self.state["orders"][index]["id"] == order["id"]:
                self.state["orders"][index] = copy.deepcopy(order)
                return
        raise PortfolioReplayError("Order ledger identity was lost")

    def _opportunity(self, signal_id: str, status: str) -> None:
        for row in self.state["opportunities"]:
            if row["signal_id"] == signal_id:
                row["status"] = status
                return
        raise PortfolioReplayError("Opportunity identity was lost")

    def _add_costs(self, costs: dict[str, int]) -> None:
        for key, amount in costs.items():
            self.state["costs"][key] += amount

    def _position_mark(self, position: dict[str, Any], now: float) -> dict[str, Any]:
        quote = self.state["quotes"].get(position["mint"])
        if quote is None:
            return {
                "status": "unknown", "value_micros": None,
                "reason": "quote_unavailable",
            }
        fresh, reason, _age = self._quote_freshness(quote, now)
        if not fresh:
            return {"status": "unknown", "value_micros": None, "reason": reason}
        if position["valuation_status"] != "priced":
            return {
                "status": "unknown", "value_micros": None,
                "reason": position["valuation_status"],
            }
        estimate = self._exit_estimate(position["quantity_atoms"], quote)
        if estimate["gross_micros"] <= 0:
            return {
                "status": "unknown", "value_micros": None,
                "reason": "value_below_quote-micro_precision",
            }
        return {"status": "priced", "value_micros": estimate["proceeds_micros"], "reason": None}

    def _record_equity(self, now: float) -> None:
        known = self.state["cash_micros"]
        unknown_basis = 0
        marks = {}
        for mint, position in sorted(self.state["positions"].items()):
            mark = self._position_mark(position, now)
            marks[mint] = mark
            if mark["value_micros"] is None:
                unknown_basis += position["remaining_basis_micros"]
            else:
                known += mark["value_micros"]
        complete = unknown_basis == 0
        conservative = known
        peak = max(self.state["peak_conservative_equity_micros"], conservative)
        self.state["peak_conservative_equity_micros"] = peak
        drawdown = (peak - conservative) * PPM // peak if peak else 0
        self.state["maximum_drawdown_ppm"] = max(self.state["maximum_drawdown_ppm"], drawdown)
        self.state["equity_curve"].append({
            "at": now, "known_equity_micros": known,
            "conservative_equity_micros": conservative,
            "at_cost_upper_micros": known + unknown_basis,
            "valuation_complete": complete, "unknown_basis_micros": unknown_basis,
            "marks": marks,
        })

    def _assert_invariants(self) -> None:
        state = self.state
        if state["cash_micros"] < 0 or state["reserved_micros"] < 0:
            raise PortfolioReplayError("Cash or reservation became negative")
        if state["reserved_micros"] > state["cash_micros"]:
            raise PortfolioReplayError("Reservations exceed available cash")
        if state["reserved_micros"] != sum(
            order["reserved_micros"] for order in state["pending_entries"].values()
        ):
            raise PortfolioReplayError("Cash reservations do not reconcile")
        executable = state["executable_quote_ids"]
        if (len(executable) != len(set(executable))
                or not set(executable) <= set(state["quote_evidence"])):
            raise PortfolioReplayError("Executable quote evidence does not reconcile")
        bought: dict[str, int] = {}
        sold: dict[str, int] = {}
        for fill in state["fills"]:
            target = bought if fill["side"] == "entry" else sold
            target[fill["mint"]] = target.get(fill["mint"], 0) + fill["quantity_atoms"]
        for mint in set(bought) | set(sold) | set(state["positions"]):
            open_atoms = state["positions"].get(mint, {}).get("quantity_atoms", 0)
            if bought.get(mint, 0) - sold.get(mint, 0) != open_atoms or open_atoms < 0:
                raise PortfolioReplayError("Token atom ledger does not reconcile")
            if open_atoms > MAX_TOKEN_ATOMS:
                raise PortfolioReplayError("Token atom balance exceeds supported bound")
        cash_expected = self.policy["starting_cash_micros"] + sum(
            row["delta_micros"] for row in state["cash_ledger"]
        )
        if cash_expected != state["cash_micros"]:
            raise PortfolioReplayError("Cash ledger does not reconcile")

    def report(self) -> dict[str, Any]:
        self._assert_invariants()
        state = copy.deepcopy(self.state)
        full_equity_curve = state["equity_curve"]
        last = full_equity_curve[-1] if full_equity_curve else {
            "known_equity_micros": self.policy["starting_cash_micros"],
            "conservative_equity_micros": self.policy["starting_cash_micros"],
            "at_cost_upper_micros": self.policy["starting_cash_micros"],
            "valuation_complete": True, "unknown_basis_micros": 0,
        }
        started = full_equity_curve[0]["at"] if full_equity_curve else None
        ended = state["last_known_at"]
        span = max(0.0, float(ended) - float(started)) if started is not None and ended is not None else 0.0
        denominator = self.policy["starting_cash_micros"] * span
        known_unrealized = 0
        for mint, position in state["positions"].items():
            mark = last.get("marks", {}).get(mint, {})
            if mark.get("value_micros") is not None:
                known_unrealized += mark["value_micros"] - position["remaining_basis_micros"]
        opportunities = len(state["opportunities"])
        entered_positions = {
            fill["position_id"] for fill in state["fills"] if fill["side"] == "entry"
        }
        open_positions = {position["id"] for position in state["positions"].values()}
        completed = len(entered_positions - open_positions)
        closed_episodes = {
            fill["episode_id"] for fill in state["fills"]
            if fill["side"] == "exit" and fill["position_id"] not in open_positions
        }
        unresolved_statuses = {"observed", "reserved"}
        resolved = sum(
            row["status"] not in unresolved_statuses
            and (row["status"] != "filled" or row["episode_id"] in closed_episodes)
            for row in state["opportunities"]
        )
        coverage_ppm = resolved * PPM // opportunities if opportunities else 0
        core = {
            "schema": REPORT_SCHEMA,
            "engine_version": ENGINE_VERSION,
            "policy": copy.deepcopy(self.policy), "costs_assumption": copy.deepcopy(self.costs),
            "execution_assumption": copy.deepcopy(self.execution),
            "capital": {
                "unit": "quote-micros", "quote_currency": self.policy["quote_currency"],
                "starting_cash_micros": self.policy["starting_cash_micros"],
                "ending_cash_micros": state["cash_micros"],
                "reserved_micros": state["reserved_micros"],
            },
            "orders": state["orders"], "fills": state["fills"],
            "cash_ledger": state["cash_ledger"], "position_ledger": state["position_ledger"],
            "open_positions": state["positions"],
            "equity_curve": _bounded_equity_curve(full_equity_curve),
            "equity_curve_sampling": {
                "method": "deterministic-even-with-first-last-extrema",
                "metrics_use_all_points": True,
                "total_points": len(full_equity_curve),
                "retained_points": min(len(full_equity_curve), MAX_REPORTED_EQUITY_POINTS),
            },
            "cost_breakdown": state["costs"], "exclusions": state["exclusions"],
            "opportunities": state["opportunities"],
            "metrics": {
                "conservative_ending_equity_micros": last["conservative_equity_micros"],
                "at_cost_upper_equity_micros": last["at_cost_upper_micros"],
                "net_result_micros": last["conservative_equity_micros"] - self.policy["starting_cash_micros"],
                "realized_pnl_micros": state["realized_pnl_micros"],
                "known_unrealized_pnl_micros": known_unrealized,
                "unknown_unpriced_basis_micros": last["unknown_basis_micros"],
                "valuation_complete": last["valuation_complete"],
                "maximum_drawdown_ppm": state["maximum_drawdown_ppm"],
                "time_weighted_exposure_ppm": (
                    state["exposure_micros_seconds"] * PPM // denominator if denominator else 0
                ),
                "opportunities": opportunities, "resolved_opportunities": resolved,
                "completed_position_exits": completed,
                "coverage_ppm": coverage_ppm,
                "independent_episode_count": len({
                    row["episode_id"] for row in state["opportunities"]
                }),
                "dependence_group_count": len({
                    row["dependence_group"] for row in state["opportunities"]
                }),
            },
            "coverage": {
                "missing_values_are_zero": False,
                "unknown_positions_preserved": True,
                "conservative_equity_treats_unknown_exit_value_as_zero": True,
            },
            "semantics": {
                "availability_time": "first_known_at",
                "market_freshness_time": "source_at",
                "freshness_boundary": "market_age_seconds <= max_quote_age_seconds",
                "entry_expiry_boundary": "fill_time < expires_at; expires_at is expired",
                "cash_unit": "quote-micros (1e-6 quote currency)",
                "price_unit": "integer 1e-18 quote currency per whole token",
                "token_unit": "integer atoms at the quote-declared decimals",
                "entry_quantity_rounding": "floor",
                "entry_quote_debit_rounding": "ceiling to quote micros; unused reserved budget remains cash",
                "percentage_cost_rounding": "floor to quote micros",
                "exit_gross_and_credit_rounding": "floor to quote micros",
                "implicit_fx_conversion": False,
            },
            "funded_performance": False, "submission_enabled": False,
        }
        ident = content_hash(core)
        return {**core, "id": ident, "reproducibility_sha256": ident}


def replay_portfolio(
    events: Iterable[dict[str, Any]],
    *,
    policy: dict[str, Any],
    costs: dict[str, Any],
    execution: dict[str, Any],
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    normalized = [_event(event) for event in events]
    normalized.sort(key=_event_key)
    replay = PortfolioReplay(policy, costs, execution)
    for event in normalized:
        if cancelled():
            raise PortfolioReplayError("Portfolio replay cancelled; no complete report was produced")
        replay.apply(event)
    return replay.report()


def split_episodes(
    episodes: Iterable[dict[str, Any]], *, train_end_at: float,
    validation_end_at: float, holdout_end_at: float, embargo_seconds: float,
) -> list[dict[str, Any]]:
    """Assign outcome-complete episodes without crossing chronological boundaries."""
    train_end = _number(train_end_at, "train_end_at")
    validation_end = _number(validation_end_at, "validation_end_at")
    holdout_end = _number(holdout_end_at, "holdout_end_at")
    embargo = _number(embargo_seconds, "embargo_seconds")
    if not 0 <= embargo or not train_end < validation_end < holdout_end:
        raise PortfolioReplayError("Split boundaries or embargo are invalid")
    result = []
    seen = set()
    for raw in episodes:
        row = _canonical(raw, "episode")
        ident = _identifier(row.get("id"), "episode.id")
        if ident in seen:
            raise PortfolioReplayError("Episode IDs must be unique")
        seen.add(ident)
        entry = _number(row.get("entry_at"), "episode.entry_at")
        outcome = row.get("outcome_at")
        if outcome is None:
            split = "censored"
        else:
            outcome_at = _number(outcome, "episode.outcome_at")
            if outcome_at < entry:
                raise PortfolioReplayError("Episode outcome cannot precede its entry")
            if entry < train_end:
                split = "train" if outcome_at <= train_end - embargo else "embargoed"
            elif entry < validation_end:
                split = "validation" if outcome_at <= validation_end - embargo else "embargoed"
            elif entry <= holdout_end:
                split = "holdout" if outcome_at <= holdout_end else "censored"
            else:
                split = "outside"
        result.append({**row, "split": split, "dependence_group": row.get("dependence_group", row.get("mint"))})
    return sorted(result, key=lambda row: (row["entry_at"], row["id"]))


def build_holding_benchmark(quote_events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build an equal-opportunity first-quote-to-last-quote holding baseline.

    The benchmark enters each eligible mint at its first retained quote and exits
    at its last quote. It is omitted per mint unless there are at least two
    quotes with consistent currency/decimals and explicit pool-migration lineage.
    Capital/capacity constraints are still applied later by ``replay_portfolio``.
    """
    quotes = [_event(event) for event in quote_events]
    if any(event["kind"] != "quote" for event in quotes):
        raise PortfolioReplayError("Holding benchmark accepts quote events only")
    by_mint: dict[str, list[dict[str, Any]]] = {}
    for event in sorted(quotes, key=_event_key):
        by_mint.setdefault(event["mint"], []).append(event)
    events: list[dict[str, Any]] = []
    eligible = []
    excluded = []
    for mint, rows in sorted(by_mint.items()):
        reason = None
        if len(rows) < 2:
            reason = "needs_at_least_two_quotes"
        elif len({row["quote_currency"] for row in rows}) != 1:
            reason = "quote_currency_changed"
        elif len({row["decimals"] for row in rows}) != 1:
            reason = "token_decimals_changed"
        else:
            route_pool = (rows[0]["route"], rows[0]["pool"])
            for row in rows[1:]:
                current = (row["route"], row["pool"])
                if current != route_pool:
                    if row.get("migration_from_pool") != route_pool[1]:
                        reason = "pool_change_without_migration_lineage"
                        break
                    route_pool = current
        if reason is not None:
            excluded.append({"mint": mint, "reason": reason})
            continue
        eligible.append(mint)
        first = rows[0]
        last = rows[-1]
        events.extend(copy.deepcopy(rows))
        events.append({
            "id": f"holding-entry:{first['id']}", "kind": "entry_signal",
            "event_at": first["event_at"], "first_known_at": first["first_known_at"],
            "mint": mint, "evidence_ids": [first["id"]],
            "episode_id": f"holding:{mint}:{first['id']}",
            "dependence_group": mint, "split": "validation",
        })
        events.append({
            "id": f"holding-exit:{last['id']}", "kind": "exit_signal",
            "event_at": last["event_at"], "first_known_at": last["first_known_at"],
            "mint": mint, "fraction_ppm": PPM, "reason": "holding_period_end",
        })
    return {
        "definition": "enter first eligible retained quote; exit last eligible retained quote",
        "events": sorted(events, key=_event_key),
        "eligible_mints": eligible, "excluded_mints": excluded,
        "missing_values_are_zero": False,
    }


def _validate_evidence_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    value = default_evidence_policy() if policy is None else _canonical(policy, "evidence_policy")
    if value.get("schema") != EVIDENCE_POLICY_SCHEMA:
        raise PortfolioReplayError(f"evidence_policy.schema must be {EVIDENCE_POLICY_SCHEMA}")
    for field in (
        "minimum_independent_episodes", "minimum_coverage_ppm", "minimum_improvement_micros",
        "maximum_drawdown_ppm", "minimum_stability_groups",
        "uncertainty_minimum_independent_episodes",
    ):
        _integer(value.get(field), f"evidence_policy.{field}")
    if not isinstance(value.get("require_nonnegative_adverse_difference"), bool):
        raise PortfolioReplayError("Evidence policy adverse requirement must be boolean")
    return value


def _uncertainty(returns: list[int], policy: dict[str, Any]) -> dict[str, Any] | None:
    if len(returns) < policy["uncertainty_minimum_independent_episodes"] or len(returns) < 2:
        return None
    mean = statistics.fmean(returns)
    standard_error = statistics.stdev(returns) / math.sqrt(len(returns))
    return {
        "method": "normal-approximation-95-percent-interval",
        "sample_assumption": "episode returns supplied as independent by the registered experiment",
        "episodes": len(returns),
        "mean_micros": round(mean),
        "lower_micros": round(mean - 1.96 * standard_error),
        "upper_micros": round(mean + 1.96 * standard_error),
    }


def compare_portfolios(
    candidates: Iterable[dict[str, Any]], *, evidence_policy: dict[str, Any] | None = None,
    input_lineage: dict[str, Any], cancelled: Callable[[], bool] = lambda: False,
    evaluate_holdout: bool = True,
) -> dict[str, Any]:
    """Compare cash/current/variants/optional holding under identical lineage.

    Each candidate supplies frozen validation and holdout events plus base and
    adverse costs. Selection uses validation only; holdout is evaluated only for
    cash, current, holding, and the selected candidate.
    """
    policy = _validate_evidence_policy(evidence_policy)
    lineage = _canonical(input_lineage, "input_lineage")
    rows = [_canonical(candidate, "candidate") for candidate in candidates]
    if not rows:
        raise PortfolioReplayError("Comparison needs the current strategy")
    if len([row for row in rows if row.get("kind") == "variant"]) > 5:
        raise PortfolioReplayError("Comparison supports at most five bounded variants")
    current_rows = [row for row in rows if row.get("kind") == "current"]
    if len(current_rows) != 1:
        raise PortfolioReplayError("Comparison needs exactly one current strategy")
    identifiers = [_identifier(row.get("id"), "candidate.id") for row in rows]
    if len(identifiers) != len(set(identifiers)):
        raise PortfolioReplayError("Candidate IDs must be unique")
    validation = []
    for row in rows:
        if cancelled():
            raise PortfolioReplayError("Portfolio comparison cancelled")
        for field in ("events_validation", "events_holdout", "episode_returns_validation_micros"):
            if not isinstance(row.get(field), list):
                raise PortfolioReplayError(f"candidate.{field} must be a list")
        base = replay_portfolio(
            row["events_validation"], policy=row["policy"], costs=row["base_costs"],
            execution=row["execution"], cancelled=cancelled,
        )
        adverse = replay_portfolio(
            row["events_validation"], policy=row.get("adverse_policy", row["policy"]),
            costs=row["adverse_costs"],
            execution=row["execution"], cancelled=cancelled,
        )
        metrics = base["metrics"]
        blockers = []
        if metrics["independent_episode_count"] < policy["minimum_independent_episodes"]:
            blockers.append("insufficient_independent_episodes")
        if metrics["coverage_ppm"] < policy["minimum_coverage_ppm"]:
            blockers.append("insufficient_coverage")
        if metrics["dependence_group_count"] < policy["minimum_stability_groups"]:
            blockers.append("insufficient_stability_groups")
        if metrics["maximum_drawdown_ppm"] > policy["maximum_drawdown_ppm"]:
            blockers.append("drawdown_limit_exceeded")
        if not metrics["valuation_complete"]:
            blockers.append("valuation_incomplete")
        if (policy["require_nonnegative_adverse_difference"] and
                adverse["metrics"]["net_result_micros"] < 0):
            blockers.append("adverse_scenario_loses_to_cash")
        validation.append({
            "candidate_id": row["id"], "kind": row["kind"], "label": row.get("label", row["id"]),
            "base": base, "adverse": adverse, "eligibility_blockers": blockers,
            "eligible": not blockers,
            "uncertainty": _uncertainty(row["episode_returns_validation_micros"], policy),
        })
    eligible = [row for row in validation if row["eligible"]]
    eligible_strategies = [row for row in eligible if row["kind"] in ("current", "variant")]
    evidence_blockers = {
        "insufficient_independent_episodes", "insufficient_coverage",
        "insufficient_stability_groups", "valuation_incomplete",
    }
    measurable = [
        row for row in validation
        if not evidence_blockers.intersection(row["eligibility_blockers"])
    ]
    current = next(row for row in validation if row["kind"] == "current")
    selected = max(
        eligible_strategies,
        key=lambda row: (
            row["base"]["metrics"]["net_result_micros"],
            -row["base"]["metrics"]["maximum_drawdown_ppm"],
            row["base"]["metrics"]["coverage_ppm"],
            row["candidate_id"] == current["candidate_id"],
            row["candidate_id"],
        ),
        default=None,
    )
    if selected is None and measurable and all(
        row["base"]["metrics"]["net_result_micros"] <= 0 for row in measurable
    ):
        recommendation = {
            "action": "prefer_cash", "candidate_id": "cash",
            "reasons": ["all_evidence_measurable_trading_alternatives_lost_to_cash"],
        }
    elif selected is None:
        recommendation = {
            "action": "insufficient_evidence", "candidate_id": None,
            "reasons": sorted({reason for row in validation for reason in row["eligibility_blockers"]}),
        }
    elif selected["base"]["metrics"]["net_result_micros"] <= 0:
        recommendation = {
            "action": "prefer_cash", "candidate_id": "cash",
            "reasons": ["all_eligible_trading_alternatives_lost_to_cash"],
        }
    elif selected["candidate_id"] == current["candidate_id"]:
        recommendation = {
            "action": "keep_current", "candidate_id": current["candidate_id"],
            "reasons": ["current_rules_ranked_first_on_validation"],
        }
    elif selected["base"]["metrics"]["net_result_micros"] - max(
        0, current["base"]["metrics"]["net_result_micros"]
    ) < policy["minimum_improvement_micros"]:
        recommendation = {
            "action": "keep_current", "candidate_id": current["candidate_id"],
            "reasons": ["candidate_improvement_below_predeclared_minimum"],
        }
    else:
        holding_hurdle = max(
            (
                row["base"]["metrics"]["net_result_micros"]
                for row in eligible if row["kind"] == "holding"
            ),
            default=0,
        )
        if selected["base"]["metrics"]["net_result_micros"] - holding_hurdle < policy["minimum_improvement_micros"]:
            recommendation = {
                "action": "keep_current", "candidate_id": current["candidate_id"],
                "reasons": ["no_bounded_variant_cleared_current_cash_and_holding_benchmarks"],
            }
        else:
            recommendation = {
                "action": "eligible_inactive_draft", "candidate_id": selected["candidate_id"],
                "reasons": ["validation_and_adverse_policy_passed"],
            }

    holdout_ids: set[str] = set()
    if evaluate_holdout:
        holdout_ids.add(current["candidate_id"])
        if selected is not None:
            holdout_ids.add(selected["candidate_id"])
        holdout_ids.update(
            row["candidate_id"] for row in validation if row["kind"] == "holding"
        )
    holdout = []
    for row in rows:
        if row["id"] not in holdout_ids:
            continue
        holdout.append({
            "candidate_id": row["id"], "kind": row["kind"],
            "base": replay_portfolio(
                row["events_holdout"], policy=row["policy"], costs=row["base_costs"],
                execution=row["execution"], cancelled=cancelled,
            ),
        })
    core = {
        "schema": COMPARISON_SCHEMA, "engine_version": ENGINE_VERSION,
        "input_lineage": lineage, "evidence_policy": policy,
        "baselines": [{
            "id": "cash", "kind": "cash", "net_result_micros": 0,
            "maximum_drawdown_ppm": 0, "eligible_outcome": True,
        }],
        "validation_results": validation, "holdout_results": holdout,
        "recommendation": recommendation,
        "holdout_policy": {
            "selection_split": "validation", "holdout_used_for_selection": False,
            "evaluated_candidate_ids": sorted(holdout_ids),
            "registry_must_prevent_reuse_as_unseen": True,
            "evaluated": evaluate_holdout,
        },
        "funded_performance": False, "submission_enabled": False,
    }
    ident = content_hash(core)
    return {**core, "id": ident, "reproducibility_sha256": ident}
