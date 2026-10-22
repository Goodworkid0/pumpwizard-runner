from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from pumpwizard_runner.decision import Decision, MarketQuote
from pumpwizard_runner.paper_research import (
    PaperResearchError,
    PaperResearchStore,
    PaperResearchWorker,
    START_CONFIRMATION,
    _portfolio_quote_event,
)
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder
from pumpwizard_runner.strategies import parse_package


SESSION = "batch4-paper-session"


def package(name: str = "batch4-paper", *, order_usd: float = 10) -> dict:
    configuration = copy.deepcopy(CONFIG)
    configuration.update({
        "entry_style": "momentum",
        "order_usd": order_usd,
        "observation_seconds": 60,
        "quote_max_age_seconds": 120,
    })
    return {
        "schema": "pumpwizard.strategy-handoff/v1",
        "strategy": {"id": name, "label": name},
        "configuration": configuration,
    }


def quote(at: float, *, mint: str = "Mint", price_usd: float = 1,
          source_at: float | None = None) -> MarketQuote:
    source = at if source_at is None else source_at
    return MarketQuote(
        mint, mint[-4:], at, "curve", "Pool-" + mint,
        20_000, price_usd, price_usd / 100, 5_000, 50,
        source="fixture", quote_usd=100, base_reserve=1_000_000,
        reserve_usd=50_000, real_token_reserve=500_000,
        supply=1_000_000_000, decimals=6, source_at=source, fx_at=source,
    )


def evidence(ident: str, *, event_at: float, received_at: float,
             mint: str = "Mint", price_usd: float = 1,
             source_at: float | None = None) -> dict:
    source = event_at if source_at is None else source_at
    return {
        "id": ident,
        "mint": mint,
        "route": "curve",
        "pool": "Pool-" + mint,
        "observed_at": event_at,
        "source_at": source,
        "source_time_basis": "provider",
        "first_received_at": received_at,
        "first_known_basis": "recorder-first-receipt",
        "run_ids": ["collection"],
        "payload": quote(
            event_at, mint=mint, price_usd=price_usd, source_at=source,
        ).as_record(),
    }


def start(store: PaperResearchStore, *, key: str = "paper-key",
          selected: dict | None = None, capital: float = 500,
          start_at: float | None = None) -> dict:
    strategy = selected or package()
    return store.start(
        source_session_id=SESSION,
        strategy_package=strategy,
        strategy_sha256=parse_package(strategy).digest,
        simulated_capital_usd=capital,
        start_at=start_at,
        idempotency_key=key,
        confirmation=START_CONFIRMATION,
    )


class EachMintEvaluator:
    def __init__(self, _package: dict):
        self.seen: set[str] = set()

    def restore(self, rows):
        self.seen.update(row["mint"] for row in rows)

    def restore_decisions(self, _rows):
        pass

    def evaluate(self, quotes, at, *, allow_entries):
        decisions = []
        for selected in quotes:
            if selected.mint not in self.seen:
                decisions.append(Decision(
                    "entry_recommended", selected.mint, at,
                    "prospective fixture", {"price_usd": selected.price_usd},
                ))
            self.seen.add(selected.mint)
        return decisions


def test_start_retry_ignores_changed_service_clock_and_is_concurrent(tmp_path):
    ticks = iter([100.0, 999.0, 1_500.0])
    store = PaperResearchStore(tmp_path, clock=lambda: next(ticks))
    try:
        first = start(store, start_at=None)
        retry = start(store, start_at=None)
        assert retry["id"] == first["id"]
        assert retry["start_at"] == 100
        with pytest.raises(PaperResearchError, match="different immutable settings"):
            start(store, start_at=None, capital=501)
        fresh = start(store, key="explicit-new-run", start_at=None)
        assert fresh["id"] != first["id"]
        assert fresh["start_at"] == 999
    finally:
        store.close()

    concurrent_key = "concurrent-delivery-retry"

    def create() -> str:
        local = PaperResearchStore(tmp_path, clock=lambda: 2_000.0)
        try:
            return start(local, key=concurrent_key, start_at=None)["id"]
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=6) as executor:
        ids = list(executor.map(lambda _index: create(), range(12)))
    assert len(set(ids)) == 1
    verify = PaperResearchStore(tmp_path)
    try:
        assert len([row for row in verify.runs() if row["id"] == ids[0]]) == 1
    finally:
        verify.close()


