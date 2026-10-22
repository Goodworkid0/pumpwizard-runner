"""Explicitly reviewed strategy orders. Replay fills never enter this module."""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
import sqlite3
import time
import uuid

from .decision import DecisionEngine, MarketQuote, PositionView
from .manual_trades import ManualTradeService, encode
from .portfolio import CONFIG
from .strategy_support import reviewed_eligible
from .pump_route import MAX_ACCOUNT_FUNDING, MAX_FEE
from .strategies import parse_package
from .transfers import TransferError, sol_text

SCHEMA = '''
CREATE TABLE IF NOT EXISTS agent_orders (
    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, mint TEXT NOT NULL, side TEXT NOT NULL,
    cash_hold TEXT NOT NULL, token_hold TEXT, status TEXT NOT NULL,
    context TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_cash_order ON agent_orders(cash_hold)
    WHERE status IN ('review','submitted');
CREATE UNIQUE INDEX IF NOT EXISTS agent_token_order ON agent_orders(token_hold)
    WHERE status IN ('review','submitted');
CREATE UNIQUE INDEX IF NOT EXISTS agent_mint_entry ON agent_orders(agent_id,mint)
    WHERE side='buy' AND status IN ('review','submitted','finalized','failed');
CREATE TABLE IF NOT EXISTS agent_positions (
    position_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, token_hold TEXT NOT NULL UNIQUE,
    entry_fx REAL NOT NULL, sold_stages TEXT NOT NULL,
    peak_price_usd REAL NOT NULL, last_usable_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_fills (
    trade_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, position_id TEXT,
    receipt TEXT NOT NULL
);
'''


def hold(journal, ident, p):
    from .session_allocations import cash_owner

    row = journal.db.execute('SELECT * FROM agent_reservations WHERE id=?', (ident,)).fetchone()
    expected_owner = cash_owner(
        journal, p['agent_order']['agent_id'], p['genesis'], p['sender']
    ) if row is not None and row['position_id'] is None else p['agent_order']['agent_id']
    if row is None or (row['agent_id'], row['genesis'], row['sender'], row['status']) != (
            expected_owner, p['genesis'], p['sender'], 'held'):
        raise TransferError('Agent reservation is missing, released or belongs to another wallet, network or agent')
    return dict(row)


def covered_resources(journal, p, state):
    """Keep whole holds counted while allowing this order to use its own units."""
    ctx = p['agent_order']
    if p['side']=='buy':
        curve = state.get('curve_reserves') or {}
        amm = state.get('amm_reserves') or {}
        quote_usd = Decimal(str(ctx['quote_usd']))
        if curve:
            base = curve.get('virtual_token_reserves', 0)
            if base <= 0:
                raise TransferError('Fresh curve reserves are required for agent entry')
            cap = (Decimal(curve['virtual_quote_reserves'])
                   * curve['token_total_supply'] / base / 10**9 * quote_usd)
            liquidity = Decimal(curve['real_quote_reserves']) / 10**9 * quote_usd
            minimum_liquidity = CONFIG['min_curve_liquidity_usd']
            venue = 'curve'
        elif amm:
            base = amm.get('base_reserves', 0)
            effective_quote = amm.get('effective_quote_reserves', 0)
            supply = amm.get('token_total_supply', 0)
            if min(base, effective_quote, supply) <= 0:
                raise TransferError('Fresh effective PumpSwap reserves are required for agent entry')
            cap = Decimal(effective_quote) * supply / base / 10**9 * quote_usd
            liquidity = Decimal(amm['quote_reserves']) / 10**9 * quote_usd
            minimum_liquidity = CONFIG['min_amm_liquidity_usd']
            venue = 'PumpSwap'
        else:
            raise TransferError('Fresh executable reserves are required for agent entry')
        if (not CONFIG['entry_cap_min'] <= cap <= CONFIG['entry_cap_max']
                or liquidity < minimum_liquidity):
            raise TransferError(
                f'Fresh on-chain {venue} is outside the strategy entry cap or liquidity limits'
            )
    cash = hold(journal, ctx['cash_hold'], p)
    debit = p['fee'] + MAX_ACCOUNT_FUNDING + (p['amount'] if p['side']=='buy' else 0)
    if cash['position_id'] is not None or int(cash['lamports']) < debit:
        raise TransferError('Use a dedicated SOL reservation covering spend, fee and account funding')
    if p['side'] == 'sell':
        tokens = hold(journal, ctx['token_hold'], p)
        owner = journal.db.execute('SELECT * FROM agent_positions WHERE position_id=?', (p['position_id'],)).fetchone()
        if (tokens['position_id'], tokens['mint']) != (p['position_id'], p['mint']) or int(tokens['token_atoms']) < p['amount']:
            raise TransferError('Agent token reservation does not cover this exit')
        if owner is None or owner['agent_id'] != ctx['agent_id'] or owner['token_hold'] != tokens['id']:
            raise TransferError('Exit position is not allocated to this agent')
    return 0, 0  # debit/sell already fully included in the reserved totals


