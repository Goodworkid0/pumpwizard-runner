"""Authoritative, display-only holding valuation records.

The records produced here deliberately separate a retained mark from an
executable liquidation quote.  They also keep purchase attribution independent
from the party currently managing a position.  No function in this module can
quote, sign, submit, or infer a missing cost basis or FX rate.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


SCHEMA = "pumpwizard.holding-valuation/v1"
MAX_MARK_AGE_SECONDS = 120
MAX_MARK_SLOT_LAG = 5_000
LAMPORTS_PER_SOL = Decimal("1000000000")


def retained_holding_valuation(
    position: dict[str, Any], observation: dict[str, Any] | None, *, now: float,
) -> dict[str, Any]:
    """Value one StrategyLibrary holding without inventing execution evidence."""
    observation = observation if isinstance(observation, dict) else {}
    mode = str(position.get("management_mode") or "unknown")
    manager = "session" if mode == "automatic" else "user" if mode == "manual" else "unknown"
    result = holding_valuation(
        quantity=position.get("quantity"),
        quantity_unit="token-units",
        basis=position.get("remaining_cost_usd"),
        basis_currency="USD",
        mark_price=observation.get("price_usd"),
        mark_currency="USD",
        mark_observed_at=observation.get("observed_at"),
        mark_source="retained_session_observation" if observation else None,
        now=now,
        purchase_attribution="user_reported_external_fill",
        purchase_owner_id=None,
        current_management=manager,
        manager_id=position.get("instance_id") if manager == "session" else None,
        fees_state="reported_basis_fee_inclusion_unknown",
        realized_proceeds=position.get("realized_proceeds_usd"),
        realized_currency="USD",
    )
    # Evidence-context identity must not churn every second. The exact source
    # time, build time and freshness class remain available; callers can derive
    # presentation age without making an approved context mutable.
    result["mark"]["age_seconds"] = None
    return result


def wallet_asset_valuation(
    *, amount_atoms: Any, decimals: Any, price_usd: Any, now: float,
    observed_at: Any = None, source_slot: Any = None, current_slot: Any = None,
    mark_source: str | None = None,
) -> dict[str, Any]:
    """Value wallet inventory with no claimed purchase basis or manager."""
    atoms = _decimal(amount_atoms)
    scale = _integer(decimals, minimum=0, maximum=255)
    quantity = atoms / (Decimal(10) ** scale) if atoms is not None and scale is not None else None
    return holding_valuation(
        quantity=quantity,
        quantity_unit="token-units",
        quantity_atoms=amount_atoms,
        decimals=scale,
        basis=None,
        basis_currency="USD",
        mark_price=price_usd,
        mark_currency="USD",
        mark_observed_at=observed_at,
        mark_source=mark_source,
        source_slot=source_slot,
        current_slot=current_slot,
        now=now,
        purchase_attribution="outside_runner_or_unknown",
        purchase_owner_id=None,
        current_management="user",
        manager_id=None,
        fees_state="unknown",
        realized_proceeds=None,
        realized_currency="USD",
        basis_source="missing_basis",
    )


def wallet_position_valuation(
    position: dict[str, Any], *, price_usd: Any, now: float,
    observed_at: Any = None, source_slot: Any = None, current_slot: Any = None,
    mark_source: str | None = None,
) -> dict[str, Any]:
    """Value a finalized wallet-trade position, retaining unknown FX honestly."""
    atoms = _decimal(position.get("quantity"))
    decimals = _integer(position.get("decimals"), minimum=0, maximum=255)
    quantity = atoms / (Decimal(10) ** decimals) if atoms is not None and decimals is not None else None
    entry_fx = _decimal(position.get("entry_fx"), positive=True)
    cost_lamports = _decimal(position.get("remaining_cost"), minimum=Decimal("0"))
    basis = None
    if cost_lamports is not None and entry_fx is not None:
        basis = cost_lamports / LAMPORTS_PER_SOL * entry_fx
    agent_id = position.get("agent_id")
    agent_id = agent_id if isinstance(agent_id, str) and agent_id.strip() else None
    managed = bool(agent_id and position.get("managed") is True)
    fees = _decimal(position.get("transaction_fees"), minimum=Decimal("0"))
    return holding_valuation(
        quantity=quantity,
        quantity_unit="token-units",
        quantity_atoms=position.get("quantity"),
        decimals=decimals,
        basis=basis,
        basis_currency="USD",
        mark_price=price_usd,
        mark_currency="USD",
        mark_observed_at=observed_at,
        mark_source=mark_source,
        source_slot=source_slot,
        current_slot=current_slot,
        now=now,
        purchase_attribution="session" if agent_id else "user_manual",
        purchase_owner_id=agent_id if agent_id else None,
        current_management="session" if managed else "user",
        manager_id=agent_id if managed else None,
        fees_state="retained_lamports" if fees is not None else "unknown",
        fees_amount=fees,
        fees_currency="lamports",
        realized_proceeds=position.get("realized_proceeds"),
        realized_currency="lamports",
        basis_source=(
            "remaining_lamport_cost_converted_by_retained_entry_fx"
            if basis is not None else
            "missing_entry_fx" if cost_lamports is not None else "missing_basis"
        ),
        basis_native_amount=cost_lamports,
        basis_native_currency="lamports",
    )


def holding_valuation(
    *, quantity: Any, quantity_unit: str, basis: Any, basis_currency: str,
    mark_price: Any, mark_currency: str, now: float,
    purchase_attribution: str, purchase_owner_id: Any,
    current_management: str, manager_id: Any,
    fees_state: str, realized_proceeds: Any, realized_currency: str,
    mark_observed_at: Any = None, mark_source: str | None = None,
    source_slot: Any = None, current_slot: Any = None,
    quantity_atoms: Any = None, decimals: int | None = None,
    fees_amount: Any = None, fees_currency: str | None = None,
    basis_source: str = "retained_remaining_cost_basis",
    basis_native_amount: Any = None, basis_native_currency: str | None = None,
) -> dict[str, Any]:
    """Create the common valuation record used by evidence and portfolio views."""
    now_value = _decimal(now)
    quantity_value = _decimal(quantity, minimum=Decimal("0"))
    basis_value = _decimal(basis, minimum=Decimal("0"))
    native_basis_value = _decimal(basis_native_amount, minimum=Decimal("0"))
    price_value = _decimal(mark_price, positive=True)
    observed_value = _decimal(mark_observed_at)
    source_slot_value = _integer(source_slot, minimum=0)
    current_slot_value = _integer(current_slot, minimum=0)
    freshness, age_seconds, slot_lag = _freshness(
        now_value, observed_value, source_slot_value, current_slot_value,
        observed_provided=mark_observed_at is not None,
        slot_provided=source_slot is not None or current_slot is not None,
    )
    blockers: list[str] = []
    if quantity_value is None:
        blockers.append("invalid_or_missing_quantity")
    if basis_value is None:
        blockers.append("unknown_cost_basis")
    if price_value is None:
        blockers.append("invalid_or_missing_mark")
    if freshness != "fresh":
        blockers.append(f"mark_{freshness}")

    mark_usable = price_value is not None and freshness == "fresh"
    current_value = quantity_value * price_value if quantity_value is not None and mark_usable else None
    unrealized = (
        current_value - basis_value
        if current_value is not None and basis_value is not None and basis_currency == mark_currency
        else None
    )
    unrealized_percent = (
        unrealized / basis_value * Decimal("100")
        if unrealized is not None and basis_value is not None and basis_value > 0
        else None
    )
    fee_value = _decimal(fees_amount, minimum=Decimal("0"))
    realized_value = _decimal(realized_proceeds)

    return {
        "schema": SCHEMA,
        "quantity": {
            "state": "known" if quantity_value is not None else "unknown",
            "amount": _text(quantity_value),
            "unit": quantity_unit,
            "atoms": _text(_decimal(quantity_atoms, minimum=Decimal("0"))),
            "decimals": decimals,
        },
        "basis": {
            "state": "known" if basis_value is not None else "unknown",
            "amount": _text(basis_value),
            "currency": basis_currency,
            "source": basis_source,
            "native_state": "known" if native_basis_value is not None else "unknown",
            "native_amount": _text(native_basis_value),
            "native_currency": basis_native_currency,
        },
        "mark": {
            "state": "available" if price_value is not None else "unavailable",
            "price": _text(price_value),
            "currency": mark_currency,
            "per_unit": quantity_unit,
            "kind": "retained_estimate",
            "source": mark_source,
            "observed_at": _number(observed_value),
            "source_slot": source_slot_value,
            "age_seconds": _number(age_seconds),
            "slot_lag": slot_lag,
            "freshness": freshness,
            "executable": False,
        },
        "current_value": {
            "state": "estimated" if current_value is not None else "unknown",
            "amount": _text(current_value),
            "currency": mark_currency,
            "calculation": "exact_quantity_times_fresh_retained_mark",
        },
        "unrealized_result": {
            "state": "estimated_before_exit_costs" if unrealized is not None else "unknown",
            "amount": _text(unrealized),
            "percent": _text(unrealized_percent),
            "currency": mark_currency,
        },
        "realized_proceeds": {
            "state": "known" if realized_value is not None else "unknown",
            "amount": _text(realized_value),
            "currency": realized_currency,
            "profit_treatment": "not_profit_without_matched_basis_and_costs",
        },
        "executable_liquidation": {
            "state": "unavailable",
            "amount": None,
            "currency": mark_currency,
            "reason": "A fresh reviewed sell quote and its costs are required.",
        },
        "ownership": {
            "purchase_attribution": purchase_attribution,
            "purchase_owner_id": purchase_owner_id,
            "current_management": current_management,
            "manager_id": manager_id,
        },
        "fees": {
            "state": fees_state,
            "amount": _text(fee_value),
            "currency": fees_currency,
            "exit_costs_included": False,
        },
        "external_flows": {
            "strategy_profit_treatment": "excluded",
        },
        "blockers": blockers,
        "authority": {
            "quote": False,
            "signer": False,
            "orders": False,
            "submission": False,
        },
    }


def _freshness(
    now: Decimal | None, observed_at: Decimal | None,
    source_slot: int | None, current_slot: int | None,
    *, observed_provided: bool, slot_provided: bool,
) -> tuple[str, Decimal | None, int | None]:
    states: list[str] = []
    age: Decimal | None = None
    lag: int | None = None
    if observed_provided:
        if observed_at is None or now is None:
            states.append("invalid_time")
        else:
            age = now - observed_at
            states.append(
                "future" if age < 0 else
                "stale" if age > MAX_MARK_AGE_SECONDS else
                "fresh"
            )
    if slot_provided:
        if source_slot is None or current_slot is None:
            states.append("invalid_time")
        else:
            lag = current_slot - source_slot
            states.append(
                "future" if lag < 0 else
                "stale" if lag > MAX_MARK_SLOT_LAG else
                "fresh"
            )
    if states:
        # A retained mark is usable only when every supplied freshness signal is
        # valid and fresh.  Prefer the most actionable fail-closed diagnosis.
        for state in ("invalid_time", "future", "stale"):
            if state in states:
                return state, age, lag
        return "fresh", age, lag
    return "missing_time", None, None


def _decimal(
    value: Any, *, minimum: Decimal | None = None, positive: bool = False,
) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite() or minimum is not None and result < minimum or positive and result <= 0:
        return None
    return result


def _integer(value: Any, *, minimum: int, maximum: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if str(result) != str(value) or result < minimum or maximum is not None and result > maximum:
        return None
    return result


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if result in {"", "-0"} else result


def _number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)
