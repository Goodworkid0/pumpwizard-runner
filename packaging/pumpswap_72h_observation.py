#!/usr/bin/env python3
"""Prepare or operate the bounded canonical PumpSwap 72-hour observation.

Preparation is zero-network. Operation is deliberately foreground-only and
requires an external approval reference plus an explicit provider-spend ceiling.
It uses the existing campaign, scheduler, recorder and paper evaluator; it has no
wallet, signer, order or transaction-submission capability.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
from pathlib import Path
import signal
import time
from typing import Any

from pumpwizard_runner.forward_campaigns import START_CONFIRMATION, ForwardCampaignStore
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.market import CanonicalPumpSwapMarketSource
from pumpwizard_runner.observation_operations import (
    _interval_class,
    build_operational_export,
    build_operational_summary,
    build_pumpswap_72_hour_request,
    build_pumpswap_72_hour_resource_model,
)
from pumpwizard_runner.paper_research import PaperResearchWorker
from pumpwizard_runner.scheduler import MonitorScheduler
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder
from pumpwizard_runner.strategies import canonical_json


SCHEMA = "pumpwizard.pumpswap-72h-operator/v1"
START_CONFIRMATION_72H = "START 72H READ-ONLY OBSERVATION"


def _encoded(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _emit(value: dict[str, Any], output: Path | None) -> None:
    encoded = _encoded(value)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
        output.with_suffix(output.suffix + ".sha256").write_text(
            f"{digest}  {output.name}\n", encoding="utf-8",
        )
    print(encoded, end="")
    print("sha256=" + digest)


def prepare(data_dir: Path) -> dict[str, Any]:
    data_dir.mkdir(parents=True, exist_ok=True)
    with ForwardCampaignStore(data_dir) as campaigns:
        preset = campaigns.preregister(
            build_pumpswap_72_hour_request(),
            build_pumpswap_72_hour_resource_model(),
            idempotency_key="pumpswap-month-72h-prepare-v1",
        )
    return {
        "schema": SCHEMA,
        "action": "prepare",
        "prepared_at": time.time(),
        "preset": preset,
        "network_reads": 0,
        "started": False,
        "open_gate": {
            "operating_scope_approval_required": True,
            "provider_spend_ceiling_required": True,
            "provider_price_basis": "unknown",
        },
        "authority": {
            "wallet": False, "signer": False, "orders": False,
            "submission": False, "provider_purchase": False,
        },
        "isolated_data_dir": str(data_dir),
    }


def _approved(args: argparse.Namespace, preset: dict[str, Any]) -> dict[str, Any]:
    reference = args.approval_reference
    budget = args.provider_budget_usd
    if (not isinstance(reference, str) or not reference.strip()
            or len(reference.strip()) > 160 or any(ord(char) < 32 for char in reference)):
        raise ValueError("Operation requires a short, non-secret approval reference")
    if (isinstance(budget, bool) or not isinstance(budget, float)
            or not math.isfinite(budget) or budget < 0):
        raise ValueError("Operation requires an explicit nonnegative provider budget ceiling")
    expected = build_pumpswap_72_hour_request()
    if canonical_json(preset["request"]) != canonical_json(expected):
        raise ValueError("Preset is not the exact frozen Session 1 PumpSwap 72-hour manifest")
    return {
        "schema": "pumpwizard.pumpswap-72h-operating-approval/v1",
        "recorded_at": time.time(),
        "approval_reference": reference.strip(),
        "provider_spend_ceiling": {"amount": budget, "currency": "USD"},
        "preset_id": preset["id"],
        "preset_identity_sha256": preset["identity_sha256"],
        "manifest_sha256": preset["request_sha256"],
        "scope": "foreground read-only canonical PumpSwap observation; no trading authority",
    }


def _retain_approval(data_dir: Path, approval: dict[str, Any]) -> dict[str, Any]:
    path = data_dir / "pumpswap-72h-operating-approval.json"
    if path.exists():
        retained = json.loads(path.read_text(encoding="utf-8"))
        comparable = {key: value for key, value in approval.items() if key != "recorded_at"}
        retained_comparable = {
            key: value for key, value in retained.items() if key != "recorded_at"
        }
        if canonical_json(retained_comparable) != canonical_json(comparable):
            raise ValueError(
                "A different operating approval is already retained in this data directory"
            )
        return retained
    path.write_text(canonical_json(approval) + "\n", encoding="utf-8")
    return approval


def _stop_for_quality(campaigns: ForwardCampaignStore,
                      recorder: ServiceEvidenceRecorder, campaign_id: str) -> str | None:
    campaign = campaigns.campaign(campaign_id, recorder)
    if any(campaign["authority"].values()):
        return "unexpected_authority"
    threshold = int(
        campaign["assumptions"]["stop_policy"]["maximum_consecutive_unusable_intervals"]
    )
    page = recorder.provider_history(campaign_id=campaign_id, limit=500)
    records = page["records"]
    consecutive = 0
    for record in reversed(records):
        if _interval_class(record, campaign["coverage"]) == "usable":
            break
        consecutive += 1
    return "consecutive_unusable_intervals" if consecutive >= threshold else None


def operate(args: argparse.Namespace) -> dict[str, Any]:
    data_dir = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / "pumpswap-72h-operator.lock"
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise ValueError("Another 72-hour operator already owns this data directory") from exc

    recorder = ServiceEvidenceRecorder(data_dir)
    campaigns = ForwardCampaignStore(data_dir)
    worker = PaperResearchWorker(data_dir, recorder)
    library = StrategyLibrary(data_dir)
    scheduler = MonitorScheduler(
        data_dir, source_factory=CanonicalPumpSwapMarketSource,
        recorder=recorder, campaigns=campaigns,
        owner="pumpswap-month-1-72h-operator",
    )
    interrupted = False

    def interrupt(_signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    old_int = signal.signal(signal.SIGINT, interrupt)
    old_term = signal.signal(signal.SIGTERM, interrupt)
    campaign_id: str | None = None
    stop_reason: str | None = None
    try:
        if not library.claim_monitor_lease(scheduler.owner, time.time()):
            raise ValueError("Another service or monitor owns this data directory")
        if args.preset_id:
            if args.confirm != START_CONFIRMATION_72H:
                raise ValueError(
                    f"Starting requires exact confirmation: {START_CONFIRMATION_72H}"
                )
            preset = campaigns.preset(args.preset_id)
            approval = _approved(args, preset)
            approval = _retain_approval(data_dir, approval)
            campaign = campaigns.start_preregistered(
                preset["id"], idempotency_key="pumpswap-month-72h-start-v1",
                confirmation=START_CONFIRMATION,
            )
            campaign_id = str(campaign["id"])
        else:
            campaign_id = str(args.campaign_id)
            if args.confirm != "RESUME " + campaign_id:
                raise ValueError(f"Resuming requires exact confirmation: RESUME {campaign_id}")
            campaign = campaigns.campaign(campaign_id, recorder)
            preset = campaigns.preset(str(campaign["preset_id"]))
            approval = _approved(args, preset)
            approval = _retain_approval(data_dir, approval)
            campaigns.recover_running()
            campaign = campaigns.command(
                campaign_id, action="resume",
                idempotency_key=f"pumpswap-month-72h-resume-{int(time.time())}",
            )

        last_request_count = int(campaign["request_count"])
        while not interrupted:
            current = campaigns.campaign(campaign_id, recorder)
            if current["state"] in {"stopped", "completed", "capped"}:
                break
            before = campaigns.evaluation_snapshot(campaign_id)
            scheduler.run_cycle(library)
            cycle_started = time.time()
            worker.run_once(should_stop=lambda: interrupted)
            cycle_finished = time.time()
            after = campaigns.evaluation_snapshot(campaign_id)
            if int(current["request_count"]) != int(
                campaigns.campaign(campaign_id)["request_count"]
            ) or after != before:
                campaigns.record_evaluation_cycle(
                    campaign_id,
                    cycle_id=f"operator-{time.time_ns()}",
                    started_at=cycle_started,
                    finished_at=cycle_finished,
                    through_received_at=after["through_received_at"],
                    observations=max(0, after["observations"] - before["observations"]),
                    decisions=max(0, after["decisions"] - before["decisions"]),
                    outcomes=max(0, after["outcomes"] - before["outcomes"]),
                )
            campaigns.reconcile_limits(recorder)
            current = campaigns.campaign(campaign_id, recorder)
            request_count = int(current["request_count"])
            if request_count > last_request_count:
                last_request_count = request_count
                stop_reason = _stop_for_quality(campaigns, recorder, campaign_id)
                if stop_reason is not None:
                    campaigns.command(
                        campaign_id, action="stop", confirmation=campaign_id,
                        idempotency_key=f"pumpswap-month-72h-quality-stop-{request_count}",
                    )
                    break
            wait = max(0.25, min(5.0, float(current["next_scan_at"]) - time.time()))
            time.sleep(wait)

        current = campaigns.campaign(campaign_id, recorder)
        if interrupted and current["state"] in {"running", "degraded"}:
            current = campaigns.command(
                campaign_id, action="pause",
                idempotency_key=f"pumpswap-month-72h-interrupt-{int(time.time())}",
            )
            stop_reason = "operator_interrupt_paused"
        summary = build_operational_summary(campaigns, recorder, campaign_id)
        return {
            "schema": SCHEMA,
            "action": "operate",
            "campaign_id": campaign_id,
            "state": current["state"],
            "stop_reason": stop_reason,
            "summary": summary,
            "export": build_operational_export(summary),
            "approval": approval,
            "isolated_data_dir": str(data_dir),
        }
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        scheduler.stop()
        library.release_monitor_lease(scheduler.owner)
        library.close()
        worker.close()
        campaigns.close()
        recorder.close()
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def inspect(data_dir: Path, campaign_id: str, *, stop: bool = False) -> dict[str, Any]:
    recorder = ServiceEvidenceRecorder(data_dir)
    campaigns = ForwardCampaignStore(data_dir)
    try:
        if stop:
            campaigns.command(
                campaign_id, action="stop", confirmation=campaign_id,
                idempotency_key=f"pumpswap-month-72h-operator-stop-{campaign_id}",
            )
        campaign = campaigns.campaign(campaign_id, recorder)
        summary = build_operational_summary(campaigns, recorder, campaign_id)
        return {
            "schema": SCHEMA,
            "action": "stop" if stop else "status",
            "campaign": campaign,
            "summary": summary,
            "export": build_operational_export(summary),
        }
    finally:
        campaigns.close()
        recorder.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    operate_parser = commands.add_parser("operate")
    target = operate_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--preset-id")
    target.add_argument("--campaign-id")
    operate_parser.add_argument("--approval-reference", required=True)
    operate_parser.add_argument("--provider-budget-usd", required=True, type=float)
    operate_parser.add_argument("--confirm", required=True)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("campaign_id")
    stop_parser = commands.add_parser("stop")
    stop_parser.add_argument("campaign_id")
    stop_parser.add_argument("--confirm", required=True)
    args = parser.parse_args()

    if args.command == "prepare":
        result = prepare(args.data_dir)
    elif args.command == "operate":
        result = operate(args)
    elif args.command == "status":
        result = inspect(args.data_dir, args.campaign_id)
    else:
        if args.confirm != args.campaign_id:
            raise ValueError("Stopping requires the exact campaign ID")
        result = inspect(args.data_dir, args.campaign_id, stop=True)
    _emit(result, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
