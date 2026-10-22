"""Whole-portfolio expectations come exclusively from the original runner."""
import copy
import json
from pathlib import Path
import sqlite3

import pytest

from pumpwizard_runner.portfolio import CONFIG, PortfolioEngine, PortfolioJournal

REFERENCE = json.loads((Path(__file__).parent/'fixtures/portfolio-reference.json').read_text())


def assert_tree(actual, expected, path='root'):
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys(), path
        for key in expected:
            assert_tree(actual[key], expected[key], path + '.' + key)
    elif isinstance(expected, list):
        assert len(actual) == len(expected), path
        for i, (left, right) in enumerate(zip(actual, expected)):
            assert_tree(left, right, f'{path}[{i}]')
    elif type(expected) is float:
        assert actual == pytest.approx(expected, rel=1e-12, abs=1e-9), path
    else:
        assert actual == expected, path


@pytest.mark.parametrize('case', REFERENCE['cases'], ids=lambda c:c['name'])
@pytest.mark.parametrize('restart', [False, True], ids=['continuous','restart-every-frame'])
def test_reference_portfolio_transitions(case, restart):
    assert CONFIG == REFERENCE['configuration']
    engine = PortfolioEngine(REFERENCE['configuration'], case['initial_state'])
    for i, frame in enumerate(case['frames']):
        events = engine.step(frame['batch'])
        assert_tree(events, frame['events'], f'frame[{i}].events')
        assert_tree(engine.state, frame['state'], f'frame[{i}].state')
        if restart:
            engine = PortfolioEngine.restore(json.loads(json.dumps(engine.checkpoint(), sort_keys=True)))


def test_durable_replay_reopens_with_exactly_once_events(tmp_path):
    case = next(c for c in REFERENCE['cases'] if c['name']=='partial_then_trail_then_no_reentry')
    for frame in case['frames']:
        journal = PortfolioJournal(tmp_path)
        try:
            events = journal.advance('test', frame['batch'])
            assert_tree(events, frame['events'])
            before = journal.checkpoint('test')
            assert_tree(before['state'], frame['state'])
            assert journal.advance('test', frame['batch']) == []
            assert journal.checkpoint('test') == before
        finally:
            journal.close()
    journal = PortfolioJournal(tmp_path)
    try:
        assert journal.db.execute('SELECT COUNT(*) FROM portfolio_frames').fetchone()[0] == len(case['frames'])
        assert not journal.checkpoint('test')['state']['positions']
        altered = copy.deepcopy(case['frames'][-1]['batch'])
        altered['evidence'] = {'TestMint': {'peak_cap': 100000}}
        with pytest.raises(ValueError, match='recorded batch changed'):
            journal.advance('test', altered)
    finally:
        journal.close()
    assert {p.name for p in tmp_path.iterdir()} == {'strategy-replays.sqlite'}


