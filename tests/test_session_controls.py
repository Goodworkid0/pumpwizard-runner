from contextlib import closing
import copy
import time
from types import SimpleNamespace

import pytest
from solders.keypair import Keypair

from pumpwizard_runner.agent_execution import SCHEMA
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.transfers import GENESIS
from pumpwizard_runner.web_api import command, payload
from pumpwizard_runner.web_session_controls import performance


class Authorizer:
    def require(self, sender, network):
        return sender, network


def attached(library, workspace, label):
    package = {'schema': 'pumpwizard.strategy-handoff/v1', 'strategy': {'id': 'core-frequency', 'label': label}, 'configuration': copy.deepcopy(CONFIG)}
    strategy = parse_package(package)
    library.import_strategy(strategy, (workspace['wallet_sender'], workspace['wallet_network']))
    brief = (dict(goal='validate-strategy', intent='Review exact session controls and receipt-backed performance.', paper_budget_usd=500, max_session_loss_usd=100, review_after_seconds=86400, session_mode='trade', execution_style='interactive', automation_scope=None, profit_management='user-directed') if workspace['mode'] == 'trade' else None)
    ident = library.create_instance(strategy.digest, brief=brief)
    library.assign_instance_to_workspace(workspace['id'], ident)
    return ident


def test_pause_all_is_wallet_scoped_and_retains_other_sessions_and_positions(tmp_path):
    sender, other = str(Keypair().pubkey()), str(Keypair().pubkey())
    q = {'sender': [sender], 'network': ['mainnet-beta']}
    with closing(StrategyLibrary(tmp_path)) as library:
        a = library.create_session_workspace('monitoring', wallet_sender=sender, wallet_network='mainnet-beta')
        b = library.create_session_workspace('monitoring', wallet_sender=sender, wallet_network='mainnet-beta')
        ids = [attached(library, a, label) for label in ('One', 'Two')]
        untouched = attached(library, b, 'Other session')
        for ident in ids + [untouched]:
            library.set_tracking(ident, True)
            library.set_execution_modes(ident, entries='automatic', exits='automatic')
        position = library.record_position(ids[0], 'MintABC', 'ABC', quantity=2, total_cost_usd=5, opened_at=100)
        with closing(TradeJournal(tmp_path)) as journal:
            journal.db.executescript(SCHEMA)
            for ident in ids + [untouched]:
                journal.db.execute('INSERT INTO agent_orders VALUES (?,?,?,?,?,?,?,?,?)', (ident+'review', ident, 'MintABC', 'buy', ident+'hold', None, 'review', '{}', 100))
            journal.db.commit()
        path = f"/api/v1/session-workspaces/{a['id']}/pause"
        with pytest.raises(ValueError, match='current wallet'):
            command(library, path, {}, query={'sender': [other], 'network': ['mainnet-beta']}, wallet_authorizer=Authorizer())
        with pytest.raises(ValueError, match='current wallet'):
            command(library, path, {}, query={}, wallet_authorizer=Authorizer())
        result = command(library, path, {}, query=q, wallet_authorizer=Authorizer())
        assert result['paused_strategies'] == 2
        for ident in ids:
            assert library.instance(ident)['status'] == 'paused'
            assert library.instance(ident)['entry_mode'] == 'disabled'
            assert library.instance(ident)['exit_mode'] == 'manual'
            assert not library.tracking_settings(ident)['enabled']
        assert library.instance(untouched)['entry_mode'] == 'automatic'
        assert library.tracking_settings(untouched)['enabled']
        assert library.positions(ids[0])[0]['id'] == position
        with closing(TradeJournal(tmp_path)) as journal:
            assert journal.db.execute('SELECT status FROM agent_orders WHERE id=?', (ids[0]+'review',)).fetchone()[0] == 'cancelled'
            assert journal.db.execute('SELECT status FROM agent_orders WHERE id=?', (untouched+'review',)).fetchone()[0] == 'review'
        # Repeated pause is safe and does not create additional financial records.
        command(library, path, {}, query=q, wallet_authorizer=Authorizer())


