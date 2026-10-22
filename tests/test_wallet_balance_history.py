from contextlib import closing
from types import SimpleNamespace

import pytest
from solders.keypair import Keypair

from pumpwizard_runner import wallet_balance_history as history
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.transfers import GENESIS, TransferError
from pumpwizard_runner.web_api import payload


SENDER = str(Keypair.from_seed(bytes([21]) * 32).pubkey())
OTHER = str(Keypair.from_seed(bytes([22]) * 32).pubkey())


class Rpc:
    def __init__(self, sender=SENDER, network='mainnet-beta'):
        self.sender, self.network = sender, network
        self.calls = []
        self.current = 199_995_000
        self.rows = [
            {'signature': 'fee', 'blockTime': 1700000200, 'slot': 3},
            {'signature': 'withdraw', 'blockTime': 1700000100, 'slot': 2},
            {'signature': 'fund', 'blockTime': 1700000000, 'slot': 1},
        ]
        self.balances = {'fund': (0, 1000000000), 'withdraw': (1000000000, 200000000), 'fee': (200000000, 199995000)}
        self.missing = set()

    def genesis(self):
        return GENESIS[self.network]

    def call(self, method, params):
        self.calls.append((method, params))
        if method == 'getSignaturesForAddress':
            assert params[0] == self.sender
            cursor = params[1].get('before')
            start = next((i + 1 for i, row in enumerate(self.rows) if row['signature'] == cursor), 0)
            stop = next((i for i, row in enumerate(self.rows) if row['signature'] == params[1].get('until')), len(self.rows))
            return self.rows[start:stop][:params[1]['limit']]
        if method == 'getTransaction':
            signature = params[0]
            if signature in self.missing:
                return None
            before, after = self.balances[signature]
            return {'transaction': {'message': {'accountKeys': [{'pubkey': OTHER}, {'pubkey': self.sender}]}},
                    'meta': {'preBalances': [1, before], 'postBalances': [1, after],
                             'err': 'failed and charged fee' if signature == 'fee' else None}}
        if method == 'getBalance':
            return {'value': self.current}
        raise AssertionError('History must only read confirmed public balances')


def test_history_pages_to_first_available_activity_and_keeps_failed_fees(tmp_path, monkeypatch):
    monkeypatch.setattr(history, 'PAGE_SIZE', 2)
    rpc = Rpc()
    result = history.read(tmp_path, SENDER, 'mainnet-beta', rpc=rpc)
    assert result['history_complete'] and result['transactions'] == 3
    assert result['missing_transactions'] == 0 and not result['limited']
    assert [(r['at'], r['lamports']) for r in result['rows'][:-1]] == [
        (1700000000, '0'), (1700000000, '1000000000'),
        (1700000100, '1000000000'), (1700000100, '200000000'),
        (1700000200, '200000000'), (1700000200, '199995000')]
    assert result['rows'][-1]['lamports'] == '199995000'
    assert result['rows'][-1]['continuous']
    assert all(row['usd'] is None for row in result['rows'])
    pages = [params[1] for method, params in rpc.calls if method == 'getSignaturesForAddress']
    assert pages == [{'commitment': 'confirmed', 'limit': 2},
                     {'commitment': 'confirmed', 'limit': 2, 'before': 'withdraw'}]
    assert [p.name for p in tmp_path.iterdir()] == ['wallet-balance-history.sqlite']


def test_missing_records_and_unexplained_balance_changes_break_history(tmp_path):
    rpc = Rpc()
    rpc.missing.add('withdraw')
    rpc.current = 100
    result = history.read(tmp_path, SENDER, 'mainnet-beta', rpc=rpc)
    assert not result['history_complete'] and result['missing_transactions'] == 1
    assert result['rows'][2] == {'at': 1700000100, 'lamports': None, 'usd': None, 'continuous': False}
    assert result['rows'][3]['lamports'] == '200000000' and not result['rows'][3]['continuous']
    assert result['rows'][-1]['lamports'] == '100' and not result['rows'][-1]['continuous']
    current_changed = Rpc()
    current_changed.current = 100
    changed = history.read(tmp_path / 'current-changed', SENDER, 'mainnet-beta', rpc=current_changed)
    assert not changed['history_complete'] and changed['missing_transactions'] == 0
    assert changed['balance_gaps'] == 1 and not changed['rows'][-1]['continuous']


