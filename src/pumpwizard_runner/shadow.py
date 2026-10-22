"""Read-only agent assessment with public RPC reads and unsigned simulation only."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import time

from solders.transaction import Transaction

from .agent_execution import AgentTradeService
from .execution_risk import RiskStore
from .library import StrategyLibrary, DATABASE_NAME, LEGACY_DATABASE_NAME, SCHEMA_VERSION
from .manual_trades import ManualTradeService, TradeJournal
from .route_check import RouteCheckRpc
from .transfers import TransferError
from .strategies import content_hash, parse_package
from .strategy_support import strategy_identity


def observation_evidence(service, agent_id, mint):
    """Record local observation provenance; live RPC reports are not offline replay inputs."""
    try:
        observation = service.library.latest_observation(agent_id, mint)
    except ValueError:
        observation = None
    return dict(observation=observation, observation_sha256=content_hash(observation))


class UnsignedRpc:
    """Constrain even injected transports, so tests and production share the boundary."""
    allowed_methods=RouteCheckRpc.allowed_methods

    def __init__(self,rpc):
        self._rpc=rpc
        self.network=rpc.network
        self.calls=[]

    def genesis(self):
        # Production transport verifies the selected network's genesis.
        return self._rpc.genesis()

    def call(self,method,params):
        if method not in self.allowed_methods:
            raise TransferError('Shadow mode permits public reads and unsigned simulation only')
        if method=='simulateTransaction':
            tx=Transaction.from_bytes(base64.b64decode(params[0],validate=True))
            if any(bytes(s)!=bytes(64) for s in tx.signatures) or params[1].get('sigVerify') is not False:
                raise TransferError('Shadow mode rejects signed transactions')
        result=self._rpc.call(method,params)
        event={'method':method}
        if method in ('simulateTransaction','getMultipleAccounts'):
            event['slot']=result.get('context',{}).get('slot')
        if method=='simulateTransaction':
            event.update(err=result.get('value',{}).get('err'),units_consumed=result.get('value',{}).get('unitsConsumed'))
        self.calls.append(event)
        return result


def _readonly(path):
    if not path.exists():
        raise TransferError('Shadow mode needs an existing local strategy and wallet journal')
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


class UnsignedPreviewService(ManualTradeService):
    def submit(self,*args,**kwargs):
        raise TransferError('Shadow mode cannot sign or submit')


class ShadowAgentService(AgentTradeService):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.manual=UnsignedPreviewService(self.rpc,self.journal)

    def prepare(self,*args,**kwargs):
        raise TransferError('Shadow mode cannot create reviewed orders')

    def submit(self,*args,**kwargs):
        raise TransferError('Shadow mode cannot sign or submit')


@contextmanager
def shadow_context(data_dir,rpc):
    directory=Path(data_dir)
    library=StrategyLibrary.__new__(StrategyLibrary)
    legacy=directory/LEGACY_DATABASE_NAME
    library.path=legacy if legacy.exists() else directory/DATABASE_NAME
    library._connection=_readonly(library.path)
    journal=None
    store=None
    try:
        tables={r[0] for r in library._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version=library._connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if 'meta' in tables else None
        if version is None or version[0] != str(SCHEMA_VERSION):
            raise TransferError('Open the current app once to upgrade its strategy library before shadow assessment')
        journal=TradeJournal.__new__(TradeJournal)
        journal.db=_readonly(directory/'wallet-transfers.sqlite')
        required={'agent_orders','agent_positions','agent_reservations','wallet_revisions','trade_positions'}
        tables={r[0] for r in journal.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables:
            raise TransferError('Open the current app once to upgrade its journal before shadow assessment')
        store=RiskStore(directory)
        yield ShadowAgentService(library,UnsignedRpc(rpc),journal,risk_store=store),store
    finally:
        if store is not None:
            store.close()
        if journal is not None and hasattr(journal,'db'):
            journal.close()
        library.close()


def run_shadow(data_dir,rpc,agent_id,sender,cash_hold,*,mint=None,position_id=None,slippage_bps=100,limit=10):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise TransferError('Shadow candidate limit must be between 1 and 20')
    if position_id and not mint:
        raise TransferError('Provide a mint when selecting a position')
    with shadow_context(data_dir,rpc) as (service,store):
        instance = service.library.instance(agent_id)
        if mint:
            candidates=[(mint,position_id)]
        else:
            # Prioritize every selected held position before entry candidates.
            positions=service.journal.db.execute('''SELECT p.mint,p.id FROM agent_positions a
                JOIN trade_positions p ON p.id=a.position_id
                WHERE a.agent_id=? AND p.sender=? AND p.status='open' ORDER BY p.opened_at,p.id''',(agent_id,sender)).fetchall()
            candidates=[(r['mint'],r['id']) for r in positions]
            held={r['mint'] for r in positions}
            seen=set()
            for d in service.library.decisions(agent_id,1000):
                if d['mint'] in seen:
                    continue
                seen.add(d['mint'])
                if d['mint'] not in held and d['kind']=='entry_recommended':
                    candidates.append((d['mint'],None))
        report=dict(schema='solanamarket.agent-shadow/v1',mode='read-only-shadow',agent_id=agent_id,
                    sender=sender,network=rpc.network,at=time.time(),submission_enabled=False,
                    assessment_scope='independent_candidates',
                    candidate_count=len(candidates),deferred=max(0,len(candidates)-limit),results=[])
        report['identity'] = strategy_identity(parse_package(instance['package']))
        report['risk_policy'] = 'executable-net-exit-v1'
        report['slippage_bps'] = slippage_bps
        report['cash_hold'] = cash_hold
        for token,position in candidates[:limit]:
            start=len(service.rpc.calls)
            item=dict(mint=token,position_id=position,
                      local_input=observation_evidence(service,agent_id,token))
            try:
                result=service.candidate(agent_id,sender,token,cash_hold,position_id=position,slippage_bps=slippage_bps)
                p=result['preview']
                item.update(status=result['status'],reason=result['reason'],risk=result['risk'],
                            proposal={k:p[k] for k in ('network','genesis','sender','mint','side','amount','route','slippage_bps',
                                                     'minimum_output','fee','max_wallet_debit','expected_wallet_delta','created_at')},
                            message_sha256=hashlib.sha256(base64.b64decode(p['message'])).hexdigest())
            except (TransferError,ValueError,KeyError,TypeError) as exc:
                item.update(status='blocked',reason=str(exc))
            item['rpc_evidence']=service.rpc.calls[start:]
            report['results'].append(item)
        report['finished_at']=time.time()
        report['report_id']=store.record(agent_id,report)
        return report
