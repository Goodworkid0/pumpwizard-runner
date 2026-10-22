"""Unsigned scheduler acceptance using existing synthetic routes and receipts."""

import copy
import json
import threading
import time

import pytest

from pumpwizard_runner.execution_worker import (
    ExecutionWorker,
    GuardedRpc,
    WorkerConfig,
    WorkerStopped,
    WorkerStore,
    candidates,
    history,
)
from pumpwizard_runner.reservations import release
from pumpwizard_runner.shadow import shadow_context
from pumpwizard_runner.transfers import TransferError
from test_agent_execution import setup as _setup, fill_buy, observe, prepare_buy
from test_pump_swap_route import SwapRpc


@pytest.fixture
def setup(tmp_path):
    yield from _setup.__wrapped__(tmp_path)


def worker(setup, rpc=None, **options):
    wallet, original, _, _, agent, cash, _, directory = setup
    config = WorkerConfig(
        agent, wallet.address, (rpc or original).network, cash_hold=cash["id"], **options
    )
    return ExecutionWorker(directory, config, lambda: rpc or original)


def financial_snapshot(setup):
    return list(setup[2].db.iterdump()), list(setup[3]._connection.iterdump())


def test_repeated_ready_cycles_never_prepare_sign_reserve_or_open_vault(setup, monkeypatch):
    from pumpwizard_runner.wallet_vault import WalletVault

    monkeypatch.setattr(WalletVault, "unlock", lambda *args: pytest.fail("Worker opened a vault"))
    before = financial_snapshot(setup)
    for _ in range(3):
        report = worker(setup).run_once()
        assert report["status"] == "checked" and report["results"][0]["status"] == "ready"
        assert report["results"][0]["side"] == "buy" and not report["submission_enabled"]
        assert report['identity']['package_sha256'] == setup[3].instance(setup[4])['strategy_digest']
        assert report['results'][0]['local_input']['observation']['mint']
    assert financial_snapshot(setup) == before and not setup[1].sent
    saved = history(setup[-1], worker(setup).config)
    assert len(saved["cycles"]) == 3 and not saved["run"]["active"]
    assert saved["run"]["status"] == "stopped"


def test_predeposit_waits_for_allocation_without_creating_one(setup):
    wallet, rpc, journal, _, _, cash, _, _ = setup
    release(journal, cash["id"])
    rpc.balance = 0
    before = financial_snapshot(setup)
    report = worker(setup).run_once()
    assert report["status"] == "blocked" and "allocation" in report["detail"]
    assert not report["results"] and not journal.positions(wallet.address)
    assert financial_snapshot(setup) == before


def test_paused_entries_still_assess_managed_exits(setup):
    wallet, rpc, journal, library, agent, _, _, _ = setup
    p, _ = fill_buy(setup)
    library.set_instance_status(agent, "paused")
    observe(library, agent, time.time(), 320)
    before = financial_snapshot(setup)
    report = worker(setup).run_once()
    assert report["results"][0]["position_id"] == p["id"]
    assert report["results"][0]["side"] == "sell" and report["results"][0]["status"] == "ready"
    assert financial_snapshot(setup) == before and len(rpc.sent) == 1
    assert journal.position(p["id"])["status"] == "open"


def test_pause_without_positions_waits_without_entry_assessment(setup):
    setup[3].set_instance_status(setup[4], "paused")
    report = worker(setup).run_once()
    assert report["status"] == "waiting" and report["results"] == []


def test_paused_worker_refreshes_portfolio_and_keeps_exits_over_drawdown_limit(setup):
    from test_wallet_risk import configure

    wallet, rpc, _, library, agent, _, _, _ = setup
    fill_buy(setup)
    configure(setup, portfolio_drawdown_lamports=1)
    library.set_instance_status(agent, "paused")
    observe(library, agent, time.time(), 320)
    before = financial_snapshot(setup)
    report = worker(setup).run_once()
    assert report['portfolio']['status'] == 'ready'
    assert report['portfolio']['drawdown'] > 1
    assert report['results'][0]['side'] == 'sell'
    assert report['results'][0]['status'] == 'ready'
    assert financial_snapshot(setup) == before and len(rpc.sent) == 1


def test_released_manual_override_is_not_scheduled_as_agent_exit(setup):
    p, _ = fill_buy(setup)
    journal = setup[2]
    token_hold = journal.db.execute(
        "SELECT token_hold FROM agent_positions WHERE position_id=?", (p["id"],)
    ).fetchone()[0]
    release(journal, token_hold)
    report = worker(setup).run_once()
    assert not report["results"]  # The original attempted entry is not queued again either.


