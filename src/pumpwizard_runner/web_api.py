"""Loopback API for the local browser client.

This module never exposes private seeds. Browser wallets may request exact
reviewed trades or SOL transfers and return signatures for revalidation,
durable reservation and broadcast. Automation-vault backups contain authenticated
ciphertext only. Production main-wallet Autopilot activation remains unavailable;
the synthetic acceptance runtime is constructor-gated and never request-enabled.
"""
from __future__ import annotations

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import copy
import csv
import io
import json
import mimetypes
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

from . import __version__
from .agent_hub import AgentHubError
from .catalog import load_catalog, package_for
from .evaluation import MAX_BYTES as EVALUATION_MAX_BYTES
from .evaluation import evaluate, example_recording
from .library import StrategyLibrary
from .market import CompositePumpMarketSource
from .market_history import MarketHistoryService, RESOLUTION_SECONDS, SOURCE, pool_histories
from .portfolio import PortfolioEngine, PortfolioJournal
from .release_notes import note_for, note_url
from .research_views import filter_market, session_state
from .paper_research import PaperResearchError, PaperResearchStore, monitoring_session, require_monitoring_session
from .campaign_performance import build_campaign_report
from .evidence_alerts import EvidenceAlertError, EvidenceAlertStore
from .forward_campaigns import (
    REQUEST_SCHEMA as FORWARD_CAMPAIGN_REQUEST_SCHEMA,
    ForwardCampaignError,
    ForwardCampaignStore,
)
from .holding_valuation import retained_holding_valuation, wallet_position_valuation
from .observation_operations import (
    build_24_hour_request,
    build_24_hour_resource_model,
    build_operational_export,
    build_operational_summary,
)
from .receipt_evidence import retained_receipts
from .research_data import (
    ResearchDataError,
    ResearchDataStore,
    freeze_service_dataset,
)
from .research_jobs import ResearchJobError, ResearchRegistry
from .service_recorder import ServiceEvidenceRecorder
from .scheduler import MonitorScheduler
from .session_review import (
    SessionReviewError,
    SessionReviewStore,
    compare_dataset,
    create_inactive_draft_request,
    freeze_dataset,
    generate_candidates,
)
from .strategies import parse_package
from .strategy_support import strategy_identity, support_label, support_text
from .strategy_creator import draft_package
from .web_assistant import BrowserAssistant
from .web_reports import evaluations, replays, shadow, worker
from .web_wallet import state as wallet_state
from .web_trading import inspect_wallet, preview as trade_preview
from .web_trading import reconcile as reconcile_trades
from .web_trading import state as trade_state
from .web_trading import submit as submit_trade
from .web_strategy_controls import overview as strategy_controls
from .web_strategy_controls import save as save_strategy_controls
from .web_session_allocations import create as create_trading_workspace
from .web_session_allocations import release_empty as release_empty_trading_workspace
from .web_trading import WalletAuthorizer
from .web_main_wallet_autopilot import MainWalletAutopilotController
from .transfers import GENESIS, TransferError, public_key
from . import web_agent_orders
from . import web_transfers
from . import web_automation
from . import wallet_portfolio


SCHEMA = "pumpwizard.runner-web/v1"
ALLOWED_ORIGINS = frozenset({"http://127.0.0.1:5173", "http://localhost:5173"})
MAX_COMMAND_BYTES = 1_000_000
MAX_REPLAY_BATCHES = 20_000
REPLAY_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
STATIC_COOKIE = "pumpwizard_browser"
MARKET_REFRESH_MIN_SECONDS = 20.0
_MARKET_REFRESH_LOCK = threading.Lock()
_MARKET_REFRESH_AT: dict[str, float] = {}
TOKEN_IMAGE_HOSTS = frozenset({
    "cdn.dexscreener.com", "dd.dexscreener.com", "cf-ipfs.com", "cloudflare-ipfs.com",
    "gateway.pinata.cloud", "ipfs.io", "pump.mypinata.cloud",
    "raw.githubusercontent.com", "backpack.exchange", "ipfs.filebase.io",
    "axiomtrading-v2.axiom-cdn.io", "pumpapi.io",
})
TOKEN_IMAGE_MAX_BYTES = 1_000_000
_TOKEN_IMAGE_CACHE: dict[str, tuple[str, bytes]] = {}
_TOKEN_IMAGE_LOCK = threading.RLock()


def envelope(**values: object) -> dict[str, object]:
    return {"schema": SCHEMA, "version": __version__, **values}


def _error_status(exc: Exception, *, read: bool = False) -> int:
    message = str(exc).lower()
    code = getattr(exc, "code", "")
    if code == "not_found":
        return 404
    if code in {"identity_conflict", "holdout_conflict"}:
        return 409
    if isinstance(exc, (
        ResearchDataError, ResearchJobError, PaperResearchError,
        ForwardCampaignError, EvidenceAlertError,
        AgentHubError,
    )):
        return 422
    if isinstance(exc, SessionReviewError):
        if "stale or mismatched" in message or "holdout" in message:
            return 409
        if read and message.startswith("unknown"):
            return 404
        return 422
    return 404 if read and "unknown" in message else 400


def _typed_error(exc: Exception, path: str) -> dict[str, object]:
    if isinstance(exc, AgentHubError):
        code = "assistant_request_invalid"
        stage = "evidence_assistant" if "/assistant/evidence" in path else "assistant"
    elif isinstance(exc, TransferError):
        code, stage = "authorization_invalid", "wallet_authorization"
    elif isinstance(exc, OSError):
        code, stage = "local_io_error", "local_storage"
    elif isinstance(exc, json.JSONDecodeError):
        code, stage = "invalid_json", "request_decode"
    else:
        code, stage = "invalid_request", "request_validation"
    return {
        "schema": "pumpwizard.error/v1",
        "code": code,
        "message": str(exc)[:300],
        "retryable": isinstance(exc, OSError),
        "stage": stage,
        "request_id": None,
        "details": {},
    }


def _session_reviews(library: StrategyLibrary, instance_id: str) -> dict[str, object]:
    """List completed local reports without starting or changing a session."""
    library.instance(instance_id)
    with SessionReviewStore(library.path.parent) as store:
        reports = store.reports(instance_id)
        evidence = store.dataset(reports[0]["dataset_id"])["evidence"] if reports else None
    return envelope(reports=reports, evidence=evidence)


def _run_session_review(library: StrategyLibrary, instance_id: str,
                        body: object) -> dict[str, object]:
    if not isinstance(body, dict) or set(body) != {"horizon_seconds"}:
        raise SessionReviewError("Session review requires one outcome horizon")
    horizon = body["horizon_seconds"]
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
        raise SessionReviewError("Comparison horizon must be a positive whole number of seconds")
    snapshot = library.session_review_snapshot(instance_id)
    with SessionReviewStore(library.path.parent) as store:
        dataset = freeze_dataset(
            store,
            source_session_id=str(snapshot["source_session_id"]),
            strategy_package=snapshot["strategy_package"],
            strategy_digest=str(snapshot["strategy_digest"]),
            observations=snapshot["observations"],
            decisions=snapshot["decisions"],
            outcomes=snapshot["outcomes"],
            feature_records=snapshot["feature_records"],
            decision_capture_complete=bool(snapshot["decision_capture_complete"]),
            first_at=float(snapshot["first_at"]),
            last_at=float(snapshot["last_at"]),
        )
        report = compare_dataset(store, dataset["id"], horizon_seconds=horizon)
    return envelope(dataset={"id": dataset["id"], "evidence": dataset["evidence"]},
                    report=report)


def _create_session_review_draft(library: StrategyLibrary, report_id: str,
                                 body: object) -> dict[str, object]:
    if (not isinstance(body, dict) or set(body) != {"candidate_id", "accepted"}
            or not isinstance(body.get("candidate_id"), str)
            or body.get("accepted") is not True):
        raise SessionReviewError("Creating a successor draft requires the selected candidate and explicit acceptance")
    with SessionReviewStore(library.path.parent) as store:
        report = store.report(report_id)
    source = library.instance(str(report["source_session_id"]))
    if source["strategy_digest"] != report["source_strategy_sha256"]:
        raise SessionReviewError("Source session strategy digest is stale or mismatched")
    request = create_inactive_draft_request(
        report, candidate_id=body["candidate_id"], accepted=True,
    )
    strategy = parse_package(request["strategy_package"])
    if strategy.digest != request["strategy_sha256"]:
        raise SessionReviewError("Candidate package identity changed after comparison")
    library.import_strategy(strategy)
    label = f"{source['label']} review draft"
    instance_id = library.create_instance(
        strategy.digest, str(label)[:120], entry_mode="disabled", exit_mode="manual",
    )
    from .workspace_scope import instance_owner
    owner = instance_owner(library, source)
    if owner:
        draft_workspace = library.create_session_workspace(
            "monitoring", str(label)[:120], wallet_sender=owner[0], wallet_network=owner[1],
        )
        library.assign_instance_to_workspace(str(draft_workspace["id"]), instance_id)
    library.set_tracking(instance_id, False)
    session = library.instance(instance_id)
    management = _session_management(library, instance_id)
    if management["allocation_lamports"] != "0":
        raise SessionReviewError("Inactive successor unexpectedly acquired a wallet allocation")
    return envelope(
        draft_request=request,
        session=session,
        brief=library.session_brief(instance_id),
        management=management,
        notice=("Created a paused, unallocated monitoring draft from the selected comparison. "
                "Entries and monitoring remain disabled."),
    )


def _research_payload(library: StrategyLibrary, path: str,
                      query: dict[str, list[str]]) -> dict[str, object]:
    root = library.path.parent
    limit = min(500, max(1, int(query.get("limit", ["100"])[0])))
    data = ResearchDataStore(root)
    registry = ResearchRegistry(root)
    paper = PaperResearchStore(root)
    campaigns = ForwardCampaignStore(root)
    recorder = ServiceEvidenceRecorder(root)
    alerts = EvidenceAlertStore(root)
    try:
        if path.startswith("/api/v1/research/sessions/"):
            source_session_id = path.removeprefix(
                "/api/v1/research/sessions/"
            ).rstrip("/")
            return _research_workspace(
                library, data, registry, paper, source_session_id, limit=limit,
            )
        if path == "/api/v1/research":
            return envelope(
                datasets=data.list(limit=limit),
                experiments=[_research_experiment_view(registry, row)
                             for row in registry.experiments(limit=limit)],
                jobs=registry.jobs(limit=limit), paper_runs=paper.runs(limit=limit),
                campaigns=campaigns.campaigns(limit=limit, recorder=recorder),
                alerts=alerts.alerts(state="open", limit=limit),
                integrity=registry.integrity_summary(),
                authority={"wallet": False, "signer": False, "orders": False},
            )
        if path == "/api/v1/research/integrity":
            return envelope(integrity=registry.integrity_summary())
        if path == "/api/v1/research/datasets":
            source = query.get("source_session_id", [None])[0]
            return envelope(datasets=data.list(source_session_id=source, limit=limit))
        if path == "/api/v1/research/experiments":
            return envelope(experiments=[
                _research_experiment_view(registry, row)
                for row in registry.experiments(
                    dataset_id=query.get("dataset_id", [None])[0], limit=limit,
                )
            ])
        if path == "/api/v1/research/jobs":
            return envelope(jobs=registry.jobs(limit=limit))
        if path == "/api/v1/research/paper-runs":
            return envelope(paper_runs=paper.runs(limit=limit))
        if path == "/api/v1/research/campaigns":
            return envelope(campaigns=campaigns.campaigns(limit=limit, recorder=recorder))
        if path == "/api/v1/research/observation-presets":
            return envelope(observation_presets=campaigns.presets(
                campaign_key=query.get("campaign_key", [None])[0], limit=limit,
            ))
        if path.startswith("/api/v1/research/observation-presets/"):
            ident = path.removeprefix(
                "/api/v1/research/observation-presets/"
            ).rstrip("/")
            return envelope(observation_preset=campaigns.preset(ident))
        if path == "/api/v1/research/alerts":
            return envelope(alerts=alerts.alerts(
                campaign_id=query.get("campaign_id", [None])[0],
                state=query.get("state", [None])[0], limit=limit,
            ))
        if path == "/api/v1/research/receipt-evidence":
            return envelope(receipt_evidence=retained_receipts(
                root, sender=query.get("sender", [None])[0], limit=limit,
            ))
        if path.startswith("/api/v1/research/campaign-reports/"):
            ident = path.removeprefix(
                "/api/v1/research/campaign-reports/"
            ).removesuffix("/export").rstrip("/")
            return envelope(campaign_report=campaigns.report(ident))
        if path.startswith("/api/v1/research/campaigns/"):
            suffix = path.removeprefix("/api/v1/research/campaigns/").rstrip("/")
            if suffix.endswith("/operational-summary"):
                ident = suffix.removesuffix("/operational-summary").rstrip("/")
                return envelope(operational_summary=build_operational_summary(
                    campaigns, recorder, ident,
                ))
            if suffix.endswith("/operational-export"):
                ident = suffix.removesuffix("/operational-export").rstrip("/")
                summary = build_operational_summary(campaigns, recorder, ident)
                return envelope(operational_export=build_operational_export(summary))
            if suffix.endswith("/history"):
                ident = suffix.removesuffix("/history").rstrip("/")
                cursor = int(query.get("after_sequence", ["0"])[0])
                return envelope(history=campaigns.history(
                    ident, after_sequence=cursor, limit=limit,
                ))
            if suffix.endswith("/provider"):
                ident = suffix.removesuffix("/provider").rstrip("/")
                raw_cursor = query.get("cursor", [None])[0]
                cursor = None
                if raw_cursor:
                    decoded = json.loads(raw_cursor)
                    if (not isinstance(decoded, list) or len(decoded) != 2
                            or not isinstance(decoded[1], str)):
                        raise ForwardCampaignError("Provider history cursor is invalid")
                    cursor = (float(decoded[0]), decoded[1])
                campaigns.campaign(ident)
                return envelope(provider=recorder.provider_history(
                    campaign_id=ident, after=cursor, limit=limit,
                ))
            if suffix.endswith("/reports"):
                ident = suffix.removesuffix("/reports").rstrip("/")
                campaigns.campaign(ident)
                return envelope(campaign_reports=campaigns.reports(ident, limit=limit))
            if suffix.endswith("/alerts"):
                ident = suffix.removesuffix("/alerts").rstrip("/")
                campaigns.campaign(ident)
                return envelope(alerts=alerts.alerts(campaign_id=ident, limit=limit))
            return envelope(campaign=campaigns.campaign(suffix, recorder))
        if path.startswith("/api/v1/research/datasets/"):
            suffix = path.removeprefix("/api/v1/research/datasets/").rstrip("/")
            if suffix.endswith("/export"):
                ident = suffix.removesuffix("/export").rstrip("/")
                return envelope(export=data.export(ident))
            if suffix.endswith("/point-in-time"):
                ident = suffix.removesuffix("/point-in-time").rstrip("/")
                known_by = float(query.get("known_by", [""])[0])
                raw_cursor = query.get("cursor", [None])[0]
                cursor = None
                if raw_cursor:
                    decoded = json.loads(raw_cursor)
                    if (not isinstance(decoded, list) or len(decoded) != 2
                            or not isinstance(decoded[1], str)):
                        raise ResearchDataError("Point-in-time cursor is invalid")
                    cursor = (float(decoded[0]), decoded[1])
                return envelope(page=data.point_in_time(
                    ident, known_by=known_by, after=cursor, limit=limit,
                ))
            return envelope(dataset=data.dataset(suffix))
        if path.startswith("/api/v1/research/experiments/"):
            ident = path.removeprefix("/api/v1/research/experiments/").rstrip("/")
            return envelope(experiment=_research_experiment_view(
                                registry, registry.experiment(ident)),
                            attempts=registry.attempts(ident))
        if path.startswith("/api/v1/research/jobs/"):
            suffix = path.removeprefix("/api/v1/research/jobs/").rstrip("/")
            if suffix.endswith("/result"):
                ident = suffix.removesuffix("/result").rstrip("/")
                return envelope(result=registry.result_view(ident))
            return envelope(job=registry.job(suffix))
        if path.startswith("/api/v1/research/paper-runs/"):
            suffix = path.removeprefix("/api/v1/research/paper-runs/").rstrip("/")
            if suffix.endswith("/history"):
                ident = suffix.removesuffix("/history").rstrip("/")
                return envelope(**paper.history(ident, limit=limit))
            return envelope(paper_run=paper.run(suffix))
        if path.startswith("/api/v1/research/reports/"):
            suffix = path.removeprefix("/api/v1/research/reports/").rstrip("/")
            if suffix.endswith("/export"):
                suffix = suffix.removesuffix("/export").rstrip("/")
            return envelope(result=registry.report_view_by_id(suffix))
        raise ResearchDataError("Unknown research read endpoint", code="not_found")
    finally:
        alerts.close()
        recorder.close()
        campaigns.close()
        paper.close()
        registry.close()
        data.close()


def _research_workspace(library: StrategyLibrary, data: ResearchDataStore,
                        registry: ResearchRegistry, paper: PaperResearchStore,
                        source_session_id: str, *, limit: int) -> dict[str, object]:
    source = library.instance(source_session_id)
    datasets = data.list(source_session_id=source_session_id, limit=limit)
    dataset_ids = {row["id"] for row in datasets}
    experiments = [row for row in registry.experiments(limit=limit)
                   if row["dataset_id"] in dataset_ids]
    experiment_ids = {row["id"] for row in experiments}
    attempts = [attempt for experiment in experiments
                for attempt in registry.attempts(experiment["id"])]
    jobs = [row for row in registry.jobs(limit=limit)
            if row["experiment_id"] in experiment_ids]
    reports = registry.reports(source_session_id=source_session_id, limit=limit)
    paper_runs = [row for row in paper.runs(limit=limit)
                  if row["source_session_id"] == source_session_id]
    return {
        "schema": "pumpwizard.research-workspace/v1",
        "source_session_id": source_session_id,
        "source_label": source["label"],
        "source_strategy_sha256": source["strategy_digest"],
        "paper_research_allowed": monitoring_session(library, source_session_id),
        "dataset_preview": None,
        "datasets": [_research_dataset_view(row) for row in datasets],
        "experiments": [_research_experiment_view(registry, row) for row in experiments],
        "attempts": [_research_attempt_view(row) for row in attempts],
        "jobs": [_research_job_view(registry, row) for row in jobs],
        "reports": [_research_report_view(data, registry, row) for row in reports],
        "integrity": registry.integrity_summary(),
        "paper_runs": [_paper_run_view(paper, row) for row in paper_runs],
    }


