"""Continuous unsigned execution rehearsal with recovery of existing signatures.

No signer, session or submission mode is accepted. Route/strategy checks remain
in AgentTradeService; this layer owns scheduling, recovery, evidence and health.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .manual_trades import ManualTradeService, TradeJournal
from .strategy_support import reviewed_eligible, strategy_identity
from .shadow import UnsignedRpc, shadow_context, observation_evidence
from .strategies import parse_package
from .transfers import GENESIS, TransferError, WithdrawalService, public_key


LEASE_SECONDS = 90


class WorkerStopped(RuntimeError):
    pass


class WorkerRpcError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerConfig:
    agent_id: str
    sender: str
    network: str
    cash_hold: str | None = None
    interval: int = 15
    slippage_bps: int = 100
    limit: int = 3
    mode: str = "unsigned"

    def __post_init__(self):
        public_key(self.sender)
        if not self.agent_id or self.network not in ("mainnet-beta", "devnet", "localnet"):
            raise TransferError("Select an agent and supported Solana network")
        for value, lower, upper, name in (
            (self.interval, 5, 300, "Worker interval"),
            (self.slippage_bps, 1, 500, "Slippage"),
            (self.limit, 1, 10, "Candidates per cycle"),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise TransferError(f"{name} must be {lower}–{upper}")
        if self.mode != "unsigned":
            raise TransferError(
                "Only unsigned worker rehearsal is available; automatic signing is not enabled"
            )

    @property
    def scope(self):
        return hashlib.sha256(
            json.dumps([self.agent_id, self.sender, self.network]).encode()
        ).hexdigest()


class WorkerStore:
    def __init__(self, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(directory) / "execution-worker.sqlite", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS worker_leases (scope TEXT PRIMARY KEY, owner TEXT NOT NULL, heartbeat REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS worker_runs (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, config TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL, started_at REAL NOT NULL,
                updated_at REAL NOT NULL, next_at REAL, failures INTEGER NOT NULL,
                cycles INTEGER NOT NULL, cursors TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS worker_cycles (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL, run_id TEXT NOT NULL,
                at REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS worker_cycle_scope ON worker_cycles(scope,id);
        """)

    def close(self):
        self.db.close()

    def claim(self, config, owner):
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute(
                "SELECT * FROM worker_leases WHERE scope=?", (config.scope,)
            ).fetchone()
            if old and -LEASE_SECONDS <= now - old["heartbeat"] <= LEASE_SECONDS:
                raise TransferError("Another worker already manages this agent, wallet and network")
            if old:
                self.db.execute(
                    "UPDATE worker_runs SET status='interrupted',detail='Worker lease expired',next_at=NULL WHERE id=?",
                    (old["owner"],),
                )
            previous = self.db.execute(
                "SELECT cursors FROM worker_runs WHERE scope=? ORDER BY started_at DESC LIMIT 1",
                (config.scope,),
            ).fetchone()
            cursors = json.loads(previous[0]) if previous else {"exits": 0, "entries": 0}
            self.db.execute(
                "INSERT OR REPLACE INTO worker_leases VALUES (?,?,?)", (config.scope, owner, now)
            )
            self.db.execute(
                "INSERT INTO worker_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    owner,
                    config.scope,
                    json.dumps(asdict(config)),
                    "running",
                    "Unsigned worker starting",
                    now,
                    now,
                    None,
                    0,
                    0,
                    json.dumps(cursors),
                ),
            )
        return cursors

    def heartbeat(self, config, owner):
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM worker_leases WHERE scope=?", (config.scope,)
            ).fetchone()
            if (
                not row
                or row["owner"] != owner
                or not -LEASE_SECONDS <= now - row["heartbeat"] <= LEASE_SECONDS
            ):
                raise WorkerStopped("Worker lease lost or expired; restart explicitly")
            self.db.execute(
                "UPDATE worker_leases SET heartbeat=? WHERE scope=? AND owner=?",
                (now, config.scope, owner),
            )

    def record(self, config, owner, report, delay, failures, cursors):
        self.heartbeat(config, owner)
        now = time.time()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT owner FROM worker_leases WHERE scope=?", (config.scope,)
            ).fetchone()
            if not row or row[0] != owner:
                raise WorkerStopped("Worker lease changed before report commit")
            self.db.execute(
                "INSERT INTO worker_cycles(scope,run_id,at,payload) VALUES (?,?,?,?)",
                (config.scope, owner, now, json.dumps(report)),
            )
            self.db.execute(
                "UPDATE worker_runs SET status=?,detail=?,updated_at=?,next_at=?,failures=?,cycles=cycles+1,cursors=? WHERE id=?",
                (
                    report["status"],
                    report["detail"],
                    now,
                    now + delay,
                    failures,
                    json.dumps(cursors),
                    owner,
                ),
            )
            # Keep bounded evidence for continuous multi-day rehearsals.
            self.db.execute(
                "DELETE FROM worker_cycles WHERE scope=? AND id NOT IN (SELECT id FROM worker_cycles WHERE scope=? ORDER BY id DESC LIMIT 500)",
                (config.scope, config.scope),
            )

    def finish(self, config, owner, state="stopped", detail="Worker stopped; no automatic restart"):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "DELETE FROM worker_leases WHERE scope=? AND owner=?", (config.scope, owner)
            )
            self.db.execute(
                "UPDATE worker_runs SET status=?,detail=?,updated_at=?,next_at=NULL WHERE id=?",
                (state, detail, time.time(), owner),
            )