def test_unknown_signature_blocks_then_finality_recovers_without_resigning(setup):
    wallet, rpc, journal, library, agent, _, service, _ = setup
    p = prepare_buy(setup)
    rpc.disconnect = True
    service.submit(p["id"], wallet)
    first = worker(setup).run_once()
    assert first["status"] == "blocked" and first["recovery"] == "pending"
    assert not journal.positions(wallet.address) and len(rpc.sent) == 1
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    observe(library, agent, time.time(), 320)
    second = worker(setup).run_once()
    assert second["recovery"] == "recovered" and second["results"][0]["position_id"] == p["id"]
    assert journal.position(p["id"])["status"] == "open" and len(rpc.sent) == 1
    worker(setup).run_once()
    assert journal.db.execute("SELECT COUNT(*) FROM agent_fills").fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["stale", "simulation", "balance"])
def test_blocked_candidates_record_reasons_without_wallet_changes(setup, monkeypatch, failure):
    rpc = setup[1]
    if failure == "stale":
        now = time.time()
        monkeypatch.setattr("pumpwizard_runner.agent_execution.time.time", lambda: now + 40)
    elif failure == "simulation":
        rpc.bad_sim = True
    else:
        rpc.balance = 1000
    before = financial_snapshot(setup)
    report = worker(setup).run_once()
    assert report["results"][0]["status"] == "blocked" and report["results"][0]["reason"]
    assert financial_snapshot(setup) == before and not rpc.sent


def test_migration_assessment_uses_existing_pumpswap_validator(setup):
    wallet, rpc, _, library, agent, _, _, _ = setup
    p, _ = fill_buy(setup)
    swap = SwapRpc(wallet)
    swap.bank = copy.deepcopy(rpc.bank)
    swap.balance, swap.tokens, swap.graduated = rpc.balance, rpc.tokens, True
    observe(library, agent, time.time(), 320)
    before = financial_snapshot(setup)
    report = worker(setup, rpc=swap).run_once()
    assert report["results"][0]["position_id"] == p["id"]
    assert report["results"][0]["proposal"]["route"].startswith("pumpswap-")
    assert financial_snapshot(setup) == before and not swap.sent


def test_duplicate_lease_and_stale_owner_cannot_overwrite_replacement(setup):
    first, second = worker(setup), worker(setup)
    store = WorkerStore(setup[-1])
    try:
        store.claim(first.config, first.owner)
        with pytest.raises(TransferError, match="Another worker"):
            store.claim(second.config, second.owner)
        with store.db:
            store.db.execute("UPDATE worker_leases SET heartbeat=?", (time.time() - 100,))
        assert history(setup[-1], first.config)["run"]["status"] == "interrupted"
        store.claim(second.config, second.owner)
        with pytest.raises(WorkerStopped):
            store.heartbeat(first.config, first.owner)
        store.finish(first.config, first.owner)
        assert store.db.execute("SELECT owner FROM worker_leases").fetchone()[0] == second.owner
    finally:
        store.close()


def test_rpc_failure_is_recorded_and_background_retries_back_off(setup, monkeypatch):
    instance = worker(setup, interval=5)
    setup[1].genesis = lambda: (_ for _ in ()).throw(TransferError("unavailable"))
    captured = []
    original = WorkerStore.record

    def record(self, config, owner, report, delay, failures, cursors):
        captured.append((delay, failures, report["status"]))
        original(self, config, owner, report, delay, failures, cursors)

    monkeypatch.setattr(WorkerStore, "record", record)

    class StopAfterWait:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _delay):
            self.stopped = True

    instance.stop_event = StopAfterWait()
    instance._run()
    assert captured == [(10, 1, "error")]
    assert history(setup[-1], instance.config)["cycles"][0]["report"]["results"] == []


def test_stop_during_rpc_discards_late_candidate_and_joins(setup):
    instance = worker(setup)
    entered, resume = threading.Event(), threading.Event()
    rpc = setup[1]
    call = rpc.call

    def delayed(method, params):
        if method == "getMultipleAccounts":
            entered.set()
            assert resume.wait(3)
        return call(method, params)

    rpc.call = delayed
    before = financial_snapshot(setup)
    instance.start()
    try:
        assert entered.wait(3)
        instance.stop()
    finally:
        resume.set()
        instance.thread.join(4)
    assert not instance.running and not rpc.sent
    assert financial_snapshot(setup) == before
    assert history(setup[-1], instance.config)["run"]["status"] == "stopped"


