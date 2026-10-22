from contextlib import closing
import json

import pytest
from solders.keypair import Keypair

from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.session_allocations import reserve_workspace
from pumpwizard_runner.strategy_risk import policy, save_policy
from pumpwizard_runner.transfers import GENESIS, TransferError
from pumpwizard_runner.web_api import command, payload
from test_session_allocations import limits
from test_session_controls import Authorizer, attached


@pytest.fixture
def account(tmp_path, monkeypatch):
    sender = str(Keypair().pubkey())
    genesis = GENESIS['mainnet-beta']
    class Rpc:
        balance = 250_000_000
        def genesis(self): return genesis
        def call(self, method, args):
            assert method == 'getBalance'
            return {'value': self.balance}
    rpc = Rpc()
    monkeypatch.setattr('pumpwizard_runner.web_trading.rpc_for', lambda *args: rpc)
    with closing(StrategyLibrary(tmp_path)) as library:
        workspace = library.create_session_workspace('trade', wallet_sender=sender, wallet_network='mainnet-beta', allocation_lamports='100000000', plan={'allocation_sol': '0.1', 'max_trade_sol': '0.05', 'max_daily_buy_sol': '0.06', 'max_daily_loss_sol': '0.01'})
        ident = attached(library, workspace, 'Funded strategy')
        library.pause_workspace(workspace['id'])
        with closing(TradeJournal(tmp_path)) as journal:
            reserve_workspace(journal, workspace['id'], genesis, sender, 100_000_000, rpc.balance)
            reserve_workspace(journal, 'other-workspace', genesis, sender, 100_000_000, rpc.balance)
            save_policy(journal, ident, genesis, sender, limits(allocation_lamports=100_000_000), rpc.balance, workspace_id=workspace['id'])
        query = {'sender': [sender], 'network': ['mainnet-beta']}
        path = f"/api/v1/session-workspaces/{workspace['id']}/funds"
        def read(): return payload(library, path, query, wallet_authorizer=Authorizer())['funds']
        def update(amount, snapshot=None):
            snapshot = snapshot or read()
            return command(library, path, {'allocation_sol': amount, 'expected_allocation_lamports': snapshot['allocation_lamports'], 'wallet_revision': snapshot['wallet_revision']}, query=query, wallet_authorizer=Authorizer())['funds']
        yield library, workspace, ident, rpc, read, update, path, query


def test_top_up_and_release_update_cash_metadata_and_policy_without_increasing_order_limits(account):
    library, workspace, ident, rpc, read, update, _, _ = account
    before = read()
    assert before['unallocated_lamports'] == '50000000'
    assert before['maximum_allocation_lamports'] == '150000000'
    result = update('0.12')
    assert result['available_lamports'] == '120000000'
    assert library.session_workspace(workspace['id'])['allocation_lamports'] == '120000000'
    assert library.session_workspace(workspace['id'])['plan']['allocation_sol'] == '0.12'
    with closing(TradeJournal(library.path.parent)) as journal:
        saved = policy(journal, ident, GENESIS['mainnet-beta'], workspace['wallet_sender'])
        assert saved['allocation_lamports'] == 120_000_000
        assert saved['max_trade_lamports'] == 50_000_000
        assert saved['max_daily_buy_lamports'] == 60_000_000
        assert journal.db.execute("SELECT allocation_lamports FROM session_workspace_allocations WHERE workspace_id='other-workspace'").fetchone()[0] == '100000000'
        # Cash has been consumed by prior receipt accounting. It is not releasable.
        journal.db.execute("UPDATE agent_reservations SET lamports='60000000' WHERE agent_id=?", ('workspace:'+workspace['id'],))
        journal.db.commit()
    with pytest.raises(TransferError, match='Only available'):
        update('0.04')
    result = update('0.07')
    assert result['available_lamports'] == '10000000'
    assert library.instance(ident)['status'] == 'paused'
    with closing(TradeJournal(library.path.parent)) as journal:
        assert journal.db.execute('SELECT COUNT(*) FROM session_workspace_allocation_changes').fetchone()[0] == 2
        assert journal.db.execute('SELECT COUNT(*) FROM trades').fetchone()[0] == 0


def test_stale_overdraw_and_foreign_wallet_edits_leave_both_stores_unchanged(account):
    library, workspace, ident, rpc, read, update, path, query = account
    before = read()
    with pytest.raises(TransferError, match='exceeds unallocated'):
        update('0.16')
    rpc.balance = 210_000_000
    with pytest.raises(TransferError, match='exceeds unallocated'):
        update('0.12', before)
    rpc.balance = 250_000_000
    update('0.12', before)
    with pytest.raises(TransferError, match='funds changed'):
        update('0.13', before)
    stale_revision = read()
    with closing(TradeJournal(library.path.parent)) as journal:
        journal.db.execute('UPDATE wallet_revisions SET revision=revision+1')
        journal.db.commit()
    with pytest.raises(TransferError, match='balance snapshot'):
        update('0.13', stale_revision)
    for q in ({'sender': [str(Keypair().pubkey())], 'network': ['mainnet-beta']}, {'scope': ['research']}, {'sender': query['sender'], 'network': ['devnet']}):
        with pytest.raises(ValueError, match='current wallet'):
            command(library, path, {}, query=q, wallet_authorizer=Authorizer())
        with pytest.raises(ValueError, match='current wallet'):
            payload(library, path, q, wallet_authorizer=Authorizer())
    assert library.session_workspace(workspace['id'])['allocation_lamports'] == '120000000'
    with closing(TradeJournal(library.path.parent)) as journal:
        assert journal.db.execute('SELECT COUNT(*) FROM session_workspace_allocation_changes').fetchone()[0] == 1