def history(directory, config):
    """UI reads never migrate the store or request a writer lock."""
    path = Path(directory) / "execution-worker.sqlite"
    if not path.exists():
        return dict(run=None, cycles=[])
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        row = db.execute(
            "SELECT * FROM worker_runs WHERE scope=? ORDER BY started_at DESC LIMIT 1",
            (config.scope,),
        ).fetchone()
        run = dict(row) if row else None
        if run:
            lease = db.execute(
                "SELECT * FROM worker_leases WHERE scope=?", (config.scope,)
            ).fetchone()
            run["active"] = bool(
                lease
                and lease["owner"] == run["id"]
                and -LEASE_SECONDS <= time.time() - lease["heartbeat"] <= LEASE_SECONDS
            )
            if not run["active"] and run["status"] not in ("stopped", "faulted", "interrupted"):
                run.update(
                    status="interrupted",
                    detail="Worker is not running; start explicitly",
                    next_at=None,
                )
            run["config"] = json.loads(run["config"])
        cycles = [
            dict(id=r["id"], at=r["at"], report=json.loads(r["payload"]))
            for r in db.execute(
                "SELECT * FROM worker_cycles WHERE scope=? ORDER BY id DESC LIMIT 30",
                (config.scope,),
            )
        ]
        return dict(run=run, cycles=cycles)
    finally:
        db.close()


def candidates(service, config, genesis, cursors):
    journal, library = service.journal, service.library
    instance = library.instance(config.agent_id)
    strategy = parse_package(instance["package"])
    if not reviewed_eligible(strategy):
        raise TransferError("Unsigned worker requires the pinned core-frequency configuration")
    exits = [
        dict(mint=r["mint"], position_id=r["id"], side="sell")
        for r in journal.db.execute(
            """
        SELECT p.mint,p.id FROM agent_positions a JOIN trade_positions p ON p.id=a.position_id
        JOIN agent_reservations h ON h.id=a.token_hold
        WHERE a.agent_id=? AND p.sender=? AND p.genesis=? AND p.status='open'
        AND h.status='held' ORDER BY p.opened_at,p.id""",
            (config.agent_id, config.sender, genesis),
        )
    ]
    held = {r["mint"] for r in exits}
    attempted = {
        r[0]
        for r in journal.db.execute(
            "SELECT mint FROM agent_orders WHERE agent_id=? AND side='buy' AND status IN ('review','submitted','finalized','failed')",
            (config.agent_id,),
        )
    }
    entries, seen = [], set()
    if instance["status"] == "monitoring" and instance["entry_mode"] != "disabled":
        for decision in library.decisions(config.agent_id, 1000):
            mint = decision["mint"]
            if mint in seen:
                continue
            seen.add(mint)
            if (
                mint not in held
                and mint not in attempted
                and decision["kind"] == "entry_recommended"
            ):
                entries.append(dict(mint=mint, position_id=None, side="buy"))

    def rotate(items, key, limit):
        if not items or limit <= 0:
            return []
        start = cursors[key] % len(items)
        selected = (items[start:] + items[:start])[:limit]
        cursors[key] = (start + len(selected)) % len(items)
        return selected

    selected = rotate(exits, "exits", config.limit)
    selected += rotate(entries, "entries", config.limit - len(selected))
    return selected, len(exits) + len(entries) - len(selected)