def test_pause_resume_retains_backlog_without_fabricating_decisions(tmp_path):
    clock = [100.0]
    store = PaperResearchStore(tmp_path, clock=lambda: clock[0])
    evaluator = EachMintEvaluator(package())
    try:
        run = start(store, start_at=100)
        assert store.append_evidence(
            run["id"], evidence("before", event_at=90, received_at=99, mint="Before"),
            evaluator,
        ) == 1
        assert store.append_evidence(
            run["id"], evidence("live-one", event_at=100, received_at=101, mint="LiveOne"),
            evaluator,
        ) == 1
        clock[0] = 110
        assert store.command(run["id"], action="pause")["state"] == "paused"
        paused = evidence("paused", event_at=119, received_at=120, mint="Paused")
        assert store.append_evidence(run["id"], paused, evaluator) == 0
        clock[0] = 200
        assert store.command(run["id"], action="resume")["state"] == "running"
        assert store.append_evidence(run["id"], paused, evaluator) == 1
        assert store.append_evidence(
            run["id"], evidence("live-two", event_at=200, received_at=201, mint="LiveTwo"),
            evaluator,
        ) == 1

        history = store.history(run["id"])
        assert [row["evidence_id"] for row in history["decisions"]] == [
            "live-one", "live-two",
        ]
        quality = {
            row["evidence_id"]: row["quality"] for row in history["observations"]
        }
        assert "before-paper-start" in quality["before"]["reasons"]
        assert "outside-operating-interval" in quality["paused"]["reasons"]
        assert len(history["operating_intervals"]) == 2
        assert all(row["ended_at"] is not None for row in history["operating_intervals"][:1])
        pause_gap = next(row for row in history["gaps"] if row["gap_id"].startswith("pause:"))
        assert (pause_gap["started_at"], pause_gap["ended_at"]) == (110, 200)
    finally:
        store.close()


def test_low_price_is_exact_and_quality_exclusions_advance_cursor(tmp_path):
    exact = _portfolio_quote_event({
        "evidence_id": "sub-micro",
        "event_at": 100,
        "first_received_at": 101,
        "source_at": 100,
        "source_time_basis": "provider",
        "mint": "Tiny",
        "route": "curve",
        "pool": "Pool-Tiny",
        "decimals": 6,
        "payload": quote(100, mint="Tiny", price_usd=1e-9).as_record(),
    })
    assert exact["price_quote_units_per_token"] == 1_000_000_000
    assert "price_micros_per_token" not in exact

    store = PaperResearchStore(tmp_path, clock=lambda: 1_000)
    evaluator = EachMintEvaluator(package())
    try:
        run = start(store, start_at=0)
        portfolio_evaluator = store.restore_portfolio_evaluator(run["id"])
        exact_low = evidence(
            "exact-low", event_at=50, received_at=51,
            mint="ExactLow", price_usd=1e-9,
        )
        too_small = evidence(
            "too-small", event_at=100, received_at=101,
            mint="TooSmall", price_usd=1e-19,
        )
        stale = evidence(
            "stale", event_at=200, received_at=500,
            source_at=200, mint="Stale",
        )
        malformed = evidence("malformed", event_at=600, received_at=601, mint="Broken")
        malformed["payload"].pop("mint")
        valid = evidence("valid", event_at=700, received_at=701, mint="Valid")
        for row in (exact_low, too_small, stale, malformed, valid):
            assert store.append_evidence(
                run["id"], row, evaluator, portfolio_evaluator,
            ) == 1

        history = store.history(run["id"])
        by_id = {row["evidence_id"]: row for row in history["observations"]}
        assert by_id["too-small"]["quality"]["reasons"] == [
            "unsupported-price-precision",
        ]
        assert by_id["exact-low"]["quality"]["status"] == "eligible"
        assert "stale-market-evidence" in by_id["stale"]["quality"]["reasons"]
        assert by_id["malformed"]["quality"]["reasons"] == ["invalid-market-quote"]
        assert by_id["valid"]["quality"]["status"] == "eligible"
        assert [row["evidence_id"] for row in history["decisions"]] == [
            "exact-low", "valid",
        ]
        overview = store.run(run["id"])
        assert overview["cursor"] == [701, "valid"]
        assert overview["counts"]["exclusions"] == 3
        assert overview["fault"] is None
    finally:
        store.close()


