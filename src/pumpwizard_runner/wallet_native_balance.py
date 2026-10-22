"""Small, independent native balance/FX read for the header. Never an execution quote."""
from copy import deepcopy
from datetime import datetime
import json
import math
import threading
import time
import urllib.request

from .market import MarketDataError
from .transfers import GENESIS, TransferError, _NoRedirect
from .wallet_portfolio import _integer

_LOCK = threading.RLock()
_QUOTES = {}
_BALANCES = {}


def _coinbase():
    request = urllib.request.Request('https://api.exchange.coinbase.com/products/SOL-USD/ticker',
                                     headers={'Accept': 'application/json', 'Cache-Control': 'no-cache',
                                              'User-Agent': 'PumpWizard/0.2'})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=3) as response:
        body = response.read(16385)
    if len(body) > 16384:
        raise ValueError('Oversized display quote')
    data = json.loads(body)
    return float(data['price']), datetime.fromisoformat(data['time'].replace('Z', '+00:00')).timestamp()


def display_quote(source):
    """Fresh public price with an independent fallback and explicitly aged retained estimate."""
    key = type(source)
    with _LOCK:
        saved = _QUOTES.get(key)
        now = time.time()
        if saved and now - saved['received_at'] < 30 and now - saved['at'] < 60:
            return dict(saved, stale=False)
        providers = [('Pump', lambda: _pump(source)), ('Coinbase', _coinbase)]
        for name, getter in providers:
            try:
                price, at = getter()
                received = time.time()
                if not math.isfinite(price) or price <= 0 or not math.isfinite(at) or not 0 <= received - at <= 60:
                    continue
                quote = {'price': price, 'at': at, 'received_at': received, 'source': name}
                _QUOTES[key] = quote
                return dict(quote, stale=False)
            except (MarketDataError, TransferError, OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
        if saved and 0 <= time.time() - saved['at'] <= 300:
            return dict(saved, stale=True)
        return None


def _pump(source):
    curve = getattr(source, 'curve_source', None)
    if curve is not None:
        curve.timeout_seconds = 3  # This is a new display-only adapter instance.
        price, at, _ = curve.sol_usd_quote()
        return price, at
    return float(source.sol_usd_price()), time.time()


def read(directory, sender, network, *, rpc=None, market_source=None, refresh=False):
    from .transfers import public_key
    public_key(sender)
    if network not in GENESIS:
        raise TransferError('Choose mainnet or devnet')
    key = (str(directory), sender, network)
    with _LOCK:
        saved = _BALANCES.get(key)
        if saved and not refresh and time.time() - saved['checked_at'] < 10:
            return deepcopy(saved)
    if rpc is None:
        from .web_trading import rpc_for
        rpc = rpc_for(network, directory)
        rpc.timeout = 4
    if rpc.genesis() != GENESIS[network]:
        raise TransferError('RPC network identity does not match the selected network')
    balance = _integer(rpc.call('getBalance', [sender, {'commitment': 'confirmed'}])['value'], 'native balance')
    checked_at = time.time()
    quote = display_quote(market_source)
    result = {'sender': sender, 'network': network, 'balance': str(balance), 'checked_at': checked_at,
              'sol_usd_price': quote['price'] if quote else None,
              'estimated_usd': balance / 1e9 * quote['price'] if quote else None,
              'price_at': quote['at'] if quote else None, 'price_source': quote['source'] if quote else None,
              'price_stale': quote['stale'] if quote else False}
    with _LOCK:
        if len(_BALANCES) >= 100:
            _BALANCES.pop(next(iter(_BALANCES)))
        _BALANCES[key] = deepcopy(result)
    return result
