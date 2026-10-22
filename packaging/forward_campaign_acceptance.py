#!/usr/bin/env python3
"""Deterministic actual-evaluator capacity rehearsal for forward campaigns."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import resource
import tempfile
import time

from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.forward_campaigns import (
    REQUEST_SCHEMA,
    START_CONFIRMATION,
    ForwardCampaignStore,
)
from pumpwizard_runner.paper_research import PaperResearchWorker
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder
from pumpwizard_runner.strategies import parse_package


class Clock:
    def __init__(self, value: float = 10_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


def strategy(label: str, growth: float) -> dict[str, object]:
    configuration = copy.deepcopy(CONFIG)
    configuration.update({
        "observation_seconds": 15,
        "minimum_samples": 4,
        "minimum_price_changes": 2,
        "minimum_growth_fraction": growth,
        "minimum_reserve_growth_usd": 0.000001,
        "quote_max_age_seconds": 300,
        "fill_delay_seconds": 2,
        "failed_attempt_probability": 0,
        "max_positions": 3,
        "max_hold_seconds": 30,
        "targets": [
            {"market_cap_usd": 30_000, "initial_fraction": 0.4},
            {"market_cap_usd": 50_000, "initial_fraction": 0.3},
        ],
    })
    return {
        "schema": "pumpwizard.strategy-handoff/v1",
        "strategy": {"id": label.lower().replace(" ", "-"), "label": label},
        "configuration": configuration,
    }


def manifest(max_requests: int) -> dict[str, object]:
    packages = [strategy("Capacity Arm A", 0.005), strategy("Capacity Arm B", 0.01)]
    return {
        "schema": REQUEST_SCHEMA,
        "campaign_key": "batch5-actual-evaluator-capacity",
        "supersedes_campaign_id": None,
        "source": {
            "provider": "CapacityFixtureSource", "minimum_cap": 1_000,
            "maximum_cap": 150_000, "priority_mints": ["CapacityMain"],
            "limit": 100, "cadence_seconds": 1,
        },
        "arms": [{
            "id": f"arm-{index + 1}",
            "source_session_id": f"capacity-session-{index + 1}",
            "strategy_sha256": parse_package(package).digest,
            "strategy_package": package,
            "simulated_capital_usd": 500,
        } for index, package in enumerate(packages)],
        "assumptions": {
            "cost_model": "exact strategy package costs",
            "execution_model": "actual paper portfolio replay v3",
        },
        "coverage": {
            "max_source_age_seconds": 300, "minimum_independent_tokens": 2,
            "minimum_observations": 100, "maximum_gap_seconds": 60,
        },
        "duration_seconds": 3_600,
        "quotas": {
            "max_requests": max_requests, "max_active_runtime_seconds": 3_600,
            "max_storage_bytes": 64 * 1024 * 1024, "max_concurrency": 2,
        },
        "provider_cost": {
            "currency": "USD", "marginal_cost_per_request": None,
            "fixed_subscription_cost": None, "price_basis": "unknown",
        },
    }


def quote(at: float, *, mint: str, price: float, cap: float,
          source_at: float | None = None) -> MarketQuote:
    source = at if source_at is None else source_at
    return MarketQuote(
        mint, mint[-8:], at, "curve", "Pool-" + mint, cap,
        price, price / 100, 5_000 + (cap / 100), 50 + (cap / 10_000),
        source="CapacityFixtureSource", quote_usd=100,
        base_reserve=1_000_000, reserve_usd=50_000 + cap,
        real_token_reserve=500_000, supply=1_000_000_000, decimals=6,
        source_at=source, fx_at=source,
    )


def run_rehearsal(data_dir: Path) -> dict[str, object]:
    started_wall = time.perf_counter()
    clock = Clock()
    max_requests = 140
    recorder = ServiceEvidenceRecorder(data_dir, clock=clock)
    campaigns = ForwardCampaignStore(data_dir, clock=clock)
    worker = PaperResearchWorker(data_dir, recorder, clock=clock)
    campaign = campaigns.start(
        manifest(max_requests), idempotency_key="capacity-start",
        confirmation=START_CONFIRMATION,
    )
    campaign_id = campaign["id"]
    pending_receipts: list[float] = []
    queue_lags: list[float] = []

    def successful(selected: MarketQuote) -> None:
        clock.value = max(clock.value, selected.observed_at + 0.2)
        run_id = recorder.begin_scan(
            minimum_cap=1_000, maximum_cap=150_000,
            source="CapacityFixtureSource", priority=[selected.mint], limit=100,
            requested_at=selected.observed_at + 0.05,
            started_at=selected.observed_at + 0.1,
            campaign_consumers={campaign_id: 2},
        )
        completed = recorder.complete_scan(
            run_id, [selected], received_at=selected.observed_at + 0.2,
        )
        pending_receipts.append(float(completed["received_at"]))
        campaigns.record_scan(
            [campaign_id], success=True, finished_at=float(completed["received_at"]),
        )

    def drain() -> None:
        worker.run_once()
        queue_lags.extend(max(0.0, clock.value - receipt) for receipt in pending_receipts)
        pending_receipts.clear()

    try:
        # Entry window, actual fill, two target exits, and a max-hold final exit.
        main_caps = [15_000, 16_000, 17_000, 18_500, 20_000, 32_000, 33_000,
                     52_000, 53_000, 54_000, 55_000, 56_000, 57_000]
        for index, cap in enumerate(main_caps):
            successful(quote(
                10_000 + index * 5, mint="CapacityMain",
                price=1 + index * 0.02, cap=cap,
            ))
        drain()

        # Bad/stale rows are retained and explicitly excluded by the actual worker.
        successful(quote(clock.value + 5, mint="PrecisionBad", price=1e-19, cap=20_000))
        successful(quote(
            clock.value + 5, mint="StaleRow", price=1, cap=20_000,
            source_at=clock.value - 600,
        ))
        drain()

        # A provider outage creates bounded backoff and no immediate retry demand.
        failed_id = recorder.begin_scan(
            minimum_cap=1_000, maximum_cap=150_000,
            source="CapacityFixtureSource", priority=["CapacityMain"], limit=100,
            requested_at=clock.value + 1, started_at=clock.value + 1,
            campaign_consumers={campaign_id: 2},
        )
        clock.value += 2
        failed = recorder.fail_scan(failed_id, TimeoutError("fixture provider timeout"))
        campaigns.record_scan(
            [campaign_id], success=False, finished_at=float(failed["received_at"]),
            next_retry_at=float(failed["next_retry_at"]), detail="fixture provider timeout",
        )
        backoff_suppressed = campaigns.scan_demands(now=clock.value) == []
        clock.value = float(failed["next_retry_at"]) + 0.1

        # Evidence received while paused remains backlog provenance, never a live decision.
        request_at = clock.value
        backlog = quote(request_at, mint="PausedBacklog", price=1.1, cap=20_000)
        run_id = recorder.begin_scan(
            minimum_cap=1_000, maximum_cap=150_000,
            source="CapacityFixtureSource", priority=[backlog.mint], limit=100,
            requested_at=request_at, started_at=request_at,
            campaign_consumers={campaign_id: 2},
        )
        clock.value = request_at + 0.1
        campaigns.command(
            campaign_id, action="pause", idempotency_key="capacity-pause",
        )
        completed = recorder.complete_scan(
            run_id, [backlog], received_at=request_at + 1.2,
        )
        pending_receipts.append(float(completed["received_at"]))
        campaigns.record_scan(
            [campaign_id], success=True, finished_at=float(completed["received_at"]),
        )
        clock.value = request_at + 2
        campaigns.command(
            campaign_id, action="resume", idempotency_key="capacity-resume",
        )
        drain()

        # Restart locks the still-running campaign until an explicit Resume.
        worker.close()
        campaigns.close()
        campaigns = ForwardCampaignStore(data_dir, clock=clock)
        recovered = campaigns.recover_running()
        restart_locked = (
            recovered == 1
            and campaigns.campaign(campaign_id)["state"] == "paused"
            and campaigns.scan_demands(now=clock.value) == []
        )
        campaigns.command(
            campaign_id, action="resume", idempotency_key="capacity-restart-resume",
        )
        worker = PaperResearchWorker(data_dir, recorder, clock=clock)

        # Grow retained history through the actual evaluator, periodically draining
        # its bounded queue before the final request hard-caps every arm.
        remaining = max_requests - campaigns.campaign(campaign_id)["request_count"]
        for index in range(remaining):
            at = clock.value + 2
            mint = f"Capacity{index % 12:02d}"
            selected = quote(
                at, mint=mint, price=1 + (index % 8) * 0.003,
                cap=14_000 + (index % 8) * 700,
            )
            successful(selected)
            if index and index % 40 == 0:
                drain()
        drain()

        final = campaigns.campaign(campaign_id, recorder)
        histories = {
            arm["id"]: campaigns.paper.history(arm["paper_run_id"])
            for arm in final["arms"]
        }
        fills = {
            arm_id: history["run"]["evaluator_report"]["replay"].get("fills", [])
            for arm_id, history in histories.items()
        }
        decision_kinds = {
            arm_id: sorted({row["decision"]["kind"] for row in history["decisions"]})
            for arm_id, history in histories.items()
        }
        exclusions = {
            arm_id: sum(row["quality"]["status"] != "eligible"
                        for row in history["observations"])
            for arm_id, history in histories.items()
        }
        backlog_decisions = sum(
            row["decision"]["mint"] == "PausedBacklog"
            for history in histories.values() for row in history["decisions"]
        )
        sqlite_bytes = sum(path.stat().st_size for path in data_dir.glob("*.sqlite*"))
        report = {
            "schema": "pumpwizard.forward-capacity-acceptance/v1",
            "campaign_id": campaign_id,
            "deterministic_fixture": True,
            "actual_evaluators": {
                "decision": "pumpwizard DecisionEngine",
                "portfolio": "pumpwizard portfolio replay v3",
                "noop_ingestion_measurement": False,
            },
            "arms": len(final["arms"]),
            "campaign_state": final["state"],
            "request_count": final["request_count"],
            "provider_usage": final["usage"],
            "arm_counts": {arm["id"]: arm["counts"] for arm in final["arms"]},
            "decision_kinds": decision_kinds,
            "fill_sides": {
                arm_id: [row["side"] for row in rows] for arm_id, rows in fills.items()
            },
            "partial_exits": {
                arm_id: sum(row["side"] == "exit" and row.get("reason") == "target"
                            for row in rows)
                for arm_id, rows in fills.items()
            },
            "final_exits": {
                arm_id: sum(row["side"] == "exit" and row.get("reason") != "target"
                            for row in rows)
                for arm_id, rows in fills.items()
            },
            "excluded_rows": exclusions,
            "paused_backlog_decisions": backlog_decisions,
            "provider_backoff_suppressed_immediate_retry": backoff_suppressed,
            "restart_required_explicit_resume": restart_locked,
            "quota_exhausted_without_retry": (
                final["state"] == "capped"
                and campaigns.scan_demands(now=clock.value + 10_000) == []
            ),
            "queue_lag_seconds": max(queue_lags, default=0),
            "elapsed_seconds": time.perf_counter() - started_wall,
            "maximum_resident_set_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "sqlite_bytes": sqlite_bytes,
            "authority": final["authority"],
        }
        report["passed"] = (
            report["arms"] >= 2
            and all("entry_recommended" in kinds for kinds in decision_kinds.values())
            and all(report["partial_exits"][arm] >= 1 for arm in fills)
            and all(report["final_exits"][arm] >= 1 for arm in fills)
            and all(value >= 2 for value in exclusions.values())
            and backlog_decisions == 0
            and backoff_suppressed and restart_locked
            and report["quota_exhausted_without_retry"]
            and not any(final["authority"].values())
        )
        return report
    finally:
        worker.close()
        campaigns.close()
        recorder.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    if args.data_dir is not None:
        args.data_dir.mkdir(parents=True, exist_ok=True)
        report = run_rehearsal(args.data_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="pumpwizard-forward-capacity-") as temporary:
            report = run_rehearsal(Path(temporary))
    encoded = json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