def test_history_limits_busy_wallets_and_repeated_rpc_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(history, 'PAGE_SIZE', 2)
    monkeypatch.setattr(history, 'MAX_TRANSACTIONS', 2)
    result = history.read(tmp_path, SENDER, 'mainnet-beta', rpc=Rpc())
    assert result['limited'] and not result['history_complete'] and result['transactions'] == 2
    monkeypatch.setattr(history, 'MAX_TRANSACTIONS', 500)
    class Repeated(Rpc):
        def call(self, method, params):
            if method == 'getSignaturesForAddress':
                self.calls.append((method, params))
                return self.rows[:2]
            return super().call(method, params)
    result = history.read(tmp_path, OTHER, 'mainnet-beta', rpc=Repeated(OTHER))
    assert result['limited'] and result['transactions'] == 2


@pytest.mark.parametrize('bad', [-1, True, '1.5', 18446744073709551616])
def test_invalid_transaction_balances_leave_gaps(tmp_path, bad):
    rpc = Rpc()
    rpc.balances['fund'] = (0, bad)
    result = history.read(tmp_path, SENDER, 'mainnet-beta', rpc=rpc)
    assert result['missing_transactions'] == 1
    assert result['rows'][0]['lamports'] is None


def test_wallet_and_network_scope_cache_and_retry_are_separate(tmp_path):
    rpc = Rpc()
    result = history.read(tmp_path, SENDER, 'mainnet-beta', rpc=rpc)
    calls = len(rpc.calls)
    result['rows'][0]['lamports'] = 'changed only in caller'
    cached = history.read(tmp_path, SENDER, 'mainnet-beta', rpc=rpc)
    assert len(rpc.calls) == calls and cached['rows'][0]['lamports'] == '0'
    history.read(tmp_path, SENDER, 'mainnet-beta', rpc=rpc, refresh=True)
    assert len(rpc.calls) > calls
    other = Rpc(OTHER, 'devnet')
    assert history.read(tmp_path, OTHER, 'devnet', rpc=other)['sender'] == OTHER
    with pytest.raises(TransferError):
        history.read(tmp_path, SENDER, 'testnet', rpc=rpc)
    with pytest.raises(TransferError, match='network identity'):
        history.read(tmp_path, OTHER, 'mainnet-beta', rpc=other)


def test_unavailable_chain_and_budget_never_fabricate_older_points(tmp_path, monkeypatch):
    class Offline(Rpc):
        def call(self, method, params):
            raise TransferError('RPC unavailable')
    with pytest.raises(TransferError, match='unavailable'):
        history.read(tmp_path, SENDER, 'mainnet-beta', rpc=Offline())
    monkeypatch.setattr(history, 'READ_SECONDS', 0)
    result = history.read(tmp_path, SENDER, 'mainnet-beta', rpc=Rpc())
    assert result['transactions'] == 0 and result['limited']
    assert len(result['rows']) == 1 and result['rows'][0]['lamports'] == '199995000'


