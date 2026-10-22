"""Durable, local dry-run order planning.

This module deliberately has no signer, transaction serializer, RPC submission,
or route builder. It converts a fresh local recommendation into a bounded plan
and checks that plan against a newer local observation. Its records form the
auditable boundary that a later executor must consume rather than bypass.
"""
from __future__ import annotations

import math
import time
from typing import Any

from .decision import MarketQuote
from .library import StrategyLibrary
from .strategies import parse_package


DRY_RUN_PLAN_VERSION = "dry-run-order-v1"


class OrderIntentError(ValueError):
    """A local decision cannot safely become a bounded dry-run order."""


def prepare_order(library: StrategyLibrary, instance_id: str, mint: str, *, spend_usd: float | None,
                  now: float | None = None) -> dict[str, object]:
    """Create one dry-run order from the newest still-actionable local decision."""
    created_at = time.time() if now is None else _finite_time(now)
    config = _dry_run_config(library, instance_id)
    decision = library.latest_actionable_decision(instance_id, mint)
    decision_at = _finite_time(decision["at"])
    _fresh(decision_at, created_at, float(config["decision_ttl_seconds"]), "decision")
    quote = _current_quote(library, instance_id, mint, created_at, float(config["decision_ttl_seconds"]))
    payload = decision["payload"]
    if not isinstance(payload, dict):
        raise OrderIntentError("Decision payload is invalid")
    if payload.get("route") not in (None, quote.route):
        raise OrderIntentError("The market route changed after this decision")
    instance = library.instance(instance_id)
    strategy = parse_package(instance["package"])
    plan: dict[str, object] = {
        "version": DRY_RUN_PLAN_VERSION,
        "network": "none",
        "execution": "dry-run-only",
        "decision": {
            "at": decision_at, "kind": decision["kind"], "mint": mint,
            "reason": decision["reason"], "strategy_id": strategy.strategy_id,
        },
        "quote": _quote_snapshot(quote),
        "limits": {
            "max_slippage_bps": config["max_slippage_bps"],
            "decision_ttl_seconds": config["decision_ttl_seconds"],
            "configured_max_order_usd": config["max_order_usd"],
        },
        "warning": "Local estimate only. No route was built, simulated on-chain, signed, or submitted.",
    }
    if decision["kind"] == "entry_recommended":
        return _prepare_buy(library, instance_id, decision, quote, strategy.effective_rules, config,
                            spend_usd, created_at, plan)
    return _prepare_sell(library, instance_id, decision, quote, config, spend_usd, created_at, plan)


def simulate_order(library: StrategyLibrary, order_id: str, *, now: float | None = None) -> dict[str, object]:
    """Reprice a draft against local observations without contacting or changing a network."""
    simulated_at = time.time() if now is None else _finite_time(now)
    order = library.order(order_id)
    if order["status"] != "draft":
        raise OrderIntentError("Only a draft order can be dry-run simulated")
    config = _dry_run_config(library, str(order["instance_id"]))
    plan = order["plan"]
    if not isinstance(plan, dict) or plan.get("version") != DRY_RUN_PLAN_VERSION:
        raise OrderIntentError("Order has an unsupported dry-run plan")
    quote = _current_quote(library, str(order["instance_id"]), str(order["mint"]), simulated_at,
                           float(config["decision_ttl_seconds"]))
    if quote.route != order["route"]:
        raise OrderIntentError("The market route changed before dry-run simulation")
    minimum = float(order["minimum_output_amount"])
    if order["side"] == "buy":
        maximum = float(order["max_input_usd"])
        output = maximum / quote.price_usd
        output_unit = "token"
    else:
        tokens = float(order["token_amount"])
        output = tokens * quote.price_usd
        output_unit = "usd"
    if not math.isfinite(output) or output <= 0 or output < minimum:
        raise OrderIntentError("Fresh local quote no longer meets this order's slippage limit")
    event = {
        "simulator": DRY_RUN_PLAN_VERSION, "at": simulated_at,
        "quote": _quote_snapshot(quote), "estimated_output_amount": output,
        "output_unit": output_unit, "minimum_output_amount": minimum,
        "submitted": False, "signature": None,
        "result": "passed_local_price_check_only",
    }
    return library.transition_order(order_id, expected_status="draft", status="simulated", at=simulated_at,
                                    event_kind="dry_run_simulated", payload=event)


def cancel_order(library: StrategyLibrary, order_id: str, *, now: float | None = None) -> dict[str, object]:
    cancelled_at = time.time() if now is None else _finite_time(now)
    order = library.order(order_id)
    if order["status"] not in ("draft", "simulated"):
        raise OrderIntentError("Only a draft or dry-run-simulated order can be cancelled")
    return library.transition_order(order_id, expected_status=str(order["status"]), status="cancelled",
                                    at=cancelled_at, event_kind="cancelled", payload={
                                        "at": cancelled_at, "reason": "user_cancelled", "submitted": False,
                                    })


