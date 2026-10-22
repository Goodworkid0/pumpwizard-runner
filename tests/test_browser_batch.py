import base64
import json
import time

import pytest

from pumpwizard_runner import web_trading, web_agent_orders, web_transfers, web_automation
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.strategy_risk import save_policy
from pumpwizard_runner.transfers import GENESIS, TransferError
from pumpwizard_runner.wallet_vault import WalletVault
from test_agent_execution import setup as _agent_setup, observe, MINT
from test_strategy_risk import limits
from test_transfers import Rpc


def signed(wallet, preview):
    message = base64.b64decode(preview['unsigned_transaction'])[65:]
    return base64.b64encode(b'\x01'+wallet.sign(message)+message).decode()


@pytest.fixture
def agent(tmp_path, monkeypatch):
    generator = _agent_setup.__wrapped__(tmp_path)
    values = next(generator)
    wallet, rpc, journal, library, ident, _, _, _ = values
    rpc.network = 'mainnet-beta'
    monkeypatch.setattr(rpc, 'genesis', lambda: GENESIS['mainnet-beta'])
    monkeypatch.setattr(web_trading, 'rpc_for', lambda network, directory=None: rpc)
    with journal.db:
        journal.db.execute('UPDATE agent_reservations SET genesis=?', (rpc.genesis(),))
    save_policy(journal, ident, rpc.genesis(), wallet.address, limits(max_trade_loss_bps=10000), rpc.balance)
    yield values
    next(generator, None)


def test_browser_strategy_signed_buy_partial_restart_and_stop_exit(agent):
    wallet, rpc, journal, library, ident, _, _, directory = agent
    identity = dict(sender=wallet.address, network=rpc.network)
    def prepare(position=None):
        return web_agent_orders.command(library, 'preview', dict(**identity, agent_id=ident, mint=MINT, position_id=position, slippage_bps=100))['preview']
    def fill(preview):
        result = web_agent_orders.command(library, 'submit', dict(**identity, preview_id=preview['id'], signed_transaction=signed(wallet, preview)))
        assert result['state'] == 'pending'
        rpc.status = {'confirmationStatus': 'finalized', 'err': None}
        return web_trading.reconcile(directory, identity)
    buy = prepare()
    assert buy['agent_order']['agent_id'] == ident
    assert web_agent_orders.overview(library, **identity)['sessions'][0]['reviews'][0]['id'] == buy['id']
    state = fill(buy)
    position = state['positions'][0]
    assert position['managed'] and position['agent_id'] == ident
    web_agent_orders.command(library, 'entries', dict(**identity, agent_id=ident, enabled=False))
    observe(library, ident, time.time(), 320)
    partial = prepare(buy['id'])
    assert int(partial['amount']) == int(position['quantity'])*4//10
    fill(partial)
    reopened = StrategyLibrary(directory)
    try:
        observe(reopened, ident, time.time(), 170)
        full = web_agent_orders.command(reopened, 'preview', dict(**identity, agent_id=ident, mint=MINT, position_id=buy['id'], slippage_bps=100))['preview']
        assert full['agent_order']['reason'] == 'stop_loss'
        state = fill(full)
        assert state['positions'][0]['status'] == 'closed'
        assert len(state['orders']) == 3
        assert all(order['agent_id'] == ident for order in state['orders'])
    finally:
        reopened.close()