def _research_dataset_view(dataset: dict[str, object]) -> dict[str, object]:
    quality = dataset["quality"]
    counts = quality["counts"]
    gaps = quality["gaps"]
    reasons = list(dataset["eligibility"]["reasons"])
    first_known = dataset["ranges"]["first_known"]
    event = dataset["ranges"]["event"]
    last_basis = first_known["last"] if first_known["last"] is not None else event["last"]
    return {
        "schema": "pumpwizard.research-dataset/v1",
        "id": dataset["id"], "content_sha256": dataset["content_sha256"],
        "source_session_id": dataset["source"]["session_id"],
        "source_strategy_sha256": dataset["source"]["strategy_sha256"],
        "created_at": first_known["last"] or event["last"],
        "event_range": {"first_at": event["first"], "last_at": event["last"]},
        "first_known_range": {
            "first_at": first_known["first"], "last_at": first_known["last"],
            "basis": "recorder first receipt; unknown rows retained and excluded point-in-time",
        },
        "source_age_seconds": max(0.0, time.time() - float(last_basis)),
        "counts": {
            "observations": counts["observations"],
            "decisions": counts.get("decisions", 0),
            "outcomes": counts.get("outcomes", 0),
            "independent_observations": max(
                0, counts["observations"] - counts["unknown_first_receipt"]
                - counts["duplicates"]
            ),
            "unknown_first_known": counts["unknown_first_receipt"],
        },
        "quality": {key: value for key, value in counts.items()
                    if key not in {"observations", "decisions", "outcomes",
                                   "collection_gaps"}},
        "gaps": [{
            "id": row["id"], "started_at": row["started_at"],
            "ended_at": row.get("received_at"), "reason": "collection outage",
            "detail": row.get("gap_reason") or "No source evidence was available.",
        } for row in gaps],
        "supported_family": "unsupported-strategy-family" not in reasons,
        "family": dataset["eligibility"]["supported_family"],
        "eligible": dataset["eligibility"]["eligible"],
        "blockers": reasons,
        "evidence_policy": (
            "First-known records only; missing, late and unknown values are never encoded as zero."
        ),
    }


def _research_experiment_view(registry: ResearchRegistry,
                              experiment: dict[str, object]) -> dict[str, object]:
    holdout = registry.holdout_status(str(experiment["id"]))
    cost = experiment["cost_model"]
    return {
        "schema": "pumpwizard.research-experiment/v1",
        "id": experiment["id"], "digest": experiment["request_sha256"],
        "dataset_id": experiment["dataset_id"], "parent_experiment_id": None,
        "created_at": experiment["registered_at"], "purpose": experiment["purpose"],
        "starting_capital": experiment["capital"]["amount"],
        "capital_unit": experiment["capital"]["unit"],
        "candidate_count": len(experiment["candidates"]),
        "candidate_package_sha256": [row["package_sha256"]
                                     for row in experiment["candidates"]],
        "cost_model": {
            "label": cost.get("label", "Declared experiment costs"),
            "capital_unit": experiment["capital"]["unit"],
            "variable_fee_fraction_each_side": cost.get("variable_fee_fraction_each_side", 0),
            "slippage_fraction_each_side": cost.get("slippage_fraction_each_side", 0),
            "fixed_cost_each_side": cost.get("fixed_cost_each_side", 0),
            "rent_each_entry": cost.get("rent_each_entry", 0),
        },
        "split": {**experiment["splits"],
                  "embargo_seconds": experiment["embargo_seconds"]},
        "evidence_policy": json.dumps(experiment["evidence_policy"], sort_keys=True),
        "code_version": experiment["code_version"],
        "replay_engine_version": experiment["engine_version"],
        "holdout_state": holdout["state"],
        "holdout_integrity": holdout,
        "changed_from_parent": [],
    }


def _research_attempt_view(attempt: dict[str, object]) -> dict[str, object]:
    return {
        "schema": "pumpwizard.research-attempt/v1", "id": attempt["id"],
        "experiment_id": attempt["experiment_id"], "number": attempt["ordinal"],
        "state": attempt["status"], "started_at": attempt["started_at"],
        "finished_at": attempt["finished_at"], "error": attempt["error"] or None,
        "restart_count": attempt["restart_count"],
    }


def _research_job_view(registry: ResearchRegistry,
                       job: dict[str, object]) -> dict[str, object]:
    attempt = next(row for row in registry.attempts(job["experiment_id"])
                   if row["id"] == job["attempt_id"])
    report_id = None
    if job["state"] == "completed":
        report_id = registry.result(job["id"]).get("id")
    return {
        "schema": "pumpwizard.research-job/v1", "id": job["id"],
        "experiment_id": job["experiment_id"], "attempt_id": job["attempt_id"],
        "state": "cancelling" if job["state"] == "cancel_requested" else job["state"],
        "progress": job["progress"],
        "cancellation_requested": job["cancellation_requested_at"] is not None,
        "restart_count": attempt["restart_count"], "report_id": report_id,
        "error": job["error"] or None, "created_at": job["queued_at"],
        "updated_at": job["updated_at"],
    }


def _research_report_view(data: ResearchDataStore, registry: ResearchRegistry,
                          report: dict[str, object]) -> dict[str, object]:
    report = registry.report_view(report)
    required = {"period", "capital_unit", "ledger", "cost_stress", "exclusions"}
    if required <= set(report):
        return report
    dataset = data.dataset(report["dataset_id"])
    capital = report.get("capital", {"amount": 0, "unit": "USD"})
    starting = float(capital.get("amount", 0))
    comparisons = [{
        "id": "cash", "kind": "cash", "label": "Cash", "package_sha256": None,
        "starting_capital": starting, "ending_capital": starting,
        "capital_unit": capital.get("unit", "USD"), "modeled_net_pnl": 0,
        "modeled_realized_pnl": 0, "modeled_unrealized_pnl": 0,
        "maximum_drawdown_fraction": 0, "coverage_fraction": 1,
        "independent_observations": dataset["quality"]["counts"]["observations"],
        "modeled_costs": 0, "uncertainty": None,
        "equity": [
            {"at": dataset["ranges"]["event"]["first"], "equity": starting,
             "gap_reason": None},
            {"at": dataset["ranges"]["event"]["last"], "equity": starting,
             "gap_reason": None},
        ],
    }]
    blockers = list(report.get("blockers", []))
    recommendation = report.get("recommendation") or {}
    action = recommendation.get("action", "collect_more_data")
    mapped_action = {
        "collect_more_data": "insufficient_evidence",
        "keep_current": "keep_current",
        "create_inactive_draft": "create_inactive_draft",
        "prefer_cash": "prefer_cash",
    }.get(action, "insufficient_evidence")
    return {
        "schema": "pumpwizard.research-replay-report/v1", "id": report["id"],
        "experiment_id": report["experiment_id"], "dataset_id": report["dataset_id"],
        "created_at": report.get("created_at", dataset["ranges"]["event"]["last"]),
        "period": {"first_at": dataset["ranges"]["event"]["first"],
                   "last_at": dataset["ranges"]["event"]["last"]},
        "capital_unit": capital.get("unit", "USD"), "comparisons": comparisons,
        "ledger": [], "cost_stress": [],
        "exclusions": [{"reason": item, "count": 1, "action": "Collect compatible evidence."}
                       for item in report.get("exclusions", []) if isinstance(item, str)],
        "recommendation": {
            "action": mapped_action, "candidate_id": recommendation.get("candidate_id"),
            "reason": recommendation.get("reason", "No eligible recommendation was produced."),
            "blockers": blockers,
        },
        "eligible": bool(report.get("eligible")),
        "holdout_state": report.get(
            "holdout_state", "ineligible" if not report.get("eligible") else "exposed"
        ),
        "integrity": report.get("integrity"),
        "limitations": report.get("limitations") or blockers or [
            "Compatibility report; finite-capital replay is unavailable."
        ],
    }


def _paper_run_view(paper: PaperResearchStore,
                    run: dict[str, object]) -> dict[str, object]:
    history = paper.history(run["id"], limit=1000)
    receipts = [row["first_received_at"] for row in history["observations"]]
    scored = [row for row in history["outcomes"] if row["status"] == "scored"]
    evaluator_report = run.get("evaluator_report") or {}
    evaluator_decisions = evaluator_report.get("decisions", [])
    replay_metrics = evaluator_report.get("replay", {}).get("metrics", {})
    conservative_equity = replay_metrics.get("conservative_ending_equity_micros")
    closed = [row for row in evaluator_decisions if row.get("outcome_status") == "closed"]
    pending = [row for row in evaluator_decisions
               if row.get("outcome_status") not in {"closed"}]
    run_counts = run.get("counts") if isinstance(run.get("counts"), dict) else {}
    excluded_observations = int(run_counts.get("exclusions", 0))
    retry = run.get("retry") if isinstance(run.get("retry"), dict) else {
        "count": 0, "maximum": 0, "next_at": None,
    }
    fault = run.get("fault") if isinstance(run.get("fault"), dict) else None
    processing_state = (
        "stalled" if fault is not None and fault.get("retryable") is False
        else "degraded" if run["state"] == "interrupted" or fault is not None
        else "healthy"
    )
    return {
        "schema": "pumpwizard.paper-run/v1", "id": run["id"],
        "source_session_id": run["source_session_id"],
        "strategy_sha256": run["strategy_sha256"], "state": run["state"],
        "starting_capital_usd": run["simulated_capital"]["amount"],
        "simulated_capital_usd": (
            conservative_equity / 1_000_000
            if isinstance(conservative_equity, int) else None
        ),
        "started_at": run["start_at"],
        "first_known_at": min(receipts) if receipts else None,
        "updated_at": run["last_cycle_at"] or run["created_at"],
        "decisions": len(evaluator_decisions) or len(history["decisions"]),
        "scored_outcomes": len(closed) or len(scored),
        "unknown_outcomes": len(pending) if evaluator_decisions else max(
            0, len(history["decisions"]) - len(history["outcomes"])
        ),
        "independent_observations": max(
            0, len(history["observations"]) - excluded_observations
        ),
        "excluded_observations": excluded_observations,
        "processing_state": processing_state,
        "fault": fault,
        "retry": retry,
        "gaps": [{"id": row["gap_id"], "started_at": row["started_at"],
                  "ended_at": row["ended_at"], "reason": row["reason"]}
                 for row in history["gaps"]],
        "assumptions": [
            "Worker 2 finite-capital paper ledger; no wallet receipt or funded performance exists."
        ],
    }


def _research_report_artifact(data_dir: Path, report_id: str,
                              format_name: str) -> tuple[str, str, bytes]:
    registry = ResearchRegistry(data_dir)
    data = ResearchDataStore(data_dir)
    try:
        report = registry.result_by_report_id(report_id)
        view = _research_report_view(data, registry, report)
    finally:
        data.close()
        registry.close()
    if format_name == "json":
        body = (json.dumps(view, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        return "application/json; charset=utf-8", f"{report_id}.json", body
    if format_name != "csv":
        raise ResearchJobError("Research export format must be json or csv")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "report_id", "dataset_id", "comparison_id", "kind", "label", "capital_unit",
        "starting_capital", "ending_capital", "modeled_net_pnl", "modeled_costs",
        "coverage_fraction", "maximum_drawdown_fraction",
    ])
    for row in view["comparisons"]:
        writer.writerow([
            view["id"], view["dataset_id"], row["id"], row["kind"], row["label"],
            row["capital_unit"], row["starting_capital"], row["ending_capital"],
            row["modeled_net_pnl"], row["modeled_costs"], row["coverage_fraction"],
            row["maximum_drawdown_fraction"],
        ])
    return "text/csv; charset=utf-8", f"{report_id}.csv", output.getvalue().encode()


def _campaign_report_artifact(data_dir: Path, report_id: str,
                              format_name: str) -> tuple[str, str, bytes]:
    campaigns = ForwardCampaignStore(data_dir)
    try:
        report = campaigns.report(report_id)
    finally:
        campaigns.close()
    if format_name == "json":
        body = (json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        return "application/json; charset=utf-8", f"{report_id}.json", body
    if format_name != "csv":
        raise ForwardCampaignError("Campaign report export format must be json or csv")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "report_id", "campaign_id", "comparison_id", "kind", "capital_unit",
        "starting_capital_micros", "ending_capital_micros", "net_result_micros",
        "matched_evidence_count", "valuation_complete", "provider_cost_status",
    ])
    for row in [*report.get("benchmarks", []), *report.get("arms", [])]:
        writer.writerow([
            report["id"], report["campaign_id"], row.get("id"), row.get("kind"),
            row.get("capital_unit"), row.get("starting_capital_micros"),
            row.get("ending_capital_micros"), row.get("net_result_micros"),
            row.get("matched_evidence_count"), row.get("valuation_complete"),
            row.get("provider_cost_status", report.get("provider_cost", {}).get("cost_status")),
        ])
    return "text/csv; charset=utf-8", f"{report_id}.csv", output.getvalue().encode()


