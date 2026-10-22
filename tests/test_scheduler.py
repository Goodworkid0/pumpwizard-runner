import json
from pathlib import Path

import pytest

from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.market import MarketDataError
from pumpwizard_runner.scheduler import MonitorScheduler
from pumpwizard_runner.runtime import LocalMonitor
from pumpwizard_runner.strategies import parse_package


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("pumpwizard_runner.scheduler.time.time", lambda: clock[0])
    library = StrategyLibrary(tmp_path)
    yield library, clock
    library.close()


def agent(library, low=10000):
    package = json.loads((Path(__file__).parents[1] / "examples/xyz-local-strategy.json").read_text())
    package["rules"]["entry_cap_min_usd"] = low
    strategy = parse_package(package)
    library.import_strategy(strategy)
    ident = library.create_instance(strategy.digest)
    library.set_tracking(ident, True)
    return ident


def quote(at):
    return MarketQuote("Mint", "TEST", at, "curve", "Curve", 15000, 1, .01,
                       1000 + at, 10 + at / 100, quote_usd=100)


class Source:
    def __init__(self, clock, callback=None):
        self.clock, self.callback, self.calls = clock, callback, []

    def collect(self, low, high, *, limit, priority):
        self.calls.append((low, high, limit, priority))
        if self.callback:
            self.callback(low)
        return [quote(self.clock[0])]


def test_shared_scan_independent_state_and_restart(setup, tmp_path):
    library, clock = setup
    first, second = agent(library), agent(library)
    source = Source(clock)
    scheduler = MonitorScheduler(tmp_path, lambda: source)
    assert scheduler.run_cycle(library) == 2
    assert len(source.calls) == 1
    assert scheduler.run_cycle(library) == 0
    assert library.monitor_checkpoint(first) == library.monitor_checkpoint(second)
    library.set_tracking(first, False)
    library.release_monitor_lease(scheduler.owner)
    # Reopen SQLite and construct a new scheduler, as after an app restart.
    reopened = StrategyLibrary(tmp_path)
    try:
        restarted = MonitorScheduler(tmp_path, lambda: source)
        clock[0] += 15
        assert restarted.run_cycle(reopened) == 1
        assert len(reopened.monitor_runs(first)) == 1
        assert len(reopened.monitor_runs(second)) == 2
        assert len(reopened.decisions(second)) == 1  # unchanged rejection is not repeated
        assert not reopened.tracking_settings(first)["enabled"]
        assert reopened.tracking_settings(second)["enabled"]
    finally:
        reopened.close()


@pytest.mark.parametrize("cli_pause", [False, True])
def test_pause_during_scan_invalidates_only_that_agent(setup, tmp_path, cli_pause):
    library, clock = setup
    first, second = agent(library), agent(library)

    def pause(_):
        other = StrategyLibrary(tmp_path)
        try:
            if cli_pause:
                other.set_instance_status(first, "paused")
            else:
                other.set_tracking(first, False)
        finally:
            other.close()

    source = Source(clock, pause)
    assert MonitorScheduler(tmp_path, lambda: source).run_cycle(library) == 1
    assert library.recent_observations(first) == []
    assert library.monitor_checkpoint(first) is None
    assert len(library.recent_observations(second)) == 1


def test_paused_agent_keeps_exit_monitoring_and_prioritizes_holdings(setup, tmp_path):
    library, clock = setup
    ident = agent(library)
    library.record_position(ident, "Mint", "TEST", quantity=1, total_cost_usd=2, opened_at=900)
    library.set_tracking(ident, False)
    source = Source(clock)
    assert MonitorScheduler(tmp_path, lambda: source).run_cycle(library) == 1
    assert source.calls[0][3] == {"Mint"}
    assert [d["kind"] for d in library.decisions(ident)] == ["exit_recommended"]


def test_provider_failure_isolated_and_retried(setup, tmp_path):
    library, clock = setup
    failing, healthy = agent(library), agent(library, 12000)

    def fail(low):
        if low == 10000:
            raise MarketDataError("offline")

    source = Source(clock, fail)
    scheduler = MonitorScheduler(tmp_path, lambda: source)
    assert scheduler.run_cycle(library) == 1
    assert library.monitor_runs(failing)[0]["status"] == "failed"
    assert library.monitor_runs(healthy)[0]["status"] == "completed"
    assert not library.tracking_settings(failing)["fault"]
    assert scheduler.run_cycle(library) == 0
    clock[0] += 15
    source.callback = None
    assert scheduler.run_cycle(library) == 2


