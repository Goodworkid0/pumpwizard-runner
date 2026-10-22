from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from solders.keypair import Keypair
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.web_api import command, payload


class Authorizer:
    def require(self, sender, network):
        assert sender and network in ('mainnet-beta', 'devnet')


def package():
    return {'schema': 'pumpwizard.strategy-handoff/v1', 'strategy': {'id': 'core-frequency', 'label': 'Private rules'}, 'configuration': CONFIG}


def test_wallet_saved_strategies_export_import_and_network_isolation(tmp_path):
    a, b = str(Keypair().pubkey()), str(Keypair().pubkey())
    qa = {'sender': [a], 'network': ['mainnet-beta']}
    qb = {'sender': [b], 'network': ['mainnet-beta']}
    with closing(StrategyLibrary(tmp_path)) as library:
        result = command(library, '/api/v1/strategies/import', package(), wallet_authorizer=Authorizer(), query=qa)
        digest = result['strategy']['digest']
        assert [r['digest'] for r in payload(library, '/api/v1/strategies', qa)['rows']] == [digest]
        for query in (qb, {'sender': [a], 'network': ['devnet']}, {'scope': ['research']}):
            assert payload(library, '/api/v1/strategies', query)['rows'] == []
            with pytest.raises(ValueError, match='Import this strategy'):
                payload(library, f'/api/v1/strategies/{digest}/export', query)
        exported = payload(library, f'/api/v1/strategies/{digest}/export', qa)['package']
        assert exported == package()
        for _ in range(2):
            command(library, '/api/v1/strategies/import', exported, wallet_authorizer=Authorizer(), query=qb)
        assert len(payload(library, '/api/v1/strategies', qb)['rows']) == 1
        assert len(library.rows()) == 1  # immutable storage can share exact content, wallet libraries cannot.
    with closing(StrategyLibrary(tmp_path)) as library:
        assert len(payload(library, '/api/v1/strategies', qa)['rows']) == 1
        assert len(payload(library, '/api/v1/strategies', qb)['rows']) == 1


def test_duplicate_assignment_is_atomic_and_removal_allows_readding(tmp_path):
    with closing(StrategyLibrary(tmp_path)) as library:
        parsed = parse_package(package())
        library.import_strategy(parsed)
        workspace = library.create_session_workspace('monitoring')
        ids = [library.create_instance(parsed.digest) for _ in range(2)]
    def assign(ident):
        with closing(StrategyLibrary(tmp_path)) as library:
            try:
                library.assign_instance_to_workspace(workspace['id'], ident)
                return ident
            except ValueError as exc:
                assert 'already in this session' in str(exc)
                return None
    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(assign, ids))
    assert sum(ident is not None for ident in result) == 1
    with closing(StrategyLibrary(tmp_path)) as library:
        winner = next(ident for ident in result if ident)
        library.remove_instance(winner)
        replacement = library.create_instance(parsed.digest)
        library.assign_instance_to_workspace(workspace['id'], replacement)
        other = library.create_session_workspace('monitoring')
        library.assign_instance_to_workspace(other['id'], library.create_instance(parsed.digest))
        assert library.session_workspace_instance_ids(workspace['id']) == [replacement]


def test_strategy_must_be_imported_into_target_wallet_before_assignment(tmp_path):
    a, b = str(Keypair().pubkey()), str(Keypair().pubkey())
    with closing(StrategyLibrary(tmp_path)) as library:
        parsed = parse_package(package())
        library.import_strategy(parsed, (a, 'mainnet-beta'))
        workspace = library.create_session_workspace('monitoring', wallet_sender=b, wallet_network='mainnet-beta')
        body = {'digest': parsed.digest, 'workspace_id': workspace['id']}
        qb = {'sender': [b], 'network': ['mainnet-beta']}
        with pytest.raises(ValueError, match='Import this strategy'):
            command(library, '/api/v1/sessions', body, query=qb)
        library.import_strategy(parsed, (b, 'mainnet-beta'))
        command(library, '/api/v1/sessions', body, query=qb)
        with pytest.raises(ValueError, match='already in this session'):
            command(library, '/api/v1/sessions', body, query=qb)
        assert len(library.instances()) == 1