def test_browser_strategy_pause_cancel_and_manual_release(agent):
    wallet, rpc, journal, library, ident, _, _, directory = agent
    identity = dict(sender=wallet.address, network=rpc.network)
    review = dict(**identity, agent_id=ident, mint=MINT, position_id=None, slippage_bps=100)
    p = web_agent_orders.command(library, 'preview', review)['preview']
    web_agent_orders.command(library, 'entries', dict(**identity, agent_id=ident, enabled=False))
    with pytest.raises(TransferError, match='paused'):
        web_agent_orders.command(library, 'submit', dict(**identity, preview_id=p['id'], signed_transaction=signed(wallet, p)))
    assert not rpc.sent
    web_agent_orders.command(library, 'cancel', dict(**identity, preview_id=p['id']))
    assert not web_agent_orders.overview(library, **identity)['sessions'][0]['reviews']
    web_agent_orders.command(library, 'entries', dict(**identity, agent_id=ident, enabled=True))
    p = web_agent_orders.command(library, 'preview', review)['preview']
    web_agent_orders.command(library, 'submit', dict(**identity, preview_id=p['id'], signed_transaction=signed(wallet, p)))
    rpc.status = {'confirmationStatus': 'finalized', 'err': None}
    web_trading.reconcile(directory, identity)
    with pytest.raises(TransferError):
        web_agent_orders.command(library, 'release-position', dict(**identity, position_id=p['id'], acknowledge=False))
    web_agent_orders.command(library, 'release-position', dict(**identity, position_id=p['id'], acknowledge='release-managed-exit'))
    position = web_trading.state(directory, **identity)['positions'][0]
    assert not position['managed'] and position['agent_id'] == ident
    sell = web_trading.preview(directory, dict(**identity, mint=MINT, side='sell', amount=str(int(position['quantity'])/1_000_000), slippage_bps=100, position_id=p['id']))['preview']
    web_trading.submit(directory, dict(**identity, preview_id=sell['id'], signed_transaction=signed(wallet, sell)))
    assert web_trading.reconcile(directory, identity)['positions'][0]['status'] == 'closed'


@pytest.fixture
def transfer(tmp_path, monkeypatch):
    wallet = WalletVault(tmp_path/'main').create('browser withdrawal password')
    recipient = WalletVault(tmp_path/'to').create('browser recipient password').address
    rpc = Rpc()
    rpc.network = 'mainnet-beta'
    monkeypatch.setattr(rpc, 'genesis', lambda: GENESIS[rpc.network])
    monkeypatch.setattr(web_trading, 'rpc_for', lambda network, directory=None: rpc)
    return tmp_path, wallet, recipient, rpc


def test_browser_withdrawal_unknown_restart_and_exact_signature(transfer):
    directory, wallet, recipient, rpc = transfer
    identity = dict(sender=wallet.address, network=rpc.network)
    preview = web_transfers.command(directory, 'preview', dict(**identity, recipient=recipient, amount='0.01'))['preview']
    rpc.disconnect = True
    body = dict(**identity, preview_id=preview['id'], signed_transaction=signed(wallet, preview))
    signature = web_transfers.command(directory, 'submit', body)['signature']
    state = web_trading.state(directory, **identity)
    assert state['pending'] and state['withdrawals'][0]['status'] == 'unknown'
    assert state['withdrawals'][0]['signature'] == signature
    with pytest.raises(TransferError):
        web_transfers.command(directory, 'submit', body)
    rpc.status = {'confirmationStatus': 'finalized', 'err': None}
    state = web_trading.reconcile(directory, identity)
    assert not state['pending'] and state['withdrawals'][0]['status'] == 'finalized'
    assert len(rpc.sent) == 1


@pytest.mark.parametrize('change', ['recipient', 'signer', 'balance', 'expiry'])
def test_browser_withdrawal_changed_review_cannot_broadcast(transfer, change):
    directory, wallet, recipient, rpc = transfer
    identity = dict(sender=wallet.address, network=rpc.network)
    p = web_transfers.command(directory, 'preview', dict(**identity, recipient=recipient, amount='0.01'))['preview']
    transaction = signed(wallet, p)
    if change == 'balance':
        rpc.balance -= 1
    if change == 'expiry':
        rpc.height = 1000
    if change == 'signer':
        transaction = signed(WalletVault(directory/'other').create('different wallet password'), p)
    if change == 'recipient':
        raw = bytearray(base64.b64decode(transaction))
        raw[101] ^= 1
        transaction = base64.b64encode(raw).decode()
    with pytest.raises(TransferError):
        web_transfers.command(directory, 'submit', dict(**identity, preview_id=p['id'], signed_transaction=transaction))
    assert not rpc.sent


