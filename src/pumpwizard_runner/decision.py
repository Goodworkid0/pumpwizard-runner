"""Deterministic local strategy evaluation.

This module has no wallet, route builder, or simulated-fill implementation. It
turns the user's own normalized observations and durable position state into
versioned entry and exit recommendations. The execution layer added later will
validate a fresh executable route before it can act on any recommendation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable

from .strategies import ImportedStrategy


RESEARCH_STYLES = {
    "persistent", "higher-low", "multi-window", "squeeze", "retest", "post-grad",
    "liquidity", "relative", "second-wave", "recovery",
}


def _number(value: Any, default: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if default is None:
            raise ValueError("Expected a finite number")
        return default
    result = float(value)
    if not math.isfinite(result):
        if default is None:
            raise ValueError("Expected a finite number")
        return default
    return result


@dataclass(frozen=True)
class MarketQuote:
    mint: str
    symbol: str
    observed_at: float
    route: str
    pool: str
    market_cap_usd: float
    price_usd: float
    price_native: float
    liquidity_usd: float
    liquidity_native: float
    source: str = "unknown"
    quote_usd: float = 0.0
    base_reserve: float = 0.0
    reserve_usd: float = 0.0
    real_token_reserve: float = 0.0
    supply: float = 0.0
    decimals: int = 0
    source_at: float = 0.0
    fx_at: float = 0.0
    created_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "MarketQuote":
        mint = record.get("mint")
        if not isinstance(mint, str) or not mint:
            raise ValueError("Market record has no mint")
        raw_route = record.get("route")
        if raw_route in ("pump_bonding_curve_sol", "curve"):
            route = "curve"
        elif raw_route in ("amm", "pumpswap") or record.get("dex_id") == "pumpswap":
            route = "amm"
        else:
            route = "unsupported"
        observed_at = _number(record.get("observed_at"))
        cap = _number(record.get("market_cap_usd"))
        quote_usd = _number(record.get("quote_usd"), 1.0)
        if "price_usd" in record:
            price_usd = _number(record["price_usd"])
        else:
            price_usd = _number(record.get("y")) / _number(record.get("x")) * quote_usd
        if "y" in record and "x" in record:
            price_native = _number(record["y"]) / _number(record["x"])
        else:
            price_native = _number(record.get("price_native"), price_usd / quote_usd)
        if "real_y" in record:
            liquidity_native = _number(record["real_y"])
            liquidity_usd = liquidity_native * quote_usd
        else:
            liquidity_usd = _number(record.get("liquidity_usd"), 0.0)
            liquidity_native = _number(record.get("liquidity_native"), liquidity_usd / quote_usd)
        if min(cap, price_usd, price_native) <= 0 or liquidity_usd < 0 or liquidity_native < 0:
            raise ValueError("Market record contains invalid price or liquidity")
        pool = record.get("pool") or (record.get("curve_address") if route == "curve" else record.get("pair_address"))
        if not isinstance(pool, str) or not pool:
            pool = mint
        return cls(mint=mint, symbol=str(record.get("symbol") or mint[:8])[:80], observed_at=observed_at,
                   route=route, pool=pool, market_cap_usd=cap, price_usd=price_usd,
                   price_native=price_native, liquidity_usd=liquidity_usd,
                   liquidity_native=liquidity_native, source=str(record.get("source") or "unknown"),
                   quote_usd=quote_usd, base_reserve=_number(record.get("base_reserve", record.get("x")), 0.0),
                   reserve_usd=_number(record.get("reserve_usd"), _number(record.get("y"), 0.0) * quote_usd),
                   real_token_reserve=_number(record.get("real_token_reserve", record.get("real_x")), 0.0),
                   supply=_number(record.get("supply"), 0.0), decimals=int(_number(record.get("decimals"), 0.0)),
                   source_at=_number(record.get("source_at"), observed_at),
                   fx_at=_number(record.get("fx_at"), observed_at),
                   created_at=_number(record.get("created_at"), 0.0),
                   metadata=dict(record.get("metadata") or {}) if isinstance(record.get("metadata"), dict) else {})

    def as_record(self) -> dict[str, Any]:
        return {
            "mint": self.mint, "symbol": self.symbol, "observed_at": self.observed_at,
            "route": self.route, "pool": self.pool, "market_cap_usd": self.market_cap_usd,
            "price_usd": self.price_usd, "price_native": self.price_native,
            "liquidity_usd": self.liquidity_usd, "liquidity_native": self.liquidity_native,
            "source": self.source, "quote_usd": self.quote_usd, "base_reserve": self.base_reserve,
            "reserve_usd": self.reserve_usd, "real_token_reserve": self.real_token_reserve,
            "supply": self.supply, "decimals": self.decimals, "source_at": self.source_at,
            "fx_at": self.fx_at, "created_at": self.created_at, "metadata": self.metadata,
        }


@dataclass(frozen=True)
class PositionView:
    position_id: str
    mint: str
    symbol: str
    entry_at: float
    initial_quantity: float
    quantity: float
    remaining_cost_usd: float
    total_cost_usd: float
    realized_proceeds_usd: float = 0.0
    sold_stages: tuple[int, ...] = ()
    peak_price_usd: float = 0.0
    last_usable_at: float = 0.0
    entry_pool: str = ""
    entry_supply: float = 0.0
    amm_pool: str | None = None
    migration_seen: bool = False
    valuation_basis: str = "modeled"


@dataclass(frozen=True)
class Decision:
    kind: str
    mint: str
    at: float
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return {"kind": self.kind, "mint": self.mint, "at": self.at,
                "reason": self.reason, "payload": self.payload}


def summarize(points: list[MarketQuote], seconds: float, now: float, *,
              maximum_gap_seconds: float = 180) -> dict[str, Any] | None:
    """Calculate the shared research feature set from locally retained samples."""
    if not points or now - points[-1].observed_at > 120:
        return None
    end = points[-1].observed_at
    prior = [index for index, point in enumerate(points) if point.observed_at <= end - seconds]
    if not prior:
        return None
    rows = points[prior[-1]:]
    if len(rows) < 6:
        return None
    if (not math.isfinite(maximum_gap_seconds) or maximum_gap_seconds <= 0
            or any(later.observed_at - earlier.observed_at > maximum_gap_seconds
                   for earlier, later in zip(rows, rows[1:]))):
        return None
    if any((point.route, point.pool) != (rows[-1].route, rows[-1].pool) for point in rows):
        return None
    values = [point.price_native if point.route == "curve" else point.price_usd for point in rows]
    if sum(left != right for left, right in zip(values, values[1:])) < 3:
        return None
    count = len(values)
    third = max(1, count // 3)
    early, middle, late = values[:third], values[third:2 * third], values[2 * third:]
    half, quarter = values[count // 2], values[3 * count // 4]
    peak, trough, prior_high = max(values), min(values[third:]), max(early)
    breakout = next((index for index in range(third, count - 2)
                     if values[index] > prior_high * 1.01), None)
    retest = breakout is not None and any(prior_high * .97 <= value <= prior_high * 1.02
                                          for value in values[breakout + 1:-1])
    first_liquidity = rows[0].liquidity_native if rows[0].route == "curve" else rows[0].liquidity_usd
    last_liquidity = rows[-1].liquidity_native if rows[-1].route == "curve" else rows[-1].liquidity_usd
    liquidity_growth = last_liquidity / first_liquidity - 1 if first_liquidity > 0 else None
    return {
        "growth": values[-1] / values[0] - 1,
        "first_half": half / values[0] - 1,
        "second_half": values[-1] / half - 1,
        "recent_quarter": values[-1] / quarter - 1,
        "drawdown": 1 - values[-1] / peak,
        "higher_lows": min(early) < min(middle) < min(late) and values[-1] > min(late) * 1.02,
        "squeeze": max(middle) / min(middle) - 1 <= .05 and values[-1] > max(middle) * 1.02,
        "retest": retest and values[-1] > prior_high * 1.02,
        "recovery": (1 - trough / max(early) >= .12 and
                     max(late[:-1] or late) / min(late[:-1] or late) - 1 <= .08 and
                     values[-1] > trough * 1.04),
        "liquidity_growth": liquidity_growth,
        "samples": count,
        "start_at": rows[0].observed_at,
        "end_at": end,
        "route": rows[-1].route,
        "pool": rows[-1].pool,
        "peer_count": 0,
        "percentile": None,
    }


class DecisionEngine:
    """A local, stateful evaluator for one imported strategy instance."""

    def __init__(self, strategy: ImportedStrategy):
        if strategy.compatibility != "monitoring-ready":
            raise ValueError("Strategy is not compatible with the local monitoring engine")
        self.strategy = strategy
        self.rules = strategy.effective_rules
        self.profile = strategy.profile
        self.histories: dict[str, list[MarketQuote]] = {}
        self.last_reason: dict[str, str | None] = {}
        self.last_exits: dict[str, float] = {}
        self.peak_caps: dict[str, float] = {}

    def checkpoint(self) -> dict[str, Any]:
        return {"version": 1, "last_reason": self.last_reason, "last_exits": self.last_exits,
                "peak_caps": self.peak_caps}

    def restore_checkpoint(self, value: dict[str, Any]) -> None:
        if value.get("version") != 1:
            raise ValueError("Unknown decision checkpoint version")
        self.last_reason = dict(value["last_reason"])
        self.last_exits = dict(value["last_exits"])
        self.peak_caps = dict(value.get("peak_caps", {}))

    @property
    def style(self) -> str:
        return str(self.rules.get("entry_style", "momentum"))

    def restore(self, records: Iterable[dict[str, Any]]) -> None:
        for record in records:
            self._ingest(MarketQuote.from_record(record))

    def restore_decisions(self, records: Iterable[dict[str, Any]]) -> None:
        """Recover transition state so one-cycle runs do not repeat alerts."""
        for record in records:
            kind = record.get("kind")
            mint = record.get("mint")
            if not isinstance(mint, str) or not isinstance(kind, str):
                continue
            if kind == "entry_blocked" and isinstance(record.get("reason"), str):
                self.last_reason[mint] = record["reason"]
            elif kind == "entry_recommended":
                self.last_reason[mint] = None
            elif kind in ("exit_recommended", "partial_exit_recommended"):
                at = record.get("at")
                if isinstance(at, (int, float)) and math.isfinite(at):
                    self.last_exits[mint] = float(at)

    def evaluate(self, quotes: Iterable[MarketQuote], at: float, *,
                 evidence: dict[str, dict[str, Any]] | None = None,
                 positions: Iterable[PositionView] = (), allow_entries: bool = True,
                 opportunities: list[dict[str, Any]] | None = None) -> list[Decision]:
        evidence = evidence or {}
        quote_map = {quote.mint: quote for quote in quotes if self.quote_usable(quote, at)}
        for quote in quote_map.values():
            self._ingest(quote)
        decisions: list[Decision] = []
        active = {position.mint: position for position in positions if position.quantity > 0}
        for position in active.values():
            decision = self._exit_decision(position, quote_map.get(position.mint), evidence.get(position.mint, {}), at)
            if decision:
                decisions.append(decision)
        if not allow_entries:
            return decisions
        capacity = int(self.rules.get("max_positions", 1))
        free_slots = max(0, capacity - len(active))
        for mint in sorted(quote_map):
            quote = quote_map[mint]
            self.peak_caps[mint] = max(self.peak_caps.get(mint, 0), quote.market_cap_usd)
            local_evidence = dict(evidence.get(mint, {}))
            local_evidence["peak_cap"] = max(local_evidence.get("peak_cap") or 0, self.peak_caps[mint])
            reason = self.entry_reason(quote, local_evidence, at)
            if mint in active:
                reason = "Position already open"
            elif not reason and free_slots <= 0:
                reason = "Strategy position capacity is full"
            unseen = mint not in self.last_reason
            previous = self.last_reason.get(mint)
            self.last_reason[mint] = reason
            opportunity_payload = {
                "symbol": quote.symbol,
                "market_cap_usd": quote.market_cap_usd,
                "price_usd": quote.price_usd,
                "route": quote.route,
                "pool": quote.pool,
                "strategy_id": self.strategy.strategy_id,
            }
            if opportunities is not None:
                opportunities.append({
                    "at": at,
                    "kind": "entry_blocked" if reason else "entry_recommended",
                    "mint": mint,
                    "reason": reason or "strategy_entry_signal",
                    "payload": opportunity_payload,
                })
            if not reason and (unseen or previous is not None):
                free_slots -= 1
                decisions.append(Decision(
                    "entry_recommended", mint, at, "strategy_entry_signal", opportunity_payload,
                ))
            elif reason and reason != previous:
                decisions.append(Decision("entry_blocked", mint, at, reason, {
                    "symbol": quote.symbol, "market_cap_usd": quote.market_cap_usd,
                }))
        return decisions

    def entry_reason(self, quote: MarketQuote, evidence: dict[str, Any], at: float) -> str | None:
        if not self.quote_usable(quote, at):
            return "No fresh supported bonding-curve quote"
        low = _number(self.rules.get("entry_cap_min", self.rules.get("entry_cap_min_usd")))
        high = _number(self.rules.get("entry_cap_max", self.rules.get("entry_cap_max_usd")))
        if self.style in RESEARCH_STYLES:
            return self._research_entry_reason(quote, evidence, at, low, high)
        if quote.route != "curve":
            return "No fresh supported bonding-curve quote"
        if not low <= quote.market_cap_usd <= high:
            return "Outside this strategy’s market-cap range"
        if self.rules.get("entry_policy", "simulation_unverified") == "strict" and not self._ownership_clear(quote.mint, evidence, at):
            return "Ownership flagged or unverified"
        history = self.histories.get(quote.mint, [])
        window = _number(self.rules.get("observation_seconds"), 60)
        minimum_samples = int(self.rules.get("minimum_samples", 4))
        if len(history) < minimum_samples or history[-1].observed_at - history[0].observed_at < window:
            return "Building a 60-second observation window" if self.profile == "ladder-v1" else "Building observation window"
        changes = sum(abs(later.price_native / earlier.price_native - 1) > 1e-8
                      for earlier, later in zip(history, history[1:]))
        if changes < int(self.rules.get("minimum_price_changes", 2)):
            return "Not enough distinct reserve-implied price changes"
        if history[-1].price_native / history[0].price_native - 1 < _number(self.rules.get("minimum_growth_fraction"), .01):
            return "No positive reserve-implied price trend yet"
        # Use the last accepted sample's FX basis. A duplicate observation may
        # carry a changed FX value without replacing the recorded history.
        reserve_growth = history[-1].liquidity_native - history[0].liquidity_native
        minimum_growth = _number(self.rules.get("minimum_reserve_growth_usd"), 0)
        fx = history[-1].quote_usd
        if (fx > 0 and reserve_growth < minimum_growth / fx) or (fx <= 0 and minimum_growth > 0):
            return "Real SOL reserve growth is insufficient"
        prices = [point.price_native for point in history]
        style = self.style
        if style == "breakout" and prices[-1] < max(prices[:-1]) * (1 + _number(self.rules.get("breakout_fraction"), .01)):
            return "Awaiting a confirmed range breakout"
        if style == "pullback_reclaim":
            peak = max(range(len(prices) - 1), key=prices.__getitem__)
            if peak >= len(prices) - 2:
                return "Awaiting a pullback after the local peak"
            trough = min(prices[peak + 1:-1])
            if 1 - trough / prices[peak] < _number(self.rules.get("pullback_min_fraction"), .10):
                return "Pullback was too shallow"
            if prices[-1] / trough - 1 < _number(self.rules.get("reclaim_fraction"), .04):
                return "Awaiting a reclaim from the pullback low"
        if style == "slow_burn":
            midpoint = prices[len(prices) // 2]
            growth = _number(self.rules.get("minimum_growth_fraction"), .01)
            if midpoint / prices[0] - 1 < growth / 2 or prices[-1] / midpoint - 1 < growth / 3:
                return "Momentum has not persisted through the observation window"
        peak_cap = _number(evidence.get("peak_cap"), quote.market_cap_usd)
        if 1 - quote.market_cap_usd / max(peak_cap, quote.market_cap_usd) > _number(self.rules.get("maximum_peak_drawdown"), .35):
            return "Too far below its recorded peak"
        viability_reason = self._modeled_entry_viability(quote)
        if viability_reason:
            return viability_reason
        return None

    def _modeled_entry_viability(self, quote: MarketQuote) -> str | None:
        """Apply the legacy ladder's liquidity/cost gate to a recommendation.

        This performs no trade simulation or fill. It only keeps the exact
        pre-entry screen from a copied early-ladder configuration, so a local
        recommendation cannot be looser than the source rule set.
        """
        keys = {"order_usd", "fixed_fee_usd", "entry_rent_usd", "fee_fraction",
                "adverse_fraction", "max_entry_reserve_fraction", "max_roundtrip_loss_fraction",
                "min_curve_liquidity_usd"}
        if not keys <= set(self.rules):
            return None
        minimum_liquidity = _number(self.rules["min_curve_liquidity_usd"] if quote.route == "curve"
                                    else self.rules.get("min_amm_liquidity_usd"))
        if quote.liquidity_usd < minimum_liquidity:
            return "Liquidity or modeled entry costs fail the screen"
        if min(quote.base_reserve, quote.reserve_usd, quote.real_token_reserve) <= 0:
            return "Current quote lacks reserves required by the strategy screen"
        order = _number(self.rules["order_usd"])
        fixed = _number(self.rules["fixed_fee_usd"])
        rent = _number(self.rules.get("entry_rent_usd"), 0.0)
        fee_fraction = _number(self.rules["fee_fraction"])
        adverse = _number(self.rules["adverse_fraction"])
        pool_usd = (order - fixed - rent) / (1 + fee_fraction)
        if pool_usd <= 0 or pool_usd > quote.liquidity_usd * _number(self.rules["max_entry_reserve_fraction"]):
            return "Liquidity or modeled entry costs fail the screen"
        raw_quantity = quote.base_reserve * pool_usd / (quote.reserve_usd + pool_usd)
        precision = 10 ** max(0, quote.decimals)
        quantity = math.floor(raw_quantity * (1 - adverse) * precision) / precision
        if quantity <= 0 or raw_quantity >= quote.real_token_reserve:
            return "Liquidity or modeled entry costs fail the screen"
        position = PositionView("screen", quote.mint, quote.symbol, quote.observed_at,
                                quantity, quantity, order, order, entry_pool=quote.pool,
                                entry_supply=quote.supply)
        net = self.ladder_sell_value(quote, position)
        if net is None or 1 - net / order > _number(self.rules["max_roundtrip_loss_fraction"]):
            return "Liquidity or modeled entry costs fail the screen"
        return None

    def quote_usable(self, quote: MarketQuote, at: float) -> bool:
        age = float(self.rules.get("quote_max_age_seconds", 120))
        return (quote.route in ("curve", "amm") and 0 <= at - quote.observed_at <= age
                and all(-2 <= at - timestamp <= age for timestamp in
                        (quote.source_at or quote.observed_at, quote.fx_at or quote.observed_at)))

    def ladder_sell_value(self, quote: MarketQuote | None, position: PositionView) -> float | None:
        """Reference cost screen only; this is not an executable swap quote."""
        if quote is None or position.quantity <= 0:
            return None
        if position.amm_pool and (quote.route != "amm" or quote.pool != position.amm_pool):
            return None
        if quote.route == "curve":
            if quote.pool != position.entry_pool or position.quantity >= quote.base_reserve or quote.liquidity_usd <= 0:
                return None
            base = quote.base_reserve - position.quantity
            reserve = quote.base_reserve * quote.reserve_usd / base
            real = quote.liquidity_usd + reserve - quote.reserve_usd
        elif quote.route == "amm":
            if not position.migration_seen or position.entry_supply <= 0 or abs(quote.market_cap_usd / quote.price_usd / position.entry_supply - 1) > .10:
                return None
            base, reserve, real = quote.base_reserve, quote.reserve_usd, quote.liquidity_usd
        else:
            return None
        gross = reserve * position.quantity / (base + position.quantity)
        if gross > real or position.quantity / (base + position.quantity) > .10:
            return None
        gross *= 1 - float(self.rules["adverse_fraction"])
        net = gross * (1 - float(self.rules["fee_fraction"])) - float(self.rules["fixed_fee_usd"])
        return net if net > 0 else None

    def _research_entry_reason(self, quote: MarketQuote, evidence: dict[str, Any], at: float,
                               low: float, high: float) -> str | None:
        if not low <= quote.market_cap_usd < high:
            return "Outside entry market-cap band"
        feature = summarize(
            self.histories.get(quote.mint, []),
            _number(self.rules.get("observation_seconds"), 300),
            at,
            maximum_gap_seconds=_number(self.rules.get("max_sample_gap_seconds"), 180),
        )
        if not feature or feature["pool"] != quote.pool:
            return "Warming up: insufficient continuous observations"
        if self.rules.get("entry_policy", "simulation_unverified") == "strict" and not self._ownership_clear(quote.mint, evidence, at):
            return "Ownership flagged or unverified"
        if self.style != "post-grad" and quote.route != "curve":
            return "Awaiting fresh supported market quote"
        if self.style == "post-grad" and quote.route != "amm":
            return "Awaiting observed post-graduation market"
        growth = feature["growth"]
        threshold = _number(self.rules.get("minimum_growth_fraction"), .02)
        if self.style not in ("recovery", "second-wave") and growth < threshold:
            return "Growth below signal threshold"
        if feature["drawdown"] > _number(self.rules.get("maximum_peak_drawdown"), .65):
            return "Too far below window high"
        self._populate_peer_metrics(quote, feature, low, high, at)
        passed = {
            "persistent": feature["first_half"] > .01 and feature["second_half"] > .01 and feature["drawdown"] < .12,
            "higher-low": feature["higher_lows"],
            "multi-window": feature["first_half"] > 0 and feature["second_half"] > .01 and feature["recent_quarter"] > .005,
            "squeeze": feature["squeeze"] and feature["liquidity_growth"] is not None and feature["liquidity_growth"] > 0,
            "retest": feature["retest"],
            "post-grad": evidence.get("graduated_observed") is True and feature["drawdown"] < .15 and feature["second_half"] > 0,
            "liquidity": feature["liquidity_growth"] is not None and feature["liquidity_growth"] >= .05 and growth >= .03,
            "relative": feature["peer_count"] >= 4 and feature["percentile"] is not None and feature["percentile"] >= .75,
            "second-wave": feature["recovery"],
            "recovery": feature["recovery"],
        }[self.style]
        if not passed:
            return "Awaiting " + self.style.replace("-", " ") + " signal"
        if self.style == "second-wave":
            last_exit = self.last_exits.get(quote.mint)
            if last_exit is not None and feature["start_at"] <= last_exit:
                return "Awaiting a fresh post-exit observation window"
        viability_reason = self._modeled_entry_viability(quote)
        if viability_reason:
            return viability_reason
        return None

    def _populate_peer_metrics(self, quote: MarketQuote, feature: dict[str, Any], low: float, high: float, at: float) -> None:
        peers: list[tuple[str, dict[str, Any]]] = []
        window = _number(self.rules.get("observation_seconds"), 300)
        for mint, history in self.histories.items():
            if not history:
                continue
            latest = history[-1]
            if low <= latest.market_cap_usd < high:
                candidate = summarize(
                    history, window, at,
                    maximum_gap_seconds=_number(
                        self.rules.get("max_sample_gap_seconds"), 180,
                    ),
                )
                if candidate and candidate["pool"] == latest.pool:
                    peers.append((mint, candidate))
        feature["peer_count"] = len(peers)
        if len(peers) > 1:
            feature["percentile"] = sum(other["growth"] < feature["growth"] for _, other in peers) / (len(peers) - 1)

    def _exit_decision(self, position: PositionView, quote: MarketQuote | None,
                       evidence: dict[str, Any], at: float) -> Decision | None:
        ownership = evidence.get("ownership") if isinstance(evidence.get("ownership"), dict) else {}
        if quote is not None and not self.quote_usable(quote, at):
            quote = None
        mark = (self.ladder_sell_value(quote, position) if self.profile == "ladder-v1" and position.valuation_basis == 'modeled'
                else quote.price_usd * position.quantity if quote else None)
        reason: str | None = None
        if ownership.get("mint") == position.mint and ownership.get("status") == "blocked":
            reason = "ownership_flag"
        elif at - position.entry_at >= _number(self.rules.get("max_hold_seconds"), 864000):
            reason = "holding_limit"
        elif mark is None:
            if at - position.last_usable_at >= _number(self.rules.get("quote_gap_exit_seconds"), 600):
                reason = "quote_gap"
        else:
            if position.remaining_cost_usd > 0 and mark / position.remaining_cost_usd - 1 <= _number(self.rules.get("stop_loss_fraction"), -.30):
                reason = "stop_loss"
            elif position.sold_stages and quote.price_usd <= max(position.peak_price_usd, quote.price_usd) * (1 - _number(self.rules.get("trailing_drawdown_fraction"), .35)):
                reason = "trailing_exit"
        if reason:
            self.last_exits[position.mint] = at
            return Decision("exit_recommended", position.mint, at, reason, {
                "position_id": position.position_id, "symbol": position.symbol,
                "fraction": 1.0, "mode": "manual_or_automatic_by_user_setting",
            })
        if quote is None or mark is None:
            return None
        stages = [index for index, target in enumerate(self.rules.get("targets", []))
                  if index not in position.sold_stages and quote.market_cap_usd >= _number(target.get("market_cap_usd"), float("inf"))]
        if stages:
            fraction = sum(_number(self.rules["targets"][index].get("initial_fraction"), 0) for index in stages)
            return Decision("partial_exit_recommended", position.mint, at, "market_cap_target", {
                "position_id": position.position_id, "symbol": position.symbol,
                "fraction_of_original": min(1.0, fraction), "stages": stages,
                "mode": "manual_or_automatic_by_user_setting",
            })
        return None

    def _ownership_clear(self, mint: str, evidence: dict[str, Any], at: float) -> bool:
        gate = evidence.get("ownership") if isinstance(evidence.get("ownership"), dict) else {}
        checked = gate.get("checked_at")
        if gate.get("mint") != mint or gate.get("status") != "clear":
            return False
        return isinstance(checked, (int, float)) and 0 <= at - checked <= _number(self.rules.get("ownership_ttl_seconds"), 120)

    def _ingest(self, quote: MarketQuote) -> None:
        history = self.histories.setdefault(quote.mint, [])
        if history and quote.observed_at <= history[-1].observed_at:
            return
        gap = _number(self.rules.get("max_sample_gap_seconds"), 180)
        if history and (quote.observed_at - history[-1].observed_at > gap or
                        (quote.route, quote.pool) != (history[-1].route, history[-1].pool)):
            history.clear()
        history.append(quote)
        window = _number(self.rules.get("observation_seconds"), 300)
        if self.style not in RESEARCH_STYLES:
            while len(history) > 1 and history[1].observed_at <= quote.observed_at - window:
                history.pop(0)
            return
        cutoff = quote.observed_at - max(window, 21600) - max(gap, 180)
        self.histories[quote.mint] = [point for point in history if point.observed_at >= cutoff]
