"""Local monitoring orchestration, without a hosted-service dependency."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import time
import uuid
from typing import Callable, Iterable

from .decision import DecisionEngine, MarketQuote, PositionView
from .library import StrategyLibrary
from .market import MarketDataError, MarketSource
from .ownership import OwnershipSource
from .session_review import SessionReviewError, capture_decision_features
from .strategies import parse_package


@dataclass(frozen=True)
class MonitorResult:
    instance_id: str
    observations: int
    decisions: int
    at: float


@dataclass(frozen=True)
class MonitorCycle:
    result: MonitorResult | None
    error: str | None = None


class LocalMonitor:
    """Run a strategy instance using only its local state and selected provider."""

    def __init__(self, library: StrategyLibrary, instance_id: str, source: MarketSource,
                 ownership_source: OwnershipSource | None = None):
        self.library = library
        self.instance = library.instance(instance_id)
        self.instance_id = instance_id
        self.source = source
        self.ownership_source = ownership_source
        self.strategy = parse_package(self.instance["package"])
        self.engine = DecisionEngine(self.strategy)
        # The longest current research window is six hours. Restore a little
        # extra time to preserve valid boundary samples after a restart.
        self.engine.restore(library.recent_observations(instance_id, time.time() - 21900))
        checkpoint = library.monitor_checkpoint(instance_id)
        if checkpoint:
            self.engine.restore_checkpoint(checkpoint)
        else:
            self.engine.restore_decisions(reversed(library.decisions(instance_id, 1000)))

    def run_once(self, positions: Iterable[PositionView] = ()) -> MonitorResult:
        self.instance = self.library.instance(self.instance_id)
        if self.instance["status"] != "monitoring":
            raise ValueError("This local strategy instance is paused")
        started_at = time.time()
        owner = uuid.uuid4().hex
        if not self.library.claim_monitor_lease(owner, started_at):
            raise ValueError("Monitoring is already managed by another process for this app directory")
        try:
            revision = self.library.tracking_settings(self.instance_id)["revision"]
            rules = self.strategy.effective_rules
            lower = float(rules.get("entry_cap_min", rules.get("entry_cap_min_usd")))
            upper = float(rules.get("entry_cap_max", rules.get("entry_cap_max_usd")))
            ledger_positions = list(positions) or self.library.position_views(self.instance_id)
            quotes = self.source.collect(lower, upper, limit=min(100, max(24, int(rules.get("max_positions", 1)) * 3)),
                                         priority={position.mint for position in ledger_positions})
            finished_at = time.time()
            result = self.process_quotes(quotes, started_at, finished_at, positions=ledger_positions,
                                         revision=revision, owner=owner)
            if result is None:
                raise ValueError("Monitor cycle cancelled after a pause or scheduler lease change")
            return result
        except Exception as exc:
            finished_at = time.time()
            self.library.record_monitor_run(
                self.instance_id, started_at=started_at, finished_at=finished_at, status="failed",
                detail=_error_detail(exc),
            )
            raise
        finally:
            self.library.release_monitor_lease(owner)

    def process_quotes(self, quotes: list[MarketQuote], started_at: float, finished_at: float, *,
                       positions: Iterable[PositionView] | None = None, allow_entries: bool = True,
                       revision: int | None = None, owner: str | None = None) -> MonitorResult | None:
        """Evaluate a shared scan with this agent's own durable state."""
        quotes = [quote for quote in quotes if self.engine.quote_usable(quote, finished_at)]
        quote_map = {quote.mint: quote for quote in quotes}
        ledger_positions = list(positions) if positions is not None else self.library.position_views(self.instance_id)
        current_positions, marks = [], []
        for position in ledger_positions:
            quote = quote_map.get(position.mint)
            usable = quote is not None and (position.valuation_basis == 'observed' or self.strategy.profile != "ladder-v1" or
                                            self.engine.ladder_sell_value(quote, position) is not None)
            if usable:
                marks.append(quote)
                position = replace(position, peak_price_usd=max(position.peak_price_usd, quote.price_usd),
                                   last_usable_at=quote.observed_at)
            current_positions.append(position)
        evidence = self._ownership_evidence(quotes, finished_at)
        opportunities: list[dict[str, object]] = []
        decisions = self.engine.evaluate(
            quotes, finished_at, evidence=evidence, positions=current_positions,
            allow_entries=allow_entries, opportunities=opportunities,
        )
        captured_features = []
        history = [quote.as_record() for mint in sorted(self.engine.histories)
                   for quote in self.engine.histories[mint] if quote.observed_at <= finished_at]
        for opportunity in opportunities:
            decision_key = _opportunity_key(
                self.instance_id, self.strategy.digest, opportunity,
            )
            opportunity["decision_key"] = decision_key
            opportunity["evaluation_key"] = decision_key
            opportunity["eligible"] = opportunity["kind"] == "entry_recommended"
            opportunity["strategy_digest"] = self.strategy.digest
            opportunity["feature_key"] = decision_key
            try:
                feature = capture_decision_features(
                    self.strategy.package,
                    decision_key=decision_key,
                    decision=opportunity,
                    observations=history,
                    engine_version=self.strategy.engine or "pumpwizard-core/1",
                    captured_at=finished_at,
                )
                feature["instance_id"] = self.instance_id
                captured_features.append(feature)
            except SessionReviewError:
                # Unsupported families and incomplete source evidence remain
                # explicit in the capture-cycle marker; monitoring still commits.
                continue
        saved = self.library.commit_monitor_cycle(self.instance_id, quotes=quotes,
            decisions=[decision.as_record() for decision in decisions], checkpoint=self.engine.checkpoint(),
            started_at=started_at, finished_at=finished_at, marks=marks, revision=revision, owner=owner,
            opportunities=opportunities, features=captured_features,
            capture_complete=bool(allow_entries and opportunities
                                  and len(captured_features) == len(opportunities)))
        return MonitorResult(self.instance_id, len(quotes), len(decisions), finished_at) if saved else None

    def _ownership_evidence(self, quotes: list[MarketQuote], now: float) -> dict[str, dict[str, object]]:
        """Load local evidence and refresh a bounded strict-policy subset."""
        ttl = float(self.strategy.effective_rules.get("ownership_ttl_seconds", 120))
        evidence: dict[str, dict[str, object]] = {}
        for quote in quotes:
            previous = self.library.latest_ownership(self.instance_id, quote.mint)
            if previous:
                evidence[quote.mint] = previous
        if self.strategy.effective_rules.get("entry_policy") != "strict" or self.ownership_source is None:
            return {mint: {"ownership": record} for mint, record in evidence.items()}
        refreshable = [quote for quote in sorted(quotes, key=lambda item: item.mint)
                       if not _fresh_ownership(evidence.get(quote.mint), now, ttl)]
        for quote in refreshable[:2]:
            mint = quote.mint
            previous = evidence.get(mint)
            try:
                checked = self.ownership_source.check(mint, now)
            except Exception:
                checked = {
                    "mint": mint, "checked_at": now, "policy_id": "ownership-basic-v1:unavailable",
                    "status": "unknown", "reasons": ["Local ownership source was unavailable"],
                }
            if not isinstance(checked, dict):
                checked = {
                    "mint": mint, "checked_at": now, "policy_id": "ownership-basic-v1:unavailable",
                    "status": "unknown", "reasons": ["Local ownership source returned invalid evidence"],
                }
            if previous and previous.get("status") == "blocked" and checked.get("status") == "unknown":
                checked = dict(checked)
                checked["status"] = "blocked"
                checked["reasons"] = list(dict.fromkeys([
                    "Previous ownership flags remain unresolved",
                    *_reasons(previous.get("reasons")), *_reasons(checked.get("reasons")),
                ]))
            self.library.record_ownership(self.instance_id, checked)
            evidence[mint] = checked
        # DecisionEngine keeps ownership under the per-mint evidence namespace
        # so future local checks can coexist without changing its contract.
        return {mint: {"ownership": record} for mint, record in evidence.items()}

    def run_forever(self, interval_seconds: float, *, sleep: Callable[[float], None] = time.sleep,
                    on_cycle: Callable[[MonitorCycle], None] | None = None,
                    max_cycles: int | None = None) -> int:
        """Keep collecting local observations until paused or interrupted.

        Public market-data failures are persisted and reported, then retried on
        the requested cadence. Configuration and database errors still stop the
        process because retrying them would conceal a local fault.
        """
        interval = float(interval_seconds)
        if not 5 <= interval <= 3600:
            raise ValueError("Monitor interval must be between 5 and 3600 seconds")
        if max_cycles is not None and (isinstance(max_cycles, bool) or max_cycles < 1):
            raise ValueError("Maximum monitor cycles must be at least one")
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            if self.library.instance(self.instance_id)["status"] != "monitoring":
                return cycles
            try:
                cycle = MonitorCycle(result=self.run_once())
            except MarketDataError as exc:
                cycle = MonitorCycle(result=None, error=str(exc))
            cycles += 1
            if on_cycle:
                on_cycle(cycle)
            if max_cycles is None or cycles < max_cycles:
                sleep(interval)
        return cycles


def _error_detail(exc: Exception) -> str:
    message = str(exc).strip()
    return (type(exc).__name__ + (": " + message if message else ""))[:500]


def _opportunity_key(instance_id: str, strategy_digest: str,
                     opportunity: dict[str, object]) -> str:
    payload = json.dumps(
        {"instance_id": instance_id, "strategy_digest": strategy_digest, **opportunity},
        allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _fresh_ownership(evidence: dict[str, object] | None, now: float, ttl: float) -> bool:
    if not evidence or not isinstance(evidence.get("checked_at"), (int, float)):
        return False
    checked_at = float(evidence["checked_at"])
    return checked_at <= now <= checked_at + ttl


def _reasons(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
