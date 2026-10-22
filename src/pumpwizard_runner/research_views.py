"""Read-only presentation of monitoring health and retained market observations."""
import math
import time


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def price_text(value):
    value = number(value)
    if value is None:
        return '—'
    if value == 0:
        return '$0.00'
    if value < 0.000000001:
        return f'${value:.5g}'
    decimals = max(2, min(12, 3 - math.floor(math.log10(value))))
    return f'${value:,.{decimals}f}'


def age_text(value, now=None):
    now = time.time() if now is None else now
    observed = number(value)
    if observed is None or observed <= 0:
        return 'Unknown age'
    if observed > now:
        return 'Clock mismatch'
    age = now - observed
    return f'{int(age)}s ago' if age < 60 else f'{int(age // 60)}m ago' if age < 3600 else f'{int(age // 3600)}h ago'


def is_fresh(value, now=None):
    now = time.time() if now is None else now
    observed = number(value)
    return observed is not None and observed > 0 and 0 <= now - observed <= 90


def session_state(instance, settings, health, held=0, now=None):
    now = time.time() if now is None else now
    last = health['last_run']
    running = bool(settings['enabled'] and instance['status'] == 'monitoring')
    fault = settings.get('fault')
    if fault:
        if fault == "KeyError: 'updated_at'":
            return dict(label='Blocked', tone='warning',
                        reason=('A previous market scan received an incomplete listing without a timestamp. '
                                'This build skips incomplete listings. Select Retry monitoring to resume.'),
                        action='View activity')
        return dict(label='Blocked', tone='warning', reason=f'Monitoring stopped: {fault}', action='Review setup')
    if last != 'never' and last['status'] == 'failed' and (running or held):
        return dict(label='Retrying' if running else 'Retrying exits', tone='warning',
                    reason=('Entries paused. ' if not running else '') + 'Last scan failed: ' + str(last.get('detail') or 'provider unavailable') + '. Monitoring retries automatically.',
                    action='View activity')
    if not running:
        if held:
            return dict(label='Exits only', tone='muted', reason='Entries paused; recorded positions remain scheduled for exit monitoring.', action='View activity')
        paused = instance['status'] == 'paused'
        return dict(label='Paused' if paused else 'Ready', tone='muted',
                    reason='Monitoring is paused.' if paused else 'Configuration saved. Monitoring has not been started.', action='Review setup')
    if last == 'never':
        return dict(label='Starting', tone='muted', reason='Waiting for the first completed monitoring scan.', action='View activity')
    if not is_fresh(last['finished_at'], now):
        return dict(label='Stale', tone='warning', reason='Last completed scan: ' + age_text(last['finished_at'], now) + '. Check connectivity and recent activity.', action='View activity')
    if not last['observations']:
        return dict(label='No data', tone='warning', reason='The last scan returned no supported quotes. Review the configuration and wait for another scan.', action='Review setup')
    return dict(label='Monitoring', tone='success', reason=f"Last scan {age_text(last['finished_at'], now)} · {last['observations']} observations. Monitoring does not authorize trades.", action='View activity')


def filter_market(rows, query='', minimum_cap=5000, maximum_cap=10_000_000,
                  minimum_liquidity=0, fresh_only=False, sort='Newest first', now=None):
    now = time.time() if now is None else now
    query = query.strip().casefold()
    selected = []
    for row in rows:
        cap, liquidity = number(row['market_cap_usd']), number(row['liquidity_usd'])
        if cap is None or liquidity is None or not minimum_cap <= cap <= maximum_cap or liquidity < minimum_liquidity:
            continue
        if query and query not in (row['symbol'] + ' ' + row['mint']).casefold():
            continue
        if fresh_only and not is_fresh(row['observed_at'], now):
            continue
        selected.append(row)
    key = {'Newest first': 'observed_at', 'Highest liquidity': 'liquidity_usd', 'Highest market cap': 'market_cap_usd'}.get(sort, 'observed_at')
    return sorted(selected, key=lambda r: number(r[key]) or 0, reverse=True)
