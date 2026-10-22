"""Bounded session signing against the existing synthetic agent lifecycle."""

import threading
import time
from types import SimpleNamespace

import pytest

from pumpwizard_runner import signing_sessions as sessions, wallet_risk
from pumpwizard_runner.agent_execution import AgentTradeService
from pumpwizard_runner.manual_trades import TradeJournal, ManualTradeService, PREVIEW_TTL
from pumpwizard_runner.transfers import TransferError
from test_agent_execution import setup as _setup, prepare_buy, observe, MINT
from test_wallet_risk import limits


@pytest.fixture
def setup(tmp_path):
    yield from _setup.__wrapped__(tmp_path)


def authorize(setup, manager=None, **changes):
    wallet, rpc, journal, _, agent, cash, service, _ = setup
    if wallet_risk.policy(journal, rpc.genesis(), wallet.address) is None:
        wallet_risk.save_policy(journal, rpc.genesis(), wallet.address, limits())
    manager = manager or sessions.SessionManager()
    options = dict(
        budget_lamports=200_000_000,
        duration_seconds=900,
        max_orders=5,
        slippage_bps=100,
        reviewed_limits=wallet_risk.policy(journal, rpc.genesis(), wallet.address),
    )
    options.update(changes)
    ident = manager.authorize(service, wallet, agent, cash["id"], **options)
    return manager, ident


def test_session_buy_partial_full_exit_and_usage_never_replenishes(setup):
    wallet, rpc, journal, library, agent, cash, service, _ = setup
    manager, ident = authorize(setup)
    p = prepare_buy(setup)
    assert not rpc.sent
    manager.submit(service, ident, p["id"])
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    service.manual.reconcile(wallet.address)
    observe(library, agent, time.time(), 320)
    partial = service.prepare(agent, wallet.address, MINT, cash["id"], position_id=p["id"])
    manager.submit(service, ident, partial["id"])
    service.manual.reconcile(wallet.address)
    observe(library, agent, time.time(), 170)
    full = service.prepare(agent, wallet.address, MINT, cash["id"], position_id=p["id"])
    manager.submit(service, ident, full["id"])
    service.manual.reconcile(wallet.address)
    status = sessions.rows(journal, wallet.address, rpc.genesis())[0]
    assert status["used_orders"] == 3 and status["used_lamports"] == 139_915_000
    assert journal.position(p["id"])["status"] == "closed"
    assert len(rpc.sent) == 3


def test_original_portfolio_ceiling_survives_looser_wallet_policy(setup):
    from test_manual_trades import buy

    wallet, rpc, journal, _, _, _, service, directory = setup
    buy((wallet, rpc, journal, ManualTradeService(rpc, journal), directory))
    wallet_risk.save_policy(journal, rpc.genesis(), wallet.address,
                            limits(portfolio_drawdown_lamports=30_000_000))
    manager, ident = authorize(setup)
    p = prepare_buy(setup)
    wallet_risk.save_policy(journal, rpc.genesis(), wallet.address, limits())
    effects = rpc.effects
    rpc.effects = lambda amount, buying: effects(amount, buying) if buying else (40_000_000-rpc.fee, -amount)
    with pytest.raises(TransferError, match='Portfolio drawdown limit'):
        manager.submit(service, ident, p['id'])
    assert len(rpc.sent) == 1
    assert not journal.db.execute('SELECT 1 FROM signing_session_orders').fetchone()


@pytest.mark.parametrize(
    "options,reason",
    [
        ({"budget_lamports": 90_000_000}, "spending cap"),
        ({"slippage_bps": 50}, "slippage"),
    ],
)
def test_session_blocks_out_of_scope_buy_before_broadcast(setup, options, reason):
    _, rpc, journal, _, _, _, service, _ = setup
    manager, ident = authorize(setup, **options)
    p = prepare_buy(setup)
    with pytest.raises(TransferError, match=reason):
        manager.submit(service, ident, p["id"])
    assert (
        not rpc.sent and not journal.db.execute("SELECT 1 FROM signing_session_orders").fetchone()
    )


