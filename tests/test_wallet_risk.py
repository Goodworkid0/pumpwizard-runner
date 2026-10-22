"""Wallet-wide entry limits on synthetic receipts; no live funds or RPC."""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from solders.hash import Hash

from pumpwizard_runner import wallet_risk as risk
from pumpwizard_runner.agent_execution import cancel
from pumpwizard_runner.manual_trades import ManualTradeService, TradeJournal
from pumpwizard_runner.reservations import reserve
from pumpwizard_runner.transfers import TransferError
from test_manual_trades import MINT, buy, setup as _manual_setup
from test_agent_execution import setup as _agent_setup, prepare_buy


@pytest.fixture
def setup(tmp_path):
    fixture = _manual_setup.__wrapped__(tmp_path)
    bank = next(fixture)
    rpc = bank[1]
    call, height = rpc.call, 0

    def fresh_blockhash(method, params):
        nonlocal height
        result = call(method, params)
        if method == "getLatestBlockhash":
            height += 1
            result["value"]["blockhash"] = str(Hash.from_bytes(height.to_bytes(32, "little")))
        return result

    rpc.call = fresh_blockhash
    yield bank
    next(fixture, None)


@pytest.fixture
def agent_setup(tmp_path):
    yield from _agent_setup.__wrapped__(tmp_path)


def limits(**changes):
    return (
        dict(
            max_buy_lamports=200_000_000,
            max_exposure_lamports=500_000_000,
            reserve_lamports=100_000_000,
            daily_loss_lamports=100_000_000,
            drawdown_lamports=100_000_000,
            portfolio_drawdown_lamports=500_000_000,
            entries_paused=False,
        )
        if not changes
        else dict(limits(), **changes)
    )


def configure(setup, **changes):
    wallet, rpc, journal = setup[:3]
    return risk.save_policy(journal, rpc.genesis(), wallet.address, limits(**changes))


def sell(setup, position, quantity="1000"):
    wallet, rpc, _, service, _ = setup
    p = service.preview(wallet.address, MINT, "sell", quantity, position_id=position)
    service.submit(p["id"], wallet)
    service.reconcile(wallet.address)
    return p


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"max_buy_lamports": 120_004_999}, "maximum buy cost"),
        (
            {"max_buy_lamports": 120_004_999, "max_exposure_lamports": 120_004_999},
            "maximum open cost",
        ),
        ({"reserve_lamports": 2_880_000_000}, "SOL reserve"),
        ({"entries_paused": True}, "paused"),
    ],
)
def test_preview_blocks_all_manual_entry_limits(setup, change, reason):
    wallet, rpc, journal, service, _ = setup
    configure(setup, **change)
    with pytest.raises(TransferError, match=reason):
        service.preview(wallet.address, MINT, "buy", "0.1")
    assert not rpc.sent and not journal.db.execute("SELECT 1 FROM trade_previews").fetchone()


def test_inclusive_buy_boundary_and_total_cost_across_positions(setup):
    wallet, _, journal, service, _ = setup
    configure(setup, max_buy_lamports=120_005_000, max_exposure_lamports=220_000_000)
    buy(setup)
    assert risk.metrics(journal, "test-bank", wallet.address)["open_cost"] == 102_044_280
    with pytest.raises(TransferError, match="maximum open cost"):
        service.preview(wallet.address, MINT, "buy", "0.1")