def test_failed_commit_rolls_back_input_events_and_state(tmp_path):
    journal = PortfolioJournal(tmp_path)
    batch = REFERENCE['cases'][0]['frames'][0]['batch']
    try:
        journal.db.execute("CREATE TRIGGER fail_commit BEFORE INSERT ON portfolio_runs BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='test failure'):
            journal.advance('test', batch)
        assert journal.checkpoint('test') is None
        assert journal.db.execute('SELECT COUNT(*) FROM portfolio_frames').fetchone()[0] == 0
        journal.db.execute('DROP TRIGGER fail_commit')
        journal.advance('test', batch)
        assert journal.checkpoint('test')['state']['last_at'] == batch['at']
    finally:
        journal.close()


def test_invalid_step_rolls_back_memory_and_rejects_other_configurations():
    engine = PortfolioEngine()
    checkpoint = engine.checkpoint()
    with pytest.raises(KeyError):
        engine.step({'at': 1, 'quotes': []})
    assert engine.checkpoint() == checkpoint
    with pytest.raises(ValueError, match='pinned'):
        PortfolioEngine(dict(CONFIG, entry_policy='strict'))
    with pytest.raises(ValueError, match='checkpoint version'):
        PortfolioEngine.restore(dict(checkpoint, version='future'))


def test_reference_scenarios_actually_exercise_required_transitions():
    cases = {c['name']:c for c in REFERENCE['cases']}

    def actions(name):
        return [e['action'] for f in cases[name]['frames'] for e in f['events']]

    assert 'failed_entry' in actions('failed_entry_never_retries')
    assert 'failed_exit' in actions('failed_exit_retries_same_order')
    assert actions('failed_exit_retries_same_order')[-1] == 'fill_exit'
    assert 'cancel_exit' in actions('target_drops_before_fill')
    assert actions('partial_then_trail_then_no_reentry').count('signal_entry') == 1
    assert 'fill_partial_exit' in actions('partial_then_trail_then_no_reentry')
    assert actions('partial_then_trail_then_no_reentry')[-1] == 'fill_exit'
    assert len(cases['pending_capacity_reserved']['frames'][-1]['state']['pending']) == 20
    assert len(cases['pending_cash_reserved']['frames'][3]['state']['pending']) == 1
    stop = [e for f in cases['stop_replaces_partial_order_and_restarts_delay']['frames'] for e in f['events']]
    assert [(e['at'],e['data']['reason']) for e in stop if e['action']=='signal_exit'] == [(1040,'market_cap_target'),(1041,'stop_loss')]
    assert stop[-1]['at'] == 1043


def test_cli_replay_resume_status_and_no_wallet_side_effects(tmp_path, capsys):
    from pumpwizard_runner.cli import main

    example = Path(__file__).parents[1]/'examples/core-frequency-replay.json'
    prefix = ['--data-dir',str(tmp_path)]
    command = prefix + ['portfolio-replay',str(example),'--run-id','example']
    assert main(command) == 0
    first = json.loads(capsys.readouterr().out)
    assert first['execution'] == 'offline-virtual-portfolio'
    assert first['new_events'][-1]['action'] == 'fill_exit'
    assert not first['checkpoint']['state']['positions']
    assert main(command) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed['new_events'] == []
    assert resumed['checkpoint'] == first['checkpoint']
    assert main(prefix + ['portfolio-status','example']) == 0
    assert json.loads(capsys.readouterr().out) == resumed
    assert {p.name for p in tmp_path.iterdir()} == {'strategy-replays.sqlite'}


def test_cli_rejects_unverified_configuration_before_opening_ledger(tmp_path, capsys):
    from pumpwizard_runner.cli import main

    recording = tmp_path/'bad.json'
    recording.write_text(json.dumps(dict(schema='pumpwizard.portfolio-recording/v1', batches=[],
                                        configuration=dict(CONFIG, max_positions=100))))
    directory = tmp_path/'data'
    assert main(['--data-dir',str(directory),'portfolio-replay',str(recording),'--run-id','bad']) == 2
    assert 'pinned' in capsys.readouterr().err
    assert not directory.exists()


def test_two_connections_cannot_apply_same_batch_twice(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    batch = next(c for c in REFERENCE['cases'] if c['name']=='partial_then_trail_then_no_reentry')['frames'][3]['batch']
    warm = next(c for c in REFERENCE['cases'] if c['name']=='partial_then_trail_then_no_reentry')['frames'][:3]
    journal = PortfolioJournal(tmp_path)
    for frame in warm:
        journal.advance('same',frame['batch'])
    journal.close()
    barrier = Barrier(2)

    def advance(_):
        journal = PortfolioJournal(tmp_path)
        try:
            barrier.wait(timeout=10)
            return journal.advance('same',batch)
        finally:
            journal.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        events = list(workers.map(advance,range(2)))
    assert sorted(len(e) for e in events) == [0,1]


@pytest.mark.parametrize('case', REFERENCE['cases'], ids=lambda c:c['name'])
def test_reference_portfolio_through_database_restart(case, tmp_path):
    journal = PortfolioJournal(tmp_path)
    checkpoint = PortfolioEngine(REFERENCE['configuration'],case['initial_state']).checkpoint()
    with journal.db:
        journal.db.execute('INSERT INTO portfolio_runs VALUES (?,?)',('test',json.dumps(checkpoint,sort_keys=True)))
    journal.close()
    for frame in case['frames']:
        journal = PortfolioJournal(tmp_path)
        try:
            before = journal.checkpoint('test')['state']['last_at']
            at = frame['batch']['at']
            if before is not None and at < before:
                with pytest.raises(ValueError,match='backwards'):
                    journal.advance('test',frame['batch'])
            else:
                assert_tree(journal.advance('test',frame['batch']),frame['events'])
            assert_tree(journal.checkpoint('test')['state'],frame['state'])
        finally:
            journal.close()