def test_long_idle_interval_renews_lease_until_next_cycle(setup, monkeypatch):
    from types import SimpleNamespace
    from pumpwizard_runner import execution_worker as module

    instance = worker(setup, interval=300)
    clock = [time.time()]
    monkeypatch.setattr(
        module, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])
    )
    visits = []

    def cycle(store, cursors):
        store.heartbeat(instance.config, instance.owner)
        visits.append(clock[0])
        if len(visits) == 2:
            instance.stop()
            raise WorkerStopped("Test completed")
        return dict(status="waiting", detail="No candidates", results=[])

    monkeypatch.setattr(instance, "cycle", cycle)
    monkeypatch.setattr(
        instance.stop_event, "wait", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )
    instance._run()
    assert len(visits) == 2 and visits[1] - visits[0] == 300
    assert instance.error is None
    assert history(setup[-1], instance.config)["run"]["status"] == "stopped"


def test_exits_rotate_fairly_and_filter_other_network_positions(setup):
    wallet, _, journal, _, agent, _, _, directory = setup
    p, _ = fill_buy(setup)
    # Additional synthetic lots exercise planning only, not on-chain balances.
    for i in range(3):
        position = dict(
            journal.db.execute("SELECT * FROM trade_positions WHERE id=?", (p["id"],)).fetchone()
        )
        owner = dict(
            journal.db.execute(
                "SELECT * FROM agent_positions WHERE position_id=?", (p["id"],)
            ).fetchone()
        )
        hold = dict(
            journal.db.execute(
                "SELECT * FROM agent_reservations WHERE id=?", (owner["token_hold"],)
            ).fetchone()
        )
        position["id"] = owner["position_id"] = hold["position_id"] = f"lot-{i}"
        hold["id"] = owner["token_hold"] = f"token-hold-{i}"
        if i == 2:
            position["genesis"] = hold["genesis"] = "other-network"
        with journal.db:
            for table, row in (
                ("trade_positions", position),
                ("agent_positions", owner),
                ("agent_reservations", hold),
            ):
                journal.db.execute(
                    f"INSERT INTO {table} VALUES ({','.join('?' for _ in row)})",
                    tuple(row.values()),
                )
    config = WorkerConfig(agent, wallet.address, "localnet", limit=1)
    cursors, seen = {"exits": 0, "entries": 0}, []
    with shadow_context(directory, setup[1]) as (service, _):
        for _ in range(3):
            batch, deferred = candidates(service, config, "test-bank", cursors)
            assert deferred == 2 and batch[0]["side"] == "sell"
            seen.append(batch[0]["position_id"])
    assert set(seen) == {p["id"], "lot-0", "lot-1"}


def test_unsigned_transport_blocks_broadcast_and_signed_simulation(setup):
    guard = GuardedRpc(setup[1], lambda: None)
    with pytest.raises(TransferError, match="cannot broadcast"):
        guard.call("sendTransaction", ["anything"])
    with pytest.raises((TransferError, ValueError)):
        guard.call("simulateTransaction", ["not a transaction", {"sigVerify": True}])
    assert not setup[1].sent


def test_wrong_network_never_recovers_or_assesses_wallet(setup):
    instance = worker(setup)
    setup[1].network = "devnet"
    with pytest.raises(TransferError, match="selected network"):
        instance.run_once()
    assert not setup[1].sent
    assert history(setup[-1], instance.config)["run"]["status"] == "faulted"


def test_invalid_network_identity_is_an_error_not_a_ready_signal(setup):
    setup[1].network = "devnet"
    report = worker(setup).run_once()
    assert report["status"] == "error" and report["results"] == []


@pytest.mark.parametrize(
    "options", [{"mode": "live"}, {"interval": 0}, {"limit": 11}, {"slippage_bps": 501}]
)
def test_unsupported_worker_settings_cannot_start(setup, options):
    with pytest.raises(TransferError):
        worker(setup, **options)


def test_history_read_does_not_create_a_store(tmp_path, setup):
    assert history(tmp_path / "empty", worker(setup).config) == {"run": None, "cycles": []}
    assert not (tmp_path / "empty").exists()


def test_report_retention_is_bounded(setup):
    instance = worker(setup)
    store = WorkerStore(setup[-1])
    try:
        cursors = store.claim(instance.config, instance.owner)
        report = dict(status="waiting", detail="No candidates", results=[])
        with store.db:
            store.db.executemany(
                "INSERT INTO worker_cycles(scope,run_id,at,payload) VALUES (?,?,?,?)",
                [(instance.config.scope, instance.owner, time.time(), json.dumps(report))] * 501,
            )
        store.record(instance.config, instance.owner, report, 15, 0, cursors)
        assert store.db.execute("SELECT COUNT(*) FROM worker_cycles").fetchone()[0] == 500
        assert len(history(setup[-1], instance.config)["cycles"]) == 30
    finally:
        store.close()