def check_binding(journal, p):
    row = journal.db.execute('SELECT * FROM agent_orders WHERE id=?', (p['id'],)).fetchone()
    if row is None or row['status'] != 'review' or json.loads(row['context']) != p['agent_order']:
        raise TransferError('Agent order is cancelled, already submitted or changed; prepare a fresh review')
    covered_resources(journal, p,p['state'])


def settle_agent(journal, row, receipt):
    """Called inside the trade/position finality transaction, once only."""
    p, ctx = row['payload'], row['payload'].get('agent_order')
    if ctx is None:
        if p['side'] == 'sell':
            owner = journal.db.execute('SELECT agent_id FROM agent_positions WHERE position_id=?', (p['position_id'],)).fetchone()
            if owner:
                journal.db.execute('INSERT INTO agent_fills VALUES (?,?,?,?)',
                                   (row['id'], owner[0], p['position_id'], encode(receipt)))
        return
    cash = hold(journal, ctx['cash_hold'], p)
    remaining = int(cash['lamports']) + receipt['wallet_delta']
    if not 0 <= remaining < 2**64:
        raise TransferError('Agent cash reconciliation conflict; wallet remains reserved')
    journal.db.execute('UPDATE agent_reservations SET lamports=? WHERE id=?', (str(remaining), cash['id']))
    success = receipt['err'] is None
    position_id = p['position_id']
    if success and p['side'] == 'buy':
        position_id, token_hold = row['id'], uuid.uuid4().hex
        journal.db.execute('INSERT INTO agent_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (token_hold,ctx['agent_id'],p['genesis'],p['sender'],'0',position_id,p['mint'],
                            str(receipt['token_delta']),receipt['block_time'],None,'held'))
        journal.db.execute('INSERT INTO agent_positions VALUES (?,?,?,?,?,?,?)',
                           (position_id,ctx['agent_id'],token_hold,ctx['quote_usd'],'[]',ctx['price_usd'],receipt['block_time']))
    elif success:
        tokens = hold(journal,ctx['token_hold'],p)
        remaining_tokens = int(tokens['token_atoms']) + receipt['token_delta']
        if remaining_tokens < 0:
            raise TransferError('Agent token reconciliation conflict; wallet remains reserved')
        journal.db.execute('UPDATE agent_reservations SET token_atoms=? WHERE id=?', (str(remaining_tokens),tokens['id']))
        stages = json.loads(journal.db.execute('SELECT sold_stages FROM agent_positions WHERE position_id=?',
                                              (position_id,)).fetchone()[0])
        journal.db.execute('UPDATE agent_positions SET sold_stages=? WHERE position_id=?',
                           (encode(sorted(set(stages) | set(ctx['stages']))),position_id))
    journal.db.execute('INSERT INTO agent_fills VALUES (?,?,?,?)', (row['id'],ctx['agent_id'],position_id,encode(receipt)))
    journal.db.execute('UPDATE agent_orders SET status=? WHERE id=?', ('finalized' if success else 'failed',row['id']))