def test_pause_locks_only_the_matching_service_and_allowances(tmp_path):
    from pumpwizard_runner.web_session_controls import pause
    sender = str(Keypair().pubkey())
    with closing(StrategyLibrary(tmp_path)) as library:
        a = library.create_session_workspace('monitoring', wallet_sender=sender, wallet_network='mainnet-beta')
        ident = attached(library, a, 'One')
        calls = []
        signer = SimpleNamespace(state=lambda: dict(sender=sender, network='mainnet-beta', session_id=ident), lock=lambda reason: calls.append(('service', reason)))
        controller = SimpleNamespace(status=lambda *args: {'allowances': [{'id': 'ours', 'status': 'active'}]}, lock=lambda *args, **kwargs: calls.append(('allowance', kwargs['allowance_id'])))
        pause(library, a, main_wallet_controller=controller, background_service=SimpleNamespace(signer=signer))
        assert [c[0] for c in calls] == ['service', 'allowance']
        calls.clear()
        signer.state = lambda: dict(sender=sender, network='mainnet-beta', session_id='different')
        pause(library, a, background_service=SimpleNamespace(signer=signer))
        assert not calls


def test_session_pnl_includes_closed_partial_released_positions_and_failed_fees(tmp_path, monkeypatch):
    sender = str(Keypair().pubkey())
    now = time.time()
    with closing(StrategyLibrary(tmp_path)) as library:
        workspace = library.create_session_workspace('trade', wallet_sender=sender, wallet_network='mainnet-beta', allocation_lamports='1000000000')
        ident = attached(library, workspace, 'Rules')
        library.append_observations(ident, [dict(mint='MintABC', symbol='ABC', observed_at=now, price_native=.01, price_usd=1, market_cap_usd=20000)])
        base = dict(sender=sender, genesis=GENESIS['mainnet-beta'], agent_id=ident, mint='MintABC', realized_pnl='1', realized_proceeds='30000000', realized_cost='20000000', remaining_cost='15000000', decimals=6, quantity='2000000', managed=False)
        positions = [{**base, 'id': 'partial', 'status': 'open'}, {**base, 'id': 'closed', 'status': 'closed'}, {**base, 'id': 'other', 'agent_id': 'unrelated', 'status': 'open'}]
        trades = dict(positions=positions, orders=[dict(id='failed-buy', agent_id=ident, position_id=None, status='failed', receipt={'fee_lamports': '5000'}), dict(id='failed-manual-sell', agent_id=None, position_id='partial', status='failed', receipt={'fee_lamports': '5000'})], pending=False)
        pnl = performance(library, workspace, [ident], trades, now=now)
        assert pnl['realized_lamports'] == '19990000'
        assert pnl['unrealized_lamports'] == '5000000'
        assert pnl['total_lamports'] == '24990000'
        assert pnl['open_positions'] == 1
        assert pnl['state'] == 'estimated'
        assert performance(library, workspace, [ident], trades, now=now+121)['total_lamports'] is None
        monkeypatch.setattr('pumpwizard_runner.web_api.trade_state', lambda *args: trades)
        q = {'sender': [sender], 'network': ['mainnet-beta']}
        result = payload(library, '/api/v1/session-workspaces', q)
        assert result['rows'][0]['performance']['total_lamports'] == '24990000'
        sales = payload(library, f"/api/v1/session-workspaces/{workspace['id']}/sell-all", q, wallet_authorizer=Authorizer())
        assert [p['id'] for p in sales['positions']] == ['partial']
        with pytest.raises(ValueError, match='current wallet'):
            payload(library, f"/api/v1/session-workspaces/{workspace['id']}/sell-all", {'scope': ['research']}, wallet_authorizer=Authorizer())
        assert performance(library, workspace, [ident], {'positions': [], 'orders': []})['total_lamports'] == '0'
        positions[0]['realized_pnl'] = None
        assert performance(library, workspace, [ident], trades, now=now)['total_lamports'] is None


