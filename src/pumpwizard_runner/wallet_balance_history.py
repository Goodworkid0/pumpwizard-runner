"""Resumable confirmed native SOL history, separate from trading journals."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time

from .transfers import GENESIS, RpcError, TransferError, public_key
from .wallet_portfolio import WalletPortfolioRpc, _account_keys, _integer

PAGE_SIZE = 100
MAX_TRANSACTIONS = 500  # Discovery per read, never a lifetime limit.
READ_SECONDS = 8
RECEIPTS_PER_READ = 12
CACHE_SECONDS = 300
_CACHE = {}
_LOCK = threading.RLock()
_WORK = threading.Lock()
_GENESIS_CHECKS = {}


def _time(value):
    return value if type(value) in (int, float) and 0 < value <= time.time() else None


def _transaction(rpc, sender, summary):
    at = _time(summary.get('blockTime'))
    tx = rpc.call('getTransaction', [summary['signature'], {
        'encoding': 'jsonParsed', 'commitment': 'confirmed', 'maxSupportedTransactionVersion': 0,
    }])
    try:
        if not isinstance(tx, dict):
            return at, None, None
        at = _time(tx.get('blockTime')) or at
        keys, _ = _account_keys(tx.get('transaction'))
        index = keys.index(sender)
        before = _integer(tx['meta']['preBalances'][index], 'historical pre balance')
        after = _integer(tx['meta']['postBalances'][index], 'historical post balance')
        return at, before, after  # Failed transactions still charge fees.
    except (TransferError, KeyError, TypeError, IndexError, ValueError):
        return at, None, None


def _state(directory, sender, network):
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / 'wallet-balance-history.sqlite', timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS history (sender TEXT, network TEXT, payload TEXT NOT NULL, PRIMARY KEY(sender, network))')
    saved = db.execute('SELECT payload FROM history WHERE sender=? AND network=?', (sender, network)).fetchone()
    state = json.loads(saved[0]) if saved else {'summaries': [], 'details': {}, 'retries': {}, 'exhausted': False,
                                               'cursor': None, 'head_at': 0, 'retry_at': 0}
    return db, state


def read(directory: Path, sender: str, network: str, *, rpc=None, refresh=False):
    public_key(sender)
    if network not in ('mainnet-beta', 'devnet'):
        raise TransferError('Choose mainnet or devnet')
    directory = Path(directory)
    key = (str(directory.resolve()), sender, network)
    with _WORK:  # Pace history requests, leaving capacity for balances and user actions.
        with _LOCK:
            cached = _CACHE.get(key)
            if not refresh and cached and time.time() - cached[0] < (2 if cached[1]['sync_pending'] else CACHE_SECONDS):
                return deepcopy(cached[1])
        if rpc is None:
            from .web_trading import rpc_for
            base = rpc_for(network, directory)
            rpc = WalletPortfolioRpc(network, base.url) if hasattr(base, 'url') else base
        paced = isinstance(rpc, WalletPortfolioRpc)
        if paced:
            rpc.timeout = 4
        provider = hashlib.sha256(getattr(rpc, 'url', type(rpc).__name__).encode()).hexdigest()
        genesis_key = (network, provider)
        if not paced or time.time() - _GENESIS_CHECKS.get(genesis_key, 0) > 600:
            if rpc.genesis() != GENESIS[network]:
                raise TransferError('RPC network identity does not match the selected network')
            _GENESIS_CHECKS[genesis_key] = time.time()
        db, state = _state(directory, sender, network)
        try:
            if state.get('provider') != provider:
                state.update(provider=provider, exhausted=False, cursor=None, head_at=0, retry_at=0)
                state.pop('head_cursor', None)
                state.pop('head_until', None)
            deadline = time.monotonic() + READ_SECONDS
            error = ''
            now = time.time()
            # Capture today before backfill; a later RPC failure cannot discard it.
            try:
                balance = _integer(rpc.call('getBalance', [sender, {'commitment': 'confirmed'}])['value'], 'wallet balance')
                state['current'] = {'at': time.time(), 'lamports': str(balance)}
            except TransferError as failure:
                error = str(failure)
                state['retry_at'] = time.time() + _delay(failure)
                if not state.get('current') and not state['details']:
                    raise
            seen = {row['signature'] for row in state['summaries']}
            discovered = 0
            if state['summaries'] and now - state['head_at'] >= CACHE_SECONDS and not state.get('head_until'):
                state['head_until'] = state['summaries'][0]['signature']
            while now >= state['retry_at'] and discovered < MAX_TRANSACTIONS and time.monotonic() < deadline:
                head = bool(state.get('head_until'))
                if state['exhausted'] and not head:
                    break
                config = {'commitment': 'confirmed', 'limit': min(PAGE_SIZE, MAX_TRANSACTIONS - discovered)}
                if head:
                    config['until'] = state['head_until']
                    if state.get('head_cursor'):
                        config['before'] = state['head_cursor']
                elif state['cursor']:
                    config['before'] = state['cursor']
                try:
                    page = rpc.call('getSignaturesForAddress', [sender, config])
                    if not isinstance(page, list) or len(page) > config['limit'] or any(
                        not isinstance(row, dict) or not isinstance(row.get('signature'), str)
                        or not 0 < len(row['signature']) <= 128 for row in page
                    ):
                        raise TransferError('Wallet RPC returned invalid balance history')
                except TransferError as failure:
                    error = str(failure)
                    state['retry_at'] = time.time() + _delay(failure)
                    break
                added = []
                for row in page:
                    if row['signature'] not in seen:
                        seen.add(row['signature'])
                        added.append({'signature': row['signature'], 'blockTime': _time(row.get('blockTime')),
                                      'slot': row.get('slot') if type(row.get('slot')) is int else 0})
                if head:
                    state['summaries'] = added + state['summaries']
                    if len(page) < config['limit']:
                        state.pop('head_until', None)
                        state.pop('head_cursor', None)
                        state['head_at'] = now
                    elif page:
                        state['head_cursor'] = page[-1]['signature']
                else:
                    state['summaries'].extend(added)
                    if page:
                        state['cursor'] = page[-1]['signature']
                    state['exhausted'] = len(page) < config['limit']
                    state['head_at'] = now
                discovered += len(page)
                if not added and page and page[-1]['signature'] == config.get('before'):
                    error = 'History provider repeated a page; confirmed readings have been retained.'
                    state['retry_at'] = now + 30
                    break
                if paced:
                    time.sleep(.3)
            state['summaries'].sort(key=lambda row: row['slot'], reverse=True)
            pending = [row for row in reversed(state['summaries']) if row['signature'] not in state['details']
                       and now >= state['retries'].get(row['signature'], 0)]
            # Oldest first: start at first funding, then progressively fill forward.
            for row in pending[:RECEIPTS_PER_READ]:
                if time.monotonic() >= deadline or now < state['retry_at']:
                    break
                try:
                    detail = _transaction(rpc, sender, row)
                except TransferError as failure:
                    error = str(failure)
                    state['retry_at'] = time.time() + _delay(failure)
                    break
                if None not in detail:
                    state['details'][row['signature']] = detail
                    state['retries'].pop(row['signature'], None)
                else:
                    state['retries'][row['signature']] = now + 300
                if paced:
                    time.sleep(.3)
            rows, previous, missing, gaps = [], None, 0, 0
            for row in reversed(state['summaries']):
                at, before, after = state['details'].get(row['signature'], (_time(row['blockTime']), None, None))
                if at is None or before is None or after is None:
                    missing += 1
                    previous = None
                    if at is not None:
                        rows.append({'at': at, 'lamports': None, 'usd': None, 'continuous': False})
                    continue
                if previous is not None and previous != before:
                    gaps += 1
                rows.extend([{'at': at, 'lamports': str(before), 'usd': None, 'continuous': previous == before},
                             {'at': at, 'lamports': str(after), 'usd': None, 'continuous': True}])
                previous = after
            current = state.get('current')
            if current:
                if previous is not None and str(previous) != current['lamports']:
                    gaps += 1
                rows.append(dict(current, usd=None, continuous=str(previous) == current['lamports']))
            rows.sort(key=lambda row: row['at'])
            pending = not state['exhausted'] or bool(missing) or bool(state.get('head_until'))
            result = {'sender': sender, 'network': network, 'rows': rows,
                      'checked_at': current['at'] if current else time.time(),
                      'history_complete': not pending and not gaps and not error,
                      'transactions': len(state['summaries']), 'loaded_transactions': len(state['details']),
                      'missing_transactions': missing, 'balance_gaps': gaps,
                      'limited': not state['exhausted'], 'sync_pending': pending, 'sync_error': error,
                      'retry_after': max(3, int(state['retry_at'] - time.time())) if pending else 300,
                      'coverage': 'Confirmed native SOL balances from first available activity. Missing receipts leave gaps; wallet key creation time and historical USD rates are not inferred.'}
            if missing and not any(now >= state['retries'].get(row['signature'], 0) for row in state['summaries'] if row['signature'] not in state['details']):
                result['retry_after'] = max(result['retry_after'], int(min(state['retries'].values()) - now))
            db.execute('INSERT OR REPLACE INTO history VALUES (?, ?, ?)', (sender, network, json.dumps(state)))
            db.commit()
            with _LOCK:
                if len(_CACHE) >= 100:
                    _CACHE.pop(next(iter(_CACHE)))
                _CACHE[key] = (time.time(), deepcopy(result))
            return result
        finally:
            db.close()


def _delay(failure):
    delay = failure.details.get('retry_after_seconds') if isinstance(failure, RpcError) else None
    return max(10, min(300, delay or 10))