def test_faulted_run_does_not_starve_healthy_run(tmp_path):
    clock = [100.0]
    recorder = ServiceEvidenceRecorder(tmp_path, clock=lambda: clock[0])
    bad_package = package("a-bad-paper")
    good_package = package("z-good-paper")
    store = PaperResearchStore(tmp_path, clock=lambda: clock[0])
    try:
        bad = start(store, key="bad", selected=bad_package, start_at=0)
        good = start(store, key="good", selected=good_package, start_at=0)
    finally:
        store.close()
    scan = recorder.begin_scan(minimum_cap=1, maximum_cap=100_000, source="fixture", limit=10)
    recorder.complete_scan(scan, [quote(90, mint="Healthy")], received_at=100)

    class Exploding(EachMintEvaluator):
        def evaluate(self, quotes, at, *, allow_entries):
            raise RuntimeError("fixture evaluator corruption")

    def factory(selected):
        if selected["strategy"]["id"] == "a-bad-paper":
            return Exploding(selected)
        return EachMintEvaluator(selected)

    worker = PaperResearchWorker(
        tmp_path, recorder, evaluator_factory=factory, clock=lambda: clock[0],
    )
    try:
        result = worker.run_once()
        assert result["runs"] == 2
        assert result["faults"] == 1
        assert worker.store.run(bad["id"])["state"] == "interrupted"
        assert worker.store.run(bad["id"])["fault"]["evidence_id"]
        assert worker.store.run(good["id"])["state"] == "running"
        assert worker.store.run(good["id"])["counts"]["observations"] == 1
        assert worker.store.run(good["id"])["counts"]["decisions"] == 1
    finally:
        worker.close()
        recorder.close()


def test_service_recovery_requires_resume_and_marks_downtime_gap(tmp_path):
    clock = [100.0]
    store = PaperResearchStore(tmp_path, clock=lambda: clock[0])
    try:
        run = start(store, start_at=100)
        clock[0] = 150
        assert store.recover_running() == 1
        interrupted = store.run(run["id"])
        assert interrupted["state"] == "interrupted"
        clock[0] = 200
        store.command(run["id"], action="resume")
        evaluator = EachMintEvaluator(package())
        store.append_evidence(
            run["id"], evidence("offline", event_at=160, received_at=170, mint="Offline"),
            evaluator,
        )
        store.append_evidence(
            run["id"], evidence("online", event_at=200, received_at=201, mint="Online"),
            evaluator,
        )
        history = store.history(run["id"])
        assert [row["evidence_id"] for row in history["decisions"]] == ["online"]
        downtime = next(
            row for row in history["gaps"]
            if row["gap_id"].startswith("service-interruption:")
        )
        assert downtime["ended_at"] == 200
    finally:
        store.close()


def test_worker_honors_cycle_cancellation_and_resumes_from_durable_cursor(tmp_path):
    recorder = ServiceEvidenceRecorder(tmp_path, clock=lambda: 500)
    store = PaperResearchStore(tmp_path, clock=lambda: 500)
    try:
        run = start(store, start_at=0)
    finally:
        store.close()
    scan = recorder.begin_scan(
        minimum_cap=1, maximum_cap=100_000, source="fixture", limit=10,
        requested_at=490, started_at=490,
    )
    recorder.complete_scan(
        scan,
        [quote(490 + index, mint=f"Mint{index}") for index in range(3)],
        received_at=500,
    )
    calls = [0]

    def cancel_after_one() -> bool:
        calls[0] += 1
        return calls[0] >= 3

    worker = PaperResearchWorker(
        tmp_path, recorder, evaluator_factory=EachMintEvaluator, clock=lambda: 500,
    )
    try:
        first = worker.run_once(should_stop=cancel_after_one)
        assert first["observations"] == 1
        assert worker.store.run(run["id"])["counts"]["observations"] == 1
        second = worker.run_once()
        assert second["observations"] == 2
        assert worker.store.run(run["id"])["counts"]["observations"] == 3
    finally:
        worker.close()
        recorder.close()


def test_bounded_history_returns_latest_rows_in_chronological_order(tmp_path):
    store = PaperResearchStore(tmp_path, clock=lambda: 1_000)
    evaluator = EachMintEvaluator(package())
    try:
        run = start(store, start_at=0)
        for index in range(4):
            store.append_evidence(
                run["id"], evidence(
                    f"row-{index}", event_at=100 + index,
                    received_at=200 + index, mint=f"Mint{index}",
                ), evaluator,
            )
        history = store.history(run["id"], limit=2)
        assert [row["evidence_id"] for row in history["observations"]] == [
            "row-2", "row-3",
        ]
        assert [row["evidence_id"] for row in history["decisions"]] == [
            "row-2", "row-3",
        ]
    finally:
        store.close()
