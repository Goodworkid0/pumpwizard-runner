"""Versioned core-frequency portfolio replay. No wallet, RPC or signer access.

USD costs and fills reproduce the historical sensitivity model. They are never
on-chain positions. Expected transitions are captured independently from the
original LadderRunner; the runtime only depends on this package.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from .decision import DecisionEngine, MarketQuote
from .strategies import parse_package

VERSION = 'core-frequency-portfolio-v1'
CONFIG = json.loads((Path(__file__).parent / 'data/core-frequency-portfolio.json').read_text())


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def initial_state():
    return dict(cash_usd=CONFIG['budget_usd'], positions={}, pending={}, attempted=[],
                histories={}, candidates={}, started_at=None, last_at=None, attempts=0,
                failed_fees_usd=0, observations=0, realized_pnl_usd=0, fees_usd=0)


class PortfolioEngine:
    def __init__(self, configuration=None, state=None):
        self.cfg = copy.deepcopy(CONFIG if configuration is None else configuration)
        if encode(self.cfg) != encode(CONFIG):
            raise ValueError('Portfolio replay supports only the pinned core-frequency configuration')
        self.rules = DecisionEngine(parse_package({
            'schema': 'pumpwizard.strategy-handoff/v1',
            'strategy': {'id': 'core-frequency', 'label': 'Core frequency'},
            'configuration': self.cfg,
        }))
        self.state = copy.deepcopy(initial_state() if state is None else state)
        self.events = []

    def checkpoint(self):
        return {'version': VERSION, 'configuration': copy.deepcopy(self.cfg), 'state': copy.deepcopy(self.state),
                'position_order': list(self.state['positions']), 'pending_order': list(self.state['pending'])}

    @classmethod
    def restore(cls, checkpoint):
        if checkpoint.get('version') != VERSION:
            raise ValueError('Unsupported portfolio checkpoint version')
        engine = cls(checkpoint['configuration'], checkpoint['state'])
        # Canonical JSON sorts object keys. Preserve execution order explicitly:
        # it determines failed-attempt draws and which entry receives cash first.
        for field, order in (('positions', 'position_order'), ('pending', 'pending_order')):
            if set(checkpoint[order]) != set(engine.state[field]) or len(checkpoint[order]) != len(engine.state[field]):
                raise ValueError('Invalid portfolio execution order')
            engine.state[field] = {mint: engine.state[field][mint] for mint in checkpoint[order]}
        return engine

    def event(self, at, mint, action, data):
        self.events.append(dict(at=at, mint=mint, action=action, data=copy.deepcopy(data)))

    def step(self, batch):
        at = batch['at']
        if type(at) not in (float, int) or not math.isfinite(at):
            raise ValueError('Portfolio clock must be finite')
        if self.state['last_at'] is not None and at <= self.state['last_at']:
            return []
        before = copy.deepcopy(self.state)
        self.events = []
        try:
            self._step(batch)
            s = self.state
            equity = s['cash_usd'] + sum(p['mark_value_usd'] for p in s['positions'].values())
            unrealized = sum(p['mark_value_usd'] - p['remaining_cost_usd'] for p in s['positions'].values())
            if abs(equity - self.cfg['budget_usd'] - (
                    s['realized_pnl_usd'] - s['failed_fees_usd'] + unrealized)) >= 1e-6:
                raise ValueError('Portfolio accounting invariant failed')
            encode(s)
        except Exception:
            self.state, self.events = before, []
            raise
        return copy.deepcopy(self.events)

    def _quote(self, row, at):
        """Reject unsupported/malformed snapshots before shared decision screens."""
        c = self.cfg
        if row.get('route') == 'pump_bonding_curve_sol':
            fields = ('observed_at', 'source_at', 'fx_at', 'quote_usd', 'x', 'y',
                      'real_x', 'real_y', 'supply', 'market_cap_usd', 'decimals')
            if row.get('validation_error') or any(type(row.get(k)) not in (int, float) or
                    not math.isfinite(row[k]) for k in fields):
                return None
            if (row.get('complete', True) or row.get('mayhem', False) or
                    min(row['quote_usd'], row['x'], row['y'], row['supply'], row['real_x']) <= 0 or
                    not 0 <= row['real_y'] <= row['y'] or row['real_x'] > row['x'] or
                    not 0 <= row['decimals'] <= 12 or int(row['decimals']) != row['decimals']):
                return None
            if any(not -2 <= at - row[k] <= c['quote_max_age_seconds'] for k in ('source_at', 'fx_at')):
                return None
            implied = row['y'] / row['x'] * row['supply'] * row['quote_usd']
            if abs(row['market_cap_usd'] / implied - 1) > .10:
                return None
            quote = MarketQuote.from_record(row)
        else:
            fields = ('observed_at', 'price_usd', 'market_cap_usd', 'base_reserve', 'liquidity_usd')
            if any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) or row[k] <= 0 for k in fields):
                return None
            if (row.get('dex_id') != 'pumpswap' or not row.get('pair_address') or
                    row.get('valuation_basis') != 'fdv' or row['liquidity_usd'] < c['min_amm_liquidity_usd']):
                return None
            # Reference AMM liquidity is one-sided reserve implied by price.
            quote = MarketQuote.from_record(dict(row, reserve_usd=row['base_reserve'] * row['price_usd'],
                                                 liquidity_usd=row['base_reserve'] * row['price_usd']))
        return quote if 0 <= at - quote.observed_at <= c['quote_max_age_seconds'] else None

    def _entry_reason(self, mint, quote, evidence, at):
        if quote is None:
            return 'No fresh supported bonding-curve quote'
        self.rules.histories[mint] = [replace(quote, observed_at=h['at'], price_native=h['price'],
                                              liquidity_native=h['real_sol'], quote_usd=h['fx'])
                                      for h in self.state['histories'].get(mint, [])]
        return self.rules.entry_reason(quote, evidence, at)

    def _sell(self, q, p, quantity=None):
        if q is None:
            return None
        qty = p['quantity'] if quantity is None else quantity
        if not 0 < qty <= p['quantity'] * (1 + 1e-9):
            return None
        if p.get('amm_pool') and (q.route != 'amm' or q.pool != p['amm_pool']):
            return None
        if q.route == 'curve':
            if q.pool != p['entry_pool'] or p['quantity'] >= q.base_reserve or q.liquidity_usd <= 0:
                return None
            base = q.base_reserve - p['quantity']
            reserve = q.base_reserve * q.reserve_usd / base
            real = q.liquidity_usd + reserve - q.reserve_usd
        else:
            if abs(q.market_cap_usd / q.price_usd / p['entry_supply'] - 1) > .10:
                return None
            base, reserve, real = q.base_reserve, q.reserve_usd, q.liquidity_usd
        gross = reserve * qty / (base + qty)
        if gross > real or qty / (base + qty) > .10:
            return None
        gross *= 1 - self.cfg['adverse_fraction']
        fees = gross * self.cfg['fee_fraction'] + self.cfg['fixed_fee_usd']
        net = gross - fees
        return (dict(quantity=qty, proceeds_usd=net, exit_price_usd=net / qty,
                     exit_fees_usd=fees, route=q.route, pool=q.pool) if net > 0 else None)

    def _buy(self, q):
        c = self.cfg
        pool = (c['order_usd'] - c['fixed_fee_usd'] - c['entry_rent_usd']) / (1 + c['fee_fraction'])
        raw = q.base_reserve * pool / (q.reserve_usd + pool)
        qty = math.floor(raw * (1 - c['adverse_fraction']) * 10**q.decimals) / 10**q.decimals
        return dict(quantity=qty, initial_quantity=qty, entry_pool=q.pool, entry_supply=q.supply,
                    cost_usd=c['order_usd'], remaining_cost_usd=c['order_usd'],
                    entry_price_usd=c['order_usd'] / qty,
                    entry_fees_usd=pool * c['fee_fraction'] + c['fixed_fee_usd'] + c['entry_rent_usd'])

    def _failed(self, at, mint, side):
        s, c = self.state, self.cfg
        s['attempts'] += 1
        value = int(hashlib.sha256(f"ladder:{s['attempts']}:{mint}:{side}".encode()).hexdigest()[:16], 16) / 2**64
        if value >= c['failed_attempt_probability']:
            return False
        fee = min(max(0, s['cash_usd']), c['fixed_fee_usd'])
        s['cash_usd'] -= fee
        s['failed_fees_usd'] += fee
        s['fees_usd'] += fee
        self.event(at, mint, 'failed_' + side, {'fee_usd': fee})
        return True

    def _schedule_exit(self, p, at, reason, stages=()):
        p['exit_order'] = dict(at=at, reason=reason, stages=list(stages))
        p['status'] = 'partial_exit_pending' if stages else 'exit_pending'
        self.event(at, p['mint'], 'signal_exit', p['exit_order'])

    def _exit(self, mint, p, q, evidence, at):
        s, c = self.state, self.cfg
        if evidence.get('curve_complete') is True and not p.get('migration_seen'):
            p['migration_seen'] = True
            self.event(at, mint, 'curve_completed', {'observed_at': at})
        if q and q.route == 'amm' and not p.get('migration_seen'):
            q = None
        mark = self._sell(q, p)
        if mark:
            if q.route == 'amm' and not p.get('amm_pool'):
                p['amm_pool'] = q.pool
                self.event(at, mint, 'route_transition', {'pool': q.pool, 'source': 'indexed_pumpswap'})
            p['last_usable_at'], p['last_cap'] = q.observed_at, q.market_cap_usd
            p['peak_price_usd'] = max(p['peak_price_usd'], q.price_usd)
        p['mark_value_usd'] = mark['proceeds_usd'] if mark else 0
        p['mark_reason'] = None if mark else 'Awaiting a fresh supported quote; unsold tokens remain open'
        reason, risk = None, evidence.get('ownership') or {}
        if risk.get('mint') == mint and risk.get('status') == 'blocked':
            reason = 'ownership_flag'
        elif at - p['entry_at'] >= c['max_hold_seconds']:
            reason = 'ten_day_limit'
        elif not mark and at - p['last_usable_at'] >= c['quote_gap_exit_seconds']:
            reason = 'quote_gap'
        elif mark and mark['proceeds_usd'] / p['remaining_cost_usd'] - 1 <= c['stop_loss_fraction']:
            reason = 'stop_loss'
        elif mark and p['sold_stages'] and q.price_usd <= p['peak_price_usd'] * (1 - c['trailing_drawdown_fraction']):
            reason = 'trailing_exit'
        order = p.get('exit_order')
        if reason and (not order or order['stages']):
            self._schedule_exit(p, at, reason)
            order = p['exit_order']
        if order and mark and q.observed_at > order['at'] and at - order['at'] >= c['fill_delay_seconds']:
            stages = [i for i in order['stages'] if q.market_cap_usd >= c['targets'][i]['market_cap_usd']]
            if order['stages'] and not stages:
                self.event(at, mint, 'cancel_exit', {'reason': 'Target no longer reached on the fill quote'})
                p.pop('exit_order')
                p['status'] = 'open'
            else:
                qty = min(p['quantity'], p['initial_quantity'] * sum(c['targets'][i]['initial_fraction'] for i in stages)) if stages else p['quantity']
                sale = self._sell(q, p, qty)
                if sale and not self._failed(at, mint, 'exit'):
                    cost = p['remaining_cost_usd'] * qty / p['quantity']
                    sale.update(mint=mint, symbol=p['symbol'], entry_at=p['entry_at'], exit_at=at,
                                entry_price_usd=p['entry_price_usd'], entry_cap=p['entry_cap'],
                                exit_cap=q.market_cap_usd, cost_usd=cost, pnl_usd=sale['proceeds_usd'] - cost,
                                initial_fraction=qty / p['initial_quantity'], stages=stages, exit_reason=order['reason'])
                    s['cash_usd'] += sale['proceeds_usd']
                    s['realized_pnl_usd'] += sale['pnl_usd']
                    s['fees_usd'] += sale['exit_fees_usd']
                    p['quantity'] = max(0, p['quantity'] - qty)
                    p['remaining_cost_usd'] = max(0, p['remaining_cost_usd'] - cost)
                    p['proceeds_usd'] += sale['proceeds_usd']
                    p['realized_pnl_usd'] += sale['pnl_usd']
                    p['sold_stages'] = sorted(set(p['sold_stages']) | set(stages))
                    p.pop('exit_order')
                    p['status'] = 'open'
                    closed = p['quantity'] <= p['initial_quantity'] * 1e-9
                    self.event(at, mint, 'fill_exit' if closed else 'fill_partial_exit', sale)
                    if closed:
                        del s['positions'][mint]
                        return
                    residual = self._sell(q, p)
                    p['mark_value_usd'] = residual['proceeds_usd'] if residual else 0
                    p['mark_reason'] = None if residual else 'Residual position has no usable exit quote'
        if mark and not p.get('exit_order'):
            stages = [i for i, target in enumerate(c['targets']) if i not in p['sold_stages']
                      and q.market_cap_usd >= target['market_cap_usd']]
            if stages:
                self._schedule_exit(p, at, 'market_cap_target', stages)

    def _step(self, batch):
        s, c, at = self.state, self.cfg, batch['at']
        if s['started_at'] is None:
            s['started_at'] = at
        s['last_at'] = at
        rows = {q['mint']: q for q in batch['quotes']}
        quotes = {mint: self._quote(row, at) for mint, row in rows.items()}
        evidence = batch['evidence']
        for mint, q in quotes.items():
            if q is None or q.route != 'curve':
                continue
            history = s['histories'].setdefault(mint, [])
            if history and q.observed_at <= history[-1]['at']:
                continue
            if history and q.observed_at - history[-1]['at'] > c['max_sample_gap_seconds']:
                history.clear()
            history.append(dict(at=q.observed_at, price=q.price_native, real_sol=q.liquidity_native, fx=q.quote_usd))
            while len(history) > 1 and history[1]['at'] <= q.observed_at - c['observation_seconds']:
                history.pop(0)
            s['observations'] += 1
        ttl = max(120, c['observation_seconds'] + c['max_sample_gap_seconds'])
        s['histories'] = {mint: h for mint, h in s['histories'].items() if h and at - h[-1]['at'] <= ttl}
        for mint, position in list(s['positions'].items()):
            self._exit(mint, position, quotes.get(mint), evidence.get(mint, {}), at)
        for mint, order in list(s['pending'].items()):
            q = quotes.get(mint)
            if at - order['at'] > c['entry_expiry_seconds']:
                del s['pending'][mint]
                self.event(at, mint, 'cancel_entry', {'reason': 'Entry quote expired'})
                continue
            if q is None or q.observed_at <= order['at'] or at - order['at'] < c['fill_delay_seconds']:
                continue
            reason = self._entry_reason(mint, q, evidence.get(mint, {}), at)
            del s['pending'][mint]
            if reason or s['cash_usd'] < c['order_usd']:
                self.event(at, mint, 'cancel_entry', {'reason': reason or 'Insufficient virtual cash'})
                continue
            s['attempted'].append(mint)
            if self._failed(at, mint, 'entry'):
                continue
            p = self._buy(q)
            s['cash_usd'] -= p['cost_usd']
            s['fees_usd'] += p['entry_fees_usd']
            p.update(mint=mint, symbol=rows[mint]['symbol'], entry_at=at, entry_cap=q.market_cap_usd,
                     last_cap=q.market_cap_usd, peak_price_usd=q.price_usd, last_usable_at=q.observed_at,
                     proceeds_usd=0, realized_pnl_usd=0, sold_stages=[], status='open', mark_reason=None,
                     ownership_status=(evidence.get(mint, {}).get('ownership') or {}).get('status', 'unknown'))
            p['mark_value_usd'] = self._sell(q, p)['proceeds_usd']
            s['positions'][mint] = p
            self.event(at, mint, 'fill_entry', p)
        s['candidates'] = {}
        for mint in sorted(rows):
            if mint in s['positions'] or mint in s['pending'] or mint in s['attempted']:
                continue
            q = quotes[mint]
            reason = self._entry_reason(mint, q, evidence.get(mint, {}), at)
            if not reason and (len(set(s['positions']) | set(s['pending'])) >= c['max_positions'] or
                               s['cash_usd'] - len(s['pending']) * c['order_usd'] < c['order_usd']):
                reason = 'Portfolio capacity or virtual cash reserved'
            if q and c['entry_cap_min'] <= q.market_cap_usd <= c['entry_cap_max']:
                s['candidates'][mint] = dict(mint=mint, symbol=rows[mint]['symbol'],
                                           market_cap_usd=q.market_cap_usd, reason=reason or 'Entry scheduled')
            if not reason:
                s['pending'][mint] = {'at': at}
                self.event(at, mint, 'signal_entry', dict(cap=q.market_cap_usd, reason='simulation_unverified_signal'))


class PortfolioJournal:
    """Atomic offline replay transitions, isolated from orders and wallet ledgers."""
    def __init__(self, data_dir):
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(data_dir) / 'strategy-replays.sqlite', timeout=10)
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS portfolio_runs (id TEXT PRIMARY KEY, checkpoint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS portfolio_frames (
                run_id TEXT NOT NULL, at REAL NOT NULL, input TEXT NOT NULL, events TEXT NOT NULL,
                PRIMARY KEY(run_id,at));
        ''')

    def close(self):
        self.db.close()

    def checkpoint(self, run_id):
        row = self.db.execute('SELECT checkpoint FROM portfolio_runs WHERE id=?', (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def advance(self, run_id, batch, configuration=None):
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError('A replay run ID is required')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            checkpoint = self.checkpoint(run_id)
            engine = PortfolioEngine(configuration)
            if checkpoint is not None:
                if checkpoint['configuration'] != engine.cfg:
                    raise ValueError('Replay configuration changed')
                engine = PortfolioEngine.restore(checkpoint)
            if engine.state['last_at'] is not None and batch['at'] <= engine.state['last_at']:
                previous = self.db.execute('SELECT input FROM portfolio_frames WHERE run_id=? AND at=?',
                                           (run_id, batch['at'])).fetchone()
                if previous is None or previous[0] != encode(batch):
                    raise ValueError('Replay clock moved backwards or a recorded batch changed')
                return []
            events = engine.step(batch)
            self.db.execute('INSERT INTO portfolio_frames VALUES (?,?,?,?)',
                            (run_id, batch['at'], encode(batch), encode(events)))
            self.db.execute('INSERT OR REPLACE INTO portfolio_runs VALUES (?,?)', (run_id, encode(engine.checkpoint())))
        return events
