import json
from pathlib import Path

from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.market import MarketDataError
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.runtime import LocalMonitor
from pumpwizard_runner.strategies import parse_package


ROOT = Path(__file__).parents[1]


class SequenceSource:
    def __init__(self, quotes: list[MarketQuote]):
        self.quotes = quotes
        self.index = 0
        self.priorities: list[set[str]] = []

    def collect(self, minimum_cap_usd: float, maximum_cap_usd: float, limit: int,
                priority: set[str] | None = None) -> list[MarketQuote]:
        self.priorities.append(priority or set())
        quote = self.quotes[self.index]
        self.index += 1
        return [quote]


class FailingSource:
    def collect(self, minimum_cap_usd: float, maximum_cap_usd: float, limit: int,
                priority: set[str] | None = None) -> list[MarketQuote]:
        raise MarketDataError("temporary source outage")


class FlakySource:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self, minimum_cap_usd: float, maximum_cap_usd: float, limit: int,
                priority: set[str] | None = None) -> list[MarketQuote]:
        self.calls += 1
        if self.calls == 1:
            raise MarketDataError("temporary source outage")
        return [_quote(100, 1.00)]


class ClearOwnershipSource:
    def __init__(self) -> None:
        self.calls = 0

    def check(self, mint: str, now: float) -> dict[str, object]:
        self.calls += 1
        return {
            "mint": mint, "checked_at": now, "policy_id": "test-ownership-policy",
            "status": "clear", "reasons": [],
        }


class BlockThenUnknownOwnershipSource:
    def __init__(self) -> None:
        self.calls = 0

    def check(self, mint: str, now: float) -> dict[str, object]:
        self.calls += 1
        return {
            "mint": mint, "checked_at": now, "policy_id": "test-ownership-policy",
            "status": "blocked" if self.calls == 1 else "unknown",
            "reasons": ["flagged" if self.calls == 1 else "RPC unavailable"],
        }


def _quote(at: float, price: float) -> MarketQuote:
    return MarketQuote(mint="MintABC", symbol="ABC", observed_at=at, route="curve", pool="CurveABC",
                       market_cap_usd=15_000, price_usd=price * 100, price_native=price,
                       liquidity_usd=(100 + at / 20) * 100, liquidity_native=100 + at / 20,
                       quote_usd=100, base_reserve=1_000_000, reserve_usd=10_000,
                       real_token_reserve=500_000, supply=1_000_000_000, decimals=6)


def test_one_cycle_monitor_restores_local_observations_between_processes(tmp_path: Path, monkeypatch):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        source = SequenceSource([_quote(100, 1.00), _quote(120, 1.01),
                                 _quote(140, 1.02), _quote(160, 1.03)])
        clock = [at for at in (100.0, 120.0, 140.0, 160.0) for _ in range(4)]
        monkeypatch.setattr("pumpwizard_runner.runtime.time.time", lambda: clock.pop(0))
        results = [LocalMonitor(library, instance_id, source).run_once() for _ in range(4)]
        assert [result.decisions for result in results] == [1, 0, 0, 1]
        assert len(library.recent_observations(instance_id)) == 4
        assert library.decisions(instance_id)[0]["kind"] == "entry_recommended"
        assert source.priorities == [set(), set(), set(), set()]
        assert [run["status"] for run in library.monitor_runs(instance_id)] == ["completed"] * 4
    finally:
        library.close()


def test_monitor_atomically_captures_every_entry_opportunity_and_decision_features(tmp_path: Path):
    strategy = parse_package({
        "schema": "pumpwizard.strategy-handoff/v1",
        "strategy": {"id": "core-frequency", "label": "Core"},
        "configuration": CONFIG,
    })
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        monitor = LocalMonitor(library, instance_id, SequenceSource([]))
        for at, price in ((100, 1.00), (120, 1.01), (140, 1.02), (160, 1.03), (180, 1.04)):
            result = monitor.process_quotes([_quote(at, price)], at - 1, at)
            assert result is not None

        snapshot = library.session_review_snapshot(instance_id)
        assert snapshot["decision_capture_complete"] is True
        assert len(snapshot["observations"]) == 5
        assert len(snapshot["decisions"]) == 5
        assert len(snapshot["feature_records"]) == 5
        assert {row["decision_key"] for row in snapshot["decisions"]} == {
            row["decision_key"] for row in snapshot["feature_records"]
        }
        assert all(row["instance_id"] == instance_id for row in snapshot["decisions"])
    finally:
        library.close()


def test_monitor_persists_market_failure_then_continues_on_schedule(tmp_path: Path):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        monitor = LocalMonitor(library, instance_id, FlakySource())
        cycles = []
        waits = []
        assert monitor.run_forever(5, sleep=waits.append, on_cycle=cycles.append, max_cycles=2) == 2
        assert cycles[0].result is None
        assert cycles[0].error == "temporary source outage"
        assert cycles[1].result is not None
        assert waits == [5]
        assert [run["status"] for run in library.monitor_runs(instance_id)] == ["completed", "failed"]
        assert library.monitor_health(instance_id)["last_run"]["status"] == "completed"
    finally:
        library.close()


def test_paused_monitor_does_not_collect_another_market_cycle(tmp_path: Path):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        source = FailingSource()
        library.set_instance_status(instance_id, "paused")
        assert LocalMonitor(library, instance_id, source).run_forever(5, max_cycles=1) == 0
        assert library.monitor_runs(instance_id) == []
    finally:
        library.close()


def test_strict_monitor_reuses_fresh_locally_recorded_ownership_evidence(tmp_path: Path, monkeypatch):
    package = json.loads((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    package["rules"]["entry_policy"] = "strict"
    strategy = parse_package(package)
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        source = SequenceSource([_quote(100, 1.00), _quote(120, 1.01),
                                 _quote(140, 1.02), _quote(160, 1.03)])
        ownership = ClearOwnershipSource()
        clock = [at for at in (100.0, 120.0, 140.0, 160.0) for _ in range(4)]
        monkeypatch.setattr("pumpwizard_runner.runtime.time.time", lambda: clock.pop(0))
        results = [LocalMonitor(library, instance_id, source, ownership).run_once() for _ in range(4)]
        assert [result.decisions for result in results] == [1, 0, 0, 1]
        assert ownership.calls == 1
        assert library.latest_ownership(instance_id, "MintABC")["status"] == "clear"
    finally:
        library.close()


def test_prior_ownership_block_remains_active_when_a_refresh_is_unknown(tmp_path: Path, monkeypatch):
    package = json.loads((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    package["rules"].update(entry_policy="strict", ownership_ttl_seconds=120)
    strategy = parse_package(package)
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        source = SequenceSource([_quote(100, 1.00), _quote(221, 1.01)])
        ownership = BlockThenUnknownOwnershipSource()
        clock = [at for at in (100.0, 221.0) for _ in range(4)]
        monkeypatch.setattr("pumpwizard_runner.runtime.time.time", lambda: clock.pop(0))
        LocalMonitor(library, instance_id, source, ownership).run_once()
        LocalMonitor(library, instance_id, source, ownership).run_once()
        latest = library.latest_ownership(instance_id, "MintABC")
        assert latest["status"] == "blocked"
        assert "Previous ownership flags remain unresolved" in latest["reasons"]
        assert ownership.calls == 2
    finally:
        library.close()
