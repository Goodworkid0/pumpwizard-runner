from types import SimpleNamespace
import time

import pytest
from solders.keypair import Keypair

from pumpwizard_runner import wallet_native_balance as native
from pumpwizard_runner.market import MarketDataError
from pumpwizard_runner.transfers import GENESIS, TransferError

SENDER = str(Keypair.from_seed(bytes([27]) * 32).pubkey())


@pytest.fixture(autouse=True)
def clear_quotes():
    native._QUOTES.clear()
    native._BALANCES.clear()


class Rpc:
    def genesis(self):
        return GENESIS['mainnet-beta']
    def call(self, method, params):
        assert method == 'getBalance'  # Token reads and historical receipts are independent.
        return {'value': 10042675}


def test_fallback_quote_keeps_current_balance_when_pump_is_unavailable(tmp_path, monkeypatch):
    def unavailable():
        raise MarketDataError('offline')
    monkeypatch.setattr(native, '_coinbase', lambda: (120, time.time()))
    result = native.read(tmp_path, SENDER, 'mainnet-beta', rpc=Rpc(), market_source=SimpleNamespace(sol_usd_price=unavailable))
    assert result['balance'] == '10042675'
    assert result['estimated_usd'] == pytest.approx(1.205121)
    assert result['price_source'] == 'Coinbase' and not result['price_stale']


@pytest.mark.parametrize('price,age', [(float('nan'), 0), (-1, 0), (100, 301), (100, -10)])
def test_invalid_or_old_quotes_never_become_current_usd(tmp_path, monkeypatch, price, age):
    monkeypatch.setattr(native, '_pump', lambda source: (price, time.time() - age))
    monkeypatch.setattr(native, '_coinbase', lambda: (price, time.time() - age))
    result = native.read(tmp_path, SENDER, 'mainnet-beta', rpc=Rpc())
    assert result['balance'] == '10042675' and result['estimated_usd'] is None


def test_recent_quote_is_aged_and_expires_instead_of_erasing_native_balance(tmp_path, monkeypatch):
    source = SimpleNamespace()
    native._QUOTES[type(source)] = {'price': 120, 'at': time.time() - 90, 'received_at': time.time() - 90, 'source': 'Pump'}
    monkeypatch.setattr(native, '_pump', lambda source: (_ for _ in ()).throw(MarketDataError('offline')))
    monkeypatch.setattr(native, '_coinbase', lambda: (_ for _ in ()).throw(OSError('offline')))
    result = native.read(tmp_path, SENDER, 'mainnet-beta', rpc=Rpc(), market_source=source)
    assert result['price_stale'] and result['estimated_usd'] == pytest.approx(1.205121)
    native._QUOTES[type(source)]['at'] = time.time() - 301
    expired = native.read(tmp_path, SENDER, 'mainnet-beta', rpc=Rpc(), market_source=source, refresh=True)
    assert expired['estimated_usd'] is None and expired['balance'] == '10042675'


def test_wrong_network_cannot_be_valued(tmp_path):
    with pytest.raises(TransferError, match='network identity'):
        native.read(tmp_path, SENDER, 'devnet', rpc=Rpc())


def test_public_native_read_does_not_authorize_private_portfolio(tmp_path, monkeypatch):
    from contextlib import closing
    from pumpwizard_runner.library import StrategyLibrary
    from pumpwizard_runner.web_api import payload
    monkeypatch.setattr(native, 'read', lambda directory, sender, network, **kwargs: {'sender': sender, 'balance': '1'})
    with closing(StrategyLibrary(tmp_path)) as library:
        query = {'sender': [SENDER], 'network': ['mainnet-beta']}
        assert payload(library, '/api/v1/wallet/balance', query)['balance'] == '1'
        with pytest.raises(TransferError, match='sign-in'):
            payload(library, '/api/v1/wallet/portfolio', query)
