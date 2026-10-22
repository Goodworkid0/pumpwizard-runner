"""Capture offline core-frequency transitions using only the original runner.

The temporary reference ledger contains synthetic observations and virtual fills.
No application wallet/ledger, provider, signer or network is opened.
"""
import argparse
import copy
import hashlib
import json
import random
from pathlib import Path
import sys
import tempfile


def capture(root):
    sys.path.insert(0, str(root))
    from paper.ladder import LadderRunner

    cfg = next(x['config'] for x in json.loads((root / 'long_hold_experiments.json').read_text())['experiments']
               if x['id'] == 'core-frequency')
    cfg = dict(cfg, entry_style='momentum')

    def quote(at, y=115, real=50, mint='TestMint', **changes):
        return dict({'mint': mint, 'symbol': mint, 'route': 'pump_bonding_curve_sol',
                     'curve_address': 'Curve' + mint, 'observed_at': at, 'source_at': at, 'fx_at': at,
                     'x': 1_000_000_000, 'y': y, 'real_x': 500_000_000, 'real_y': real,
                     'supply': 1_000_000_000, 'quote_usd': 100, 'market_cap_usd': y * 100,
                     'decimals': 6, 'complete': False, 'mayhem': False}, **changes)

    def frame(at, y=115, evidence=None, **changes):
        return {'at': at, 'quotes': [quote(at, y, **changes)], 'evidence': evidence or {}}

    def warm(mints=('TestMint',)):
        return [{'at': 1000+i*10, 'quotes': [quote(1000+i*10, 110+i, 40+i, mint=m) for m in mints],
                 'evidence': {}} for i in range(4)]

    def empty(at):
        return {'at': at, 'quotes': [], 'evidence': {}}

    def amm(at, pool='AmmPool'):
        return {'at': at, 'quotes': [{'mint': 'TestMint', 'symbol': 'TestMint', 'route': 'amm',
                 'dex_id': 'pumpswap', 'pair_address': pool, 'valuation_basis': 'fdv',
                 'observed_at': at, 'price_usd': .00032, 'market_cap_usd': 320000,
                 'base_reserve': 100_000_000, 'liquidity_usd': 64000}], 'evidence': {}}

    def failed(n, mint, side):
        return int(hashlib.sha256(f'ladder:{n}:{mint}:{side}'.encode()).hexdigest()[:16], 16)/2**64 < cfg['failed_attempt_probability']

    failure_mint = next(f'Failed{i}' for i in range(10000) if failed(1, f'Failed{i}', 'entry'))
    retry_seed = next(n for n in range(10000) if not failed(n+1, 'TestMint', 'entry')
                      and failed(n+2, 'TestMint', 'exit') and not failed(n+3, 'TestMint', 'exit'))
    base = warm() + [frame(1031), frame(1032)]
    staggered = warm(('Z',)) + [
        {'at': 1005+i*10, 'quotes': [quote(1005+i*10,110+i,40+i,mint='A')], 'evidence': {}}
        for i in range(4)]
    staggered.sort(key=lambda b:b['at'])
    staggered += [{'at': at, 'quotes': [quote(at,y,mint=m) for m in ('A','Z')], 'evidence': {}}
                  for at,y in ((1037,115),(1040,60),(1042,60),(1044,60))]
    cases = [
        ('duplicate_observation_keeps_recorded_fx_basis', {}, [
            frame(1000+i*10,110+i,real=40+i*.11/3,
                  evidence={'TestMint': {'peak_cap': 50000}}) for i in range(4)] + [
            {'at':1031,'quotes':[quote(1030,113,40.11,quote_usd=90,market_cap_usd=10170)],'evidence':{}}]),
        ('staggered_mints_preserve_execution_order', {}, staggered),
        ('entry_delay_and_duplicate_observation', {}, warm() + [frame(1031),
            {'at': 1032, 'quotes': [quote(1030, 113, 43)], 'evidence': {}}, frame(1033)]),
        ('pending_cash_reserved', {'cash_usd': 10, 'realized_pnl_usd': -490}, warm(('A', 'B')) + [
            {'at': 1032, 'quotes': [quote(1032, mint='A'), quote(1032, mint='B')], 'evidence': {}}]),
        ('pending_capacity_reserved', {}, warm(tuple(f'Mint{i:02}' for i in range(21))) + [empty(1031)]),
        ('expiry_boundary_and_retry', {}, warm() + [
            {'at': 1060, 'quotes': [quote(1030, 113, 43)], 'evidence': {}}, empty(1061)] +
            [frame(at, 110+i, real=40+i) for i, at in enumerate((1070,1080,1090,1100,1102))]),
        ('changed_signal_cancels_entry', {}, warm() + [frame(1032,90), frame(1040,116)]),
        ('stale_quote_waits_then_expires', {}, warm() + [frame(1032, source_at=1), empty(1061)]),
        ('failed_entry_never_retries', {}, warm((failure_mint,)) +
            [frame(at, 115+i, mint=failure_mint) for i,at in enumerate((1032,1040,1050,1060,1070))]),
        ('partial_then_trail_then_no_reentry', {}, base + [frame(1040,320), frame(1041,320),
            frame(1042,320), frame(1050,170), frame(1052,170)] +
            [frame(at,110+i,real=40+i) for i,at in enumerate((1060,1070,1080,1090,1100))]),
        ('target_drops_before_fill', {}, base + [frame(1040,320), frame(1042,280), frame(1044,320), frame(1046,320)]),
        ('stop_replaces_partial_order_and_restarts_delay', {}, base + [frame(1040,320),
            frame(1041,60), frame(1042,60), frame(1043,60)]),
        ('ownership_precedes_stop_and_target', {}, base + [frame(1040,320),
            frame(1041,60,evidence={'TestMint': {'ownership': {'mint':'TestMint','status':'blocked'}}}),
            frame(1043,60)]),
        ('wrong_mint_ownership_does_not_exit', {}, base + [frame(1040,115,
            evidence={'TestMint': {'ownership': {'mint':'Other','status':'blocked'}}})]),
        ('multiple_targets_and_residual', {}, base + [frame(1040,1100,real=500),
            frame(1042,1100,real=500),frame(1050,600,real=400),frame(1052,600,real=400)]),
        ('only_still_reached_targets_fill', {}, base + [frame(1040,650,real=500),
            frame(1042,320),frame(1044,650,real=500),frame(1046,650,real=500)]),
        ('failed_exit_retries_same_order', {'attempts': retry_seed}, base + [frame(1040,60),frame(1042,60),frame(1044,60)]),
        ('quote_gap_keeps_unsold_position', {}, base + [empty(1631),empty(1632),empty(1640),frame(1642,115)]),
        ('holding_limit_with_no_quote', {}, base + [empty(865032),frame(865034,115)]),
        ('wrong_pool_cannot_fill_exit', {}, base + [frame(1040,320),frame(1042,320,curve_address='Other'),frame(1044,320)]),
        ('amm_requires_migration_then_pins_pool', {}, base + [amm(1040),
            dict(amm(1042),evidence={'TestMint': {'curve_complete': True}}),amm(1044,'Other'),amm(1046),frame(1048,115)]),
        ('invalid_curve_snapshots', {}, [frame(1000,complete=True),frame(1010,mayhem=True),
            frame(1020,validation_error='unverified'),frame(1030,market_cap_usd=90000)]),
        ('expired_histories_are_pruned', {}, warm() + [empty(1160),empty(1161)]),
        ('duplicate_and_backwards_clock', {}, base + [copy.deepcopy(base[-1]),empty(1000),frame(1040)]),
    ]
    cases.extend([
        ('source_and_observation_age_boundaries', {}, [
            frame(1000, observed_at=1001), frame(1010, source_at=1013),
            frame(1020, fx_at=1023), frame(1030, source_at=1000),
            frame(1040, source_at=1009), frame(1050, fx_at=1020),
            frame(1060, fx_at=1029), frame(1070, observed_at=1040),
            frame(1080, observed_at=1049)]),
        ('sample_gap_boundary_and_reset', {}, warm() + [
            frame(1032), frame(1062), frame(1093), frame(1103), frame(1113), frame(1123)]),
        ('migration_without_exit_then_quote_gap', {}, base + [
            frame(1040, complete=True), amm(1042),
            dict(amm(1044), evidence={'TestMint': {'curve_complete': True}}),
            empty(1645), amm(1647)]),
        ('partial_exit_then_fx_drawdown', {}, base + [
            frame(1040, 320), frame(1042, 320),
            frame(1050, 320, quote_usd=50, market_cap_usd=16000),
            frame(1052, 320, quote_usd=50, market_cap_usd=16000)]),
    ])
    # Independent seeded multi-mint streams exercise scheduling combinations,
    # gaps, changed FX, duplicate source observations, targets and failed fills.
    for seed in (7, 29, 101):
        rng = random.Random(seed)
        batches = warm(('A', 'B', 'C'))
        at = 1030
        for _ in range(48):
            at += rng.choice((1, 2, 10, 31))
            quotes = []
            evidence = {}
            for mint in ('A', 'B', 'C'):
                if rng.random() < .18:
                    continue
                y = rng.choice((60, 110, 115, 180, 320, 650, 1100))
                fx = rng.choice((80, 100, 120))
                quotes.append(quote(at, y, min(y, 50), mint=mint,
                                    quote_usd=fx, market_cap_usd=y*fx))
                if rng.random() < .08:
                    evidence[mint] = {'ownership': {'mint': mint, 'status': 'blocked'}}
            batches.append(dict(at=at, quotes=quotes, evidence=evidence))
        cases.append((f'multi_mint_seed_{seed}', {}, batches))
    result = []
    with tempfile.TemporaryDirectory(prefix='portfolio-reference-') as directory:
        for name, initial, batches in cases:
            runner = LadderRunner(Path(directory)/f'{name}.sqlite', cfg, {})
            try:
                runner.state.update(copy.deepcopy(initial))
                initial_state = copy.deepcopy(runner.state)
                frames = []
                for batch in batches:
                    before = runner.db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
                    runner.step(batch)
                    events = [dict(at=r['at'],mint=r['mint'],action=r['action'],data=json.loads(r['data']))
                              for r in runner.db.execute('SELECT * FROM events WHERE id>? ORDER BY id',(before,))]
                    frames.append({'batch': batch, 'state': copy.deepcopy(runner.state), 'events': events})
                result.append(dict(name=name, initial_state=initial_state, frames=frames))
            finally:
                runner.close()
    return dict(schema='pumpwizard.portfolio-reference/v1', configuration=cfg,
                scope='core-frequency fixed configuration; offline virtual portfolio, not chain fills',
                source_hashes={n: hashlib.sha256((root/'paper'/n).read_bytes()).hexdigest()
                               for n in ('ladder.py','model.py')}, cases=result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--pumpwizard-root', type=Path, required=True)
    args = parser.parse_args()
    target = Path(__file__).parents[1]/'tests/fixtures/portfolio-reference.json'
    target.write_text(json.dumps(capture(args.pumpwizard_root), indent=2)+'\n')
    print(target)
