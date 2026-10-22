from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
from threading import Thread
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
import pytest
from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.agent_hub import AgentHubError
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.evaluation import example_recording
from pumpwizard_runner.evidence_alerts import EvidenceAlertStore
from pumpwizard_runner.forward_campaigns import (
    REQUEST_SCHEMA as FORWARD_CAMPAIGN_REQUEST_SCHEMA,
    START_CONFIRMATION,
    ForwardCampaignStore,
)
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.transfers import TransferError
from pumpwizard_runner.web_api import assistant_command, command, payload, refresh_market, run_monitor_scan, run_portfolio_replay, serve, token_image, workspace
from pumpwizard_runner.web_assistant import BrowserAssistant
from pumpwizard_runner.web_reports import shadow, worker
from pumpwizard_runner.web_wallet import state as wallet_state


def test_read_api_exposes_library_records_only(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        release = payload(library, "/api/v1/release-note", {})
        assert release["note"]["status"] in {"draft", "published"}
        assert release["version"] in release["note"]["title"]
        assert release["url"].endswith("#" + release["note"]["id"])
        strategy = parse_package({"schema": "pumpwizard.strategy-handoff/v1", "strategy": {"id": "core-frequency", "label": "Core"}, "configuration": CONFIG})
        library.import_strategy(strategy)
        ident = library.create_instance(strategy.digest)
        assert workspace(library)["sessions"] == 1
        sessions = payload(library, "/api/v1/sessions", {})["rows"]
        assert sessions[0]["instance"]["id"] == ident
        assert sessions[0]["brief"]["schema"] == "pumpwizard.session-brief/v3"
        assert sessions[0]["brief"]["mode"] == "monitoring"
        assert sessions[0]["brief"]["permissions"]["automatic_entries"] is False
        assert sessions[0]["state"]["label"] == "Ready"
        assert sessions[0]["positions"] == []
        assert sessions[0]["entry_considerations"] == []
        assert sessions[0]["exit_plan"] == {
            "approval": "manual",
            "stop_loss_percent": 30.0,
            "take_profit_percent": None,
            "trailing_drawdown_percent": 35.0,
            "max_hold_seconds": 864000.0,
            "targets": [
                {"market_cap_usd": 30000.0, "sell_percent": 40.0},
                {"market_cap_usd": 60000.0, "sell_percent": 30.0},
                {"market_cap_usd": 100000.0, "sell_percent": 20.0},
            ],
        }
        session = payload(library, f"/api/v1/sessions/{ident}", {})
        assert session["state"]["action"] == "Review setup"
        assert session["exit_plan"] == sessions[0]["exit_plan"]
        assert session["monitor_plan"]["entry_cap_min"] == CONFIG["entry_cap_min"]
        assert session["monitor_plan"]["entry_cap_max"] == CONFIG["entry_cap_max"]
        assert session["monitor_plan"]["scan_interval_seconds"] == 15
        assert session["risk_policy"] is None
        assert session["observations"] == []
        assert session["entry_considerations"] == []
        assert session["outcomes"] == []
        assert session["monitor_lease"] is None
        library.append_observations(ident, [{
            "mint": "CandidateMint", "symbol": "CAND", "observed_at": 100,
            "price_usd": 0.01, "market_cap_usd": 10_000, "liquidity_usd": 2_000,
        }])
        library.record_decisions(ident, [{
            "kind": "entry_blocked", "mint": "CandidateMint", "at": 100,
            "reason": "Building observation window", "payload": {"symbol": "CAND"},
        }])
        current = payload(library, "/api/v1/sessions", {})["rows"][0]
        assert current["entry_considerations"][0]["kind"] == "entry_blocked"
        assert current["entry_considerations"][0]["observation"]["liquidity_usd"] == 2_000
        library.record_position(ident, "CandidateMint", "CAND", quantity=10,
                                total_cost_usd=1, opened_at=101)
        positioned = payload(library, "/api/v1/sessions", {})["rows"][0]
        assert positioned["entry_considerations"] == []
        assert positioned["positions"][0]["valuation"]["schema"] == (
            "pumpwizard.holding-valuation/v1"
        )
        assert positioned["positions"][0]["valuation"]["basis"]["amount"] == "1"
        assert positioned["positions"][0]["valuation"]["mark"]["freshness"] == "stale"
        assert positioned["positions"][0]["valuation"]["current_value"]["state"] == "unknown"
        assert payload(library, "/api/v1/market", {"limit": ["1"]})["rows"] == []
        strategy_row = payload(library, "/api/v1/strategies", {})["rows"][0]
        assert strategy_row["strategy_id"] == "core-frequency"
        assert strategy_row["configuration"] == CONFIG
        detail = payload(library, f"/api/v1/strategies/{strategy.digest}", {})
        assert detail["identity"]["package_sha256"] == strategy.digest
        assert detail["capability"] == "Reviewed eligible"
        assert "Funded agent lifecycle acceptance: pending." in detail["support"]
        assert "package" not in detail["strategy"]
        exported = payload(library, f"/api/v1/strategies/{strategy.digest}/export", {})
        assert exported["package"]["schema"] == "pumpwizard.strategy-handoff/v1"
        assert "wallet" in exported["notice"].lower()
        marketplace = payload(library, "/api/v1/marketplace", {})
        assert marketplace["rows"] and marketplace["rows"][0]["package"]["schema"] == "pumpwizard.strategy-handoff/v1"
        assert marketplace["rows"][0]["digest"] == parse_package(
            marketplace["rows"][0]["package"]
        ).digest
        assert payload(library, "/api/v1/evaluations", {})["rows"] == []
        assert payload(library, "/api/v1/shadow-reports", {})["rows"] == []
        assert payload(library, "/api/v1/worker-rehearsals", {})["rows"] == []
        assert payload(library, "/api/v1/portfolio-replays", {})["rows"] == []
        wallet = payload(library, "/api/v1/wallet-state", {})
        assert wallet["browser_wallet_connected"] is False
        assert wallet["signing_available"] is False
        assert wallet["balance_snapshots"] == []
        assert "wallet" not in json.dumps(payload(library, "/api/v1/workspace", {})).lower()
    finally:
        library.close()


def test_trade_positions_expose_backend_typed_valuation_without_provider_reads(
        tmp_path: Path, monkeypatch):
    sender = "11111111111111111111111111111111"
    observed_at = time.time() - 5
    library = StrategyLibrary(tmp_path)
    try:
        library.record_market_watch_quote(MarketQuote(
            mint="TradeMint", symbol="TRADE", observed_at=observed_at,
            route="curve", pool="TradePool", market_cap_usd=10_000,
            price_usd=2, price_native=.02, liquidity_usd=5_000,
            liquidity_native=50,
        ))

        monkeypatch.setattr("pumpwizard_runner.web_api.trade_state", lambda *_args: {
            "sender": sender,
            "network": "mainnet-beta",
            "positions": [{
                "id": "position-1", "mint": "TradeMint", "status": "open",
                "quantity": "500000", "decimals": 6,
                "remaining_cost": "500000000", "original_cost": "500000000",
                "realized_proceeds": "0", "transaction_fees": "5000",
                "agent_id": None, "managed": False, "entry_fx": "100",
                "sold_stages": [], "peak_price_usd": None, "last_usable_at": None,
            }],
            "orders": [],
            "pending": None,
        })

        class Authorizer:
            def require(self, actual_sender, network):
                assert (actual_sender, network) == (sender, "mainnet-beta")

        result = payload(
            library,
            "/api/v1/trades",
            {"sender": [sender], "network": ["mainnet-beta"]},
            wallet_authorizer=Authorizer(),
        )
        valuation = result["positions"][0]["valuation"]
        assert valuation["schema"] == "pumpwizard.holding-valuation/v1"
        assert valuation["quantity"]["amount"] == "0.5"
        assert valuation["basis"]["amount"] == "50"
        assert valuation["basis"]["native_amount"] == "500000000"
        assert valuation["mark"]["source"] == "retained_market_watch"
        assert valuation["current_value"]["amount"] == "1"
        assert valuation["unrealized_result"]["amount"] == "-49"
        assert valuation["authority"] == {
            "quote": False, "signer": False, "orders": False, "submission": False,
        }
    finally:
        library.close()


def test_campaign_api_resolves_frozen_arms_and_keeps_reads_inert(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package({
            "schema": "pumpwizard.strategy-handoff/v1",
            "strategy": {"id": "campaign-api", "label": "Campaign API"},
            "configuration": CONFIG,
        })
        library.import_strategy(strategy)
        first = library.create_instance(strategy.digest, "First arm")
        second = library.create_instance(strategy.digest, "Second arm")
        manifest = {
            "schema": FORWARD_CAMPAIGN_REQUEST_SCHEMA,
            "campaign_key": "browser-campaign",
            "supersedes_campaign_id": None,
            "source": {
                "provider": "CompositePumpMarketSource", "minimum_cap": 1_000,
                "maximum_cap": 50_000, "priority_mints": [], "limit": 20,
                "cadence_seconds": 15,
            },
            "arms": [{
                "id": "first", "source_session_id": first,
                "strategy_sha256": strategy.digest, "simulated_capital_usd": 100,
            }, {
                "id": "second", "source_session_id": second,
                "strategy_sha256": strategy.digest, "simulated_capital_usd": 100,
            }],
            "assumptions": {
                "cost_model": "strategy package costs",
                "execution_model": "paper replay v3",
            },
            "coverage": {
                "max_source_age_seconds": 120, "minimum_independent_tokens": 1,
                "minimum_observations": 10, "maximum_gap_seconds": 60,
            },
            "duration_seconds": 600,
            "quotas": {
                "max_requests": 20, "max_active_runtime_seconds": 600,
                "max_storage_bytes": 10_000_000, "max_concurrency": 2,
            },
            "provider_cost": {
                "currency": "USD", "marginal_cost_per_request": None,
                "fixed_subscription_cost": None, "price_basis": "unknown",
            },
        }
        started = command(library, "/api/v1/research/campaigns", {
            "schema": "pumpwizard.forward-campaign-start/v1",
            "campaign": manifest, "idempotency_key": "browser-start-1",
            "confirmation": START_CONFIRMATION,
        })["campaign"]
        campaign_id = started["id"]
        assert len(started["arms"]) == 2
        assert started["authority"]["orders"] is False
        repeated = command(library, "/api/v1/research/campaigns", {
            "schema": "pumpwizard.forward-campaign-start/v1",
            "campaign": manifest, "idempotency_key": "browser-start-1",
            "confirmation": START_CONFIRMATION,
        })["campaign"]
        assert repeated["id"] == campaign_id

        before = payload(library, f"/api/v1/research/campaigns/{campaign_id}", {})
        after = payload(library, f"/api/v1/research/campaigns/{campaign_id}", {})
        assert before["campaign"]["state"] == after["campaign"]["state"] == "running"
        assert before["campaign"]["progress"]["events"] == \
            after["campaign"]["progress"]["events"] == 1
        paused = command(library, f"/api/v1/research/campaigns/{campaign_id}/pause", {
            "schema": "pumpwizard.forward-campaign-command/v1",
            "campaign_id": campaign_id, "command": "pause",
            "idempotency_key": "pause-1",
        })["campaign"]
        assert paused["state"] == "paused"
        resumed = command(library, f"/api/v1/research/campaigns/{campaign_id}/resume", {
            "schema": "pumpwizard.forward-campaign-command/v1",
            "campaign_id": campaign_id, "command": "resume",
            "idempotency_key": "resume-1",
        })["campaign"]
        assert resumed["state"] == "running"
        report = command(library, f"/api/v1/research/campaigns/{campaign_id}/reports", {
            "schema": "pumpwizard.forward-report-request/v1",
            "campaign_id": campaign_id, "previous_report_id": None,
            "created_at": started["start_at"] + 1,
        })["campaign_report"]
        assert report["campaign_id"] == campaign_id
        assert report["claims"]["funded_performance"] is False
        assert payload(
            library, f"/api/v1/research/campaign-reports/{report['id']}", {},
        )["campaign_report"] == report
        stopped = command(library, f"/api/v1/research/campaigns/{campaign_id}/stop", {
            "schema": "pumpwizard.forward-campaign-command/v1",
            "campaign_id": campaign_id, "command": "stop",
            "idempotency_key": "stop-1", "confirmation": campaign_id,
        })["campaign"]
        assert stopped["state"] == "stopped"
    finally:
        library.close()


def test_observation_preset_routes_prepare_without_io_and_export_bounded_summary(
        tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        prepared = command(
            library, "/api/v1/research/observation-presets/24-hour", {
                "schema": "pumpwizard.observation-preset-prepare/v1",
                "idempotency_key": "browser-prepare-24h",
            },
        )
        preset = prepared["observation_preset"]
        assert preset["network_reads"] == 0
        assert preset["dry_run"]["scheduled_intervals"] == 96
        assert preset["dry_run"]["cost"]["fully_costed_profitability_allowed"] is False
        assert "billing remains unknown" in prepared["notice"]

        listed = payload(
            library, "/api/v1/research/observation-presets", {},
        )["observation_presets"]
        assert [item["id"] for item in listed] == [preset["id"]]
        detail = payload(
            library, f"/api/v1/research/observation-presets/{preset['id']}", {},
        )["observation_preset"]
        assert detail["identity_sha256"] == preset["identity_sha256"]

        started = command(
            library, f"/api/v1/research/observation-presets/{preset['id']}/start", {
                "schema": "pumpwizard.observation-preset-start/v1",
                "preset_id": preset["id"],
                "idempotency_key": "browser-start-24h",
                "confirmation": START_CONFIRMATION,
            },
        )
        campaign = started["campaign"]
        assert campaign["preset_id"] == preset["id"]
        assert campaign["authority"]["orders"] is False
        assert campaign["authority"]["submission"] is False

        summary = payload(
            library,
            f"/api/v1/research/campaigns/{campaign['id']}/operational-summary", {},
        )["operational_summary"]
        assert summary["manifest_sha256"] == campaign["request_sha256"]
        assert summary["resources"]["logical_provider_attempts"] == 0
        assert summary["profitability"]["fully_costed_allowed"] is False
        exported = payload(
            library,
            f"/api/v1/research/campaigns/{campaign['id']}/operational-export", {},
        )["operational_export"]
        assert exported["summary"]["campaign_id"] == campaign["id"]
        assert len(exported["summary_sha256"]) == 64

        stopped = command(
            library, f"/api/v1/research/campaigns/{campaign['id']}/stop", {
                "schema": "pumpwizard.forward-campaign-command/v1",
                "campaign_id": campaign["id"], "command": "stop",
                "idempotency_key": "browser-stop-24h",
                "confirmation": campaign["id"],
            },
        )
        assert stopped["campaign"]["state"] == "stopped"
    finally:
        library.close()


def test_campaign_alert_api_acknowledges_without_resolving(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        with ForwardCampaignStore(tmp_path) as campaigns:
            # Reuse the strict manifest helper from the campaign module tests via
            # a one-arm server-side package to avoid involving a collection loop.
            strategy = parse_package({
                "schema": "pumpwizard.strategy-handoff/v1",
                "strategy": {"id": "alert-api", "label": "Alert API"},
                "configuration": CONFIG,
            })
            request = {
                "schema": FORWARD_CAMPAIGN_REQUEST_SCHEMA,
                "campaign_key": "alert-api", "supersedes_campaign_id": None,
                "source": {"provider": "CompositePumpMarketSource", "minimum_cap": 1_000,
                           "maximum_cap": 50_000, "priority_mints": [], "limit": 10,
                           "cadence_seconds": 15},
                "arms": [{"id": "only", "source_session_id": "session-only",
                          "strategy_sha256": strategy.digest,
                          "strategy_package": strategy.package,
                          "simulated_capital_usd": 100}],
                "assumptions": {"cost_model": "declared", "execution_model": "paper"},
                "coverage": {"max_source_age_seconds": 120,
                             "minimum_independent_tokens": 1,
                             "minimum_observations": 1, "maximum_gap_seconds": 60},
                "duration_seconds": 600,
                "quotas": {"max_requests": 1, "max_active_runtime_seconds": 600,
                           "max_storage_bytes": 1_000_000, "max_concurrency": 1},
                "provider_cost": {"currency": "USD", "marginal_cost_per_request": None,
                                  "fixed_subscription_cost": None,
                                  "price_basis": "unknown"},
            }
            campaign = campaigns.start(
                request, idempotency_key="alert-api", confirmation=START_CONFIRMATION,
            )
            campaigns.record_scan(
                [campaign["id"]], success=True, finished_at=time.time() + 0.001,
            )
            view = campaigns.campaign(campaign["id"])
        with EvidenceAlertStore(tmp_path) as alerts:
            alerts.evaluate(view)
            alert = alerts.alerts(campaign_id=campaign["id"])[0]
        result = command(
            library, f"/api/v1/research/alerts/{alert['id']}/acknowledge", {
                "schema": "pumpwizard.evidence-alert-acknowledgment/v1",
                "alert_id": alert["id"],
            },
        )["alert"]
        assert result["state"] == "open"
        assert result["acknowledged_at"] is not None
    finally:
        library.close()


@pytest.mark.parametrize("owner_kind", ["unowned", "workspace", "legacy-policy"])
def test_session_review_api_freezes_historical_evidence_and_creates_only_a_safe_draft(tmp_path: Path, owner_kind):
    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package({
            "schema": "pumpwizard.strategy-handoff/v1",
            "strategy": {"id": "core-frequency", "label": "Core"},
            "configuration": CONFIG,
        })
        library.import_strategy(strategy)
        source_id = library.create_instance(strategy.digest, "Source evidence")
        from solders.keypair import Keypair
        from pumpwizard_runner.transfers import GENESIS
        sender = str(Keypair().pubkey())
        if owner_kind == "workspace":
            owned = library.create_session_workspace("monitoring", wallet_sender=sender, wallet_network="mainnet-beta")
            library.assign_instance_to_workspace(owned["id"], source_id)
        elif owner_kind == "legacy-policy":
            with closing(TradeJournal(tmp_path)) as journal:
                with journal.db:
                    journal.db.execute("INSERT INTO strategy_risk_policies VALUES (?,?,?,?,?)",
                                       (source_id, GENESIS["mainnet-beta"], sender, "{}", 1))
        quotes = [MarketQuote(
            mint="ReviewMint", symbol="REV", observed_at=float(at), route="curve",
            pool="ReviewPool", market_cap_usd=15_000 + at, price_usd=1 + at / 10_000,
            price_native=.01 + at / 1_000_000, liquidity_usd=10_000,
            liquidity_native=100, source="synthetic-test", quote_usd=100,
            base_reserve=1_000_000, reserve_usd=10_000,
            real_token_reserve=500_000, supply=1_000_000_000, decimals=6,
            source_at=float(at), fx_at=float(at),
        ).as_record() for at in (1_000, 1_300, 1_600, 1_900, 2_200, 2_500)]
        library.append_observations(source_id, quotes)
        library.record_decisions(source_id, [{
            "kind": "entry_blocked", "mint": "ReviewMint", "at": 1_300,
            "reason": "Building a 60-second observation window", "payload": {},
        }])

        result = command(
            library, f"/api/v1/sessions/{source_id}/reviews", {"horizon_seconds": 300},
        )
        assert result["dataset"]["evidence"]["eligible_decision_capture"] == "historical_transition_records_only"
        assert result["report"]["recommendation"]["action"] == "collect_more_data"
        listed = payload(library, f"/api/v1/sessions/{source_id}/reviews", {})
        assert listed["reports"] == [result["report"]]
        assert listed["evidence"] == result["dataset"]["evidence"]

        report = result["report"]
        candidate = next(row for row in report["candidate_packages"] if row["kind"] == "candidate")
        report["recommendation"] = {
            "action": "create_inactive_draft",
            "candidate_id": candidate["id"],
            "reason": "Synthetic selected-candidate API fixture.",
        }
        review_db = sqlite3.connect(tmp_path / "session-reviews.sqlite")
        try:
            review_db.execute(
                "UPDATE reports SET payload_json=? WHERE id=?",
                (json.dumps(report, sort_keys=True, separators=(",", ":")), report["id"]),
            )
            review_db.commit()
        finally:
            review_db.close()

        draft = command(library, f"/api/v1/session-reviews/{report['id']}/draft", {
            "candidate_id": candidate["id"], "accepted": True,
        })
        assert library.instance(source_id)["strategy_digest"] == strategy.digest
        successor = library.instance(draft["session"]["id"])
        assert successor["status"] == "paused"
        assert successor["entry_mode"] == "disabled"
        assert successor["exit_mode"] == "manual"
        assert library.tracking_settings(successor["id"])["enabled"] == 0
        assert draft["management"]["allocation_lamports"] == "0"
        assert draft["brief"]["session_mode"] == "monitoring"
        if owner_kind == "unowned":
            assert successor["workspace_id"] is None
        else:
            owner = library.session_workspace(successor["workspace_id"])
            assert (owner["wallet_sender"], owner["wallet_network"]) == (sender, "mainnet-beta")
            assert owner["allocation_lamports"] == "0"
        assert draft["draft_request"]["requested_session_state"]["copy_execution_authorization"] is False
    finally:
        library.close()


def test_token_icon_proxy_requires_approved_hosts_and_bounded_artwork(tmp_path: Path, monkeypatch):
    mint = "So11111111111111111111111111111111111111112"
    library = StrategyLibrary(tmp_path)
    library.record_coin_metadata(mint, {
        "image_url": "https://cdn.dexscreener.com/cms/images/token.png",
    }, updated_at=time.time())

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self):
            return "https://cdn.dexscreener.com/cms/images/token.png"

        def read(self, _limit):
            return b"\x89PNG\r\n\x1a\nsynthetic"

    class Opener:
        def open(self, *_args, **_kwargs):
            return Reply()

    monkeypatch.setattr("pumpwizard_runner.web_api.urllib.request.build_opener", lambda *_args: Opener())
    try:
        assert token_image(library, mint) == ("image/png", b"\x89PNG\r\n\x1a\nsynthetic")
        library.record_coin_metadata(mint, {"image_url": "https://example.com/token.png"}, updated_at=time.time() + 1)
        with pytest.raises(ValueError, match="not approved"):
            token_image(library, mint)
    finally:
        library.close()


def test_token_icon_proxy_uses_exact_mint_alternative_and_caches_resolution(tmp_path, monkeypatch):
    from pumpwizard_runner import web_api

    mint = "So11111111111111111111111111111111111111112"
    library = StrategyLibrary(tmp_path)
    primary = "https://pumpapi.io/token-artwork.png"
    alternative = "https://backpack.exchange/token-artwork.png"
    library.record_coin_metadata(mint, {"image_url": primary, "image_urls": [primary, alternative]}, updated_at=time.time())
    calls = []
    monkeypatch.setattr(web_api, "_TOKEN_IMAGE_CACHE", {})

    class Reply:
        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def geturl(self):
            return self.url

        def read(self, _limit):
            return b"<svg>not a raster</svg>" if self.url == primary else b"\x89PNG\r\n\x1a\nvalid alternative"

    class Opener:
        def open(self, request, **_kwargs):
            calls.append(request.full_url)
            return Reply(request.full_url)

    monkeypatch.setattr(web_api.urllib.request, "build_opener", lambda *_args: Opener())
    try:
        expected = ("image/png", b"\x89PNG\r\n\x1a\nvalid alternative")
        assert token_image(library, mint) == expected
        assert token_image(library, mint) == expected
        assert calls == [primary, alternative]
        redirect = web_api._TokenImageRedirect()
        with pytest.raises(ValueError, match="unapproved"):
            redirect.redirect_request(Request(primary), None, 302, "Found", {}, "http://127.0.0.1/private")
        assert not web_api._approved_token_image_url("https://user:password@backpack.exchange/icon.png")
        assert not web_api._approved_token_image_url("https://backpack.exchange:8443/icon.png")
    finally:
        library.close()


def test_browser_session_creation_is_disabled_and_manual(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package({"schema": "pumpwizard.strategy-handoff/v1", "strategy": {"id": "core-frequency", "label": "Core"}, "configuration": CONFIG})
        library.import_strategy(strategy)
        result = command(library, "/api/v1/sessions", {"digest": strategy.digest, "label": "Browser session"})
        instance = result["instance"]
        assert instance["entry_mode"] == "disabled"
        assert instance["exit_mode"] == "manual"
        assert library.tracking_settings(instance["id"])["enabled"] == 0
        assert result["brief"]["permissions"]["prepare_orders"] is False
        assert result["brief"]["permissions"]["automatic_entries"] is False
    finally:
        library.close()


def test_main_wallet_autopilot_routes_require_wallet_auth_and_exact_runtime_revision(tmp_path: Path):
    sender = "11111111111111111111111111111111"
    network = "mainnet-beta"
    calls = []

    class Authorizer:
        def require(self, requested_sender, requested_network):
            assert (requested_sender, requested_network) == (sender, network)
            calls.append(("authenticated", requested_sender, requested_network))
            return requested_sender, requested_network

    class Controller:
        def status(self, requested_sender, requested_network, session_id=None):
            assert (requested_sender, requested_network) == (sender, network)
            return {
                "allowances": [{
                    "id": "allowance-1", "session_id": session_id or "session-1",
                    "authorization_revision": 4, "status": "authorized",
                }],
                "runtime": {},
            }

        def review(self, _library, **values):
            calls.append(("review", values))
            return {"id": "review-1", "ready": True}

        def heartbeat(self, **values):
            calls.append(("heartbeat", values))
            return {"allowance_id": "allowance-1", "authorization_revision": 4,
                    "heartbeat_deadline": 1000}

    library = StrategyLibrary(tmp_path)
    controller = Controller()
    authorizer = Authorizer()
    try:
        with pytest.raises(TransferError, match="unavailable"):
            payload(
                library, "/api/v1/main-wallet-autopilot",
                {"sender": [sender], "network": [network]},
                main_wallet_controller=controller,
            )
        status = payload(
            library, "/api/v1/main-wallet-autopilot/status",
            {"sender": [sender], "network": [network], "session_id": ["session-1"]},
            wallet_authorizer=authorizer, main_wallet_controller=controller,
        )
        assert status["allowances"][0]["session_id"] == "session-1"
        reviewed = command(
            library, "/api/v1/main-wallet-autopilot/review", {
                "sender": sender, "network": network, "session_id": "session-1",
                "wallet_kind": "local", "plan": {"synthetic": True},
            }, wallet_authorizer=authorizer, main_wallet_controller=controller,
        )
        assert reviewed["review"] == {"id": "review-1", "ready": True}
        with pytest.raises(TransferError, match="fields are invalid"):
            command(
                library, "/api/v1/main-wallet-autopilot/review",
                {"sender": sender, "network": network, "session_id": "session-1"},
                wallet_authorizer=authorizer, main_wallet_controller=controller,
            )
        with pytest.raises(TransferError, match="revision is stale"):
            command(
                library, "/api/v1/main-wallet-autopilot/heartbeat", {
                    "sender": sender, "network": network, "allowance_id": "allowance-1",
                    "runtime_token": "ephemeral", "authorization_revision": 3,
                }, wallet_authorizer=authorizer, main_wallet_controller=controller,
            )
        heartbeat = command(
            library, "/api/v1/main-wallet-autopilot/heartbeat", {
                "sender": sender, "network": network, "allowance_id": "allowance-1",
                "runtime_token": "ephemeral", "authorization_revision": 4,
            }, wallet_authorizer=authorizer, main_wallet_controller=controller,
        )
        assert heartbeat["heartbeat_deadline"] == 1000
        assert any(call[0] == "review" for call in calls)
        assert any(call[0] == "heartbeat" for call in calls)
    finally:
        library.close()


def test_browser_session_creation_retains_reviewed_research_brief(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package({"schema": "pumpwizard.strategy-handoff/v1", "strategy": {"id": "core-frequency", "label": "Core"}, "configuration": CONFIG})
        library.import_strategy(strategy)
        result = command(library, "/api/v1/sessions", {
            "digest": strategy.digest,
            "label": "Evidence run",
            "brief": {
                "goal": "collect-evidence",
                "intent": "Retain decisions for a later chronological Lab comparison.",
                "paper_budget_usd": 500,
                "max_session_loss_usd": 100,
                "review_after_seconds": 86_400,
            },
        })
        ident = result["instance"]["id"]
        assert result["brief"]["goal"] == "collect-evidence"
        assert result["brief"]["paper_order_usd"] == CONFIG["order_usd"]
        assert result["brief"]["max_positions"] == CONFIG["max_positions"]
        assert payload(library, f"/api/v1/sessions/{ident}", {})["brief"] == result["brief"]
        assert "monitoring session" in result["notice"]
    finally:
        library.close()


def test_monitoring_confirmation_creates_empty_session_then_accepts_strategies(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        created = command(library, "/api/v1/session-workspaces", {"mode": "monitoring"})
        workspace_id = created["session"]["id"]
        assert created["session"]["strategy_count"] == 0
        assert created["session"]["allocation_lamports"] == "0"
        assert "no wallet allocation" in created["notice"]
        assert payload(library, "/api/v1/session-workspaces", {})["rows"][0]["strategy_ids"] == []

        strategy = parse_package({
            "schema": "pumpwizard.strategy-handoff/v1",
            "strategy": {"id": "core-frequency", "label": "Core"},
            "configuration": CONFIG,
        })
        library.import_strategy(strategy)
        attached = command(library, "/api/v1/sessions", {
            "digest": strategy.digest,
            "label": "Core observer",
            "workspace_id": workspace_id,
            "brief": {
                "goal": "collect-evidence",
                "intent": "Retain monitoring evidence without wallet authority.",
                "paper_budget_usd": 500,
                "max_session_loss_usd": 100,
                "review_after_seconds": 86_400,
                "session_mode": "monitoring",
                "execution_style": None,
                "profit_management": None,
            },
        })
        assert attached["instance"]["workspace_id"] == workspace_id
        listed = payload(library, "/api/v1/session-workspaces", {})["rows"][0]
        assert listed["strategy_ids"] == [attached["instance"]["id"]]
        assert listed["strategy_count"] == 1
        with pytest.raises(ValueError, match="assigned strategies"):
            command(library, f"/api/v1/session-workspaces/{workspace_id}/remove", {"confirm": workspace_id})
    finally:
        library.close()


def test_empty_monitoring_session_can_be_removed_without_strategy_evidence(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        created = command(library, "/api/v1/session-workspaces", {"mode": "monitoring"})
        workspace_id = created["session"]["id"]
        removed = command(
            library, f"/api/v1/session-workspaces/{workspace_id}/remove", {"confirm": workspace_id},
        )
        assert removed["removed"] is True
        assert payload(library, "/api/v1/session-workspaces", {})["rows"] == []
    finally:
        library.close()


def test_trading_workspace_reserves_once_and_shares_cash_with_assigned_strategies(
    tmp_path: Path, monkeypatch,
):
    sender = "11111111111111111111111111111111"

    class Rpc:
        def genesis(self):
            from pumpwizard_runner.transfers import GENESIS

            return GENESIS["mainnet-beta"]

        def call(self, method, params):
            assert method == "getBalance"
            assert params == [sender, {"commitment": "confirmed"}]
            return {"value": 1_000_000_000}

    class Authorizer:
        def require(self, actual_sender, network):
            assert (actual_sender, network) == (sender, "mainnet-beta")

    monkeypatch.setattr(
        "pumpwizard_runner.web_session_allocations.web_trading.rpc_for",
        lambda network, directory: Rpc(),
    )
    monkeypatch.setattr(
        "pumpwizard_runner.web_strategy_controls.web_trading.rpc_for",
        lambda network, directory: Rpc(),
    )
    funding = {
        "sender": sender,
        "network": "mainnet-beta",
        "allocation_sol": "0.2",
    }
    strategy_funding = {
        **funding,
        "buys_enabled": False,
        "sells_enabled": True,
        "min_trade_sol": "0.005",
        "max_trade_sol": "0.02",
        "max_daily_buy_sol": "0.07",
        "max_trades_per_day": 8,
        "max_daily_loss_sol": "0.02",
        "max_daily_loss_percent": "10",
        "max_trade_loss_percent": "15",
        "max_open_positions": 5,
    }
    plan = {
        "session_mode": "trade",
        "execution_style": "interactive",
        "automation_scope": None,
        "custody_mode": None,
        "profit_management": "user-directed",
        "allocation_sol": "0.2",
    }
    brief = {
        "goal": "validate-strategy",
        "intent": "Test two strategies against one bounded session wallet pool.",
        "paper_budget_usd": 500,
        "max_session_loss_usd": 100,
        "review_after_seconds": 86_400,
        "session_mode": "trade",
        "execution_style": "interactive",
        "profit_management": "user-directed",
    }
    library = StrategyLibrary(tmp_path)
    try:
        created = command(
            library,
            "/api/v1/session-workspaces",
            {"mode": "trade", "funding": funding, "plan": plan},
            wallet_authorizer=Authorizer(),
        )
        workspace_id = created["session"]["id"]
        assert created["session"]["allocation_lamports"] == "200000000"
        assert created["session"]["available_lamports"] == "200000000"
        assert created["session"]["strategy_count"] == 0
        assert created["session"]["plan"] == plan

        empty_funding = {**funding, "allocation_sol": "0.1"}
        empty_plan = {**plan, "allocation_sol": "0.1"}
        empty = command(
            library,
            "/api/v1/session-workspaces",
            {"mode": "trade", "funding": empty_funding, "plan": empty_plan},
            wallet_authorizer=Authorizer(),
        )
        empty_id = empty["session"]["id"]
        released_empty = command(
            library,
            f"/api/v1/session-workspaces/{empty_id}/remove",
            {"confirm": empty_id},
        )
        assert released_empty["released_allocation_lamports"] == "100000000"

        strategy = parse_package({
            "schema": "pumpwizard.strategy-handoff/v1",
            "strategy": {"id": "shared-session", "label": "Shared"},
            "configuration": CONFIG,
        })
        library.import_strategy(strategy)
        results = []
        for label in ("First", "Second"):
            assigned = strategy
            if label == "Second":
                assigned = parse_package({"schema": "pumpwizard.strategy-handoff/v1",
                    "strategy": {"id": "second-strategy", "label": "Second strategy"},
                    "configuration": {**CONFIG, "entry_cap_min": 11000}})
                library.import_strategy(assigned)
            results.append(command(
                library,
                "/api/v1/sessions",
                {
                    "digest": assigned.digest,
                    "label": label,
                    "workspace_id": workspace_id,
                    "brief": brief,
                    "funding": strategy_funding,
                },
                wallet_authorizer=Authorizer(),
            ))

        journal = TradeJournal(tmp_path)
        try:
            cash = list(journal.db.execute(
                "SELECT * FROM agent_reservations "
                "WHERE status='held' AND position_id IS NULL"
            ))
            assert len(cash) == 1
            assert cash[0]["agent_id"] == f"workspace:{workspace_id}"
            bindings = list(journal.db.execute(
                "SELECT * FROM strategy_workspace_allocations ORDER BY agent_id"
            ))
            assert {row["agent_id"] for row in bindings} == {
                result["instance"]["id"] for result in results
            }
            assert {row["cash_hold"] for row in bindings} == {cash[0]["id"]}
            assert {result["funding"]["hold_id"] for result in results} == {cash[0]["id"]}
        finally:
            journal.close()

        listed = payload(library, "/api/v1/session-workspaces", {})["rows"][0]
        assert listed["strategy_count"] == 2
        assert listed["available_lamports"] == "200000000"
        assert listed["committed_lamports"] == "0"
        first_id = results[0]["instance"]["id"]
        detail = payload(library, f"/api/v1/sessions/{first_id}", {})
        assert detail["management"]["allocation_lamports"] == "0"
        assert detail["management"]["workspace_allocation_lamports"] == "200000000"
        funded_detail = payload(
            library,
            f"/api/v1/sessions/{first_id}",
            {"sender": [sender], "network": ["mainnet-beta"]},
        )
        assert funded_detail["risk_policy"]["available_cash_lamports"] == "200000000"
        removed = command(
            library,
            f"/api/v1/sessions/{first_id}/remove",
            {"confirm": first_id, "release_allocation": False},
        )
        assert removed["released_allocation_lamports"] == "0"
        journal = TradeJournal(tmp_path)
        try:
            assert journal.db.execute(
                "SELECT COUNT(*) FROM agent_reservations WHERE status='held' AND position_id IS NULL"
            ).fetchone()[0] == 1
            assert journal.db.execute(
                "SELECT COUNT(*) FROM strategy_workspace_allocations"
            ).fetchone()[0] == 1
        finally:
            journal.close()
    finally:
        library.close()


def test_browser_session_creation_commits_reviewed_funding_plan(tmp_path: Path, monkeypatch):
    library = StrategyLibrary(tmp_path)
    calls = []

    class Authorizer:
        def require(self, sender, network):
            calls.append(("authorize", sender, network))

    def save_controls(saved_library, directory, instance_id, funding):
        calls.append(("save", saved_library, directory, instance_id, funding))
        return {"instance": {"id": instance_id}, "policy": {"allocation_lamports": 200_000_000}}

    monkeypatch.setattr("pumpwizard_runner.web_api.save_strategy_controls", save_controls)
    try:
        strategy = parse_package({
            "schema": "pumpwizard.strategy-handoff/v1",
            "strategy": {"id": "core-frequency", "label": "Core"},
            "configuration": CONFIG,
        })
        library.import_strategy(strategy)
        funding = {
            "sender": "11111111111111111111111111111111",
            "network": "mainnet-beta",
            "allocation_sol": "0.2",
            "buys_enabled": False,
            "sells_enabled": True,
            "min_trade_sol": "0.005",
            "max_trade_sol": "0.02",
            "max_daily_buy_sol": "0.07",
            "max_trades_per_day": 8,
            "max_daily_loss_sol": "0.02",
            "max_daily_loss_percent": "10",
            "max_trade_loss_percent": "15",
            "max_open_positions": 5,
        }
        result = command(
            library,
            "/api/v1/sessions",
            {"digest": strategy.digest, "label": "Funded plan", "funding": funding, "brief": {
                "goal": "validate-strategy",
                "intent": "Prepare reviewed orders and require a wallet signature for every submission.",
                "paper_budget_usd": 500,
                "max_session_loss_usd": 100,
                "review_after_seconds": 86_400,
                "session_mode": "trade",
                "execution_style": "interactive",
                "profit_management": "user-directed",
            }},
            wallet_authorizer=Authorizer(),
        )
        instance_id = result["instance"]["id"]
        assert calls[0] == ("authorize", funding["sender"], funding["network"])
        assert calls[1][1:4] == (library, tmp_path, instance_id)
        assert calls[1][4] == funding
        assert result["funding"]["policy"]["allocation_lamports"] == 200_000_000
        assert "durable main-wallet allocation" in result["notice"]
        assert result["brief"]["mode"] == "trade-interactive"
        assert result["brief"]["permissions"]["user_signature_each_order"] is True
        assert result["brief"]["permissions"]["automatic_entries"] is False
        assert library.instance(instance_id)["entry_mode"] == "disabled"
    finally:
        library.close()


def test_browser_session_mode_and_funding_must_agree(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package({
            "schema": "pumpwizard.strategy-handoff/v1",
            "strategy": {"id": "core-frequency", "label": "Core"},
            "configuration": CONFIG,
        })
        library.import_strategy(strategy)
        brief = {
            "goal": "collect-evidence",
            "intent": "Retain a bounded history for later comparison.",
            "paper_budget_usd": 500,
            "max_session_loss_usd": 100,
            "review_after_seconds": 86_400,
            "session_mode": "trade",
            "execution_style": "autopilot",
            "profit_management": "winner-handoff",
        }
        with pytest.raises(ValueError, match="require a reviewed wallet allocation"):
            command(library, "/api/v1/sessions", {"digest": strategy.digest, "brief": brief})
        assert payload(library, "/api/v1/sessions", {})["rows"] == []

        monitoring = {
            **brief,
            "session_mode": "monitoring",
            "execution_style": None,
            "profit_management": None,
        }
        with pytest.raises(ValueError, match="cannot reserve wallet funds"):
            command(library, "/api/v1/sessions", {
                "digest": strategy.digest,
                "brief": monitoring,
                "funding": {},
            })
        assert payload(library, "/api/v1/sessions", {})["rows"] == []
    finally:
        library.close()


def test_browser_session_management_renames_and_safely_removes_active_session(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package({"schema": "pumpwizard.strategy-handoff/v1", "strategy": {"id": "core-frequency", "label": "Core"}, "configuration": CONFIG})
        library.import_strategy(strategy)
        ident = library.create_instance(strategy.digest)
        library.set_tracking(ident, True)
        library.record_monitor_run(
            ident, started_at=100, finished_at=101, status="completed",
            observations=1, decisions=0, detail="retained evidence",
        )

        renamed = command(library, f"/api/v1/sessions/{ident}/edit", {"label": "  Renamed monitor  "})
        assert renamed["instance"]["label"] == "Renamed monitor"
        assert payload(library, f"/api/v1/sessions/{ident}", {})["management"]["removal_blocks"] == []
        with pytest.raises(ValueError, match="Confirm the exact"):
            command(library, f"/api/v1/sessions/{ident}/remove", {"confirm": "wrong", "release_allocation": False})

        removed = command(library, f"/api/v1/sessions/{ident}/remove", {"confirm": ident, "release_allocation": False})
        assert removed["removed"] is True
        assert payload(library, "/api/v1/sessions", {})["rows"] == []
        assert workspace(library)["sessions"] == 0
        assert library.summary()["instances"] == 0
        assert library._connection.execute(
            "SELECT enabled FROM monitor_settings WHERE instance_id=?", (ident,)
        ).fetchone()[0] == 0
        assert library._connection.execute(
            "SELECT detail FROM monitor_runs WHERE instance_id=?", (ident,)
        ).fetchone()[0] == "retained evidence"
        with pytest.raises(ValueError, match="Unknown strategy instance"):
            library.instance(ident)
    finally:
        library.close()


def test_browser_session_removal_reports_local_positions_and_wallet_allocations(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package({"schema": "pumpwizard.strategy-handoff/v1", "strategy": {"id": "core-frequency", "label": "Core"}, "configuration": CONFIG})
        library.import_strategy(strategy)
        local_position = library.create_instance(strategy.digest, label="Local position")
        library.record_position(local_position, "MintABC", "ABC", quantity=1, total_cost_usd=1, opened_at=100)
        local_blocks = payload(library, f"/api/v1/sessions/{local_position}", {})["management"]["removal_blocks"]
        assert any("open local position" in block for block in local_blocks)
        with pytest.raises(ValueError, match="open local position"):
            command(library, f"/api/v1/sessions/{local_position}/remove", {"confirm": local_position, "release_allocation": False})

        allocated = library.create_instance(strategy.digest, label="Allocated")
        journal = TradeJournal(tmp_path)
        try:
            with journal.db:
                journal.db.execute(
                    "INSERT INTO agent_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("hold", allocated, "genesis", "sender", "1000", None, None, "0", 100, None, "held"),
                )
            management = payload(library, f"/api/v1/sessions/{allocated}", {})["management"]
            assert management == {"removal_blocks": [], "allocation_lamports": "1000"}
            with pytest.raises(ValueError, match="Confirm release"):
                command(library, f"/api/v1/sessions/{allocated}/remove", {"confirm": allocated, "release_allocation": False})
            removed = command(
                library, f"/api/v1/sessions/{allocated}/remove",
                {"confirm": allocated, "release_allocation": True},
            )
            assert removed["released_allocation_lamports"] == "1000"
            assert journal.db.execute(
                "SELECT status FROM agent_reservations WHERE id='hold'"
            ).fetchone()[0] == "released"
        finally:
            journal.close()
    finally:
        library.close()


def test_browser_one_time_monitor_scan_uses_existing_scheduler_only_after_tracking_starts(tmp_path: Path):
    class Scheduler:
        def run_cycle(self, received):
            assert received is library
            assert library.tracking_settings(instance_id)['next_at'] == 0
            return 1

    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package({"schema": "pumpwizard.strategy-handoff/v1", "strategy": {"id": "core-frequency", "label": "Core"}, "configuration": CONFIG})
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        with pytest.raises(ValueError, match="Start local monitoring"):
            run_monitor_scan(library, instance_id, Scheduler())
        library.set_tracking(instance_id, True)
        with library._connection:
            library._connection.execute('UPDATE monitor_settings SET next_at=? WHERE instance_id=?', (time.time()+60, instance_id))
        result = run_monitor_scan(library, instance_id, Scheduler())
        assert result["completed"] == 1
        assert "sign" in result["notice"].lower()
    finally:
        library.close()


def test_run_monitor_starts_background_scanning_without_authorizing_trades(tmp_path: Path):
    from pumpwizard_runner.background_service import BackgroundServiceEngine

    library = StrategyLibrary(tmp_path)
    engine = BackgroundServiceEngine(tmp_path)
    # Use the real durable job configuration without starting a network worker.
    from pumpwizard_runner.background_service import BackgroundServiceStore
    engine.store = BackgroundServiceStore(tmp_path)
    try:
        strategy = parse_package({'schema': 'pumpwizard.strategy-handoff/v1',
                                  'strategy': {'id': 'scan-start', 'label': 'Scan start'},
                                  'configuration': CONFIG})
        library.import_strategy(strategy)
        ident = library.create_instance(strategy.digest)
        assert not engine.store.job()['enabled']
        command(library, f'/api/v1/sessions/{ident}/tracking', {'enabled': True}, background_service=engine)
        assert engine.store.job()['enabled']
        assert engine.wake_event.is_set()
        assert library.tracking_settings(ident)['enabled']
        assert engine._blocked_signer.state()['signer_state'] == 'locked'
        assert library.instance(ident)['entry_mode'] == 'disabled'
        command(library, f'/api/v1/sessions/{ident}/tracking', {'enabled': False}, background_service=engine)
        assert not library.tracking_settings(ident)['enabled']
        assert engine.store.job()['enabled']  # Other sessions/exits may still need scans.
    finally:
        engine.store.close()
        library.close()


def test_browser_creates_a_local_research_draft_through_existing_package_validation(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        result = command(library, "/api/v1/strategies/draft", {
            "label": "Browser research draft", "identifier": "browser-research-draft",
            "entry_style": "momentum", "entry_cap_min": 10_000, "entry_cap_max": 25_000,
            "order_usd": 10, "budget_usd": 500,
        })
        assert result["imported"] is True
        assert result["strategy"]["schema_name"] == "pumpwizard.strategy-handoff/v1"
        assert "reserve funds" in result["notice"]
        assert library.rows()[0]["strategy_id"] == "browser-research-draft"
    finally:
        library.close()


def test_browser_strategy_goal_resolves_to_existing_package_rules(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        result = command(library, "/api/v1/strategies/draft", {
            "label": "Squeeze watch", "identifier": "squeeze-watch",
            "strategy_goal": "squeeze-watch", "entry_style": "squeeze",
            "entry_cap_min": 25_000, "entry_cap_max": 250_000,
            "order_usd": 10, "budget_usd": 500,
        })
        package = library.strategy(result["strategy"]["digest"])["package"]
        configuration = package["configuration"]
        assert configuration["entry_style"] == "squeeze"
        assert configuration["observation_seconds"] == 300
        assert configuration["max_hold_seconds"] == 21_600
        assert configuration["targets"][0] == {
            "market_cap_usd": 287_500,
            "initial_fraction": .4,
        }
        assert "strategy_goal" not in package
    finally:
        library.close()


@pytest.mark.parametrize(("goal", "style", "observation", "maximum_hold"), (
    ("day-trader", "momentum", 60, 14_400),
    ("long-hold", "slow_burn", 1_800, 604_800),
    ("unicorn-hunter", "persistent", 900, 864_000),
))
def test_browser_each_guided_strategy_goal_uses_supported_existing_rules(
        tmp_path: Path, goal: str, style: str, observation: int, maximum_hold: int):
    library = StrategyLibrary(tmp_path)
    try:
        result = command(library, "/api/v1/strategies/draft", {
            "label": goal, "identifier": goal, "strategy_goal": goal, "entry_style": style,
            "entry_cap_min": 10_000, "entry_cap_max": 250_000,
            "order_usd": 10, "budget_usd": 500,
        })
        package = library.strategy(result["strategy"]["digest"])["package"]
        assert package["configuration"]["entry_style"] == style
        assert package["configuration"]["observation_seconds"] == observation
        assert package["configuration"]["max_hold_seconds"] == maximum_hold
        assert parse_package(package).compatibility == "monitoring-ready"
    finally:
        library.close()


def test_browser_market_refresh_is_bounded_public_data_only(tmp_path: Path):
    class Source:
        def collect(self, minimum, maximum, limit):
            assert (minimum, maximum, limit) == (5_000, 10_000_000, 100)
            return []

    library = StrategyLibrary(tmp_path)
    try:
        result = refresh_market(library, Source())
        assert result["collected"] == 0
        assert result["refreshed"] is True
        assert "wallet" in result["notice"].lower()
    finally:
        library.close()


def test_browser_market_refresh_throttles_duplicate_provider_reads(tmp_path: Path):
    class Source:
        calls = 0

        def collect(self, minimum, maximum, limit):
            self.calls += 1
            return []

    source = Source()
    library = StrategyLibrary(tmp_path)
    try:
        first = refresh_market(library, source, minimum_interval_seconds=30)
        second = refresh_market(library, source, minimum_interval_seconds=30)
        assert source.calls == 1
        assert first["refreshed"] is True
        assert second["refreshed"] is False
        assert 1 <= second["retry_after_seconds"] <= 30
    finally:
        library.close()


def test_browser_market_refresh_replaces_five_minute_snapshot_and_pages_history(tmp_path: Path):
    class Source:
        def __init__(self, observed_at: float):
            self.observed_at = observed_at

        def collect(self, _minimum, _maximum, _limit):
            return [MarketQuote(
                mint="mint-one", symbol="ONE", observed_at=self.observed_at,
                route="curve", pool="pool-one", market_cap_usd=self.observed_at,
                price_usd=0.1, price_native=0.001, liquidity_usd=5000,
                liquidity_native=50,
            )]

    library = StrategyLibrary(tmp_path)
    try:
        refresh_market(library, Source(1_000))
        refresh_market(library, Source(1_100))
        refresh_market(library, Source(1_301))
        assert [row["observed_at"] for row in library.coin_history("mint-one")] == [1_301, 1_100]
        first = payload(library, "/api/v1/coins/mint-one", {"limit": ["1"]})
        assert [row["observed_at"] for row in first["history"]] == [1_301]
        assert first["next_before"] == 1_301
        earlier = payload(library, "/api/v1/coins/mint-one", {"limit": ["1"], "before": ["1301"]})
        assert [row["observed_at"] for row in earlier["history"]] == [1_100]
        assert earlier["next_before"] is None
    finally:
        library.close()


def test_market_history_compaction_keeps_latest_hourly_and_daily_samples(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    current = 10_000_000.0
    eight_days_ago = current - 8 * 86_400
    ninety_one_days_ago = current - 91 * 86_400
    try:
        with library._connection:
            for observed_at in (eight_days_ago, eight_days_ago + 10, ninety_one_days_ago, ninety_one_days_ago + 10):
                library._connection.execute(
                    "INSERT INTO market_watch_samples(mint,symbol,observed_at,price_usd,market_cap_usd,liquidity_usd) VALUES(?,?,?,?,?,?)",
                    ("mint-one", "ONE", observed_at, "0.1", "10000", "5000"),
                )
        assert library.compact_market_watch_history(now=current, minimum_interval_seconds=0) == 2
        assert [row["observed_at"] for row in library.coin_history("mint-one")] == [eight_days_ago + 10, ninety_one_days_ago + 10]
    finally:
        library.close()


def test_browser_runs_only_the_existing_bounded_virtual_portfolio_replay(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        result = run_portfolio_replay(library, "browser-replay", example_recording())
        assert result["replay"]["id"] == "browser-replay"
        assert result["replay"]["checkpoint"]["state"]["last_at"] is not None
        assert "wallet" in result["notice"].lower()
        rows = payload(library, "/api/v1/portfolio-replays", {})["rows"]
        assert rows[0]["id"] == "browser-replay"
    finally:
        library.close()


def test_loopback_api_requires_the_launch_token_and_browser_origin(tmp_path: Path):
    token = "browser-test-token-which-is-long-enough"
    server = serve(tmp_path, token, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/v1/workspace"
    try:
        try:
            urlopen(url)
        except HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("The browser API accepted an unauthenticated request")
        with pytest.raises(HTTPError) as inventory_rejected:
            urlopen(url.replace("/workspace", "/wallet/inventory"))
        assert inventory_rejected.value.code == 403
        request = Request(url, headers={"Origin": "http://127.0.0.1:5173", "X-PumpWizard-Browser-Token": token})
        with urlopen(request) as response:
            body = json.loads(response.read())
        assert body["schema"] == "pumpwizard.runner-web/v1"
        assert body["sessions"] == 0
        capability_request = Request(
            url.replace("/workspace", "/capabilities"),
            headers={
                "Origin": "http://127.0.0.1:5173",
                "X-PumpWizard-Browser-Token": token,
            },
        )
        with urlopen(capability_request) as response:
            capabilities = json.loads(response.read())["capabilities"]
        assert capabilities["authentication"]["state"] == "authenticated"
        assert capabilities["authentication"]["wallet_identity"] is False
        assert capabilities["research"]["state"] == "available"
        assert capabilities["service"]["lifecycle"] == "browser_owned"
        assert capabilities["wallet_actions"]["network_scan"] == "not_started"
        assert not any(capabilities["read_effects"].values())
        options = Request(url, method="OPTIONS", headers={"Origin": "http://127.0.0.1:5173"})
        with urlopen(options) as response:
            assert response.status == 204
            assert response.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
            assert response.headers["Access-Control-Allow-Headers"] == "Content-Type, X-PumpWizard-Browser-Token"
        try:
            urlopen(Request(url, data=b"{}", method="POST"))
        except HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("The browser API accepted an unauthenticated command")
        untrusted = Request(url, headers={"Origin": "https://untrusted.example", "X-PumpWizard-Browser-Token": token})
        try:
            urlopen(untrusted)
        except HTTPError as exc:
            assert exc.code == 403
            assert exc.headers.get("Access-Control-Allow-Origin") is None
        else:
            raise AssertionError("The browser API accepted an untrusted origin")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_authenticated_clean_profile_reaches_research_without_wallet_or_provider(
        tmp_path: Path):
    token = "browser-test-token-which-is-long-enough"
    server = serve(tmp_path, token, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {
        "Content-Type": "application/json",
        "Origin": "http://127.0.0.1:5173",
        "X-PumpWizard-Browser-Token": token,
    }
    try:
        with urlopen(Request(base + "/api/v1/capabilities", headers=headers)) as response:
            capabilities = json.loads(response.read())["capabilities"]
        assert capabilities["research"]["wallet_required"] is False
        assert capabilities["research"]["ai_provider_required"] is False

        create = Request(
            base + "/api/v1/session-workspaces",
            data=json.dumps({"mode": "monitoring", "label": "First research"}).encode(),
            method="POST", headers=headers,
        )
        with urlopen(create) as response:
            created = json.loads(response.read())
        assert created["session"]["mode"] == "monitoring"
        assert created["session"]["strategy_count"] == 0
        assert "no wallet allocation" in created["notice"].lower()

        with urlopen(Request(base + "/api/v1/research", headers=headers)) as response:
            research = json.loads(response.read())
        assert research["authority"] == {
            "wallet": False, "signer": False, "orders": False,
        }
        assert research["campaigns"] == []
        with urlopen(Request(base + "/api/v1/wallet-state", headers=headers)) as response:
            wallet = json.loads(response.read())
        assert wallet["browser_wallet_connected"] is False
        assert wallet["wallets"] == []
        assert wallet["policies"] == []
        assert wallet["transactions"] == []
        assert wallet["signing_sessions"] == []
        assert not (tmp_path / "service-wallets").exists()
        assert not (tmp_path / "ai-credentials").exists()

        inspect = Request(
            base + "/api/v1/wallet/inspect", data=b"{}", method="POST", headers=headers,
        )
        with pytest.raises(HTTPError) as rejected:
            urlopen(inspect)
        assert rejected.value.code == 400
        error = json.loads(rejected.value.read())
        assert error["typed_error"]["code"] == "authorization_invalid"
        with urlopen(Request(base + "/api/v1/wallet-state", headers=headers)) as response:
            wallet_after = json.loads(response.read())
        assert wallet_after["wallets"] == []
        assert wallet_after["transactions"] == []
        assert wallet_after["signing_sessions"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_loopback_api_runs_only_an_explicit_offline_evaluation(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package({
            "schema": "pumpwizard.strategy-handoff/v1",
            "strategy": {"id": "core-frequency", "label": "Core"},
            "configuration": CONFIG,
        })
        library.import_strategy(strategy)
    finally:
        library.close()
    token = "browser-test-token-which-is-long-enough"
    server = serve(tmp_path, token, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/v1/evaluations/{strategy.digest}/run"
    try:
        request = Request(
            url,
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:5173",
                "X-PumpWizard-Browser-Token": token,
            },
        )
        with urlopen(request) as response:
            result = json.loads(response.read())
        assert result["evaluation"]["strategy_digest"] == strategy.digest
        assert result["evaluation"]["submission_enabled"] is False
        assert result["evaluation"]["funded_acceptance"] is False
        assert "wallet" in result["notice"].lower()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_loopback_evidence_assistant_returns_typed_4xx_without_closing_connection(
        tmp_path: Path):
    token = "browser-test-token-which-is-long-enough"
    server = serve(tmp_path, token, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {
        "Content-Type": "application/json",
        "Origin": "http://127.0.0.1:5173",
        "X-PumpWizard-Browser-Token": token,
    }
    try:
        request = Request(
            base + "/api/v1/assistant/evidence",
            data=b"{}", method="POST", headers=headers,
        )
        with pytest.raises(HTTPError) as rejected:
            urlopen(request)
        assert rejected.value.code == 422
        body = json.loads(rejected.value.read())
        assert body["typed_error"] == {
            "schema": "pumpwizard.error/v1",
            "code": "assistant_request_invalid",
            "message": "Evidence read requires selector and question fields",
            "retryable": False,
            "stage": "evidence_assistant",
            "request_id": None,
            "details": {},
        }
        with urlopen(Request(
            base + "/api/v1/assistant",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "X-PumpWizard-Browser-Token": token,
            },
        )) as response:
            assert json.loads(response.read())["configured"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(("bind_host", "browser_host"), (("127.0.0.1", "127.0.0.1"), ("localhost", "localhost")))
def test_same_origin_static_browser_uses_an_httponly_session_cookie(tmp_path: Path, bind_host: str, browser_host: str):
    web_root = tmp_path / "browser-dist"
    web_root.mkdir()
    (web_root / "index.html").write_text("<!doctype html><title>Runner</title>", encoding="utf-8")
    (web_root / "asset.js").write_text("console.log('local')", encoding="utf-8")
    token = "browser-test-token-which-is-long-enough"
    server = serve(tmp_path, token, host=bind_host, port=0, web_root=web_root)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{browser_host}:{server.server_address[1]}"
    browser = build_opener(HTTPCookieProcessor(CookieJar()))
    try:
        with browser.open(f"{base}/") as response:
            assert response.status == 200
            assert "HttpOnly" in response.headers["Set-Cookie"]
            assert "SameSite=Strict" in response.headers["Set-Cookie"]
            assert "pumpwizard_browser" not in response.read().decode()
        with browser.open(Request(f"{base}/api/v1/workspace", headers={"Origin": base})) as response:
            body = json.loads(response.read())
        assert body["schema"] == "pumpwizard.runner-web/v1"
        try:
            browser.open(Request(f"{base}/api/v1/workspace"))
        except HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("The static API accepted its cookie without same-origin request metadata")
        with browser.open(Request(f"{base}/api/v1/workspace", headers={"Sec-Fetch-Site": "same-origin"})) as response:
            assert json.loads(response.read())["schema"] == "pumpwizard.runner-web/v1"
        try:
            urlopen(Request(f"{base}/api/v1/workspace", headers={"Origin": base}))
        except HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("The same-origin API accepted a request without its HttpOnly launch cookie")
        with browser.open(f"{base}/asset.js") as response:
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
            assert "script-src 'self'" in response.headers["Content-Security-Policy"]
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert "camera=()" in response.headers["Permissions-Policy"]
            assert response.read() == b"console.log('local')"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_static_browser_rejects_a_mismatched_build_identity(tmp_path: Path):
    web_root = tmp_path / "browser-dist"
    web_root.mkdir()
    (web_root / "index.html").write_text("<!doctype html><title>Runner</title>", encoding="utf-8")
    (web_root / "build-identity.json").write_text(
        json.dumps({
            "schema": "pumpwizard.build-identity/v1",
            "build_id": "stale-build",
            "version": "0.0.0.stale",
            "source_checkpoint": "stale",
            "packaged_at": "stale",
            "package_kind": "stale",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match this Runner executable"):
        serve(tmp_path, "browser-test-token-which-is-long-enough", port=0, web_root=web_root)


def test_browser_assistant_previews_before_contacting_a_provider(tmp_path: Path):
    assistant = BrowserAssistant(tmp_path)
    assert assistant.overview()["configured"] is False
    profile = assistant.configure({"provider": "openai", "model": "gpt-5.6-terra", "api_key": "test-key", "endpoint": ""})
    assert profile["key_available"] is True
    assert "test-key" not in json.dumps(profile)
    conversation = assistant.create()["conversation"]
    prepared = assistant.prepare({"conversation": conversation, "message": "Compare a local strategy.", "max_output": 512})
    assert prepared["request_id"]
    assert prepared["estimated_input"] > 0
    messages = assistant.messages(conversation)["messages"]
    assert messages[-1]["role"] == "user"
    assert assistant.decline(prepared["request_id"])["declined"] is True


def test_browser_evidence_routes_return_local_context_without_provider(tmp_path: Path):
    assistant = BrowserAssistant(tmp_path)
    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package({
            "schema": "pumpwizard.strategy-handoff/v1",
            "strategy": {"id": "assistant-route", "label": "Assistant route"},
            "configuration": CONFIG,
        })
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        position_id = library.record_position(
            instance_id, "EvidenceMint", "EVID", quantity=2,
            total_cost_usd=1, opened_at=time.time(),
        )
    finally:
        library.close()
    result = assistant_command(assistant, "/api/v1/assistant/evidence", {
        "selector": {
            "schema": "pumpwizard.evidence-selector/v1",
            "kind": "holding",
            "id": position_id,
            "parent_id": instance_id,
        },
        "question": "valuation_reliability",
    })
    assert result["schema"] == "pumpwizard.evidence-assistant-view/v1"
    assert result["explanation"]["provider_used"] is False
    assert result["ai"]["available"] is False
    assert assistant.pending == {}


def test_browser_evidence_prepare_route_requires_configured_provider(tmp_path: Path):
    assistant = BrowserAssistant(tmp_path)
    with pytest.raises(AgentHubError, match="assistant API key"):
        assistant_command(assistant, "/api/v1/assistant/evidence/prepare", {
            "conversation": None,
            "message": "Explain only supported facts.",
            "max_output": 400,
            "selector": {
                "schema": "pumpwizard.evidence-selector/v1",
                "kind": "holding",
                "id": "missing-holding",
                "parent_id": "missing-session",
            },
            "question": "valuation_reliability",
            "context_id": "missing-context",
        })


@pytest.mark.parametrize(("provider", "model", "label", "endpoint"), [
    ("anthropic", "claude-sonnet-5", "Anthropic Claude", "https://api.anthropic.com/v1/messages"),
    ("deepseek", "deepseek-flash", "DeepSeek", "https://api.deepseek.com/chat/completions"),
])
def test_browser_assistant_has_official_claude_and_deepseek_presets(
        tmp_path: Path, provider: str, model: str, label: str, endpoint: str):
    assistant = BrowserAssistant(tmp_path / provider)
    profile = assistant.configure({"provider": provider, "model": model, "api_key": "test-key", "endpoint": ""})
    assert profile["label"] == label
    assert profile["endpoint"] == endpoint
    assert profile["model"] == model
    assert profile["key_available"] is True
    changed = assistant.configure({"provider": provider, "model": model + "-alternate", "api_key": "", "endpoint": ""})
    assert changed["model"] == model + "-alternate"


def test_browser_assistant_requires_a_new_key_when_provider_changes(tmp_path: Path):
    assistant = BrowserAssistant(tmp_path)
    assistant.configure({"provider": "deepseek", "model": "deepseek-flash", "api_key": "deepseek-key", "endpoint": ""})
    with pytest.raises(AgentHubError, match="API key"):
        assistant.configure({"provider": "anthropic", "model": "claude-sonnet-5", "api_key": "", "endpoint": ""})


def test_browser_can_run_the_bounded_offline_evaluation_only_for_an_imported_strategy(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package({
            "schema": "pumpwizard.strategy-handoff/v1",
            "strategy": {"id": "core-frequency", "label": "Core"},
            "configuration": CONFIG,
        })
        library.import_strategy(strategy)
        result = command(library, f"/api/v1/evaluations/{strategy.digest}/run", {})
        assert result["evaluation"]["strategy_digest"] == strategy.digest
        assert result["evaluation"]["submission_enabled"] is False
        assert result["evaluation"]["funded_acceptance"] is False
        assert "wallet" in result["notice"].lower()
        rows = payload(library, "/api/v1/evaluations", {})["rows"]
        assert rows[0]["report"]["id"] == result["evaluation"]["id"]
        assert "recording" not in rows[0]["report"]
        assert "strategy_package" not in rows[0]["report"]
    finally:
        library.close()


def test_browser_evidence_views_do_not_expose_worker_or_shadow_detail(tmp_path: Path):
    risk = sqlite3.connect(tmp_path / "agent-risk.sqlite")
    try:
        risk.execute("CREATE TABLE shadow_reports(id INTEGER, at REAL, agent_id TEXT, payload TEXT)")
        risk.execute(
            "INSERT INTO shadow_reports VALUES (1, 1, 'agent-1', ?)",
            (json.dumps({"results": [{"unsigned_transaction": "must-not-leave-store"}]}),),
        )
        risk.commit()
    finally:
        risk.close()
    rehearsal = sqlite3.connect(tmp_path / "execution-worker.sqlite")
    try:
        rehearsal.execute("CREATE TABLE worker_runs(id TEXT, scope TEXT, config TEXT, status TEXT, detail TEXT, started_at REAL, updated_at REAL, next_at REAL, failures INTEGER, cycles INTEGER, cursors TEXT)")
        rehearsal.execute("CREATE TABLE worker_leases(scope TEXT, owner TEXT, heartbeat REAL)")
        rehearsal.execute("CREATE TABLE worker_cycles(id INTEGER, scope TEXT, run_id TEXT, at REAL, payload TEXT)")
        rehearsal.execute(
            "INSERT INTO worker_runs VALUES ('run-1', 'scope-1', ?, 'stopped', 'saved', 1, 1, NULL, 0, 2, '{}')",
            (json.dumps({"agent_id": "agent-1", "sender": "public-address", "mode": "unsigned"}),),
        )
        rehearsal.execute(
            "INSERT INTO worker_cycles VALUES (1, 'scope-1', 'run-1', 1, ?)",
            (json.dumps({"results": [{"unsigned_transaction": "must-not-leave-store"}]}),),
        )
        rehearsal.commit()
    finally:
        rehearsal.close()
    shadow_rows, worker_rows = shadow(tmp_path), worker(tmp_path)
    assert shadow_rows == [{"id": 1, "at": 1.0, "agent_id": "agent-1", "result_count": 1, "submission_enabled": False}]
    assert worker_rows[0]["mode"] == "unsigned"
    serialized = json.dumps({"shadow": shadow_rows, "worker": worker_rows})
    assert "unsigned_transaction" not in serialized
    assert "public-address" not in serialized
    assert "reports" not in worker_rows[0]
    assert "detail" not in worker_rows[0]


def test_browser_wallet_state_returns_saved_portfolio_headlines_only(tmp_path: Path):
    db = sqlite3.connect(tmp_path / "portfolio-risk.sqlite")
    try:
        db.execute("CREATE TABLE portfolios(genesis TEXT, sender TEXT, peak TEXT, payload TEXT)")
        db.execute(
            "INSERT INTO portfolios VALUES ('mainnet', 'public-address', '20', ?)",
            (json.dumps({
                "status": "ready", "started_at": 123, "net_liquidation": 100,
                "total_pnl": 20, "unrealized_pnl": 10, "drawdown": 0,
                "basis": "must-not-leave-store", "quotes": [{"route": "must-not-leave-store"}],
            }),),
        )
        db.commit()
    finally:
        db.close()
    result = wallet_state(tmp_path)
    assert result["portfolio_valuations"] == [{
        "genesis": "mainnet", "sender": "public-address", "status": "ready", "started_at": 123,
        "net_liquidation": 100, "total_pnl": 20, "unrealized_pnl": 10, "drawdown": 0,
    }]
    serialized = json.dumps(result)
    assert "must-not-leave-store" not in serialized
    assert "quotes" not in serialized


@pytest.mark.parametrize("activity", ["observations", "paper"])
def test_monitor_activity_is_persisted_without_authority(tmp_path: Path, activity):
    with closing(StrategyLibrary(tmp_path)) as library:
        created = command(library, "/api/v1/session-workspaces", {
            "mode": "monitoring", "monitor_activity": activity,
        })["session"]
        assert created["plan"] == {"monitor_activity": activity}
        assert created["allocation_lamports"] == "0"
        assert created["wallet_sender"] is None
        assert created["strategy_count"] == 0
        assert library.session_workspace(created["id"])["plan"] == created["plan"]


@pytest.mark.parametrize("activity", [None, "trade", {"automatic_entries": True}, ["paper"]])
def test_monitor_activity_rejects_unknown_choices(tmp_path: Path, activity):
    with closing(StrategyLibrary(tmp_path)) as library:
        with pytest.raises(ValueError, match="Choose observations or paper trading"):
            command(library, "/api/v1/session-workspaces", {
                "mode": "monitoring", "monitor_activity": activity,
            })


@pytest.mark.parametrize("plan", [
    {"monitor_activity": "paper", "automatic_entries": True},
    {"monitor_activity": "trade"}, {"allocation_sol": "1"},
])
def test_monitor_activity_cannot_carry_trading_plan(tmp_path: Path, plan):
    with closing(StrategyLibrary(tmp_path)) as library:
        with pytest.raises(ValueError, match="Monitoring sessions accept only"):
            library.create_session_workspace("monitoring", plan=plan)


def test_public_wallet_inventory_does_not_grant_wallet_sign_in(tmp_path, monkeypatch):
    calls = []
    def public_inventory(directory, library, sender, network, **kwargs):
        calls.append((sender, network, kwargs["refresh"]))
        return {"sender": sender, "network": network, "balance": "42", "assets": []}
    monkeypatch.setattr("pumpwizard_runner.web_api.wallet_portfolio.inventory", public_inventory)
    library = StrategyLibrary(tmp_path)
    try:
        query = {"sender": ["public-address"], "network": ["mainnet-beta"], "refresh": ["true"]}
        result = payload(library, "/api/v1/wallet/inventory", query)
        assert result["balance"] == "42"
        assert calls == [("public-address", "mainnet-beta", True)]
        with pytest.raises(TransferError, match="sign-in"):
            payload(library, "/api/v1/wallet/portfolio", query)
    finally:
        library.close()


def test_session_activity_pages_durable_decisions_without_replaying_or_losing_backdated_rows(tmp_path):
    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package({"schema": "pumpwizard.strategy-handoff/v1", "strategy": {"id": "core-frequency", "label": "Core"}, "configuration": CONFIG})
        library.import_strategy(strategy)
        ident = library.create_instance(strategy.digest, "Activity strategy")
        first = payload(library, "/api/v1/session-activity", {})
        assert first["rows"] == [] and first["cursor"] == 0
        records = [dict(at=time.time() - i, kind="entry_recommended" if i % 2 else "entry_blocked", mint=f"coin-{i}", reason="strategy_entry_signal", payload={"symbol": f"COIN{i}"}) for i in range(205)]
        library.record_decisions(ident, records)
        library.record_decisions(ident, records)
        page = payload(library, "/api/v1/session-activity", {"after": ["0"]})
        assert len(page["rows"]) == 100 and page["has_more"]
        assert page["rows"][0]["session_id"] == ident
        assert page["rows"][0]["strategy"] == "Activity strategy"
        ids = [row["id"] for row in page["rows"]]
        while page["has_more"]:
            page = payload(library, "/api/v1/session-activity", {"after": [str(page["cursor"])]})
            ids.extend(row["id"] for row in page["rows"])
        assert len(ids) == len(set(ids)) == 205
        assert payload(library, "/api/v1/session-activity", {"after": [str(page["cursor"])]})["rows"] == []
        recent = payload(library, "/api/v1/session-activity", {})
        assert len(recent["rows"]) == 100 and not recent["has_more"]
        assert [row["id"] for row in recent["rows"]] == ids[-100:]
        library.record_decisions(ident, [dict(at=1, kind="entry_blocked", mint="late-coin", reason="stale_quote", payload={})])
        delayed = payload(library, "/api/v1/session-activity", {"after": [str(page["cursor"])]})
        assert len(delayed["rows"]) == 1 and delayed["rows"][0]["at"] == 1
        for cursor in ("-1", "bad", str(2**64)):
            with pytest.raises(ValueError):
                payload(library, "/api/v1/session-activity", {"after": [cursor]})
    finally:
        library.close()
