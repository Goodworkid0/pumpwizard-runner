"""Serve the built browser client with an isolated real Runner library for E2E."""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import struct
import os
from pathlib import Path
import pickle
import shutil
import signal
import sys
import tempfile
import threading
import time

from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.market import MarketDataError
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.market_history import MarketHistoryService, RESOLUTION_SECONDS
from pumpwizard_runner.web_api import serve
from pumpwizard_runner import web_api, web_trading
from pumpwizard_runner.main_wallet_autopilot import RuntimeManager
from pumpwizard_runner.web_main_wallet_autopilot import MainWalletAutopilotController
from pumpwizard_runner.background_service import BackgroundServiceEngine
from pumpwizard_runner.service_recorder import ServiceEvidenceRecorder
from pumpwizard_runner.service_signing import ServiceRuntimeManager, ServiceSigningController
from browser_trade_bank import MINT, SyntheticChain


class FixedMarketSource:
    """Keep automatic browser refreshes deterministic and offline in E2E."""

    def sol_usd_price(self):
        return 100.0

    def collect(self, _minimum: float, _maximum: float, _limit: int = 100, **_kwargs):
        return []

    def token_profiles(self, mints):
        if MINT not in mints:
            return {}
        return {MINT: {
            "metadata": {}, "price_usd": .00005, "price_block_id": None,
            "liquidity_usd": 3_500, "market_cap_usd": 50_000,
            "price_change_24h": 12.5, "volume_change_24h": 8.0,
            "holder_count": 120, "organic_score": 22,
            "organic_score_label": "low", "suspicious": False,
            "top_holders_percent": 61, "created_at": None,
        }}


# Campaign manifests bind the configured production adapter identity.  The E2E
# source is a deterministic offline substitute for that same adapter boundary.
FixedMarketSource.__name__ = "CompositePumpMarketSource"


class FixedHistorySource:
    """Return one bounded historical page without contacting a provider."""

    label = "Public market history"

    def fetch(self, _mint: str, before: int, _limit: int = 1000):
        return [{
            "opened_at": before - (index + 2) * RESOLUTION_SECONDS,
            "open_usd": str(.000008 + index * .0000002),
            "high_usd": str(.000009 + index * .0000002),
            "low_usd": str(.000007 + index * .0000002),
            "close_usd": str(.0000085 + index * .0000002),
            "volume_usd": str(500 + index * 20),
        } for index in range(12)]