def _prepare_buy(library: StrategyLibrary, instance_id: str, decision: dict[str, object], quote: MarketQuote,
                 rules: dict[str, Any], config: dict[str, object], spend_usd: float | None,
                 created_at: float, plan: dict[str, object]) -> dict[str, object]:
    if quote.route != "curve":
        raise OrderIntentError("This development executor can dry-run only bonding-curve entries")
    spend = _positive(spend_usd, "Entry spend")
    maximum = float(config["max_order_usd"])
    if spend > maximum + 1e-9:
        raise OrderIntentError("Entry spend exceeds the local executor maximum")
    copied_order = rules.get("order_usd")
    if copied_order is not None and abs(spend - _positive(copied_order, "Copied strategy order")) > 1e-9:
        raise OrderIntentError("Entry spend must exactly match the copied strategy order size")
    expected_tokens = spend / quote.price_usd
    minimum_tokens = expected_tokens * (1 - int(config["max_slippage_bps"]) / 10_000)
    if minimum_tokens <= 0:
        raise OrderIntentError("Local slippage limit leaves no minimum token output")
    plan.update({
        "side": "buy", "max_input_usd": spend, "estimated_output_amount": expected_tokens,
        "minimum_output_amount": minimum_tokens, "output_unit": "token",
    })
    order_id = library.create_order(
        instance_id, decision_key=str(decision["decision_key"]), decision_at=_finite_time(decision["at"]),
        created_at=created_at, side="buy", mint=str(decision["mint"]),
        symbol=quote.symbol, route=quote.route, position_id=None, max_input_usd=spend,
        token_amount=expected_tokens, minimum_output_amount=minimum_tokens, plan=plan,
    )
    return library.order(order_id)


def _prepare_sell(library: StrategyLibrary, instance_id: str, decision: dict[str, object], quote: MarketQuote,
                  config: dict[str, object], spend_usd: float | None, created_at: float,
                  plan: dict[str, object]) -> dict[str, object]:
    if spend_usd is not None:
        raise OrderIntentError("Exit orders take their token amount from the recorded position")
    payload = decision["payload"]
    if not isinstance(payload, dict) or not isinstance(payload.get("position_id"), str):
        raise OrderIntentError("Exit decision has no local position reference")
    position_id = payload["position_id"]
    position = next((row for row in library.positions(instance_id, include_closed=False) if row["id"] == position_id), None)
    if position is None:
        raise OrderIntentError("The recorded position is no longer open")
    fraction = payload.get("fraction", payload.get("fraction_of_original"))
    if not isinstance(fraction, (int, float)) or isinstance(fraction, bool) or not 0 < fraction <= 1:
        raise OrderIntentError("Exit decision has an invalid fraction")
    quantity = min(float(position["quantity"]), float(position["initial_quantity"]) * float(fraction))
    if not math.isfinite(quantity) or quantity <= 0:
        raise OrderIntentError("Recorded position has no sellable quantity")
    expected_usd = quantity * quote.price_usd
    minimum_usd = expected_usd * (1 - int(config["max_slippage_bps"]) / 10_000)
    if minimum_usd <= 0:
        raise OrderIntentError("Local slippage limit leaves no minimum exit value")
    plan.update({
        "side": "sell", "position_id": position_id, "token_amount": quantity,
        "estimated_output_amount": expected_usd, "minimum_output_amount": minimum_usd,
        "output_unit": "usd",
    })
    order_id = library.create_order(
        instance_id, decision_key=str(decision["decision_key"]), decision_at=_finite_time(decision["at"]),
        created_at=created_at, side="sell", mint=str(decision["mint"]), symbol=quote.symbol,
        route=quote.route, position_id=position_id, max_input_usd=None, token_amount=quantity,
        minimum_output_amount=minimum_usd, plan=plan,
    )
    return library.order(order_id)


def _dry_run_config(library: StrategyLibrary, instance_id: str) -> dict[str, object]:
    config = library.executor_config(instance_id)
    if config is None or config["mode"] != "dry-run":
        raise OrderIntentError("Enable bounded dry-run mode before preparing an order")
    return config


def _current_quote(library: StrategyLibrary, instance_id: str, mint: str, now: float, ttl: float) -> MarketQuote:
    quote = MarketQuote.from_record(library.latest_observation(instance_id, mint))
    _fresh(quote.observed_at, now, ttl, "market quote")
    if quote.route not in ("curve", "amm") or quote.price_usd <= 0:
        raise OrderIntentError("No current supported local market quote exists for this order")
    return quote


def _fresh(at: float, now: float, ttl: float, label: str) -> None:
    if now < at or now - at > ttl:
        raise OrderIntentError(f"The {label} is outside the local executor time limit")


def _quote_snapshot(quote: MarketQuote) -> dict[str, object]:
    return {
        "observed_at": quote.observed_at, "route": quote.route, "pool": quote.pool,
        "price_usd": quote.price_usd, "price_native": quote.price_native,
        "quote_usd": quote.quote_usd, "liquidity_usd": quote.liquidity_usd,
        "source": quote.source,
    }


def _positive(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OrderIntentError(label + " must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise OrderIntentError(label + " must be a finite positive number")
    return result


def _finite_time(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OrderIntentError("Order time must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise OrderIntentError("Order time must be finite")
    return result