def test_created_strategy_is_saved_only_to_its_wallet(tmp_path):
    a, b = str(Keypair().pubkey()), str(Keypair().pubkey())
    with closing(StrategyLibrary(tmp_path)) as library:
        query = {'sender': [a], 'network': ['mainnet-beta']}
        draft = command(library, '/api/v1/strategies/draft', {
            'label': 'Wallet draft', 'identifier': 'wallet-draft', 'entry_style': 'momentum',
            'entry_cap_min': 10000, 'entry_cap_max': 25000, 'order_usd': 10, 'budget_usd': 500,
        }, wallet_authorizer=Authorizer(), query=query)
        assert payload(library, '/api/v1/strategies', query)['rows'][0]['digest'] == draft['strategy']['digest']
        assert payload(library, '/api/v1/strategies', {'sender': [b], 'network': ['mainnet-beta']})['rows'] == []


def test_edit_saves_scoped_revision_without_retargeting_sessions_or_copying_results(tmp_path):
    import copy
    a, b = str(Keypair().pubkey()), str(Keypair().pubkey())
    qa = {'sender': [a], 'network': ['mainnet-beta']}
    qb = {'sender': [b], 'network': ['mainnet-beta']}
    with closing(StrategyLibrary(tmp_path)) as library:
        source = copy.deepcopy(package())
        source['report'] = {'pnl_usd': 1000}
        old = parse_package(source)
        library.import_strategy(old, (a, 'mainnet-beta'))
        session = library.create_instance(old.digest)
        changes = copy.deepcopy(CONFIG)
        changes.update(order_usd=7, max_positions=3, stop_loss_fraction=-.12)
        changes['targets'] = [{'market_cap_usd': 35000, 'initial_fraction': .6}, {'market_cap_usd': 75000, 'initial_fraction': .4}]
        body = {'label': 'Edited rules', 'configuration': changes}
        path = f'/api/v1/strategies/{old.digest}/revisions'
        with pytest.raises(ValueError, match='Import this strategy'):
            command(library, path, body, wallet_authorizer=Authorizer(), query=qb)
        result = command(library, path, body, wallet_authorizer=Authorizer(), query=qa)
        revised = result['strategy']['digest']
        assert revised != old.digest
        assert library.instance(session)['strategy_digest'] == old.digest
        assert library.strategy(old.digest)['package'] == source
        saved = library.strategy(revised)['package']
        assert saved['configuration'] == changes
        assert saved['strategy']['label'] == 'Edited rules'
        assert 'report' not in saved
        assert len(payload(library, '/api/v1/strategies', qa)['rows']) == 2
        assert payload(library, '/api/v1/strategies', qb)['rows'] == []
        # Retrying a save is idempotent; no duplicate package or session created.
        assert command(library, path, body, wallet_authorizer=Authorizer(), query=qa)['strategy']['digest'] == revised
        assert len(library.instances()) == 1
        with pytest.raises(ValueError, match='Check the rules'):
            command(library, path, {**body, 'configuration': {**changes, 'max_positions': 0}}, wallet_authorizer=Authorizer(), query=qa)
        with pytest.raises(ValueError, match='format'):
            command(library, path, {**body, 'configuration': {**changes, 'mode': 'live'}}, wallet_authorizer=Authorizer(), query=qa)
        assert len(library.rows()) == 2


