"""Local durable storage for imported strategy packages.

This schema is intentionally separate from future wallet and position tables so
opening the initial app never creates or touches a wallet.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Iterator
import uuid

from .decision import MarketQuote, PositionView
from .strategies import ImportedStrategy


SCHEMA_VERSION = 16
DATABASE_NAME = "solanamarket-wizard.sqlite"
LEGACY_DATABASE_NAME = "pumpwizard.sqlite"
_CONNECTION_SETUP_LOCK = threading.Lock()
SESSION_BRIEF_SCHEMA = "pumpwizard.session-brief/v3"
SERVICE_SESSION_BRIEF_SCHEMA = "pumpwizard.session-brief/v4"
SESSION_GOALS = frozenset({"observe-market", "validate-strategy", "collect-evidence"})
SESSION_RISK_TOLERANCES = frozenset({"conservative", "balanced", "aggressive"})
SESSION_INVESTMENT_STYLES = frozenset({"small", "standard", "concentrated"})
SESSION_TIME_HORIZONS = frozenset({"intraday", "swing", "long-term"})
SESSION_MODES = frozenset({"monitoring", "trade"})
SESSION_EXECUTION_STYLES = frozenset({"interactive", "autopilot"})
SESSION_PROFIT_MANAGEMENT = frozenset({"user-directed", "strategy-managed", "winner-handoff"})
SESSION_AUTOMATION_SCOPES = frozenset({"entries-only", "exits-only", "entries-and-exits"})
DECISION_OUTCOME_HORIZONS = (300, 1800, 14_400)
DECISION_OUTCOME_GRACE = 120


class StrategyLibrary:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        current_path = data_dir / DATABASE_NAME
        legacy_path = data_dir / LEGACY_DATABASE_NAME
        # Keep prior local libraries visible after the product rebrand. New
        # installs always receive the current branded filename.
        self.path = legacy_path if legacy_path.exists() and not current_path.exists() else current_path
        self._connection = sqlite3.connect(self.path, timeout=10)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=10000")
        with _CONNECTION_SETUP_LOCK:
            for attempt in range(20):
                try:
                    journal_mode = self._connection.execute("PRAGMA journal_mode").fetchone()[0]
                    if str(journal_mode).lower() != "wal":
                        self._connection.execute("PRAGMA journal_mode=WAL")
                    self._connection.execute("PRAGMA foreign_keys=ON")
                    self._initialize()
                    break
                except sqlite3.OperationalError as exc:
                    self._connection.rollback()
                    if "locked" not in str(exc).lower() or attempt == 19:
                        raise
                    time.sleep(0.05)

    def _initialize(self) -> None:
        with self._connection:
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS monitor_settings (
                    instance_id TEXT PRIMARY KEY REFERENCES instances(id),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL DEFAULT 0,
                    fault TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS monitor_checkpoints (
                    instance_id TEXT PRIMARY KEY REFERENCES instances(id),
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitor_lease (
                    id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategies (
                    digest TEXT PRIMARY KEY,
                    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    schema_name TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    strategy_version TEXT,
                    engine TEXT,
                    compatibility TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    package_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_wallets (
                    digest TEXT NOT NULL REFERENCES strategies(digest),
                    sender TEXT NOT NULL, network TEXT NOT NULL,
                    PRIMARY KEY(digest,sender,network)
                );
                CREATE TABLE IF NOT EXISTS strategy_allocation_sizing (
                    digest TEXT NOT NULL REFERENCES strategies(digest),
                    sender TEXT NOT NULL, network TEXT NOT NULL, workspace_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(digest,sender,network,workspace_id)
                );
                CREATE INDEX IF NOT EXISTS strategy_library_order
                    ON strategies(strategy_id, imported_at DESC);
                CREATE TABLE IF NOT EXISTS instances (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    strategy_digest TEXT NOT NULL REFERENCES strategies(digest),
                    label TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('monitoring','paused')),
                    entry_mode TEXT NOT NULL CHECK(entry_mode IN ('disabled','manual','automatic')),
                    exit_mode TEXT NOT NULL CHECK(exit_mode IN ('manual','automatic'))
                );
                CREATE TABLE IF NOT EXISTS session_workspaces (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    label TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('monitoring','trade')),
                    allocation_lamports TEXT NOT NULL DEFAULT '0',
                    wallet_sender TEXT,
                    wallet_network TEXT,
                    plan_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS session_workspace_instances (
                    workspace_id TEXT NOT NULL REFERENCES session_workspaces(id),
                    instance_id TEXT NOT NULL UNIQUE REFERENCES instances(id),
                    assigned_at REAL NOT NULL,
                    PRIMARY KEY(workspace_id,instance_id)
                );
                CREATE INDEX IF NOT EXISTS session_workspace_instances_order
                    ON session_workspace_instances(workspace_id,assigned_at,instance_id);
                CREATE TABLE IF NOT EXISTS session_briefs (
                    instance_id TEXT PRIMARY KEY REFERENCES instances(id),
                    schema_name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    review_at REAL NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS removed_instances (
                    instance_id TEXT PRIMARY KEY REFERENCES instances(id),
                    removed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY,
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    mint TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(instance_id,mint,observed_at)
                );
                CREATE INDEX IF NOT EXISTS observations_recent
                    ON observations(instance_id,observed_at,mint);
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY,
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    decision_key TEXT NOT NULL,
                    at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    mint TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(instance_id,decision_key)
                );
                CREATE INDEX IF NOT EXISTS decisions_recent
                    ON decisions(instance_id,at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS decision_outcomes (
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    decision_key TEXT NOT NULL,
                    horizon_seconds INTEGER NOT NULL CHECK(horizon_seconds > 0),
                    due_at REAL NOT NULL,
                    scored_at REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('scored','unknown')),
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(instance_id,decision_key,horizon_seconds)
                );
                CREATE INDEX IF NOT EXISTS decision_outcomes_recent
                    ON decision_outcomes(instance_id,scored_at DESC,decision_key);
                CREATE TABLE IF NOT EXISTS decision_features (
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    decision_key TEXT NOT NULL,
                    captured_at REAL NOT NULL,
                    schema_name TEXT NOT NULL,
                    strategy_digest TEXT NOT NULL,
                    engine_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(instance_id,decision_key)
                );
                CREATE INDEX IF NOT EXISTS decision_features_recent
                    ON decision_features(instance_id,captured_at,decision_key);
                CREATE TABLE IF NOT EXISTS evaluated_opportunities (
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    evaluation_key TEXT NOT NULL,
                    at REAL NOT NULL,
                    mint TEXT NOT NULL,
                    decision_key TEXT NOT NULL,
                    eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
                    reason TEXT NOT NULL,
                    strategy_digest TEXT NOT NULL,
                    feature_key TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(instance_id,evaluation_key),
                    UNIQUE(instance_id,decision_key)
                );
                CREATE INDEX IF NOT EXISTS evaluated_opportunities_recent
                    ON evaluated_opportunities(instance_id,at,mint,evaluation_key);
                CREATE TABLE IF NOT EXISTS opportunity_capture_cycles (
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    cycle_key TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL NOT NULL,
                    first_observed_at REAL,
                    last_observed_at REAL,
                    complete INTEGER NOT NULL CHECK(complete IN (0,1)),
                    strategy_digest TEXT NOT NULL,
                    PRIMARY KEY(instance_id,cycle_key)
                );
                CREATE INDEX IF NOT EXISTS opportunity_capture_cycles_recent
                    ON opportunity_capture_cycles(instance_id,finished_at,cycle_key);
                CREATE TABLE IF NOT EXISTS monitor_runs (
                    id INTEGER PRIMARY KEY,
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    started_at REAL NOT NULL,
                    finished_at REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('completed','failed')),
                    observations INTEGER NOT NULL DEFAULT 0 CHECK(observations >= 0),
                    decisions INTEGER NOT NULL DEFAULT 0 CHECK(decisions >= 0),
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS monitor_runs_recent
                    ON monitor_runs(instance_id,finished_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS ownership_checks (
                    id INTEGER PRIMARY KEY,
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    mint TEXT NOT NULL,
                    checked_at REAL NOT NULL,
                    policy_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('clear','blocked','unknown')),
                    payload_json TEXT NOT NULL,
                    UNIQUE(instance_id,mint,checked_at,policy_id)
                );
                CREATE INDEX IF NOT EXISTS ownership_checks_recent
                    ON ownership_checks(instance_id,mint,checked_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS wallet_checks (
                    id INTEGER PRIMARY KEY,
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    address TEXT NOT NULL,
                    checked_at REAL NOT NULL,
                    slot INTEGER NOT NULL CHECK(slot >= 0),
                    sol_lamports TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(instance_id,address,checked_at)
                );
                CREATE INDEX IF NOT EXISTS wallet_checks_recent
                    ON wallet_checks(instance_id,address,checked_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS executor_configs (
                    instance_id TEXT PRIMARY KEY REFERENCES instances(id),
                    updated_at REAL NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('disabled','dry-run')),
                    max_order_usd TEXT NOT NULL,
                    max_slippage_bps INTEGER NOT NULL CHECK(max_slippage_bps >= 0 AND max_slippage_bps <= 2500),
                    decision_ttl_seconds REAL NOT NULL CHECK(decision_ttl_seconds >= 5 AND decision_ttl_seconds <= 600)
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    decision_key TEXT NOT NULL,
                    decision_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft','simulated','cancelled')),
                    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
                    mint TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    route TEXT NOT NULL,
                    position_id TEXT,
                    max_input_usd TEXT,
                    token_amount TEXT,
                    minimum_output_amount TEXT,
                    plan_json TEXT NOT NULL,
                    UNIQUE(instance_id,decision_key)
                );
                CREATE INDEX IF NOT EXISTS orders_recent
                    ON orders(instance_id,created_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS order_events (
                    id INTEGER PRIMARY KEY,
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    order_id TEXT NOT NULL REFERENCES orders(id),
                    at REAL NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('prepared','dry_run_simulated','cancelled')),
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS order_events_recent
                    ON order_events(instance_id,at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS localnet_checks (
                    id INTEGER PRIMARY KEY,
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    checked_at REAL NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    lamports TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('confirmed')),
                    payload_json TEXT NOT NULL,
                    UNIQUE(instance_id,signature)
                );
                CREATE INDEX IF NOT EXISTS localnet_checks_recent
                    ON localnet_checks(instance_id,checked_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    mint TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('open','closed','external_change')),
                    management_mode TEXT NOT NULL CHECK(management_mode IN ('manual','automatic')),
                    opened_at REAL NOT NULL,
                    closed_at REAL,
                    initial_quantity TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    total_cost_usd TEXT,
                    remaining_cost_usd TEXT,
                    realized_proceeds_usd TEXT NOT NULL DEFAULT '0',
                    sold_stages_json TEXT NOT NULL DEFAULT '[]',
                    peak_price_usd TEXT NOT NULL DEFAULT '0',
                    last_usable_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS position_events (
                    id INTEGER PRIMARY KEY,
                    instance_id TEXT NOT NULL REFERENCES instances(id),
                    position_id TEXT NOT NULL REFERENCES positions(id),
                    at REAL NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('external_entry','external_partial_exit','external_exit')),
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS position_events_recent
                    ON position_events(instance_id,at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS market_watch_samples (
                    id INTEGER PRIMARY KEY,
                    mint TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    created_at REAL NOT NULL DEFAULT 0,
                    price_usd TEXT NOT NULL,
                    market_cap_usd TEXT NOT NULL,
                    liquidity_usd TEXT NOT NULL,
                    UNIQUE(mint,observed_at)
                );
                CREATE INDEX IF NOT EXISTS market_watch_samples_recent
                    ON market_watch_samples(mint,observed_at DESC);
                CREATE TABLE IF NOT EXISTS market_token_metadata (
                    mint TEXT PRIMARY KEY,
                    updated_at REAL NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_history_candles (
                    mint TEXT NOT NULL,
                    source TEXT NOT NULL,
                    resolution_seconds INTEGER NOT NULL CHECK(resolution_seconds > 0),
                    opened_at REAL NOT NULL,
                    open_usd TEXT NOT NULL,
                    high_usd TEXT NOT NULL,
                    low_usd TEXT NOT NULL,
                    close_usd TEXT NOT NULL,
                    volume_usd TEXT NOT NULL,
                    PRIMARY KEY(mint,source,resolution_seconds,opened_at)
                );
                CREATE INDEX IF NOT EXISTS market_history_candles_recent
                    ON market_history_candles(mint,opened_at DESC);
                CREATE TABLE IF NOT EXISTS market_history_jobs (
                    mint TEXT NOT NULL,
                    source TEXT NOT NULL,
                    resolution_seconds INTEGER NOT NULL CHECK(resolution_seconds > 0),
                    status TEXT NOT NULL CHECK(status IN ('ready','running','complete','error')),
                    before_at REAL NOT NULL,
                    earliest_at REAL,
                    latest_at REAL,
                    row_count INTEGER NOT NULL DEFAULT 0 CHECK(row_count >= 0),
                    updated_at REAL NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    coverage TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(mint,source,resolution_seconds)
                );
            """)
            workspace_columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(session_workspaces)")
            }
            if "wallet_sender" not in workspace_columns:
                self._connection.execute("ALTER TABLE session_workspaces ADD COLUMN wallet_sender TEXT")
            if "wallet_network" not in workspace_columns:
                self._connection.execute("ALTER TABLE session_workspaces ADD COLUMN wallet_network TEXT")
            if "plan_json" not in workspace_columns:
                self._connection.execute(
                    "ALTER TABLE session_workspaces ADD COLUMN plan_json TEXT NOT NULL DEFAULT '{}'"
                )
            current = self._connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if current is None:
                self._connection.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            elif current["value"] == "1":
                # 0.2 only adds independent-monitoring tables.  The original
                # imported strategy records are untouched.
                columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(positions)")}
                if columns and "closed_at" not in columns:
                    self._connection.execute("ALTER TABLE positions ADD COLUMN closed_at REAL")
                self._connection.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
            elif current["value"] in (
                "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15",
            ):
                self._connection.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
            elif current["value"] != str(SCHEMA_VERSION):
                raise RuntimeError("Unsupported local strategy-library database version")

    def close(self) -> None:
        self._connection.close()

    def tracking_settings(self, instance_id: str) -> dict:
        self.instance(instance_id)
        row = self._connection.execute("SELECT * FROM monitor_settings WHERE instance_id=?", (instance_id,)).fetchone()
        return dict(row) if row else dict(instance_id=instance_id, enabled=0, revision=0, next_at=0, fault="")

    def set_tracking(self, instance_id: str, enabled: bool) -> None:
        self.instance(instance_id)
        if enabled and self.instance(instance_id)['entry_mode'] != 'disabled':
            from .session_funding import require_ready
            require_ready(self, instance_id)
        with self._connection:
            self._connection.execute("""INSERT INTO monitor_settings(instance_id,enabled,revision)
                VALUES (?,?,1) ON CONFLICT(instance_id) DO UPDATE SET enabled=excluded.enabled,
                revision=monitor_settings.revision+1,next_at=0,fault=''""", (instance_id, int(enabled)))
            self._connection.execute("UPDATE instances SET status=? WHERE id=?",
                                     ("monitoring" if enabled else "paused", instance_id))

    def pause_workspace(self, workspace_id: str) -> None:
        """Disable scans and automatic orders for every current session member."""
        self.session_workspace(workspace_id)
        with self._connection:
            self._connection.execute('BEGIN IMMEDIATE')
            ids = self.session_workspace_instance_ids(workspace_id)
            for ident in ids:
                self._connection.execute("""INSERT INTO monitor_settings(instance_id,enabled,revision)
                    VALUES (?,0,1) ON CONFLICT(instance_id) DO UPDATE SET enabled=0,
                    revision=monitor_settings.revision+1,next_at=0,fault=''""", (ident,))
                self._connection.execute("UPDATE instances SET status='paused',entry_mode='disabled',exit_mode='manual' WHERE id=?", (ident,))

    def set_execution_modes(self, instance_id: str, *, entries: str, exits: str) -> None:
        """Set runtime execution modes without changing the immutable brief."""
        if entries not in ("disabled", "manual", "automatic"):
            raise ValueError("Invalid entry execution mode")
        if exits not in ("manual", "automatic"):
            raise ValueError("Invalid exit execution mode")
        if entries == 'automatic':
            from .session_funding import require_ready
            require_ready(self, instance_id)
        with self._connection:
            cursor = self._connection.execute(
                "UPDATE instances SET entry_mode=?,exit_mode=? WHERE id=?",
                (entries, exits, instance_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown strategy instance")

    def scheduled_instances(self) -> list[dict]:
        from .agent_execution import owned_position_rows

        owned = owned_position_rows(self)
        result = [dict(row) for row in self._connection.execute("""
            SELECT i.id, i.status, coalesce(m.enabled,0) AS enabled,
                coalesce(m.revision,0) AS revision,coalesce(m.next_at,0) AS next_at,
                coalesce(m.fault,'') AS fault,
                (SELECT count(*) FROM positions p WHERE p.instance_id=i.id AND p.status='open') AS held
            FROM instances i LEFT JOIN monitor_settings m ON m.instance_id=i.id
            WHERE NOT EXISTS (SELECT 1 FROM removed_instances r WHERE r.instance_id=i.id)
            ORDER BY next_at,i.created_at,i.id
        """)]
        for row in result:
            row['held'] += sum(p['agent_id']==row['id'] for p in owned)
        return [r for r in result if r['held'] or (r['enabled'] and r['status']=='monitoring')]

    def claim_monitor_lease(self, owner: str, at: float, ttl: float = 180) -> bool:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute("SELECT * FROM monitor_lease WHERE id=1").fetchone()
            if row and row["owner"] != owner and row["expires_at"] > at:
                return False
            self._connection.execute("INSERT OR REPLACE INTO monitor_lease VALUES (1,?,?)", (owner, at+ttl))
        return True

    def release_monitor_lease(self, owner: str) -> None:
        with self._connection:
            self._connection.execute("DELETE FROM monitor_lease WHERE owner=?", (owner,))

    def monitor_lease(self) -> dict[str, object] | None:
        """Return the current scheduler lease without claiming or extending it."""
        row = self._connection.execute("SELECT owner, expires_at FROM monitor_lease WHERE id=1").fetchone()
        return dict(row) if row is not None else None

    def monitor_checkpoint(self, instance_id: str) -> dict | None:
        row = self._connection.execute("SELECT payload_json FROM monitor_checkpoints WHERE instance_id=?", (instance_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def commit_monitor_cycle(self, instance_id: str, *, quotes: list[MarketQuote], decisions: list[dict],
                             checkpoint: dict, started_at: float, finished_at: float,
                             marks: list[MarketQuote], revision: int | None = None, owner: str | None = None,
                             error: str = "", fault: bool = False,
                             opportunities: list[dict] | None = None,
                             features: list[dict] | None = None,
                             capture_complete: bool = False) -> bool:
        """One commit for observations, transitions, position marks, and scheduler state.

        A pause or lease takeover during collection invalidates the in-flight batch.
        """
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if owner:
                lease = self._connection.execute("SELECT * FROM monitor_lease WHERE id=1").fetchone()
                if not lease or lease["owner"] != owner or lease["expires_at"] <= time.time():
                    return False
            if revision is not None and self.tracking_settings(instance_id)["revision"] != revision:
                return False
            for quote in quotes:
                self._connection.execute("INSERT OR IGNORE INTO observations(instance_id,mint,observed_at,payload_json) VALUES (?,?,?,?)",
                    (instance_id, quote.mint, quote.observed_at, json.dumps(quote.as_record(), allow_nan=False)))
            for item in decisions:
                payload = json.dumps(item.get("payload", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                key = "|".join((str(item["kind"]), str(item["mint"]), str(item["at"]), str(item["reason"]), payload))
                self._connection.execute("""INSERT OR IGNORE INTO decisions
                    (instance_id,decision_key,at,kind,mint,reason,payload_json) VALUES (?,?,?,?,?,?,?)""",
                    (instance_id,key,item["at"],item["kind"],item["mint"],item["reason"],payload))
            for item in opportunities or []:
                payload = json.dumps(item.get("payload", {}), ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":"))
                self._connection.execute("""INSERT OR IGNORE INTO evaluated_opportunities(
                    instance_id,evaluation_key,at,mint,decision_key,eligible,reason,
                    strategy_digest,feature_key,payload_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                    instance_id, item["evaluation_key"], item["at"], item["mint"],
                    item["decision_key"], int(bool(item["eligible"])), item["reason"],
                    item["strategy_digest"], item.get("feature_key"), payload,
                ))
            for item in features or []:
                self._connection.execute("""INSERT OR IGNORE INTO decision_features(
                    instance_id,decision_key,captured_at,schema_name,strategy_digest,
                    engine_version,payload_json
                ) VALUES (?,?,?,?,?,?,?)""", (
                    instance_id, item["decision_key"], item["captured_at"], item["schema"],
                    item["strategy_sha256"], item["engine_version"], json.dumps(
                        item, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    ),
                ))
            if quotes and not error:
                cycle_key = f"{started_at:.9f}:{finished_at:.9f}"
                observed = [float(quote.observed_at) for quote in quotes]
                self._connection.execute("""INSERT OR IGNORE INTO opportunity_capture_cycles(
                    instance_id,cycle_key,started_at,finished_at,first_observed_at,
                    last_observed_at,complete,strategy_digest
                ) VALUES (?,?,?,?,?,?,?,?)""", (
                    instance_id, cycle_key, started_at, finished_at, min(observed), max(observed),
                    int(bool(capture_complete)), self.instance(instance_id)["strategy_digest"],
                ))
            if not error:
                self._connection.execute("INSERT OR REPLACE INTO monitor_checkpoints VALUES (?,?)",
                                         (instance_id,json.dumps(checkpoint, allow_nan=False)))
                for quote in marks:
                    self._connection.execute("""UPDATE positions SET peak_price_usd=
                        max(cast(peak_price_usd AS REAL),?),last_usable_at=max(last_usable_at,?)
                        WHERE instance_id=? AND mint=? AND status='open'""",
                        (quote.price_usd,quote.observed_at,instance_id,quote.mint))
            self._connection.execute("""INSERT INTO monitor_runs(instance_id,started_at,finished_at,status,observations,decisions,detail)
                VALUES (?,?,?,?,?,?,?)""", (instance_id,started_at,finished_at,"failed" if error else "completed",
                    len(quotes),len(decisions),error or ("No supported quotes returned" if not quotes else "")))
            if revision is not None:
                self._connection.execute("""INSERT INTO monitor_settings(instance_id,next_at,fault) VALUES (?,?,?)
                    ON CONFLICT(instance_id) DO UPDATE SET next_at=excluded.next_at,fault=excluded.fault""",
                    (instance_id,finished_at+15,error if fault else ""))
            if not error:
                self._score_decision_outcomes(instance_id, finished_at)
        return True

    def import_strategy(self, strategy: ImportedStrategy, scope: tuple[str, str] = ("", "")) -> bool:
        with self._connection:
            cursor = self._connection.execute("""
                INSERT OR IGNORE INTO strategies(
                    digest, schema_name, strategy_id, label, strategy_version, engine,
                    compatibility, reason, package_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (strategy.digest, strategy.schema, strategy.strategy_id, strategy.label,
                  strategy.version, strategy.engine, strategy.compatibility, strategy.reason,
                  json.dumps(strategy.package, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
            self._connection.execute("INSERT OR IGNORE INTO strategy_wallets VALUES (?,?,?)",
                                     (strategy.digest, *scope))
        return cursor.rowcount == 1

    def rows(self) -> list[dict[str, str | None]]:
        return [dict(row) for row in self._connection.execute("""
            SELECT digest, imported_at, schema_name, strategy_id, label,
                   strategy_version, engine, compatibility, reason
            FROM strategies ORDER BY imported_at DESC, strategy_id
        """)]

    def strategy(self, digest: str) -> dict[str, object]:
        row = self._connection.execute("SELECT * FROM strategies WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise ValueError("Unknown strategy digest")
        value = dict(row)
        value["package"] = json.loads(value.pop("package_json"))
        return value

    def create_instance(self, digest: str, label: str | None = None, *, entry_mode: str = "disabled",
                        exit_mode: str = "manual", brief: dict[str, object] | None = None) -> str:
        strategy = self.strategy(digest)
        if strategy["compatibility"] != "monitoring-ready":
            raise ValueError("This strategy cannot be started by the local monitoring engine")
        if entry_mode not in ("disabled", "manual", "automatic") or exit_mode not in ("manual", "automatic"):
            raise ValueError("Invalid local automation mode")
        instance_id = uuid.uuid4().hex
        instance_label = (label or str(strategy["label"])).strip()
        if not instance_label or len(instance_label) > 120:
            raise ValueError("Instance label must be 1–120 characters")
        session_brief = _validated_session_brief(brief, strategy)
        with self._connection:
            self._connection.execute("""
                INSERT INTO instances(id,strategy_digest,label,status,entry_mode,exit_mode)
                VALUES (?,?,?,?,?,?)
            """, (instance_id, digest, instance_label, "monitoring", entry_mode, exit_mode))
            self._connection.execute("""
                INSERT INTO session_briefs(instance_id,schema_name,created_at,review_at,payload_json)
                VALUES (?,?,?,?,?)
            """, (instance_id, session_brief["schema"], session_brief["created_at"],
                  session_brief["review_at"], json.dumps(
                      session_brief, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
        return instance_id

    def create_session_workspace(self, mode: str, label: str | None = None, *,
                                 allocation_lamports: str = "0",
                                 wallet_sender: str | None = None,
                                 wallet_network: str | None = None,
                                 plan: dict[str, object] | None = None) -> dict[str, object]:
        """Create a user-visible session before any strategy is assigned to it."""
        if mode not in ("monitoring", "trade"):
            raise ValueError("Choose a monitoring or trading session")
        if not isinstance(allocation_lamports, str) or not allocation_lamports.isdigit():
            raise ValueError("Session allocation must use exact nonnegative lamports")
        if mode == "monitoring" and allocation_lamports != "0":
            raise ValueError("Monitoring sessions cannot hold a wallet allocation")
        if mode == "trade" and int(allocation_lamports) <= 0:
            raise ValueError("Trading session allocation must be greater than zero")
        if (mode == "trade" or wallet_sender is not None or wallet_network is not None) and (
                not isinstance(wallet_sender, str) or not wallet_sender
                or wallet_network not in {"mainnet-beta", "devnet"}):
            raise ValueError("Trading sessions require one wallet and Solana network")
        if plan is not None and not isinstance(plan, dict):
            raise ValueError("Session plan must be an object")
        if mode == "monitoring" and plan:
            if (set(plan) != {"monitor_activity"}
                    or plan["monitor_activity"] not in ("observations", "paper")):
                raise ValueError("Monitoring sessions accept only an observations or paper activity")
        try:
            plan_json = json.dumps(
                plan or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
            )
        except (TypeError, ValueError):
            raise ValueError("Session plan must contain finite JSON values") from None
        if len(plan_json.encode()) > 20_000:
            raise ValueError("Session plan is too large")
        workspace_id = uuid.uuid4().hex
        if label is None:
            count = self._connection.execute(
                "SELECT COUNT(*) FROM session_workspaces WHERE mode=?", (mode,)
            ).fetchone()[0]
            cleaned = f"{'Monitoring' if mode == 'monitoring' else 'Trading'} session {count + 1}"
        elif not isinstance(label, str):
            raise ValueError("Session label must be text")
        else:
            cleaned = label.strip()
        if not cleaned or len(cleaned) > 120 or any(ord(character) < 32 for character in cleaned):
            raise ValueError("Session label must be 1–120 printable characters")
        created_at = time.time()
        with self._connection:
            self._connection.execute("""
                INSERT INTO session_workspaces(
                    id,created_at,label,mode,allocation_lamports,wallet_sender,wallet_network,plan_json
                ) VALUES (?,?,?,?,?,?,?,?)
            """, (
                workspace_id, created_at, cleaned, mode, allocation_lamports,
                wallet_sender, wallet_network, plan_json,
            ))
        return self.session_workspace(workspace_id)

    def session_workspace(self, workspace_id: str) -> dict[str, object]:
        row = self._connection.execute(
            "SELECT * FROM session_workspaces WHERE id=?", (workspace_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown session")
        value = dict(row)
        value["plan"] = json.loads(value.pop("plan_json"))
        return value

    def session_workspaces(self) -> list[dict[str, object]]:
        rows = self._connection.execute("""
            SELECT session_workspaces.*,
                   COUNT(DISTINCT CASE WHEN removed_instances.instance_id IS NULL THEN session_workspace_instances.instance_id END) AS strategy_count,
                   COUNT(DISTINCT CASE WHEN removed_instances.instance_id IS NULL AND positions.status='open' THEN positions.id END) AS position_count
            FROM session_workspaces
            LEFT JOIN session_workspace_instances
              ON session_workspace_instances.workspace_id=session_workspaces.id
            LEFT JOIN removed_instances
              ON removed_instances.instance_id=session_workspace_instances.instance_id
            LEFT JOIN positions
              ON positions.instance_id=session_workspace_instances.instance_id
            GROUP BY session_workspaces.id
            ORDER BY session_workspaces.created_at DESC,session_workspaces.id
        """)
        values = []
        for row in rows:
            value = dict(row)
            value["plan"] = json.loads(value.pop("plan_json"))
            values.append(value)
        return values

    def assign_instance_to_workspace(self, workspace_id: str, instance_id: str) -> None:
        workspace = self.session_workspace(workspace_id)
        brief = self.session_brief(instance_id)
        if brief.get("session_mode", "monitoring") != workspace["mode"]:
            raise ValueError("The strategy mode must match its session")
        with self._connection:
            cursor = self._connection.execute("""
                INSERT INTO session_workspace_instances(workspace_id,instance_id,assigned_at)
                SELECT ?,?,? WHERE NOT EXISTS (
                    SELECT 1 FROM session_workspace_instances a
                    JOIN instances i ON i.id=a.instance_id
                    WHERE a.workspace_id=? AND (i.strategy_digest=(SELECT strategy_digest FROM instances WHERE id=?)
                      OR (SELECT json_extract(payload_json,'$.strategy_digest_at_creation') FROM session_briefs WHERE instance_id=i.id)=(SELECT strategy_digest FROM instances WHERE id=?))
                      AND NOT EXISTS (SELECT 1 FROM removed_instances r WHERE r.instance_id=i.id)
                )
            """, (workspace_id, instance_id, time.time(), workspace_id, instance_id, instance_id))
            if cursor.rowcount != 1:
                raise ValueError("This strategy is already in this session")
            if workspace.get("wallet_sender"):
                self._connection.execute("INSERT OR IGNORE INTO strategy_wallets VALUES (?,?,?)",
                    (self.instance(instance_id)["strategy_digest"], workspace["wallet_sender"], workspace["wallet_network"]))

    def session_workspace_instance_ids(self, workspace_id: str) -> list[str]:
        self.session_workspace(workspace_id)
        return [str(row["instance_id"]) for row in self._connection.execute("""
            SELECT session_workspace_instances.instance_id
            FROM session_workspace_instances
            JOIN instances ON instances.id=session_workspace_instances.instance_id
            LEFT JOIN removed_instances ON removed_instances.instance_id=instances.id
            WHERE session_workspace_instances.workspace_id=? AND removed_instances.instance_id IS NULL
            ORDER BY session_workspace_instances.assigned_at,session_workspace_instances.instance_id
        """, (workspace_id,))]

    def session_workspace_assignment_count(self, workspace_id: str) -> int:
        self.session_workspace(workspace_id)
        return int(self._connection.execute(
            "SELECT COUNT(*) FROM session_workspace_instances WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()[0])

    def session_workspace_strategy_history(self, workspace_id: str) -> list[dict[str, object]]:
        """Display attribution for every assignment, including removed strategies."""
        self.session_workspace(workspace_id)
        return [dict(id=row['id'], label=row['label'], removed=bool(row['removed']))
                for row in self._connection.execute("""
            SELECT instances.id,instances.label,removed_instances.instance_id IS NOT NULL AS removed
            FROM session_workspace_instances
            JOIN instances ON instances.id=session_workspace_instances.instance_id
            LEFT JOIN removed_instances ON removed_instances.instance_id=instances.id
            WHERE session_workspace_instances.workspace_id=?
            ORDER BY session_workspace_instances.assigned_at,instances.id
        """, (workspace_id,))]

    def remove_empty_session_workspace(self, workspace_id: str) -> None:
        """Remove only a session that has never had a strategy assigned."""
        self.session_workspace(workspace_id)
        if self.session_workspace_assignment_count(workspace_id):
            raise ValueError("Remove assigned strategies before removing this session")
        with self._connection:
            self._connection.execute("DELETE FROM session_workspaces WHERE id=?", (workspace_id,))

    def session_brief(self, instance_id: str) -> dict[str, object]:
        """Return the immutable local research envelope for one active session."""
        instance = self.instance(instance_id)
        row = self._connection.execute(
            "SELECT payload_json FROM session_briefs WHERE instance_id=?", (instance_id,)
        ).fetchone()
        if row is not None:
            return json.loads(row["payload_json"])
        # Libraries created before v13 gain a stable default only when first read.
        brief = _validated_session_brief(None, instance)
        with self._connection:
            self._connection.execute("""
                INSERT OR IGNORE INTO session_briefs(instance_id,schema_name,created_at,review_at,payload_json)
                VALUES (?,?,?,?,?)
            """, (instance_id, SESSION_BRIEF_SCHEMA, brief["created_at"], brief["review_at"],
                  json.dumps(brief, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
        return brief

    def revise_instance_strategy(self, instance_id: str, strategy: ImportedStrategy, *,
                                 scope=("", ""), before_update=None, update_brief=False) -> dict[str, object]:
        """Attach a new exact package only before the session produces evidence."""
        def check():
            instance = self.instance(instance_id)
            if self.tracking_settings(instance_id)["enabled"]:
                raise ValueError("Stop monitoring before editing this strategy")
            for table in ('monitor_runs', 'decisions', 'positions', 'orders'):
                if self._connection.execute(f'SELECT 1 FROM {table} WHERE instance_id=? LIMIT 1', (instance_id,)).fetchone():
                    raise ValueError("This strategy has activity. Create a new strategy session to change its rules")
            if instance.get('workspace_id') and self._connection.execute(
                """SELECT 1 FROM session_workspace_instances a JOIN instances i ON i.id=a.instance_id
                WHERE a.workspace_id=? AND i.strategy_digest=? AND i.id!=?
                AND NOT EXISTS (SELECT 1 FROM removed_instances r WHERE r.instance_id=i.id)""",
                (instance['workspace_id'], strategy.digest, instance_id)).fetchone():
                raise ValueError('This strategy is already in this session')
            return instance
        check()
        if strategy.compatibility != "monitoring-ready":
            raise ValueError("The edited strategy is not compatible with local monitoring")
        self.import_strategy(strategy, scope)
        with self._connection:
            self._connection.execute('BEGIN IMMEDIATE')
            check()
            if before_update:
                before_update()
            self._connection.execute(
                "UPDATE instances SET strategy_digest=?,entry_mode='disabled',exit_mode='manual' WHERE id=?",
                (strategy.digest, instance_id),
            )
            if update_brief:
                brief = self.session_brief(instance_id)
                config = strategy.package['configuration']
                brief.update(paper_budget_usd=config['budget_usd'], paper_order_usd=config['order_usd'],
                             max_positions=config['max_positions'],
                             max_session_loss_usd=min(brief['max_session_loss_usd'], config['budget_usd']))
                for answer in brief.get('answers', []):
                    if answer['question_id'] in ('paper_budget_usd', 'max_session_loss_usd'):
                        answer['answer'] = brief[answer['question_id']]
                self._connection.execute('UPDATE session_briefs SET payload_json=? WHERE instance_id=?',
                                         (json.dumps(brief), instance_id))
                self._connection.execute('UPDATE instances SET label=? WHERE id=?', (strategy.label, instance_id))
            self._connection.execute("DELETE FROM monitor_checkpoints WHERE instance_id=?", (instance_id,))
            self._connection.execute("""INSERT INTO monitor_settings(instance_id,enabled,revision,next_at,fault)
                VALUES (?,0,1,0,'') ON CONFLICT(instance_id) DO UPDATE SET enabled=0,
                revision=monitor_settings.revision+1,next_at=0,fault=''""", (instance_id,))
        return self.instance(instance_id)

    def instances(self) -> list[dict[str, object]]:
        rows = self._connection.execute("""
            SELECT instances.*, strategies.strategy_id, strategies.strategy_version,
                   strategies.compatibility,
                   session_workspace_instances.workspace_id
            FROM instances JOIN strategies ON strategies.digest=instances.strategy_digest
            LEFT JOIN session_workspace_instances ON session_workspace_instances.instance_id=instances.id
            WHERE NOT EXISTS (
                SELECT 1 FROM removed_instances WHERE removed_instances.instance_id=instances.id
            )
            ORDER BY instances.created_at DESC
        """)
        return [dict(row) for row in rows]

    def rename_instance(self, instance_id: str, label: str) -> dict[str, object]:
        self.instance(instance_id)
        if not isinstance(label, str):
            raise ValueError("Strategy name must be text")
        cleaned = label.strip()
        if not cleaned or len(cleaned) > 120 or any(ord(character) < 32 for character in cleaned):
            raise ValueError("Strategy name must be 1–120 printable characters")
        with self._connection:
            self._connection.execute("UPDATE instances SET label=? WHERE id=?", (cleaned, instance_id))
        return self.instance(instance_id)

    def removal_blocks(self, instance_id: str) -> list[str]:
        self.instance(instance_id)
        blocks: list[str] = []
        if self._connection.execute(
            "SELECT 1 FROM positions WHERE instance_id=? AND status='open' LIMIT 1", (instance_id,)
        ).fetchone():
            blocks.append("Close this strategy's open local position before removing it")
        return blocks

    def remove_instance(self, instance_id: str, *, removed_at: float | None = None) -> None:
        """Hide an inactive strategy session while retaining its evidence and references."""
        blocks = self.removal_blocks(instance_id)
        if blocks:
            raise ValueError(blocks[0])
        when = time.time() if removed_at is None else float(removed_at)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute("""INSERT INTO monitor_settings(instance_id,enabled,revision,next_at,fault)
                VALUES (?,0,1,0,'') ON CONFLICT(instance_id) DO UPDATE SET enabled=0,
                revision=monitor_settings.revision+1,next_at=0,fault=''""", (instance_id,))
            self._connection.execute("UPDATE instances SET status='paused' WHERE id=?", (instance_id,))
            self._connection.execute(
                "INSERT INTO removed_instances(instance_id,removed_at) VALUES (?,?)", (instance_id, when)
            )

    def record_market_watch_quote(self, quote: MarketQuote, *, bucket_seconds: int | None = None) -> None:
        """Keep a quote locally, optionally replacing its time-bucket snapshot."""
        if bucket_seconds is not None and (type(bucket_seconds) is not int or bucket_seconds < 1):
            raise ValueError("Market history bucket must be a positive whole number of seconds")
        values = (quote.mint, quote.symbol, quote.observed_at, quote.created_at,
                  str(quote.price_usd), str(quote.market_cap_usd), str(quote.liquidity_usd))
        with self._connection:
            if bucket_seconds is not None:
                bucket_start = math.floor(quote.observed_at / bucket_seconds) * bucket_seconds
                self._connection.execute("""
                    DELETE FROM market_watch_samples
                    WHERE mint=? AND observed_at>=? AND observed_at<?
                """, (quote.mint, bucket_start, bucket_start + bucket_seconds))
            self._connection.execute("""
                INSERT OR IGNORE INTO market_watch_samples(
                    mint,symbol,observed_at,created_at,price_usd,market_cap_usd,liquidity_usd
                ) VALUES (?,?,?,?,?,?,?)
            """, values)
            if quote.metadata:
                self._connection.execute("""
                    INSERT INTO market_token_metadata(mint,updated_at,metadata_json) VALUES (?,?,?)
                    ON CONFLICT(mint) DO UPDATE SET updated_at=excluded.updated_at,
                        metadata_json=excluded.metadata_json
                    WHERE excluded.updated_at >= market_token_metadata.updated_at
                """, (quote.mint, quote.observed_at,
                      json.dumps(quote.metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))

    def compact_market_watch_history(self, *, now: float | None = None,
                                     minimum_interval_seconds: int = 3600) -> int:
        """Tier retained snapshots: five minutes for 7d, hourly for 90d, then daily."""
        current = time.time() if now is None else now
        if (isinstance(current, bool) or not isinstance(current, (int, float)) or
                not math.isfinite(current) or current <= 0):
            raise ValueError("Market history compaction requires a positive timestamp")
        if type(minimum_interval_seconds) is not int or minimum_interval_seconds < 0:
            raise ValueError("Market history compaction interval must be a non-negative whole number")
        marker = self._connection.execute(
            "SELECT value FROM meta WHERE key='market_history_compacted_at'"
        ).fetchone()
        if marker is not None and current - float(marker["value"]) < minimum_interval_seconds:
            return 0
        hourly_before = current - 7 * 86_400
        daily_before = current - 90 * 86_400
        changes_before = self._connection.total_changes
        with self._connection:
            self._connection.execute("""
                DELETE FROM market_watch_samples WHERE id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY mint, CAST(observed_at / 3600 AS INTEGER)
                            ORDER BY observed_at DESC, id DESC
                        ) AS retained_rank
                        FROM market_watch_samples
                        WHERE observed_at>=? AND observed_at<?
                    ) WHERE retained_rank>1
                )
            """, (daily_before, hourly_before))
            self._connection.execute("""
                DELETE FROM market_watch_samples WHERE id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY mint, CAST(observed_at / 86400 AS INTEGER)
                            ORDER BY observed_at DESC, id DESC
                        ) AS retained_rank
                        FROM market_watch_samples
                        WHERE observed_at<?
                    ) WHERE retained_rank>1
                )
            """, (daily_before,))
            self._connection.execute("""
                INSERT INTO meta(key,value) VALUES('market_history_compacted_at',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (str(current),))
        return self._connection.total_changes - changes_before - 1

    def market_watch_rows(self, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 500:
            raise ValueError("Market-watch limit must be 1–500")
        rows = self._connection.execute("""
            SELECT latest.mint, latest.symbol, latest.observed_at, latest.created_at,
                   latest.price_usd, latest.market_cap_usd, latest.liquidity_usd,
                   metadata.metadata_json
            FROM market_watch_samples AS latest
            JOIN (
                SELECT mint, MAX(observed_at) AS observed_at
                FROM market_watch_samples GROUP BY mint
            ) AS newest ON newest.mint=latest.mint AND newest.observed_at=latest.observed_at
            LEFT JOIN market_token_metadata AS metadata ON metadata.mint=latest.mint
            ORDER BY latest.observed_at DESC LIMIT ?
        """, (limit,)).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            values = dict(row)
            raw_metadata = values.pop("metadata_json", None)
            values["metadata"] = json.loads(raw_metadata) if raw_metadata else {}
            history = self._connection.execute("""
                SELECT market_cap_usd FROM market_watch_samples
                WHERE mint=? ORDER BY observed_at DESC LIMIT 24
            """, (values["mint"],)).fetchall()
            values["market_cap_history"] = [float(sample["market_cap_usd"]) for sample in reversed(history)]
            result.append(values)
        return result

    def coin_metadata(self, mint: str) -> dict[str, object]:
        if not isinstance(mint, str) or not mint:
            raise ValueError("Coin metadata requires a mint")
        row = self._connection.execute(
            "SELECT metadata_json FROM market_token_metadata WHERE mint=?", (mint,)
        ).fetchone()
        return json.loads(row["metadata_json"]) if row else {}

    def record_coin_metadata(self, mint: str, metadata: dict[str, object], *, updated_at: float) -> None:
        """Retain bounded provider metadata even when no executable quote exists."""
        if not isinstance(mint, str) or not mint or not isinstance(metadata, dict):
            raise ValueError("Coin metadata requires a mint and metadata object")
        if (isinstance(updated_at, bool) or not isinstance(updated_at, (int, float)) or
                not math.isfinite(updated_at) or updated_at <= 0):
            raise ValueError("Coin metadata timestamp must be positive")
        with self._connection:
            self._connection.execute("""
                INSERT INTO market_token_metadata(mint,updated_at,metadata_json) VALUES(?,?,?)
                ON CONFLICT(mint) DO UPDATE SET updated_at=excluded.updated_at,
                    metadata_json=excluded.metadata_json
                WHERE excluded.updated_at >= market_token_metadata.updated_at
            """, (mint, updated_at,
                  json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))

    def coin_history(self, mint: str, limit: int = 100, *, before: float | None = None) -> list[dict]:
        """Bounded retained snapshots, newest first; never requests fresh data."""
        if not isinstance(mint, str) or not mint or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('Coin history requires a mint and a limit of 1–500')
        if before is not None and (isinstance(before, bool) or not isinstance(before, (int, float))
                                   or not math.isfinite(before) or before <= 0):
            raise ValueError('Coin history cursor must be a positive timestamp')
        if before is None:
            rows = self._connection.execute('''
                SELECT mint,symbol,observed_at,created_at,price_usd,market_cap_usd,liquidity_usd
                FROM market_watch_samples WHERE mint=? ORDER BY observed_at DESC LIMIT ?''', (mint, limit))
        else:
            rows = self._connection.execute('''
                SELECT mint,symbol,observed_at,created_at,price_usd,market_cap_usd,liquidity_usd
                FROM market_watch_samples WHERE mint=? AND observed_at<?
                ORDER BY observed_at DESC LIMIT ?''', (mint, before, limit))
        return [dict(r) for r in rows]

    def market_history_candles(self, mint: str, limit: int = 500, *,
                               before: float | None = None, source: str | None = None) -> list[dict[str, object]]:
        """Read attributed imported candles newest first without contacting a provider."""
        if not isinstance(mint, str) or not mint or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Historical candles require a mint and a limit of 1–1000")
        if before is not None and (isinstance(before, bool) or not isinstance(before, (int, float)) or
                                   not math.isfinite(before) or before <= 0):
            raise ValueError("Historical candle cursor must be a positive timestamp")
        where = "mint=?" if before is None else "mint=? AND opened_at<?"
        params = (mint,) if before is None else (mint, before)
        if source is not None:
            where += " AND source=?"
            params += (source,)
        params += (limit,)
        rows = self._connection.execute(f"""
            SELECT mint,source,resolution_seconds,opened_at,open_usd,high_usd,low_usd,close_usd,volume_usd
            FROM market_history_candles WHERE {where}
            ORDER BY opened_at DESC,source LIMIT ?
        """, params)
        return [dict(row) for row in rows]

    def market_history_job(self, mint: str, source: str, resolution_seconds: int) -> dict[str, object] | None:
        if (not isinstance(mint, str) or not mint or not isinstance(source, str) or not source or
                type(resolution_seconds) is not int or resolution_seconds <= 0):
            raise ValueError("Historical job identity is invalid")
        row = self._connection.execute("""
            SELECT mint,source,resolution_seconds,status,before_at,earliest_at,latest_at,row_count,
                   updated_at,error,coverage
            FROM market_history_jobs WHERE mint=? AND source=? AND resolution_seconds=?
        """, (mint, source, resolution_seconds)).fetchone()
        return dict(row) if row is not None else None

    def update_market_history_job(self, mint: str, source: str, resolution_seconds: int, *,
                                  status: str, before_at: float, error: str,
                                  coverage: str, updated_at: float) -> None:
        if (not isinstance(mint, str) or not mint or not isinstance(source, str) or not source or
                type(resolution_seconds) is not int or resolution_seconds <= 0 or
                not isinstance(error, str) or not isinstance(coverage, str)):
            raise ValueError("Historical job fields are invalid")
        if status not in ("ready", "running", "complete", "error"):
            raise ValueError("Historical job status is invalid")
        if (not isinstance(before_at, (int, float)) or isinstance(before_at, bool) or
                not math.isfinite(before_at) or before_at <= 0 or
                not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool) or
                not math.isfinite(updated_at) or updated_at <= 0):
            raise ValueError("Historical job timestamps are invalid")
        with self._connection:
            self._connection.execute("""
                INSERT INTO market_history_jobs(
                    mint,source,resolution_seconds,status,before_at,updated_at,error,coverage
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(mint,source,resolution_seconds) DO UPDATE SET
                    status=excluded.status,before_at=excluded.before_at,updated_at=excluded.updated_at,
                    error=excluded.error,coverage=excluded.coverage
            """, (mint, source, resolution_seconds, status, before_at, updated_at,
                  error[:500], coverage[:500]))

    def record_market_history_page(self, mint: str, source: str, resolution_seconds: int,
                                   candles: list[dict[str, object]], *, status: str,
                                   before_at: float, error: str, coverage: str,
                                   updated_at: float) -> None:
        if len(candles) > 1000:
            raise ValueError("Historical candle page exceeds 1000 rows")
        if status not in ("ready", "complete"):
            raise ValueError("Imported historical page must be ready or complete")
        fields = ("opened_at", "open_usd", "high_usd", "low_usd", "close_usd", "volume_usd")
        values = []
        for candle in candles:
            if not isinstance(candle, dict) or set(candle) != set(fields):
                raise ValueError("Historical candle fields are invalid")
            opened_at = candle["opened_at"]
            numbers = [candle[field] for field in fields[1:]]
            try:
                parsed = [float(value) for value in numbers]
            except (TypeError, ValueError):
                raise ValueError("Historical candle numbers are invalid") from None
            if (isinstance(opened_at, bool) or not isinstance(opened_at, (int, float)) or
                    not math.isfinite(opened_at) or opened_at <= 0 or
                    not all(math.isfinite(value) and value >= 0 for value in parsed) or
                    min(parsed[:4]) <= 0 or parsed[1] < max(parsed[0], parsed[3]) or
                    parsed[2] > min(parsed[0], parsed[3])):
                raise ValueError("Historical candle numbers are invalid")
            values.append((mint, source, resolution_seconds, *(candle[field] for field in fields)))
        self.update_market_history_job(
            mint, source, resolution_seconds, status="running", before_at=before_at,
            error="", coverage=coverage, updated_at=updated_at,
        )
        with self._connection:
            self._connection.executemany("""
                INSERT INTO market_history_candles(
                    mint,source,resolution_seconds,opened_at,open_usd,high_usd,low_usd,close_usd,volume_usd
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(mint,source,resolution_seconds,opened_at) DO UPDATE SET
                    open_usd=excluded.open_usd,high_usd=excluded.high_usd,low_usd=excluded.low_usd,
                    close_usd=excluded.close_usd,volume_usd=excluded.volume_usd
            """, values)
            summary = self._connection.execute("""
                SELECT MIN(opened_at) AS earliest_at,MAX(opened_at) AS latest_at,COUNT(*) AS row_count
                FROM market_history_candles WHERE mint=? AND source=? AND resolution_seconds=?
            """, (mint, source, resolution_seconds)).fetchone()
            self._connection.execute("""
                UPDATE market_history_jobs SET status=?,before_at=?,earliest_at=?,latest_at=?,row_count=?,
                    updated_at=?,error=?,coverage=?
                WHERE mint=? AND source=? AND resolution_seconds=?
            """, (status, before_at, summary["earliest_at"], summary["latest_at"], summary["row_count"],
                  updated_at, error[:500], coverage[:500], mint, source, resolution_seconds))

    def coin_activity(self, mint: str, limit: int = 100) -> list[dict]:
        """Local monitor decisions only; these are not transaction receipts."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('Activity limit must be 1–500')
        return [dict(r) for r in self._connection.execute('''
            SELECT d.instance_id,i.label,d.at,d.kind,d.reason FROM decisions d
            JOIN instances i ON i.id=d.instance_id WHERE d.mint=?
            ORDER BY d.at DESC,d.id DESC LIMIT ?''', (mint, limit))]

    def instance(self, instance_id: str) -> dict[str, object]:
        row = self._connection.execute("""
            SELECT instances.*, strategies.package_json, strategies.schema_name,
                   strategies.strategy_id, strategies.label AS strategy_label,
                   session_workspace_instances.workspace_id
            FROM instances JOIN strategies ON strategies.digest=instances.strategy_digest
            LEFT JOIN session_workspace_instances ON session_workspace_instances.instance_id=instances.id
            WHERE instances.id=? AND NOT EXISTS (
                SELECT 1 FROM removed_instances WHERE removed_instances.instance_id=instances.id
            )
        """, (instance_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown strategy instance")
        value = dict(row)
        value["package"] = json.loads(value.pop("package_json"))
        return value

    def set_instance_status(self, instance_id: str, status: str) -> None:
        if status not in ("monitoring", "paused"):
            raise ValueError("Instance status must be monitoring or paused")
        if status == 'monitoring' and self.instance(instance_id)['entry_mode'] != 'disabled':
            from .session_funding import require_ready
            require_ready(self, instance_id)
        with self._connection:
            cursor = self._connection.execute("UPDATE instances SET status=? WHERE id=?", (status, instance_id))
            if cursor.rowcount != 1:
                raise ValueError("Unknown strategy instance")
            self._connection.execute("""INSERT INTO monitor_settings(instance_id,revision) VALUES (?,1)
                ON CONFLICT(instance_id) DO UPDATE SET revision=revision+1,next_at=0""", (instance_id,))

    def append_observations(self, instance_id: str, observations: list[dict[str, object]]) -> int:
        rows = [(instance_id, str(item["mint"]), float(item["observed_at"]),
                 json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                for item in observations]
        with self._connection:
            cursor = self._connection.executemany("""
                INSERT OR IGNORE INTO observations(instance_id,mint,observed_at,payload_json)
                VALUES (?,?,?,?)
            """, rows)
        return cursor.rowcount

    def recent_observations(self, instance_id: str, after: float = 0) -> list[dict[str, object]]:
        rows = self._connection.execute("""
            SELECT payload_json FROM observations WHERE instance_id=? AND observed_at>=?
            ORDER BY observed_at,mint
        """, (instance_id, after))
        return [json.loads(row["payload_json"]) for row in rows]

    def latest_observations(self, instance_id: str, limit: int = 12) -> list[dict[str, object]]:
        """Return only the newest retained quote for each observed mint."""
        self.instance(instance_id)
        if not 1 <= limit <= 100:
            raise ValueError("Latest-observation limit must be between 1 and 100")
        rows = self._connection.execute("""
            SELECT current.payload_json FROM observations AS current
            WHERE current.instance_id=? AND current.id=(
                SELECT newest.id FROM observations AS newest
                WHERE newest.instance_id=current.instance_id AND newest.mint=current.mint
                ORDER BY newest.observed_at DESC,newest.id DESC LIMIT 1
            )
            ORDER BY current.observed_at DESC,current.id DESC LIMIT ?
        """, (instance_id, limit))
        return [json.loads(row["payload_json"]) for row in rows]

    def record_decisions(self, instance_id: str, decisions: list[dict[str, object]]) -> int:
        rows = []
        for decision in decisions:
            payload = json.dumps(decision.get("payload", {}), ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":"))
            key = "|".join((str(decision["kind"]), str(decision["mint"]),
                            str(decision["at"]), str(decision["reason"]), payload))
            rows.append((instance_id, key, float(decision["at"]), str(decision["kind"]),
                         str(decision["mint"]), str(decision["reason"]), payload))
        with self._connection:
            cursor = self._connection.executemany("""
                INSERT OR IGNORE INTO decisions(instance_id,decision_key,at,kind,mint,reason,payload_json)
                VALUES (?,?,?,?,?,?,?)
            """, rows)
        return cursor.rowcount

    def decisions(self, instance_id: str, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 1000:
            raise ValueError("Decision limit must be between 1 and 1000")
        rows = self._connection.execute("""
            SELECT decision_key,at,kind,mint,reason,payload_json FROM decisions
            WHERE instance_id=? ORDER BY at DESC,id DESC LIMIT ?
        """, (instance_id, limit))
        return [dict(decision_key=row["decision_key"], at=row["at"], kind=row["kind"],
                     mint=row["mint"], reason=row["reason"],
                     payload=json.loads(row["payload_json"])) for row in rows]

    def entry_activity(self, after: int | None = None, instance_ids: set[str] | None = None) -> dict[str, object]:
        """Read durable entry decisions in insertion order without losing bursts.

        The first page is the latest 100 records. Follow-up pages use journal
        IDs, not decision timestamps, so delayed observations remain visible.
        """
        if after is not None and (type(after) is not int or not 0 <= after <= 2**63 - 1):
            raise ValueError("Activity cursor must be a non-negative journal ID")
        direction = "DESC" if after is None else "ASC"
        scope_sql = "" if instance_ids is None else " AND i.id IN (SELECT value FROM json_each(?))"
        params = (after or 0,) if instance_ids is None else (after or 0, json.dumps(sorted(instance_ids)))
        rows = self._connection.execute(f"""
            SELECT d.id,d.decision_key,d.instance_id,d.at,d.kind,d.mint,d.reason,
                   d.payload_json,i.label,w.label AS workspace_label
            FROM decisions d JOIN instances i ON i.id=d.instance_id
            LEFT JOIN session_workspace_instances a ON a.instance_id=i.id
            LEFT JOIN session_workspaces w ON w.id=a.workspace_id
            WHERE d.kind IN ('entry_recommended','entry_blocked') AND d.id>?
              AND NOT EXISTS (SELECT 1 FROM removed_instances r WHERE r.instance_id=i.id)
            {scope_sql}
            ORDER BY d.id {direction} LIMIT 101
        """, params).fetchall()
        more = after is not None and len(rows) > 100
        rows = rows[:100]
        if after is None:
            rows.reverse()
        result = []
        for row in rows:
            detail = json.loads(row["payload_json"])
            result.append({
                "id": "decision:" + hashlib.sha256(f"{row['instance_id']}:{row['decision_key']}".encode()).hexdigest(),
                "session_id": row["instance_id"], "strategy": row["label"],
                "workspace": row["workspace_label"], "at": row["at"],
                "kind": row["kind"], "mint": row["mint"], "reason": row["reason"],
                "symbol": str(detail.get("symbol") or "")[:80],
            })
        return {"rows": result, "cursor": rows[-1]["id"] if rows else after or 0,
                "has_more": more}

    def entry_considerations(self, instance_id: str, limit: int = 12) -> list[dict[str, object]]:
        """Return each coin's latest durable enter / do-not-enter stance.

        The attached observation is the newest saved quote at or before that
        decision, so the UI never presents a later quote as the evidence that
        produced an earlier decision. Full observation and decision history
        remains available through the session-detail APIs.
        """
        self.instance(instance_id)
        if not 1 <= limit <= 100:
            raise ValueError("Entry-consideration limit must be between 1 and 100")
        rows = self._connection.execute("""
            SELECT decision.at,decision.kind,decision.mint,decision.reason,
                   decision.payload_json,
                   (SELECT observation.payload_json FROM observations AS observation
                    WHERE observation.instance_id=decision.instance_id
                      AND observation.mint=decision.mint
                      AND observation.observed_at<=decision.at
                    ORDER BY observation.observed_at DESC,observation.id DESC LIMIT 1
                   ) AS observation_json
            FROM decisions AS decision
            WHERE decision.instance_id=?
              AND decision.kind IN ('entry_recommended','entry_blocked')
              AND decision.id=(
                  SELECT newest.id FROM decisions AS newest
                  WHERE newest.instance_id=decision.instance_id
                    AND newest.mint=decision.mint
                    AND newest.kind IN ('entry_recommended','entry_blocked')
                  ORDER BY newest.at DESC,newest.id DESC LIMIT 1
              )
            ORDER BY decision.at DESC,decision.id DESC LIMIT ?
        """, (instance_id, limit))
        return [dict(
            at=row["at"], kind=row["kind"], mint=row["mint"], reason=row["reason"],
            payload=json.loads(row["payload_json"]),
            observation=json.loads(row["observation_json"]) if row["observation_json"] else None,
        ) for row in rows]

    def score_decision_outcomes(self, instance_id: str, *, now: float | None = None) -> int:
        """Score matured decisions from retained quotes without fetching or filling gaps."""
        self.instance(instance_id)
        scored_at = time.time() if now is None else _positive_number(now, "Outcome score time")
        with self._connection:
            return self._score_decision_outcomes(instance_id, scored_at)

    def _score_decision_outcomes(self, instance_id: str, scored_at: float) -> int:
        inserted = 0
        decisions = self._connection.execute("""
            SELECT decision_key,at,kind,mint,reason FROM decisions
            WHERE instance_id=? AND at<=?
              AND NOT EXISTS (
                  SELECT 1 FROM evaluated_opportunities AS opportunity
                  WHERE opportunity.instance_id=decisions.instance_id
                    AND opportunity.at=decisions.at
                    AND opportunity.mint=decisions.mint
                    AND (CASE opportunity.eligible
                         WHEN 1 THEN 'entry_recommended' ELSE 'entry_blocked' END)=decisions.kind
              )
            UNION ALL
            SELECT decision_key,at,
                   CASE eligible WHEN 1 THEN 'entry_recommended' ELSE 'entry_blocked' END,
                   mint,reason
            FROM evaluated_opportunities WHERE instance_id=? AND at<=?
            ORDER BY at,decision_key
        """, (
            instance_id, scored_at - min(DECISION_OUTCOME_HORIZONS) - DECISION_OUTCOME_GRACE,
            instance_id, scored_at - min(DECISION_OUTCOME_HORIZONS) - DECISION_OUTCOME_GRACE,
        )).fetchall()
        for decision in decisions:
            for horizon in DECISION_OUTCOME_HORIZONS:
                due_at = float(decision["at"]) + horizon
                if scored_at < due_at + DECISION_OUTCOME_GRACE:
                    continue
                exists = self._connection.execute("""
                    SELECT 1 FROM decision_outcomes
                    WHERE instance_id=? AND decision_key=? AND horizon_seconds=?
                """, (instance_id, decision["decision_key"], horizon)).fetchone()
                if exists:
                    continue
                outcome = self._decision_outcome(instance_id, decision, horizon, due_at, scored_at)
                self._connection.execute("""
                    INSERT INTO decision_outcomes(instance_id,decision_key,horizon_seconds,due_at,
                        scored_at,status,payload_json) VALUES (?,?,?,?,?,?,?)
                """, (instance_id, decision["decision_key"], horizon, due_at, scored_at,
                      outcome["status"], json.dumps(
                          outcome, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
                inserted += 1
        return inserted

    def _decision_outcome(self, instance_id: str, decision: sqlite3.Row, horizon: int,
                          due_at: float, scored_at: float) -> dict[str, object]:
        common = {
            "decision_at": float(decision["at"]), "decision_kind": str(decision["kind"]),
            "decision_reason": str(decision["reason"]), "mint": str(decision["mint"]),
            "horizon_seconds": horizon, "due_at": due_at, "scored_at": scored_at,
            "basis": "counterfactual_retained_quotes",
        }
        baseline_row = self._connection.execute("""
            SELECT observed_at,payload_json FROM observations
            WHERE instance_id=? AND mint=? AND observed_at BETWEEN ? AND ?
            ORDER BY abs(observed_at-?),id DESC LIMIT 1
        """, (instance_id, decision["mint"], float(decision["at"]) - DECISION_OUTCOME_GRACE,
              float(decision["at"]) + DECISION_OUTCOME_GRACE, float(decision["at"]))).fetchone()
        endpoint_row = self._connection.execute("""
            SELECT observed_at,payload_json FROM observations
            WHERE instance_id=? AND mint=? AND observed_at BETWEEN ? AND ?
            ORDER BY abs(observed_at-?),id DESC LIMIT 1
        """, (instance_id, decision["mint"], due_at - DECISION_OUTCOME_GRACE,
              due_at + DECISION_OUTCOME_GRACE, due_at)).fetchone()
        if baseline_row is None or endpoint_row is None:
            missing = "baseline" if baseline_row is None else "horizon"
            return {**common, "status": "unknown", "coverage": f"missing_{missing}_quote"}
        try:
            baseline = json.loads(baseline_row["payload_json"])
            endpoint = json.loads(endpoint_row["payload_json"])
            baseline_price = _positive_number(baseline.get("price_usd"), "Baseline price")
            endpoint_price = _positive_number(endpoint.get("price_usd"), "Outcome price")
        except (ValueError, TypeError, json.JSONDecodeError):
            return {**common, "status": "unknown", "coverage": "invalid_price_evidence"}
        if ((baseline.get("route"), baseline.get("pool")) !=
                (endpoint.get("route"), endpoint.get("pool"))):
            return {**common, "status": "unknown", "coverage": "route_or_pool_changed"}
        rows = self._connection.execute("""
            SELECT observed_at,payload_json FROM observations
            WHERE instance_id=? AND mint=? AND observed_at BETWEEN ? AND ?
            ORDER BY observed_at,id
        """, (instance_id, decision["mint"], baseline_row["observed_at"],
              endpoint_row["observed_at"])).fetchall()
        samples: list[tuple[float, float]] = []
        try:
            for row in rows:
                payload = json.loads(row["payload_json"])
                if (payload.get("route"), payload.get("pool")) != (baseline.get("route"), baseline.get("pool")):
                    return {**common, "status": "unknown", "coverage": "route_or_pool_changed"}
                samples.append((float(row["observed_at"]), _positive_number(payload.get("price_usd"), "Sample price")))
        except (ValueError, TypeError, json.JSONDecodeError):
            return {**common, "status": "unknown", "coverage": "invalid_price_evidence"}
        if len(samples) < 2 or any(right[0] - left[0] > 180 for left, right in zip(samples, samples[1:])):
            return {**common, "status": "unknown", "coverage": "discontinuous_quotes",
                    "sample_count": len(samples)}
        prices = [price for _, price in samples]
        raw_return = endpoint_price / baseline_price - 1
        exit_decision = decision["kind"] in ("exit_recommended", "partial_exit_recommended")
        return {
            **common, "status": "scored", "coverage": "continuous", "sample_count": len(samples),
            "baseline_observed_at": float(baseline_row["observed_at"]),
            "outcome_observed_at": float(endpoint_row["observed_at"]),
            "baseline_price_usd": baseline_price, "outcome_price_usd": endpoint_price,
            "price_return_fraction": raw_return,
            "decision_return_fraction": -raw_return if exit_decision else raw_return,
            "maximum_favorable_fraction": max(prices) / baseline_price - 1,
            "maximum_adverse_fraction": min(prices) / baseline_price - 1,
            "interpretation": "avoided_price_move" if exit_decision else "hypothetical_long_move",
        }

    def decision_outcomes(self, instance_id: str, limit: int = 100) -> list[dict[str, object]]:
        self.instance(instance_id)
        if not 1 <= limit <= 1000:
            raise ValueError("Decision-outcome limit must be between 1 and 1000")
        return [json.loads(row["payload_json"]) for row in self._connection.execute("""
            SELECT payload_json FROM decision_outcomes WHERE instance_id=?
            ORDER BY scored_at DESC,due_at DESC,decision_key LIMIT ?
        """, (instance_id, limit))]

    def session_review_snapshot(self, instance_id: str, *,
                                observation_limit: int = 20_000,
                                decision_limit: int = 5_000) -> dict[str, object]:
        """Read one bounded, internally consistent evidence snapshot for offline review."""
        if not 100 <= observation_limit <= 100_000:
            raise ValueError("Session-review observation limit must be 100–100000")
        if not 1 <= decision_limit <= 20_000:
            raise ValueError("Session-review decision limit must be 1–20000")
        with self._connection:
            self._connection.execute("BEGIN")
            instance = self.instance(instance_id)
            strategy_digest = str(instance["strategy_digest"])
            cycles = list(self._connection.execute("""
                SELECT * FROM opportunity_capture_cycles
                WHERE instance_id=? AND strategy_digest=?
                ORDER BY first_observed_at,finished_at,cycle_key
            """, (instance_id, strategy_digest)))
            capture_start = min(
                (float(row["first_observed_at"]) for row in cycles
                 if row["first_observed_at"] is not None),
                default=None,
            )
            if capture_start is None:
                observation_count = self._connection.execute(
                    "SELECT COUNT(*) AS total FROM observations WHERE instance_id=?",
                    (instance_id,),
                ).fetchone()["total"]
                observation_rows = self._connection.execute("""
                    SELECT payload_json FROM (
                        SELECT observed_at,mint,id,payload_json FROM observations
                        WHERE instance_id=? ORDER BY observed_at DESC,id DESC LIMIT ?
                    ) ORDER BY observed_at,mint,id
                """, (instance_id, observation_limit)).fetchall()
            else:
                observation_count = self._connection.execute("""
                    SELECT COUNT(*) AS total FROM observations
                    WHERE instance_id=? AND observed_at>=?
                """, (instance_id, capture_start)).fetchone()["total"]
                observation_rows = self._connection.execute("""
                    SELECT payload_json FROM (
                        SELECT observed_at,mint,id,payload_json FROM observations
                        WHERE instance_id=? AND observed_at>=?
                        ORDER BY observed_at DESC,id DESC LIMIT ?
                    ) ORDER BY observed_at,mint,id
                """, (instance_id, capture_start, observation_limit)).fetchall()
            observations = [
                {**json.loads(row["payload_json"]), "instance_id": instance_id}
                for row in observation_rows
            ]
            if not observations:
                raise ValueError("Session review needs retained observations")
            first_at = min(float(row["observed_at"]) for row in observations)
            last_at = max(float(row["observed_at"]) for row in observations)

            opportunity_rows = self._connection.execute("""
                SELECT * FROM (
                    SELECT * FROM evaluated_opportunities
                    WHERE instance_id=? AND at BETWEEN ? AND ? AND strategy_digest=?
                    ORDER BY at DESC,evaluation_key DESC LIMIT ?
                ) ORDER BY at,evaluation_key
            """, (instance_id, first_at, last_at, strategy_digest, decision_limit)).fetchall()
            opportunity_count = self._connection.execute("""
                SELECT COUNT(*) AS total FROM evaluated_opportunities
                WHERE instance_id=? AND at BETWEEN ? AND ? AND strategy_digest=?
            """, (instance_id, first_at, last_at, strategy_digest)).fetchone()["total"]
            if opportunity_rows:
                decisions = [{
                    "instance_id": instance_id,
                    "decision_key": row["decision_key"],
                    "at": row["at"],
                    "kind": "entry_recommended" if row["eligible"] else "entry_blocked",
                    "mint": row["mint"],
                    "reason": row["reason"],
                    "payload": json.loads(row["payload_json"]),
                } for row in opportunity_rows]
                decision_count = int(opportunity_count)
            else:
                transition_count = self._connection.execute("""
                    SELECT COUNT(*) AS total FROM decisions
                    WHERE instance_id=? AND at BETWEEN ? AND ?
                """, (instance_id, first_at, last_at)).fetchone()["total"]
                transition_rows = self._connection.execute("""
                    SELECT * FROM (
                        SELECT id,decision_key,at,kind,mint,reason,payload_json FROM decisions
                        WHERE instance_id=? AND at BETWEEN ? AND ?
                        ORDER BY at DESC,id DESC LIMIT ?
                    ) ORDER BY at,id
                """, (instance_id, first_at, last_at, decision_limit)).fetchall()
                decisions = [{
                    "instance_id": instance_id,
                    "decision_key": row["decision_key"],
                    "at": row["at"],
                    "kind": row["kind"],
                    "mint": row["mint"],
                    "reason": row["reason"],
                    "payload": json.loads(row["payload_json"]),
                } for row in transition_rows]
                decision_count = int(transition_count)

            decision_keys = {str(row["decision_key"]) for row in decisions}
            feature_records = []
            for row in self._connection.execute("""
                SELECT decision_key,payload_json FROM decision_features
                WHERE instance_id=? AND captured_at BETWEEN ? AND ?
                ORDER BY captured_at,decision_key
            """, (instance_id, first_at, last_at + DECISION_OUTCOME_GRACE)):
                if row["decision_key"] in decision_keys:
                    payload = json.loads(row["payload_json"])
                    payload["instance_id"] = instance_id
                    feature_records.append(payload)
            outcomes = []
            for row in self._connection.execute("""
                SELECT decision_key,payload_json FROM decision_outcomes
                WHERE instance_id=? ORDER BY scored_at,decision_key,horizon_seconds
            """, (instance_id,)):
                if row["decision_key"] in decision_keys:
                    payload = json.loads(row["payload_json"])
                    payload["instance_id"] = instance_id
                    payload["decision_key"] = row["decision_key"]
                    outcomes.append(payload)
            relevant_cycles = [row for row in cycles
                               if row["last_observed_at"] is not None
                               and float(row["last_observed_at"]) >= first_at
                               and float(row["first_observed_at"]) <= last_at]
            decision_capture_complete = bool(
                opportunity_rows
                and opportunity_count == len(opportunity_rows)
                and relevant_cycles
                and all(bool(row["complete"]) for row in relevant_cycles)
                and len(feature_records) == len(decisions)
                and min(float(row["first_observed_at"]) for row in relevant_cycles) <= first_at
                and max(float(row["last_observed_at"]) for row in relevant_cycles) >= last_at
            )
            return {
                "source_session_id": instance_id,
                "strategy_digest": strategy_digest,
                "strategy_package": instance["package"],
                "first_at": first_at,
                "last_at": last_at,
                "observations": observations,
                "decisions": decisions,
                "outcomes": outcomes,
                "feature_records": feature_records,
                "decision_capture_complete": decision_capture_complete,
                "source_counts": {
                    "observations": int(observation_count),
                    "decisions": decision_count,
                    "features": len(feature_records),
                    "outcomes": len(outcomes),
                },
            }

    def latest_actionable_decision(self, instance_id: str, mint: str) -> dict[str, object]:
        """Return the newest decision for a mint only when it is still actionable."""
        self.instance(instance_id)
        row = self._connection.execute("""
            SELECT decision_key,at,kind,mint,reason,payload_json FROM decisions
            WHERE instance_id=? AND mint=? ORDER BY at DESC,id DESC LIMIT 1
        """, (instance_id, mint)).fetchone()
        if row is None:
            raise ValueError("No local decision exists for this mint")
        if row["kind"] not in ("entry_recommended", "exit_recommended", "partial_exit_recommended"):
            raise ValueError("The newest local decision for this mint is not actionable")
        return {
            "decision_key": row["decision_key"], "at": row["at"], "kind": row["kind"],
            "mint": row["mint"], "reason": row["reason"], "payload": json.loads(row["payload_json"]),
        }

    def latest_observation(self, instance_id: str, mint: str) -> dict[str, object]:
        self.instance(instance_id)
        row = self._connection.execute("""
            SELECT payload_json FROM observations WHERE instance_id=? AND mint=?
            ORDER BY observed_at DESC,id DESC LIMIT 1
        """, (instance_id, mint)).fetchone()
        if row is None:
            raise ValueError("No local market observation exists for this mint")
        return json.loads(row["payload_json"])

    def record_monitor_run(self, instance_id: str, *, started_at: float, finished_at: float,
                           status: str, observations: int = 0, decisions: int = 0,
                           detail: str = "") -> None:
        self.instance(instance_id)
        if status not in ("completed", "failed"):
            raise ValueError("Monitor run status must be completed or failed")
        started = _finite_number(started_at, "Monitor start time")
        finished = _finite_number(finished_at, "Monitor finish time")
        if finished < started:
            raise ValueError("Monitor finish time cannot precede its start time")
        if isinstance(observations, bool) or not isinstance(observations, int) or observations < 0:
            raise ValueError("Observation count must be a non-negative integer")
        if isinstance(decisions, bool) or not isinstance(decisions, int) or decisions < 0:
            raise ValueError("Decision count must be a non-negative integer")
        if not isinstance(detail, str):
            raise ValueError("Monitor detail must be text")
        with self._connection:
            self._connection.execute("""
                INSERT INTO monitor_runs(instance_id,started_at,finished_at,status,observations,decisions,detail)
                VALUES (?,?,?,?,?,?,?)
            """, (instance_id, started, finished, status, observations, decisions, detail[:500]))

    def monitor_runs(self, instance_id: str, limit: int = 100) -> list[dict[str, object]]:
        self.instance(instance_id)
        if not 1 <= limit <= 1000:
            raise ValueError("Monitor-run limit must be between 1 and 1000")
        rows = self._connection.execute("""
            SELECT started_at,finished_at,status,observations,decisions,detail
            FROM monitor_runs WHERE instance_id=? ORDER BY finished_at DESC,id DESC LIMIT ?
        """, (instance_id, limit))
        return [dict(row) for row in rows]

    def monitor_health(self, instance_id: str) -> dict[str, object]:
        instance = self.instance(instance_id)
        row = self._connection.execute("""
            SELECT finished_at,status,observations,decisions,detail
            FROM monitor_runs WHERE instance_id=? ORDER BY finished_at DESC,id DESC LIMIT 1
        """, (instance_id,)).fetchone()
        health: dict[str, object] = {"instance_id": instance_id, "status": instance["status"]}
        if row is None:
            health["last_run"] = "never"
        else:
            health["last_run"] = dict(row)
        return health

    def record_ownership(self, instance_id: str, evidence: dict[str, object]) -> None:
        self.instance(instance_id)
        mint = evidence.get("mint")
        checked_at = evidence.get("checked_at")
        policy_id = evidence.get("policy_id")
        status = evidence.get("status")
        if not isinstance(mint, str) or not mint or len(mint) > 100:
            raise ValueError("Ownership evidence requires a mint")
        checked = _finite_number(checked_at, "Ownership check time")
        if not isinstance(policy_id, str) or not policy_id or len(policy_id) > 160:
            raise ValueError("Ownership evidence requires a policy identifier")
        if status not in ("clear", "blocked", "unknown"):
            raise ValueError("Ownership evidence has an invalid status")
        with self._connection:
            self._connection.execute("""
                INSERT OR IGNORE INTO ownership_checks(instance_id,mint,checked_at,policy_id,status,payload_json)
                VALUES (?,?,?,?,?,?)
            """, (instance_id, mint, checked, policy_id, status,
                  json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))

    def latest_ownership(self, instance_id: str, mint: str) -> dict[str, object] | None:
        row = self._connection.execute("""
            SELECT payload_json FROM ownership_checks WHERE instance_id=? AND mint=?
            ORDER BY checked_at DESC,id DESC LIMIT 1
        """, (instance_id, mint)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def ownership_history(self, instance_id: str, mint: str | None = None,
                          limit: int = 100) -> list[dict[str, object]]:
        self.instance(instance_id)
        if not 1 <= limit <= 1000:
            raise ValueError("Ownership-history limit must be between 1 and 1000")
        if mint is None:
            rows = self._connection.execute("""
                SELECT payload_json FROM ownership_checks WHERE instance_id=?
                ORDER BY checked_at DESC,id DESC LIMIT ?
            """, (instance_id, limit))
        else:
            rows = self._connection.execute("""
                SELECT payload_json FROM ownership_checks WHERE instance_id=? AND mint=?
                ORDER BY checked_at DESC,id DESC LIMIT ?
            """, (instance_id, mint, limit))
        return [json.loads(row["payload_json"]) for row in rows]

    def record_wallet_check(self, instance_id: str, snapshot: dict[str, object]) -> None:
        """Store one user-requested public-address snapshot without wallet credentials."""
        self.instance(instance_id)
        address = snapshot.get("address")
        checked_at = snapshot.get("checked_at")
        slot = snapshot.get("slot")
        lamports = snapshot.get("sol_lamports")
        if not isinstance(address, str) or not address or len(address) > 100:
            raise ValueError("Wallet snapshot requires an address")
        checked = _finite_number(checked_at, "Wallet check time")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
            raise ValueError("Wallet snapshot requires a non-negative slot")
        if not isinstance(lamports, str) or not lamports.isdecimal():
            raise ValueError("Wallet snapshot requires a lamport balance")
        with self._connection:
            self._connection.execute("""
                INSERT OR IGNORE INTO wallet_checks(instance_id,address,checked_at,slot,sol_lamports,payload_json)
                VALUES (?,?,?,?,?,?)
            """, (instance_id, address, checked, slot, lamports,
                  json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))

    def wallet_history(self, instance_id: str, address: str | None = None,
                       limit: int = 100) -> list[dict[str, object]]:
        self.instance(instance_id)
        if not 1 <= limit <= 1000:
            raise ValueError("Wallet-history limit must be between 1 and 1000")
        if address is None:
            rows = self._connection.execute("""
                SELECT payload_json FROM wallet_checks WHERE instance_id=?
                ORDER BY checked_at DESC,id DESC LIMIT ?
            """, (instance_id, limit))
        else:
            rows = self._connection.execute("""
                SELECT payload_json FROM wallet_checks WHERE instance_id=? AND address=?
                ORDER BY checked_at DESC,id DESC LIMIT ?
            """, (instance_id, address, limit))
        return [json.loads(row["payload_json"]) for row in rows]

    def recent_wallet_snapshots(self, limit: int = 100) -> list[dict[str, object]]:
        """Read retained public-address snapshots across sessions without an RPC call."""
        if not 1 <= limit <= 1000:
            raise ValueError("Wallet snapshot limit must be between 1 and 1000")
        rows = self._connection.execute("""
            SELECT instance_id,payload_json FROM wallet_checks
            ORDER BY checked_at DESC,id DESC LIMIT ?
        """, (limit,))
        return [dict(instance_id=row["instance_id"], snapshot=json.loads(row["payload_json"])) for row in rows]

    def set_executor_config(self, instance_id: str, *, mode: str, max_order_usd: float,
                            max_slippage_bps: int, decision_ttl_seconds: float, updated_at: float) -> None:
        self.instance(instance_id)
        if mode not in ("disabled", "dry-run"):
            raise ValueError("Only disabled and dry-run executor modes are available")
        maximum = _positive_number(max_order_usd, "Executor maximum order")
        if isinstance(max_slippage_bps, bool) or not isinstance(max_slippage_bps, int) or not 0 <= max_slippage_bps <= 2500:
            raise ValueError("Executor slippage must be an integer from 0 to 2500 basis points")
        ttl = _finite_number(decision_ttl_seconds, "Executor decision TTL")
        if not 5 <= ttl <= 600:
            raise ValueError("Executor decision TTL must be between 5 and 600 seconds")
        updated = _finite_number(updated_at, "Executor configuration time")
        with self._connection:
            self._connection.execute("""
                INSERT INTO executor_configs(instance_id,updated_at,mode,max_order_usd,max_slippage_bps,decision_ttl_seconds)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(instance_id) DO UPDATE SET
                  updated_at=excluded.updated_at,mode=excluded.mode,max_order_usd=excluded.max_order_usd,
                  max_slippage_bps=excluded.max_slippage_bps,decision_ttl_seconds=excluded.decision_ttl_seconds
            """, (instance_id, updated, mode, _decimal_text(maximum), max_slippage_bps, ttl))

    def executor_config(self, instance_id: str) -> dict[str, object] | None:
        self.instance(instance_id)
        row = self._connection.execute("SELECT * FROM executor_configs WHERE instance_id=?", (instance_id,)).fetchone()
        return dict(row) if row else None

    def create_order(self, instance_id: str, *, decision_key: str, decision_at: float, created_at: float,
                     side: str, mint: str, symbol: str, route: str, position_id: str | None,
                     max_input_usd: float | None, token_amount: float, minimum_output_amount: float | None,
                     plan: dict[str, object]) -> str:
        self.instance(instance_id)
        if side not in ("buy", "sell") or not all(isinstance(value, str) and value for value in (decision_key, mint, symbol, route)):
            raise ValueError("Order fields are incomplete")
        decision_time = _finite_number(decision_at, "Order decision time")
        created = _finite_number(created_at, "Order creation time")
        if created < decision_time:
            raise ValueError("Order cannot precede its decision")
        if position_id is not None and (not isinstance(position_id, str) or not position_id):
            raise ValueError("Order position reference is invalid")
        amount = _positive_number(token_amount, "Order token amount")
        maximum = _positive_number(max_input_usd, "Order maximum input") if max_input_usd is not None else None
        minimum = _positive_number(minimum_output_amount, "Order minimum output") if minimum_output_amount is not None else None
        order_id = uuid.uuid4().hex
        with self._connection:
            try:
                self._connection.execute("""
                    INSERT INTO orders(id,instance_id,decision_key,decision_at,created_at,updated_at,status,side,mint,
                                       symbol,route,position_id,max_input_usd,token_amount,minimum_output_amount,plan_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (order_id, instance_id, decision_key, decision_time, created, created, "draft", side, mint,
                      symbol, route, position_id, _decimal_text(maximum) if maximum is not None else None,
                      _decimal_text(amount), _decimal_text(minimum) if minimum is not None else None,
                      json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
            except sqlite3.IntegrityError as exc:
                raise ValueError("This decision already has a local order") from exc
            self._record_order_event(instance_id, order_id, created, "prepared", plan)
        return order_id

    def order(self, order_id: str) -> dict[str, object]:
        row = self._connection.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown local order")
        value = dict(row)
        value["plan"] = json.loads(value.pop("plan_json"))
        return value

    def orders(self, instance_id: str, limit: int = 100) -> list[dict[str, object]]:
        self.instance(instance_id)
        if not 1 <= limit <= 1000:
            raise ValueError("Order limit must be between 1 and 1000")
        rows = self._connection.execute("""
            SELECT * FROM orders WHERE instance_id=? ORDER BY created_at DESC,id DESC LIMIT ?
        """, (instance_id, limit))
        result = []
        for row in rows:
            value = dict(row)
            value["plan"] = json.loads(value.pop("plan_json"))
            result.append(value)
        return result

    def transition_order(self, order_id: str, *, expected_status: str, status: str,
                         at: float, event_kind: str, payload: dict[str, object]) -> dict[str, object]:
        if expected_status not in ("draft", "simulated") or status not in ("simulated", "cancelled"):
            raise ValueError("Unsupported local order state transition")
        changed = _finite_number(at, "Order transition time")
        with self._connection:
            row = self._connection.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown local order")
            if row["status"] != expected_status:
                raise ValueError(f"Order is {row['status']}, not {expected_status}")
            if changed < float(row["updated_at"]):
                raise ValueError("Order transition cannot precede its prior state")
            self._connection.execute("UPDATE orders SET status=?,updated_at=? WHERE id=?", (status, changed, order_id))
            self._record_order_event(str(row["instance_id"]), order_id, changed, event_kind, payload)
        return self.order(order_id)

    def order_events(self, order_id: str, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 1000:
            raise ValueError("Order-event limit must be between 1 and 1000")
        self.order(order_id)
        rows = self._connection.execute("""
            SELECT at,kind,payload_json FROM order_events WHERE order_id=?
            ORDER BY at DESC,id DESC LIMIT ?
        """, (order_id, limit))
        return [dict(at=row["at"], kind=row["kind"], payload=json.loads(row["payload_json"])) for row in rows]

    def record_localnet_check(self, instance_id: str, result: dict[str, object]) -> None:
        """Retain public evidence of one explicitly invoked loopback signer check."""
        self.instance(instance_id)
        checked_at = _finite_number(result.get("checked_at"), "Localnet check time")
        sender = result.get("sender")
        recipient = result.get("recipient")
        lamports = result.get("lamports")
        signature = result.get("signature")
        if (not isinstance(sender, str) or not isinstance(recipient, str) or not isinstance(signature, str) or
                not sender or not recipient or not signature or isinstance(lamports, bool) or
                not isinstance(lamports, int) or lamports < 1):
            raise ValueError("Localnet check evidence is incomplete")
        if result.get("network") != "localnet" or result.get("status") != "confirmed":
            raise ValueError("Only confirmed localnet signer checks can be retained")
        with self._connection:
            self._connection.execute("""
                INSERT OR IGNORE INTO localnet_checks(
                    instance_id,checked_at,sender,recipient,lamports,signature,status,payload_json
                ) VALUES (?,?,?,?,?,?,?,?)
            """, (instance_id, checked_at, sender, recipient, str(lamports), signature, "confirmed",
                  json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))

    def localnet_checks(self, instance_id: str, limit: int = 100) -> list[dict[str, object]]:
        self.instance(instance_id)
        if not 1 <= limit <= 1000:
            raise ValueError("Localnet-check limit must be between 1 and 1000")
        rows = self._connection.execute("""
            SELECT payload_json FROM localnet_checks WHERE instance_id=?
            ORDER BY checked_at DESC,id DESC LIMIT ?
        """, (instance_id, limit))
        return [json.loads(row["payload_json"]) for row in rows]

    def record_position(self, instance_id: str, mint: str, symbol: str, *, quantity: float,
                        total_cost_usd: float, opened_at: float, management_mode: str = "manual") -> str:
        """Record a fill the user made outside this build's future executor.

        The position ledger deliberately needs an explicit user-reported fill;
        a recommendation can never silently turn into a position.
        """
        self.instance(instance_id)
        if not isinstance(mint, str) or not mint.strip() or len(mint) > 100:
            raise ValueError("Position mint must be 1–100 characters")
        if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 80:
            raise ValueError("Position symbol must be 1–80 characters")
        amount = _positive_number(quantity, "Position quantity")
        cost = _positive_number(total_cost_usd, "Position cost")
        opened = _finite_number(opened_at, "Position opening time")
        if management_mode not in ("manual", "automatic"):
            raise ValueError("Position management mode must be manual or automatic")
        position_id = uuid.uuid4().hex
        with self._connection:
            self._connection.execute("""
                INSERT INTO positions(id,instance_id,mint,symbol,status,management_mode,opened_at,
                                      initial_quantity,quantity,total_cost_usd,remaining_cost_usd)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (position_id, instance_id, mint.strip(), symbol.strip(), "open", management_mode, opened,
                  _decimal_text(amount), _decimal_text(amount), _decimal_text(cost), _decimal_text(cost)))
            self._record_position_event(instance_id, position_id, opened, "external_entry", {
                "mint": mint.strip(), "symbol": symbol.strip(), "quantity": amount,
                "total_cost_usd": cost, "management_mode": management_mode,
            })
        return position_id

    def record_position_exit(self, position_id: str, *, quantity: float, proceeds_usd: float,
                             exited_at: float, stages: tuple[int, ...] = ()) -> None:
        """Apply a user-confirmed partial or full exit to the local ledger."""
        amount = _positive_number(quantity, "Exit quantity")
        proceeds = _finite_number(proceeds_usd, "Exit proceeds")
        if proceeds < 0:
            raise ValueError("Exit proceeds cannot be negative")
        exited = _finite_number(exited_at, "Exit time")
        if any(not isinstance(stage, int) or stage < 0 for stage in stages):
            raise ValueError("Exit stages must be non-negative integers")
        row = self._connection.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown position")
        if row["status"] != "open":
            raise ValueError("Position is not open")
        held = float(row["quantity"])
        if amount > held * (1 + 1e-9):
            raise ValueError("Exit quantity exceeds the open position")
        remaining_cost = float(row["remaining_cost_usd"] or 0)
        allocated_cost = remaining_cost * min(1.0, amount / held)
        left = max(0.0, held - amount)
        stored_stages = json.loads(row["sold_stages_json"])
        all_stages = sorted(set(stored_stages) | set(stages))
        status = "closed" if left <= 1e-12 else "open"
        with self._connection:
            self._connection.execute("""
                UPDATE positions
                SET quantity=?, remaining_cost_usd=?, realized_proceeds_usd=?, sold_stages_json=?,
                    status=?,closed_at=?
                WHERE id=?
            """, (_decimal_text(left), _decimal_text(max(0.0, remaining_cost - allocated_cost)),
                  _decimal_text(float(row["realized_proceeds_usd"]) + proceeds),
                  json.dumps(all_stages, separators=(",", ":")), status,
                  exited if status == "closed" else None, position_id))
            self._record_position_event(str(row["instance_id"]), position_id, exited,
                                        "external_exit" if status == "closed" else "external_partial_exit", {
                                            "quantity": amount, "proceeds_usd": proceeds,
                                            "allocated_cost_usd": allocated_cost, "stages": all_stages,
                                        })

    def positions(self, instance_id: str, include_closed: bool = True) -> list[dict[str, object]]:
        self.instance(instance_id)
        query = "SELECT * FROM positions WHERE instance_id=?"
        if not include_closed:
            query += " AND status='open'"
        query += " ORDER BY opened_at DESC,id DESC"
        return [dict(row) for row in self._connection.execute(query, (instance_id,))]

    def position_views(self, instance_id: str) -> list[PositionView]:
        from .agent_execution import monitored_positions

        return [PositionView(position_id=str(row["id"]), mint=str(row["mint"]), symbol=str(row["symbol"]),
                             entry_at=float(row["opened_at"]), initial_quantity=float(row["initial_quantity"]),
                             quantity=float(row["quantity"]),
                             remaining_cost_usd=float(row["remaining_cost_usd"] or 0),
                             total_cost_usd=float(row["total_cost_usd"] or 0),
                             realized_proceeds_usd=float(row["realized_proceeds_usd"]),
                             sold_stages=tuple(json.loads(row["sold_stages_json"])),
                             peak_price_usd=float(row["peak_price_usd"]),
                             last_usable_at=float(row["last_usable_at"]),
                             **self._position_route(instance_id, row))
                for row in self.positions(instance_id, include_closed=False)] + monitored_positions(self,instance_id)

    def _position_route(self, instance_id: str, position: dict) -> dict:
        # Only contemporaneous observations can establish the original curve.
        row = self._connection.execute("""SELECT payload_json FROM observations
            WHERE instance_id=? AND mint=? AND observed_at BETWEEN ? AND ?
            ORDER BY observed_at DESC LIMIT 1""", (instance_id,position["mint"],
                float(position["opened_at"])-120,float(position["opened_at"]))).fetchone()
        if not row:
            return {}
        quote = MarketQuote.from_record(json.loads(row[0]))
        return {"entry_pool":quote.pool,"entry_supply":quote.supply} if quote.route == "curve" else {}

    def position_events(self, instance_id: str, limit: int = 100) -> list[dict[str, object]]:
        self.instance(instance_id)
        if not 1 <= limit <= 1000:
            raise ValueError("Position event limit must be between 1 and 1000")
        rows = self._connection.execute("""
            SELECT position_id,at,kind,payload_json FROM position_events
            WHERE instance_id=? ORDER BY at DESC,id DESC LIMIT ?
        """, (instance_id, limit))
        return [dict(position_id=row["position_id"], at=row["at"], kind=row["kind"],
                     payload=json.loads(row["payload_json"])) for row in rows]

    def mark_position_quotes(self, instance_id: str, quotes: list[MarketQuote]) -> None:
        """Persist only fresh marks for positions already in the user ledger."""
        quote_map = {quote.mint: quote for quote in quotes if quote.route != "unsupported"}
        rows = list(self._connection.execute("""
                SELECT id,mint,peak_price_usd FROM positions
                WHERE instance_id=? AND status='open'
            """, (instance_id,)))
        with self._connection:
            for row in rows:
                quote = quote_map.get(row["mint"])
                if quote is None:
                    continue
                self._connection.execute("""
                    UPDATE positions SET peak_price_usd=?,last_usable_at=? WHERE id=?
                """, (max(float(row["peak_price_usd"]), quote.price_usd), quote.observed_at, row["id"]))

    def _record_position_event(self, instance_id: str, position_id: str, at: float, kind: str,
                               payload: dict[str, object]) -> None:
        self._connection.execute("""
            INSERT INTO position_events(instance_id,position_id,at,kind,payload_json)
            VALUES (?,?,?,?,?)
        """, (instance_id, position_id, at, kind,
              json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))

    def _record_order_event(self, instance_id: str, order_id: str, at: float, kind: str,
                            payload: dict[str, object]) -> None:
        self._connection.execute("""
            INSERT INTO order_events(instance_id,order_id,at,kind,payload_json)
            VALUES (?,?,?,?,?)
        """, (instance_id, order_id, at, kind,
              json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))

    def summary(self) -> dict[str, int | str]:
        total = self._connection.execute("SELECT count(*) FROM strategies").fetchone()[0]
        compatible = self._connection.execute(
            "SELECT count(*) FROM strategies WHERE compatibility='monitoring-ready'").fetchone()[0]
        instances = self._connection.execute("""SELECT count(*) FROM instances
            WHERE NOT EXISTS (SELECT 1 FROM removed_instances WHERE removed_instances.instance_id=instances.id)""").fetchone()[0]
        positions = self._connection.execute("SELECT count(*) FROM positions WHERE status='open'").fetchone()[0]
        failed_runs = self._connection.execute("SELECT count(*) FROM monitor_runs WHERE status='failed'").fetchone()[0]
        ownership_checks = self._connection.execute("SELECT count(*) FROM ownership_checks").fetchone()[0]
        wallet_checks = self._connection.execute("SELECT count(*) FROM wallet_checks").fetchone()[0]
        orders = self._connection.execute("SELECT count(*) FROM orders").fetchone()[0]
        localnet_checks = self._connection.execute("SELECT count(*) FROM localnet_checks").fetchone()[0]
        return {"database": str(self.path), "schema_version": SCHEMA_VERSION,
                "strategies": total, "monitoring_ready_strategies": compatible,
                "instances": instances, "open_positions": positions, "failed_monitor_runs": failed_runs,
                "ownership_checks": ownership_checks, "wallet_checks": wallet_checks, "orders": orders,
                "localnet_checks": localnet_checks}


@contextmanager
def open_library(data_dir: Path) -> Iterator[StrategyLibrary]:
    library = StrategyLibrary(data_dir)
    try:
        yield library
    finally:
        library.close()


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(label + " must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(label + " must be finite")
    return number


def _positive_number(value: object, label: str) -> float:
    number = _finite_number(value, label)
    if number <= 0:
        raise ValueError(label + " must be greater than zero")
    return number


def _validated_session_brief(value: dict[str, object] | None,
                             strategy: dict[str, object]) -> dict[str, object]:
    package = strategy.get("package")
    configuration = package.get("configuration") if isinstance(package, dict) else None
    config = configuration if isinstance(configuration, dict) else {}
    default_budget = config.get("budget_usd", 500)
    if (isinstance(default_budget, bool) or not isinstance(default_budget, (int, float))
            or not math.isfinite(float(default_budget)) or float(default_budget) <= 0):
        default_budget = 500
    order_usd = config.get("order_usd", 0)
    if (isinstance(order_usd, bool) or not isinstance(order_usd, (int, float))
            or not math.isfinite(float(order_usd)) or float(order_usd) < 0):
        order_usd = 0
    positions = config.get("max_positions", 1)
    if type(positions) is not int or not 1 <= positions <= 100:
        positions = 1
    label = str(strategy.get("label") or strategy.get("strategy_label") or "this strategy")
    supplied = value is not None
    if value is None:
        budget = float(default_budget)
        value = {
            "goal": "collect-evidence",
            "intent": f"Observe {label} and retain evidence for later review.",
            "paper_budget_usd": budget,
            "max_session_loss_usd": min(budget, max(float(order_usd), budget * .2)),
            "review_after_seconds": 86_400,
        }
    fields = {"goal", "intent", "paper_budget_usd", "max_session_loss_usd", "review_after_seconds"}
    mode_fields = {"session_mode", "execution_style", "profit_management"}
    scope_fields = mode_fields | {"automation_scope"}
    custody_fields = scope_fields | {"custody_mode"}
    accepted = (
        fields,
        fields | {"preferences"},
        fields | mode_fields,
        fields | mode_fields | {"preferences"},
        fields | scope_fields,
        fields | scope_fields | {"preferences"},
        fields | custody_fields,
        fields | custody_fields | {"preferences"},
    )
    if not isinstance(value, dict) or set(value) not in accepted:
        raise ValueError("Session brief requires goal, intent, paper budget, loss threshold, and review window")
    session_mode = value.get("session_mode", "monitoring")
    execution_style = value.get("execution_style")
    profit_management = value.get("profit_management")
    automation_scope = value.get("automation_scope")
    custody_mode = value.get("custody_mode")
    if not isinstance(session_mode, str) or session_mode not in SESSION_MODES:
        raise ValueError("Choose monitoring or trade session mode")
    if session_mode == "monitoring":
        if (execution_style is not None or profit_management is not None
                or automation_scope is not None or custody_mode is not None):
            raise ValueError("Monitoring sessions cannot request trading execution or profit management")
        stored_mode = "monitoring"
    else:
        if not isinstance(execution_style, str) or execution_style not in SESSION_EXECUTION_STYLES:
            raise ValueError("Trading sessions require interactive or autopilot execution")
        if not isinstance(profit_management, str) or profit_management not in SESSION_PROFIT_MANAGEMENT:
            raise ValueError("Trading sessions require a supported profit-management policy")
        if execution_style == "interactive" and profit_management != "user-directed":
            raise ValueError("Interactive sessions require user-directed profit management")
        if execution_style == "interactive" and automation_scope is not None:
            raise ValueError("Interactive sessions cannot request automatic actions")
        if execution_style == "interactive" and custody_mode is not None:
            raise ValueError("Interactive sessions use connected-wallet custody")
        if execution_style == "autopilot":
            # A missing scope preserves the exact behavior of v2 clients: their
            # only Autopilot choice requested both entries and exits.
            automation_scope = automation_scope or "entries-and-exits"
            if automation_scope not in SESSION_AUTOMATION_SCOPES:
                raise ValueError("Choose entries-only, exits-only or entries-and-exits scope")
            if automation_scope == "entries-only":
                if profit_management != "user-directed":
                    raise ValueError("Automatic entries only requires user-directed exits")
            elif profit_management not in {"strategy-managed", "winner-handoff"}:
                raise ValueError("Autopilot sessions with automatic exits require strategy-managed exits or winner handoff")
            custody_mode = custody_mode or "main-wallet-browser"
            if custody_mode not in {"main-wallet-browser", "main-wallet-service"}:
                raise ValueError("Autopilot sessions require browser or local-service custody")
        stored_mode = "trade-" + execution_style
    goal = value["goal"]
    if not isinstance(goal, str) or goal not in SESSION_GOALS:
        raise ValueError("Choose a supported session goal")
    intent = value["intent"]
    if (not isinstance(intent, str) or not intent.strip() or len(intent.strip()) > 500
            or any(ord(character) < 32 and character not in "\n\t" for character in intent)):
        raise ValueError("Session intent must be 1–500 printable characters")
    budget = _positive_number(value["paper_budget_usd"], "Paper research budget")
    loss = _positive_number(value["max_session_loss_usd"], "Session loss review threshold")
    if budget > 1_000_000_000:
        raise ValueError("Paper research budget is too large")
    if supplied and budget < float(order_usd):
        raise ValueError("Paper research budget must cover at least one configured strategy order")
    if loss > budget:
        raise ValueError("Session loss review threshold cannot exceed the paper research budget")
    review_seconds = value["review_after_seconds"]
    if type(review_seconds) is not int or not 900 <= review_seconds <= 2_592_000:
        raise ValueError("Session review window must be between 15 minutes and 30 days")
    preferences = value.get("preferences")
    if preferences is not None:
        preference_fields = {
            "risk_tolerance", "reward_risk_ratio", "investment_style", "time_horizon",
        }
        if not isinstance(preferences, dict) or set(preferences) != preference_fields:
            raise ValueError("Session preferences require risk, reward ratio, investment size, and time horizon")
        if not isinstance(preferences["risk_tolerance"], str) or preferences["risk_tolerance"] not in SESSION_RISK_TOLERANCES:
            raise ValueError("Choose a supported session risk tolerance")
        if not isinstance(preferences["investment_style"], str) or preferences["investment_style"] not in SESSION_INVESTMENT_STYLES:
            raise ValueError("Choose a supported session investment size")
        if not isinstance(preferences["time_horizon"], str) or preferences["time_horizon"] not in SESSION_TIME_HORIZONS:
            raise ValueError("Choose a supported session time horizon")
        ratio = _finite_number(preferences["reward_risk_ratio"], "Reward-to-risk ratio")
        if not 1 <= ratio <= 10:
            raise ValueError("Reward-to-risk ratio must be between 1:1 and 10:1")
        preferences = {
            "risk_tolerance": preferences["risk_tolerance"],
            "reward_risk_ratio": ratio,
            "investment_style": preferences["investment_style"],
            "time_horizon": preferences["time_horizon"],
        }
    if session_mode == "monitoring":
        permissions = {
            "prepare_orders": False,
            "automatic_entries": False,
            "automatic_protective_exits": False,
            "automatic_profit_exits": False,
            "winner_handoff": False,
            "user_signature_each_order": False,
            "automation_wallet_required": False,
            "generated_wallet_required": False,
            "activation_required": False,
            "custody": "none",
        }
    elif execution_style == "interactive":
        permissions = {
            "prepare_orders": True,
            "automatic_entries": False,
            "automatic_protective_exits": False,
            "automatic_profit_exits": False,
            "winner_handoff": False,
            "user_signature_each_order": True,
            "automation_wallet_required": False,
            "generated_wallet_required": False,
            "activation_required": True,
            "custody": "connected-wallet",
        }
    else:
        automatic_entries = automation_scope in {"entries-only", "entries-and-exits"}
        permissions = {
            "prepare_orders": True,
            "automatic_entries": automatic_entries,
            "automatic_protective_exits": automation_scope != "entries-only",
            "automatic_profit_exits": profit_management == "strategy-managed",
            "winner_handoff": False,
            "user_signature_each_order": False,
            "automation_wallet_required": False,
            "generated_wallet_required": True,
            "activation_required": True,
            "custody": custody_mode,
        }
        if custody_mode == "main-wallet-service":
            permissions.update(
                service_vault_required=True,
                continues_without_browser=True,
            )
    created_at = time.time()
    answers = [
        {"question_id": "session_mode", "answer": session_mode},
        {"question_id": "execution_style", "answer": execution_style},
        {"question_id": "automation_scope", "answer": automation_scope},
        {"question_id": "profit_management", "answer": profit_management},
        {"question_id": "goal", "answer": goal},
        {"question_id": "intent", "answer": intent.strip()},
        {"question_id": "paper_budget_usd", "answer": budget},
        {"question_id": "max_session_loss_usd", "answer": loss},
        {"question_id": "review_after_seconds", "answer": review_seconds},
    ]
    if custody_mode == "main-wallet-service":
        answers.append({"question_id": "custody_mode", "answer": custody_mode})
    if preferences is not None:
        answers.extend({"question_id": name, "answer": answer} for name, answer in preferences.items())
    return {
        "schema": (SERVICE_SESSION_BRIEF_SCHEMA
                   if custody_mode == "main-wallet-service" else SESSION_BRIEF_SCHEMA),
        "strategy_digest_at_creation": str(strategy.get("digest") or strategy.get("strategy_digest") or ""),
        "goal": goal,
        "intent": intent.strip(),
        "mode": stored_mode,
        "session_mode": session_mode,
        "execution_style": execution_style,
        "automation_scope": automation_scope,
        **({"custody_mode": custody_mode} if custody_mode == "main-wallet-service" else {}),
        "profit_management": profit_management,
        "permissions": permissions,
        "paper_budget_usd": budget,
        "paper_order_usd": float(order_usd),
        "max_session_loss_usd": loss,
        "max_positions": positions,
        "review_after_seconds": review_seconds,
        "created_at": created_at,
        "review_at": created_at + review_seconds,
        "answers": answers,
        "preferences": preferences,
        "limits": ("This monitoring brief cannot authorize execution or reserve wallet funds."
                   if session_mode == "monitoring" else
                   "This permission manifest records requested behavior but cannot activate execution. Wallet allocation, signer authorization, and reviewed-order limits are enforced separately."),
    }


def _decimal_text(value: float) -> str:
    return format(value, ".17g")