class AgentTradeService:
    def __init__(self, library, rpc, journal, *, risk_store=None):
        self.library, self.rpc, self.journal = library, rpc, journal
        self.risk_store = risk_store
        self.manual = ManualTradeService(rpc,journal,agent_authorizer=self.validate)

    def _action(self, agent_id, mint, position_id=None, *, exclude_review=None):
        instance = self.library.instance(agent_id)
        strategy = parse_package(instance['package'])
        if not reviewed_eligible(strategy):
            raise TransferError('Only the pinned core-frequency configuration supports reviewed agent orders')
        engine = DecisionEngine(strategy)
        now = time.time()
        engine.restore(self.library.recent_observations(agent_id,now-120))
        checkpoint = self.library.monitor_checkpoint(agent_id)
        if checkpoint:
            engine.restore_checkpoint(checkpoint)
        quote = MarketQuote.from_record(self.library.latest_observation(agent_id,mint))
        if not engine.quote_usable(quote,now) or quote.quote_usd <= 0:
            raise TransferError('A fresh local market observation and SOL/USD price are required')
        if position_id is None:
            if instance['status'] != 'monitoring' or instance['entry_mode'] == 'disabled':
                raise TransferError('Agent entries are paused or disabled')
            decision = self.library.latest_actionable_decision(agent_id,mint)
            if decision['kind'] != 'entry_recommended' or not 0 <= now-decision['at'] <= CONFIG['entry_expiry_seconds']:
                raise TransferError('No fresh entry recommendation remains')
            reason = engine.entry_reason(quote,{'peak_cap': engine.peak_caps.get(mint,quote.market_cap_usd)},now)
            if reason:
                raise TransferError(reason)
            if self.journal.db.execute("SELECT 1 FROM agent_orders WHERE agent_id=? AND mint=? AND side='buy' AND status IN ('submitted','finalized','failed')", (agent_id,mint)).fetchone():
                raise TransferError('This agent already attempted an entry for this mint')
            count = self.journal.db.execute(
                "SELECT COUNT(*) FROM agent_positions a "
                "JOIN trade_positions p ON p.id=a.position_id "
                "JOIN agent_reservations h ON h.id=a.token_hold "
                "WHERE a.agent_id=? AND p.status='open' AND h.status='held'",
                (agent_id,),
            ).fetchone()[0]
            count += self.journal.db.execute("SELECT COUNT(*) FROM agent_orders WHERE agent_id=? AND side='buy' AND status IN ('review','submitted') AND id!=?", (agent_id,exclude_review or '')).fetchone()[0]
            if count >= CONFIG['max_positions']:
                raise TransferError('Agent position capacity is reserved or full')
            return dict(side='buy',reason=decision['reason'],stages=[],decision_key=decision['decision_key'],
                        decision_at=decision['at'],quote_usd=quote.quote_usd,price_usd=quote.price_usd)
        p = self.journal.position(position_id)
        owner = self.journal.db.execute('SELECT * FROM agent_positions WHERE position_id=? AND agent_id=?', (position_id,agent_id)).fetchone()
        if owner is None or p['mint'] != mint or p['status'] != 'open':
            raise TransferError('Select an open finalized position belonging to this agent')
        return dict(side='sell',reason='net_exit_probe',stages=[],amount=int(p['quantity']),
                    decision_at=now,decision_key=hashlib.sha256(encode([position_id,p['quantity'],owner['sold_stages']]).encode()).hexdigest(),
                    quote_usd=quote.quote_usd,price_usd=quote.price_usd,market_cap_usd=quote.market_cap_usd,
                    last_usable_at=quote.observed_at,token_hold=owner['token_hold'])

    def candidate(self, agent_id, sender, mint, cash_hold, *, position_id=None, slippage_bps=100, exclude_review=None):
        """Unsigned assessment only: no preview, order, hold or fill is persisted."""
        from .execution_risk import RiskStore, net_values
        from .reservations import require_revision, wallet_revision

        action = self._action(agent_id,mint,position_id,exclude_review=exclude_review)
        if self.journal.db.execute("SELECT 1 FROM agent_orders WHERE (cash_hold=? OR (token_hold IS NOT NULL AND token_hold=?)) AND status IN ('review','submitted') AND id!=?",
                                  (cash_hold,action.get('token_hold'),exclude_review or '')).fetchone():
            raise TransferError('This reservation already has an agent order; cancel its unsigned review first')
        genesis=self.rpc.genesis()
        revision=wallet_revision(self.journal,genesis,sender)
        amount = action.get('amount')
        if amount is None:
            total = int((Decimal(str(CONFIG['order_usd']))/Decimal(str(action['quote_usd']))*10**9).to_integral_value(rounding=ROUND_FLOOR))
            amount = total - MAX_FEE - MAX_ACCOUNT_FUNDING
            if amount <= 0:
                raise TransferError('Strategy budget cannot cover conservative account funding and fee allowance')
        ctx = dict(action,agent_id=agent_id,cash_hold=cash_hold,token_hold=action.get('token_hold'))
        def quote(quantity,context):
            return self.manual.preview(sender,mint,action['side'],sol_text(quantity) if action['side']=='buy' else str(Decimal(quantity)/10**6),
                                       slippage_bps,position_id,agent_order=context,persist=False)
        p=quote(amount,ctx)
        from .strategy_risk import check_preview, stop_loss_fraction

        check_preview(self.journal, p)
        require_revision(self.journal,genesis,sender,revision)
        current=self._action(agent_id,mint,position_id,exclude_review=exclude_review)
        if any(current.get(k)!=action.get(k) for k in ('decision_key','quote_usd','market_cap_usd','amount')):
            raise TransferError('Local action changed during unsigned assessment; refresh')
        risk=None
        status='ready'
        if position_id:
            position=self.journal.position(position_id)
            owner=self.journal.db.execute('SELECT * FROM agent_positions WHERE position_id=?',(position_id,)).fetchone()
            store=self.risk_store or RiskStore(self.library.path.parent)
            try:
                risk=store.assess(
                    p, position, owner, action,
                    {'ownership': self.library.latest_ownership(agent_id, mint) or {}},
                    stop_loss_fraction=stop_loss_fraction(
                        self.journal, agent_id, p['genesis'], p['sender']
                    ),
                )
            finally:
                if self.risk_store is None:
                    store.close()
            if risk['reason'] is None:
                status='hold'
            else:
                ctx.update(reason=risk['reason'],stages=risk['stages'],amount=risk['amount'],net_risk=risk)
                if risk['amount'] != amount:
                    full=p
                    p=quote(risk['amount'],ctx)
                    if p['route']!=full['route'] or any(p['state'][k]!=full['state'][k] for k in ('wallet_lamports','token_atoms')):
                        raise TransferError('Route or wallet changed between full-position risk and partial-exit quotes')
                    ctx['partial_net']=net_values(p)
                p['agent_order']=ctx
                check_preview(self.journal, p)
        require_revision(self.journal,genesis,sender,revision)
        if p['genesis']!=genesis or not 0 <= time.time()-p['created_at'] <= 30:
            raise TransferError('Unsigned assessment expired or network changed')
        if risk is not None and not 0 <= time.time()-risk['quoted_at'] <= 30:
            raise TransferError('Full-position net risk quote expired during partial-exit assessment')
        # Recheck source freshness after all simulations, including partial exits.
        latest=self._action(agent_id,mint,position_id,exclude_review=exclude_review)
        if any(latest.get(k)!=action.get(k) for k in ('decision_key','quote_usd','market_cap_usd','amount')):
            raise TransferError('Local action changed during unsigned assessment; refresh')
        return dict(status=status,reason=ctx['reason'] if status=='ready' else 'No current executable-net exit trigger',risk=risk,preview=p)

    def prepare(self, agent_id, sender, mint, cash_hold, *, position_id=None, slippage_bps=100):
        assessment=self.candidate(agent_id,sender,mint,cash_hold,position_id=position_id,slippage_bps=slippage_bps)
        if assessment['status']!='ready':
            raise TransferError(assessment['reason'])
        p=assessment['preview']
        ctx=p['agent_order']
        try:
            with self.journal.db:
                self.journal.db.execute('BEGIN IMMEDIATE')
                self._action(agent_id,mint,position_id)
                from .strategy_risk import check_preview

                check_preview(self.journal, p)
                self.journal.check_agent_reservations(p,p['state'])
                self.journal.db.execute('INSERT INTO trade_previews VALUES (?,?)',(p['id'],encode(p)))
                self.journal.db.execute('INSERT INTO agent_orders VALUES (?,?,?,?,?,?,?,?,?)',
                                       (p['id'],agent_id,mint,p['side'],cash_hold,ctx['token_hold'],'review',encode(ctx),time.time()))
                if position_id:
                    self.journal.db.execute('UPDATE agent_positions SET last_usable_at=? WHERE position_id=?',
                                           (ctx['last_usable_at'],position_id))
        except sqlite3.IntegrityError:
            raise TransferError('This mint or reservation already has an agent order; cancel its unsigned review first') from None
        return p

    def validate(self,p):
        check_binding(self.journal,p)
        from .strategy_risk import check_preview

        check_preview(self.journal, p)
        ctx = p['agent_order']
        if p['side']=='sell':
            from .execution_risk import VERSION

            if (ctx.get('net_risk') or {}).get('version')!=VERSION:
                raise TransferError('Prepare a new review with executable net exit evidence')
            assessment=self.candidate(ctx['agent_id'],p['sender'],p['mint'],ctx['cash_hold'],position_id=p['position_id'],
                                      slippage_bps=p['slippage_bps'],exclude_review=p['id'])
            if assessment['status']!='ready':
                raise TransferError('Executable exit trigger changed; prepare a fresh review')
            action=assessment['preview']['agent_order']
            if assessment['preview']['route']!=p['route']:
                raise TransferError('Route changed after review; prepare a fresh quote')
        else:
            action = self._action(ctx['agent_id'],p['mint'],p['position_id'],exclude_review=p['id'])
        if action['side'] != p['side'] or action['reason'] != ctx['reason'] or action['stages'] != ctx['stages']:
            raise TransferError('Strategy action changed; prepare a fresh review')
        if p['side']=='sell' and action['amount'] != p['amount']:
            raise TransferError('Agent exit allocation changed')
        if p['side']=='buy':
            if action['decision_key'] != ctx['decision_key']:
                raise TransferError('Entry recommendation changed')
            if action['quote_usd'] != ctx['quote_usd']:
                raise TransferError('SOL/USD price changed; prepare a fresh budget review')
            total = Decimal(p['amount']+MAX_ACCOUNT_FUNDING+p['fee'])/10**9*Decimal(str(action['quote_usd']))
            if total > Decimal(str(CONFIG['order_usd'])):
                raise TransferError('Fresh SOL/USD price exceeds the reviewed strategy budget')

    def submit(self, ident, wallet):
        # Freeze pause/configuration/observations during the explicit submission.
        # Lock order is library, then wallet journal. Reconciliation needs only
        # the wallet journal, so it remains independent of monitoring state.
        with self.library._connection:
            self.library._connection.execute('BEGIN IMMEDIATE')
            return self.manual.submit(ident,wallet)

    def submit_signed(self, ident, sender, transaction):
        """Browser signatures use the same locked strategy revalidation."""
        with self.library._connection:
            self.library._connection.execute('BEGIN IMMEDIATE')
            return self.manual.submit_signed(ident, sender, transaction)


