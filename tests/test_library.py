from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import pytest

from pumpwizard_runner.decision import MarketQuote
from pumpwizard_runner.library import DATABASE_NAME, LEGACY_DATABASE_NAME, StrategyLibrary
from pumpwizard_runner.strategies import parse_package


ROOT = Path(__file__).parents[1]


def test_library_deduplicates_by_canonical_content(tmp_path: Path):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    try:
        assert library.import_strategy(strategy)
        assert not library.import_strategy(strategy)
        rows = library.rows()
        assert len(rows) == 1
        assert rows[0]["strategy_id"] == "xyz-momentum-example"
    finally:
        library.close()


def test_local_position_ledger_keeps_entry_and_partial_exit_history(tmp_path: Path):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest, entry_mode="manual", exit_mode="manual")
        position_id = library.record_position(instance_id, "MintABC", "ABC", quantity=100,
                                              total_cost_usd=20, opened_at=100)
        library.record_position_exit(position_id, quantity=25, proceeds_usd=8, exited_at=150, stages=(0,))
        open_position = library.positions(instance_id, include_closed=False)[0]
        assert open_position["quantity"] == "75"
        assert open_position["remaining_cost_usd"] == "15"
        library.record_position_exit(position_id, quantity=75, proceeds_usd=30, exited_at=200)
        closed_position = library.positions(instance_id)[0]
        assert closed_position["status"] == "closed"
        assert closed_position["realized_proceeds_usd"] == "38"
        assert [event["kind"] for event in library.position_events(instance_id)] == [
            "external_exit", "external_partial_exit", "external_entry",
        ]
    finally:
        library.close()


def test_latest_observations_returns_one_newest_quote_per_mint(tmp_path: Path):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        library.append_observations(instance_id, [
            {"mint": "MintA", "symbol": "A", "observed_at": 100, "market_cap_usd": 10_000},
            {"mint": "MintB", "symbol": "B", "observed_at": 150, "market_cap_usd": 20_000},
            {"mint": "MintA", "symbol": "A", "observed_at": 200, "market_cap_usd": 12_000},
        ])
        rows = library.latest_observations(instance_id)
        assert [(row["mint"], row["market_cap_usd"]) for row in rows] == [
            ("MintA", 12_000), ("MintB", 20_000),
        ]
    finally:
        library.close()


def test_entry_considerations_keep_latest_saved_entry_stance_and_matching_quote(tmp_path: Path):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    library.import_strategy(strategy)
    instance_id = library.create_instance(strategy.digest)
    library.append_observations(instance_id, [
        {"mint": "MintA", "symbol": "A", "observed_at": 100, "price_usd": 1.0},
        {"mint": "MintA", "symbol": "A", "observed_at": 200, "price_usd": 1.2},
        {"mint": "MintB", "symbol": "B", "observed_at": 175, "price_usd": 2.0},
    ])
    library.record_decisions(instance_id, [
        {"kind": "entry_blocked", "mint": "MintA", "at": 150,
         "reason": "Building observation window", "payload": {"symbol": "A"}},
        {"kind": "exit_recommended", "mint": "MintA", "at": 210,
         "reason": "stop_loss", "payload": {}},
        {"kind": "entry_recommended", "mint": "MintA", "at": 250,
         "reason": "strategy_entry_signal", "payload": {"symbol": "A"}},
        {"kind": "entry_blocked", "mint": "MintB", "at": 180,
         "reason": "Thin liquidity", "payload": {"symbol": "B"}},
    ])
    library.close()

    reopened = StrategyLibrary(tmp_path)
    try:
        rows = reopened.entry_considerations(instance_id)
        assert [(row["mint"], row["kind"]) for row in rows] == [
            ("MintA", "entry_recommended"), ("MintB", "entry_blocked"),
        ]
        assert rows[0]["observation"]["price_usd"] == 1.2
        assert rows[1]["observation"]["observed_at"] == 175
        assert reopened.entry_considerations(instance_id, 1)[0]["mint"] == "MintA"
    finally:
        reopened.close()