class GuardedRpc:
    """No broadcast method, even if the supplied transport supports one."""

    allowed_methods = UnsignedRpc.allowed_methods | {
        "getSignatureStatuses",
        "getTransaction",
        "getBalance",
    }

    def __init__(self, rpc, checkpoint):
        self.rpc, self.network, self.checkpoint = rpc, rpc.network, checkpoint

    def genesis(self):
        self.checkpoint()
        try:
            result = self.rpc.genesis()
        except (TransferError, OSError, TimeoutError):
            raise WorkerRpcError("RPC network identity unavailable") from None
        self.checkpoint()
        if self.network in GENESIS and result != GENESIS[self.network]:
            raise WorkerRpcError("RPC network identity does not match selected network")
        return result

    def call(self, method, params):
        self.checkpoint()
        if method not in self.allowed_methods:
            raise TransferError("Unsigned worker cannot broadcast transactions")
        if method == "simulateTransaction":
            # Reuse the independent signature validation in UnsignedRpc.
            return UnsignedRpc(_GuardTransport(self)).call(method, params)
        return self._call(method, params)

    def _call(self, method, params):
        try:
            result = self.rpc.call(method, params)
        except (TransferError, OSError, TimeoutError):
            raise WorkerRpcError(f"RPC unavailable for {method}") from None
        self.checkpoint()
        return result


class _GuardTransport:
    def __init__(self, guard):
        self.guard, self.network = guard, guard.network

    def call(self, method, params):
        return self.guard._call(method, params)