def cancel(journal, ident):
    with journal.db:
        journal.db.execute('BEGIN IMMEDIATE')
        row = journal.db.execute('SELECT status FROM agent_orders WHERE id=?',(ident,)).fetchone()
        if row is None or row[0] != 'review':
            raise TransferError('Only an unsigned agent review can be cancelled')
        journal.db.execute("UPDATE agent_orders SET status='cancelled' WHERE id=?",(ident,))


def owned_position_rows(library):
    """Read finalized holdings without creating/opening a vault or writable journal."""
    path = library.path.parent / 'wallet-transfers.sqlite'
    if not path.exists():
        return []
    db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    db.row_factory = sqlite3.Row
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_positions'").fetchone():
            return []
        return [dict(r) for r in db.execute('''SELECT p.*,a.agent_id,a.entry_fx,a.sold_stages,a.peak_price_usd,a.last_usable_at
            FROM agent_positions a JOIN trade_positions p ON p.id=a.position_id JOIN agent_reservations h ON h.id=a.token_hold WHERE p.status='open' AND h.status='held' ''')]
    finally:
        db.close()


def monitored_positions(library, agent_id):
    result = []
    for p in owned_position_rows(library):
        if p['agent_id'] != agent_id:
            continue
        records = [MarketQuote.from_record(r) for r in library.recent_observations(agent_id,p['opened_at'])
                   if r['mint']==p['mint']]
        now = time.time()
        usable = [q for q in records if q.route in ('curve','amm') and q.observed_at <= now and
                  all(-2 <= q.observed_at-t <= CONFIG['quote_max_age_seconds'] for t in (q.source_at,q.fx_at))]
        peak = max([p['peak_price_usd']] + [q.price_usd for q in usable])
        last = max([p['last_usable_at']] + [q.observed_at for q in usable])
        result.append(PositionView(p['id'],p['mint'],p['mint'][:8],p['opened_at'],int(p['initial_quantity'])/1e6,
                                   int(p['quantity'])/1e6,int(p['remaining_cost'])/1e9*p['entry_fx'],
                                   int(p['original_cost'])/1e9*p['entry_fx'],
                                   realized_proceeds_usd=int(p['realized_proceeds'])/1e9*p['entry_fx'],
                                   sold_stages=tuple(json.loads(p['sold_stages'])),peak_price_usd=peak,last_usable_at=last,
                                   valuation_basis='observed'))
    return result
