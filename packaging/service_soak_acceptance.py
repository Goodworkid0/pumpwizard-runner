#!/usr/bin/env python3
"""Bounded synthetic background-service soak with outage and clean restart."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import time

from pumpwizard_runner.background_service import BackgroundServiceEngine
from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.market import MarketDataError
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder
from pumpwizard_runner.strategies import parse_package


SERVICE_PORT = 8863


class SoakSource:
    def __init__(self) -> None:
        self.calls = 0
        self.successes = 0
        self.failures = 0
        self.offline = False

    def collect(self, _low, _high, *, limit, priority):
        assert 1 <= limit <= 100
        del priority
        self.calls += 1
        if self.offline:
            self.failures += 1
            raise MarketDataError("synthetic soak provider outage")
        self.successes += 1
        observed_at = time.time()
        quote = MarketQuote(
            "SoakMint", "SOAK", observed_at, "curve", "SoakPool", 15_000,
            1, .01, 1_000, 10, source="service-soak-fixture",
            quote_usd=100, source_at=observed_at - 1,
        )
        # Duplicate delivery in one response proves the recorder's payload
        # fingerprint remains idempotent under provider replay.
        return [quote, quote]


def _create_agent(directory: Path) -> None:
    package = json.loads(
        (Path(__file__).parents[1] / "examples/xyz-local-strategy.json").read_text()
    )
    strategy = parse_package(package)
    library = StrategyLibrary(directory)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        library.set_tracking(instance_id, True)
    finally:
        library.close()


def _counts(directory: Path) -> dict[str, int]:
    service = sqlite3.connect(directory / "background-service.sqlite")
    evidence = sqlite3.connect(directory / "service-evidence.sqlite")
    try:
        return {
            "instances": service.execute("SELECT COUNT(*) FROM service_instances").fetchone()[0],
            "runs": service.execute("SELECT COUNT(*) FROM service_job_runs").fetchone()[0],
            "completed_runs": service.execute(
                "SELECT COUNT(*) FROM service_job_runs WHERE status='completed'"
            ).fetchone()[0],
            "failed_runs": service.execute(
                "SELECT COUNT(*) FROM service_job_runs WHERE status='failed'"
            ).fetchone()[0],
            "raw_rows": evidence.execute("SELECT COUNT(*) FROM raw_evidence").fetchone()[0],
            "distinct_fingerprints": evidence.execute(
                "SELECT COUNT(DISTINCT fingerprint) FROM raw_evidence"
            ).fetchone()[0],
            "complete_scans": evidence.execute(
                "SELECT COUNT(*) FROM collection_runs WHERE status='complete'"
            ).fetchone()[0],
            "gap_scans": evidence.execute(
                "SELECT COUNT(*) FROM collection_runs WHERE status='gap'"
            ).fetchone()[0],
        }
    finally:
        evidence.close()
        service.close()


def run(directory: Path, duration: float, *, port: int = SERVICE_PORT) -> dict[str, object]:
    if duration < 45:
        raise ValueError("Soak duration must be at least 45 seconds")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Soak service port must be between 1 and 65535")
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Soak directory must be new or empty")
    directory.mkdir(parents=True, exist_ok=True)
    _create_agent(directory)
    source = SoakSource()
    engine = BackgroundServiceEngine(directory, port=port, source_factory=lambda: source)
    started_at = time.monotonic()
    outage_start = duration * .10
    outage_minimum_end = duration * .30
    restart_at = duration * .55
    next_report = 0.0
    outage_started = False
    outage_recovered = False
    restarted = False
    engine.start()
    engine.configure_job({"enabled": True, "interval_seconds": 5})
    try:
        while True:
            elapsed = time.monotonic() - started_at
            if elapsed >= duration:
                break
            if (
                not outage_started
                and source.successes >= 1
                and elapsed >= outage_start
            ):
                source.offline = True
                outage_started = True
            if (
                outage_started
                and not outage_recovered
                and source.failures >= 1
                and elapsed >= outage_minimum_end
            ):
                source.offline = False
                outage_recovered = True
            if elapsed >= restart_at and outage_recovered and not restarted:
                engine.stop()
                engine = BackgroundServiceEngine(
                    directory, port=port, source_factory=lambda: source
                )
                engine.start()
                restarted = True
            if elapsed >= next_report:
                status = engine.status()
                print(json.dumps({
                    "elapsed_seconds": round(elapsed, 1),
                    "provider": "offline" if source.offline else "online",
                    "source_calls": source.calls,
                    "source_successes": source.successes,
                    "source_failures": source.failures,
                    "instance_revision": status["service"]["instance_revision"],
                    "job_failures": status["jobs"][0]["consecutive_failures"],
                    "coverage_rows": len(status.get("coverage", [])),
                }, sort_keys=True), flush=True)
                next_report += 30
            time.sleep(.2)
    finally:
        engine.stop()

    recorder = ServiceEvidenceRecorder(directory)
    try:
        coverage = recorder.coverage(500)
    finally:
        recorder.close()
    counts = _counts(directory)
    result = {
        "schema": "pumpwizard.service-soak-acceptance/v1",
        "directory": str(directory.resolve()),
        "requested_duration_seconds": duration,
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "source_calls": source.calls,
        "source_successes": source.successes,
        "source_failures": source.failures,
        "outage_started": outage_started,
        "outage_recovered": outage_recovered,
        "coverage_complete": sum(row["status"] == "complete" for row in coverage),
        "coverage_gaps": sum(row["status"] == "gap" for row in coverage),
        **counts,
    }
    failures = []
    if not restarted or counts["instances"] < 2:
        failures.append("clean restart did not create a second service instance")
    if not outage_started or source.failures < 1 or counts["gap_scans"] < 1:
        failures.append("provider outage did not create a durable coverage gap")
    if not outage_recovered or source.successes < 2 or counts["complete_scans"] < 2:
        failures.append("collection did not complete before and after recovery")
    if counts["raw_rows"] != counts["distinct_fingerprints"]:
        failures.append("duplicate evidence fingerprints were retained")
    result["status"] = "failed" if failures else "passed"
    result["failures"] = failures
    result_path = directory / "soak-acceptance-result.json"
    descriptor = os.open(
        result_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(
            descriptor,
            (json.dumps(result, indent=2, sort_keys=True) + "\n").encode(),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)
    if failures:
        raise RuntimeError("; ".join(failures))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=1_800)
    parser.add_argument("--port", type=int, default=SERVICE_PORT)
    args = parser.parse_args()
    run(args.directory, args.duration, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