def test_order_count_blocks_exit_but_fresh_per_trade_password_path_remains(setup):
    wallet, rpc, journal, library, agent, cash, service, _ = setup
    manager, ident = authorize(setup, max_orders=1)
    p = prepare_buy(setup)
    manager.submit(service, ident, p["id"])
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    service.manual.reconcile(wallet.address)
    observe(library, agent, time.time(), 320)
    exit_order = service.prepare(agent, wallet.address, MINT, cash["id"], position_id=p["id"])
    with pytest.raises(TransferError, match="order count"):
        manager.submit(service, ident, exit_order["id"])
    service.submit(exit_order["id"], wallet)
    service.manual.reconcile(wallet.address)
    assert int(journal.position(p["id"])["quantity"]) < int(
        journal.position(p["id"])["initial_quantity"]
    )


@pytest.mark.parametrize("kind", ["local", "durable", "restart", "expired", "replaced"])
def test_revocation_expiry_restart_and_replacement_prevent_signing(setup, kind):
    wallet, rpc, journal, _, _, _, service, directory = setup
    manager, ident = authorize(setup)
    p = prepare_buy(setup)
    if kind == "local":
        manager.lock(ident)
    elif kind == "durable":
        other = TradeJournal(directory)
        try:
            sessions.revoke(other, ident, wallet.address, rpc.genesis())
        finally:
            other.close()
    elif kind == "restart":
        manager = sessions.SessionManager()
    elif kind == "expired":
        manager._leases[ident].deadline = time.monotonic() - 1
    else:
        authorize(setup, sessions.SessionManager())
    with pytest.raises(TransferError, match="session"):
        manager.submit(service, ident, p["id"])
    assert (
        not rpc.sent and not journal.db.execute("SELECT 1 FROM signing_session_orders").fetchone()
    )


def test_unknown_submission_usage_survives_recovery_and_does_not_resubmit(setup):
    wallet, rpc, journal, library, _, _, service, directory = setup
    manager, ident = authorize(setup)
    p = prepare_buy(setup)
    rpc.disconnect = True
    manager.submit(service, ident, p["id"])
    assert sessions.rows(journal, wallet.address, rpc.genesis())[0]["used_orders"] == 1
    reopened = TradeJournal(directory)
    try:
        recovered = AgentTradeService(library, rpc, reopened)
        rpc.status = {"confirmationStatus": "finalized", "err": None}
        recovered.manual.reconcile(wallet.address)
        recovered.manual.reconcile(wallet.address)
        assert sessions.rows(reopened, wallet.address, rpc.genesis())[0]["used_orders"] == 1
        with pytest.raises(TransferError):
            manager.submit(recovered, ident, p["id"])
        assert len(rpc.sent) == 1
    finally:
        reopened.close()


def test_looser_wallet_policy_cannot_expand_existing_session(setup):
    wallet, rpc, journal, _, _, _, service, _ = setup
    wallet_risk.save_policy(
        journal, rpc.genesis(), wallet.address, limits(max_buy_lamports=90_000_000)
    )
    manager, ident = authorize(setup)
    wallet_risk.save_policy(journal, rpc.genesis(), wallet.address, limits())
    p = prepare_buy(setup)
    with pytest.raises(TransferError, match="maximum buy cost"):
        manager.submit(service, ident, p["id"])
    assert not rpc.sent


def test_manual_order_cannot_use_agent_session(setup):
    wallet, rpc, journal, _, _, _, _, _ = setup
    manager, ident = authorize(setup)
    manual = ManualTradeService(rpc, journal)
    p = manual.preview(wallet.address, MINT, "buy", "0.1")
    # A manual path has no agent binding and cannot use this capability.
    with pytest.raises(TransferError, match="outside"):
        manager.submit(manual, ident, p["id"])
    assert not rpc.sent


def test_cancel_during_authorization_drops_keys_and_revokes_record(setup):
    _, rpc, journal, _, _, _, _, _ = setup
    cancelled = threading.Event()
    cancelled.set()
    manager = sessions.SessionManager()
    with pytest.raises(TransferError, match="cancelled"):
        authorize(setup, manager, cancellation=cancelled)
    assert not manager._leases and not rpc.sent
    assert journal.db.execute("SELECT status FROM signing_sessions").fetchone()[0] == "revoked"