def test_public_balance_history_api_never_grants_wallet_authority(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(history, 'read', lambda directory, sender, network, **kwargs: calls.append((sender, network)) or {'sender': sender, 'network': network, 'rows': []})
    query = {'sender': [SENDER], 'network': ['mainnet-beta']}
    with closing(StrategyLibrary(tmp_path)) as library:
        assert payload(library, '/api/v1/wallet/balance-history', query)['rows'] == []
        assert calls == [(SENDER, 'mainnet-beta')]
        required = []
        authorizer = SimpleNamespace(require=lambda sender, network: required.append((sender, network)))
        result = payload(library, '/api/v1/wallet/balance-history', query, wallet_authorizer=authorizer)
        assert result['rows'] == [] and required == []
        assert calls == [(SENDER, 'mainnet-beta')] * 2


def test_backfill_resumes_beyond_batch_limit_and_across_process_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(history, 'PAGE_SIZE', 2)
    monkeypatch.setattr(history, 'MAX_TRANSACTIONS', 2)
    monkeypatch.setattr(history, 'RECEIPTS_PER_READ', 1)
    rpc = Rpc()
    first = history.read(tmp_path, SENDER, 'mainnet-beta', rpc=rpc)
    assert first['sync_pending'] and first['loaded_transactions'] == 1
    history._CACHE.clear()
    second = history.read(tmp_path, SENDER, 'mainnet-beta', rpc=rpc, refresh=True)
    assert second['transactions'] == 3 and second['loaded_transactions'] == 2
    third = history.read(tmp_path, SENDER, 'mainnet-beta', rpc=rpc, refresh=True)
    assert third['history_complete'] and not third['sync_pending']
    receipts = [p[0] for method, p in rpc.calls if method == 'getTransaction']
    assert len(receipts) == len(set(receipts)) == 3
    assert third['rows'][0]['lamports'] == '0'


def test_usd_history_api_prices_historical_balances_and_validates_currency(tmp_path, monkeypatch):
    from pumpwizard_runner import wallet_native_balance, wallet_usd_history
    start = 1700002800
    raw = {'sender': SENDER, 'network': 'mainnet-beta', 'rows': [
        {'at': start, 'lamports': '1000000000', 'usd': None, 'continuous': False},
        {'at': start + 3600, 'lamports': '2000000000', 'usd': None, 'continuous': True},
    ], 'history_complete': True, 'sync_pending': False}
    scopes = []
    monkeypatch.setattr(history, 'read', lambda directory, sender, network, **kwargs: raw)
    def quote(directory, sender, network, **kwargs):
        scopes.append((sender, network))
        return {'sol_usd_price': 100.0}
    monkeypatch.setattr(wallet_native_balance, 'read', quote)
    monkeypatch.setattr(wallet_usd_history, '_fetch', lambda start, end, interval: {at: 80.0 for at in range(start, end, interval)})
    query = {'sender': [SENDER], 'network': ['mainnet-beta']}
    with closing(StrategyLibrary(tmp_path)) as library:
        assert payload(library, '/api/v1/wallet/balance-history', query)['rows'] == raw['rows']
        assert scopes == []
        result = payload(library, '/api/v1/wallet/balance-history', dict(query, currency=['USD']))
        assert [r['usd'] for r in result['rows']] == [80.0, 200.0]
        assert result['usd_history']['unpriced_readings'] == 0
        assert scopes == [(SENDER, 'mainnet-beta')]
        with pytest.raises(TransferError, match='SOL or USD'):
            payload(library, '/api/v1/wallet/balance-history', dict(query, currency=['EUR']))


def test_new_head_paginates_without_losing_oldest_cursor(tmp_path, monkeypatch):
    rpc = Rpc()
    history.read(tmp_path, SENDER, 'mainnet-beta', rpc=rpc)
    monkeypatch.setattr(history, 'PAGE_SIZE', 2)
    monkeypatch.setattr(history, 'CACHE_SECONDS', 0)
    for i in range(5):
        signature = f'new-{i}'
        rpc.rows.insert(0, {'signature': signature, 'blockTime': 1700000300 + i, 'slot': 4 + i})
        rpc.balances[signature] = (rpc.current, rpc.current + 1)
        rpc.current += 1
    result = history.read(tmp_path, SENDER, 'mainnet-beta', rpc=rpc, refresh=True)
    assert result['transactions'] == 8 and result['history_complete']
    assert result['rows'][0]['lamports'] == '0'
    assert result['rows'][-1]['lamports'] == str(rpc.current)


def test_rate_limit_preserves_progress_and_retries_only_pending_receipts(tmp_path, monkeypatch):
    from pumpwizard_runner.transfers import RpcError
    class Limited(Rpc):
        limited = True
        def call(self, method, params):
            if method == 'getTransaction' and params[0] == 'withdraw' and self.limited:
                raise RpcError(method, http_status=429)
            return super().call(method, params)
    rpc = Limited()
    monkeypatch.setattr(history, '_delay', lambda failure: 0)
    result = history.read(tmp_path, SENDER, 'mainnet-beta', rpc=rpc)
    assert result['sync_pending'] and result['loaded_transactions'] == 1
    assert 'rate limit' in result['sync_error']
    rpc.limited = False
    result = history.read(tmp_path, SENDER, 'mainnet-beta', rpc=rpc, refresh=True)
    assert result['history_complete']
    assert [p[0] for method, p in rpc.calls if method == 'getTransaction'].count('fund') == 1
