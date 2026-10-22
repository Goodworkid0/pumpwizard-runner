from pumpwizard_runner import wallet_usd_history as usd


HOUR = 3600
START = 1700002800


def history(rows):
    return {'sender': 'test', 'network': 'mainnet-beta', 'rows': rows, 'checked_at': rows[-1]['at'],
            'transactions': 1, 'history_complete': True, 'sync_pending': False, 'retry_after': 300}


def row(at, balance, continuous=True):
    return {'at': at, 'lamports': balance, 'usd': None, 'continuous': continuous}


def test_usd_uses_historical_rates_and_adds_price_changes_between_transactions(tmp_path):
    raw = history([row(START, '1000000000', False), row(START + 2 * HOUR, '1000000000')])
    def rates(*args):
        return {START: 80, START + HOUR: 90, START + 2 * HOUR: 100}, False, '', 300
    result = usd.value_history(tmp_path, raw, 120, fetch_prices=rates)
    assert [(r['at'], r['usd']) for r in result['rows']] == [(START, 80), (START + HOUR, 90), (START + 2 * HOUR, 120)]
    assert all(r['lamports'] == '1000000000' for r in result['rows'])
    assert all(r['usd'] is None for r in raw['rows'])
    assert result['usd_history']['resolution'] == 'hourly'


def test_missing_price_intervals_and_receipts_stay_gaps(tmp_path):
    raw = history([row(START, '1000000000', False), row(START + 2 * HOUR, None, False), row(START + 3 * HOUR, '2000000000', False)])
    result = usd.value_history(tmp_path, raw, None, fetch_prices=lambda *args: ({START: 80, START + 3 * HOUR: 100}, False, '', 300))
    assert [r['usd'] for r in result['rows']] == [80, None, 200]
    assert not result['rows'][-1]['continuous']
    long = history([row(START, '1000000000', False), row(START + 2 * HOUR, '1000000000')])
    result = usd.value_history(tmp_path, long, 120, fetch_prices=lambda *args: ({START: 80}, False, '', 300))
    assert result['rows'][1]['usd'] is None
    assert result['usd_history']['unpriced_readings'] == 1


def test_price_cache_resumes_bounded_pages_and_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(usd, 'PAGE_BUCKETS', 2)
    monkeypatch.setattr(usd, 'PAGES_PER_READ', 1)
    calls = []
    def fetch(start, end, interval):
        calls.append((start, end))
        return {at: 80 + i for i, at in enumerate(range(start, end, interval))}
    monkeypatch.setattr(usd, '_fetch', fetch)
    first = usd._prices(tmp_path, START, START + 4 * HOUR, HOUR)
    assert first[1] and len(first[0]) == 2
    second = usd._prices(tmp_path, START, START + 4 * HOUR, HOUR)
    assert second[1] and len(second[0]) == 4
    third = usd._prices(tmp_path, START, START + 4 * HOUR, HOUR)
    assert not third[1] and len(third[0]) == 5
    again = usd._prices(tmp_path, START, START + 4 * HOUR, HOUR)
    assert again == third and len(calls) == 3


def test_provider_failure_keeps_cached_prices_and_sets_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(usd, 'PAGE_BUCKETS', 2)
    monkeypatch.setattr(usd, 'PAGES_PER_READ', 1)
    monkeypatch.setattr(usd, '_fetch', lambda start, end, interval: {at: 80 for at in range(start, end, interval)})
    usd._prices(tmp_path, START, START + 4 * HOUR, HOUR)
    def offline(*args):
        raise OSError('provider unavailable')
    monkeypatch.setattr(usd, '_fetch', offline)
    prices, pending, error, retry = usd._prices(tmp_path, START, START + 4 * HOUR, HOUR)
    assert prices == {START: 80, START + HOUR: 80}
    assert pending and 'temporarily unavailable' in error and retry >= 59


def test_long_history_uses_daily_rates_and_zero_balance_is_known(tmp_path):
    raw = history([row(START, '0', False), row(START + 20 * 86400, '0')])
    result = usd.value_history(tmp_path, raw, None, fetch_prices=lambda *args: ({}, False, '', 300))
    assert result['usd_history']['resolution'] == 'daily'
    assert all(r['usd'] == 0 for r in result['rows'])