class ExecutionWorker:
    def __init__(self, directory, config, rpc_factory):
        self.directory, self.config, self.rpc_factory = Path(directory), config, rpc_factory
        self.owner = uuid.uuid4().hex
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self):
        if self.thread is not None:
            raise TransferError("Create a fresh worker to start another run")
        self.thread = threading.Thread(target=self._run, name="pump-unsigned-worker", daemon=False)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        store = None
        claimed = False
        try:
            store = WorkerStore(self.directory)
            cursors = store.claim(self.config, self.owner)
            claimed = True
            failures = 0
            while not self.stop_event.is_set():
                report = self.cycle(store, cursors)
                failures = failures + 1 if report["status"] == "error" else 0
                delay = min(300, self.config.interval * 2 ** min(failures, 5))
                store.record(self.config, self.owner, report, delay, failures, cursors)
                deadline = time.monotonic() + delay
                while not self.stop_event.is_set() and time.monotonic() < deadline:
                    self.stop_event.wait(min(15, max(0, deadline - time.monotonic())))
                    if not self.stop_event.is_set():
                        store.heartbeat(self.config, self.owner)
        except WorkerStopped as exc:
            self.error = str(exc) if not self.stop_event.is_set() else None
        except Exception as exc:
            self.error = (
                str(exc)[:250]
                if isinstance(exc, TransferError)
                else f"Worker stopped ({type(exc).__name__}); inspect setup before restarting"
            )
        finally:
            if store:
                try:
                    if claimed:
                        store.finish(
                            self.config,
                            self.owner,
                            "faulted" if self.error else "stopped",
                            self.error or "Worker stopped; start explicitly to resume",
                        )
                finally:
                    store.close()

    def run_once(self):
        store = WorkerStore(self.directory)
        claimed = False
        try:
            cursors = store.claim(self.config, self.owner)
            claimed = True
            report = self.cycle(store, cursors)
            store.record(
                self.config, self.owner, report, 0, int(report["status"] == "error"), cursors
            )
            return report
        except Exception as exc:
            if claimed:
                detail = (
                    str(exc)[:250]
                    if isinstance(exc, (TransferError, WorkerStopped))
                    else f"Worker stopped ({type(exc).__name__})"
                )
                store.finish(self.config, self.owner, "faulted", detail)
                claimed = False
            raise
        finally:
            if claimed:
                store.finish(self.config, self.owner)
            store.close()

    def cycle(self, store, cursors):
        """One cycle, also usable synchronously for deterministic acceptance tests."""
        config = self.config

        def checkpoint():
            if self.stop_event.is_set():
                raise WorkerStopped("Worker stop requested")
            store.heartbeat(config, self.owner)

        checkpoint()
        started = time.time()
        report = dict(
            schema="solanamarket.execution-worker/v1",
            mode="unsigned",
            submission_enabled=False,
            agent_id=config.agent_id,
            sender=config.sender,
            network=config.network,
            started_at=started,
            status="waiting",
            detail="",
            recovery="none",
            results=[],
            deferred=0,
        )
        try:
            rpc = GuardedRpc(self.rpc_factory(), checkpoint)
            if rpc.network != config.network:
                raise TransferError("Worker RPC does not match selected network")
            genesis = rpc.genesis()
            with shadow_context(self.directory, rpc) as (service, _):
                report['identity'] = strategy_identity(parse_package(service.library.instance(config.agent_id)['package']))
                report['risk_policy'] = 'executable-net-exit-v1'
                report['slippage_bps'] = config.slippage_bps
                pending = service.journal.pending(config.sender, genesis)
            if pending:
                checkpoint()
                journal = TradeJournal(self.directory)
                try:
                    ManualTradeService(rpc, journal).reconcile(config.sender)
                    WithdrawalService(rpc, journal).reconcile(config.sender)
                    pending = journal.pending(config.sender, genesis)
                finally:
                    journal.close()
                report["recovery"] = "pending" if pending else "recovered"
                if pending:
                    report.update(
                        status="blocked",
                        detail="Original signature still unresolved; no new order is created",
                    )
                    return self._finished(report)
            with shadow_context(self.directory, rpc) as (service, _):
                from .portfolio_risk import refresh
                from .wallet_risk import policy

                limits = policy(service.journal, genesis, config.sender)
                if limits and limits.get('portfolio_drawdown_lamports') is not None:
                    report['portfolio'] = refresh(service.journal, rpc, config.sender)
                selected, report["deferred"] = candidates(service, config, genesis, cursors)
                holds = [
                    dict(r)
                    for r in service.journal.db.execute(
                        """SELECT h.* FROM agent_reservations h
                    WHERE h.agent_id=? AND h.sender=? AND h.genesis=? AND h.status='held' AND h.position_id IS NULL
                    AND NOT EXISTS (SELECT 1 FROM agent_orders o WHERE o.cash_hold=h.id AND o.status IN ('review','submitted'))
                    ORDER BY h.created_at,h.id""",
                        (config.agent_id, config.sender, genesis),
                    )
                ]
                holds.sort(key=lambda r: -int(r["lamports"]))
                cash = (
                    next((r for r in holds if r["id"] == config.cash_hold), None)
                    if config.cash_hold
                    else next(iter(holds), None)
                )
                if not selected:
                    report["detail"] = (
                        "No eligible signals or managed exits; keep market tracking active"
                    )
                elif cash is None:
                    report.update(
                        status="blocked",
                        detail="No available cash allocation; allocate SOL or resolve/cancel its bound review",
                    )
                else:
                    for item in selected:
                        checkpoint()
                        item['local_input'] = observation_evidence(service, config.agent_id, item['mint'])
                        start = len(service.rpc.calls)
                        try:
                            result = service.candidate(
                                config.agent_id,
                                config.sender,
                                item["mint"],
                                cash["id"],
                                position_id=item["position_id"],
                                slippage_bps=config.slippage_bps,
                            )
                            p = result["preview"]
                            item.update(
                                status=result["status"],
                                reason=result["reason"],
                                risk=result["risk"],
                                proposal={
                                    k: p[k]
                                    for k in (
                                        "side",
                                        "amount",
                                        "route",
                                        "minimum_output",
                                        "fee",
                                        "max_wallet_debit",
                                        "expected_wallet_delta",
                                    )
                                },
                            )
                        except (TransferError, ValueError, KeyError, TypeError) as exc:
                            item.update(status="blocked", reason=str(exc)[:350])
                        item["rpc_evidence"] = service.rpc.calls[start:]
                        report["results"].append(item)
                    report.update(
                        status="checked",
                        detail=f"Assessed {len(selected)} candidates; all results remain unsigned",
                    )
                report["cash_hold"] = cash["id"] if cash else config.cash_hold
            checkpoint()
        except WorkerRpcError as exc:
            report.update(status="error", detail=str(exc))
        return self._finished(report)

    @staticmethod
    def _finished(report):
        report["finished_at"] = time.time()
        report["duration_seconds"] = max(0, report["finished_at"] - report["started_at"])
        return report
