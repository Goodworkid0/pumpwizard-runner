"""Display-only native USD history using time-bucketed public SOL/USD prices."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

from .transfers import TransferError, _NoRedirect

_LOCK = threading.Lock()
PAGE_BUCKETS = 250
PAGES_PER_READ = 2
READ_SECONDS = 7


def _fetch(start, end, interval):
    def stamp(at):
        return datetime.fromtimestamp(at, timezone.utc).isoformat()
    query = urllib.parse.urlencode({'start': stamp(start), 'end': stamp(end), 'granularity': interval})
    request = urllib.request.Request('https://api.exchange.coinbase.com/products/SOL-USD/candles?' + query,
                                     headers={'Accept': 'application/json', 'User-Agent': 'PumpWizard/0.2'})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=3) as response:
        body = response.read(100001)
    if len(body) > 100000:
        raise ValueError('Oversized USD history')
    raw = json.loads(body)
    if not isinstance(raw, list) or len(raw) > 300:
        raise ValueError('Invalid USD history')
    result = {}
    for row in raw:
        if not isinstance(row, list) or len(row) != 6 or type(row[0]) is not int:
            raise ValueError('Invalid historical USD candle')
        at, price = row[0], float(row[3])  # Opening trade: never use a later close to price an earlier balance.
        if at % interval or not math.isfinite(price) or price <= 0:
            raise ValueError('Invalid historical USD candle')
        if start <= at < end and at <= time.time():
            result[at] = price
    return result


def _prices(directory, start, end, interval):
    first, last = int(start // interval) * interval, int(end // interval) * interval
    with _LOCK, closing(sqlite3.connect(Path(directory) / 'wallet-usd-history.sqlite', timeout=10)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS prices (interval INTEGER, at INTEGER, price REAL, PRIMARY KEY(interval, at))')
        db.execute('CREATE TABLE IF NOT EXISTS ranges (interval INTEGER, start INTEGER, end INTEGER, PRIMARY KEY(interval, start, end))')
        db.execute('CREATE TABLE IF NOT EXISTS retry (id INTEGER PRIMARY KEY, at REAL)')
        ranges = db.execute('SELECT start,end FROM ranges WHERE interval=? AND end>? AND start<=? ORDER BY start', (interval, first, last)).fetchall()
        cursor = first
        def advance():
            nonlocal cursor
            for left, right in ranges:
                if left <= cursor < right:
                    cursor = right
        advance()
        delay = db.execute('SELECT at FROM retry WHERE id=1').fetchone()
        retry_at = delay[0] if delay else 0
        error = ''
        deadline = time.monotonic() + READ_SECONDS
        for _ in range(PAGES_PER_READ):
            if cursor > last or time.time() < retry_at or time.monotonic() >= deadline:
                break
            stop = min(last + interval, cursor + PAGE_BUCKETS * interval)
            try:
                prices = _fetch(cursor, stop, interval)
                db.executemany('INSERT OR REPLACE INTO prices VALUES (?,?,?)', [(interval, at, price) for at, price in prices.items()])
                db.execute('INSERT OR IGNORE INTO ranges VALUES (?,?,?)', (interval, cursor, stop))
                ranges.append((cursor, stop))
                ranges.sort()
                cursor = stop
                advance()
            except (TransferError, OSError, ValueError, KeyError, TypeError):
                error = 'Historical USD price provider is temporarily unavailable.'
                retry_at = time.time() + 60
                db.execute('INSERT OR REPLACE INTO retry VALUES (1,?)', (retry_at,))
                break
        prices = dict(db.execute('SELECT at,price FROM prices WHERE interval=? AND at>=? AND at<=? ORDER BY at', (interval, first, last)).fetchall())
        return prices, cursor <= last, error, max(3, int(retry_at - time.time()))


def value_history(directory, history, current_usd=None, *, fetch_prices=None):
    """Retain SOL receipts and add estimated USD, including price changes while SOL is held."""
    result = deepcopy(history)
    rows = history['rows']
    if not rows:
        return result
    start, end = rows[0]['at'], rows[-1]['at']
    interval = 3600 if end - start <= 12 * 86400 else 86400
    if end > start:
        prices, pending, error, retry = (fetch_prices or _prices)(directory, start, end, interval)
    else:
        prices, pending, error, retry = {}, False, '', 300
    enriched = []
    previous = None
    unpriced = 0
    for index, row in enumerate(rows):
        at = row['at']
        if previous and row.get('continuous') and previous['lamports'] is not None:
            first = (int(previous['at'] // interval) + 1) * interval
            for sample in range(first, int(at), interval):
                rate = prices.get(sample)
                usd = int(previous['lamports']) / 1e9 * rate if rate else 0.0 if previous['lamports'] == '0' else None
                if usd is None:
                    unpriced += 1
                enriched.append({'at': sample, 'lamports': previous['lamports'], 'usd': usd, 'continuous': True})
        rate = prices.get(int(at // interval) * interval)
        usd = int(row['lamports']) / 1e9 * rate if row['lamports'] is not None and rate else None
        if row['lamports'] == '0':
            usd = 0.0
        if index == len(rows) - 1 and current_usd is not None:
            usd = current_usd
        if row['lamports'] is not None and usd is None:
            unpriced += 1
        enriched.append(dict(row, usd=usd))
        previous = row
    result['rows'] = enriched
    result['usd_history'] = {'source': 'Coinbase SOL-USD', 'interval_seconds': interval,
                             'resolution': 'hourly' if interval == 3600 else 'daily',
                             'unpriced_readings': unpriced, 'sync_pending': pending, 'error': error}
    result['sync_pending'] = bool(result.get('sync_pending')) or pending
    if pending:
        result['retry_after'] = max(result.get('retry_after', 3) if history.get('sync_pending') else 3, retry)
    return result