def test_agent_reviews_share_budget_with_other_agents_and_manual_buys(agent_setup):
    wallet, rpc, journal, library, agent, _, service, _ = agent_setup
    configure(agent_setup, max_exposure_lamports=180_000_000, max_buy_lamports=150_000_000)
    first = prepare_buy(agent_setup)
    other = library.create_instance(library.instance(agent)["strategy_digest"], entry_mode="manual")
    for record in library.recent_observations(agent, 0):
        library.append_observations(other, [record])
    decision = library.latest_actionable_decision(agent, MINT)
    library.record_decisions(
        other,
        [
            dict(
                at=decision["at"],
                kind="entry_recommended",
                mint=MINT,
                reason="strategy_entry_signal",
                payload={},
            )
        ],
    )
    cash = reserve(journal, rpc, other, wallet.address, lamports=200_000_000)
    with pytest.raises(TransferError, match="maximum open cost"):
        service.prepare(other, wallet.address, MINT, cash["id"])
    with pytest.raises(TransferError, match="maximum open cost"):
        ManualTradeService(rpc, journal).preview(wallet.address, MINT, "buy", "0.1")
    # Cancelled reviews stop consuming exposure, but remain ordinary cash holds.
    cancel(journal, first["id"])
    service.prepare(other, wallet.address, MINT, cash["id"])


def test_late_policy_change_blocks_before_signing(setup):
    wallet, rpc, journal, service, _ = setup
    configure(setup)
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    call = rpc.call

    def changed(method, params):
        if method == "getBlockHeight":
            configure(setup, entries_paused=True)
        return call(method, params)

    rpc.call = changed
    signed = []
    signer = SimpleNamespace(address=wallet.address, sign=lambda message: signed.append(message))
    with pytest.raises(TransferError, match="changed the balance snapshot"):
        service.submit(p["id"], signer)
    assert not signed and not rpc.sent
    assert not journal.pending(wallet.address, rpc.genesis())


def test_signing_and_reservation_hold_same_database_lock(setup):
    wallet, rpc, _, service, directory = setup
    configure(setup)
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    other = TradeJournal(directory)
    other.db.execute("PRAGMA busy_timeout=0")

    def sign(message):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            risk.save_policy(other, rpc.genesis(), wallet.address, limits(entries_paused=True))
        return wallet.sign(message)

    try:
        service.submit(p["id"], SimpleNamespace(address=wallet.address, sign=sign))
        assert len(rpc.sent) == 1
    finally:
        other.close()