def test_automation_separate_encrypted_backup_owner_and_restart_gate(tmp_path):
    main = WalletVault(tmp_path/'main').create('main wallet password')
    library = StrategyLibrary(tmp_path/'runner')
    identity = dict(sender=main.address, network='mainnet-beta')
    try:
        state = web_automation.command(library, 'create', dict(**identity, password='automation password'))
        assert state['address'] != main.address and state['locked'] and not state['active']
        backup = web_automation.command(library, 'backup', dict(**identity, password='automation password'))['backup']
        assert 'seed' not in json.dumps(backup) and 'ciphertext' in json.dumps(backup)
        with pytest.raises(TransferError):
            web_automation.state(library.path.parent, state['address'])
        for action in ('authorize', 'start'):
            with pytest.raises(TransferError):
                web_automation.command(library, action, identity)
        stopped = web_automation.command(library, 'stop', identity)
        assert stopped['stopped_at'] and not stopped['active']
        restored = StrategyLibrary(tmp_path/'restore')
        try:
            result = web_automation.command(restored, 'restore', dict(**identity, password='automation password', backup=backup))
            assert result['address'] == state['address'] and result['locked']
        finally:
            restored.close()
    finally:
        library.close()


def test_spendable_balance_excludes_allocations_and_fees(agent):
    wallet, rpc, _, _, _, _, _, directory = agent
    funds = web_trading.inspect_wallet(dict(sender=wallet.address, network=rpc.network), directory)
    assert funds['reserved_lamports'] == '200000000'
    assert int(funds['buy_capacity_lamports']) <= int(funds['unallocated_lamports'])-int(funds['buy_buffer_lamports'])


def test_automation_draft_limits_are_bounded_and_never_authorize(agent):
    wallet, _, _, library, ident, _, _, _ = agent
    identity = dict(sender=wallet.address, network='mainnet-beta')
    web_automation.command(library, 'create', dict(**identity, password='automation password'))
    plan = dict(**identity, sessions=[ident], budget_sol='0.05', max_order_sol='0.01', daily_buy_sol='0.02',
                daily_loss_sol='0.005', reserve_sol='0.01', portfolio_drawdown_sol='0.01',
                max_buys_per_day=2, max_trades_per_day=10, max_positions=2,
                slippage_bps=100, duration_minutes=15, daily_loss_bps=1000, trade_loss_bps=2000,
                priority_fee_lamports=0)
    state = web_automation.command(library, 'plan', plan)
    assert state['plan']['status'] == 'draft' and not state['active']
    for changed in ({'budget_sol':'0.100000001'}, {'duration_minutes':61}, {'priority_fee_lamports':1},
                    {'max_buys_per_day':11}, {'slippage_bps':501}, {'sessions':[]}):
        with pytest.raises(TransferError):
            web_automation.command(library, 'plan', dict(plan, **changed))
    assert web_automation.state(library.path.parent, wallet.address)['plan'] == state['plan']


def test_browser_funding_budget_returns_exact_amounts_and_uncertain_state(transfer):
    directory, wallet, recipient, rpc = transfer
    identity = dict(sender=wallet.address, network=rpc.network)
    preview = web_transfers.command(directory, 'preview', dict(**identity, recipient=recipient, amount='2', funding_budget=True))['preview']
    assert preview['funding_budget'] == '2000000000'
    assert preview['source_reserve'] == '2000000'
    assert preview['lamports'] == '1997995000'
    rpc.disconnect = True
    result = web_transfers.command(directory, 'submit', dict(**identity, preview_id=preview['id'], signed_transaction=signed(wallet, preview)))
    assert result['state'] == 'unknown'
    assert 'uncertain' in result['detail'].lower()
    assert result['signature']


def test_browser_rejects_non_boolean_funding_budget(transfer):
    directory, wallet, recipient, rpc = transfer
    with pytest.raises(TransferError, match='review requires'):
        web_transfers.command(directory, 'preview', dict(sender=wallet.address, network=rpc.network, recipient=recipient, amount='1', funding_budget='true'))
    assert not rpc.sent