def test_running_strategy_pending_wallet_and_active_allowance_prevent_changes(account):
    library, workspace, ident, rpc, read, update, _, _ = account
    library.set_tracking(ident, True)
    with pytest.raises(TransferError, match='Pause all'):
        update('0.12')
    library.pause_workspace(workspace['id'])
    with closing(TradeJournal(library.path.parent)) as journal:
        journal.db.execute('INSERT INTO wallet_intents VALUES (?,?,?,?)', (GENESIS['mainnet-beta'], workspace['wallet_sender'], 'pending', 'withdrawal'))
        journal.db.commit()
    with pytest.raises(TransferError, match='original wallet'):
        update('0.12')
    from pumpwizard_runner.main_wallet_autopilot import install
    import time
    with closing(TradeJournal(library.path.parent)) as journal:
        journal.db.execute('DELETE FROM wallet_intents')
        journal.db.commit()
        install(journal)
        cash = journal.db.execute('SELECT cash_hold FROM session_workspace_allocations WHERE workspace_id=?', (workspace['id'],)).fetchone()[0]
        journal.db.execute('INSERT INTO main_wallet_allowances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('11'*16, '22'*16, ident, GENESIS['mainnet-beta'], workspace['wallet_sender'], '33'*32, 1, 1, '{}', '{}', json.dumps({'cash_hold': cash}), time.time(), time.time()+60, 'authorized', 0, 'fixture'))
        journal.db.commit()
    with pytest.raises(TransferError, match='trading allowance'):
        update('0.12')
    assert library.session_workspace(workspace['id'])['allocation_lamports'] == '100000000'


def test_reduction_clamps_order_caps_to_budget(account):
    library, workspace, ident, _, _, update, _, _ = account
    result = update('0.04')
    assert result['available_lamports'] == '40000000'
    with closing(TradeJournal(library.path.parent)) as journal:
        saved = policy(journal, ident, GENESIS['mainnet-beta'], workspace['wallet_sender'])
        assert saved['max_trade_lamports'] == 40_000_000
        assert saved['max_daily_buy_lamports'] == 40_000_000
        assert saved['max_daily_loss_lamports'] == 10_000_000
    assert library.session_workspace(workspace['id'])['plan']['max_trade_sol'] == '0.04'


def test_policy_error_rolls_back_all_changes_after_an_earlier_policy_update(account):
    library, workspace, ident, rpc, _, update, _, _ = account
    second = attached(library, workspace, 'Higher minimum')
    library.pause_workspace(workspace['id'])
    with closing(TradeJournal(library.path.parent)) as journal:
        save_policy(journal, second, GENESIS['mainnet-beta'], workspace['wallet_sender'], limits(allocation_lamports=100_000_000, min_trade_lamports=50_000_000), rpc.balance, workspace_id=workspace['id'])
    with pytest.raises(TransferError, match='Minimum trade'):
        update('0.04')
    assert library.session_workspace(workspace['id'])['allocation_lamports'] == '100000000'
    with closing(TradeJournal(library.path.parent)) as journal:
        assert policy(journal, ident, GENESIS['mainnet-beta'], workspace['wallet_sender'])['allocation_lamports'] == 100_000_000
        assert journal.db.execute('SELECT COUNT(*) FROM session_workspace_allocation_changes').fetchone()[0] == 0
        assert journal.db.execute('SELECT allocation_lamports FROM session_workspace_allocations WHERE workspace_id=?', (workspace['id'],)).fetchone()[0] == '100000000'


def test_unsigned_session_order_blocks_changes(account):
    library, workspace, ident, _, _, update, _, _ = account
    with closing(TradeJournal(library.path.parent)) as journal:
        cash = journal.db.execute('SELECT cash_hold FROM session_workspace_allocations WHERE workspace_id=?', (workspace['id'],)).fetchone()[0]
        journal.db.execute('INSERT INTO agent_orders VALUES (?,?,?,?,?,?,?,?,?)', ('review-one', ident, 'fixture-mint', 'buy', cash, None, 'review', '{}', 100))
        journal.db.commit()
    with pytest.raises(TransferError, match='Finish or cancel'):
        update('0.12')
    assert library.session_workspace(workspace['id'])['allocation_lamports'] == '100000000'


def test_concurrent_edits_cannot_both_use_the_same_wallet_snapshot(account):
    import threading
    from pumpwizard_runner.web_session_allocations import update as resize
    library, workspace, ident, _, read, _, _, _ = account
    snapshot = read()
    barrier = threading.Barrier(2)
    results = []
    def worker(amount):
        with closing(StrategyLibrary(library.path.parent)) as own:
            barrier.wait(timeout=10)
            try:
                resize(own, own.path.parent, workspace, {'allocation_sol': amount, 'expected_allocation_lamports': snapshot['allocation_lamports'], 'wallet_revision': snapshot['wallet_revision']})
                results.append('saved')
            except TransferError:
                results.append('stale')
    workers = [threading.Thread(target=worker, args=(amount,)) for amount in ('0.12', '0.13')]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join(timeout=15)
        assert not worker_thread.is_alive()
    assert sorted(results) == ['saved', 'stale']
    with closing(TradeJournal(library.path.parent)) as journal:
        assert journal.db.execute('SELECT COUNT(*) FROM session_workspace_allocation_changes').fetchone()[0] == 1
