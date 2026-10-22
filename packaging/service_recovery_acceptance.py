#!/usr/bin/env python3
"""Real-process recovery acceptance for the isolated background service.

The parent process owns every child it signals and verifies the child's PID,
command line, data directory, port, and durable service identity immediately
before sending a signal.  The worker-only source injection is available only by
executing this acceptance module; no service request can enable it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time

from pumpwizard_runner.background_service import BackgroundServiceEngine
from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.service_lifecycle import (
    access_token,
    configure_job,
    probe,
    stop,
)
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.web_api import serve


SERVICE_PORT = 8863
RESULT_SCHEMA = "pumpwizard.service-process-recovery-acceptance/v1"


class AcceptanceSource:
    """Deterministic, duplicate-delivering source confined to the child fixture."""

    def collect(self, _low, _high, *, limit, priority):
        assert 1 <= limit <= 100
        del priority
        observed_at = time.time()
        quote = MarketQuote(
            "RecoveryMint", "RECOVER", observed_at, "curve", "RecoveryPool",
            15_000, 1, .01, 1_000, 10, source="service-recovery-fixture",
            quote_usd=100, source_at=observed_at - 1,
        )
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


def _worker(directory: Path, port: int) -> int:
    token = access_token(directory, create=True)
    engine = BackgroundServiceEngine(
        directory, port=port, source_factory=AcceptanceSource
    )
    server = serve(
        directory, token, "127.0.0.1", port, None, background_service=engine
    )
    previous_handlers: dict[signal.Signals, object] = {}
    try:
        engine.start()

        def stop_for_signal(signum, _frame):
            engine.request_shutdown(f"Acceptance signal {signum} requested shutdown")
            threading.Thread(target=server.shutdown, daemon=True).start()

        for selected in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[selected] = signal.getsignal(selected)
            signal.signal(selected, stop_for_signal)
        server.serve_forever()
        return 0
    finally:
        for selected, handler in previous_handlers.items():
            signal.signal(selected, handler)
        engine.stop()
        server.server_close()


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _ephemeral_port(excluded: set[int]) -> int:
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        if port not in excluded:
            return port
    raise RuntimeError("Could not reserve an auxiliary recovery-acceptance port")


def _spawn(directory: Path, port: int, log_path: Path) -> subprocess.Popen:
    command = [
        sys.executable, str(Path(__file__).resolve()), "--worker",
        "--directory", str(directory.resolve()), "--port", str(port),
    ]
    descriptor = os.open(
        log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        return subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=descriptor, stderr=descriptor,
            close_fds=True,
        )
    finally:
        os.close(descriptor)


def _wait_for_start(
    process: subprocess.Popen, directory: Path, port: int, log_path: Path,
    timeout: float = 10,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = probe(directory, port=port, timeout=.25)
        if status is not None:
            _verified_pid(process, status, directory, port)
            return status
        if process.poll() is not None:
            raise RuntimeError(
                f"Service worker exited {process.returncode}: {_log_tail(log_path)}"
            )
        time.sleep(.05)
    raise RuntimeError(f"Service worker did not start: {_log_tail(log_path)}")


def _verified_pid(
    process: subprocess.Popen, status: dict[str, object], directory: Path, port: int,
) -> int:
    service = status["service"]
    with sqlite3.connect(directory / "background-service.sqlite") as database:
        row = database.execute(
            "SELECT pid,port,state FROM service_instances WHERE instance_id=?",
            (service["instance_id"],),
        ).fetchone()
    if row is None or row != (process.pid, port, "running"):
        raise RuntimeError("Refusing to signal a process without an exact durable identity")
    command = Path(f"/proc/{process.pid}/cmdline").read_bytes().split(b"\0")
    decoded = [part.decode(errors="replace") for part in command if part]
    expected = str(directory.resolve())
    if "--worker" not in decoded or expected not in decoded or str(port) not in decoded:
        raise RuntimeError("Refusing to signal a process with an unexpected command line")
    return process.pid


def _signal_verified(
    process: subprocess.Popen, status: dict[str, object], directory: Path, port: int,
    selected: signal.Signals,
) -> int:
    pid = _verified_pid(process, status, directory, port)
    os.kill(pid, selected)
    return pid


def _wait_for(predicate, message: str, timeout: float = 12) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise RuntimeError(message)


def _counts(directory: Path) -> dict[str, int]:
    with sqlite3.connect(directory / "background-service.sqlite") as service:
        instances = service.execute("SELECT COUNT(*) FROM service_instances").fetchone()[0]
        runs = service.execute("SELECT COUNT(*) FROM service_job_runs").fetchone()[0]
    with sqlite3.connect(directory / "service-evidence.sqlite") as evidence:
        raw_rows = evidence.execute("SELECT COUNT(*) FROM raw_evidence").fetchone()[0]
        distinct = evidence.execute(
            "SELECT COUNT(DISTINCT fingerprint) FROM raw_evidence"
        ).fetchone()[0]
    return {
        "instances": instances,
        "job_runs": runs,
        "raw_rows": raw_rows,
        "distinct_fingerprints": distinct,
    }


def _instance(directory: Path, instance_id: str) -> dict[str, object]:
    with sqlite3.connect(directory / "background-service.sqlite") as database:
        database.row_factory = sqlite3.Row
        row = database.execute(
            "SELECT revision,pid,port,state,current_activity,stop_reason "
            "FROM service_instances WHERE instance_id=?", (instance_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("Expected service instance is missing")
    return dict(row)


def _vault_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    root = directory / "service-wallets"
    for path in sorted(root.glob("*")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _expect_failed(process: subprocess.Popen, label: str, timeout: float = 5) -> None:
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{label} did not fail closed") from None
    if return_code == 0:
        raise RuntimeError(f"{label} unexpectedly started")


def _log_tail(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-2_000:]
    except OSError:
        return "acceptance log unavailable"


def run(directory: Path, *, service_port: int = SERVICE_PORT) -> dict[str, object]:
    directory = directory.resolve()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Recovery acceptance directory must be new or empty")
    if isinstance(service_port, bool) or not isinstance(service_port, int) or not 1 <= service_port <= 65535:
        raise ValueError("Recovery acceptance port must be between 1 and 65535")
    if not _port_available(service_port):
        raise ValueError(f"Recovery acceptance port is occupied: {service_port}")
    competing_port = _ephemeral_port({service_port})
    foreign_port = _ephemeral_port({service_port, competing_port})
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    _create_agent(directory)
    vault_root = directory / "service-wallets"
    vault_root.mkdir(mode=0o700)
    sentinel = vault_root / "acceptance-sentinel.bin"
    sentinel.write_bytes(os.urandom(32))
    sentinel.chmod(0o600)
    vault_before = _vault_digest(directory)
    log_path = directory / "process-acceptance.log"
    owned: list[tuple[subprocess.Popen, int]] = []
    signals_sent: list[dict[str, object]] = []

    try:
        first = _spawn(directory, service_port, log_path)
        owned.append((first, service_port))
        first_status = _wait_for_start(first, directory, service_port, log_path)
        configure_job(
            directory, {"enabled": True, "interval_seconds": 5}, port=service_port
        )
        _wait_for(
            lambda: _counts(directory)["raw_rows"] >= 1,
            "Initial subprocess did not retain collection evidence",
        )

        baseline_counts = _counts(directory)
        competing = _spawn(directory, competing_port, log_path)
        owned.append((competing, competing_port))
        _expect_failed(competing, "Competing owner")
        if probe(directory, port=service_port)["service"]["instance_id"] != first_status["service"]["instance_id"]:
            raise RuntimeError("Competing owner disturbed the verified service")
        if _counts(directory)["instances"] != baseline_counts["instances"]:
            raise RuntimeError("Competing owner created a service instance")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as foreign_listener:
            foreign_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            foreign_listener.bind(("127.0.0.1", foreign_port))
            foreign_listener.listen()
            foreign = _spawn(directory, foreign_port, log_path)
            owned.append((foreign, foreign_port))
            _expect_failed(foreign, "Foreign-port worker")
        if probe(directory, port=service_port)["service"]["instance_id"] != first_status["service"]["instance_id"]:
            raise RuntimeError("Foreign port disturbed the verified service")
        if _vault_digest(directory) != vault_before:
            raise RuntimeError("A losing process changed service vault state")

        first_pid = _signal_verified(
            first, first_status, directory, service_port, signal.SIGTERM
        )
        signals_sent.append({"signal": "SIGTERM", "pid": first_pid})
        if first.wait(timeout=8) != 0:
            raise RuntimeError(f"SIGTERM worker did not stop cleanly: {_log_tail(log_path)}")
        first_record = _instance(directory, first_status["service"]["instance_id"])
        if first_record["state"] != "stopped" or first_record["current_activity"] != "stopped":
            raise RuntimeError("SIGTERM did not record a clean stop")

        second = _spawn(directory, service_port, log_path)
        owned.append((second, service_port))
        second_status = _wait_for_start(second, directory, service_port, log_path)
        if second_status["service"]["instance_revision"] != first_status["service"]["instance_revision"] + 1:
            raise RuntimeError("Clean restart did not advance service identity")
        if not second_status["jobs"][0]["enabled"]:
            raise RuntimeError("Clean restart did not retain the opted-in collection job")
        if second_status["trading"]["signer_state"] != "locked":
            raise RuntimeError("Clean restart recreated signing authority")
        _wait_for(
            lambda: _counts(directory)["raw_rows"] > baseline_counts["raw_rows"],
            "Collection did not resume after clients closed and service restarted",
            # The durable service job wakes every five seconds, while the
            # imported strategy's own persisted scan schedule is fifteen.
            # Observe that real schedule instead of forcing its database due.
            timeout=25,
        )

        second_pid = _signal_verified(
            second, second_status, directory, service_port, signal.SIGKILL
        )
        signals_sent.append({"signal": "SIGKILL", "pid": second_pid})
        second.wait(timeout=5)

        third = _spawn(directory, service_port, log_path)
        owned.append((third, service_port))
        third_status = _wait_for_start(third, directory, service_port, log_path)
        if third_status["service"]["instance_revision"] != second_status["service"]["instance_revision"] + 1:
            raise RuntimeError("Crash restart did not advance service identity")
        second_record = _instance(directory, second_status["service"]["instance_id"])
        if second_record["current_activity"] != "interrupted":
            raise RuntimeError("Crash predecessor was not recorded as interrupted")
        if not third_status["jobs"][0]["enabled"]:
            raise RuntimeError("Crash restart did not retain the opted-in collection job")
        if third_status["trading"]["signer_state"] != "locked":
            raise RuntimeError("Crash restart recreated signing authority")
        stopped = stop(directory, port=service_port, timeout=8)
        if not stopped["stopped"] or third.wait(timeout=8) != 0:
            raise RuntimeError("Final authenticated shutdown did not stop cleanly")

        final_counts = _counts(directory)
        if final_counts["instances"] != 3:
            raise RuntimeError("Losing processes changed the durable instance count")
        if final_counts["raw_rows"] != final_counts["distinct_fingerprints"]:
            raise RuntimeError("Recovery retained duplicate evidence fingerprints")
        if _vault_digest(directory) != vault_before:
            raise RuntimeError("Recovery changed service vault state")
        result = {
            "schema": RESULT_SCHEMA,
            "status": "passed",
            "directory": str(directory),
            "ports": {
                "service": service_port,
                "competing_owner": competing_port,
                "foreign_listener": foreign_port,
            },
            "signals_sent_to_verified_children": signals_sent,
            "instance_ids": [
                first_status["service"]["instance_id"],
                second_status["service"]["instance_id"],
                third_status["service"]["instance_id"],
            ],
            "job_enabled_after_crash": third_status["jobs"][0]["enabled"],
            "signer_state_after_crash": third_status["trading"]["signer_state"],
            "crashed_predecessor_activity": second_record["current_activity"],
            "competing_owner_failed_closed": True,
            "foreign_port_failed_closed": True,
            "vault_unchanged": True,
            **final_counts,
        }
        result_path = directory / "process-acceptance-result.json"
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        result_path.chmod(0o600)
        print(json.dumps(result, indent=2, sort_keys=True), flush=True)
        return result
    finally:
        for process, port in owned:
            if process.poll() is not None:
                continue
            status = probe(directory, port=port, timeout=.2)
            if status is None:
                continue
            try:
                _signal_verified(process, status, directory, port, signal.SIGTERM)
                process.wait(timeout=3)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                # Never broaden cleanup to an unverified PID. A failed cleanup
                # remains visible to the operator for manual inspection.
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=SERVICE_PORT)
    args = parser.parse_args()
    if args.worker:
        if isinstance(args.port, bool) or not 1 <= args.port <= 65535:
            raise ValueError("Acceptance worker port is invalid")
        return _worker(args.directory.resolve(), args.port)
    run(args.directory, service_port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
