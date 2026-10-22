"""Wallet-authenticated browser reviews over the existing agent trade service."""
import json
import time

from . import web_trading
from .agent_execution import AgentTradeService, cancel, reviewed_eligible
from .manual_trades import TradeJournal, PREVIEW_TTL
from .portfolio import CONFIG
from .reservations import release
from .strategies import parse_package
from .strategy_risk import status
from .transfers import GENESIS, TransferError


def overview(library, sender, network):
    sender, network = web_trading._identity(sender, network)
    journal = TradeJournal(library.path.parent)
    try:
        sessions = []
        from .workspace_scope import scoped_instance_ids
        visible = scoped_instance_ids(library, (sender, network))
        for instance in library.instances():
            if instance['id'] not in visible:
                continue
            ident = instance['id']
            eligible = reviewed_eligible(parse_package(library.instance(ident)['package']))
            latest = {}
            for decision in library.decisions(ident, 100):
                latest.setdefault(decision['mint'], decision)
            decisions = [row for row in latest.values() if row['kind'] == 'entry_recommended'
                         and 0 <= time.time()-row['at'] <= CONFIG['entry_expiry_seconds']]
            reviews = []
            for row in journal.db.execute("SELECT o.id,p.payload FROM agent_orders o JOIN trade_previews p ON p.id=o.id WHERE agent_id=? AND status='review'", (ident,)):
                p = json.loads(row['payload'])
                if (p['sender'], p['genesis']) == (sender, GENESIS[network]):
                    reviews.append(dict(id=row['id'], mint=p['mint'], side=p['side'], expires_at=p['created_at']+PREVIEW_TTL))
            sessions.append(dict(id=ident, label=instance['label'], eligible=eligible,
                                 entry_mode=instance['entry_mode'], decisions=decisions, reviews=reviews,
                                 **status(journal, ident, GENESIS[network], sender)))
        return dict(sessions=sessions)
    finally:
        journal.close()


def command(library, action, body):
    sender, network = web_trading._identity(body.get('sender'), body.get('network'))
    identity = {'sender', 'network'}
    journal = TradeJournal(library.path.parent)
    try:
        if action == 'entries':
            if set(body) != identity | {'agent_id', 'enabled'} or type(body['enabled']) is not bool:
                raise TransferError('Choose a session and reviewed-entry preference')
            instance = library.instance(body['agent_id'])
            if body['enabled'] and not reviewed_eligible(parse_package(instance['package'])):
                raise TransferError('Only the pinned core-frequency configuration supports reviewed orders')
            if body['enabled']:
                from .session_funding import require_ready
                require_ready(library, instance['id'])
            with library._connection:
                library._connection.execute("UPDATE instances SET entry_mode=?,exit_mode='manual' WHERE id=?",
                                            ('manual' if body['enabled'] else 'disabled', instance['id']))
            return dict(saved=True)
        if action == 'preview':
            if set(body) != identity | {'agent_id', 'mint', 'position_id', 'slippage_bps'}:
                raise TransferError('Choose a strategy, mint, position and slippage')
            if type(body['slippage_bps']) is not int or not 1 <= body['slippage_bps'] <= 500:
                raise TransferError('Slippage must be 1–500 basis points')
            current = status(journal, body['agent_id'], GENESIS[network], sender)
            hold = current['metrics']['hold_id']
            if not hold:
                raise TransferError('Assign strategy funds and limits before reviewing an order')
            service = AgentTradeService(library, web_trading.rpc_for(network, library.path.parent), journal)
            service.manual.reconcile(sender)
            p = service.prepare(body['agent_id'], sender, body['mint'], hold,
                                position_id=body['position_id'], slippage_bps=body['slippage_bps'])
            return dict(preview=web_trading._preview_view(p) | {'agent_order': p['agent_order'],
                        'strategy_limits': current['policy']})
        if action in ('submit', 'cancel'):
            fields = identity | {'preview_id'} | ({'signed_transaction'} if action == 'submit' else set())
            if set(body) != fields:
                raise TransferError('Choose the original reviewed order')
            row = journal.db.execute('SELECT payload FROM trade_previews WHERE id=?', (body['preview_id'],)).fetchone()
            p = json.loads(row[0]) if row else {}
            if (p.get('sender'), p.get('genesis')) != (sender, GENESIS[network]) or not p.get('agent_order'):
                raise TransferError('Review does not belong to this wallet and network')
            if action == 'cancel':
                cancel(journal, body['preview_id'])
                return dict(cancelled=True)
            signature = AgentTradeService(library, web_trading.rpc_for(network, library.path.parent), journal).submit_signed(
                body['preview_id'], sender, body['signed_transaction'])
            row = next(row for row in journal.trades(sender) if row['signature'] == signature)
            return dict(signature=signature, state=row['status'], detail=row['detail'])
        if action == 'release-position':
            if set(body) != identity | {'position_id', 'acknowledge'} or body['acknowledge'] != 'release-managed-exit':
                raise TransferError('Acknowledge that strategy management stops for this position')
            position = journal.position(body['position_id'])
            if (position['sender'], position['genesis'], position['status']) != (sender, GENESIS[network], 'open'):
                raise TransferError('Choose your own open finalized position')
            row = journal.db.execute('SELECT token_hold FROM agent_positions WHERE position_id=?', (body['position_id'],)).fetchone()
            if row is None:
                raise TransferError('Position has no managed reservation')
            release(journal, row[0])
            return dict(released=True, position_id=body['position_id'])
        raise TransferError('Unknown strategy order action')
    finally:
        journal.close()