def test_local_fault_requires_explicit_retry(setup, tmp_path):
    library, clock = setup
    ident = agent(library)

    def fail(_):
        raise ValueError("invalid adapter state")

    source = Source(clock, fail)
    scheduler = MonitorScheduler(tmp_path, lambda: source)
    assert scheduler.run_cycle(library) == 0
    assert "invalid adapter state" in library.tracking_settings(ident)["fault"]
    clock[0] += 60
    source.callback = None
    assert scheduler.run_cycle(library) == 0
    library.set_tracking(ident, True)
    assert scheduler.run_cycle(library) == 1


def test_lease_exclusion_and_takeover_discard_old_batch(setup, tmp_path):
    library, clock = setup
    ident = agent(library)
    source = Source(clock)
    first, second = MonitorScheduler(tmp_path, lambda: source), MonitorScheduler(tmp_path, lambda: source)
    assert library.claim_monitor_lease(first.owner, clock[0])
    assert second.run_cycle(library) == 0
    assert source.calls == []
    with pytest.raises(ValueError, match="another process"):
        LocalMonitor(library, ident, source).run_once()
    assert source.calls == []

    def takeover(_):
        other = StrategyLibrary(tmp_path)
        try:
            clock[0] += 181
            assert other.claim_monitor_lease(second.owner, clock[0])
        finally:
            other.close()

    source.callback = takeover
    assert first.run_cycle(library) == 0
    assert library.monitor_runs(ident) == []
    source.callback = None
    assert second.run_cycle(library) == 1


def test_stop_during_scan_discards_batch_without_disabling_preference(setup, tmp_path):
    library, clock = setup
    ident = agent(library)
    source = Source(clock)
    scheduler = MonitorScheduler(tmp_path, lambda: source)
    source.callback = lambda _: scheduler.stop()
    assert scheduler.run_cycle(library) == 0
    assert library.monitor_runs(ident) == []
    assert library.tracking_settings(ident)["enabled"]


def test_cycle_commit_rolls_back_all_outputs(setup):
    library, clock = setup
    ident = agent(library)
    with pytest.raises(ValueError):
        library.commit_monitor_cycle(ident, quotes=[quote(clock[0])], decisions=[],
            checkpoint={"invalid": float("nan")}, started_at=clock[0], finished_at=clock[0], marks=[])
    assert library.recent_observations(ident) == []
    assert library.monitor_runs(ident) == []
    assert library.monitor_checkpoint(ident) is None


def test_all_held_mints_collected_in_bounded_chunks(setup, tmp_path):
    library, clock = setup
    ident = agent(library)
    for number in range(65):
        library.record_position(ident, f"Mint{number}", "TEST", quantity=1, total_cost_usd=1, opened_at=900)
    source = Source(clock)
    assert MonitorScheduler(tmp_path, lambda: source).run_cycle(library) == 1
    assert len(source.calls) == 3
    assert set.union(*(call[3] for call in source.calls)) == {f"Mint{i}" for i in range(65)}
    assert all(len(call[3]) <= 30 and call[2] <= 100 for call in source.calls)


def test_schema_eight_upgrade_preserves_library_and_requires_start(tmp_path):
    library = StrategyLibrary(tmp_path)
    ident = agent(library)
    library.record_position(ident, "Mint", "TEST", quantity=1, total_cost_usd=1, opened_at=900)
    with library._connection:
        for table in ("monitor_settings", "monitor_checkpoints", "monitor_lease"):
            library._connection.execute(f"DROP TABLE {table}")
        library._connection.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
    library.close()
    upgraded = StrategyLibrary(tmp_path)
    try:
        assert upgraded.instance(ident)["label"] == "XYZ momentum example"
        assert len(upgraded.positions(ident)) == 1
        assert not upgraded.tracking_settings(ident)["enabled"]
        assert upgraded.scheduled_instances()[0]["held"] == 1
    finally:
        upgraded.close()