def test_percentage_sizing_uses_allocated_sol_and_server_price(tmp_path, monkeypatch):
    import copy
    from pumpwizard_runner import web_api
    from pumpwizard_runner.manual_trades import TradeJournal
    from pumpwizard_runner.session_allocations import reserve_workspace
    from pumpwizard_runner.transfers import GENESIS
    sender, other = str(Keypair().pubkey()), str(Keypair().pubkey())
    query = {'sender': [sender], 'network': ['mainnet-beta']}
    class Price:
        def sol_usd_price(self):
            return 100
    monkeypatch.setattr(web_api, 'CompositePumpMarketSource', Price)
    with closing(StrategyLibrary(tmp_path)) as library:
        source = parse_package(package())
        library.import_strategy(source, (sender, 'mainnet-beta'))
        workspace = library.create_session_workspace('trade', allocation_lamports='22200000', wallet_sender=sender, wallet_network='mainnet-beta')
        with closing(TradeJournal(tmp_path)) as journal:
            reserve_workspace(journal, workspace['id'], GENESIS['mainnet-beta'], sender, 22200000, 100000000)
        q = {**query, 'workspace_id': [workspace['id']], 'trade_percent': ['25']}
        preview = payload(library, '/api/v1/strategy-sizing', q)['sizing']
        assert preview['budget_sol'] == '0.0222'
        assert preview['trade_budget_sol'] == '0.00555'
        assert preview['budget_usd'] == 2.22
        assert preview['order_usd'] == .555
        with pytest.raises(ValueError, match='owned by this wallet'):
            payload(library, '/api/v1/strategy-sizing', {**q, 'sender': [other]})
        body = {'label': 'Allocated strategy', 'configuration': copy.deepcopy(CONFIG), 'sizing': {'workspace_id': workspace['id'], 'allocation_lamports': '22200000', 'trade_percent': '25'}}
        path = f'/api/v1/strategies/{source.digest}/revisions'
        changed = command(library, path, body, wallet_authorizer=Authorizer(), query=query)
        config = changed['strategy']['package']['configuration']
        assert config['budget_usd'] == 2.22  # Never accept the template's $500.
        assert config['order_usd'] == .555
        assert library.strategy(source.digest)['package']['configuration']['budget_usd'] == 500
        row = next(row for row in payload(library, '/api/v1/strategies', query)['rows'] if row['digest'] == changed['strategy']['digest'])
        assert row['allocation_sizing'] == [preview]
        assert 'sizing' not in payload(library, f"/api/v1/strategies/{row['digest']}/export", query)['package']
        for value in ('0', '-1', '101', 'NaN', 'Infinity'):
            with pytest.raises(ValueError, match='percentage'):
                command(library, path, {**body, 'sizing': {**body['sizing'], 'trade_percent': value}}, wallet_authorizer=Authorizer(), query=query)
        with pytest.raises(ValueError, match='allocation changed'):
            command(library, path, {**body, 'sizing': {**body['sizing'], 'allocation_lamports': '1'}}, wallet_authorizer=Authorizer(), query=query)
        class MissingPrice:
            def sol_usd_price(self):
                return float('nan')
        monkeypatch.setattr(web_api, 'CompositePumpMarketSource', MissingPrice)
        with pytest.raises(ValueError, match='SOL/USD is unavailable'):
            command(library, path, body, wallet_authorizer=Authorizer(), query=query)
        assert len(library.rows()) == 2
    with closing(StrategyLibrary(tmp_path)) as library:
        rows = payload(library, '/api/v1/strategies', query)['rows']
        assert sum(bool(row['allocation_sizing']) for row in rows) == 1


def test_attached_revision_updates_only_inactive_owned_instance_and_blocks_source_duplicate(tmp_path):
    import copy
    sender, other = str(Keypair().pubkey()), str(Keypair().pubkey())
    query = {'sender': [sender], 'network': ['mainnet-beta']}
    with closing(StrategyLibrary(tmp_path)) as library:
        source = parse_package(package())
        library.import_strategy(source, (sender, 'mainnet-beta'))
        workspace = library.create_session_workspace('monitoring', wallet_sender=sender, wallet_network='mainnet-beta')
        created = command(library, '/api/v1/sessions', {'digest': source.digest, 'workspace_id': workspace['id']}, query=query)
        ident = created['instance']['id']
        config = copy.deepcopy(CONFIG)
        config.update(budget_usd=30, order_usd=2, max_positions=3)
        body = {'label': 'Session revision', 'configuration': config, 'instance_id': ident}
        path = f'/api/v1/strategies/{source.digest}/revisions'
        library.import_strategy(source, (other, 'mainnet-beta'))
        with pytest.raises(ValueError, match='wallet and source revision'):
            command(library, path, body, query={'sender': [other], 'network': ['mainnet-beta']}, wallet_authorizer=Authorizer())
        result = command(library, path, body, query=query, wallet_authorizer=Authorizer())
        digest = result['strategy']['digest']
        assert digest != source.digest
        assert result['instance_id'] == ident
        assert library.instance(ident)['strategy_digest'] == digest
        assert library.instance(ident)['entry_mode'] == 'disabled'
        assert not library.tracking_settings(ident)['enabled']
        brief = library.session_brief(ident)
        assert brief['strategy_digest_at_creation'] == source.digest
        assert brief['paper_budget_usd'] == 30
        assert brief['paper_order_usd'] == 2
        assert brief['max_positions'] == 3
        assert len(library.instances()) == 1
        with pytest.raises(ValueError, match='source revision'):
            command(library, path, body, query=query, wallet_authorizer=Authorizer())
        duplicate = library.create_instance(source.digest)
        with pytest.raises(ValueError, match='already in this session'):
            library.assign_instance_to_workspace(workspace['id'], duplicate)
        assert library.session_workspace_instance_ids(workspace['id']) == [ident]
        library.record_position(ident, 'MintABC', 'ABC', quantity=1, total_cost_usd=2, opened_at=100)
        with pytest.raises(ValueError, match='activity|cannot be edited'):
            command(library, f'/api/v1/strategies/{digest}/revisions', {**body, 'label': 'Do not change'}, query=query, wallet_authorizer=Authorizer())
        assert library.instance(ident)['strategy_digest'] == digest
