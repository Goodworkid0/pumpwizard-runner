import copy
import pytest

from pumpwizard_runner.research_views import age_text, filter_market, is_fresh, price_text, session_state
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.strategies import parse_package
from pumpwizard_runner.assistant_chat import local_context


def state(enabled=True, paused=False, held=0, fault='', last=None):
    return session_state({'status': 'paused' if paused else 'monitoring'}, {'enabled': enabled, 'fault': fault},
                         {'last_run': last or 'never'}, held, now=1000)


def test_monitoring_states_keep_fault_pause_and_trading_separate():
    completed = dict(finished_at=980, status='completed', observations=3)
    assert state(last=completed)['label'] == 'Monitoring'
    assert 'does not authorize' in state(last=completed)['reason']
    assert state(enabled=False)['label'] == 'Ready'
    assert state(paused=True)['label'] == 'Paused'
    assert state(paused=True, held=1)['label'] == 'Exits only'
    assert state()['label'] == 'Starting'
    assert state(fault='Invalid rules', last=completed)['label'] == 'Blocked'
    assert 'Invalid rules' in state(fault='Invalid rules')['reason']
    prior_listing_fault = state(fault="KeyError: 'updated_at'", last=completed)
    assert prior_listing_fault['label'] == 'Blocked'
    assert 'incomplete listing' in prior_listing_fault['reason']
    assert prior_listing_fault['action'] == 'View activity'
    assert state(last=dict(completed, finished_at=800))['label'] == 'Stale'
    assert state(last=dict(completed, finished_at=2000))['label'] == 'Stale'
    assert state(last=dict(completed, observations=0))['label'] == 'No data'
    failed = dict(completed, status='failed', detail='RPC timeout')
    assert state(last=failed)['label'] == 'Retrying'
    assert state(paused=True, held=1, last=failed)['label'] == 'Retrying exits'
    assert state(paused=True, last=failed)['label'] == 'Paused'
    assert 'RPC timeout' in state(last=failed)['reason']


def test_market_filter_freshness_missing_data_and_precise_prices():
    row = dict(mint='mint-one', symbol='COIN', market_cap_usd='10000', liquidity_usd='2000', observed_at=980)
    rows = [row, dict(row, mint='mint-two', observed_at=700, liquidity_usd='4000'), dict(row, mint='future', observed_at=1100)]
    before = copy.deepcopy(rows)
    assert [r['mint'] for r in filter_market(rows, fresh_only=True, now=1000)] == ['mint-one']
    assert [r['mint'] for r in filter_market(rows, query='MINT-TWO', now=1000)] == ['mint-two']
    assert filter_market(rows, sort='Highest liquidity', now=1000)[0]['mint'] == 'mint-two'
    assert not filter_market(rows, query='absent', now=1000)
    assert not filter_market([dict(row, liquidity_usd='nan')], now=1000)
    assert rows == before
    assert not is_fresh(1100, 1000) and is_fresh(910, 1000) and not is_fresh(909, 1000)
    assert age_text(1100, 1000) == 'Clock mismatch'
    assert price_text('0.00000012') != '$0.00'
    assert price_text(None) == '—' and price_text('nan') == '—' and price_text(-1) == '—'


def test_coin_reads_are_exact_bounded_and_do_not_mutate(tmp_path):
    library = StrategyLibrary(tmp_path)
    try:
        strategy = parse_package({'schema': 'pumpwizard.strategy-handoff/v1', 'strategy': {'id': 'core-frequency', 'label': 'Session'}, 'configuration': CONFIG})
        library.import_strategy(strategy)
        ident = library.create_instance(strategy.digest)
        with library._connection:
            for mint in ('mint-one', 'mint-two'):
                for i in range(4):
                    library._connection.execute('INSERT INTO market_watch_samples(mint,symbol,observed_at,price_usd,market_cap_usd,liquidity_usd) VALUES(?,?,?,?,?,?)', (mint, 'SAME', 100+i, '.00001', '10000', '2000'))
        library.record_decisions(ident, [dict(at=105, mint='mint-one', kind='entry_blocked', reason='Stale observation')])
        before = list(library._connection.iterdump())
        assert [r['observed_at'] for r in library.coin_history('mint-one', 2)] == [103, 102]
        assert [r['observed_at'] for r in library.coin_history('mint-one', 2, before=102)] == [101, 100]
        assert not library.coin_history("' OR 1=1 --")
        assert library.coin_activity('mint-one')[0]['instance_id'] == ident
        assert not library.coin_activity('mint-two')
        with library._connection:
            library._connection.execute("UPDATE market_watch_samples SET symbol='mint-one' WHERE mint='mint-two'")
        context = local_context(library, tmp_path / 'catalog.json',
                                dict(kind='coin_history', query='mint-one', limit=100, reason='Selected coin'), exact_mint=True)
        assert len(context['rows']) == 4 and all(r['mint'] == 'mint-one' for r in context['rows'])
        with library._connection:
            library._connection.execute("UPDATE market_watch_samples SET symbol='SAME' WHERE mint='mint-two'")
        with pytest.raises(ValueError):
            library.coin_history('mint-one', 501)
        with pytest.raises(ValueError):
            library.coin_history('mint-one', before=float('nan'))
        assert before == list(library._connection.iterdump())
    finally:
        library.close()