def test_signing_failure_rolls_back_session_usage_and_wallet_intent(setup):
    wallet, rpc, journal, _, _, _, service, _ = setup
    manager, ident = authorize(setup)
    manager._leases[ident].wallet = SimpleNamespace(address=wallet.address, sign=lambda _: b"wrong")
    p = prepare_buy(setup)
    with pytest.raises((TransferError, ValueError)):
        manager.submit(service, ident, p["id"])
    assert not journal.db.execute("SELECT 1 FROM signing_session_orders").fetchone()
    assert not journal.pending(wallet.address, rpc.genesis()) and not rpc.sent


def test_scope_is_pinned_to_cash_allocation(setup):
    from pumpwizard_runner.reservations import reserve

    wallet, rpc, journal, _, agent, _, service, _ = setup
    manager, ident = authorize(setup)
    another = reserve(journal, rpc, agent, wallet.address, lamports=200_000_000)
    p = service.prepare(agent, wallet.address, MINT, another["id"])
    with pytest.raises(TransferError, match="allocation"):
        manager.submit(service, ident, p["id"])
    assert not rpc.sent


@pytest.mark.parametrize("action", ["expire", "revoke"])
def test_expiry_during_revalidation_never_reaches_signer(setup, action):
    _, rpc, _, _, _, _, service, _ = setup
    manager, ident = authorize(setup)
    p = prepare_buy(setup)
    call = rpc.call

    def expire(method, params):
        if method == "getBlockHeight":
            if action == "expire":
                manager._leases[ident].deadline = time.monotonic() - 1
            else:
                manager.lock(ident)
        return call(method, params)

    rpc.call = expire
    with pytest.raises(TransferError, match="expired"):
        manager.submit(service, ident, p["id"])
    assert not rpc.sent


@pytest.mark.parametrize(
    "options",
    [
        {"budget_lamports": 0},
        {"budget_lamports": 200_000_001},
        {"duration_seconds": 0},
        {"duration_seconds": 61},
        {"duration_seconds": 3601},
        {"max_orders": True},
        {"max_orders": 101},
        {"slippage_bps": 501},
        {"reviewed_limits": {}},
    ],
)
def test_invalid_scope_never_creates_session(setup, options):
    journal = setup[2]
    with pytest.raises(TransferError):
        authorize(setup, **options)
    assert not journal.db.execute("SELECT 1 FROM signing_sessions").fetchone()


def test_session_requires_explicit_wallet_policy(setup):
    wallet, _, journal, _, agent, cash, service, _ = setup
    with pytest.raises(TransferError, match="Configure wallet risk"):
        sessions.SessionManager().authorize(
            service,
            wallet,
            agent,
            cash["id"],
            budget_lamports=200_000_000,
            duration_seconds=900,
            max_orders=5,
            slippage_bps=100,
            reviewed_limits=None,
        )
    assert not journal.db.execute("SELECT 1 FROM signing_sessions").fetchone()


@pytest.mark.parametrize("shift", [-3600, 3600])
def test_wall_clock_jump_cannot_extend_or_reactivate_session(setup, monkeypatch, shift):
    _, rpc, journal, _, _, _, service, _ = setup
    manager, ident = authorize(setup)
    p = prepare_buy(setup)
    real_time = time.time
    monkeypatch.setattr(
        sessions,
        "time",
        SimpleNamespace(time=lambda: real_time() + shift, monotonic=time.monotonic),
    )
    with pytest.raises(TransferError, match="session"):
        manager.submit(service, ident, p["id"])
    assert not manager.available(ident) and not rpc.sent
    assert not journal.db.execute("SELECT 1 FROM signing_session_orders").fetchone()


def test_quote_expiry_during_session_policy_check_rolls_back_usage(setup, monkeypatch):
    _, rpc, journal, _, _, _, service, _ = setup
    manager, ident = authorize(setup)
    p = prepare_buy(setup)
    check = wallet_risk.check_buy
    real_time = time.time

    def slow_check(*args, **kwargs):
        check(*args, **kwargs)
        if kwargs.get("limits") is not None:
            monkeypatch.setattr(
                sessions,
                "time",
                    SimpleNamespace(time=lambda: real_time() + PREVIEW_TTL + 1, monotonic=time.monotonic),
            )

    monkeypatch.setattr(wallet_risk, "check_buy", slow_check)
    with pytest.raises(TransferError, match="quote expired"):
        manager.submit(service, ident, p["id"])
    assert (
        not rpc.sent and not journal.db.execute("SELECT 1 FROM signing_session_orders").fetchone()
    )
