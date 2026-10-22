"""Command-line entry points shared by the packaged local application."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import time
import webbrowser

from . import __version__
from .desktop import run_desktop
from .library import open_library
from .localnet import LocalnetError, signing_check
from .market import CompositePumpMarketSource, MarketDataError
from .orders import OrderIntentError, cancel_order, prepare_order, simulate_order
from .ownership import SolanaOwnershipSource
from .paths import default_data_dir
from .runtime import LocalMonitor, MonitorCycle, MonitorResult
from .strategies import StrategyImportError, parse_package
from .wallet import SolanaWalletSource, WalletDataError


def _data_dir(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else default_data_dir()


def _browser_root(value: Path | None = None) -> Path:
    """Find built browser assets in a source checkout or native bundle."""
    if value is not None:
        return value.expanduser().resolve()
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "web-client")
        candidates.append(Path(sys.executable).resolve().parent.parent / "Resources" / "web-client")
    candidates.append(Path(__file__).resolve().parents[2] / "web-client" / "dist")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    raise ValueError(
        "Built browser assets are missing. Run `cd web-client && npm run build`, "
        "or use the packaged application."
    )


def _add_web_arguments(parser: argparse.ArgumentParser, *, generated_token: bool) -> None:
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    if not generated_token:
        parser.add_argument("--browser-token", default=os.environ.get("PUMPWIZARD_BROWSER_TOKEN"),
                            help="Per-launch browser token (or set PUMPWIZARD_BROWSER_TOKEN)")
    root_help = ("Built browser directory; defaults to the source or packaged client"
                 if generated_token else "Optional built browser directory for same-origin serving")
    parser.add_argument("--web-root", type=Path, help=root_help)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="solanamarket-wizard", description=__doc__)
    parser.add_argument("--data-dir", help="Local application-data directory")
    parser.add_argument("--version", action="version", version=f"PumpWizard Runner {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("desktop", help="Open the legacy native desktop client")
    browser = commands.add_parser("browser", help="Open the local browser application")
    _add_web_arguments(browser, generated_token=True)
    browser.add_argument("--no-open", action="store_true",
                         help="Serve the app without opening the system browser")
    web = commands.add_parser("web-api", help="Serve the loopback-only browser API")
    _add_web_arguments(web, generated_token=False)
    service = commands.add_parser("service", help="Manage the local background service")
    service_commands = service.add_subparsers(dest="service_command", required=True)
    for name, help_text in (
        ("start", "Start or reuse the verified background service"),
        ("launch-dashboard", "Start or reuse the service, then open its Dashboard"),
        ("status", "Read authenticated background service status"),
        ("stop", "Stop and lock the verified background service"),
        ("open-dashboard", "Open the verified service dashboard"),
    ):
        child = service_commands.add_parser(name, help=help_text)
        child.add_argument("--port", type=int, default=8781)
        if name in {"start", "launch-dashboard"}:
            child.add_argument("--web-root", type=Path)
    service_run = service_commands.add_parser("run", help="Run the service in the foreground")
    service_run.add_argument("--port", type=int, default=8781)
    service_run.add_argument("--web-root", type=Path)
    service_run.add_argument("--expected-version")
    service_job = service_commands.add_parser(
        "configure-monitor", help="Persist explicit read-only monitor job settings"
    )
    service_job.add_argument("--port", type=int, default=8781)
    service_job.add_argument("--enabled", action=argparse.BooleanOptionalAction, required=True)
    service_job.add_argument("--interval", type=float, default=15)
    service_install = service_commands.add_parser(
        "install-user", help="Stage a version-pinned Linux user-service unit"
    )
    service_install.add_argument("--executable", type=Path, required=True)
    service_install.add_argument("--unit-dir", type=Path)
    service_install.add_argument("--port", type=int, default=8781)
    service_remove = service_commands.add_parser(
        "remove-user", help="Remove only a PumpWizard-managed user-service unit"
    )
    service_remove.add_argument("--unit-dir", type=Path)
    service_backup = service_commands.add_parser(
        "backup", help="Create a new offline integrity-checked service backup"
    )
    service_backup.add_argument("--destination", type=Path, required=True)
    service_restore = service_commands.add_parser(
        "restore", help="Restore a verified service backup into a new data directory"
    )
    service_restore.add_argument("--backup", type=Path, required=True)
    importer = commands.add_parser("import", help="Import a local strategy JSON file")
    importer.add_argument("file", type=Path)
    commands.add_parser("strategies", help="List imported strategies")
    commands.add_parser("status", help="Show local library status")
    worker = commands.add_parser('rehearse-agent', help='Run the unsigned execution worker; never sign or create orders')
    worker.add_argument('instance_id')
    worker.add_argument('sender')
    worker.add_argument('--network', choices=('mainnet-beta','devnet'), default='mainnet-beta')
    worker.add_argument('--cash-reservation', help='Pin one cash allocation; otherwise assess with the largest available agent cash hold')
    worker.add_argument('--slippage-bps', type=int, default=100)
    worker.add_argument('--limit', type=int, default=3)
    worker.add_argument('--interval', type=int, default=15)
    worker.add_argument('--watch', action='store_true', help='Continue until interrupted; default is one cycle')
    worker.add_argument('--rpc-env')
    worker_status = commands.add_parser('worker-status', help='Read recent unsigned worker health and cycle history')
    worker_status.add_argument('instance_id')
    worker_status.add_argument('sender')
    worker_status.add_argument('--network', choices=('mainnet-beta','devnet'), default='mainnet-beta')
    shadow = commands.add_parser('shadow-agent',help='Assess agent actions with unsigned simulations; never sign or reserve funds')
    shadow.add_argument('instance_id')
    shadow.add_argument('sender')
    shadow.add_argument('--cash-reservation',required=True)
    shadow.add_argument('--mint',help='One candidate mint; otherwise held positions then current entry recommendations')
    shadow.add_argument('--position-id')
    shadow.add_argument('--slippage-bps',type=int,default=100)
    shadow.add_argument('--limit',type=int,default=10)
    shadow.add_argument('--network',choices=('mainnet-beta','devnet'),default='mainnet-beta')
    shadow.add_argument('--rpc-env')
    shadow_reports = commands.add_parser('shadow-reports',help='Show recorded unsigned agent assessments')
    shadow_reports.add_argument('instance_id')
    agent_prepare = commands.add_parser('prepare-agent-order',help='Build an unsigned reviewed order from a current agent action')
    agent_prepare.add_argument('instance_id')
    agent_prepare.add_argument('sender')
    agent_prepare.add_argument('mint')
    agent_prepare.add_argument('--cash-reservation',required=True)
    agent_prepare.add_argument('--position-id',help='Exit an agent-owned finalized position')
    agent_prepare.add_argument('--slippage-bps',type=int,default=100)
    agent_prepare.add_argument('--network',choices=('mainnet-beta','devnet'),default='mainnet-beta')
    agent_prepare.add_argument('--rpc-env')
    agent_review = commands.add_parser('review-agent-order',help='Interactively review and authorize exactly one agent order')
    agent_review.add_argument('order_id')
    agent_review.add_argument('--rpc-env')
    agent_cancel = commands.add_parser('cancel-agent-order',help='Cancel an unsigned agent order without releasing its funds')
    agent_cancel.add_argument('order_id')
    agent_orders = commands.add_parser('agent-orders',help='List agent order and transaction states')
    agent_orders.add_argument('instance_id')
    agent_positions = commands.add_parser('agent-positions',help='List finalized agent-owned positions')
    agent_positions.add_argument('instance_id')
    agent_reconcile = commands.add_parser('reconcile-agent-orders',help='Refresh original signatures and finalized receipts without signing')
    agent_reconcile.add_argument('sender')
    agent_reconcile.add_argument('--network',choices=('mainnet-beta','devnet'),default='mainnet-beta')
    agent_reconcile.add_argument('--rpc-env')
    replay = commands.add_parser("portfolio-replay", help="Replay core-frequency virtual portfolio inputs; no signing")
    replay.add_argument("file", type=Path, help="Local pumpwizard.portfolio-recording/v1 JSON")
    replay.add_argument("--run-id", required=True, help="Durable replay ID; reuse to resume the same recording")
    portfolio_status = commands.add_parser("portfolio-status", help="Show an offline virtual portfolio checkpoint")
    portfolio_status.add_argument("run_id")
    research_freeze = commands.add_parser(
        "research-freeze", help="Freeze one immutable session/recorder research dataset"
    )
    research_freeze.add_argument("instance_id")
    research_freeze.add_argument(
        "--network", choices=("mainnet-beta", "devnet", "localnet", "unknown"),
        default="unknown",
    )
    research_datasets = commands.add_parser(
        "research-datasets", help="List immutable local research datasets"
    )
    research_datasets.add_argument("--instance-id")
    research_datasets.add_argument("--limit", type=int, default=100)
    research_export = commands.add_parser(
        "research-export", help="Print one verified deterministic research export"
    )
    research_export.add_argument("dataset_id")
    research_register = commands.add_parser(
        "research-register", help="Register an exact experiment JSON before execution"
    )
    research_register.add_argument("file", type=Path)
    research_enqueue = commands.add_parser(
        "research-enqueue", help="Queue one registered background comparison"
    )
    research_enqueue.add_argument("experiment_id")
    research_enqueue.add_argument("--idempotency-key", required=True)
    research_jobs = commands.add_parser(
        "research-jobs", help="List durable research jobs and attempt state"
    )
    research_jobs.add_argument("--limit", type=int, default=100)
    commands.add_parser(
        "research-integrity",
        help="Show the read-only holdout exposure and historical migration summary",
    )
    paper_start = commands.add_parser(
        "paper-start", help="Explicitly start prospective paper research; never trade"
    )
    paper_start.add_argument("instance_id")
    paper_start.add_argument("--capital-usd", type=float, required=True)
    paper_start.add_argument("--idempotency-key", required=True)
    paper_start.add_argument("--start-at", type=float)
    paper_start.add_argument("--confirm", required=True)
    paper_runs = commands.add_parser("paper-runs", help="List prospective paper research runs")
    paper_runs.add_argument("--limit", type=int, default=100)
    campaign_start = commands.add_parser(
        "campaign-start", help="Start one bounded forward-observation campaign; never trade"
    )
    campaign_start.add_argument("file", type=Path,
                                help="pumpwizard.forward-campaign-request/v1 JSON")
    campaign_start.add_argument("--idempotency-key", required=True)
    campaign_start.add_argument("--confirm", required=True)
    campaigns = commands.add_parser("campaigns", help="List forward campaigns")
    campaigns.add_argument("--limit", type=int, default=100)
    campaign_status = commands.add_parser("campaign-status", help="Inspect one campaign")
    campaign_status.add_argument("campaign_id")
    campaign_command = commands.add_parser(
        "campaign-command", help="Pause, resume, or stop one forward campaign"
    )
    campaign_command.add_argument("campaign_id")
    campaign_command.add_argument("action", choices=("pause", "resume", "stop"))
    campaign_command.add_argument("--idempotency-key", required=True)
    campaign_command.add_argument("--confirm")
    campaign_provider = commands.add_parser(
        "campaign-provider", help="Inspect credential-free provider history"
    )
    campaign_provider.add_argument("campaign_id")
    campaign_provider.add_argument("--limit", type=int, default=100)
    campaign_report = commands.add_parser(
        "campaign-report", help="Save one append-only campaign performance snapshot"
    )
    campaign_report.add_argument("campaign_id")
    campaign_report.add_argument("--previous-report-id")
    campaign_report.add_argument("--at", type=float)
    campaign_alerts = commands.add_parser(
        "campaign-alerts", help="List local campaign evidence alerts"
    )
    campaign_alerts.add_argument("campaign_id")
    campaign_alerts.add_argument("--state", choices=("open", "resolved"))
    campaign_alerts.add_argument("--limit", type=int, default=100)
    alert_ack = commands.add_parser(
        "campaign-alert-ack", help="Acknowledge local review without resolving the condition"
    )
    alert_ack.add_argument("alert_id")
    observation_prepare = commands.add_parser(
        "observation-prepare-24h",
        help="Freeze the reviewed 24-hour observation preset without network access",
    )
    observation_prepare.add_argument("--idempotency-key", required=True)
    observation_presets = commands.add_parser(
        "observation-presets", help="List immutable observation presets"
    )
    observation_presets.add_argument("--limit", type=int, default=100)
    observation_start = commands.add_parser(
        "observation-start", help="Explicitly start one preregistered paper observation"
    )
    observation_start.add_argument("preset_id")
    observation_start.add_argument("--idempotency-key", required=True)
    observation_start.add_argument("--confirm", required=True)
    observation_summary = commands.add_parser(
        "observation-summary", help="Show bounded operational accounting for one campaign"
    )
    observation_summary.add_argument("campaign_id")
    observation_export = commands.add_parser(
        "observation-export", help="Export digest-bound operational accounting"
    )
    observation_export.add_argument("campaign_id")
    evaluation = commands.add_parser("evaluate-recording", help="Save an offline replay report bound to an imported agent")
    evaluation.add_argument("instance_id")
    evaluation.add_argument("file", type=Path)
    route_check = commands.add_parser("route-check", help="Read public route accounts; optionally simulate unsigned swaps")
    route_check.add_argument("mints", nargs="+")
    route_check.add_argument("--network", choices=("mainnet-beta", "devnet"), default="mainnet-beta")
    route_check.add_argument("--rpc-env", help="Environment variable containing an HTTPS RPC URL")
    route_check.add_argument("--sender", help="Public address only, for unsigned simulation")
    route_check.add_argument("--side", choices=("buy", "sell"), default="buy")
    route_check.add_argument("--amount", help="SOL for buy, tokens for sell; requires --sender")
    route_check.add_argument("--round-trip", action="store_true", help="Unsigned curve buy + partial sell in one diagnostic transaction")
    instance = commands.add_parser("create-instance", help="Create an independent local monitor")
    instance.add_argument("strategy_digest", help="Full SHA-256 digest displayed by strategies")
    instance.add_argument("--label")
    instance.add_argument("--entry-mode", choices=("disabled", "manual", "automatic"), default="disabled")
    instance.add_argument("--exit-mode", choices=("manual", "automatic"), default="manual")
    commands.add_parser("instances", help="List local strategy instances")
    monitor = commands.add_parser("monitor", help="Collect independent market data and evaluate one instance")
    monitor.add_argument("instance_id")
    monitor_mode = monitor.add_mutually_exclusive_group(required=True)
    monitor_mode.add_argument("--once", action="store_true", help="Run one collection cycle")
    monitor_mode.add_argument("--interval", type=float, help="Continuously collect every 5–3600 seconds")
    monitor.add_argument("--ownership-rpc-env", metavar="NAME",
                         help="Environment variable containing the user-selected HTTPS Solana RPC URL")
    monitor_runs = commands.add_parser("monitor-runs", help="Show local monitor health history")
    monitor_runs.add_argument("instance_id")
    monitor_runs.add_argument("--limit", type=int, default=30)
    pause = commands.add_parser("pause", help="Pause a local strategy monitor")
    pause.add_argument("instance_id")
    resume = commands.add_parser("resume", help="Resume a paused local strategy monitor")
    resume.add_argument("instance_id")
    ownership_check = commands.add_parser("ownership-check", help="Collect local on-chain ownership evidence")
    ownership_check.add_argument("instance_id")
    ownership_check.add_argument("mint")
    ownership_check.add_argument("--rpc-env", required=True,
                                 help="Environment variable containing the HTTPS Solana RPC URL")
    ownership_history = commands.add_parser("ownership-history", help="Show stored local ownership evidence")
    ownership_history.add_argument("instance_id")
    ownership_history.add_argument("--mint")
    ownership_history.add_argument("--limit", type=int, default=30)
    wallet_check = commands.add_parser("wallet-check", help="Record a read-only public-address balance snapshot")
    wallet_check.add_argument("instance_id")
    wallet_check.add_argument("address", help="Public Solana address; no private key is accepted")
    wallet_check.add_argument("--rpc-env", required=True,
                              help="Environment variable containing the HTTPS Solana RPC URL")
    wallet_history = commands.add_parser("wallet-history", help="Show local public-address balance snapshots")
    wallet_history.add_argument("instance_id")
    wallet_history.add_argument("--address")
    wallet_history.add_argument("--limit", type=int, default=30)
    executor_config = commands.add_parser("executor-config", help="Set bounded local dry-run order limits")
    executor_config.add_argument("instance_id")
    executor_config.add_argument("--mode", required=True, choices=("disabled", "dry-run"))
    executor_config.add_argument("--max-order-usd", required=True, type=float)
    executor_config.add_argument("--max-slippage-bps", type=int, default=500)
    executor_config.add_argument("--decision-ttl-seconds", type=float, default=45)
    prepare = commands.add_parser("prepare-order", help="Create a bounded dry-run order from the newest decision")
    prepare.add_argument("instance_id")
    prepare.add_argument("mint")
    prepare.add_argument("--spend-usd", type=float, help="Required for entry decisions; capped locally")
    simulate = commands.add_parser("simulate-order", help="Reprice a draft order with local observations only")
    simulate.add_argument("order_id")
    cancel = commands.add_parser("cancel-order", help="Cancel a local draft or dry-run order")
    cancel.add_argument("order_id")
    orders = commands.add_parser("orders", help="Show local dry-run order history")
    orders.add_argument("instance_id")
    orders.add_argument("--limit", type=int, default=30)
    order_events = commands.add_parser("order-events", help="Show local lifecycle events for an order")
    order_events.add_argument("order_id")
    order_events.add_argument("--limit", type=int, default=30)
    reserve_agent = commands.add_parser("reserve-agent", help="Hold wallet resources for one agent; does not sign")
    reserve_agent.add_argument("instance_id")
    reserve_agent.add_argument("sender", help="Public wallet address")
    reserve_agent.add_argument("--network", choices=("mainnet-beta", "devnet"), default="mainnet-beta")
    reserve_agent.add_argument("--rpc-env", help="Environment variable containing an HTTPS RPC URL")
    reserve_agent.add_argument("--sol", help="Exact SOL to hold, including the agent's fee/rent allowance")
    reserve_agent.add_argument("--position-id", help="Confirmed manual-trade position ID")
    reserve_agent.add_argument("--tokens", help="Exact quantity from that confirmed position")
    reservations = commands.add_parser("agent-reservations", help="List durable wallet resource holds")
    reservations.add_argument("--sender")
    release_agent = commands.add_parser("release-agent-reservation", help="Explicitly release an agent resource hold")
    release_agent.add_argument("reservation_id")
    localnet_check = commands.add_parser("localnet-signing-check", help="Submit one explicit loopback-only signer test")
    localnet_check.add_argument("instance_id")
    localnet_check.add_argument("--keypair", required=True, type=Path,
                                help="0600 local-validator test keypair JSON; never stored by the app")
    localnet_check.add_argument("--recipient", required=True, help="Different local-validator public address")
    localnet_check.add_argument("--lamports", required=True, type=int, help="1–100000000 local test lamports")
    localnet_check.add_argument("--rpc-url", default="http://127.0.0.1:8899",
                                help="Must be loopback HTTP on port 8899")
    localnet_history = commands.add_parser("localnet-checks", help="Show confirmed loopback signer tests")
    localnet_history.add_argument("instance_id")
    localnet_history.add_argument("--limit", type=int, default=30)
    decisions = commands.add_parser("decisions", help="Show stored local decisions")
    decisions.add_argument("instance_id")
    decisions.add_argument("--limit", type=int, default=30)
    positions = commands.add_parser("positions", help="Show the local position library")
    positions.add_argument("instance_id")
    positions.add_argument("--open-only", action="store_true")
    entry = commands.add_parser("record-position", help="Record a user-confirmed external entry")
    entry.add_argument("instance_id")
    entry.add_argument("mint")
    entry.add_argument("symbol")
    entry.add_argument("--quantity", required=True, type=float)
    entry.add_argument("--cost-usd", required=True, type=float)
    entry.add_argument("--opened-at", type=float, default=None, help="Unix timestamp; default is now")
    entry.add_argument("--management-mode", choices=("manual", "automatic"), default="manual")
    exit_position = commands.add_parser("record-exit", help="Record a user-confirmed external exit")
    exit_position.add_argument("position_id")
    exit_position.add_argument("--quantity", required=True, type=float)
    exit_position.add_argument("--proceeds-usd", required=True, type=float)
    exit_position.add_argument("--exited-at", type=float, default=None, help="Unix timestamp; default is now")
    exit_position.add_argument("--stage", action="append", type=int, default=[])
    position_events = commands.add_parser("position-events", help="Show local position-entry and exit records")
    position_events.add_argument("instance_id")
    position_events.add_argument("--limit", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "browser"
    if command == "route-check":
        from .route_check import RouteCheckRpc, check_routes
        from .transfers import TransferError

        try:
            url = os.environ.get(args.rpc_env) if args.rpc_env else None
            if args.rpc_env and not url:
                raise TransferError("The selected RPC environment variable is empty")
            if not args.rpc_env and args.network in ('mainnet-beta', 'devnet'):
                from .rpc_settings import endpoint
                url = endpoint(args.network, _data_dir(args.data_dir))[0]
            report = check_routes(RouteCheckRpc(args.network, url), args.mints,
                                  sender=args.sender, side=args.side, amount=args.amount,
                                  round_trip=args.round_trip)
        except TransferError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2))
        return 0 if report["status"] == "passed" else 1
    data_dir = _data_dir(args.data_dir)
    if command == "service":
        return _service_command(args, data_dir)
    if command == "desktop":
        return run_desktop(data_dir)
    if command == "browser":
        from .web_api import serve

        server = None
        try:
            web_root = _browser_root(getattr(args, "web_root", None))
            host = getattr(args, "host", "127.0.0.1")
            port = getattr(args, "port", 8765)
            server = serve(data_dir, secrets.token_urlsafe(32), host, port, web_root)
            location = f"http://{host}:{server.server_address[1]}/"
            print(f"PumpWizard browser app: {location}", flush=True)
            if not getattr(args, "no_open", False) and not webbrowser.open_new_tab(location):
                print(f"Open {location} in your browser.", file=sys.stderr)
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        finally:
            if server is not None:
                server.server_close()
        return 0
    if command == "web-api":
        from .web_api import serve
        try:
            if not args.browser_token:
                raise ValueError("Set PUMPWIZARD_BROWSER_TOKEN or pass --browser-token before starting the browser API")
            server = serve(data_dir, args.browser_token, args.host, args.port, args.web_root)
            location = f"http://{args.host}:{args.port}/" if args.web_root else f"http://{args.host}:{args.port}/api/v1/workspace"
            print(f"PumpWizard browser API: {location}")
            server.serve_forever()
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    if command in {
        "campaign-start", "campaigns", "campaign-status", "campaign-command",
        "campaign-provider", "campaign-report", "campaign-alerts",
        "campaign-alert-ack", "observation-prepare-24h", "observation-presets",
        "observation-start", "observation-summary", "observation-export",
    }:
        from .campaign_performance import build_campaign_report
        from .evidence_alerts import EvidenceAlertStore
        from .forward_campaigns import ForwardCampaignStore
        from .observation_operations import (
            build_24_hour_request,
            build_24_hour_resource_model,
            build_operational_export,
            build_operational_summary,
        )
        from .service_recorder import ServiceEvidenceRecorder

        campaign_store = recorder = alerts = None
        try:
            campaign_store = ForwardCampaignStore(data_dir)
            recorder = ServiceEvidenceRecorder(data_dir)
            alerts = EvidenceAlertStore(data_dir)
            if command == "observation-prepare-24h":
                result = campaign_store.preregister(
                    build_24_hour_request(), build_24_hour_resource_model(),
                    idempotency_key=args.idempotency_key,
                )
            elif command == "observation-presets":
                result = campaign_store.presets(limit=args.limit)
            elif command == "observation-start":
                result = campaign_store.start_preregistered(
                    args.preset_id, idempotency_key=args.idempotency_key,
                    confirmation=args.confirm,
                )
                result = campaign_store.campaign(result["id"], recorder)
            elif command == "observation-summary":
                result = build_operational_summary(
                    campaign_store, recorder, args.campaign_id,
                )
            elif command == "observation-export":
                result = build_operational_export(build_operational_summary(
                    campaign_store, recorder, args.campaign_id,
                ))
            elif command == "campaign-start":
                manifest = json.loads(args.file.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    raise ValueError("Campaign manifest must be a JSON object")
                source = manifest.get("source")
                if (not isinstance(source, dict)
                        or source.get("provider") != "CompositePumpMarketSource"):
                    raise ValueError(
                        "CLI campaigns require provider CompositePumpMarketSource"
                    )
                raw_arms = manifest.get("arms")
                if not isinstance(raw_arms, list):
                    raise ValueError("Campaign manifest arms must be a list")
                resolved_arms = []
                with open_library(data_dir) as library:
                    for raw in raw_arms:
                        if not isinstance(raw, dict):
                            raise ValueError("Campaign arms must be JSON objects")
                        if "strategy_package" in raw:
                            resolved_arms.append(raw)
                            continue
                        instance = library.instance(str(raw.get("source_session_id")))
                        if instance["strategy_digest"] != raw.get("strategy_sha256"):
                            raise ValueError("Campaign source strategy identity changed")
                        resolved_arms.append({**raw, "strategy_package": instance["package"]})
                result = campaign_store.start(
                    {**manifest, "arms": resolved_arms},
                    idempotency_key=args.idempotency_key, confirmation=args.confirm,
                )
                result = campaign_store.campaign(result["id"], recorder)
            elif command == "campaigns":
                result = campaign_store.campaigns(limit=args.limit, recorder=recorder)
            elif command == "campaign-status":
                result = campaign_store.campaign(args.campaign_id, recorder)
            elif command == "campaign-command":
                result = campaign_store.command(
                    args.campaign_id, action=args.action, confirmation=args.confirm,
                    idempotency_key=args.idempotency_key,
                )
                result = campaign_store.campaign(result["id"], recorder)
            elif command == "campaign-provider":
                campaign_store.campaign(args.campaign_id)
                result = recorder.provider_history(
                    campaign_id=args.campaign_id, limit=args.limit,
                )
            elif command == "campaign-report":
                result = build_campaign_report(
                    campaign_store, recorder, args.campaign_id,
                    previous_report_id=args.previous_report_id,
                    created_at=args.at,
                )
            elif command == "campaign-alerts":
                campaign_store.campaign(args.campaign_id)
                result = alerts.inbox(
                    args.campaign_id, state=args.state, limit=args.limit,
                )
            else:
                result = alerts.acknowledge(args.alert_id)
            print(json.dumps(result, indent=2, allow_nan=False))
            return 0
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            print(f"Campaign: {exc}", file=sys.stderr)
            return 2
        finally:
            if alerts is not None:
                alerts.close()
            if recorder is not None:
                recorder.close()
            if campaign_store is not None:
                campaign_store.close()
    if command in {
        "research-freeze", "research-datasets", "research-export",
        "research-register", "research-enqueue", "research-jobs", "research-integrity",
        "paper-start", "paper-runs",
    }:
        from .paper_research import PaperResearchStore
        from .research_data import ResearchDataStore, freeze_service_dataset
        from .research_jobs import ResearchRegistry
        from .service_recorder import ServiceEvidenceRecorder

        data = registry = paper = recorder = None
        try:
            data = ResearchDataStore(data_dir)
            registry = ResearchRegistry(data_dir)
            paper = PaperResearchStore(data_dir)
            if command == "research-freeze":
                recorder = ServiceEvidenceRecorder(data_dir)
                with open_library(data_dir) as library:
                    result = freeze_service_dataset(
                        library, recorder, data, source_session_id=args.instance_id,
                        network=args.network,
                    )["dataset"]
            elif command == "research-datasets":
                result = data.list(source_session_id=args.instance_id, limit=args.limit)
            elif command == "research-export":
                result = data.export(args.dataset_id)
            elif command == "research-register":
                request = json.loads(args.file.read_text(encoding="utf-8"))
                result = registry.register(request)
            elif command == "research-enqueue":
                result = registry.enqueue(
                    args.experiment_id, idempotency_key=args.idempotency_key,
                )
            elif command == "research-jobs":
                result = registry.jobs(limit=args.limit)
            elif command == "research-integrity":
                result = registry.integrity_summary()
            elif command == "paper-start":
                with open_library(data_dir) as library:
                    instance = library.instance(args.instance_id)
                result = paper.start(
                    source_session_id=args.instance_id,
                    strategy_package=instance["package"],
                    strategy_sha256=instance["strategy_digest"],
                    simulated_capital_usd=args.capital_usd,
                    start_at=args.start_at,
                    idempotency_key=args.idempotency_key,
                    confirmation=args.confirm,
                )
            else:
                result = paper.runs(limit=args.limit)
            print(json.dumps(result, indent=2, allow_nan=False))
            return 0
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            print(f"Research: {exc}", file=sys.stderr)
            return 2
        finally:
            if recorder is not None:
                recorder.close()
            if paper is not None:
                paper.close()
            if registry is not None:
                registry.close()
            if data is not None:
                data.close()
    if command in ('rehearse-agent', 'worker-status'):
        from .execution_worker import ExecutionWorker, WorkerConfig, history
        from .manual_trades import TradeRpc
        from .transfers import TransferError

        worker = None
        try:
            if command == 'worker-status':
                result = history(data_dir, WorkerConfig(args.instance_id, args.sender, args.network))
            else:
                url = os.environ.get(args.rpc_env) if args.rpc_env else None
                if args.rpc_env and not url:
                    raise TransferError('The selected RPC environment variable is empty')
                config = WorkerConfig(args.instance_id, args.sender, args.network, cash_hold=args.cash_reservation,
                                      interval=args.interval, slippage_bps=args.slippage_bps, limit=args.limit)
                worker = ExecutionWorker(data_dir, config, lambda: TradeRpc(args.network, url))
                if args.watch:
                    worker.start()
                    try:
                        while worker.running:
                            worker.thread.join(.5)
                    except KeyboardInterrupt:
                        worker.stop()
                        worker.thread.join()
                    if worker.error:
                        raise TransferError(worker.error)
                    result = history(data_dir, config)
                else:
                    result = worker.run_once()
            print(json.dumps(result, indent=2))
            return 1 if result.get('status') == 'error' else 0
        except (TransferError, ValueError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        finally:
            if worker:
                worker.stop()
    if command in ('shadow-agent','shadow-reports'):
        from .execution_risk import RiskStore
        from .route_check import RouteCheckRpc
        from .shadow import run_shadow
        from .transfers import TransferError

        try:
            if command=='shadow-reports':
                store=RiskStore(data_dir)
                try:
                    result=store.reports(args.instance_id)
                finally:
                    store.close()
            else:
                url=os.environ.get(args.rpc_env) if args.rpc_env else None
                if args.rpc_env and not url:
                    raise TransferError('The selected RPC environment variable is empty')
                result=run_shadow(data_dir,RouteCheckRpc(args.network,url),args.instance_id,args.sender,args.cash_reservation,
                                  mint=args.mint,position_id=args.position_id,slippage_bps=args.slippage_bps,limit=args.limit)
            print(json.dumps(result,indent=2))
            return 0
        except (TransferError,ValueError) as exc:
            print(str(exc),file=sys.stderr)
            return 2
    if command in ('prepare-agent-order','review-agent-order','cancel-agent-order','agent-orders','agent-positions','reconcile-agent-orders'):
        from .agent_execution import AgentTradeService, cancel
        from .manual_trades import ManualTradeService, TradeJournal, TradeRpc
        from .transfers import TransferError, sol_text

        journal = TradeJournal(data_dir)
        try:
            if command == 'cancel-agent-order':
                cancel(journal,args.order_id)
                result = {'id':args.order_id,'status':'cancelled','funds':'still reserved for agent'}
            elif command == 'agent-orders':
                result = [dict(r) for r in journal.db.execute('''SELECT a.*,t.signature,t.status AS transaction_status
                    FROM agent_orders a LEFT JOIN trades t ON t.id=a.id WHERE a.agent_id=? ORDER BY a.created_at''',(args.instance_id,))]
            elif command == 'agent-positions':
                result = [dict(r) for r in journal.db.execute('''SELECT p.*,a.agent_id,a.token_hold,a.sold_stages
                    FROM agent_positions a JOIN trade_positions p ON p.id=a.position_id WHERE a.agent_id=?''',(args.instance_id,))]
            else:
                url = os.environ.get(args.rpc_env) if args.rpc_env else None
                if args.rpc_env and not url:
                    raise TransferError('The selected RPC environment variable is empty')
                if command == 'reconcile-agent-orders':
                    result = ManualTradeService(TradeRpc(args.network,url),journal).reconcile(args.sender)
                    print(json.dumps(result,indent=2))
                    return 0
                with open_library(data_dir) as library:
                    if command == 'prepare-agent-order':
                        service = AgentTradeService(library,TradeRpc(args.network,url),journal)
                        result = service.prepare(args.instance_id,args.sender,args.mint,args.cash_reservation,
                                                 position_id=args.position_id,slippage_bps=args.slippage_bps)
                    else:
                        from getpass import getpass
                        from decimal import Decimal
                        from .pump_route import MAX_ACCOUNT_FUNDING
                        from .wallet_vault import WalletVault

                        p = journal.previews(args.order_id)
                        if not p.get('agent_order'):
                            raise TransferError('This preview is not an agent order')
                        service = AgentTradeService(library,TradeRpc(p['network'],url),journal)
                        service.validate(p)
                        buy = p['side']=='buy'
                        amount = sol_text(p['amount'])+' SOL' if buy else str(Decimal(p['amount'])/10**6)+' tokens'
                        minimum = str(Decimal(p['minimum_output'])/10**6)+' tokens' if buy else sol_text(p['minimum_output'])+' SOL before fees/account funding'
                        print(f"Agent: {p['agent_order']['agent_id']}\nReason: {p['agent_order']['reason']}\nNetwork: {p['network']}\nWallet: {p['sender']}\nMint: {p['mint']}\n{p['side'].upper()}: {amount}\nMinimum output: {minimum}")
                        print('Network fee: '+sol_text(p['fee'])+' SOL; account funding allowance: '+sol_text(MAX_ACCOUNT_FUNDING)+' SOL.')
                        print('Estimated wallet change: '+sol_text(p['expected_wallet_delta'])+' SOL.')
                        print(('Maximum debit: '+sol_text(p['max_wallet_debit']) if buy else
                               'Conservative net floor: '+sol_text(p['minimum_output']-p['fee']-MAX_ACCOUNT_FUNDING))+' SOL.')
                        print('This submits one real transaction. Automatic execution remains disabled.')
                        if not sys.stdin.isatty():
                            raise TransferError('Interactive terminal review is required; no noninteractive signing')
                        if input('Type SIGN '+p['id']+' to authorize: ').strip() != 'SIGN '+p['id']:
                            raise TransferError('Not authorized; nothing signed. The unsigned order remains available to cancel.')
                        wallet = WalletVault(data_dir).unlock(getpass('Wallet password: '))
                        signature = service.submit(p['id'],wallet)
                        service.manual.reconcile(wallet.address)
                        state = journal.db.execute('SELECT status FROM trades WHERE id=?',(p['id'],)).fetchone()[0]
                        result = {'id':p['id'],'signature':signature,'status':state}
            print(json.dumps(result,indent=2))
            return 0
        except (TransferError,ValueError,EOFError) as exc:
            print(str(exc),file=sys.stderr)
            return 2
        finally:
            journal.close()
    if command == "evaluate-recording":
        from .evaluation import evaluate, read_recording

        try:
            with open_library(data_dir) as library:
                package = library.instance(args.instance_id)["package"]
            print(json.dumps(evaluate(data_dir, package, read_recording(args.file)), indent=2))
            return 0
        except (OSError, ValueError, TypeError, KeyError) as exc:
            print(f"Local evaluation: {exc}", file=sys.stderr)
            return 2
    if command in ("portfolio-replay", "portfolio-status"):
        from .portfolio import PortfolioEngine, PortfolioJournal

        journal = None
        try:
            if command == "portfolio-replay":
                recording = json.loads(args.file.read_text())
                if recording.get('schema') != 'pumpwizard.portfolio-recording/v1' or not isinstance(recording.get('batches'), list):
                    raise ValueError('Expected a pumpwizard.portfolio-recording/v1 recording')
                configuration = recording['configuration']
                PortfolioEngine(configuration)
            journal = PortfolioJournal(data_dir)
            events = []
            if command == "portfolio-replay":
                for batch in recording['batches']:
                    events.extend(journal.advance(args.run_id, batch, configuration))
            checkpoint = journal.checkpoint(args.run_id)
            if checkpoint is None:
                raise ValueError('No recorded portfolio state for this run')
            print(json.dumps({'execution': 'offline-virtual-portfolio', 'run_id': args.run_id,
                              'new_events': events, 'checkpoint': checkpoint}, indent=2))
            return 0
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            print(f'Portfolio replay: {exc}', file=sys.stderr)
            return 2
        finally:
            if journal is not None:
                journal.close()
    if command in ("reserve-agent", "agent-reservations", "release-agent-reservation"):
        from .manual_trades import TradeJournal, TradeRpc, parse_tokens
        from .reservations import release, reserve, rows
        from .transfers import TransferError, parse_sol

        journal = TradeJournal(data_dir)
        try:
            if command == "reserve-agent":
                with open_library(data_dir) as library:
                    library.instance(args.instance_id)
                url = os.environ.get(args.rpc_env) if args.rpc_env else None
                if args.rpc_env and not url:
                    raise TransferError("The selected RPC environment variable is empty")
                result = reserve(journal, TradeRpc(args.network, url), args.instance_id, args.sender,
                                 lamports=parse_sol(args.sol) if args.sol is not None else 0,
                                 position_id=args.position_id,
                                 token_atoms=parse_tokens(args.tokens) if args.tokens is not None else 0)
            elif command == "agent-reservations":
                result = rows(journal, args.sender)
            else:
                result = release(journal, args.reservation_id)
            print(json.dumps(result, indent=2))
            return 0
        except (TransferError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        finally:
            journal.close()
    if command == "import":
        try:
            imported = parse_package(args.file.read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"Cannot read strategy file: {exc}", file=sys.stderr)
            return 2
        except StrategyImportError as exc:
            print(f"Cannot import strategy: {exc}", file=sys.stderr)
            return 2
        with open_library(data_dir) as library:
            created = library.import_strategy(imported)
        verb = "Imported" if created else "Already in library"
        print(f"{verb}: {imported.label} ({imported.strategy_id})")
        print(f"Compatibility: {imported.compatibility}")
        print(imported.reason)
        return 0
    with open_library(data_dir) as library:
        if command == "status":
            print(json.dumps({"app_version": __version__, **library.summary()}, indent=2))
            return 0
        if command == "create-instance":
            try:
                instance_id = library.create_instance(args.strategy_digest, args.label,
                                                     entry_mode=args.entry_mode, exit_mode=args.exit_mode)
            except ValueError as exc:
                print(f"Cannot create local instance: {exc}", file=sys.stderr)
                return 2
            print(f"Created local monitoring instance: {instance_id}")
            print("Creation does not authorize trading. Reviewed orders require separate eligibility checks and approval.")
            return 0
        if command == "instances":
            rows = library.instances()
            if not rows:
                print("No local strategy instances. Import a monitoring-ready configuration first.")
                return 0
            for row in rows:
                print(f"{row['id']} | {row['status']} | {row['strategy_id']} | "
                      f"entry={row['entry_mode']} exit={row['exit_mode']} | {row['label']}")
            return 0
        if command == "monitor":
            try:
                ownership_source = _ownership_source_from_env(args.ownership_rpc_env)
                monitor = LocalMonitor(library, args.instance_id, CompositePumpMarketSource(), ownership_source)
                if args.once:
                    result = monitor.run_once()
                    print(json.dumps(_monitor_result(result), indent=2))
                    return 0

                def report(cycle: MonitorCycle) -> None:
                    result = cycle.result
                    if result is None:
                        print(json.dumps({"instance_id": args.instance_id,
                                          "status": "market_data_failed",
                                          "detail": cycle.error}, indent=2), flush=True)
                    else:
                        print(json.dumps(_monitor_result(result), indent=2), flush=True)

                try:
                    monitor.run_forever(args.interval, on_cycle=report)
                except KeyboardInterrupt:
                    print("Local monitor stopped.", file=sys.stderr)
                return 0
            except (ValueError, MarketDataError) as exc:
                print(f"Monitor cycle did not complete: {exc}", file=sys.stderr)
                return 2
        if command == "monitor-runs":
            try:
                print(json.dumps({"health": library.monitor_health(args.instance_id),
                                  "runs": library.monitor_runs(args.instance_id, args.limit)}, indent=2))
            except ValueError as exc:
                print(f"Cannot read monitor history: {exc}", file=sys.stderr)
                return 2
            return 0
        if command in ("pause", "resume"):
            try:
                library.set_instance_status(args.instance_id, "paused" if command == "pause" else "monitoring")
            except ValueError as exc:
                print(f"Cannot change monitor state: {exc}", file=sys.stderr)
                return 2
            print("Local monitor paused." if command == "pause" else "Local monitor resumed.")
            return 0
        if command == "ownership-check":
            try:
                evidence = _ownership_source_from_env(args.rpc_env).check(args.mint, time.time())
                library.record_ownership(args.instance_id, evidence)
            except ValueError as exc:
                print(f"Cannot collect ownership evidence: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(evidence, indent=2))
            return 0
        if command == "ownership-history":
            try:
                print(json.dumps(library.ownership_history(args.instance_id, args.mint, args.limit), indent=2))
            except ValueError as exc:
                print(f"Cannot read ownership history: {exc}", file=sys.stderr)
                return 2
            return 0
        if command == "wallet-check":
            try:
                tracked_mints = {position.mint for position in library.position_views(args.instance_id)}
                snapshot = SolanaWalletSource(_rpc_value_from_env(args.rpc_env, "Wallet")).snapshot(
                    args.address, tracked_mints, time.time())
                library.record_wallet_check(args.instance_id, snapshot)
            except (ValueError, WalletDataError) as exc:
                print(f"Cannot collect wallet snapshot: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(snapshot, indent=2))
            return 0
        if command == "wallet-history":
            try:
                print(json.dumps(library.wallet_history(args.instance_id, args.address, args.limit), indent=2))
            except ValueError as exc:
                print(f"Cannot read wallet history: {exc}", file=sys.stderr)
                return 2
            return 0
        if command == "executor-config":
            try:
                library.set_executor_config(
                    args.instance_id, mode=args.mode, max_order_usd=args.max_order_usd,
                    max_slippage_bps=args.max_slippage_bps,
                    decision_ttl_seconds=args.decision_ttl_seconds, updated_at=time.time(),
                )
                print(json.dumps(library.executor_config(args.instance_id), indent=2))
            except ValueError as exc:
                print(f"Cannot set dry-run executor configuration: {exc}", file=sys.stderr)
                return 2
            return 0
        if command == "prepare-order":
            try:
                print(json.dumps(prepare_order(
                    library, args.instance_id, args.mint, spend_usd=args.spend_usd, now=time.time()), indent=2))
            except (ValueError, OrderIntentError) as exc:
                print(f"Cannot prepare dry-run order: {exc}", file=sys.stderr)
                return 2
            return 0
        if command == "simulate-order":
            try:
                print(json.dumps(simulate_order(library, args.order_id, now=time.time()), indent=2))
            except (ValueError, OrderIntentError) as exc:
                print(f"Cannot dry-run simulate order: {exc}", file=sys.stderr)
                return 2
            return 0
        if command == "cancel-order":
            try:
                print(json.dumps(cancel_order(library, args.order_id, now=time.time()), indent=2))
            except (ValueError, OrderIntentError) as exc:
                print(f"Cannot cancel local order: {exc}", file=sys.stderr)
                return 2
            return 0
        if command == "orders":
            try:
                print(json.dumps(library.orders(args.instance_id, args.limit), indent=2))
            except ValueError as exc:
                print(f"Cannot read local orders: {exc}", file=sys.stderr)
                return 2
            return 0
        if command == "order-events":
            try:
                print(json.dumps(library.order_events(args.order_id, args.limit), indent=2))
            except ValueError as exc:
                print(f"Cannot read local order events: {exc}", file=sys.stderr)
                return 2
            return 0
        if command == "localnet-signing-check":
            try:
                result = signing_check(args.keypair, args.recipient, args.lamports,
                                       rpc_url=args.rpc_url, now=time.time())
                library.record_localnet_check(args.instance_id, result)
                print(json.dumps(result, indent=2))
            except (ValueError, LocalnetError) as exc:
                print(f"Cannot complete localnet signing check: {exc}", file=sys.stderr)
                return 2
            return 0
        if command == "localnet-checks":
            try:
                print(json.dumps(library.localnet_checks(args.instance_id, args.limit), indent=2))
            except ValueError as exc:
                print(f"Cannot read localnet signer checks: {exc}", file=sys.stderr)
                return 2
            return 0
        if command == "decisions":
            try:
                print(json.dumps(library.decisions(args.instance_id, args.limit), indent=2))
            except ValueError as exc:
                print(f"Cannot read decisions: {exc}", file=sys.stderr)
                return 2
            return 0
        if command == "positions":
            try:
                print(json.dumps(library.positions(args.instance_id, include_closed=not args.open_only), indent=2))
            except ValueError as exc:
                print(f"Cannot read positions: {exc}", file=sys.stderr)
                return 2
            return 0
        if command == "record-position":
            try:
                position_id = library.record_position(
                    args.instance_id, args.mint, args.symbol, quantity=args.quantity,
                    total_cost_usd=args.cost_usd, opened_at=args.opened_at if args.opened_at is not None else time.time(),
                    management_mode=args.management_mode,
                )
            except ValueError as exc:
                print(f"Cannot record position: {exc}", file=sys.stderr)
                return 2
            print(f"Recorded user-confirmed position: {position_id}")
            return 0
        if command == "record-exit":
            try:
                library.record_position_exit(
                    args.position_id, quantity=args.quantity, proceeds_usd=args.proceeds_usd,
                    exited_at=args.exited_at if args.exited_at is not None else time.time(),
                    stages=tuple(args.stage),
                )
            except ValueError as exc:
                print(f"Cannot record exit: {exc}", file=sys.stderr)
                return 2
            print("Recorded user-confirmed exit.")
            return 0
        if command == "position-events":
            try:
                print(json.dumps(library.position_events(args.instance_id, args.limit), indent=2))
            except ValueError as exc:
                print(f"Cannot read position events: {exc}", file=sys.stderr)
                return 2
            return 0
        if command == "strategies":
            rows = library.rows()
    if not rows:
        print("No strategies imported. Use: solanamarket-wizard import path/to/strategy.json")
        return 0
    for row in rows:
        version = row["strategy_version"] or "legacy"
        print(f"{row['digest']} | {row['strategy_id']} {version} | {row['compatibility']} | {row['label']}")
    return 0


def _monitor_result(result: MonitorResult) -> dict[str, object]:
    return {"instance_id": result.instance_id, "observations": result.observations,
            "decisions": result.decisions, "at": result.at, "status": "completed"}


def _ownership_source_from_env(name: str | None) -> SolanaOwnershipSource | None:
    if name is None:
        return None
    return SolanaOwnershipSource(_rpc_value_from_env(name, "Ownership"))


def _rpc_value_from_env(name: str, purpose: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{purpose} RPC environment variable {name!r} is not set")
    return value


def _service_command(args, data_dir: Path) -> int:
    from .background_service import SERVICE_PORT
    from . import service_lifecycle

    action = args.service_command
    port = getattr(args, "port", SERVICE_PORT)
    try:
        if action == "run":
            web_root = _browser_root(args.web_root)
            return service_lifecycle.run_foreground(
                data_dir, port=port, web_root=web_root,
                expected_version=args.expected_version,
            )
        if action in {"start", "launch-dashboard"}:
            result = service_lifecycle.start(
                data_dir, port=port, web_root=_browser_root(args.web_root)
            )
            if action == "launch-dashboard":
                location = service_lifecycle.dashboard_url(data_dir, port=port)
                result = {
                    **result,
                    "url": location,
                    "opened": bool(webbrowser.open_new_tab(location)),
                    "notice": (
                        "Started or reused the authenticated local service and opened its Dashboard. "
                        "Dashboard access did not unlock a wallet or authorize trading."
                    ),
                }
        elif action == "status":
            result = service_lifecycle.probe(data_dir, port=port)
            if result is None:
                result = {
                    "service": {
                        "schema": "pumpwizard.background-service-status/v1",
                        "instance_id": None,
                        "instance_revision": 0,
                        "data_directory_id": service_lifecycle.data_directory_id(data_dir),
                        "version": __version__,
                        "state": "stopped",
                        "health": "unknown",
                        "production_trading": "blocked",
                    }
                }
        elif action == "stop":
            result = service_lifecycle.stop(data_dir, port=port)
        elif action == "open-dashboard":
            location = service_lifecycle.dashboard_url(data_dir, port=port)
            opened = webbrowser.open_new_tab(location)
            result = {"url": location, "opened": bool(opened),
                      "notice": "Dashboard access did not unlock or authorize trading."}
        elif action == "configure-monitor":
            result = service_lifecycle.configure_job(
                data_dir, {"enabled": args.enabled, "interval_seconds": args.interval},
                port=port,
            )
        elif action == "install-user":
            path = service_lifecycle.install_user_unit(
                executable=args.executable, data_dir=data_dir, port=port,
                unit_dir=args.unit_dir,
            )
            result = {
                "installed": str(path),
                "enabled": False,
                "started": False,
                "linger_changed": False,
                "notice": "Unit staged only. Enable/start and logout-survival policy remain explicit user choices.",
            }
        elif action == "remove-user":
            result = {"removed": service_lifecycle.remove_user_unit(unit_dir=args.unit_dir)}
        elif action == "backup":
            from .service_backup import create_backup

            result = create_backup(data_dir, args.destination)
        elif action == "restore":
            from .service_backup import restore_backup

            result = restore_backup(args.backup, data_dir)
        else:
            raise ValueError("Unknown service lifecycle command")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0