def test_session_history_and_pnl_keep_removed_strategy_attribution(tmp_path, monkeypatch):
    sender, other = str(Keypair().pubkey()), str(Keypair().pubkey())
    with closing(StrategyLibrary(tmp_path)) as library:
        workspace = library.create_session_workspace('trade', wallet_sender=sender, wallet_network='mainnet-beta', allocation_lamports='1000000000')
        ident = attached(library, workspace, 'Past rules')
        library.remove_instance(ident)
        assert library.session_workspace_instance_ids(workspace['id']) == []
        trades = dict(positions=[dict(id='closed', agent_id=ident, sender=sender, genesis=GENESIS['mainnet-beta'], status='closed', realized_pnl='10000000', realized_proceeds='30000000', realized_cost='20000000')], orders=[], pending=False)
        monkeypatch.setattr('pumpwizard_runner.web_api.trade_state', lambda *args: trades)
        query = {'sender': [sender], 'network': ['mainnet-beta']}
        row = payload(library, '/api/v1/session-workspaces', query)['rows'][0]
        assert row['strategy_ids'] == []
        assert row['history_strategies'] == [dict(id=ident, label='Past rules', removed=True)]
        assert row['performance']['total_lamports'] == '10000000'
        assert payload(library, '/api/v1/session-workspaces', {'sender': [other], 'network': ['mainnet-beta']})['rows'] == []


def test_strategy_performance_compares_shared_session_and_preserves_archived_costs(tmp_path, monkeypatch):
    sender, other = str(Keypair().pubkey()), str(Keypair().pubkey())
    now = time.time()
    with closing(StrategyLibrary(tmp_path)) as library:
        workspace = library.create_session_workspace('trade', wallet_sender=sender,
            wallet_network='mainnet-beta', allocation_lamports='1000000000')
        a, b = [attached(library, workspace, label) for label in ('Momentum', 'Past rules')]
        library.append_observations(a, [dict(mint='MintABC', symbol='ABC', observed_at=now,
            price_native=.01, price_usd=1, market_cap_usd=20000)])
        library.remove_instance(b)
        base = dict(sender=sender, genesis=GENESIS['mainnet-beta'], mint='MintABC',
            realized_pnl='1', realized_proceeds='30000000', realized_cost='20000000',
            remaining_cost='15000000', decimals=6, quantity='2000000', managed=False, transaction_fees='10000')
        trades = dict(positions=[{**base, 'id': 'a', 'agent_id': a, 'status': 'open'},
            {**base, 'id': 'b', 'agent_id': b, 'status': 'closed'},
            {**base, 'id': 'unrelated', 'agent_id': 'outside', 'status': 'closed'}],
            orders=[dict(id='manual-exit-fee', agent_id=None, position_id='a',
                status='failed', receipt={'fee_lamports': '5000'})], pending=False)
        monkeypatch.setattr('pumpwizard_runner.web_api.trade_state', lambda *args: trades)
        query = {'sender': [sender], 'network': ['mainnet-beta']}
        row = payload(library, '/api/v1/session-workspaces', query)['rows'][0]
        results = {item['id']: item for item in row['strategy_performance']}
        assert set(results) == {a, b}
        assert results[a]['performance']['total_lamports'] == '14995000'
        assert results[a]['performance']['closed_positions'] == 0
        assert results[a]['performance']['fees_lamports'] == '10000'
        assert results[b]['removed'] is True
        assert results[b]['performance']['total_lamports'] == '10000000'
        assert results[b]['performance']['closed_positions'] == 1
        assert sum(int(item['performance']['total_lamports']) for item in results.values()) == int(row['performance']['total_lamports'])
        assert row['allocation_lamports'] == '1000000000'
        assert payload(library, '/api/v1/session-workspaces', {'sender': [other], 'network': ['mainnet-beta']})['rows'] == []
        trades['positions'][0]['realized_pnl'] = None
        results = payload(library, '/api/v1/session-workspaces', query)['rows'][0]['strategy_performance']
        assert next(item for item in results if item['id'] == a)['performance']['total_lamports'] is None
        assert next(item for item in results if item['id'] == b)['performance']['total_lamports'] == '10000000'
