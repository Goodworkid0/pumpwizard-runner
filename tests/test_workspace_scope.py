from contextlib import closing
import sqlite3

import pytest
from solders.keypair import Keypair

from pumpwizard_runner.agent_hub import AgentHubError
from pumpwizard_runner.assistant_chat import ChatStore
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.manual_trades import TradeJournal
from pumpwizard_runner.transfers import GENESIS
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.transfers import TransferError
from pumpwizard_runner.web_api import assistant_command, assistant_payload, command, payload
from pumpwizard_runner.web_assistant import BrowserAssistant


def test_wallet_sessions_strategies_activity_and_networks_are_separate(tmp_path):
    addresses = [str(Keypair().pubkey()), str(Keypair().pubkey())]
    with closing(StrategyLibrary(tmp_path)) as library:
        strategy = parse_package({'schema': 'pumpwizard.strategy-handoff/v1',
                                  'strategy': {'id': 'core-frequency', 'label': 'Core'}, 'configuration': CONFIG})
        library.import_strategy(strategy)
        groups = [(addresses[0], 'mainnet-beta'), (addresses[1], 'mainnet-beta'),
                  (addresses[0], 'devnet'), (None, None)]
        expected = []
        for address, network in groups:
            ws = library.create_session_workspace('monitoring', wallet_sender=address, wallet_network=network)
            instance = library.create_instance(strategy.digest)
            library.assign_instance_to_workspace(ws['id'], instance)
            library.record_decisions(instance, [dict(at=1, kind='entry_recommended', mint='coin', reason='signal', payload={})])
            expected.append((ws['id'], instance))
        for (address, network), (workspace, instance) in zip(groups, expected):
            query = {'sender': [address], 'network': [network]} if address else {'scope': ['research']}
            assert [r['id'] for r in payload(library, '/api/v1/session-workspaces', query)['rows']] == [workspace]
            assert [r['instance']['id'] for r in payload(library, '/api/v1/sessions', query)['rows']] == [instance]
            assert payload(library, f'/api/v1/sessions/{instance}', query)['instance']['id'] == instance
            other_id = next(row[1] for row in expected if row[1] != instance)
            with pytest.raises(ValueError, match='selected wallet workspace'):
                payload(library, f'/api/v1/sessions/{other_id}', query)
            events = payload(library, '/api/v1/session-activity', query)
            assert [r['session_id'] for r in events['rows']] == [instance]
            assert payload(library, '/api/v1/session-activity', {**query, 'after': [str(events['cursor'])]})['rows'] == []
        # Ownership filtering happens before the latest-page limit.
        other = expected[1][1]
        library.record_decisions(other, [dict(at=i+2, kind='entry_blocked', mint=str(i), reason='skip', payload={}) for i in range(120)])
        assert len(payload(library, '/api/v1/session-activity', {'sender': [addresses[0]], 'network': ['mainnet-beta']})['rows']) == 1
        with pytest.raises(TransferError):
            payload(library, '/api/v1/session-workspaces', {'sender': [addresses[0]]})


def test_monitor_owner_requires_sign_in_but_no_funding(tmp_path):
    sender = str(Keypair().pubkey())
    owner = {'sender': sender, 'network': 'mainnet-beta'}
    class Authorizer:
        def require(self, address, network):
            assert (address, network) == (sender, 'mainnet-beta')
    with closing(StrategyLibrary(tmp_path)) as library:
        body = {'mode': 'monitoring', 'owner': owner, 'monitor_activity': 'paper'}
        with pytest.raises(TransferError, match='sign-in'):
            command(library, '/api/v1/session-workspaces', body)
        created = command(library, '/api/v1/session-workspaces', body, wallet_authorizer=Authorizer())['session']
        assert created['wallet_sender'] == sender
        assert created['allocation_lamports'] == '0'
        assert created['strategy_count'] == 0


def test_chat_history_pending_requests_and_networks_are_wallet_scoped(tmp_path):
    assistant = BrowserAssistant(tmp_path)
    assistant.configure({'provider': 'openai', 'model': 'test-model', 'api_key': 'test-key', 'endpoint': ''})
    a, b = str(Keypair().pubkey()), str(Keypair().pubkey())
    queries = [{'sender': [a], 'network': ['mainnet-beta']}, {'sender': [b], 'network': ['mainnet-beta']},
               {'sender': [a], 'network': ['devnet']}, {'scope': ['research']}]
    requests = []
    for index, query in enumerate(queries):
        requests.append(assistant_command(assistant, '/api/v1/assistant/prepare',
                        {'conversation': None, 'message': f'Wallet conversation {index}', 'max_output': 128}, query))
    for index, query in enumerate(queries):
        own = requests[index]
        assert [c['id'] for c in assistant_payload(assistant, '/api/v1/assistant', query)['conversations']] == [own['conversation']]
        assert assistant_payload(assistant, '/api/v1/assistant/conversations/'+own['conversation'], query)['messages'][0]['content'] == f'Wallet conversation {index}'
        other = requests[(index+1) % len(requests)]
        with pytest.raises(AgentHubError, match='Unknown local conversation'):
            assistant_payload(assistant, '/api/v1/assistant/conversations/'+other['conversation'], query)
        for action in ('approve', 'decline'):
            with pytest.raises(AgentHubError, match='Unknown local conversation'):
                assistant_command(assistant, '/api/v1/assistant/'+action, {'request_id': other['request_id']}, query)
        with pytest.raises(AgentHubError, match='Unknown local conversation'):
            assistant_command(assistant, '/api/v1/assistant/conversations/'+other['conversation']+'/delete', {}, query)
        assert other['request_id'] in assistant.pending
    for own, query in zip(requests, queries):
        assert assistant_command(assistant, '/api/v1/assistant/decline', {'request_id': own['request_id']}, query)['declined']


def test_legacy_chat_migration_preserves_unowned_history(tmp_path):
    with sqlite3.connect(tmp_path / 'assistant-history.sqlite') as db:
        db.execute('CREATE TABLE conversations(id TEXT PRIMARY KEY,profile_id TEXT,title TEXT,updated REAL)')
        db.execute("INSERT INTO conversations VALUES ('old','provider','Old chat',1)")
    with closing(ChatStore(tmp_path)) as store:
        assert store.conversations('provider')[0]['id'] == 'old'
        assert store.conversations('provider', 'wallet:mainnet-beta') == []
        created = store.create('provider', 'wallet:mainnet-beta')
        assert store.conversations('provider', 'wallet:mainnet-beta')[0]['id'] == created


def test_legacy_strategy_ownership_uses_retained_wallet_policy(tmp_path):
    from pumpwizard_runner.workspace_scope import scoped_instance_ids
    address = str(Keypair().pubkey())
    with closing(StrategyLibrary(tmp_path)) as library:
        strategy = parse_package({'schema': 'pumpwizard.strategy-handoff/v1',
                                  'strategy': {'id': 'core-frequency', 'label': 'Core'}, 'configuration': CONFIG})
        library.import_strategy(strategy)
        owned = library.create_instance(strategy.digest)
        unowned = library.create_instance(strategy.digest)
        with closing(TradeJournal(tmp_path)) as journal:
            with journal.db:
                journal.db.execute('INSERT INTO strategy_risk_policies VALUES (?,?,?,?,?)',
                                   (owned, GENESIS['mainnet-beta'], address, '{}', 1))
        assert scoped_instance_ids(library, (address, 'mainnet-beta')) == {owned}
        assert scoped_instance_ids(library, (address, 'devnet')) == set()
        assert scoped_instance_ids(library, ('', '')) == {unowned}