def _campaign_manifest(library: StrategyLibrary,
                       value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ForwardCampaignError("Campaign manifest must be an object")
    expected = {
        "schema", "campaign_key", "supersedes_campaign_id", "source", "arms",
        "assumptions", "coverage", "duration_seconds", "quotas", "provider_cost",
    }
    if set(value) != expected or value.get("schema") != FORWARD_CAMPAIGN_REQUEST_SCHEMA:
        raise ForwardCampaignError("Campaign manifest fields do not match the v1 contract")
    source = value.get("source")
    if not isinstance(source, dict) or source.get("provider") != "CompositePumpMarketSource":
        raise ForwardCampaignError(
            "Browser campaigns require the configured CompositePumpMarketSource provider"
        )
    raw_arms = value.get("arms")
    if not isinstance(raw_arms, list):
        raise ForwardCampaignError("Campaign arms must be a list")
    arms = []
    for raw in raw_arms:
        if not isinstance(raw, dict) or set(raw) != {
            "id", "source_session_id", "strategy_sha256", "simulated_capital_usd",
        }:
            raise ForwardCampaignError("Campaign arm reference is incomplete")
        instance = library.instance(str(raw["source_session_id"]))
        require_monitoring_session(library, str(raw["source_session_id"]))
        if instance["strategy_digest"] != raw["strategy_sha256"]:
            raise ForwardCampaignError(
                "Campaign source strategy identity changed", code="identity_conflict",
            )
        arms.append({**raw, "strategy_package": instance["package"]})
    return {**value, "arms": arms}


def _research_command(library: StrategyLibrary, path: str, body: dict[str, object],
                      background_service=None) -> dict[str, object]:
    root = library.path.parent
    if path == "/api/v1/research/observation-presets/24-hour":
        if (set(body) != {"schema", "idempotency_key"}
                or body.get("schema") != "pumpwizard.observation-preset-prepare/v1"):
            raise ForwardCampaignError("Observation preset preparation schema is invalid")
        campaigns = ForwardCampaignStore(root)
        try:
            preset = campaigns.preregister(
                build_24_hour_request(), build_24_hour_resource_model(),
                idempotency_key=str(body["idempotency_key"]),
            )
            return {
                "schema": "pumpwizard.research-command-result/v1",
                "observation_preset": preset,
                "notice": (
                    "Prepared an immutable zero-network 24-hour observation preset. "
                    "Collection has not started and provider billing remains unknown."
                ),
            }
        finally:
            campaigns.close()
    if (path.startswith("/api/v1/research/observation-presets/")
            and path.endswith("/start")):
        ident = path.removeprefix(
            "/api/v1/research/observation-presets/"
        ).removesuffix("/start").rstrip("/")
        expected = {"schema", "preset_id", "idempotency_key", "confirmation"}
        if (set(body) != expected
                or body.get("schema") != "pumpwizard.observation-preset-start/v1"
                or body.get("preset_id") != ident):
            raise ForwardCampaignError("Observation preset start identity is invalid")
        campaigns = ForwardCampaignStore(root)
        recorder = ServiceEvidenceRecorder(root)
        try:
            campaign = campaigns.start_preregistered(
                ident,
                idempotency_key=str(body["idempotency_key"]),
                confirmation=str(body["confirmation"]),
            )
            if background_service is not None:
                background_service.wake_event.set()
            return {
                "schema": "pumpwizard.research-command-result/v1",
                "campaign": campaigns.campaign(campaign["id"], recorder),
                "notice": (
                    "Started the exact preregistered paper observation. It has no wallet, "
                    "signer, order, submission, or provider-purchase authority."
                ),
            }
        finally:
            recorder.close()
            campaigns.close()
    if path == "/api/v1/research/campaigns":
        if (set(body) != {"schema", "campaign", "idempotency_key", "confirmation"}
                or body.get("schema") != "pumpwizard.forward-campaign-start/v1"):
            raise ForwardCampaignError("Campaign start request schema is invalid")
        manifest = _campaign_manifest(library, body["campaign"])
        campaigns = ForwardCampaignStore(root)
        recorder = ServiceEvidenceRecorder(root)
        try:
            campaign = campaigns.start(
                manifest, idempotency_key=str(body["idempotency_key"]),
                confirmation=str(body["confirmation"]),
            )
            if background_service is not None:
                background_service.wake_event.set()
            return {
                "schema": "pumpwizard.research-command-result/v1",
                "campaign": campaigns.campaign(campaign["id"], recorder),
                "notice": (
                    "Started a bounded forward-observation campaign with paper arms only; "
                    "no wallet, signer, order, or provider-purchase authority was granted."
                ),
            }
        finally:
            recorder.close()
            campaigns.close()
    if path.startswith("/api/v1/research/campaigns/") and any(
        path.endswith("/" + action) for action in ("pause", "resume", "stop")
    ):
        suffix = path.removeprefix("/api/v1/research/campaigns/").rstrip("/")
        ident, action = suffix.rsplit("/", 1)
        expected = {"schema", "campaign_id", "command", "idempotency_key"}
        if action == "stop":
            expected.add("confirmation")
        if (set(body) != expected
                or body.get("schema") != "pumpwizard.forward-campaign-command/v1"
                or body.get("campaign_id") != ident or body.get("command") != action):
            raise ForwardCampaignError("Campaign command identity is invalid")
        campaigns = ForwardCampaignStore(root)
        recorder = ServiceEvidenceRecorder(root)
        try:
            if action == "resume":
                saved = campaigns.campaign(ident)
                # The fixed observation preset has independent monitoring arms.
                if not saved.get("preset_id"):
                    for arm in saved["arms"]:
                        require_monitoring_session(library, arm["source_session_id"])
            campaign = campaigns.command(
                ident, action=action,
                confirmation=str(body.get("confirmation")) if action == "stop" else None,
                idempotency_key=str(body["idempotency_key"]),
            )
            if background_service is not None and action == "resume":
                background_service.wake_event.set()
            return {
                "schema": "pumpwizard.research-command-result/v1",
                "campaign": campaigns.campaign(campaign["id"], recorder),
                "notice": f"Forward campaign {action} recorded without trading authority.",
            }
        finally:
            recorder.close()
            campaigns.close()
    if path.startswith("/api/v1/research/campaigns/") and path.endswith("/reports"):
        ident = path.removeprefix(
            "/api/v1/research/campaigns/"
        ).removesuffix("/reports").rstrip("/")
        if (set(body) != {"schema", "campaign_id", "previous_report_id", "created_at"}
                or body.get("schema") != "pumpwizard.forward-report-request/v1"
                or body.get("campaign_id") != ident):
            raise ForwardCampaignError("Campaign report request identity is invalid")
        campaigns = ForwardCampaignStore(root)
        recorder = ServiceEvidenceRecorder(root)
        try:
            previous = body.get("previous_report_id")
            if previous is not None:
                prior = campaigns.report(str(previous))
                if prior.get("campaign_id") != ident:
                    raise ForwardCampaignError(
                        "Previous report belongs to another campaign",
                        code="identity_conflict",
                    )
            report = build_campaign_report(
                campaigns, recorder, ident,
                previous_report_id=None if previous is None else str(previous),
                created_at=float(body["created_at"]),
            )
            return {
                "schema": "pumpwizard.research-command-result/v1",
                "campaign_report": report,
                "notice": "Saved one append-only paper-performance snapshot.",
            }
        finally:
            recorder.close()
            campaigns.close()
    if path.startswith("/api/v1/research/alerts/") and path.endswith("/acknowledge"):
        ident = path.removeprefix(
            "/api/v1/research/alerts/"
        ).removesuffix("/acknowledge").rstrip("/")
        if (set(body) != {"schema", "alert_id"}
                or body.get("schema") != "pumpwizard.evidence-alert-acknowledgment/v1"
                or body.get("alert_id") != ident):
            raise EvidenceAlertError("Alert acknowledgment identity is invalid")
        alerts = EvidenceAlertStore(root)
        try:
            return {
                "schema": "pumpwizard.research-command-result/v1",
                "alert": alerts.acknowledge(ident),
                "notice": "Acknowledgment recorded; it does not resolve the condition.",
            }
        finally:
            alerts.close()
    if path == "/api/v1/research/datasets/freeze":
        if (set(body) != {"schema", "source_session_id", "idempotency_key"}
                or body.get("schema") != "pumpwizard.research-freeze-request/v1"):
            raise ResearchDataError("Dataset freeze request schema is invalid")
        return _research_command(
            library, "/api/v1/research/datasets",
            {"source_session_id": body["source_session_id"], "network": "unknown"},
            background_service,
        )
    if path == "/api/v1/research/jobs" and body.get("schema") == \
            "pumpwizard.research-comparison-request/v1":
        if set(body) != {"schema", "dataset_id", "settings", "idempotency_key"}:
            raise ResearchJobError("Comparison request schema is invalid")
        settings = body["settings"]
        expected_settings = {
            "starting_capital", "capital_unit", "purpose",
            "variable_fee_bps_each_side", "slippage_bps_each_side",
            "fixed_cost_each_side", "rent_each_entry", "embargo_seconds",
        }
        if not isinstance(settings, dict) or set(settings) != expected_settings:
            raise ResearchJobError("Comparison settings are incomplete")
        data = ResearchDataStore(root)
        registry = ResearchRegistry(root)
        try:
            dataset = data.dataset(str(body["dataset_id"]))
            package = dataset["source"]["strategy_package"]
            try:
                candidate_rows = generate_candidates(package)
                candidates = [row["package"] for row in candidate_rows]
            except SessionReviewError:
                candidates = [package]
            first = float(dataset["ranges"]["event"]["first"])
            last = float(dataset["ranges"]["event"]["last"])
            span = last - first
            embargo = float(settings["embargo_seconds"])
            usable = span - 2 * embargo
            if usable <= 0:
                raise ResearchJobError(
                    "Dataset period is too short for the declared two embargo intervals"
                )
            segment = usable / 3
            train_end = first + segment
            validation_end = train_end + embargo + segment
            experiment = registry.register({
                "dataset_id": dataset["id"], "candidates": candidates,
                "cost_model": {
                    "label": "Browser-declared route costs",
                    "variable_fee_fraction_each_side": (
                        float(settings["variable_fee_bps_each_side"]) / 10_000
                    ),
                    "slippage_fraction_each_side": (
                        float(settings["slippage_bps_each_side"]) / 10_000
                    ),
                    "fixed_cost_each_side": float(settings["fixed_cost_each_side"]),
                    "rent_each_entry": float(settings["rent_each_entry"]),
                },
                "capital": {"amount": settings["starting_capital"],
                            "unit": settings["capital_unit"]},
                "splits": {"train_end_at": train_end,
                           "validation_end_at": validation_end,
                           "holdout_end_at": last},
                "embargo_seconds": embargo,
                "objective": "maximize finite-capital terminal equity with coverage",
                "evidence_policy": {
                    "point_in_time": True, "missing": "unknown",
                    "horizon_seconds": 1800,
                },
                "purpose": settings["purpose"],
                "engine_version": "pumpwizard-research-replay/v1",
                "code_version": __version__,
            })
            job = registry.enqueue(
                experiment["id"], idempotency_key=str(body["idempotency_key"]),
            )
            return {"schema": "pumpwizard.research-command-result/v1",
                    "experiment": _research_experiment_view(registry, experiment),
                    "job": _research_job_view(registry, job),
                    "notice": "Registered the exact experiment and queued one bounded comparison."}
        finally:
            registry.close()
            data.close()
    if path.startswith("/api/v1/research/jobs/") and path.endswith("/retry"):
        ident = path.removeprefix("/api/v1/research/jobs/").removesuffix("/retry").rstrip("/")
        if (set(body) != {"schema", "job_id", "idempotency_key"}
                or body.get("schema") != "pumpwizard.research-retry-request/v1"
                or body.get("job_id") != ident):
            raise ResearchJobError("Research retry request identity is invalid")
        registry = ResearchRegistry(root)
        try:
            prior = registry.job(ident)
            job = registry.enqueue(
                prior["experiment_id"], idempotency_key=str(body["idempotency_key"]),
            )
            return {"schema": "pumpwizard.research-command-result/v1", "job": job,
                    "notice": "Queued another preserved experiment attempt."}
        finally:
            registry.close()
    if path.startswith("/api/v1/research/reports/") and path.endswith("/draft"):
        report_id = path.removeprefix("/api/v1/research/reports/").removesuffix("/draft").rstrip("/")
        if (set(body) != {"schema", "report_id", "candidate_id", "accepted",
                         "idempotency_key"}
                or body.get("schema") != "pumpwizard.research-draft-request/v1"
                or body.get("report_id") != report_id):
            raise ResearchJobError("Research draft request identity is invalid")
        registry = ResearchRegistry(root)
        try:
            job_id = next((job["id"] for job in registry.jobs(limit=500)
                           if job["state"] == "completed"
                           and registry.result(job["id"]).get("id") == report_id), None)
        finally:
            registry.close()
        if job_id is None:
            raise ResearchJobError("Unknown completed research report", code="not_found")
        return _create_research_draft(library, job_id, {
            "candidate_id": body["candidate_id"], "accepted": body["accepted"],
        })
    if path.startswith("/api/v1/research/jobs/") and path.endswith("/cancel") \
            and body.get("schema") == "pumpwizard.research-cancel-request/v1":
        ident = path.removeprefix("/api/v1/research/jobs/").removesuffix("/cancel").rstrip("/")
        if (set(body) != {"schema", "job_id", "idempotency_key"}
                or body.get("job_id") != ident):
            raise ResearchJobError("Research cancellation identity is invalid")
        registry = ResearchRegistry(root)
        try:
            return {"schema": "pumpwizard.research-command-result/v1",
                    "job": registry.cancel(ident),
                    "notice": "Cancellation was durably requested."}
        finally:
            registry.close()
    if path == "/api/v1/research/paper-runs" and body.get("schema") == \
            "pumpwizard.paper-start-request/v1":
        required = {"schema", "source_session_id", "strategy_sha256",
                    "starting_capital_usd", "confirmation", "idempotency_key"}
        if set(body) != required:
            raise PaperResearchError("Paper start request schema is invalid")
        instance = library.instance(str(body["source_session_id"]))
        require_monitoring_session(library, str(body["source_session_id"]))
        if instance["strategy_digest"] != body["strategy_sha256"]:
            raise PaperResearchError("Paper source strategy identity changed",
                                     code="identity_conflict")
        paper = PaperResearchStore(root)
        try:
            run = paper.start(
                source_session_id=str(body["source_session_id"]),
                strategy_package=instance["package"],
                strategy_sha256=str(body["strategy_sha256"]),
                simulated_capital_usd=body["starting_capital_usd"],
                start_at=time.time(), idempotency_key=str(body["idempotency_key"]),
                confirmation=str(body["confirmation"]),
            )
            return {"schema": "pumpwizard.research-command-result/v1",
                    "paper_run": _paper_run_view(paper, run),
                    "notice": "Started prospective paper research without wallet authority."}
        finally:
            paper.close()
    if path.startswith("/api/v1/research/paper-runs/") and any(
        path.endswith("/" + action) for action in ("pause", "resume", "stop")
    ) and body.get("schema") == "pumpwizard.paper-command/v1":
        suffix = path.removeprefix("/api/v1/research/paper-runs/").rstrip("/")
        ident, action = suffix.rsplit("/", 1)
        if (set(body) != {"schema", "run_id", "command", "idempotency_key"}
                or body.get("run_id") != ident or body.get("command") != action):
            raise PaperResearchError("Paper command identity is invalid")
        paper = PaperResearchStore(root)
        try:
            if action == "resume":
                require_monitoring_session(library, paper.run(ident)["source_session_id"])
            run = (paper.stop(ident, confirmation=ident) if action == "stop"
                   else paper.command(ident, action=action))
            return {"schema": "pumpwizard.research-command-result/v1",
                    "paper_run": _paper_run_view(paper, run),
                    "notice": f"Paper run {action} recorded."}
        finally:
            paper.close()
    if path == "/api/v1/research/datasets":
        if set(body) != {"source_session_id", "network"}:
            raise ResearchDataError("Dataset freeze requires source_session_id and network")
        recorder = (getattr(background_service, "recorder", None)
                    if background_service is not None else None)
        owned_recorder = recorder is None
        recorder = recorder or ServiceEvidenceRecorder(root)
        data = ResearchDataStore(root)
        try:
            result = freeze_service_dataset(
                library, recorder, data,
                source_session_id=body["source_session_id"], network=body["network"],
            )
            return envelope(dataset=result["dataset"])
        finally:
            data.close()
            if owned_recorder:
                recorder.close()
    if path == "/api/v1/research/experiments":
        registry = ResearchRegistry(root)
        try:
            return envelope(experiment=registry.register(body))
        finally:
            registry.close()
    if path == "/api/v1/research/jobs":
        if set(body) != {"experiment_id", "idempotency_key"}:
            raise ResearchJobError("Research enqueue requires experiment_id and idempotency_key")
        registry = ResearchRegistry(root)
        try:
            return envelope(job=registry.enqueue(
                body["experiment_id"], idempotency_key=body["idempotency_key"],
            ))
        finally:
            registry.close()
    if path.startswith("/api/v1/research/jobs/") and path.endswith("/cancel"):
        ident = path.removeprefix("/api/v1/research/jobs/").removesuffix("/cancel").rstrip("/")
        if set(body) != {"confirmation"} or body["confirmation"] != ident:
            raise ResearchJobError("Cancelling research requires the exact job ID")
        registry = ResearchRegistry(root)
        try:
            return envelope(job=registry.cancel(ident))
        finally:
            registry.close()
    if path == "/api/v1/research/paper-runs":
        required = {
            "source_session_id", "strategy_sha256", "simulated_capital_usd",
            "start_at", "idempotency_key", "confirmation",
        }
        if set(body) != required:
            raise PaperResearchError("Paper start requires the exact immutable run fields")
        instance = library.instance(str(body["source_session_id"]))
        require_monitoring_session(library, str(body["source_session_id"]))
        if instance["strategy_digest"] != body["strategy_sha256"]:
            raise PaperResearchError("Paper source strategy identity changed",
                                     code="identity_conflict")
        paper = PaperResearchStore(root)
        try:
            return envelope(paper_run=paper.start(
                source_session_id=str(body["source_session_id"]),
                strategy_package=instance["package"],
                strategy_sha256=str(body["strategy_sha256"]),
                simulated_capital_usd=body["simulated_capital_usd"],
                start_at=body["start_at"], idempotency_key=str(body["idempotency_key"]),
                confirmation=str(body["confirmation"]),
            ))
        finally:
            paper.close()
    if path.startswith("/api/v1/research/paper-runs/") and path.endswith("/stop"):
        ident = path.removeprefix("/api/v1/research/paper-runs/").removesuffix("/stop").rstrip("/")
        if set(body) != {"confirmation"}:
            raise PaperResearchError("Paper stop requires confirmation")
        paper = PaperResearchStore(root)
        try:
            return envelope(paper_run=paper.stop(
                ident, confirmation=str(body["confirmation"]),
            ))
        finally:
            paper.close()
    if path.startswith("/api/v1/research/jobs/") and path.endswith("/draft"):
        ident = path.removeprefix("/api/v1/research/jobs/").removesuffix("/draft").rstrip("/")
        return _create_research_draft(library, ident, body)
    raise ResearchDataError("Unknown research command endpoint", code="not_found")


def _create_research_draft(library: StrategyLibrary, job_id: str,
                           body: object) -> dict[str, object]:
    if (not isinstance(body, dict) or set(body) != {"candidate_id", "accepted"}
            or body.get("accepted") is not True or not isinstance(body.get("candidate_id"), str)):
        raise ResearchJobError(
            "Creating a research draft requires one candidate and explicit acceptance"
        )
    registry = ResearchRegistry(library.path.parent)
    try:
        job = registry.job(job_id)
        result = registry.result_view(job_id)
        experiment = registry.experiment(job["experiment_id"])
    finally:
        registry.close()
    if result.get("eligible") is not True:
        raise ResearchJobError("Only an eligible completed comparison can create a draft")
    recommendation = result.get("recommendation")
    if (not isinstance(recommendation, dict)
            or recommendation.get("action") != "create_inactive_draft"
            or recommendation.get("candidate_id") != body["candidate_id"]):
        raise ResearchJobError("Completed comparison does not recommend that candidate")
    candidate = next(
        (row for row in experiment["candidates"] if row["id"] == body["candidate_id"]), None
    )
    if candidate is None:
        raise ResearchJobError("Unknown registered experiment candidate", code="not_found")
    strategy = parse_package(candidate["package"])
    if strategy.digest != candidate["package_sha256"]:
        raise ResearchJobError("Registered candidate identity changed", code="identity_conflict")
    source = library.instance(experiment["lineage"]["source_session_id"])
    library.import_strategy(strategy)
    instance_id = library.create_instance(
        strategy.digest, f"{source['label']} research draft"[:120],
        entry_mode="disabled", exit_mode="manual",
    )
    library.set_tracking(instance_id, False)
    management = _session_management(library, instance_id)
    if management["allocation_lamports"] != "0":
        raise ResearchJobError("Inactive draft unexpectedly acquired wallet allocation")
    return envelope(
        session=library.instance(instance_id), management=management,
        notice=("Created a paused, unallocated draft. Monitoring, entries, signing and "
                "execution remain disabled."),
    )


def _main_wallet_runtime_revision(controller: MainWalletAutopilotController,
                                  body: dict[str, object]) -> None:
    revision = body.get("authorization_revision")
    if type(revision) is not int:
        raise TransferError("Main-wallet runtime requires its authorization revision")
    status = controller.status(body.get("sender"), body.get("network"))
    allowance = next((row for row in status["allowances"]
                      if row["id"] == body.get("allowance_id")), None)
    if allowance is None or allowance["authorization_revision"] != revision:
        raise TransferError("Main-wallet allowance authorization revision is stale")


def _main_wallet_command(library: StrategyLibrary, action: str, body: dict[str, object],
                         wallet_authorizer: WalletAuthorizer | None,
                         controller: MainWalletAutopilotController | None) -> dict[str, object]:
    if wallet_authorizer is None or controller is None:
        raise TransferError("Main-wallet Autopilot is unavailable")
    contracts = {
        "review": {"sender", "network", "session_id", "wallet_kind", "plan"},
        "authorize": {"sender", "network", "session_id", "review_id", "confirmation"},
        "activate": {"sender", "network", "allowance_id", "wallet_kind"},
        "heartbeat": {"sender", "network", "allowance_id", "runtime_token", "authorization_revision"},
        "pause": {"sender", "network", "allowance_id", "runtime_token", "authorization_revision", "paused"},
        "revoke": {"sender", "network", "allowance_id"},
        "lock": {"sender", "network", "allowance_id", "runtime_token"},
        "ticket": {"sender", "network", "allowance_id", "runtime_token", "authorization_revision", "order_id"},
        "next": {"sender", "network", "allowance_id", "runtime_token", "authorization_revision"},
        "signed": {"sender", "network", "allowance_id", "runtime_token", "authorization_revision", "ticket_id", "signed_transaction"},
    }
    fields = contracts.get(action)
    if fields is None or set(body) != fields:
        raise TransferError("Main-wallet Autopilot command fields are invalid")
    sender, network = wallet_authorizer.require(body.get("sender"), body.get("network"))
    common = {"sender": sender, "network": network}
    if action == "review":
        wallet_kind = body["wallet_kind"]
        if wallet_kind not in ("local", "extension"):
            raise TransferError("Choose the connected wallet capability")
        return envelope(review=controller.review(
            library, **common, session_id=body["session_id"],
            wallet_kind=wallet_kind, plan=body["plan"],
        ))
    if action == "authorize":
        return envelope(allowance=controller.authorize(
            library, **common, session_id=body["session_id"],
            review_id=body["review_id"], confirmation=body["confirmation"],
        ))
    if action == "activate":
        return envelope(runtime=controller.activate(
            library, **common, allowance_id=body["allowance_id"], wallet_kind=body["wallet_kind"],
        ))
    if action in {"heartbeat", "pause", "ticket", "next", "signed"}:
        _main_wallet_runtime_revision(controller, body)
    if action == "heartbeat":
        return envelope(**controller.heartbeat(
            **common, allowance_id=body["allowance_id"], runtime_token=body["runtime_token"],
        ))
    if action == "pause":
        controller.pause_entries(
            library, **common, allowance_id=body["allowance_id"], runtime_token=body["runtime_token"],
            paused=body["paused"],
        )
        return envelope(paused=body["paused"])
    if action == "revoke":
        controller.revoke(library, **common, allowance_id=body["allowance_id"])
        return envelope(revoked=True)
    if action == "lock":
        if not isinstance(body["runtime_token"], str):
            raise TransferError("Main-wallet runtime token is invalid")
        controller.lock(library, **common, allowance_id=body["allowance_id"])
        return envelope(locked=True)
    if action == "ticket":
        return envelope(ticket=controller.ticket(
            library, **common, allowance_id=body["allowance_id"],
            runtime_token=body["runtime_token"], order_id=body["order_id"],
        ))
    if action == "next":
        next_order = controller.next_ticket(
            library, **common, allowance_id=body["allowance_id"], runtime_token=body["runtime_token"],
        )
        return envelope(**next_order)
    signature = controller.submit_signed(
        library, **common, allowance_id=body["allowance_id"],
        runtime_token=body["runtime_token"], ticket_id=body["ticket_id"],
        signed_transaction=body["signed_transaction"],
    )
    return envelope(signature=signature)


def _approved_token_image_url(source: str) -> bool:
    try:
        parsed = urlparse(source)
        return (parsed.scheme == "https" and parsed.hostname in TOKEN_IMAGE_HOSTS
                and not parsed.username and not parsed.password
                and parsed.port in (None, 443))
    except ValueError:
        return False


class _TokenImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _approved_token_image_url(newurl):
            raise ValueError("Token icon redirected to an unapproved host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def token_image(library: StrategyLibrary, mint: str) -> tuple[str, bytes]:
    """Proxy bounded exact-mint artwork, including static SVG and alternatives."""
    public_key(mint)
    metadata = library.coin_metadata(mint)
    alternatives = metadata.get("image_urls")
    raw = [metadata.get("image_url"), *(alternatives[:3] if isinstance(alternatives, list) else [])]
    sources = list(dict.fromkeys(item for item in raw if isinstance(item, str) and len(item) <= 500))[:3]
    if not sources:
        raise ValueError("Token icon unavailable")
    approved = [source for source in sources if _approved_token_image_url(source)]
    if not approved:
        raise ValueError("Token icon host is not approved")
    resolution_key = mint + "\n" + "\n".join(approved)
    with _TOKEN_IMAGE_LOCK:
        cached = _TOKEN_IMAGE_CACHE.get(resolution_key)
        if cached is not None:
            return cached
    for source in approved:
        with _TOKEN_IMAGE_LOCK:
            cached = _TOKEN_IMAGE_CACHE.get(source)
        if cached is not None:
            result = cached
        else:
            request = urllib.request.Request(source, headers={
                "Accept": "image/png,image/jpeg,image/webp,image/gif,image/svg+xml",
                "User-Agent": "SolanaMarketWizard/0.2",
            })
            try:
                opener = urllib.request.build_opener(_TokenImageRedirect())
                with opener.open(request, timeout=4) as response:
                    if not _approved_token_image_url(response.geturl()):
                        raise ValueError("Token icon redirected to an unapproved host")
                    body = response.read(TOKEN_IMAGE_MAX_BYTES + 1)
                if len(body) > TOKEN_IMAGE_MAX_BYTES:
                    continue
                if body.startswith(b"\x89PNG\r\n\x1a\n"):
                    mime = "image/png"
                elif body.startswith(b"\xff\xd8\xff"):
                    mime = "image/jpeg"
                elif body.startswith((b"GIF87a", b"GIF89a")):
                    mime = "image/gif"
                elif len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
                    mime = "image/webp"
                elif b"<svg" in body[:1000]:
                    from .token_artwork import sanitize_token_svg
                    body = sanitize_token_svg(body)
                    mime = "image/svg+xml"
                else:
                    continue
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                continue
            result = (mime, body)
        with _TOKEN_IMAGE_LOCK:
            while len(_TOKEN_IMAGE_CACHE) >= 99:
                _TOKEN_IMAGE_CACHE.pop(next(iter(_TOKEN_IMAGE_CACHE)))
            _TOKEN_IMAGE_CACHE[source] = result
            _TOKEN_IMAGE_CACHE[resolution_key] = result
        return result
    raise ValueError("Token icon unavailable from its recorded artwork sources")


def workspace(library: StrategyLibrary) -> dict:
    instances = library.instances()
    return envelope(sessions=len(instances),
                    monitoring=sum(row["status"] == "monitoring" for row in instances),
                    strategies=len(library.rows()),
                    market_rows=len(library.market_watch_rows(500)))


def first_use_capabilities(background_service=None) -> dict[str, object]:
    """Describe authenticated local access without inferring wallet authority.

    The HTTP handler authenticates the launch token before dispatching here.
    This read deliberately avoids wallet inspection, provider access and local
    mutations so a clean browser can choose Research before connecting a wallet.
    """
    if background_service is None:
        lifecycle, service_state, signer_state = "browser_owned", "running", "unavailable"
    else:
        status = background_service.status()
        service = status.get("service", {})
        trading = status.get("trading", {})
        lifecycle = "background_service"
        service_state = str(service.get("state", "unknown"))
        signer_state = str(trading.get("signer_state", "locked"))
    return envelope(capabilities={
        "schema": "pumpwizard.first-use-capabilities/v1",
        "authentication": {
            "state": "authenticated",
            "method": "local_launch_token",
            "scope": "local_data_directory",
            "wallet_identity": False,
        },
        "research": {
            "state": "available",
            "wallet_required": False,
            "deposit_required": False,
            "signer_required": False,
            "ai_provider_required": False,
            "collection_start": "explicit",
        },
        "wallet_actions": {
            "state": "connection_required",
            "ownership_proof_required": True,
            "address": None,
            "network_scan": "not_started",
        },
        "service": {
            "lifecycle": lifecycle,
            "state": service_state,
            "signer_state": signer_state,
            # Closing a browser tab is never itself a service-stop command.
            # The compatibility browser-owned process can still end for an
            # independent lifecycle reason, but tab closure does not request
            # or prove that shutdown.
            "browser_close_stops_service": False,
        },
        "optional_ai": {
            "state": "optional",
            "provider_required_for_local_evidence": False,
            "provider_requests_require_approval": True,
        },
        "production_execution": {
            "state": "blocked",
            "automatic_execution": False,
            "wallet_connection_grants_execution": False,
        },
        "read_effects": {
            "wallet_scan": False,
            "provider_request": False,
            "collection_start": False,
            "signing_request": False,
            "submission_request": False,
        },
    })


def _percentage(value: object, *, absolute: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = abs(float(value)) if absolute else float(value)
    return round(number * 100, 4)


def _exit_plan(library: StrategyLibrary, instance: dict) -> dict[str, object]:
    """Expose a bounded, display-only summary of the exact imported exit rules."""
    stored = library.strategy(str(instance["strategy_digest"]))
    rules = parse_package(stored["package"]).effective_rules
    targets = []
    for target in rules.get("targets", []) if isinstance(rules.get("targets"), list) else []:
        if not isinstance(target, dict):
            continue
        cap, fraction = target.get("market_cap_usd"), target.get("initial_fraction")
        if isinstance(cap, (int, float)) and not isinstance(cap, bool) and isinstance(fraction, (int, float)) and not isinstance(fraction, bool):
            targets.append({"market_cap_usd": float(cap), "sell_percent": round(float(fraction) * 100, 4)})
    maximum = rules.get("max_hold_seconds")
    return {
        "approval": "manual" if instance.get("exit_mode") == "manual" else str(instance.get("exit_mode", "manual")),
        "stop_loss_percent": _percentage(rules.get("stop_loss_fraction"), absolute=True),
        "take_profit_percent": _percentage(rules.get("take_profit_fraction")),
        "trailing_drawdown_percent": _percentage(rules.get("trailing_drawdown_fraction")),
        "max_hold_seconds": float(maximum) if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) else None,
        "targets": targets[:10],
    }


def _session_positions(library: StrategyLibrary, instance_id: str) -> list[dict[str, object]]:
    """Return retained session positions with one authoritative typed valuation."""
    now = time.time()
    positions = library.positions(instance_id, include_closed=False)
    for position in positions:
        try:
            observation = library.latest_observation(instance_id, str(position["mint"]))
        except ValueError:
            observation = None
        position["valuation"] = retained_holding_valuation(position, observation, now=now)
    return positions


def _trade_position_valuation(
    library: StrategyLibrary, position: dict[str, object], *, now: float,
) -> dict[str, object]:
    """Use retained session/market evidence without triggering a provider read."""
    observation = None
    mark_source = None
    owner = position.get("agent_id")
    if isinstance(owner, str) and owner:
        try:
            observation = library.latest_observation(owner, str(position["mint"]))
            mark_source = "retained_session_observation"
        except ValueError:
            pass
    if observation is None:
        retained = library.coin_history(str(position["mint"]), 1)
        if retained:
            observation = retained[0]
            mark_source = "retained_market_watch"
    observation = observation or {}
    return wallet_position_valuation(
        position,
        price_usd=observation.get("price_usd"),
        observed_at=observation.get("observed_at"),
        mark_source=mark_source,
        now=now,
    )


def _service_order_controls(
    data_dir: Path,
    sender: str,
    network: str,
    allowances: object,
) -> dict[str, object]:
    """Expose control dispositions without granting or changing order authority."""
    empty = {
        "schema": "pumpwizard.service-order-controls/v1",
        "cancelled_reviews": [],
        "unresolved_submissions": [],
        "authority": {"orders": False, "signer": False, "submission": False},
    }
    if not isinstance(allowances, list):
        return empty
    allowance_ids = {
        str(row.get("id"))
        for row in allowances
        if isinstance(row, dict) and row.get("id")
    }
    session_ids = {
        str(row.get("session_id"))
        for row in allowances
        if isinstance(row, dict) and row.get("session_id")
    }
    if not allowance_ids or not session_ids:
        return empty

    from .manual_trades import TradeJournal

    journal = TradeJournal(data_dir)
    try:
        tables = {
            str(row[0])
            for row in journal.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {
            "agent_orders", "trade_previews", "trades",
            "main_wallet_allowance_events",
        }
        if not required.issubset(tables):
            return empty

        cancelled_by_id: dict[str, dict[str, object]] = {}
        placeholders = ",".join("?" for _ in allowance_ids)
        events = journal.db.execute(
            "SELECT at,detail,payload FROM main_wallet_allowance_events "
            f"WHERE kind='unsigned-reviews-cancelled' AND allowance_id IN ({placeholders}) "
            "ORDER BY at DESC LIMIT 50",
            tuple(sorted(allowance_ids)),
        )
        for event in events:
            try:
                payload = json.loads(event["payload"])
            except (TypeError, ValueError):
                continue
            order_ids = payload.get("order_ids") if isinstance(payload, dict) else None
            if not isinstance(order_ids, list):
                continue
            for order_id in order_ids:
                if isinstance(order_id, str) and order_id not in cancelled_by_id:
                    cancelled_by_id[order_id] = {
                        "cancelled_at": float(event["at"]),
                        "detail": str(event["detail"]),
                    }

        cancelled_reviews: list[dict[str, object]] = []
        for order_id, disposition in list(cancelled_by_id.items())[:25]:
            row = journal.db.execute(
                "SELECT o.id,o.agent_id,o.side,o.status,o.created_at,p.payload "
                "FROM agent_orders o JOIN trade_previews p ON p.id=o.id WHERE o.id=?",
                (order_id,),
            ).fetchone()
            if row is None or row["status"] != "cancelled" or row["agent_id"] not in session_ids:
                continue
            try:
                preview = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if (preview.get("sender"), preview.get("genesis")) != (sender, GENESIS[network]):
                continue
            cancelled_reviews.append({
                "order_id": row["id"],
                "session_id": row["agent_id"],
                "side": row["side"],
                "status": "cancelled",
                "created_at": float(row["created_at"]),
                **disposition,
            })

        session_placeholders = ",".join("?" for _ in session_ids)
        submissions = journal.db.execute(
            "SELECT o.id,o.agent_id,o.side,o.created_at,t.signature,t.status AS submission_state "
            "FROM agent_orders o JOIN trades t ON t.id=o.id "
            f"WHERE o.status='submitted' AND o.agent_id IN ({session_placeholders}) "
            "AND t.sender=? AND t.genesis=? ORDER BY o.created_at DESC LIMIT 25",
            (*sorted(session_ids), sender, GENESIS[network]),
        )
        unresolved_submissions = [{
            "order_id": row["id"],
            "session_id": row["agent_id"],
            "side": row["side"],
            "status": "submitted",
            "submission_state": row["submission_state"],
            "signature": row["signature"],
            "created_at": float(row["created_at"]),
        } for row in submissions]
        return {
            **empty,
            "cancelled_reviews": cancelled_reviews,
            "unresolved_submissions": unresolved_submissions,
        }
    finally:
        journal.close()


def _monitor_plan(library: StrategyLibrary, instance: dict) -> dict[str, object]:
    """Expose the bounded strategy inputs needed to understand a monitor."""
    stored = library.strategy(str(instance["strategy_digest"]))
    rules = parse_package(stored["package"]).effective_rules

    def number(name: str) -> float | None:
        value = rules.get(name)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    return {
        "entry_style": str(rules.get("entry_style", "unknown")),
        "entry_cap_min": number("entry_cap_min") if "entry_cap_min" in rules else number("entry_cap_min_usd"),
        "entry_cap_max": number("entry_cap_max") if "entry_cap_max" in rules else number("entry_cap_max_usd"),
        "observation_seconds": number("observation_seconds"),
        "minimum_samples": number("minimum_samples"),
        "minimum_price_changes": number("minimum_price_changes"),
        "order_usd": number("order_usd"),
        "max_positions": number("max_positions"),
        "min_curve_liquidity_usd": number("min_curve_liquidity_usd"),
        "min_amm_liquidity_usd": number("min_amm_liquidity_usd"),
        "scan_interval_seconds": 15,
    }


def _risk_policy(library: StrategyLibrary, instance_id: str, query: dict[str, list[str]]) -> dict | None:
    sender = query.get("sender", [None])[0]
    network = query.get("network", [None])[0]
    if not isinstance(sender, str) or network not in GENESIS:
        return None
    from .manual_trades import TradeJournal
    from .strategy_risk import metrics, policy

    journal = TradeJournal(library.path.parent)
    try:
        saved = policy(journal, instance_id, GENESIS[network], sender)
        if saved is None:
            return None
        available = metrics(journal, instance_id, GENESIS[network], sender)["allocated_lamports"]
        return {**saved, "available_cash_lamports": str(available)}
    finally:
        journal.close()


def _edit_session_configuration(library: StrategyLibrary, instance_id: str, body: object) -> dict[str, object]:
    fields = {
        "entry_cap_min", "entry_cap_max", "order_usd", "min_curve_liquidity_usd",
        "min_amm_liquidity_usd", "stop_loss_percent", "trailing_exit_percent",
        "max_hold_hours", "sender", "network",
    }
    if not isinstance(body, dict) or set(body) != fields:
        raise ValueError("Supply every editable strategy setting")

    def number(name: str) -> float:
        value = body[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name.replace('_', ' ')} must be a number")
        result = float(value)
        if not result > 0 or result == float("inf"):
            raise ValueError(f"{name.replace('_', ' ')} must be positive")
        return result

    minimum, maximum = number("entry_cap_min"), number("entry_cap_max")
    if minimum >= maximum:
        raise ValueError("Minimum market cap must be lower than maximum market cap")
    order = number("order_usd")
    curve, amm = number("min_curve_liquidity_usd"), number("min_amm_liquidity_usd")
    stop, trailing = number("stop_loss_percent"), number("trailing_exit_percent")
    hold_hours = number("max_hold_hours")
    if stop >= 100 or trailing >= 100:
        raise ValueError("Loss and trailing-exit percentages must be below 100")
    sender, network = body["sender"], body["network"]
    if not isinstance(sender, str) or not isinstance(network, str) or network not in GENESIS:
        raise ValueError("Choose the connected Solana wallet")
    public_key(sender)

    instance = library.instance(instance_id)
    package = copy.deepcopy(instance["package"])
    configuration = package.get("configuration") if isinstance(package, dict) else None
    if not isinstance(configuration, dict) or configuration.get("version") != "early-ladder-v1":
        raise ValueError("Only complete local early-ladder strategies can be edited")
    configuration.update(
        entry_cap_min=minimum,
        entry_cap_max=maximum,
        order_usd=order,
        budget_usd=max(order, float(configuration.get("budget_usd", order))),
        min_curve_liquidity_usd=curve,
        min_amm_liquidity_usd=amm,
        stop_loss_fraction=-stop / 100,
        trailing_drawdown_fraction=trailing / 100,
        max_hold_seconds=hold_hours * 3600,
    )
    strategy = parse_package(package)
    library.revise_instance_strategy(instance_id, strategy)
    from .manual_trades import TradeJournal
    from .strategy_risk import update_trade_loss

    journal = TradeJournal(library.path.parent)
    try:
        update_trade_loss(journal, instance_id, GENESIS[network], sender, round(stop * 100))
    finally:
        journal.close()
    return {
        "instance": library.instance(instance_id),
        "notice": "Saved a new immutable local strategy revision. Monitoring and reviewed entries remain off.",
    }


def browser_session_state(library, instance, tracking, health, held):
    state = session_state(instance, tracking, health, held=held)
    lease = library.monitor_lease()
    if (tracking["enabled"] or held) and not tracking.get("fault") and (
        lease is None or not time.time() < lease["expires_at"] <= time.time() + 181
    ):
        state = dict(label="Waiting for monitor", tone="warning", action="View activity",
                     reason="Tracking is enabled but no active monitoring service holds the local lease.")
    return state


def _session_management(library: StrategyLibrary, instance_id: str) -> dict[str, object]:
    """Check both durable stores before an active strategy can be hidden."""
    from .manual_trades import TradeJournal

    blocks = library.removal_blocks(instance_id)
    journal = TradeJournal(library.path.parent)
    try:
        if journal.db.execute(
            "SELECT 1 FROM agent_orders WHERE agent_id=? AND status IN ('review','submitted') LIMIT 1",
            (instance_id,),
        ).fetchone():
            blocks.append("Finish or cancel this strategy's active order before removing it")
        if journal.db.execute(
            """SELECT 1 FROM agent_positions a JOIN trade_positions p ON p.id=a.position_id
               JOIN agent_reservations h ON h.id=a.token_hold
               WHERE a.agent_id=? AND p.status='open' AND h.status='held' LIMIT 1""",
            (instance_id,),
        ).fetchone():
            blocks.append("Close or release this strategy's managed wallet position before removing it")
        held = list(journal.db.execute(
            "SELECT lamports,position_id,token_atoms FROM agent_reservations WHERE agent_id=? AND status='held'",
            (instance_id,),
        ))
        if any(row["position_id"] is not None and int(row["token_atoms"]) > 0 for row in held):
            blocks.append("Release this strategy's managed token allocation before removing it")
        allocation_lamports = sum(
            int(row["lamports"]) for row in held if row["position_id"] is None
        )
        if journal.db.execute(
            "SELECT 1 FROM signing_sessions WHERE agent_id=? AND status='active' LIMIT 1",
            (instance_id,),
        ).fetchone():
            blocks.append("Revoke this strategy's active signing session before removing it")
        has_allowance_table = journal.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='main_wallet_allowances'"
        ).fetchone()
        if has_allowance_table and journal.db.execute(
            "SELECT 1 FROM main_wallet_allowances WHERE session_id=? "
            "AND status IN ('authorized','active','entry-paused') LIMIT 1",
            (instance_id,),
        ).fetchone():
            blocks.append("Revoke this strategy's main-wallet allowance before removing it")
        authorizations = journal.db.execute(
            "SELECT scope FROM automation_authorizations WHERE status IN ('authorized','running','paused')"
        )
        for row in authorizations:
            try:
                strategies = json.loads(row["scope"]).get("strategies", {})
            except (AttributeError, TypeError, json.JSONDecodeError):
                strategies = {}
            if instance_id in strategies:
                blocks.append("Stop the Automation Wallet authorization that includes this strategy")
                break
    finally:
        journal.close()
    result = {
        "removal_blocks": list(dict.fromkeys(blocks)),
        "allocation_lamports": str(allocation_lamports),
    }
    instance = library.instance(instance_id)
    workspace_id = instance.get("workspace_id")
    if workspace_id is not None:
        workspace = library.session_workspace(str(workspace_id))
        result["workspace_allocation_lamports"] = str(workspace["allocation_lamports"])
    return result


def _release_session_allocation(library: StrategyLibrary, instance_id: str) -> int:
    """Release cash holds under the wallet lock after explicit removal confirmation."""
    from .manual_trades import TradeJournal

    journal = TradeJournal(library.path.parent)
    try:
        with journal.db:
            journal.db.execute("BEGIN IMMEDIATE")
            rows = list(journal.db.execute(
                """SELECT id,genesis,sender,lamports FROM agent_reservations
                   WHERE agent_id=? AND status='held' AND position_id IS NULL""",
                (instance_id,),
            ))
            if journal.db.execute(
                "SELECT 1 FROM agent_orders WHERE agent_id=? AND status IN ('review','submitted') LIMIT 1",
                (instance_id,),
            ).fetchone():
                raise TransferError("Finish or cancel this strategy's active order before removing it")
            if journal.db.execute(
                "SELECT 1 FROM signing_sessions WHERE agent_id=? AND status='active' LIMIT 1",
                (instance_id,),
            ).fetchone():
                raise TransferError("Revoke this strategy's active signing session before removing it")
            now = time.time()
            journal.db.execute(
                """UPDATE agent_reservations SET status='released',released_at=?
                   WHERE agent_id=? AND status='held' AND position_id IS NULL""",
                (now, instance_id),
            )
            for genesis, sender in {(row["genesis"], row["sender"]) for row in rows}:
                journal.db.execute(
                    """INSERT INTO wallet_revisions VALUES (?,?,1)
                       ON CONFLICT(genesis,sender) DO UPDATE SET revision=revision+1""",
                    (genesis, sender),
                )
        return sum(int(row["lamports"]) for row in rows)
    finally:
        journal.close()


def payload(library: StrategyLibrary, path: str, query: dict[str, list[str]],
            wallet_authorizer: WalletAuthorizer | None = None,
            history_service: MarketHistoryService | None = None,
            automation_controller=None,
            main_wallet_controller: MainWalletAutopilotController | None = None,
            background_service=None) -> dict:
    if path == "/api/v1/app-state":
        from .app_state import read
        return envelope(**read(library.path.parent))
    if path == "/api/v1/capabilities":
        return first_use_capabilities(background_service)
    if path == "/api/v1/research" or path.startswith("/api/v1/research/"):
        return _research_payload(library, path, query)
    if path.startswith("/api/v1/service/"):
        if background_service is None:
            raise ValueError("Background service status is unavailable in browser-owned mode")
        status = background_service.status()
        if path == "/api/v1/service/status":
            from .build_identity import current_build_identity

            return envelope(**status, build_identity=current_build_identity())
        if path == "/api/v1/service/jobs":
            return envelope(jobs=status["jobs"])
        if path == "/api/v1/service/coverage":
            return envelope(coverage=status["coverage"])
        if path == "/api/v1/service/trading":
            return envelope(trading=status["trading"])
        if path == "/api/v1/service/signing":
            sender = query.get("sender", [""])[0]
            network = query.get("network", [""])[0]
            if wallet_authorizer is None:
                raise TransferError("Wallet sign-in is unavailable")
            wallet_authorizer.require(sender, network)
            overview = background_service.signing_overview(sender, network)
            return envelope(
                **overview,
                order_controls=_service_order_controls(
                    library.path.parent, sender, network, overview.get("allowances"),
                ),
            )
        if path == "/api/v1/service/signing/acceptance/evidence":
            sender = query.get("sender", [""])[0]
            network = query.get("network", [""])[0]
            approval_id = query.get("approval_id", [""])[0]
            if wallet_authorizer is None:
                raise TransferError("Wallet sign-in is unavailable")
            wallet_authorizer.require(sender, network)
            return envelope(
                evidence=background_service.signing_acceptance_evidence(
                    sender, network, approval_id,
                )
            )
        raise ValueError("Unknown background service read endpoint")
    if path == "/api/v1/release-note":
        return envelope(note=note_for(__version__), url=note_url(__version__))
    if path == "/api/v1/workspace":
        return workspace(library)
    if path == '/api/v1/diagnostics':
        from .diagnostics import report
        return envelope(report=report(library, background_service))
    if path == '/api/v1/rpc':
        from .rpc_settings import overview
        return envelope(connection=overview(library.path.parent))
    if path.startswith('/api/v1/session-workspaces/') and path.endswith('/funds'):
        from .web_session_controls import owned_workspace
        from .web_session_allocations import funds
        ident = path.removeprefix('/api/v1/session-workspaces/').removesuffix('/funds')
        selected = owned_workspace(library, ident, query, wallet_authorizer)
        return envelope(funds=funds(library, library.path.parent, selected))
    if path.startswith('/api/v1/session-workspaces/') and path.endswith('/sell-all'):
        from .web_session_controls import owned_workspace, live_positions
        ident = path.removeprefix('/api/v1/session-workspaces/').removesuffix('/sell-all')
        selected = owned_workspace(library, ident, query, wallet_authorizer)
        if selected['mode'] != 'trade':
            raise ValueError('Sell all requires a trading session')
        result = trade_state(library.path.parent, selected['wallet_sender'], selected['wallet_network'])
        ids = library.session_workspace_instance_ids(ident)
        rows = [p for p in live_positions(selected, ids, result) if p['status'] == 'open']
        return envelope(session_id=ident, positions=rows, pending=result['pending'])
    if path == "/api/v1/session-workspaces":
        from .manual_trades import TradeJournal
        from .session_allocations import summary as session_allocation_summary
        from .session_funding import status as session_funding_status

        from .workspace_scope import read_scope, workspace_matches
        scope = read_scope(query)
        rows = []
        wallet_trades = {}
        journal = TradeJournal(library.path.parent)
        try:
            for session_workspace in library.session_workspaces():
                if not workspace_matches(session_workspace, scope):
                    continue
                instance_ids = library.session_workspace_instance_ids(str(session_workspace["id"]))
                history = library.session_workspace_strategy_history(str(session_workspace["id"]))
                running = 0
                attention = 0
                for instance_id in instance_ids:
                    tracking = library.tracking_settings(instance_id)
                    if tracking["enabled"] and not tracking["fault"]:
                        running += 1
                    if tracking["fault"]:
                        attention += 1
                allocation_summary = (
                    session_allocation_summary(journal, str(session_workspace["id"]))
                    if session_workspace["mode"] == "trade"
                    else {
                        "available_lamports": "0",
                        "committed_lamports": "0",
                        "attributed_open_cost_lamports": "0",
                        "allocation_status": "none",
                    }
                )
                from .web_session_controls import performance, position_summary
                identity = (session_workspace.get('wallet_sender'), session_workspace.get('wallet_network'))
                if session_workspace['mode'] == 'trade' and identity not in wallet_trades:
                    wallet_trades[identity] = trade_state(library.path.parent, *identity)
                pnl = performance(library, session_workspace, [row['id'] for row in history], wallet_trades.get(identity, {}))
                strategy_performance = [
                    {**item, "performance": performance(
                        library, session_workspace, [item['id']], wallet_trades.get(identity, {}),
                    )}
                    for item in history
                ]
                rows.append({
                    **session_workspace,
                    "funding": (session_funding_status(journal, session_workspace['wallet_sender'], session_workspace['wallet_network'])
                                if session_workspace['mode'] == 'trade' and session_workspace['wallet_sender'] else None),
                    "performance": pnl,
                    "strategy_performance": strategy_performance,
                    "position_summary": (position_summary(library, session_workspace, [row['id'] for row in history], wallet_trades.get(identity, {}))
                                         if session_workspace['mode'] == 'trade' else None),
                    **allocation_summary,
                    "strategy_ids": instance_ids,
                    "history_strategies": history,
                    "running_strategies": running,
                    "attention_strategies": attention,
                })
        finally:
            journal.close()
        return envelope(rows=rows)
    if path == "/api/v1/session-activity":
        raw_cursor = query.get("after", [None])[0]
        from .workspace_scope import read_scope, scoped_instance_ids
        return envelope(**library.entry_activity(
            None if raw_cursor is None else int(raw_cursor), scoped_instance_ids(library, read_scope(query)),
        ))
    if path == "/api/v1/sessions":
        from .workspace_scope import read_scope, scoped_instance_ids
        visible = scoped_instance_ids(library, read_scope(query))
        rows = []
        for instance in library.instances():
            ident = str(instance["id"])
            if visible is not None and ident not in visible:
                continue
            tracking, health = library.tracking_settings(ident), library.monitor_health(ident)
            positions = _session_positions(library, ident)
            held_mints = {str(position["mint"]) for position in positions}
            rows.append({"instance": instance, "brief": library.session_brief(ident),
                         "paper_research_allowed": monitoring_session(library, ident),
                         "tracking": tracking, "health": health,
                         "state": browser_session_state(library, instance, tracking, health, len(library.position_views(str(instance["id"])))),
                         "positions": positions, "exit_plan": _exit_plan(library, instance),
                         "monitor_plan": _monitor_plan(library, instance),
                         "risk_policy": _risk_policy(library, ident, query),
                         "entry_considerations": [item for item in library.entry_considerations(ident, 12)
                                                  if str(item["mint"]) not in held_mints],
                         "decisions": library.decisions(ident, 5)})
        return envelope(rows=rows)
    if path.startswith("/api/v1/sessions/"):
        from .workspace_scope import read_scope, scoped_instance_ids
        visible = scoped_instance_ids(library, read_scope(query))
        selected_id = path.removeprefix("/api/v1/sessions/").split("/", 1)[0]
        if visible is not None and selected_id not in visible:
            raise ValueError("Session does not belong to the selected wallet workspace")
    if path.startswith("/api/v1/sessions/") and path.endswith("/reviews"):
        instance_id = path.removeprefix("/api/v1/sessions/").removesuffix("/reviews").rstrip("/")
        if not instance_id:
            raise ValueError("Unknown strategy instance")
        return _session_reviews(library, instance_id)
    if path.startswith("/api/v1/sessions/"):
        instance_id = path.rsplit("/", 1)[-1]
        instance = library.instance(instance_id)
        tracking, health = library.tracking_settings(instance_id), library.monitor_health(instance_id)
        positions = _session_positions(library, instance_id)
        held_mints = {str(position["mint"]) for position in positions}
        lease = library.monitor_lease()
        return envelope(instance=instance, brief=library.session_brief(instance_id),
                        paper_research_allowed=monitoring_session(library, instance_id),
                        tracking=tracking, health=health,
                        state=browser_session_state(library, instance, tracking, health, len(library.position_views(str(instance["id"])))), positions=positions,
                        exit_plan=_exit_plan(library, instance),
                        monitor_plan=_monitor_plan(library, instance),
                        risk_policy=_risk_policy(library, instance_id, query),
                        entry_considerations=[item for item in library.entry_considerations(instance_id, 12)
                                              if str(item["mint"]) not in held_mints],
                        observations=library.latest_observations(instance_id, 12),
                        decisions=library.decisions(instance_id, 100),
                        outcomes=library.decision_outcomes(instance_id, 100),
                        runs=library.monitor_runs(instance_id, 25),
                        monitor_lease=lease,
                        management=_session_management(library, instance_id))
    if path == "/api/v1/market":
        limit = min(500, max(1, int(query.get("limit", ["100"])[0])))
        rows = library.market_watch_rows(500)
        rows = filter_market(rows, query=query.get("query", [""])[0],
                             minimum_cap=float(query.get("minimum_cap", ["0"])[0]),
                             maximum_cap=float(query.get("maximum_cap", ["inf"])[0]),
                             minimum_liquidity=float(query.get("minimum_liquidity", ["0"])[0]),
                             fresh_only=query.get("fresh_only", ["false"])[0].lower() == "true",
                             sort=query.get("sort", ["Newest first"])[0])
        return envelope(rows=rows[:limit])
    if path == '/api/v1/strategy-sizing':
        from .strategy_editor import allocation_sizing
        from .workspace_scope import read_scope
        return envelope(sizing=allocation_sizing(library, read_scope(query),
                        query.get('workspace_id', [''])[0], query.get('trade_percent', ['10'])[0],
                        CompositePumpMarketSource()))
    if path == "/api/v1/strategies":
        from .workspace_scope import read_scope, strategy_digests
        scope = read_scope(query)
        visible = strategy_digests(library, scope)
        rows = [row for row in library.rows() if visible is None or row["digest"] in visible]
        for row in rows:
            package = library.strategy(str(row["digest"]))["package"]
            configuration = package.get("configuration") if isinstance(package, dict) else None
            row["configuration"] = configuration if isinstance(configuration, dict) else {}
            row['allocation_sizing'] = [json.loads(saved[0]) for saved in library._connection.execute(
                'SELECT payload FROM strategy_allocation_sizing WHERE digest=? AND sender=? AND network=?',
                (row['digest'], *(scope or ('', ''))))]
        return envelope(rows=rows)
    if path.startswith("/api/v1/strategies/"):
        suffix = path.removeprefix("/api/v1/strategies/")
        export = suffix.endswith("/export")
        digest = suffix.removesuffix("/export").rstrip("/")
        if not digest or "/" in digest:
            raise ValueError("Strategy detail requires one imported strategy digest")
        from .workspace_scope import read_scope, require_strategy
        require_strategy(library, digest, read_scope(query))
        stored = library.strategy(digest)
        package = stored.pop("package")
        parsed = parse_package(package)
        if export:
            return envelope(
                package=package,
                filename=f"{parsed.strategy_id}-{parsed.digest[:12]}.pumpwizard-strategy.json",
                notice="Exact imported pumpwizard.strategy-handoff/v1 package. Exporting does not change local compatibility, monitoring, wallet state, or execution permissions.",
            )
        return envelope(
            strategy=stored,
            identity=strategy_identity(parsed),
            capability=support_label(parsed),
            support=support_text(parsed),
        )
    if path == "/api/v1/marketplace":
        catalog = load_catalog(library.path.parent / "strategy-catalog.json")
        rows = []
        for row in catalog["strategies"]:
            package = package_for(row)
            parsed = parse_package(package)
            rows.append({"id": row["id"], "digest": parsed.digest,
                         "label": row["label"], "family": row["family"],
                         "description": row["description"], "report": row.get("report"),
                         "source_as_of": row.get("source_as_of"), "source_stale": bool(row.get("source_stale")),
                         "compatibility": parsed.compatibility, "reason": parsed.reason, "package": package})
        return envelope(rows=rows, source="bundled/cached local strategy catalog; no website fetch")
    if path == "/api/v1/evaluations":
        return envelope(rows=evaluations(library.path.parent, library))
    if path == "/api/v1/shadow-reports":
        return envelope(rows=shadow(library.path.parent, query.get("agent_id", [None])[0]))
    if path == "/api/v1/worker-rehearsals":
        return envelope(rows=worker(library.path.parent, query.get("agent_id", [None])[0]))
    if path == "/api/v1/portfolio-replays":
        return envelope(rows=replays(library.path.parent))
    if path == "/api/v1/wallet-state":
        state = wallet_state(library.path.parent)
        state["balance_snapshots"] = library.recent_wallet_snapshots()
        return envelope(**state, browser_wallet_connected=False,
                        signing_available=False, automatic_execution_enabled=False)
    if path == "/api/v1/wallet/inventory":
        # The enclosing HTTP handler still requires the local browser token.
        # Reading public balances neither signs in nor exposes wallet journal/history.
        return envelope(**wallet_portfolio.inventory(
            library.path.parent, library, query.get("sender", [""])[0],
            query.get("network", [""])[0],
            refresh=query.get("refresh", ["false"])[0].lower() == "true",
            market_source=CompositePumpMarketSource(),
        ))
    if path == "/api/v1/wallet/balance":
        # Public chain data only, protected by the local browser token like inventory.
        # This never grants ownership proof, journal access or signing authority.
        from .wallet_native_balance import read as native_balance
        sender = query.get("sender", [""])[0]
        network = query.get("network", [""])[0]
        return envelope(**native_balance(
            library.path.parent, sender, network,
            refresh=query.get("refresh", ["false"])[0].lower() == "true",
            market_source=CompositePumpMarketSource(),
        ))
    if path == "/api/v1/wallet/balance-history":
        # Public confirmed balances; private Portfolio/journal routes still require proof.
        from .wallet_balance_history import read as balance_history
        sender = query.get("sender", [""])[0]
        network = query.get("network", [""])[0]
        currency = query.get("currency", ["SOL"])[0]
        if currency not in ("SOL", "USD"):
            raise TransferError("Choose SOL or USD for wallet history")
        history = balance_history(
            library.path.parent, sender, network,
            refresh=query.get("refresh", ["false"])[0].lower() == "true",
        )
        if currency == "USD":
            from .wallet_usd_history import value_history
            from .wallet_native_balance import read as native_balance
            try:
                current = native_balance(library.path.parent, sender, network,
                                         market_source=CompositePumpMarketSource())
                current_usd = (int(history['rows'][-1]['lamports']) / 1e9 * current['sol_usd_price']
                               if history['rows'] and history['rows'][-1]['lamports'] is not None and current['sol_usd_price'] else None)
            except (TransferError, KeyError, TypeError, ValueError):
                current_usd = None
            history = value_history(library.path.parent, history, current_usd)
        return envelope(**history)
    if path == "/api/v1/wallet/portfolio":
        sender = query.get("sender", [""])[0]
        network = query.get("network", [""])[0]
        if wallet_authorizer is None:
            raise TransferError("Wallet sign-in is unavailable")
        wallet_authorizer.require(sender, network)
        try:
            limit = int(query.get("limit", [str(wallet_portfolio.HISTORY_LIMIT)])[0])
        except (TypeError, ValueError):
            raise TransferError("Choose a valid wallet history limit") from None
        refresh = query.get("refresh", ["false"])[0].lower() == "true"
        return envelope(**wallet_portfolio.read(
            library.path.parent, library, sender, network, limit=limit, refresh=refresh,
            market_source=CompositePumpMarketSource(),
        ))
    if path == "/api/v1/strategy-controls":
        sender = query.get("sender", [None])[0]
        network = query.get("network", [None])[0]
        if wallet_authorizer is None:
            raise TransferError("Wallet sign-in is unavailable")
        wallet_authorizer.require(sender, network)
        return envelope(**strategy_controls(library, library.path.parent, sender, network))
    if path in ("/api/v1/main-wallet-autopilot", "/api/v1/main-wallet-autopilot/status"):
        sender = query.get("sender", [None])[0]
        network = query.get("network", [None])[0]
        session_id = query.get("session_id", [None])[0]
        if wallet_authorizer is None or main_wallet_controller is None:
            raise TransferError("Main-wallet Autopilot is unavailable")
        wallet_authorizer.require(sender, network)
        return envelope(**main_wallet_controller.status(sender, network, session_id))
    if path == '/api/v1/automation':
        sender, network = query.get('sender', [''])[0], query.get('network', [''])[0]
        if wallet_authorizer is None:
            raise TransferError('Wallet sign-in is unavailable')
        wallet_authorizer.require(sender, network)
        return envelope(**web_automation.state(
            library.path.parent, sender, network, library=library,
            controller=automation_controller,
        ))
    if path == "/api/v1/agent-orders":
        sender, network = query.get('sender', [''])[0], query.get('network', [''])[0]
        if wallet_authorizer is None:
            raise TransferError('Wallet sign-in is unavailable')
        wallet_authorizer.require(sender, network)
        return envelope(**web_agent_orders.overview(library, sender, network))
    if path == "/api/v1/trades":
        sender = query.get("sender", [None])[0]
        network = query.get("network", [None])[0]
        if wallet_authorizer is None:
            raise TransferError("Wallet sign-in is unavailable")
        wallet_authorizer.require(sender, network)
        result = trade_state(library.path.parent, sender, network)
        instances = {str(row["id"]): row for row in library.instances()}
        now = time.time()
        for position in result["positions"]:
            owner = instances.get(str(position.get("agent_id") or ""))
            position["strategy_label"] = owner.get("label") if owner else None
            position["exit_plan"] = _exit_plan(library, owner) if owner and position.get('managed') else None
            position["valuation"] = _trade_position_valuation(library, position, now=now)
        return envelope(**result)
    if path.startswith("/api/v1/coins/"):
        mint = path.rsplit("/", 1)[-1]
        limit = min(500, max(1, int(query.get("limit", ["100"])[0])))
        candle_limit = min(1000, max(1, int(query.get("candle_limit", ["500"])[0])))
        raw_before = query.get("before", [None])[0]
        before = float(raw_before) if raw_before is not None else None
        raw_candle_before = query.get("candle_before", [None])[0]
        candle_before = float(raw_candle_before) if raw_candle_before is not None else None
        history = library.coin_history(mint, limit, before=before)
        older = bool(history and library.coin_history(mint, 1, before=float(history[-1]["observed_at"])))
        candles = library.market_history_candles(mint, candle_limit, before=candle_before, source=SOURCE)
        older_candles = bool(candles and library.market_history_candles(
            mint, 1, before=float(candles[-1]["opened_at"]), source=SOURCE
        ))
        job = library.market_history_job(mint, SOURCE, RESOLUTION_SECONDS) or {
            "mint": mint, "source": SOURCE, "resolution_seconds": RESOLUTION_SECONDS,
            "status": "not_started", "before_at": None, "earliest_at": None,
            "latest_at": None, "row_count": 0, "updated_at": None, "error": "",
            "coverage": "Earlier public price history will load automatically.",
        }
        if job['status'] == 'complete' and job.get('updated_at') and time.time()-job['updated_at'] >= 300 and pool_histories(library, mint):
            job = {**job, 'status': 'ready'}
        return envelope(mint=mint, history=history,
                        next_before=float(history[-1]["observed_at"]) if older else None,
                        candles=candles, pool_histories=pool_histories(library, mint),
                        next_candle_before=float(candles[-1]["opened_at"]) if older_candles else None,
                        history_backfill=job,
                        activity=library.coin_activity(mint, 100),
                        metadata=library.coin_metadata(mint))
    raise ValueError("Unknown read endpoint")


def refresh_market(library: StrategyLibrary, source=None, *, minimum_interval_seconds: float = 0) -> dict[str, object]:
    """Run one bounded public market scan; no wallet, RPC or monitor is involved."""
    if (isinstance(minimum_interval_seconds, bool) or
            not isinstance(minimum_interval_seconds, (int, float)) or minimum_interval_seconds < 0):
        raise ValueError("Market refresh interval must be a non-negative number")
    key = str(library.path.resolve())
    with _MARKET_REFRESH_LOCK:
        now = time.monotonic()
        elapsed = now - _MARKET_REFRESH_AT.get(key, -minimum_interval_seconds)
        if minimum_interval_seconds and elapsed < minimum_interval_seconds:
            retry_after = max(1, int(minimum_interval_seconds - elapsed + .999))
            return envelope(collected=0, retained=len(library.market_watch_rows(500)), refreshed=False,
                            retry_after_seconds=retry_after,
                            notice="The latest public market scan is still current. Retained observations were returned without another provider request.")
        selected_source = source or CompositePumpMarketSource()
        quotes = selected_source.collect(5_000, 10_000_000, 100)
        for quote in quotes:
            library.record_market_watch_quote(quote, bucket_seconds=300)
        compacted = library.compact_market_watch_history()
        _MARKET_REFRESH_AT[key] = time.monotonic()
        return envelope(collected=len(quotes), retained=len(library.market_watch_rows(500)), refreshed=True,
                        compacted=compacted,
                        retry_after_seconds=int(minimum_interval_seconds),
                        notice="Public market scan stored local observations only. It did not inspect a wallet or authorize trading.")


def run_evaluation(library: StrategyLibrary, strategy_digest: str, recording: object) -> dict[str, object]:
    """Save one explicit, local-only evaluation for an already imported strategy."""
    if not strategy_digest or "/" in strategy_digest:
        raise ValueError("Evaluation requires an imported strategy digest")
    strategy = library.strategy(strategy_digest)
    selected_recording = example_recording() if recording == {} else recording
    if not isinstance(selected_recording, dict):
        raise ValueError("Choose the bundled example or provide a recording JSON object")
    report = evaluate(library.path.parent, strategy["package"], selected_recording)
    return envelope(
        evaluation={
            "id": report["id"],
            "strategy_digest": strategy_digest,
            "strategy_label": strategy["label"],
            "completed_at": report["completed_at"],
            "summary": report["summary"],
            "limitations": report["limitations"],
            "submission_enabled": report["submission_enabled"],
            "funded_acceptance": report["funded_acceptance"],
        },
        notice="Offline modeled evaluation saved locally. It did not contact RPC, inspect a wallet, reserve funds, create an order, sign, or trade.",
    )


def run_portfolio_replay(library: StrategyLibrary, run_id: str, recording: object) -> dict[str, object]:
    """Advance the existing isolated virtual portfolio journal with local input only."""
    if not REPLAY_RUN_ID.fullmatch(run_id):
        raise ValueError("Replay ID must use 1–80 letters, numbers, dots, underscores, or hyphens")
    if not isinstance(recording, dict) or recording.get("schema") != "pumpwizard.portfolio-recording/v1":
        raise ValueError("Choose a pumpwizard.portfolio-recording/v1 recording")
    batches = recording.get("batches")
    if not isinstance(batches, list) or not 1 <= len(batches) <= MAX_REPLAY_BATCHES:
        raise ValueError("Replay recording must contain 1–20,000 batches")
    if len(json.dumps(recording, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")) > EVALUATION_MAX_BYTES:
        raise ValueError("Replay recording exceeds the 16 MiB local limit")
    # This preserves the existing pinned-configuration guard before the journal
    # creates any replay state.
    PortfolioEngine(recording.get("configuration"))
    journal = PortfolioJournal(library.path.parent)
    try:
        events = []
        for batch in batches:
            events.extend(journal.advance(run_id, batch, recording["configuration"]))
        checkpoint = journal.checkpoint(run_id)
    finally:
        journal.close()
    return envelope(
        replay={"id": run_id, "checkpoint": checkpoint, "new_events": len(events)},
        notice="Offline virtual portfolio replay saved locally. It did not contact RPC, inspect a wallet, reserve funds, create an order, sign, or trade.",
    )


def run_monitor_scan(library: StrategyLibrary, instance_id: str, scheduler=None) -> dict[str, object]:
    """Explicitly run one existing local monitor-scheduler pass without execution access."""
    instance = library.instance(instance_id)
    tracking = library.tracking_settings(instance_id)
    if not tracking["enabled"] or instance["status"] != "monitoring":
        raise ValueError("Start local monitoring before requesting a one-time scan")
    with library._connection:
        library._connection.execute('UPDATE monitor_settings SET next_at=0 WHERE instance_id=?', (instance_id,))
    runner = scheduler or MonitorScheduler(library.path.parent)
    completed = runner.run_cycle(library)
    if not completed and hasattr(runner, 'wake'):
        runner.wake()
    return envelope(
        instance=library.instance(instance_id),
        completed=completed,
        notice="One bounded local monitoring pass was requested. It only collects public market observations and recommendations; it cannot create an order, sign, move funds, or trade.",
    )


def command(library: StrategyLibrary, path: str, body: object,
            wallet_authorizer: WalletAuthorizer | None = None,
            request_origin: str | None = None,
            history_service: MarketHistoryService | None = None,
            monitor_scheduler: MonitorScheduler | None = None,
            automation_controller=None,
            main_wallet_controller: MainWalletAutopilotController | None = None,
            background_service=None, query=None) -> dict[str, object]:
    """Run authenticated browser commands; wallet signing stays in the wallet."""
    if not isinstance(body, dict):
        raise ValueError("Browser command body must be a JSON object")
    if path.startswith("/api/v1/app-state/"):
        from .app_state import mutate
        return envelope(**mutate(library.path.parent, path, body))
    if path.startswith('/api/v1/strategies/') and path.endswith('/revisions'):
        from .strategy_editor import revised_package, allocation_sizing
        from .workspace_scope import read_scope, require_strategy
        scope = read_scope(query or {}) or ('', '')
        if scope[0]:
            if wallet_authorizer is None:
                raise TransferError('Wallet sign-in is unavailable')
            wallet_authorizer.require(*scope)
        digest = path.removeprefix('/api/v1/strategies/').removesuffix('/revisions')
        require_strategy(library, digest, scope)
        values = dict(body)
        target_id = values.pop('instance_id', None)
        if target_id is not None:
            from .workspace_scope import workspace_matches
            if not isinstance(target_id, str):
                raise ValueError('Choose the strategy attached to this session.')
            target = library.instance(target_id)
            if (target['strategy_digest'] != digest or not target.get('workspace_id')
                    or not workspace_matches(library.session_workspace(target['workspace_id']), scope)):
                raise ValueError('The attached strategy does not match this wallet and source revision.')
            blocks = _session_management(library, target_id)['removal_blocks']
            if blocks:
                raise ValueError('This strategy cannot be edited: ' + blocks[0])
        sizing = values.pop('sizing', None)
        resolved = None
        if sizing is not None:
            if not isinstance(sizing, dict) or set(sizing) != {'workspace_id', 'trade_percent', 'allocation_lamports'}:
                raise ValueError('Sizing requires the selected session, allocation and trade percentage.')
            resolved = allocation_sizing(library, scope, sizing['workspace_id'], sizing['trade_percent'], CompositePumpMarketSource())
            if target_id and resolved['workspace_id'] != target['workspace_id']:
                raise ValueError('Sizing must belong to the attached strategy session.')
            if sizing['allocation_lamports'] != resolved['allocation_lamports']:
                raise ValueError('Session allocation changed. Refresh sizing before saving.')
            if not isinstance(values.get('configuration'), dict):
                raise ValueError('Supply all strategy configuration fields.')
            values['configuration'] = {**values['configuration'], 'budget_usd': resolved['budget_usd'], 'order_usd': resolved['order_usd']}
        strategy = revised_package(library.strategy(digest)['package'], values)
        if target_id:
            def update_inactive_policy():
                if library.instance(target_id)['strategy_digest'] != digest:
                    raise ValueError('Strategy changed. Reload before saving this revision.')
                from .manual_trades import TradeJournal
                from .strategy_risk import policy, save_policy
                from .web_strategy_controls import _balance
                from .transfers import parse_sol
                journal = TradeJournal(library.path.parent)
                try:
                    if journal.db.execute('SELECT 1 FROM agent_orders WHERE agent_id=? LIMIT 1', (target_id,)).fetchone():
                        raise ValueError('This strategy has order history. Create a new strategy session to change its rules.')
                    blocks = _session_management(library, target_id)['removal_blocks']
                    if blocks:
                        raise ValueError('This strategy cannot be edited: ' + blocks[0])
                    current = policy(journal, target_id, GENESIS[scope[1]], scope[0]) if scope[0] else None
                    if current and resolved:
                        genesis, balance = _balance(*scope, library.path.parent)
                        current['max_trade_lamports'] = parse_sol(resolved['trade_budget_sol'])
                        save_policy(journal, target_id, genesis, scope[0], current, balance,
                                    workspace_id=target['workspace_id'])
                finally:
                    journal.close()
            library.revise_instance_strategy(target_id, strategy, scope=scope,
                                             before_update=update_inactive_policy, update_brief=True)
        else:
            library.import_strategy(strategy, scope)
        if resolved:
            with library._connection:
                library._connection.execute('INSERT OR REPLACE INTO strategy_allocation_sizing VALUES (?,?,?,?,?)',
                    (strategy.digest, *scope, resolved['workspace_id'], json.dumps(resolved)))
        return envelope(strategy=library.strategy(strategy.digest), imported=True, sizing=resolved, instance_id=target_id,
                        notice=('Updated this inactive session strategy. Monitoring and trading remain off.' if target_id
                                else 'Saved a new strategy revision. Existing sessions and results retain their original rules.'))
    if path in {"/api/v1/strategies/import", "/api/v1/strategies/draft", "/api/v1/marketplace/import"}:
        from .workspace_scope import read_scope
        strategy_scope = read_scope(query or {}) or ("", "")
        if strategy_scope[0]:
            if wallet_authorizer is None:
                raise TransferError("Wallet sign-in is unavailable")
            wallet_authorizer.require(*strategy_scope)
    if path == "/api/v1/research" or path.startswith("/api/v1/research/"):
        return _research_command(library, path, body, background_service)
    if path.startswith("/api/v1/service/"):
        if background_service is None:
            raise ValueError("Background service control is unavailable in browser-owned mode")
        if path == "/api/v1/service/jobs/strategy-monitor":
            return envelope(job=background_service.configure_job(body))
        if path == "/api/v1/service/pause-entries":
            if body:
                raise ValueError("Pause entries does not accept caller authority")
            return envelope(trading=background_service.pause_entries())
        if path == "/api/v1/service/resume-entries":
            return envelope(trading=background_service.resume_entries(body))
        if path == "/api/v1/service/revoke-trading":
            if body:
                raise ValueError("Revoke trading does not accept caller authority")
            return envelope(trading=background_service.revoke_trading())
        if path in {
            "/api/v1/service/signing/enroll",
            "/api/v1/service/signing/review",
            "/api/v1/service/signing/authorize",
            "/api/v1/service/signing/start",
            "/api/v1/service/signing/acceptance/request",
            "/api/v1/service/signing/acceptance/install",
        }:
            sender, network = body.get("sender"), body.get("network")
            if wallet_authorizer is None:
                raise TransferError("Wallet sign-in is unavailable")
            wallet_authorizer.require(sender, network)
            if path.endswith("/enroll"):
                return envelope(enrollment=background_service.enroll_signer(body))
            if path.endswith("/review"):
                return envelope(review=background_service.review_signing(library, body))
            if path.endswith("/authorize"):
                return envelope(allowance=background_service.authorize_signing(library, body))
            if path.endswith("/acceptance/request"):
                return envelope(
                    acceptance=background_service.review_signing_acceptance(body)
                )
            if path.endswith("/acceptance/install"):
                return envelope(
                    trading=background_service.install_signing_acceptance(body)
                )
            return envelope(trading=background_service.start_signing(body))
        if path == "/api/v1/service/shutdown":
            if body:
                raise ValueError("Service shutdown does not accept caller authority")
            return envelope(**background_service.request_shutdown())
        raise ValueError("Unknown background service command endpoint")
    if path == "/api/v1/session-workspaces":
        mode = body.get("mode")
        allowed = {"mode", "label", "monitor_activity", "owner"} if mode == "monitoring" else {"mode", "label", "funding", "plan"}
        if set(body) - allowed or mode not in {"monitoring", "trade"}:
            raise ValueError("Choose a monitoring or trading session")
        label = body.get("label")
        if label is not None and not isinstance(label, str):
            raise ValueError("Session label must be text")
        if mode == "trade":
            funding = body.get("funding")
            if not isinstance(funding, dict):
                raise ValueError("Trading sessions require a reviewed wallet allocation")
            if wallet_authorizer is None:
                raise TransferError("Wallet sign-in is unavailable")
            wallet_authorizer.require(funding.get("sender"), funding.get("network"))
            created = create_trading_workspace(
                library, library.path.parent, label=label, funding=funding, plan=body.get("plan"),
            )
            workspace = created["workspace"]
            return envelope(
                session={
                    **workspace,
                    "strategy_count": 0,
                    "position_count": 0,
                    "strategy_ids": [],
                    "running_strategies": 0,
                    "attention_strategies": 0,
                    "available_lamports": workspace["allocation_lamports"],
                    "committed_lamports": "0",
                    "attributed_open_cost_lamports": "0",
                },
                allocation=created["allocation"],
                notice=("Trading session created with one durable shared wallet allocation. "
                        "No strategy, order, position, signing allowance, or execution was started."),
            )
        activity = body.get("monitor_activity", "observations")
        if activity not in ("observations", "paper"):
            raise ValueError("Choose observations or paper trading")
        owner = body.get("owner")
        sender = network = None
        if owner is not None:
            if not isinstance(owner, dict) or set(owner) != {"sender", "network"}:
                raise ValueError("Monitoring owner requires wallet and network")
            from .web_strategy_controls import _identity
            sender, network = _identity(owner["sender"], owner["network"])
            if wallet_authorizer is None:
                raise TransferError("Wallet sign-in is unavailable")
            wallet_authorizer.require(sender, network)
        workspace = library.create_session_workspace(
            "monitoring", label, wallet_sender=sender, wallet_network=network,
            plan={"monitor_activity": activity},
        )
        return envelope(
            session={
                **workspace,
                "strategy_count": 0,
                "position_count": 0,
                "strategy_ids": [],
                "running_strategies": 0,
                "attention_strategies": 0,
            },
            notice=("Monitoring session created. It has no wallet allocation, strategies, positions, "
                    "or active scans until you add and explicitly start a strategy."),
        )
    if path.startswith('/api/v1/session-workspaces/') and path.endswith('/funds'):
        from .web_session_controls import owned_workspace
        from .web_session_allocations import update
        ident = path.removeprefix('/api/v1/session-workspaces/').removesuffix('/funds')
        selected = owned_workspace(library, ident, query, wallet_authorizer)
        result = update(library, library.path.parent, selected, body)
        return envelope(funds=result, notice='Session funds updated. Strategies stay paused. Review signing authorization before restarting automatic trading.')
    if path.startswith('/api/v1/session-workspaces/') and path.endswith('/pause-entries'):
        from .web_session_controls import owned_workspace
        from .session_funding import pause_entries
        if body:
            raise ValueError('Entry pause takes no additional fields')
        ident = path.removeprefix('/api/v1/session-workspaces/').removesuffix('/pause-entries')
        selected = owned_workspace(library, ident, query, wallet_authorizer)
        if selected['mode'] != 'trade':
            raise ValueError('Entry pause requires a trading session')
        result = pause_entries(library, selected)
        if monitor_scheduler:
            monitor_scheduler.wake()
        return envelope(**result)
    if path.startswith('/api/v1/session-workspaces/') and path.endswith('/pause'):
        from .web_session_controls import owned_workspace, pause
        if body:
            raise ValueError('Session pause takes no additional fields')
        ident = path.removeprefix('/api/v1/session-workspaces/').removesuffix('/pause')
        selected = owned_workspace(library, ident, query, wallet_authorizer)
        result = pause(library, selected, main_wallet_controller=main_wallet_controller,
                       background_service=background_service)
        if monitor_scheduler:
            monitor_scheduler.wake()
        return envelope(**result)
    if path.startswith("/api/v1/session-workspaces/") and path.endswith("/remove"):
        workspace_id = path.removeprefix("/api/v1/session-workspaces/").removesuffix("/remove").rstrip("/")
        if set(body) != {"confirm"} or body["confirm"] != workspace_id:
            raise ValueError("Confirm the exact empty session before removing it")
        if library.session_workspace_assignment_count(workspace_id):
            raise ValueError("Remove assigned strategies before removing this session")
        released = release_empty_trading_workspace(library, library.path.parent, workspace_id)
        library.remove_empty_session_workspace(workspace_id)
        return envelope(removed=True, session_id=workspace_id,
                        released_allocation_lamports=str(released),
                        notice=("Removed the empty session and released its wallet allocation."
                                if released else
                                "Removed the empty session. No strategy evidence or wallet allocation was changed."))
    if path.startswith("/api/v1/sessions/") and path.endswith("/reviews"):
        instance_id = path.removeprefix("/api/v1/sessions/").removesuffix("/reviews").rstrip("/")
        if not instance_id:
            raise ValueError("Unknown strategy instance")
        return _run_session_review(library, instance_id, body)
    if path.startswith("/api/v1/session-reviews/") and path.endswith("/draft"):
        report_id = path.removeprefix("/api/v1/session-reviews/").removesuffix("/draft").rstrip("/")
        if not report_id:
            raise SessionReviewError("Unknown completed session review")
        return _create_session_review_draft(library, report_id, body)
    if path.startswith("/api/v1/main-wallet-autopilot/"):
        action = path.removeprefix("/api/v1/main-wallet-autopilot/").rstrip("/")
        return _main_wallet_command(
            library, action, body, wallet_authorizer, main_wallet_controller,
        )
    if path == '/api/v1/rpc':
        from .rpc_settings import command as rpc_command
        return envelope(**rpc_command(library.path.parent, body))
    if path == "/api/v1/wallet/challenge":
        if wallet_authorizer is None or request_origin is None or set(body) != {"sender", "network"}:
            raise TransferError("Wallet sign-in challenge requires sender and network")
        return envelope(**wallet_authorizer.challenge(body["sender"], body["network"], request_origin))
    if path == "/api/v1/wallet/verify":
        if wallet_authorizer is None or request_origin is None:
            raise TransferError("Wallet sign-in verification is unavailable")
        return envelope(**wallet_authorizer.verify(body, request_origin))
    if path == "/api/v1/wallet/disconnect":
        if wallet_authorizer is None or set(body) != {"sender", "network"}:
            raise TransferError("Wallet disconnect requires sender and network")
        if automation_controller is not None:
            automation_controller.stop(body["sender"], detail="Funding wallet disconnected")
        if main_wallet_controller is not None:
            current = main_wallet_controller.status(body["sender"], body["network"])
            for allowance in current["allowances"]:
                if allowance["status"] in ("authorized", "active", "entry-paused"):
                    main_wallet_controller.lock(
                        library, sender=body["sender"], network=body["network"],
                        allowance_id=allowance["id"],
                    )
        wallet_authorizer.revoke(body["sender"], body["network"])
        return envelope(disconnected=True)
    if path == "/api/v1/wallet/inspect":
        if wallet_authorizer is None:
            raise TransferError("Wallet sign-in is unavailable")
        wallet_authorizer.require(body.get("sender"), body.get("network"))
        return envelope(**inspect_wallet(body, library.path.parent))
    if path.startswith("/api/v1/coins/") and path.endswith("/history/backfill"):
        if history_service is None or body != {}:
            raise ValueError("Historical import requires an empty request and the automatic history service")
        mint = path.removeprefix("/api/v1/coins/").removesuffix("/history/backfill").rstrip("/")
        if not mint or "/" in mint:
            raise ValueError("Historical import requires one token mint")
        return envelope(job=history_service.import_page(library, mint))
    if path.startswith('/api/v1/automation/'):
        if wallet_authorizer is None:
            raise TransferError('Wallet sign-in is unavailable')
        action = path.removeprefix('/api/v1/automation/')
        # Emergency stop remains reachable from the authenticated same-origin app
        # even when the short-lived Phantom ownership proof has expired.
        if action != 'stop':
            wallet_authorizer.require(body.get('sender'), body.get('network'))
        return envelope(**web_automation.command(
            library, action, body, controller=automation_controller
        ))
    if path.startswith('/api/v1/withdrawals/'):
        if wallet_authorizer is None:
            raise TransferError('Wallet sign-in is unavailable')
        wallet_authorizer.require(body.get('sender'), body.get('network'))
        return envelope(**web_transfers.command(library.path.parent, path.removeprefix('/api/v1/withdrawals/'), body))
    if path.startswith('/api/v1/agent-orders/'):
        if wallet_authorizer is None:
            raise TransferError('Wallet sign-in is unavailable')
        wallet_authorizer.require(body.get('sender'), body.get('network'))
        return envelope(**web_agent_orders.command(library, path.removeprefix('/api/v1/agent-orders/'), body))
    if path == "/api/v1/trades/preview":
        if wallet_authorizer is None:
            raise TransferError("Wallet sign-in is unavailable")
        wallet_authorizer.require(body.get("sender"), body.get("network"))
        return envelope(**trade_preview(library.path.parent, body))
    if path == '/api/v1/trades/signing-diagnostic':
        if wallet_authorizer is None:
            raise TransferError('Wallet sign-in is unavailable')
        wallet_authorizer.require(body.get('sender'), body.get('network'))
        from .wallet_signing_diagnostics import capture
        return envelope(**capture(library.path.parent, body))
    if path == "/api/v1/trades/submit":
        if wallet_authorizer is None:
            raise TransferError("Wallet sign-in is unavailable")
        wallet_authorizer.require(body.get("sender"), body.get("network"))
        return envelope(**submit_trade(library.path.parent, body))
    if path == "/api/v1/trades/reconcile":
        if wallet_authorizer is None:
            raise TransferError("Wallet sign-in is unavailable")
        wallet_authorizer.require(body.get("sender"), body.get("network"))
        return envelope(**reconcile_trades(library.path.parent, body))
    if path.startswith("/api/v1/strategy-controls/"):
        if wallet_authorizer is None:
            raise TransferError("Wallet sign-in is unavailable")
        if not isinstance(body, dict):
            raise TransferError("Strategy controls must be a JSON object")
        wallet_authorizer.require(body.get("sender"), body.get("network"))
        agent_id = path.removeprefix("/api/v1/strategy-controls/").rstrip("/")
        if not agent_id or "/" in agent_id:
            raise TransferError("Choose one strategy monitor")
        return envelope(**save_strategy_controls(
            library, library.path.parent, agent_id, body
        ))
    if path == "/api/v1/strategies/draft":
        fields = {
            "label", "identifier", "entry_style", "entry_cap_min", "entry_cap_max", "order_usd", "budget_usd",
        }
        numeric = {"entry_cap_min", "entry_cap_max", "order_usd", "budget_usd"}
        text = {"label", "identifier", "entry_style"}
        if (set(body) not in (fields, fields | {"strategy_goal"})
                or not all(isinstance(body[field], str) for field in text)
                or ("strategy_goal" in body and not isinstance(body["strategy_goal"], str))
                or not all(isinstance(body[field], (int, float)) and not isinstance(body[field], bool) for field in numeric)):
            raise ValueError("Research draft requires all configured template fields")
        package = draft_package(
            body["label"], body["identifier"], body["entry_style"],
            float(body["entry_cap_min"]), float(body["entry_cap_max"]),
            float(body["order_usd"]), float(body["budget_usd"]),
            body.get("strategy_goal", "custom"),
        )
        strategy = parse_package(package)
        library.import_strategy(strategy, strategy_scope)
        return envelope(
            strategy=library.strategy(strategy.digest),
            imported=True,
            notice="Created and imported a local research strategy. It does not enable entries, reserve funds, authorize signing, or establish execution eligibility.",
        )
    if path.startswith("/api/v1/evaluations/") and path.endswith("/run"):
        strategy_digest = path.removeprefix("/api/v1/evaluations/").removesuffix("/run").rstrip("/")
        return run_evaluation(library, strategy_digest, body)
    if path.startswith("/api/v1/sessions/") and path.endswith("/scan"):
        if body != {}:
            raise ValueError("One-time monitoring scan does not accept parameters")
        instance_id = path.removeprefix("/api/v1/sessions/").removesuffix("/scan").rstrip("/")
        return run_monitor_scan(library, instance_id, monitor_scheduler)
    if path.startswith("/api/v1/sessions/") and path.endswith("/configuration"):
        instance_id = path.removeprefix("/api/v1/sessions/").removesuffix("/configuration").rstrip("/")
        return envelope(**_edit_session_configuration(library, instance_id, body))
    if path.startswith("/api/v1/sessions/") and path.endswith("/edit"):
        instance_id = path.removeprefix("/api/v1/sessions/").removesuffix("/edit").rstrip("/")
        if set(body) != {"label"} or not isinstance(body["label"], str):
            raise ValueError("Session edit requires one display name")
        instance = library.rename_instance(instance_id, body["label"])
        return envelope(instance=instance, notice="Session display name updated. The imported rules are unchanged.")
    if path.startswith("/api/v1/sessions/") and path.endswith("/remove"):
        instance_id = path.removeprefix("/api/v1/sessions/").removesuffix("/remove").rstrip("/")
        if (set(body) != {"confirm", "release_allocation"} or body["confirm"] != instance_id
                or type(body["release_allocation"]) is not bool):
            raise ValueError("Confirm the exact strategy session before removing it")
        instance = library.instance(instance_id)
        management = _session_management(library, instance_id)
        blocks = management["removal_blocks"]
        if blocks:
            raise ValueError(f"Cannot remove strategy: {blocks[0]}.")
        allocation = int(management["allocation_lamports"])
        if allocation and not body["release_allocation"]:
            raise ValueError("Confirm release of this strategy's SOL allocation before removing it")
        released = _release_session_allocation(library, instance_id) if allocation else 0
        library.remove_instance(instance_id)
        if instance.get("workspace_id") is not None:
            from .manual_trades import TradeJournal
            from .session_allocations import unbind_strategy

            journal = TradeJournal(library.path.parent)
            try:
                unbind_strategy(journal, instance_id)
            finally:
                journal.close()
        if monitor_scheduler:
            monitor_scheduler.wake()
        return envelope(removed=True, instance_id=instance_id, released_allocation_lamports=str(released),
                        notice="Removed from active sessions. Retained activity, brief, and receipt evidence was not deleted.")
    if path.startswith("/api/v1/portfolio-replays/") and path.endswith("/run"):
        run_id = path.removeprefix("/api/v1/portfolio-replays/").removesuffix("/run").rstrip("/")
        return run_portfolio_replay(library, run_id, body)
    if path == "/api/v1/strategies/import":
        strategy = parse_package(body)
        library.import_strategy(strategy, strategy_scope)
        return envelope(strategy=library.strategy(strategy.digest), imported=True)
    if path == "/api/v1/marketplace/import":
        if not isinstance(body, dict) or set(body) != {"id"} or not isinstance(body["id"], str):
            raise ValueError("Marketplace import requires a strategy ID")
        catalog = load_catalog(library.path.parent / "strategy-catalog.json")
        row = next((row for row in catalog["strategies"] if row["id"] == body["id"]), None)
        if row is None:
            raise ValueError("Unknown local marketplace strategy")
        strategy = parse_package(package_for(row))
        library.import_strategy(strategy, strategy_scope)
        return envelope(strategy=library.strategy(strategy.digest), imported=True,
                        notice="Imported the exact bundled/cached package. No website request was made.")
    if path == "/api/v1/market/refresh":
        if body != {}:
            raise ValueError("Public market refresh does not accept parameters")
        return refresh_market(library, minimum_interval_seconds=MARKET_REFRESH_MIN_SECONDS)
    if path == "/api/v1/sessions":
        if set(body) - {"digest", "label", "brief", "funding", "workspace_id"} or not isinstance(body.get("digest"), str):
            raise ValueError("Session creation requires a strategy digest, optional label, brief, and funding plan")
        label = body.get("label")
        if label is not None and not isinstance(label, str):
            raise ValueError("Session label must be text")
        brief = body.get("brief")
        if brief is not None and not isinstance(brief, dict):
            raise ValueError("Session brief must be an object")
        funding = body.get("funding")
        workspace_id = body.get("workspace_id")
        if workspace_id is not None and not isinstance(workspace_id, str):
            raise ValueError("Session assignment requires one session ID")
        requested_mode = brief.get("session_mode") if isinstance(brief, dict) else None
        if requested_mode == "trade" and funding is None:
            raise ValueError("Trading sessions require a reviewed wallet allocation")
        if requested_mode == "monitoring" and funding is not None:
            raise ValueError("Monitoring sessions cannot reserve wallet funds")
        if funding is not None:
            if not isinstance(funding, dict):
                raise ValueError("Session funding must be an object")
            if wallet_authorizer is None:
                raise TransferError("Wallet sign-in is unavailable")
            wallet_authorizer.require(funding.get("sender"), funding.get("network"))
        if workspace_id is not None:
            workspace = library.session_workspace(workspace_id)
            if workspace["mode"] != (requested_mode or "monitoring"):
                raise ValueError("The strategy mode must match its session")
        from .workspace_scope import read_scope, require_strategy, workspace_matches
        scope = read_scope(query or {})
        require_strategy(library, body["digest"], scope)
        if workspace_id is not None and not workspace_matches(workspace, scope):
            raise ValueError("Session does not belong to the selected wallet workspace")
        if workspace_id is not None and funding is not None:
            sized = library._connection.execute(
                'SELECT payload FROM strategy_allocation_sizing WHERE digest=? AND sender=? AND network=? AND workspace_id=?',
                (body['digest'], funding['sender'], funding['network'], workspace_id)).fetchone()
            if sized:
                from .transfers import parse_sol
                sizing = json.loads(sized[0])
                if sizing['allocation_lamports'] != workspace['allocation_lamports']:
                    raise ValueError('Allocation changed. Edit the strategy budget before adding it.')
                if parse_sol(funding['max_trade_sol']) > parse_sol(sizing['trade_budget_sol']):
                    raise ValueError('Maximum buy cannot exceed the saved percentage trade budget.')
        instance_id = library.create_instance(
            body["digest"], label, entry_mode="disabled", exit_mode="manual", brief=brief,
        )
        saved_funding = None
        try:
            if workspace_id is not None:
                library.assign_instance_to_workspace(workspace_id, instance_id)
            if funding is not None:
                saved_funding = save_strategy_controls(
                    library, library.path.parent, instance_id, funding,
                )
        except Exception:
            # A failed assignment, live-balance or policy check must not leave a
            # visible, incomplete strategy behind. The tombstone retains an audit trail.
            library.remove_instance(instance_id)
            raise
        stored_brief = library.session_brief(instance_id)
        if stored_brief["mode"] == "trade-autopilot":
            if stored_brief["permissions"].get("custody") == "main-wallet-service":
                notice = ("Created with explicit local-service custody intent and a durable main-wallet allocation. "
                          "Encrypted service enrollment, an exact allowance, fresh unlock, and Start are still required. "
                          "Production activation remains blocked pending service-custody funded acceptance.")
            else:
                notice = ("Created with a durable main-wallet allocation. Autopilot remains disabled until "
                          "an exact generated-main-wallet allowance is reviewed, explicitly authorized, and activated. "
                          "Production activation remains blocked pending funded acceptance.")
        elif stored_brief["mode"] == "trade-interactive":
            notice = ("Created with a durable main-wallet allocation. Entries and exits remain disabled "
                      "until reviewed, and every submitted order requires the user's wallet signature.")
        else:
            notice = ("Created as a monitoring session without a wallet allocation. Entries remain disabled, "
                      "and monitoring stays off until started locally.")
        return envelope(instance=library.instance(instance_id), brief=stored_brief,
                        funding=saved_funding,
                        notice=notice)
    if path.startswith("/api/v1/sessions/") and path.endswith("/tracking"):
        instance_id = path.removeprefix("/api/v1/sessions/").removesuffix("/tracking").rstrip("/")
        if set(body) != {"enabled"} or type(body["enabled"]) is not bool:
            raise ValueError("Tracking update requires an enabled boolean")
        library.set_tracking(instance_id, body["enabled"])
        if body["enabled"] and background_service is not None:
            job = background_service.store.job()
            background_service.configure_job({"enabled": True, "interval_seconds": job["interval_seconds"]})
        if monitor_scheduler:
            monitor_scheduler.wake()
        return envelope(instance=library.instance(instance_id), tracking=library.tracking_settings(instance_id),
                        notice="Monitoring state updated. This command cannot create an order, sign, or move funds.")
    raise ValueError("Unknown browser command")


def assistant_payload(assistant: BrowserAssistant, path: str, query=None) -> dict[str, object]:
    from .workspace_scope import chat_scope
    scope = chat_scope(query)
    if path == "/api/v1/assistant":
        return envelope(**assistant.overview(scope))
    if path.startswith("/api/v1/assistant/conversations/"):
        conversation = path.rsplit("/", 1)[-1]
        return envelope(**assistant.messages(conversation, scope))
    raise ValueError("Unknown assistant read endpoint")


def assistant_command(assistant: BrowserAssistant, path: str, body: object, query=None) -> dict[str, object]:
    from .workspace_scope import chat_scope
    scope = chat_scope(query)
    if path == "/api/v1/assistant/setup":
        return envelope(profile=assistant.configure(body))
    if path == "/api/v1/assistant/conversations":
        return envelope(**assistant.create(scope))
    if path.startswith("/api/v1/assistant/conversations/") and path.endswith("/delete"):
        return envelope(**assistant.delete(path.removeprefix("/api/v1/assistant/conversations/").removesuffix("/delete").rstrip("/"), scope))
    if path == "/api/v1/assistant/prepare":
        return envelope(**assistant.prepare(body, scope))
    if path == "/api/v1/assistant/evidence":
        return envelope(**assistant.evidence(body))
    if path == "/api/v1/assistant/evidence/prepare":
        return envelope(**assistant.prepare_evidence(body, scope))
    if path == "/api/v1/assistant/approve":
        if not isinstance(body, dict) or set(body) != {"request_id"} or not isinstance(body["request_id"], str):
            raise ValueError("Assistant approval requires a request ID")
        return envelope(**assistant.approve(body["request_id"], scope))
    if path == "/api/v1/assistant/decline":
        if not isinstance(body, dict) or set(body) != {"request_id"} or not isinstance(body["request_id"], str):
            raise ValueError("Assistant decline requires a request ID")
        return envelope(**assistant.decline(body["request_id"], scope))
    raise ValueError("Unknown assistant command")


def _validate_browser_build_identity(static_root: Path) -> None:
    """Refuse a packaged backend/frontend pair that was not built together."""
    from .build_identity import current_build_identity

    expected = current_build_identity()
    identity_path = static_root / "build-identity.json"
    if not identity_path.is_file():
        if expected["package_kind"] != "source":
            raise ValueError("Packaged browser build identity is missing")
        return
    try:
        observed = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Browser build identity is invalid") from exc
    fields = (
        "schema", "build_id", "version", "source_checkpoint", "packaged_at", "package_kind",
    )
    if not isinstance(observed, dict) or any(observed.get(name) != expected[name] for name in fields):
        raise ValueError(
            "Browser build identity does not match this Runner executable; refusing to serve it"
        )


def serve(data_dir: Path, browser_token: str, host: str = "127.0.0.1", port: int = 8765,
          web_root: Path | None = None,
          history_service: MarketHistoryService | None = None,
          main_wallet_controller: MainWalletAutopilotController | None = None,
          background_service=None) -> ThreadingHTTPServer:
    """Serve the local API and, optionally, a built same-origin browser client.

    Vite development continues to authenticate with its injected request header.
    When a built client is served here, its launch token is a session-only
    HttpOnly cookie scoped to `/api`; it is never placed in JavaScript, HTML,
    a URL, or a browser-readable storage API.
    """
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("Browser API may only bind to loopback")
    if len(browser_token) < 24:
        raise ValueError("Browser API token must contain at least 24 characters")
    static_root = Path(web_root).resolve() if web_root is not None else None
    if static_root is not None and not (static_root / "index.html").is_file():
        raise ValueError("Browser web root must contain a built index.html")
    if static_root is not None:
        _validate_browser_build_identity(static_root)
    assistant = BrowserAssistant(data_dir)
    history = history_service or MarketHistoryService()
    wallet_authorizer = WalletAuthorizer()
    from .automation_runtime import AutomationController

    automation_controller = AutomationController(data_dir)
    main_wallet_controller = main_wallet_controller or MainWalletAutopilotController(data_dir)

    class Handler(BaseHTTPRequestHandler):
        def _same_origins(self) -> frozenset[str]:
            address, active_port = self.server.server_address[:2]
            def origin(value: str) -> str:
                return f"http://{'[' + value + ']' if ':' in value else value}:{active_port}"
            # `localhost` may resolve to a loopback address while preserving
            # `localhost` in the browser's Origin header.
            return frozenset((origin(str(address)), origin(host)))

        def _origin_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            return origin in ALLOWED_ORIGINS or (static_root is not None and origin in self._same_origins())

        def _request_origin(self) -> str:
            origin = self.headers.get("Origin")
            if origin:
                return origin
            candidate = f"http://{self.headers.get('Host', '')}"
            if static_root is not None and candidate in self._same_origins():
                return candidate
            raise TransferError("Wallet sign-in requires an authenticated browser origin")

        def _cookie_authorized(self) -> bool:
            if static_root is None:
                return False
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get("Cookie", ""))
            except (TypeError, ValueError):
                return False
            return cookies.get(STATIC_COOKIE) is not None and cookies[STATIC_COOKIE].value == browser_token

        def _cookie_request_is_same_origin(self) -> bool:
            """Permit an Origin-less same-origin GET from the served browser.

            Fetch implementations may omit `Origin` for same-origin GETs. A
            cross-site request has an Origin header and cannot send this
            SameSite=Strict cookie; an Origin-less cookie request must carry
            Chromium's explicit same-origin fetch metadata.
            """
            return self._origin_allowed() or (
                self.headers.get("Origin") is None
                and self.headers.get("Sec-Fetch-Site") == "same-origin"
            )

        def _authorized(self) -> bool:
            if self.headers.get("X-PumpWizard-Browser-Token") == browser_token:
                return self._origin_allowed()
            return self._cookie_authorized() and self._cookie_request_is_same_origin()

        def _headers(self) -> None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if self._origin_allowed():
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-PumpWizard-Browser-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")

        def _write(self, body: bytes) -> None:
            """Ignore a browser navigation that closes an obsolete response."""
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _serve_static(self, path: str) -> None:
            """Serve only build output; unknown client routes use the SPA shell."""
            assert static_root is not None
            relative = path.lstrip("/") or "index.html"
            candidate = (static_root / relative).resolve()
            try:
                candidate.relative_to(static_root)
            except ValueError:
                self.send_error(404, "Unknown browser asset")
                return
            if not candidate.is_file():
                # The React client owns its client-side paths; never apply this
                # fallback to an asset-looking request.
                if Path(relative).suffix:
                    self.send_error(404, "Unknown browser asset")
                    return
                candidate = static_root / "index.html"
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            body = candidate.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store" if candidate.name == "index.html" else "public, max-age=31536000, immutable")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
            )
            self.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
            )
            if candidate.name == "index.html":
                self.send_header("Set-Cookie", f"{STATIC_COOKIE}={browser_token}; HttpOnly; SameSite=Strict; Path=/api")
            self.end_headers()
            self._write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/") and static_root is not None:
                self._serve_static(parsed.path)
                return
            if not self._authorized():
                self.send_response(403)
                self._headers()
                self.end_headers()
                self._write(b'{"error":"Browser authorization required"}')
                return
            if parsed.path.startswith("/api/v1/token-images/"):
                mint = parsed.path.removeprefix("/api/v1/token-images/")
                library = StrategyLibrary(data_dir)
                try:
                    content_type, body = token_image(library, mint)
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "private, max-age=3600")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                    self.end_headers()
                    self._write(body)
                except (ValueError, OSError):
                    self.send_error(404, "Token icon unavailable")
                finally:
                    library.close()
                return
            if (parsed.path.startswith("/api/v1/research/reports/")
                    and parsed.path.endswith("/export")):
                report_id = parsed.path.removeprefix(
                    "/api/v1/research/reports/"
                ).removesuffix("/export").rstrip("/")
                try:
                    format_name = parse_qs(parsed.query).get("format", ["json"])[0]
                    content_type, filename, artifact = _research_report_artifact(
                        data_dir, report_id, format_name,
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                    self.send_header("Content-Length", str(len(artifact)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self._write(artifact)
                except (ValueError, OSError) as exc:
                    body = json.dumps({"error": str(exc)}).encode()
                    self.send_response(_error_status(exc, read=True))
                    self._headers()
                    self.end_headers()
                    self._write(body)
                return
            if (parsed.path.startswith("/api/v1/research/campaign-reports/")
                    and parsed.path.endswith("/export")):
                report_id = parsed.path.removeprefix(
                    "/api/v1/research/campaign-reports/"
                ).removesuffix("/export").rstrip("/")
                try:
                    format_name = parse_qs(parsed.query).get("format", ["json"])[0]
                    content_type, filename, artifact = _campaign_report_artifact(
                        data_dir, report_id, format_name,
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                    self.send_header("Content-Length", str(len(artifact)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self._write(artifact)
                except (ValueError, OSError) as exc:
                    body = json.dumps({"error": str(exc)}).encode()
                    self.send_response(_error_status(exc, read=True))
                    self._headers()
                    self.end_headers()
                    self._write(body)
                return
            try:
                if parsed.path.startswith("/api/v1/assistant"):
                    body = json.dumps(assistant_payload(assistant, parsed.path, parse_qs(parsed.query)), allow_nan=False).encode()
                else:
                    library = StrategyLibrary(data_dir)
                    try:
                        body = json.dumps(payload(
                            library, parsed.path, parse_qs(parsed.query), wallet_authorizer, history,
                            automation_controller, main_wallet_controller, background_service,
                        ), allow_nan=False).encode()
                    finally:
                        library.close()
                self.send_response(200)
            except (ValueError, OSError, TransferError, AgentHubError) as exc:
                body = json.dumps({
                    "error": str(exc), "typed_error": _typed_error(exc, parsed.path),
                }).encode()
                self.send_response(_error_status(exc, read=True))
            self._headers()
            self.end_headers()
            self._write(body)

        def do_OPTIONS(self):
            if not self._origin_allowed():
                self.send_error(403, "Untrusted browser origin")
                return
            self.send_response(204)
            self._headers()
            self.end_headers()

        def do_POST(self):
            if not self._authorized():
                self.send_error(403, "Browser authorization required")
                return
            parsed = urlparse(self.path)
            should_shutdown = False
            try:
                length = int(self.headers.get("Content-Length", "0"))
                maximum = EVALUATION_MAX_BYTES if any((
                    parsed.path.startswith("/api/v1/evaluations/") and parsed.path.endswith("/run"),
                    parsed.path.startswith("/api/v1/portfolio-replays/") and parsed.path.endswith("/run"),
                )) else MAX_COMMAND_BYTES
                if not 1 <= length <= maximum:
                    raise ValueError(
                        f"Browser command body must be 1 byte to {maximum // (1024 * 1024)} MiB"
                    )
                body = json.loads(self.rfile.read(length))
                if parsed.path.startswith("/api/v1/assistant"):
                    response = json.dumps(assistant_command(assistant, parsed.path, body, parse_qs(parsed.query)), allow_nan=False).encode()
                else:
                    library = StrategyLibrary(data_dir)
                    try:
                        response = json.dumps(command(
                            library, parsed.path, body, wallet_authorizer, self._request_origin(), history,
                            getattr(self.server, "monitor", None), automation_controller,
                            main_wallet_controller, background_service, query=parse_qs(parsed.query),
                        ), allow_nan=False).encode()
                        should_shutdown = parsed.path == "/api/v1/service/shutdown"
                    finally:
                        library.close()
                self.send_response(200)
            except (ValueError, OSError, TransferError, AgentHubError, json.JSONDecodeError) as exc:
                response = json.dumps({
                    "error": str(exc), "typed_error": _typed_error(exc, parsed.path),
                }).encode()
                self.send_response(_error_status(exc))
            self._headers()
            self.end_headers()
            self._write(response)
            if should_shutdown:
                threading.Thread(target=self.server.shutdown,
                                 name="pumpwizard-service-shutdown", daemon=True).start()

        def log_message(self, *_args):
            pass

    class BrowserServer(ThreadingHTTPServer):
        def serve_forever(self, poll_interval=0.5):
            if background_service is None:
                self.monitor = MonitorScheduler(
                    data_dir, source_factory=lambda: CompositePumpMarketSource()
                )
                self.monitor.start()
            else:
                self.monitor = background_service.scheduler
                background_service.start()
                self.monitor = background_service.scheduler
            try:
                super().serve_forever(poll_interval)
            finally:
                if background_service is None:
                    self.monitor.stop()
                    self.monitor.thread.join(timeout=2)
                else:
                    background_service.stop()
                main_wallet_controller.shutdown()
                automation_controller.shutdown()

    return BrowserServer((host, port), Handler)
