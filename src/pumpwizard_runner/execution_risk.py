"""Net exit evidence from unsigned executable quotes; no signing capability."""
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time

from .manual_trades import PREVIEW_TTL, encode
from .portfolio import CONFIG
from .pump_route import MAX_ACCOUNT_FUNDING
from .transfers import TransferError

VERSION = 'executable-net-exit-v1'


def net_values(preview):
    if preview['side'] != 'sell' or not 0 <= time.time()-preview['created_at'] <= PREVIEW_TTL:
        raise TransferError('A fresh executable sell quote is required for exit risk')
    # Expected wallet delta already contains actual simulated fee/account costs.
    # The enforceable receipt bound additionally allows worst-case account funding.
    floor = preview['minimum_output']-preview['fee']-MAX_ACCOUNT_FUNDING
    if floor <= 0:
        raise TransferError('Executable net exit floor is not positive after slippage, fee and account funding')
    return dict(expected_net_lamports=preview['expected_wallet_delta'],net_floor_lamports=floor,
                fee_lamports=preview['fee'],account_funding_allowance=MAX_ACCOUNT_FUNDING,
                minimum_route_output=preview['minimum_output'],quantity_atoms=preview['amount'],
                route=preview['route'],slot=preview['state']['slot'],quoted_at=preview['created_at'])


class RiskStore:
    """Separate evidence ledger; never contains wallet holds, orders or fills."""
    def __init__(self, data_dir):
        Path(data_dir).mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(Path(data_dir)/'agent-risk.sqlite',timeout=10)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS net_exit_peaks (
                key TEXT PRIMARY KEY, peak TEXT NOT NULL, last_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS shadow_reports (
                id INTEGER PRIMARY KEY, at REAL NOT NULL, agent_id TEXT NOT NULL, payload TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def assess(self, preview, position, owner, action, evidence, *, stop_loss_fraction=None):
        values=net_values(preview)
        now=time.time()
        if preview['amount'] != int(position['quantity']):
            raise TransferError('Exit risk requires a quote for the full remaining position')
        fx=Decimal(str(action['quote_usd']))
        value=Decimal(values['net_floor_lamports'])/10**9*fx
        cost=Decimal(position['remaining_cost'])/10**9*Decimal(str(owner['entry_fx']))
        unit=value/preview['amount']
        key=encode([VERSION,preview['genesis'],preview['sender'],owner['agent_id'],position['id'],preview['slippage_bps']])
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            previous=self.db.execute('SELECT peak,last_at FROM net_exit_peaks WHERE key=?',(key,)).fetchone()
            if previous and preview['created_at'] < previous['last_at']:
                raise TransferError('A newer executable exit assessment already exists; refresh this quote')
            peak=max(unit,Decimal(previous['peak']) if previous else unit)
            self.db.execute('INSERT OR REPLACE INTO net_exit_peaks VALUES (?,?,?)',(key,str(peak),preview['created_at']))
        risk=(evidence or {}).get('ownership') or {}
        reason=None
        default_stop = Decimal(str(CONFIG['stop_loss_fraction']))
        effective_stop = (
            default_stop if stop_loss_fraction is None
            else max(default_stop, Decimal(str(stop_loss_fraction)))
        )
        if risk.get('mint')==position['mint'] and risk.get('status')=='blocked':
            reason='ownership_flag'
        elif now-position['opened_at'] >= CONFIG['max_hold_seconds']:
            reason='holding_limit'
        elif cost > 0 and value/cost-1 <= effective_stop:
            reason='stop_loss'
        elif json.loads(owner['sold_stages']) and unit <= peak*(1-Decimal(str(CONFIG['trailing_drawdown_fraction']))):
            reason='trailing_exit'
        stages=[]
        if reason is None:
            stages=[i for i,t in enumerate(CONFIG['targets']) if i not in json.loads(owner['sold_stages'])
                    and action['market_cap_usd'] >= t['market_cap_usd']]
            if stages:
                reason='market_cap_target'
        fraction=sum((Decimal(str(CONFIG['targets'][i]['initial_fraction'])) for i in stages),Decimal(0)) if stages else Decimal(1)
        amount=min(int(position['quantity']),int(Decimal(position['initial_quantity'])*fraction))
        return dict(version=VERSION,**values,quote_usd=str(fx),remaining_cost_usd=str(cost),
                    net_floor_usd=str(value),net_usd_per_atom=str(unit),peak_net_usd_per_atom=str(peak),
                    reason=reason,stages=stages,amount=amount)

    def record(self,agent_id,report):
        with self.db:
            cursor=self.db.execute('INSERT INTO shadow_reports(at,agent_id,payload) VALUES (?,?,?)',
                                   (time.time(),agent_id,encode(report)))
        return cursor.lastrowid

    def reports(self,agent_id,limit=30):
        return [dict(id=r['id'],at=r['at'],report=json.loads(r['payload'])) for r in self.db.execute(
            'SELECT * FROM shadow_reports WHERE agent_id=? ORDER BY id DESC LIMIT ?',(agent_id,limit))]
