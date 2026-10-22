#!/usr/bin/env python3
"""Bounded synthetic forward-paper timing/cursor acceptance for Batch 4."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import resource
import tempfile
import time

from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.paper_research import (
    MAX_OBSERVATIONS_PER_CYCLE,
    PaperResearchStore,
    PaperResearchWorker,
    START_CONFIRMATION,
)
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder
from pumpwizard_runner.strategies import parse_package


class NoopEvaluator:
    def __init__(self, _package: dict[str, object]):
        pass

    def restore(self, _rows) -> None:
        pass

    def restore_decisions(self, _rows) -> None:
        pass

    def evaluate(self, _quotes, _at, *, allow_entries: bool) -> list[object]:
        if not allow_entries:
            raise AssertionError("Acceptance evidence must be inside the operating interval")
        return []


def _package() -> dict[str, object]:
    configuration = copy.deepcopy(CONFIG)
    configuration.update({
        "entry_style": "momentum", "order_usd": 10,
        "observation_seconds": 60, "quote_max_age_seconds": 120,
    })
    return {
        "schema": "pumpwizard.strategy-handoff/v1",
        "strategy": {"id": "batch4-cap-acceptance", "label": "Batch 4 cap acceptance"},
        "configuration": configuration,
    }


def _quote(index: int, at: float) -> MarketQuote:
    mint = f"FixtureMint{index:05d}"
    return MarketQuote(
        mint, f"F{index:04d}", at, "curve", "Pool-" + mint,
        20_000 + index, 1 + index / 1_000_000, 0.01,
        5_000, 50, source="batch4-cap-fixture", quote_usd=100,
        base_reserve=1_000_000, reserve_usd=50_000,
        real_token_reserve=500_000, supply=1_000_000_000, decimals=6,
        source_at=at, fx_at=at,
    )


def run(root: Path, rows: int) -> dict[str, object]:
    if rows <= MAX_OBSERVATIONS_PER_CYCLE:
        raise ValueError("Acceptance rows must exceed the per-cycle cap")
    now = 2_000.0
    selected = _package()
    with PaperResearchStore(root, clock=lambda: now) as paper:
        paper_run = paper.start(
            source_session_id="batch4-cap-session",
            strategy_package=selected,
            strategy_sha256=parse_package(selected).digest,
            simulated_capital_usd=500,
            start_at=now,
            idempotency_key="batch4-cap-acceptance",
            confirmation=START_CONFIRMATION,
        )
    recorder = ServiceEvidenceRecorder(root, clock=lambda: now)
    try:
        for offset in range(0, rows, 100):
            page = [_quote(index, now - 1) for index in range(offset, min(rows, offset + 100))]
            scan = recorder.begin_scan(
                minimum_cap=1, maximum_cap=1_000_000,
                source="batch4-cap-fixture", limit=min(100, len(page)),
                requested_at=now, started_at=now,
            )
            recorder.complete_scan(scan, page, received_at=now)
        worker = PaperResearchWorker(
            root, recorder, evaluator_factory=NoopEvaluator, clock=lambda: now,
        )
        try:
            started = time.perf_counter()
            first = worker.run_once()
            first_seconds = time.perf_counter() - started
            after_first = worker.store.run(paper_run["id"])
            started = time.perf_counter()
            second = worker.run_once()
            second_seconds = time.perf_counter() - started
            final = worker.store.run(paper_run["id"])
            history = worker.store.history(paper_run["id"], limit=1_000)
        finally:
            worker.close()
    finally:
        recorder.close()
    return {
        "schema": "pumpwizard.research-batch4-acceptance/v1",
        "synthetic": True,
        "paper": {
            "input_rows": rows,
            "per_cycle_cap": MAX_OBSERVATIONS_PER_CYCLE,
            "first_cycle": first,
            "first_cycle_seconds": round(first_seconds, 6),
            "rows_after_first_cycle": after_first["counts"]["observations"],
            "second_cycle": second,
            "second_cycle_seconds": round(second_seconds, 6),
            "final_rows": final["counts"]["observations"],
            "input_cap_gaps": sum(
                row["gap_id"].startswith("input-cap:") for row in history["gaps"]
            ),
            "cursor": final["cursor"],
            "fault": final["fault"],
        },
        "resources": {
            "paper_database_bytes": (root / "paper-research.sqlite").stat().st_size,
            "recorder_database_bytes": (root / "service-evidence.sqlite").stat().st_size,
            "process_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
        "authority": {
            "wallet_allocation": False, "signer": False,
            "orders": False, "submission": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=1_005)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if args.data_dir is not None:
        args.data_dir.mkdir(parents=True, exist_ok=False)
        result = run(args.data_dir, args.rows)
    else:
        with tempfile.TemporaryDirectory(prefix="pumpwizard-batch4-paper-") as directory:
            result = run(Path(directory), args.rows)
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