def test_daily_loss_partial_exit_restart_and_remaining_exit(setup):
    wallet, rpc, journal, _, directory = setup
    configure(setup, daily_loss_lamports=1_000_000)
    p, _ = buy(setup)
    sell(setup, p["id"], "500")
    values = risk.metrics(journal, rpc.genesis(), wallet.address)
    assert values["daily_loss"] == 1_027_140
    assert values["realized_drawdown"] == 1_027_140
    reopened = TradeJournal(directory)
    try:
        service = ManualTradeService(rpc, reopened)
        with pytest.raises(TransferError, match="Daily realized loss limit"):
            service.preview(wallet.address, MINT, "buy", "0.1")
        configure(setup, daily_loss_lamports=1_000_000, entries_paused=True)
        sell((wallet, rpc, reopened, service, directory), p["id"], "500")
        service.reconcile(wallet.address)
        values = risk.metrics(reopened, rpc.genesis(), wallet.address)
        assert values["daily_loss"] == 2_054_280 and values["open_cost"] == 0
        # Saving does not reset losses; the UTC boundary resets only daily loss.
        timestamp = journal.trades(wallet.address)[0]["receipt"]["block_time"]
        next_day = int(timestamp // 86400 + 1) * 86400
        assert (
            risk.metrics(reopened, rpc.genesis(), wallet.address, now=next_day)["daily_loss"] == 0
        )
        assert (
            risk.metrics(reopened, rpc.genesis(), wallet.address, now=next_day)["realized_drawdown"]
            == 2_054_280
        )
    finally:
        reopened.close()


def test_activation_includes_legacy_receipts_and_drawdown_from_profit_peak(setup):
    wallet, rpc, journal, service, _ = setup
    p, _ = buy(setup)
    effects = rpc.effects
    rpc.effects = (
        lambda amount, buying: effects(amount, buying) if buying else (150_000_000, -amount)
    )
    sell(setup, p["id"])
    p, _ = buy(setup)
    rpc.effects = (
        lambda amount, buying: effects(amount, buying) if buying else (50_000_000, -amount)
    )
    sell(setup, p["id"])
    configure(setup, drawdown_lamports=40_000_000)
    values = risk.metrics(journal, rpc.genesis(), wallet.address)
    assert values["realized_peak"] == 47_955_720
    assert values["realized_drawdown"] == 52_044_280
    assert values["daily_loss"] == 52_044_280
    with pytest.raises(TransferError, match="drawdown limit"):
        service.preview(wallet.address, MINT, "buy", "0.1")


def test_pending_unknown_cost_and_network_isolation(setup):
    wallet, rpc, journal, service, _ = setup
    configure(setup)
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    rpc.disconnect = True
    service.submit(p["id"], wallet)
    values = risk.metrics(journal, rpc.genesis(), wallet.address)
    assert values["committed_cost"] == 120_005_000 and values["open_cost"] == 0
    assert risk.policy(journal, "other-network", wallet.address) is None
    assert risk.metrics(journal, "other-network", wallet.address)["committed_cost"] == 0
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    service.reconcile(wallet.address)
    values = risk.metrics(journal, rpc.genesis(), wallet.address)
    assert values["committed_cost"] == 0 and values["open_cost"] == 102_044_280


def test_failed_trade_fees_count_once(setup):
    wallet, rpc, journal, service, _ = setup
    configure(setup, daily_loss_lamports=5000)
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    service.submit(p["id"], wallet)
    row = journal.trades(wallet.address)[0]
    # Settlement-boundary fee-only receipt, independent of the successful bank stub.
    receipt = dict(err="failed", wallet_delta=-5000, token_delta=0, block_time=p["created_at"])
    journal.settle(row, receipt)
    journal.settle(row, receipt)
    assert risk.metrics(journal, rpc.genesis(), wallet.address)["daily_loss"] == 5000
    with pytest.raises(TransferError, match="Daily realized loss"):
        service.preview(wallet.address, MINT, "buy", "0.1")


@pytest.mark.parametrize(
    "change",
    [
        {"daily_loss_lamports": 0},
        {"reserve_lamports": -1},
        {"max_buy_lamports": True},
        {"max_buy_lamports": 2**64},
        {"entries_paused": "yes"},
        {"max_exposure_lamports": 1},
    ],
)
def test_invalid_policy_never_overwrites_saved_limits(setup, change):
    wallet, rpc, journal = setup[:3]
    configure(setup)
    with pytest.raises(TransferError):
        configure(setup, **change)
    assert risk.policy(journal, rpc.genesis(), wallet.address) == limits()
    assert journal.db.execute("SELECT COUNT(*) FROM wallet_risk_changes").fetchone()[0] == 1


def test_incomplete_legacy_history_blocks_buys_but_not_sells(setup):
    wallet, rpc, journal, service, _ = setup
    p, _ = buy(setup)
    configure(setup)
    with journal.db:
        journal.db.execute("UPDATE trades SET receipt=NULL WHERE id=?", (p["id"],))
    with pytest.raises(TransferError, match="finalized trade receipts"):
        service.preview(wallet.address, MINT, "buy", "0.1")
    assert service.preview(wallet.address, MINT, "sell", "500", position_id=p["id"])


def test_final_gate_rechecks_policy_even_without_balance_revision(setup):
    wallet, rpc, journal, service, _ = setup
    configure(setup)
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    # Exercise the atomic reservation guard directly, independently of early quote checks.
    with journal.db:
        journal.db.execute(
            "UPDATE wallet_risk_policies SET payload=?", (json.dumps(limits(entries_paused=True)),)
        )
    signed = []
    with pytest.raises(TransferError, match="paused"):
        journal.reserve_trade(p, signer=lambda: signed.append(True))
    assert not signed and not rpc.sent