def seed_provider_diagnostic_scenarios(data_dir: Path, now: float, mint: str) -> None:
    """Retain real recorder rows for every provider-readiness browser state."""
    recorder = ServiceEvidenceRecorder(data_dir)
    try:
        # Keep UI-only readiness scenarios outside the seeded research dataset's
        # time window so their deliberate failure rows cannot become comparison
        # gaps in an otherwise eligible Batch 4 fixture.
        fixture_period = now - 4 * 60 * 60
        scenarios = (
            ("healthy", True, {}, 1),
            ("empty", False, {"empty_eligible_universe": 1}, 0),
            ("empty", False, {"unavailable_route": 1}, 0),
            ("partial", True, {"unsupported_shape": 1}, 1),
            ("stale", False, {"stale_or_unknown_source_time": 2}, 0),
            ("failing", False, {"request_failure": 2}, 0),
        )
        for offset, (status, ready, rejections, result_count) in enumerate(scenarios, 1):
            started = fixture_period + offset * 10
            finished = started + 0.25
            stages = {
                "fx_requests": 1,
                "discovery_requests": 2,
                "listing_rows": 2 if status not in ("empty", "failing") else 0,
                "unique_listings": 1 if result_count else 0,
                "eligible_candidates": result_count,
                "selected_records": result_count,
                "selected_detail_requests": 0,
                "returned_details": 0,
                "normalized_quotes": result_count,
                "http_requests": 3,
                "usable_discovery_responses": 0 if status == "failing" else 2,
                "response_bytes": 2_048,
            }
            canonical_route_unavailable = bool(rejections.get("unavailable_route"))
            diagnostics = {
                "schema": "pumpwizard.provider-diagnostics/v1",
                "provider": (
                    "CanonicalPumpSwapMarketSource"
                    if canonical_route_unavailable else "CompositePumpMarketSource"
                ),
                "started_at": started,
                "finished_at": finished,
                "status": status,
                "bounds": {
                    "maximum_http_requests": 6,
                    "maximum_elapsed_seconds": 60,
                    "maximum_response_bytes": 12_000_000,
                    "maximum_concurrency": 2,
                },
                "stages": stages,
                "rejections": rejections,
                "provenance": {
                    "provider_hosts": ["fixture.invalid"],
                    "endpoint_classes": ["fixture_listing"],
                    "normalized_routes": ["curve"],
                    "source_timestamp_field": "updated_at",
                    "receipt_timestamp_field": "observed_at",
                    "response_byte_basis": "canonical-decoded-json",
                },
                "readiness": {
                    "ready": ready,
                    "routes": ["curve"] if ready else [],
                    "reasons": (
                        ["canonical_route_unavailable"]
                        if canonical_route_unavailable else [] if ready else [f"fixture_{status}"]
                    ),
                    "first_received_at": finished if result_count else None,
                    "earliest_source_at": started if result_count else None,
                    "latest_source_at": started if result_count else None,
                    "maximum_source_age_seconds": finished - started if result_count else None,
                },
            }
            run_id = recorder.begin_scan(
                minimum_cap=1_000,
                maximum_cap=150_000,
                source="CompositePumpMarketSource",
                limit=3,
                requested_at=started,
                started_at=started,
            )
            quotes = [MarketQuote(
                mint=mint,
                symbol="ALPHA",
                observed_at=finished,
                route="curve",
                pool=mint,
                market_cap_usd=50_000,
                price_usd=.00005,
                price_native=.0000005,
                liquidity_usd=3_500,
                liquidity_native=35,
                source="fixture-provider",
                quote_usd=100,
                source_at=started,
                fx_at=started,
            )] if result_count else []
            if status == "failing":
                recorder.fail_scan(
                    run_id, MarketDataError("Fixture provider request failed"),
                    received_at=finished, diagnostics=diagnostics,
                )
            else:
                recorder.complete_scan(
                    run_id, quotes, received_at=finished, diagnostics=diagnostics,
                )
    finally:
        recorder.close()

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--web-root", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    data_dir = args.data_dir or Path(tempfile.mkdtemp(prefix="pumpwizard-browser-e2e-"))
    checkpoint = data_dir / "synthetic-chain.pickle"
    # Test-only process checkpoint, containing only generated fixture data.
    chain = pickle.loads(checkpoint.read_bytes()) if checkpoint.exists() else SyntheticChain()
    web_trading.rpc_for = chain.rpc
    from pumpwizard_runner import rpc_settings
    from pumpwizard_runner.transfers import GENESIS, TransferError

    class OfflineConnectionProbe:
        def __init__(self, network, url):
            self.url = url

        def genesis(self):
            if 'wrong-network' in self.url:
                raise TransferError('RPC network does not match the selected network')
            return GENESIS['mainnet-beta']

        def call(self, method, params):
            assert method == 'getLatestBlockhash'
            return {'value': {'blockhash': '1' * 32, 'lastValidBlockHeight': 100}}

    rpc_settings.ConnectionProbe = OfflineConnectionProbe
    web_api.CompositePumpMarketSource = FixedMarketSource
    from pumpwizard_runner import wallet_usd_history
    wallet_usd_history._fetch = lambda start, end, interval: {
        at: 100.0 for at in range(start, end, interval)
    }
    library = StrategyLibrary(data_dir)
    now = time.time()
    mint = MINT
    try:
        for index in range(110):
            price = 0.000012 + index * 0.00000035
            library.record_market_watch_quote(MarketQuote(
                mint=mint, symbol="ALPHA", observed_at=now - (109 - index) * 61,
                route="curve", pool=mint, market_cap_usd=12_000 + index * 725,
                price_usd=price, price_native=price / 150, liquidity_usd=3_200 + index * 130,
                liquidity_native=(3_200 + index * 130) / 150, created_at=now - 7_200,
                metadata={
                    "name": "Alpha Coin", "description": "A deterministic token record for the browser acceptance flow.",
                    "creator": "11111111111111111111111111111111", "website": "https://example.com/alpha",
                    "twitter": "https://x.com/example", "image_url": "https://cdn.dexscreener.com/alpha.png",
                    "verified": True, "source": "Pump.fun",
                },
            ))
    finally:
        library.close()
    # Test-only artwork response keeps the browser acceptance suite offline
    # while exercising the same-origin token image path used by production.
    icon = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    web_api.token_image = lambda _library, _mint: ("image/png", icon)
    main_wallet_controller = MainWalletAutopilotController(
        data_dir,
        manager=RuntimeManager(allow_synthetic=True),
        rpc_factory=lambda network: chain.rpc(network, data_dir),
    )
    service_signer = ServiceSigningController(
        data_dir,
        manager=ServiceRuntimeManager(allow_synthetic=True),
        rpc_factory=lambda network: chain.rpc(network, data_dir),
    )
    background_service = BackgroundServiceEngine(
        data_dir,
        port=args.port,
        source_factory=FixedMarketSource,
        signer=service_signer,
    )
    server = serve(
        data_dir,
        "browser-e2e-token-kept-out-of-the-page",
        "127.0.0.1",
        args.port,
        args.web_root,
        MarketHistoryService(FixedHistorySource()),
        main_wallet_controller,
        background_service,
    )
    restart_requested = False
    command = web_api.command
    service_sequence = {
        "phase": "idle", "detail": "", "sent": 0,
        "started_at": None, "released_at": None, "completed_at": None,
    }
    service_sequence_lock = threading.Lock()
    service_sequence_gate = threading.Event()
    first_use_effects = {
        "wallet_scan": 0,
        "provider_request": 0,
        "collection_start": 0,
        "signing_request": 0,
        "submission_request": 0,
    }

    def sequence_snapshot():
        with service_sequence_lock:
            return dict(service_sequence)

    def update_sequence(**changes):
        with service_sequence_lock:
            service_sequence.update(changes)

    def run_service_sequence(sender: str, session_id: str, baseline: int) -> None:
        from pumpwizard_runner.manual_trades import TradeJournal
        from pumpwizard_runner.portfolio import CONFIG
        from test_agent_execution import observe

        def wait_for(check, phase: str, timeout: float = 10.0):
            update_sequence(phase=phase, sent=len(chain.bank(sender).sent))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if check():
                    return
                time.sleep(.05)
            raise AssertionError(f"Timed out during {phase}")

        try:
            bank = chain.bank(sender)
            update_sequence(phase="waiting-for-browser-close", sent=len(bank.sent))
            if not service_sequence_gate.wait(10):
                raise AssertionError("Timed out waiting for the browser-closed release gate")
            update_sequence(phase="released", released_at=time.time(), sent=len(bank.sent))
            lifecycle_library = StrategyLibrary(data_dir)
            try:
                decision_at = time.time()
                observe(lifecycle_library, session_id, decision_at, 115)
                lifecycle_library.record_decisions(session_id, [{
                    "at": decision_at,
                    "kind": "entry_recommended",
                    "mint": MINT,
                    "reason": "strategy_entry_signal",
                    "payload": {},
                }])
            finally:
                lifecycle_library.close()
            wait_for(lambda: len(bank.sent) >= baseline + 1, "waiting-for-entry")
            chain.control(sender, {"finalize": True})

            def entry_finalized():
                journal = TradeJournal(data_dir)
                try:
                    trades = journal.trades(sender)
                    return any(row["status"] == "finalized" for row in trades)
                finally:
                    journal.close()

            wait_for(entry_finalized, "waiting-for-entry-reconciliation")
            journal = TradeJournal(data_dir)
            try:
                with journal.db:
                    journal.db.execute(
                        "UPDATE trade_positions SET opened_at=? WHERE sender=? AND status='open'",
                        (time.time() - CONFIG["max_hold_seconds"] - 1, sender),
                    )
            finally:
                journal.close()
            lifecycle_library = StrategyLibrary(data_dir)
            try:
                observe(lifecycle_library, session_id, time.time(), 115)
            finally:
                lifecycle_library.close()
            wait_for(lambda: len(bank.sent) >= baseline + 2, "waiting-for-exit")
            chain.control(sender, {"finalize": True})

            def lifecycle_finalized():
                journal = TradeJournal(data_dir)
                try:
                    trades = journal.trades(sender)
                    completed = [row for row in trades if row["status"] == "finalized"]
                    open_positions = journal.db.execute(
                        "SELECT COUNT(*) FROM trade_positions WHERE sender=? AND status='open'",
                        (sender,),
                    ).fetchone()[0]
                    return len(completed) >= 2 and open_positions == 0
                finally:
                    journal.close()

            wait_for(lifecycle_finalized, "waiting-for-exit-reconciliation")
            update_sequence(
                phase="completed",
                detail="synthetic entry and full exit finalized",
                sent=len(bank.sent),
                completed_at=time.time(),
            )
        except Exception as exc:
            update_sequence(phase="failed", detail=f"{type(exc).__name__}: {exc}", sent=len(chain.bank(sender).sent))

    def test_command(library, path, body, *context, **options):
        nonlocal restart_requested
        request_body = body if isinstance(body, dict) else {}
        if path == '/api/v1/test-service-runtime':
            if set(body) != {'mode'} or body['mode'] not in {'wallet-approved', 'synthetic'}:
                raise ValueError('Choose the isolated service test runtime')
            if service_signer.state()['signer_state'] != 'locked':
                raise ValueError('Lock the isolated test signer before replacing its runtime')
            # Exercise the normal wallet-approved gate with the offline trade bank.
            # This endpoint exists only in this test server, never in the app.
            service_signer.manager = ServiceRuntimeManager(allow_synthetic=body['mode'] == 'synthetic')
            return {'trading': service_signer.state()}
        if path == '/api/v1/test-batch7-read-only-counters':
            if body != {}:
                raise ValueError('Batch 7 read-only counters accept no fields')
            return {
                'schema': 'pumpwizard.batch7-read-only-counters/v1',
                **first_use_effects,
                'service_signer_state': background_service.status()['trading']['signer_state'],
            }
        if path == '/api/v1/wallet/inspect':
            first_use_effects['wallet_scan'] += 1
        if path == '/api/v1/market/refresh' or (
                path.startswith('/api/v1/sessions/') and path.endswith('/scan')):
            first_use_effects['provider_request'] += 1
        if ((path.startswith('/api/v1/sessions/') and path.endswith('/tracking')
                and request_body.get('enabled') is True)
                or path == '/api/v1/research/campaigns'
                or (path == '/api/v1/service/jobs/strategy-monitor'
                    and request_body.get('enabled') is True)):
            first_use_effects['collection_start'] += 1
        if (path.startswith('/api/v1/wallet/')
                and path not in {'/api/v1/wallet/disconnect', '/api/v1/wallet/inspect'}):
            first_use_effects['signing_request'] += 1
        if (path == '/api/v1/trades/submit' or path.endswith('/signed')
                or path.endswith('/broadcast')):
            first_use_effects['submission_request'] += 1
        if path == '/api/v1/test-provider-scenarios':
            if body != {}:
                raise ValueError('Provider scenario fixture accepts no fields')
            seeded_at = time.time()
            seed_provider_diagnostic_scenarios(data_dir, seeded_at, MINT)
            return {
                'schema': 'pumpwizard.provider-browser-fixture/v1',
                'seeded_at': seeded_at,
                'states': ['healthy', 'empty', 'unavailable_route', 'partial', 'stale', 'failing'],
                'wallet_authority': False,
            }
        if path == '/api/v1/test-strategy':
            from pumpwizard_runner.portfolio import CONFIG
            from pumpwizard_runner.strategies import parse_package
            from pumpwizard_runner.strategy_risk import save_policy
            from pumpwizard_runner.manual_trades import TradeJournal
            from test_agent_execution import observe
            from test_strategy_risk import limits

            sender = body['sender']
            bank = chain.bank(sender)
            raw = bytearray(base64.b64decode(bank.bank['curve']['data'][0]))
            for offset, value in ((8, 10**15), (16, 120_000_000_000), (32, 50_000_000_000), (40, 10**15)):
                struct.pack_into('<Q', raw, offset, value)
            bank.bank['curve']['data'][0] = base64.b64encode(raw).decode()
            service = body.get('service') is True
            autopilot = body.get('autopilot') is True or service
            label = 'Service Autopilot fixture' if service else 'Autopilot browser fixture' if autopilot else 'Reviewed browser fixture'
            if body.get('label'):
                label = body['label']
            strategy = parse_package({'schema':'pumpwizard.strategy-handoff/v1', 'strategy':{'id':'core-frequency','label':label}, 'configuration':{**CONFIG, 'max_positions': body.get('strategy_positions', CONFIG['max_positions'])}})
            library.import_strategy(strategy)
            brief = ({
                'goal': 'validate-strategy',
                'intent': 'Review an exact generated main-wallet allowance without enabling live activation.',
                'paper_budget_usd': 500,
                'max_session_loss_usd': 100,
                'review_after_seconds': 86_400,
                'session_mode': 'trade',
                'execution_style': 'autopilot',
                'automation_scope': 'entries-and-exits',
                'profit_management': 'strategy-managed',
                **({'custody_mode': 'main-wallet-service'} if service else {}),
            } if autopilot else None)
            if (body.get('workspace') is True or body.get('workspace_id')) and brief is None:
                brief = dict(goal='validate-strategy', intent='Test session-wide controls with finalized synthetic receipts.', paper_budget_usd=500, max_session_loss_usd=100, review_after_seconds=86400, session_mode='trade', execution_style='interactive', automation_scope=None, profit_management='user-directed')
            ident = library.create_instance(
                strategy.digest, entry_mode='disabled' if autopilot else 'manual', brief=brief,
            )
            now = time.time()
            for i in range(6):
                observe(library, ident, now-50+i*10, 110+i, 40+i)
            if not service:
                library.record_decisions(ident, [dict(at=now,kind='entry_recommended',mint=MINT,reason='strategy_entry_signal',payload={})])
            journal = TradeJournal(library.path.parent)
            try:
                workspace = None
                if body.get('workspace') is True:
                    from pumpwizard_runner.session_allocations import reserve_workspace
                    workspace = library.create_session_workspace('trade', body.get('workspace_label', 'Controls fixture'), allocation_lamports='300000000', wallet_sender=sender, wallet_network='mainnet-beta')
                    reserve_workspace(journal, workspace['id'], bank.genesis(), sender, 300000000, bank.balance)
                    library.assign_instance_to_workspace(workspace['id'], ident)
                elif body.get('workspace_id'):
                    workspace = library.session_workspace(body['workspace_id'])
                    if (workspace['wallet_sender'], workspace['wallet_network']) != (sender, 'mainnet-beta'):
                        raise ValueError('Focus fixture workspace belongs to another wallet')
                    library.assign_instance_to_workspace(workspace['id'], ident)
                save_policy(journal, ident, bank.genesis(), sender, limits(max_trade_loss_bps=10000, max_open_positions=body.get('policy_positions', limits()['max_open_positions'])), bank.balance, workspace_id=workspace['id'] if workspace else None)
                if autopilot:
                    from pumpwizard_runner import wallet_risk
                    wallet_risk.save_policy(journal, bank.genesis(), sender, {
                        'max_buy_lamports': 250_000_000,
                        'max_exposure_lamports': 1_000_000_000,
                        'reserve_lamports': 100_000_000,
                        'daily_loss_lamports': 200_000_000,
                        'drawdown_lamports': 200_000_000,
                        'portfolio_drawdown_lamports': 500_000_000,
                        'entries_paused': False,
                    })
            finally:
                journal.close()
            return {'id':ident, 'workspace_id': workspace['id'] if workspace else None}
        if path == '/api/v1/test-service-lifecycle':
            action = body.get('action')
            if action == 'status':
                return sequence_snapshot()
            if action == 'release':
                if set(body) != {'action'}:
                    raise ValueError('Synthetic service lifecycle release accepts no identity fields')
                if sequence_snapshot()['phase'] != 'waiting-for-browser-close':
                    raise ValueError('Synthetic service lifecycle is not waiting for browser closure')
                service_sequence_gate.set()
                return sequence_snapshot()
            if action != 'start' or set(body) != {'action', 'sender', 'session_id'}:
                raise ValueError('Synthetic service lifecycle requires start/status and exact identity')
            current = sequence_snapshot()
            if current['phase'] not in ('idle', 'completed', 'failed'):
                raise ValueError('Synthetic service lifecycle is already running')
            sender, session_id = body['sender'], body['session_id']
            if library.session_brief(session_id).get('custody_mode') != 'main-wallet-service':
                raise ValueError('Synthetic service lifecycle requires service custody')
            baseline = len(chain.bank(sender).sent)
            service_sequence_gate.clear()
            update_sequence(
                phase='starting', detail='', sent=baseline,
                started_at=time.time(), released_at=None, completed_at=None,
            )
            threading.Thread(
                target=run_service_sequence,
                args=(sender, session_id, baseline),
                name='pumpwizard-service-e2e-lifecycle',
                daemon=True,
            ).start()
            return sequence_snapshot()
        if path == '/api/v1/test-service-control-order':
            if set(body) != {'action', 'sender', 'session_id'}:
                raise ValueError('Synthetic service order control requires exact action and identity')
            action, sender, session_id = body['action'], body['sender'], body['session_id']
            from pumpwizard_runner.manual_trades import TradeJournal
            from pumpwizard_runner.transfers import GENESIS

            journal = TradeJournal(data_dir)
            try:
                allowance = journal.db.execute(
                    "SELECT id,scope FROM main_wallet_allowances "
                    "WHERE session_id=? AND sender=? ORDER BY created_at DESC LIMIT 1",
                    (session_id, sender),
                ).fetchone()
                if allowance is None:
                    raise ValueError('Synthetic service order control requires an allowance')
                scope = json.loads(allowance['scope'])
                if action == 'issue-review':
                    order_id = 'browser-unsigned-service-review'
                    preview = {
                        'id': order_id, 'sender': sender,
                        'genesis': GENESIS['mainnet-beta'], 'side': 'buy',
                        'amount': 1, 'fee': 0, 'created_at': time.time(),
                    }
                    with journal.db:
                        journal.db.execute(
                            'INSERT INTO agent_orders VALUES (?,?,?,?,?,?,?,?,?)',
                            (order_id, session_id, MINT, 'buy', scope['cash_hold'], None,
                             'review', '{}', time.time()),
                        )
                        journal.db.execute(
                            'INSERT INTO trade_previews VALUES (?,?)',
                            (order_id, json.dumps(preview, sort_keys=True, separators=(',', ':'))),
                        )
                    return {'order_id': order_id, 'status': 'review'}
                if action == 'issue-submitted':
                    order_id = 'browser-unresolved-service-submission'
                    signature = 'browser-fixture-signature-awaiting-public-reconciliation'
                    recorded = {
                        'sender': sender, 'genesis': GENESIS['mainnet-beta'],
                        'network': 'mainnet-beta', 'side': 'sell', 'mint': MINT,
                        'amount': 1, 'decimals': 6, 'position_id': None,
                        'route': 'pumpswap-canonical-sol-spl-exit-v1',
                        'last_valid_height': 100,
                        'agent_order': {'agent_id': session_id},
                    }
                    bank = chain.bank(sender)
                    bank.receipts[signature] = {'meta': {'err': None}}
                    bank.statuses[signature] = None
                    with journal.db:
                        journal.db.execute(
                            'INSERT INTO agent_orders VALUES (?,?,?,?,?,?,?,?,?)',
                            (order_id, session_id, MINT, 'sell', scope['cash_hold'],
                             'browser-fixture-token-hold', 'submitted', '{}', time.time()),
                        )
                        journal.db.execute(
                            'INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,NULL)',
                            (order_id, GENESIS['mainnet-beta'], sender, signature, 'unknown',
                             time.time(), json.dumps(recorded, sort_keys=True, separators=(',', ':')),
                             'Original signature retained for public reconciliation'),
                        )
                        journal.db.execute(
                            'INSERT INTO wallet_intents VALUES (?,?,?,?)',
                            (GENESIS['mainnet-beta'], sender, order_id, 'trade'),
                        )
                    return {'order_id': order_id, 'status': 'submitted', 'signature': signature}
                if action == 'resolve-submitted':
                    order_id = 'browser-unresolved-service-submission'
                    receipt = {
                        'err': 'Synthetic public reconciliation completed without a fill',
                        'fee_lamports': 0, 'wallet_delta': 0, 'token_delta': 0,
                        'slot': 1, 'block_time': time.time(),
                        'signature': 'browser-fixture-signature-awaiting-public-reconciliation',
                        'route': 'pumpswap-canonical-sol-spl-exit-v1',
                        'cost_basis': 'synthetic no-fill reconciliation',
                    }
                    with journal.db:
                        journal.db.execute(
                            "UPDATE trades SET status='failed',detail=?,receipt=? WHERE id=?",
                            ('Synthetic public reconciliation completed without a fill',
                             json.dumps(receipt, sort_keys=True, separators=(',', ':')), order_id),
                        )
                        journal.db.execute(
                            "UPDATE agent_orders SET status='failed' WHERE id=?", (order_id,),
                        )
                        journal.db.execute(
                            'DELETE FROM wallet_intents WHERE id=?', (order_id,),
                        )
                    return {'order_id': order_id, 'status': 'failed'}
                if action == 'status':
                    rows = journal.db.execute(
                        'SELECT id,status FROM agent_orders WHERE agent_id=? ORDER BY created_at',
                        (session_id,),
                    ).fetchall()
                    events = journal.db.execute(
                        "SELECT kind,payload FROM main_wallet_allowance_events "
                        "WHERE allowance_id=? ORDER BY at",
                        (allowance['id'],),
                    ).fetchall()
                    return {
                        'orders': [dict(row) for row in rows],
                        'events': [dict(row) for row in events],
                    }
                raise ValueError('Unknown synthetic service order control action')
            finally:
                journal.close()
        if path == '/api/v1/test-research-batch3-fixture':
            if set(body) != {'session_id'}:
                raise ValueError('Research Batch 3 fixture requires one source session ID')
            session_id = body['session_id']
            instance = library.instance(session_id)
            strategy_digest = instance['strategy_digest']
            existing = [
                MarketQuote.from_record(dict(row))
                for row in library.session_review_snapshot(session_id)['observations']
            ]
            end_at = time.time() - 10
            observations = []
            for index in range(110):
                observed_at = end_at - (109 - index) * 61
                selected = MarketQuote.from_record(dict(
                    mint=MINT, symbol='TEST', observed_at=observed_at,
                    source_at=observed_at, fx_at=observed_at,
                    route='pump_bonding_curve_sol', curve_address='Curve',
                    x=1e6, y=100 + index * .3, real_x=5e5,
                    real_y=40 + index * .1, supply=1e6, quote_usd=100,
                    market_cap_usd=(100 + index * .3) * 100, decimals=6,
                    source='batch3-browser-fixture',
                ))
                observations.append(selected)
            observations = sorted(
                {f'{row.mint}:{row.observed_at:.9f}': row
                 for row in [*existing, *observations]}.values(),
                key=lambda row: (row.observed_at, row.mint),
            )
            opportunities = []
            features = []
            for index, selected in enumerate(observations):
                decision_key = f'batch3:{index}:{selected.observed_at:.9f}'
                opportunities.append({
                    'evaluation_key': decision_key,
                    'decision_key': decision_key,
                    'at': selected.observed_at,
                    'mint': selected.mint,
                    'eligible': False,
                    'reason': 'Synthetic fixture observation; no trading signal claimed',
                    'strategy_digest': strategy_digest,
                    'feature_key': decision_key,
                    'payload': {'route': selected.route, 'pool': selected.pool},
                })
                features.append({
                    'schema': 'pumpwizard.session-decision-features/v1',
                    'decision_key': decision_key,
                    'captured_at': selected.observed_at,
                    'strategy_sha256': strategy_digest,
                    'engine_version': 'batch3-browser-fixture/1',
                    'mint': selected.mint,
                    'features': {'fixture_index': index},
                    'provenance': {'future_observations_included': False},
                })
            library.commit_monitor_cycle(
                session_id, quotes=observations, decisions=[], checkpoint={},
                started_at=observations[0].observed_at,
                finished_at=observations[-1].observed_at,
                marks=[], opportunities=opportunities, features=features,
                capture_complete=True,
            )
            if background_service.recorder is None:
                raise ValueError('Research fixture requires the running service recorder')
            # The reviewed-session fixture already has six exact observations.
            # Give every source row recorder provenance so the browser exercise
            # proves one complete source rather than silently ignoring the
            # pre-existing rows as unknown first-receipt evidence.
            source_snapshot = library.session_review_snapshot(session_id)
            recorded_observations = [
                MarketQuote.from_record(dict(row))
                for row in source_snapshot['observations']
            ]
            for selected in recorded_observations:
                event_at = selected.observed_at
                run_id = background_service.recorder.begin_scan(
                    minimum_cap=1, maximum_cap=1_000_000,
                    source='batch3-browser-fixture', priority=[MINT], limit=1,
                    requested_at=event_at + 1, started_at=event_at + 2,
                )
                background_service.recorder.complete_scan(
                    run_id, [selected], received_at=event_at + 3,
                )
            return {
                'schema': 'pumpwizard.research-browser-fixture/v1',
                'session_id': session_id,
                'strategy_sha256': strategy_digest,
                'observations': len(recorded_observations),
                'collection_runs': len(recorded_observations),
                'first_event_at': recorded_observations[0].observed_at,
                'last_event_at': recorded_observations[-1].observed_at,
                'wallet_authority': False,
                'signer_calls': 0,
                'submission_calls': 0,
            }
        if path == '/api/v1/test-research-batch4-quality-fixture':
            if set(body) != {'session_id'}:
                raise ValueError('Research Batch 4 quality fixture requires one source session ID')
            if background_service.recorder is None:
                raise ValueError('Research quality fixture requires the running service recorder')
            at = time.time()
            selected = MarketQuote(
                MINT, 'TEST', at, 'curve', 'Curve', 20_000, 1e-19, 1e-21,
                5_000, 50, source='batch4-browser-quality-fixture', quote_usd=100,
                base_reserve=1_000_000, reserve_usd=50_000,
                real_token_reserve=500_000, supply=1_000_000_000, decimals=6,
                source_at=at, fx_at=at,
            )
            run_id = background_service.recorder.begin_scan(
                minimum_cap=1, maximum_cap=1_000_000,
                source='batch4-browser-quality-fixture', priority=[MINT], limit=1,
                requested_at=at, started_at=at,
            )
            background_service.recorder.complete_scan(
                run_id, [selected], received_at=at + .001,
            )
            return {
                'schema': 'pumpwizard.research-browser-quality-fixture/v1',
                'session_id': body['session_id'], 'evidence_rows': 1,
                'price_usd': 1e-19, 'expected_quality': 'unsupported-price-precision',
                'wallet_authority': False,
            }
        if path == '/api/v1/test-research-batch4-queue-fixture':
            if set(body) != {'session_id'}:
                raise ValueError('Research Batch 4 queue fixture requires one source session ID')
            from pumpwizard_runner.research_data import ResearchDataStore
            from pumpwizard_runner.research_jobs import ResearchRegistry

            with ResearchDataStore(data_dir) as data:
                datasets = data.list(source_session_id=body['session_id'], limit=10)
            if not datasets:
                raise ValueError('Research queue fixture requires frozen source evidence')
            with ResearchRegistry(data_dir) as registry:
                experiments = registry.experiments(dataset_id=datasets[0]['id'], limit=100)
                exposed = next(
                    (row for row in experiments
                     if registry.holdout_status(row['id'])['state'] == 'exposed'),
                    None,
                )
                if exposed is None:
                    raise ValueError('Research queue fixture requires an exposed eligible experiment')
                base = {
                    'dataset_id': exposed['dataset_id'],
                    'candidates': [row['package'] for row in exposed['candidates']],
                    'cost_model': exposed['cost_model'], 'capital': exposed['capital'],
                    'splits': exposed['splits'],
                    'embargo_seconds': exposed['embargo_seconds'],
                    'objective': 'Batch 4 conflicting queue fixture',
                    'evidence_policy': exposed['evidence_policy'],
                    'purpose': 'eligible',
                    'engine_version': exposed['engine_version'],
                    'code_version': exposed['code_version'],
                }
                blocked_experiment = registry.register(base)
                blocked = registry.enqueue(
                    blocked_experiment['id'], idempotency_key='batch4-browser-blocked-oldest',
                )
                base['purpose'] = 'exploratory'
                base['objective'] = 'Batch 4 valid queue fixture with holdout withheld'
                valid_experiment = registry.register(base)
                valid = registry.enqueue(
                    valid_experiment['id'], idempotency_key='batch4-browser-valid-newest',
                )
            return {
                'schema': 'pumpwizard.research-browser-queue-fixture/v1',
                'blocked_job_id': blocked['id'], 'valid_job_id': valid['id'],
                'blocked_experiment_id': blocked_experiment['id'],
                'valid_experiment_id': valid_experiment['id'],
                'wallet_authority': False,
            }
        if path == '/api/v1/test-service-cleanup':
            if set(body) != {'session_id'}:
                raise ValueError('Synthetic service cleanup requires one session ID')
            session_id = body['session_id']
            from pumpwizard_runner.manual_trades import TradeJournal

            journal = TradeJournal(data_dir)
            try:
                senders = [row[0] for row in journal.db.execute(
                    'SELECT DISTINCT sender FROM agent_reservations WHERE agent_id=?',
                    (session_id,),
                )]
                with journal.db:
                    journal.db.execute(
                        'DELETE FROM main_wallet_allowance_events WHERE allowance_id IN '
                        '(SELECT id FROM main_wallet_allowances WHERE session_id=?)',
                        (session_id,),
                    )
                    journal.db.execute(
                        'DELETE FROM main_wallet_allowance_orders WHERE allowance_id IN '
                        '(SELECT id FROM main_wallet_allowances WHERE session_id=?)',
                        (session_id,),
                    )
                    journal.db.execute(
                        'DELETE FROM main_wallet_allowances WHERE session_id=?', (session_id,),
                    )
                    journal.db.execute(
                        'DELETE FROM main_wallet_allowance_reviews WHERE session_id=?', (session_id,),
                    )
                    journal.db.execute(
                        'DELETE FROM trades WHERE id IN '
                        '(SELECT trade_id FROM agent_fills WHERE agent_id=?)',
                        (session_id,),
                    )
                    journal.db.execute(
                        'DELETE FROM trade_positions WHERE id IN '
                        '(SELECT position_id FROM agent_positions WHERE agent_id=?)',
                        (session_id,),
                    )
                    journal.db.execute(
                        'DELETE FROM trade_previews WHERE id IN '
                        '(SELECT id FROM agent_orders WHERE agent_id=?)',
                        (session_id,),
                    )
                    for table in ('agent_fills', 'agent_orders', 'agent_positions',
                                  'agent_reservations', 'strategy_risk_policies',
                                  'strategy_risk_changes'):
                        journal.db.execute(f'DELETE FROM {table} WHERE agent_id=?', (session_id,))
                    for sender in senders:
                        for table in ('wallet_risk_policies', 'wallet_risk_changes',
                                      'wallet_revisions', 'wallet_intents'):
                            journal.db.execute(f'DELETE FROM {table} WHERE sender=?', (sender,))
            finally:
                journal.close()
            portfolio_path = data_dir / 'portfolio-risk.sqlite'
            if portfolio_path.exists() and senders:
                portfolio_db = sqlite3.connect(portfolio_path)
                try:
                    with portfolio_db:
                        portfolio_db.executemany(
                            'DELETE FROM portfolios WHERE sender=?', [(sender,) for sender in senders],
                        )
                finally:
                    portfolio_db.close()
            library.remove_instance(session_id)
            return {'removed': True, 'session_id': session_id}
        if path == '/api/v1/test-session-review-suggestion':
            database = sqlite3.connect(library.path.parent / 'session-reviews.sqlite')
            try:
                row = database.execute(
                    'SELECT payload_json FROM reports WHERE id=?', (body['report_id'],),
                ).fetchone()
                if row is None:
                    raise ValueError('Unknown synthetic session review')
                report = json.loads(row[0])
                candidate = next(item for item in report['candidate_packages'] if item['kind'] == 'candidate')
                report['recommendation'] = {
                    'action': 'create_inactive_draft',
                    'candidate_id': candidate['id'],
                    'reason': 'Synthetic browser fixture selected this bounded candidate.',
                }
                database.execute(
                    'UPDATE reports SET payload_json=? WHERE id=?',
                    (json.dumps(report, sort_keys=True, separators=(',', ':')), report['id']),
                )
                database.commit()
                return {'report_id': report['id'], 'candidate_id': candidate['id']}
            finally:
                database.close()
        if path == "/api/v1/test-chain":
            return chain.control(body["sender"], body.get("changes", {}))
        if path == "/api/v1/test-restart":
            checkpoint.write_bytes(pickle.dumps(chain))
            restart_requested = True
            threading.Thread(target=server.shutdown, daemon=True).start()
            return {"restarting": True}
        return command(library, path, body, *context, **options)

    web_api.command = test_command

    def stop(*_args: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if restart_requested:
            os.execv(sys.executable, [sys.executable, __file__, "--web-root", str(args.web_root),
                                    "--port", str(args.port), "--data-dir", str(data_dir)])
        shutil.rmtree(data_dir)


if __name__ == "__main__":
    main()