def test_v1_library_migrates_without_losing_existing_strategy_rows(tmp_path: Path):
    database = tmp_path / "solanamarket-wizard.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta(key,value) VALUES ('schema_version','1');
        CREATE TABLE strategies (
            digest TEXT PRIMARY KEY, imported_at TEXT NOT NULL, schema_name TEXT NOT NULL,
            strategy_id TEXT NOT NULL, label TEXT NOT NULL, strategy_version TEXT, engine TEXT,
            compatibility TEXT NOT NULL, reason TEXT NOT NULL, package_json TEXT NOT NULL
        );
        INSERT INTO strategies VALUES ('digest','now','schema','example','Example',NULL,NULL,
                                      'legacy-inspection-only','reason','{}');
    """)
    connection.close()
    library = StrategyLibrary(tmp_path)
    try:
        assert library.summary()["schema_version"] == 16
        assert library.rows()[0]["strategy_id"] == "example"
        assert {"position_events", "monitor_runs", "ownership_checks", "wallet_checks", "orders", "order_events",
                "localnet_checks", "market_watch_samples", "market_token_metadata",
                "market_history_candles", "market_history_jobs", "removed_instances", "session_briefs",
                "decision_outcomes", "decision_features", "evaluated_opportunities",
                "opportunity_capture_cycles", "session_workspaces",
                "session_workspace_instances"} <= {
            row["name"] for row in library._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        library.close()


def test_session_brief_is_validated_immutable_and_durable(tmp_path: Path):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        brief = {
            "goal": "validate-strategy",
            "intent": "Challenge the entry signal with a bounded observation session.",
            "paper_budget_usd": 500,
            "max_session_loss_usd": 75,
            "review_after_seconds": 14_400,
            "session_mode": "monitoring",
            "execution_style": None,
            "profit_management": None,
            "preferences": {
                "risk_tolerance": "balanced",
                "reward_risk_ratio": 2,
                "investment_style": "small",
                "time_horizon": "swing",
            },
        }
        instance_id = library.create_instance(strategy.digest, "Validation session", brief=brief)
        stored = library.session_brief(instance_id)
        assert stored["schema"] == "pumpwizard.session-brief/v3"
        assert stored["goal"] == "validate-strategy"
        assert stored["strategy_digest_at_creation"] == strategy.digest
        assert stored["mode"] == "monitoring"
        assert stored["session_mode"] == "monitoring"
        assert stored["permissions"]["prepare_orders"] is False
        assert stored["permissions"]["custody"] == "none"
        assert stored["paper_budget_usd"] == 500
        assert stored["max_session_loss_usd"] == 75
        assert stored["preferences"] == brief["preferences"]
        assert stored["review_at"] == stored["created_at"] + 14_400
        assert {"question_id": "intent", "answer": brief["intent"]} in stored["answers"]
        with pytest.raises(ValueError, match="cannot exceed"):
            library.create_instance(strategy.digest, brief={**brief, "max_session_loss_usd": 501})
        with pytest.raises(ValueError, match="between 1:1 and 10:1"):
            library.create_instance(strategy.digest, brief={
                **brief,
                "preferences": {**brief["preferences"], "reward_risk_ratio": 11},
            })
        with pytest.raises(ValueError, match="cannot request trading execution"):
            library.create_instance(strategy.digest, brief={
                **brief, "execution_style": "interactive", "profit_management": "user-directed",
            })
        autopilot = {
            **brief,
            "session_mode": "trade",
            "execution_style": "autopilot",
            "profit_management": "winner-handoff",
        }
        autopilot_id = library.create_instance(strategy.digest, "Autopilot intent", brief=autopilot)
        stored_autopilot = library.session_brief(autopilot_id)
        assert stored_autopilot["mode"] == "trade-autopilot"
        assert stored_autopilot["automation_scope"] == "entries-and-exits"
        assert stored_autopilot["permissions"]["automatic_entries"] is True
        assert stored_autopilot["permissions"]["automatic_protective_exits"] is True
        assert stored_autopilot["permissions"]["automatic_profit_exits"] is False
        assert stored_autopilot["permissions"]["winner_handoff"] is False
        assert stored_autopilot["permissions"]["automation_wallet_required"] is False
        assert stored_autopilot["permissions"]["generated_wallet_required"] is True
        assert stored_autopilot["permissions"]["custody"] == "main-wallet-browser"
        for custody in ("main-wallet-browser", "main-wallet-service"):
            entries_brief = {
                **autopilot, "automation_scope": "entries-only",
                "profit_management": "user-directed", "custody_mode": custody,
            }
            entries_id = library.create_instance(strategy.digest, "Automatic entries", brief=entries_brief)
            entries = library.session_brief(entries_id)
            assert entries["permissions"]["automatic_entries"] is True
            assert entries["permissions"]["automatic_protective_exits"] is False
            assert entries["permissions"]["automatic_profit_exits"] is False
            assert entries["profit_management"] == "user-directed"
            with pytest.raises(ValueError, match="requires user-directed exits"):
                library.create_instance(strategy.digest, brief={**entries_brief, "profit_management": "strategy-managed"})
        service_autopilot = {
            **autopilot,
            "automation_scope": "exits-only",
            "custody_mode": "main-wallet-service",
        }
        service_id = library.create_instance(
            strategy.digest, "Background Autopilot intent", brief=service_autopilot,
        )
        stored_service = library.session_brief(service_id)
        assert stored_service["schema"] == "pumpwizard.session-brief/v4"
        assert stored_service["custody_mode"] == "main-wallet-service"
        assert stored_service["permissions"]["custody"] == "main-wallet-service"
        assert stored_service["permissions"]["automatic_entries"] is False
        assert stored_service["permissions"]["service_vault_required"] is True
        assert stored_service["permissions"]["continues_without_browser"] is True
        legacy = {
            **stored_autopilot,
            "schema": "pumpwizard.session-brief/v1",
            "permissions": {
                **stored_autopilot["permissions"],
                "automation_wallet_required": True,
                "custody": "automation-wallet",
            },
        }
        legacy["permissions"].pop("generated_wallet_required")
        encoded_legacy = json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with library._connection:
            library._connection.execute(
                "UPDATE session_briefs SET schema_name=?,payload_json=? WHERE instance_id=?",
                ("pumpwizard.session-brief/v1", encoded_legacy, autopilot_id),
            )
    finally:
        library.close()
    reopened = StrategyLibrary(tmp_path)
    try:
        assert reopened.session_brief(instance_id) == stored
        assert json.dumps(
            reopened.session_brief(autopilot_id), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ) == encoded_legacy
    finally:
        reopened.close()


def test_session_workspace_exists_before_strategies_and_groups_positions(tmp_path: Path):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        workspace = library.create_session_workspace("monitoring")
        assert workspace["label"] == "Monitoring session 1"
        assert workspace["allocation_lamports"] == "0"
        assert library.session_workspaces()[0]["strategy_count"] == 0

        instance_id = library.create_instance(strategy.digest, "First strategy")
        library.assign_instance_to_workspace(str(workspace["id"]), instance_id)
        grouped = library.session_workspaces()[0]
        assert grouped["strategy_count"] == 1
        assert grouped["position_count"] == 0
        assert library.instance(instance_id)["workspace_id"] == workspace["id"]
        assert library.session_workspace_instance_ids(str(workspace["id"])) == [instance_id]
    finally:
        library.close()


def test_decision_outcomes_require_continuous_fixed_horizon_evidence(tmp_path: Path):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        library.append_observations(instance_id, [
            {"mint": "MintA", "symbol": "A", "observed_at": at, "route": "curve",
             "pool": "PoolA", "price_usd": price, "market_cap_usd": 10_000}
            for at, price in ((100, 1.0), (200, 1.1), (300, .9), (400, 1.3))
        ] + [
            {"mint": "MintB", "symbol": "B", "observed_at": 100, "route": "curve",
             "pool": "PoolB", "price_usd": 2.0, "market_cap_usd": 20_000},
        ])
        library.record_decisions(instance_id, [
            {"kind": "entry_recommended", "mint": "MintA", "at": 100,
             "reason": "strategy_entry_signal", "payload": {}},
            {"kind": "entry_blocked", "mint": "MintB", "at": 100,
             "reason": "Building observation window", "payload": {}},
        ])
        assert library.score_decision_outcomes(instance_id, now=519) == 0
        assert library.score_decision_outcomes(instance_id, now=520) == 2
        rows = library.decision_outcomes(instance_id)
        scored = next(row for row in rows if row["mint"] == "MintA")
        unknown = next(row for row in rows if row["mint"] == "MintB")
        assert scored["status"] == "scored"
        assert scored["horizon_seconds"] == 300
        assert scored["price_return_fraction"] == pytest.approx(.3)
        assert scored["maximum_favorable_fraction"] == pytest.approx(.3)
        assert scored["maximum_adverse_fraction"] == pytest.approx(-.1)
        assert unknown["status"] == "unknown"
        assert unknown["coverage"] == "missing_horizon_quote"
        assert library.score_decision_outcomes(instance_id, now=520) == 0
    finally:
        library.close()


def test_market_watch_history_is_durable_and_groups_samples_by_coin(tmp_path: Path):
    library = StrategyLibrary(tmp_path)
    try:
        base = dict(mint="MintABC", symbol="ABC", route="curve", pool="CurveABC", price_usd=0.01,
                    price_native=0.0001, liquidity_usd=400, liquidity_native=4, market_cap_usd=10_000,
                    created_at=20)
        library.record_market_watch_quote(MarketQuote(observed_at=100, metadata={"name": "Alpha Coin", "source": "Pump.fun"}, **base))
        library.record_market_watch_quote(MarketQuote(observed_at=130, market_cap_usd=12_500, **{
            key: value for key, value in base.items() if key != "market_cap_usd"}))
        rows = library.market_watch_rows()
        assert rows[0]["mint"] == "MintABC"
        assert rows[0]["market_cap_history"] == [10_000.0, 12_500.0]
        assert rows[0]["metadata"] == {"name": "Alpha Coin", "source": "Pump.fun"}
        assert library.coin_metadata("MintABC")["name"] == "Alpha Coin"
    finally:
        library.close()


def test_library_keeps_an_existing_pre_rebrand_database_visible(tmp_path: Path):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    first = StrategyLibrary(tmp_path)
    try:
        first.import_strategy(strategy)
    finally:
        first.close()
    (tmp_path / DATABASE_NAME).replace(tmp_path / LEGACY_DATABASE_NAME)

    library = StrategyLibrary(tmp_path)
    try:
        assert library.path.name == LEGACY_DATABASE_NAME
        assert library.rows()[0]["strategy_id"] == "xyz-momentum-example"
    finally:
        library.close()


def test_wallet_snapshots_are_durable_and_do_not_need_wallet_credentials(tmp_path: Path):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        snapshot = {
            "address": "So11111111111111111111111111111111111111112", "checked_at": 100,
            "slot": 123, "sol_lamports": "2500000000", "token_balances": {},
        }
        library.record_wallet_check(instance_id, snapshot)
        assert library.wallet_history(instance_id) == [snapshot]
        assert library.recent_wallet_snapshots() == [{"instance_id": instance_id, "snapshot": snapshot}]
        assert library.summary()["wallet_checks"] == 1
    finally:
        library.close()


def test_confirmed_localnet_signer_check_has_a_separate_public_history(tmp_path: Path):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        evidence = {
            "checked_at": 100, "network": "localnet", "status": "confirmed",
            "sender": "sender", "recipient": "recipient", "lamports": 1000, "signature": "signature",
        }
        library.record_localnet_check(instance_id, evidence)
        assert library.localnet_checks(instance_id) == [evidence]
        assert library.summary()["localnet_checks"] == 1
    finally:
        library.close()


def test_ownership_evidence_is_a_durable_per_mint_history(tmp_path: Path):
    strategy = parse_package((ROOT / "examples" / "xyz-local-strategy.json").read_text())
    library = StrategyLibrary(tmp_path)
    try:
        library.import_strategy(strategy)
        instance_id = library.create_instance(strategy.digest)
        evidence = {"mint": "MintABC", "checked_at": 100, "policy_id": "ownership-basic-v1:test",
                    "status": "clear", "reasons": []}
        library.record_ownership(instance_id, evidence)
        assert library.latest_ownership(instance_id, "MintABC") == evidence
        assert library.ownership_history(instance_id, "MintABC") == [evidence]
    finally:
        library.close()


def test_parallel_clients_can_open_a_new_library(tmp_path: Path):
    """Parallel browser reads must not race while the first client enables WAL."""
    def open_and_read(_index: int) -> dict[str, int]:
        library = StrategyLibrary(tmp_path)
        try:
            return library.summary()
        finally:
            library.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        summaries = list(pool.map(open_and_read, range(16)))

    assert len(summaries) == 16
    assert all(summary["strategies"] == 0 for summary in summaries)
