from contextlib import closing
import json

import pytest
from solders.keypair import Keypair

from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.session_allocations import reserve_workspace
from pumpwizard_runner.session_funding import check, status
from pumpwizard_runner.transfers import GENESIS, TransferError
from pumpwizard_runner.web_api import command, payload
from test_session_controls import Authorizer, attached


@pytest.fixture
def funded(tmp_path, monkeypatch):
    sender = str(Keypair().pubkey())
    class Rpc:
        balance = 300_000_000
        failure = False
        network = GENESIS['mainnet-beta']
        hook = None
        def genesis(self): return self.network
        def call(self, method, params):
            assert method == 'getBalance'
            assert params == [sender, {'commitment': 'confirmed'}]
            if self.failure:
                raise TransferError('offline')
            if self.hook:
                self.hook()
            return {'value': self.balance}
    rpc = Rpc()
    monkeypatch.setattr('pumpwizard_runner.web_trading.rpc_for', lambda *args: rpc)
    with closing(StrategyLibrary(tmp_path)) as library:
        workspaces, ids = [], []
        for label in ('First', 'Second'):
            workspace = library.create_session_workspace('trade', label, wallet_sender=sender, wallet_network='mainnet-beta', allocation_lamports='100000000')
            ident = attached(library, workspace, label)
            library.set_tracking(ident, True)
            library.set_execution_modes(ident, entries='automatic', exits='automatic')
            workspaces.append(workspace)
            ids.append(ident)
        with closing(TradeJournal(tmp_path)) as journal:
            for workspace in workspaces:
                reserve_workspace(journal, workspace['id'], GENESIS['mainnet-beta'], sender, 100_000_000, rpc.balance)
        yield library, sender, rpc, workspaces, ids


