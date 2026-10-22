"""Cross-connection wallet holds; all RPC responses and fills are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.reservations import release, require_available, reserve, rows
from pumpwizard_runner.transfers import TransferError
from test_manual_trades import MINT, buy, setup as _trade_setup
from test_transfers import setup as _withdrawal_setup


@pytest.fixture(name='trade_setup')
def trade_fixture(tmp_path):
    yield from _trade_setup.__wrapped__(tmp_path)


@pytest.fixture(name='withdrawal_setup')
def withdrawal_fixture(tmp_path):
    yield from _withdrawal_setup.__wrapped__(tmp_path)


def test_competing_agents_cannot_double_reserve_and_restart_preserves_holds(withdrawal_setup, tmp_path):
    wallet, _, rpc, _, _ = withdrawal_setup
    barrier = Barrier(2)

    def allocate(agent):
        journal = TradeJournal(tmp_path)
        try:
            barrier.wait(timeout=10)
            try:
                return reserve(journal, rpc, agent, wallet.address, lamports=1_500_000_001)
            except TransferError:
                return None
        finally:
            journal.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(allocate, ['agent-a', 'agent-b']))
    assert sum(r is not None for r in results) == 1
    journal = TradeJournal(tmp_path)
    try:
        held = rows(journal)
        assert len(held) == 1 and held[0]['lamports'] == '1500000001'
        assert held[0]['status'] == 'held'
        with pytest.raises(TransferError, match='reserved'):
            reserve(journal, rpc, 'agent-c', wallet.address, lamports=500_000_000)
        reserve(journal, rpc, 'agent-c', wallet.address, lamports=499_999_999)
        assert len(rows(journal)) == 2
        release(journal, held[0]['id'])
        assert release(journal, held[0]['id'])['status'] == 'released'
        reserve(journal, rpc, 'agent-c', wallet.address, lamports=1_500_000_001)
    finally:
        journal.close()
    assert rpc.sent == []


def test_withdrawal_review_and_submission_respect_new_holds(withdrawal_setup):
    wallet, recipient, rpc, journal, service = withdrawal_setup
    preview = service.preview(wallet.address, recipient, '0.5')
    hold = reserve(journal, rpc, 'agent-a', wallet.address, lamports=1_600_000_000)
    with pytest.raises(TransferError, match='reserved'):
        service.preview(wallet.address, recipient, '0.5')
    with pytest.raises(TransferError, match='reserved'):
        service.submit(preview, wallet)
    assert rpc.sent == [] and journal.rows() == []
    release(journal, hold['id'])
    service.submit(preview, wallet)
    assert len(rpc.sent) == 1
    with pytest.raises(TransferError, match='pending'):
        reserve(journal, rpc, 'agent-a', wallet.address, lamports=1)


def test_atomic_withdrawal_guard_catches_hold_added_at_signing(withdrawal_setup):
    wallet, recipient, rpc, journal, service = withdrawal_setup
    preview = service.preview(wallet.address, recipient, '0.5')

    class RacingSigner:
        address = wallet.address

        def sign(self, message):
            reserve(journal, rpc, 'agent-a', wallet.address, lamports=1_600_000_000)
            return wallet.sign(message)

    with pytest.raises(TransferError, match='reserved'):
        service.submit(preview, RacingSigner())
    assert not rpc.sent and not journal.rows()
    assert not journal.db.execute('SELECT 1 FROM wallet_intents').fetchone()


def test_manual_buy_cannot_consume_reserved_sol(trade_setup):
    wallet, rpc, journal, service, _ = trade_setup
    preview = service.preview(wallet.address, MINT, 'buy', '0.1')
    hold = reserve(journal, rpc, 'agent-a', wallet.address, lamports=2_900_000_000)
    with pytest.raises(TransferError, match='reserved'):
        service.preview(wallet.address, MINT, 'buy', '0.1')
    with pytest.raises(TransferError, match='reserved'):
        service.submit(preview['id'], wallet)
    assert not rpc.sent
    release(journal, hold['id'])
    service.submit(preview['id'], wallet)
    assert len(rpc.sent) == 1


def test_atomic_trade_guard_catches_hold_added_before_signing_lock(trade_setup):
    wallet, rpc, journal, service, _ = trade_setup
    preview = service.preview(wallet.address, MINT, 'buy', '0.1')

    call = rpc.call
    def racing_call(method, params):
        if method == 'getBlockHeight':
            rpc.call = call
            reserve(journal, rpc, 'agent-a', wallet.address, lamports=2_900_000_000)
        return call(method, params)
    rpc.call = racing_call

    with pytest.raises(TransferError, match='reserved'):
        service.submit(preview['id'], wallet)
    assert not rpc.sent and not journal.trades(wallet.address)
    assert not journal.pending(wallet.address, rpc.genesis())


def test_partial_sell_preserves_agent_tokens_then_release_allows_full_exit(trade_setup):
    wallet, rpc, journal, service, directory = trade_setup
    preview, _ = buy(trade_setup)
    hold = reserve(journal, rpc, 'agent-a', wallet.address,
                   position_id=preview['id'], token_atoms=600_000_000, lamports=50_000_000)
    with pytest.raises(TransferError, match='reserved'):
        reserve(journal, rpc, 'agent-b', wallet.address, position_id=preview['id'], token_atoms=400_000_001)
    with pytest.raises(TransferError, match='reserved'):
        service.preview(wallet.address, MINT, 'sell', '401', position_id=preview['id'])
    sale = service.preview(wallet.address, MINT, 'sell', '400', position_id=preview['id'])
    rpc.disconnect = True
    service.submit(sale['id'], wallet)
    reopened = TradeJournal(directory)
    try:
        assert rows(reopened)[0]['status'] == 'held'
        with pytest.raises(TransferError, match='pending'):
            release(reopened, hold['id'])
        service.reconcile(wallet.address)
        assert journal.position(preview['id'])['quantity'] == '600000000'
        with pytest.raises(TransferError, match='reserved'):
            service.preview(wallet.address, MINT, 'sell', '1', position_id=preview['id'])
        release(reopened, hold['id'])
        sale = service.preview(wallet.address, MINT, 'sell', '600', position_id=preview['id'])
        service.submit(sale['id'], wallet)
        service.reconcile(wallet.address)
        assert journal.position(preview['id'])['status'] == 'closed'
    finally:
        reopened.close()


def test_position_specific_hold_cannot_be_borrowed_from_second_position(trade_setup):
    wallet, rpc, journal, service, _ = trade_setup
    first, _ = buy(trade_setup)
    second = service.preview(wallet.address, MINT, 'buy', '0.2')
    service.submit(second['id'], wallet)
    service.reconcile(wallet.address)
    reserve(journal, rpc, 'agent-a', wallet.address, position_id=first['id'], token_atoms=900_000_000)
    with pytest.raises(TransferError, match='Position quantity'):
        reserve(journal, rpc, 'agent-b', wallet.address, position_id=first['id'], token_atoms=100_000_001)
    with pytest.raises(TransferError, match='Position quantity'):
        service.preview(wallet.address, MINT, 'sell', '101', position_id=first['id'])


def test_deficits_fail_closed_but_other_network_is_independent(withdrawal_setup):
    wallet, _, rpc, journal, _ = withdrawal_setup
    hold = reserve(journal, rpc, 'agent-a', wallet.address, lamports=1_000_000_000)
    rpc.balance = 900_000_000
    with pytest.raises(TransferError, match='reserved'):
        reserve(journal, rpc, 'agent-b', wallet.address, lamports=1)
    require_available(journal, 'different-network', wallet.address, 100, 100)
    release(journal, hold['id'])
    reserve(journal, rpc, 'agent-b', wallet.address, lamports=rpc.balance)


@pytest.mark.parametrize('amount', [True, -1, 1.5, 2**64])
def test_invalid_units_leave_no_hold(withdrawal_setup, amount):
    wallet, _, rpc, journal, _ = withdrawal_setup
    with pytest.raises(TransferError):
        reserve(journal, rpc, 'agent-a', wallet.address, lamports=amount)
    assert rows(journal) == []


def test_failed_balance_read_rolls_back_and_releases_database_lock(withdrawal_setup):
    wallet, _, rpc, journal, _ = withdrawal_setup

    class BrokenRpc:
        genesis = rpc.genesis

        def call(self, method, params):
            raise TransferError('connection lost')

    with pytest.raises(TransferError, match='connection lost'):
        reserve(journal, BrokenRpc(), 'agent-a', wallet.address, lamports=1)
    assert rows(journal) == [] and not journal.db.in_transaction
    reserve(journal, rpc, 'agent-a', wallet.address, lamports=1)


def test_cli_reserve_list_release_and_unknown_instance(withdrawal_setup, tmp_path, monkeypatch, capsys):
    import json
    from pumpwizard_runner import cli, manual_trades
    from test_orders import _library

    wallet, _, rpc, _, _ = withdrawal_setup
    library, instance = _library(tmp_path)
    library.close()
    calls = []

    def connect(network, url):
        calls.append((network, url))
        return rpc

    monkeypatch.setattr(manual_trades, 'TradeRpc', connect)
    prefix = ['--data-dir', str(tmp_path)]
    assert cli.main(prefix + ['reserve-agent', 'unknown', wallet.address, '--sol', '0.1']) == 2
    assert 'Unknown strategy instance' in capsys.readouterr().err
    assert calls == []
    assert cli.main(prefix + ['reserve-agent', instance, wallet.address, '--sol', '0.100000001']) == 0
    hold = json.loads(capsys.readouterr().out)
    assert hold['agent_id'] == instance and hold['lamports'] == '100000001'
    assert cli.main(prefix + ['agent-reservations', '--sender', wallet.address]) == 0
    assert json.loads(capsys.readouterr().out) == [hold]
    assert cli.main(prefix + ['release-agent-reservation', hold['id']]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'released'
    assert len(calls) == 1 and rpc.sent == []


@pytest.mark.parametrize('extra', [
    {}, {'token_atoms': 1}, {'position_id': 'missing'},
    {'lamports': 1, 'position_id': 'missing'},
])
def test_invalid_position_amount_pair_is_rejected(withdrawal_setup, extra):
    wallet, _, rpc, journal, _ = withdrawal_setup
    with pytest.raises(TransferError, match='Reserve positive'):
        reserve(journal, rpc, 'agent-a', wallet.address, **extra)
    assert rows(journal) == []


def test_completed_trade_during_validation_invalidates_old_balance_snapshot(trade_setup):
    wallet, rpc, journal, service, _ = trade_setup
    first = service.preview(wallet.address, MINT, 'buy', '0.1')
    other = service.preview(wallet.address, MINT, 'buy', '0.2')

    # Signing now holds the write lock. Inject the competing completed trade
    # at the last RPC check before that lock, where concurrency remains possible.
    call = rpc.call
    def racing_call(method, params):
        if method == 'getBlockHeight':
            rpc.call = call
            service.submit(other['id'], wallet)
            rpc.status = {'confirmationStatus': 'finalized', 'err': None}
            service.reconcile(wallet.address)
            assert not journal.pending(wallet.address, rpc.genesis())
        return call(method, params)
    rpc.call = racing_call

    with pytest.raises(TransferError, match='balance snapshot'):
        service.submit(first['id'], wallet)
    assert len(rpc.sent) == len(journal.trades(wallet.address)) == 1
    assert journal.trades(wallet.address)[0]['id'] == other['id']


def test_completed_withdrawal_during_validation_invalidates_snapshot(withdrawal_setup):
    wallet, recipient, rpc, journal, service = withdrawal_setup
    first = service.preview(wallet.address, recipient, '0.1')
    other = service.preview(wallet.address, recipient, '0.2')

    class RacingSigner:
        address = wallet.address

        def sign(self, message):
            service.submit(other, wallet)
            rpc.status = {'confirmationStatus': 'finalized', 'err': None}
            service.reconcile(wallet.address)
            assert not journal.db.execute('SELECT 1 FROM wallet_intents').fetchone()
            return wallet.sign(message)

    with pytest.raises(TransferError, match='balance snapshot'):
        service.submit(first, RacingSigner())
    assert len(rpc.sent) == len(journal.rows()) == 1
    assert journal.rows()[0]['id'] == other.id