def test_external_spending_pauses_entries_and_preserves_exits_for_shared_wallet(funded):
    library, sender, rpc, workspaces, ids = funded
    other = library.create_session_workspace('monitoring', wallet_sender=sender, wallet_network='mainnet-beta')
    untouched = attached(library, other, 'Observation')
    library.set_tracking(untouched, True)
    foreign = library.create_session_workspace('trade', wallet_sender=str(Keypair().pubkey()), wallet_network='mainnet-beta', allocation_lamports='100000000')
    foreign_id = attached(library, foreign, 'Foreign')
    library.set_tracking(foreign_id, True)
    with closing(TradeJournal(library.path.parent)) as journal:
        from pumpwizard_runner.main_wallet_autopilot import install
        install(journal)
        journal.db.execute('INSERT INTO main_wallet_allowances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('allowance', 'review', ids[0], GENESIS['mainnet-beta'], sender, 'digest', 1, 1, '{}', '{}', '{}', 100, 99999999999, 'active', 0, 'fixture'))
        journal.db.execute('INSERT INTO automation_authorizations VALUES (?,?,?,?,?,?,?,?,?,?,?)', ('legacy', sender, GENESIS['mainnet-beta'], 'separate-automation-wallet', 1, json.dumps({'strategies': {ids[0]: {}}}), 100, 99999999999, 'running', 0, 'fixture'))
        journal.db.execute('INSERT INTO automation_authorizations VALUES (?,?,?,?,?,?,?,?,?,?,?)', ('foreign-legacy', sender, GENESIS['mainnet-beta'], 'foreign-automation-wallet', 1, json.dumps({'strategies': {foreign_id: {}}}), 100, 99999999999, 'running', 0, 'fixture'))
        for ident in ids:
            journal.db.execute('INSERT INTO agent_orders VALUES (?,?,?,?,?,?,?,?,?)', (ident+'review', ident, 'MintABC', 'buy', ident+'hold', None, 'review', '{}', 100))
            journal.db.execute('INSERT INTO agent_orders VALUES (?,?,?,?,?,?,?,?,?)', (ident+'exit', ident, 'MintXYZ', 'sell', ident+'exit-hold', ident+'tokens', 'review', '{}', 100))
            journal.db.execute('INSERT INTO signing_sessions VALUES (?,?,?,?,?,?,?,?)', (ident+'lease', GENESIS['mainnet-beta'], sender, ident, '{}', 100, 99999999999, 'active'))
        journal.db.commit()
    rpc.balance = 800_000
    result = check(library, sender, 'mainnet-beta', rpc=rpc)
    assert result['state'] == 'insufficient'
    assert result['required_lamports'] == '200000000'
    assert result['shortfall_lamports'] == '199200000'
    revisions = []
    for ident in ids:
        instance = library.instance(ident)
        assert (instance['status'], instance['entry_mode'], instance['exit_mode']) == ('monitoring', 'disabled', 'automatic')
        assert library.tracking_settings(ident)['enabled']
        assert not library.tracking_settings(ident)['fault']
        revisions.append(library.tracking_settings(ident)['revision'])
    assert library.tracking_settings(untouched)['enabled']
    assert library.tracking_settings(foreign_id)['enabled']
    with closing(TradeJournal(library.path.parent)) as journal:
        assert {r[0] for r in journal.db.execute("SELECT status FROM agent_orders WHERE side='buy'")} == {'cancelled'}
        assert {r[0] for r in journal.db.execute("SELECT status FROM agent_orders WHERE side='sell'")} == {'review'}
        assert {r[0] for r in journal.db.execute('SELECT status FROM signing_sessions')} == {'active'}
        assert {r[0] for r in journal.db.execute('SELECT status FROM agent_reservations')} == {'held'}
        allowance = journal.db.execute('SELECT status,authorization_revision,detail FROM main_wallet_allowances').fetchone()
        assert (allowance['status'], allowance['authorization_revision']) == ('entry-paused', 1)
        assert 'Insufficient' in allowance['detail']
        assert dict(journal.db.execute('SELECT id,entries_paused FROM automation_authorizations')) == {'legacy': 1, 'foreign-legacy': 0}
        assert journal.db.execute('SELECT COUNT(*) FROM trades').fetchone()[0] == 0
    check(library, sender, 'mainnet-beta', rpc=rpc)
    assert [library.tracking_settings(i)['revision'] for i in ids] == revisions
    rows = payload(library, '/api/v1/session-workspaces', {'sender': [sender], 'network': ['mainnet-beta']})['rows']
    assert all(row['funding']['state'] == 'insufficient' and row['position_summary']['entries_paused'] for row in rows if row['mode'] == 'trade')
    assert [library.session_workspace(w['id'])['allocation_lamports'] for w in workspaces] == ['100000000'] * 2


def test_compare_remaining_cash_not_original_allocations_and_exact_boundary(funded):
    library, sender, rpc, _, ids = funded
    with closing(TradeJournal(library.path.parent)) as journal:
        journal.db.execute("UPDATE agent_reservations SET lamports='50000000'")
        journal.db.commit()
    rpc.balance = 100_000_000
    assert check(library, sender, 'mainnet-beta', rpc=rpc)['state'] == 'sufficient'
    assert all(library.tracking_settings(i)['enabled'] for i in ids)
    rpc.balance -= 1
    assert check(library, sender, 'mainnet-beta', rpc=rpc)['shortfall_lamports'] == '1'


def test_refill_never_resumes_and_restart_requires_fresh_balance(funded):
    library, sender, rpc, _, ids = funded
    rpc.balance = 0
    check(library, sender, 'mainnet-beta', rpc=rpc)
    library.set_tracking(ids[0], True)
    assert library.instance(ids[0])['entry_mode'] == 'disabled'
    with pytest.raises(TransferError, match='Insufficient wallet funds'):
        library.set_execution_modes(ids[0], entries='automatic', exits='automatic')
    rpc.balance = 200_000_000
    assert check(library, sender, 'mainnet-beta', rpc=rpc)['state'] == 'sufficient'
    assert all(library.instance(i)['entry_mode'] == 'disabled' for i in ids)
    library.set_execution_modes(ids[0], entries='automatic', exits='automatic')
    assert library.instance(ids[0])['entry_mode'] == 'automatic'
    assert library.instance(ids[1])['entry_mode'] == 'disabled'


@pytest.mark.parametrize('error', ['offline', 'network', 'malformed'])
def test_rpc_failure_does_not_fabricate_balance_or_pause_but_blocks_restart(funded, error):
    library, sender, rpc, _, ids = funded
    if error == 'offline':
        rpc.failure = True
    elif error == 'network':
        rpc.network = GENESIS['devnet']
    else:
        rpc.balance = -1
    result = check(library, sender, 'mainnet-beta', rpc=rpc)
    assert result['state'] == 'unavailable'
    assert result['balance_lamports'] is None
    assert all(library.tracking_settings(i)['enabled'] for i in ids)
    with pytest.raises(TransferError, match='could not be verified'):
        library.set_tracking(ids[0], True)


@pytest.mark.parametrize('during_sample', [False, True])
def test_pending_or_raced_local_activity_defers_shortfall(funded, during_sample):
    library, sender, rpc, _, ids = funded
    def change():
        with closing(TradeJournal(library.path.parent)) as journal:
            journal.db.execute('INSERT INTO wallet_intents VALUES (?,?,?,?)', (GENESIS['mainnet-beta'], sender, 'pending', 'trade'))
            journal.db.commit()
    if during_sample:
        rpc.hook = change
    else:
        change()
    rpc.balance = 0
    result = check(library, sender, 'mainnet-beta', rpc=rpc)
    assert result['state'] == 'pending'
    assert result['balance_lamports'] is None
    assert all(library.tracking_settings(i)['enabled'] for i in ids)
    with closing(TradeJournal(library.path.parent)) as journal:
        assert status(journal, sender, 'mainnet-beta')['state'] == 'pending'
        assert journal.pending(sender, GENESIS['mainnet-beta'])


def test_background_guard_works_with_collector_disabled_and_no_browser(funded, monkeypatch):
    import time
    from pumpwizard_runner.background_service import BackgroundServiceEngine
    library, sender, rpc, _, ids = funded
    monkeypatch.setattr('pumpwizard_runner.session_funding.CHECK_INTERVAL', 0.05)
    engine = BackgroundServiceEngine(library.path.parent)
    engine.start()
    try:
        assert not engine.store.job()['enabled']
        rpc.balance = 0
        deadline = time.monotonic() + 5
        while any(library.instance(i)['entry_mode'] != 'disabled' for i in ids):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        with closing(TradeJournal(library.path.parent)) as journal:
            assert status(journal, sender, 'mainnet-beta')['state'] == 'insufficient'
    finally:
        assert engine.stop()['stopped']


def test_user_entry_pause_is_scoped_and_preserves_monitoring_and_exit_reviews(funded):
    library, sender, rpc, workspaces, ids = funded
    query = {'sender': [sender], 'network': ['mainnet-beta']}
    path = f"/api/v1/session-workspaces/{workspaces[0]['id']}/pause-entries"
    rpc.failure = True
    with closing(TradeJournal(library.path.parent)) as journal:
        journal.db.execute('INSERT INTO agent_orders VALUES (?,?,?,?,?,?,?,?,?)', ('keep-exit', ids[0], 'MintXYZ', 'sell', 'cash-exit', 'tokens-exit', 'review', '{}', 100))
        journal.db.commit()
    with pytest.raises(ValueError, match='current wallet'):
        command(library, path, {}, query={'sender': [str(Keypair().pubkey())], 'network': ['mainnet-beta']}, wallet_authorizer=Authorizer())
    result = command(library, path, {}, query=query, wallet_authorizer=Authorizer())
    assert result['paused_strategies'] == 1
    assert library.instance(ids[0])['entry_mode'] == 'disabled'
    assert library.instance(ids[0])['exit_mode'] == 'automatic'
    assert library.tracking_settings(ids[0])['enabled']
    assert library.instance(ids[1])['entry_mode'] == 'automatic'
    with closing(TradeJournal(library.path.parent)) as journal:
        assert journal.db.execute("SELECT status FROM agent_orders WHERE id='keep-exit'").fetchone()[0] == 'review'


def test_position_summary_keeps_history_and_distinguishes_other_wallet_positions(funded, monkeypatch):
    library, sender, _, workspaces, ids = funded
    removed = attached(library, workspaces[0], 'Removed position owner')
    library.remove_instance(removed)
    base = dict(sender=sender, genesis=GENESIS['mainnet-beta'], status='open', mint='MintXYZ')
    positions = [dict(base, id='managed', agent_id=ids[0], managed=True),
                 dict(base, id='historical', agent_id=removed, managed=False),
                 dict(base, id='other-session', agent_id=ids[1], managed=True),
                 dict(base, id='closed', agent_id=ids[0], status='closed'),
                 dict(base, id='foreign-network', agent_id=ids[0], genesis=GENESIS['devnet'])]
    monkeypatch.setattr('pumpwizard_runner.web_api.trade_state', lambda *args: dict(positions=positions, orders=[], pending=True))
    rows = payload(library, '/api/v1/session-workspaces', {'sender': [sender], 'network': ['mainnet-beta']})['rows']
    summary = next(r['position_summary'] for r in rows if r['id'] == workspaces[0]['id'])
    assert summary == dict(open_positions=2, managed_positions=1, manual_positions=1,
                           other_wallet_positions=1, entries_paused=False,
                           paused_entry_strategies=0, automatic_exit_strategies=1, pending=True)
