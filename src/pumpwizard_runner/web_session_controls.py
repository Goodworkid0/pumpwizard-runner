"""Wallet-scoped session controls and display-only live trade performance."""
from decimal import Decimal, InvalidOperation
import time

from .transfers import GENESIS, TransferError
from .workspace_scope import read_scope, workspace_matches


def owned_workspace(library, workspace_id, query, authorizer):
    workspace = library.session_workspace(workspace_id)
    scope = read_scope(query or {})
    if scope is None or not workspace_matches(workspace, scope):
        raise ValueError('Select a session in the current wallet workspace')
    if scope[0]:
        if authorizer is None:
            raise TransferError('Wallet sign-in is unavailable')
        authorizer.require(*scope)
    return workspace


def live_positions(workspace, instance_ids, trade_state):
    return [p for p in trade_state.get('positions', [])
            if p.get('agent_id') in instance_ids and p.get('sender') == workspace.get('wallet_sender')
            and p.get('genesis') == GENESIS.get(workspace.get('wallet_network'))]


def position_summary(library, workspace, history_ids, trade_state):
    """Recorded session ownership includes removed strategies and manual exits."""
    wallet_positions = [p for p in trade_state.get('positions', [])
                        if p.get('sender') == workspace.get('wallet_sender')
                        and p.get('genesis') == GENESIS.get(workspace.get('wallet_network'))
                        and p.get('status') == 'open']
    owned = [p for p in wallet_positions if p.get('agent_id') in history_ids]
    managed = sum(bool(p.get('managed')) for p in owned)
    ids = library.session_workspace_instance_ids(workspace['id'])
    paused = sum(library.instance(ident)['entry_mode'] == 'disabled' for ident in ids)
    automatic = sum(library.instance(ident)['exit_mode'] == 'automatic' for ident in ids)
    return dict(open_positions=len(owned), managed_positions=managed,
                manual_positions=len(owned) - managed,
                other_wallet_positions=len(wallet_positions) - len(owned),
                entries_paused=bool(ids) and paused == len(ids),
                paused_entry_strategies=paused, automatic_exit_strategies=automatic,
                pending=bool(trade_state.get('pending')))


def performance(library, workspace, instance_ids, trade_state, *, now=None):
    """Combine all receipt-backed realized results with fresh retained SOL marks."""
    if workspace['mode'] != 'trade':
        return dict(state='not_applicable', total_lamports=None, realized_lamports=None,
                    unrealized_lamports=None, fees_lamports=None, open_positions=0, closed_positions=0,
                    detail='Live trading P&L is shown for trading sessions.')
    now = time.time() if now is None else now
    positions = live_positions(workspace, instance_ids, trade_state)
    fees_known = all(p.get('transaction_fees') is not None for p in positions)
    fees = sum(int(p['transaction_fees']) for p in positions if p.get('transaction_fees') is not None)
    realized, unrealized = 0, 0
    realized_known, marks_known = True, True
    open_count = 0
    for position in positions:
        if position.get('realized_pnl') is None:
            realized_known = False
        else:
            realized += int(position['realized_proceeds']) - int(position['realized_cost'])
        if position['status'] != 'open':
            continue
        open_count += 1
        try:
            observation = library.latest_observation(position['agent_id'], position['mint'])
        except ValueError:
            observation = {}
        if not observation:
            retained = library.coin_history(position['mint'], 1)
            observation = retained[0] if retained else {}
        try:
            age = now - float(observation['observed_at'])
            if not 0 <= age <= 120:
                raise ValueError('stale mark')
            native = observation.get('price_native')
            if native is None:
                fx = Decimal(str(observation['quote_usd']))
                if not fx.is_finite() or fx <= 0:
                    raise ValueError('invalid FX')
                native = Decimal(str(observation['price_usd'])) / fx
            native = Decimal(str(native))
            decimals = position['decimals']
            if not isinstance(decimals, int) or not 0 <= decimals <= 255 or not native.is_finite() or native <= 0:
                raise ValueError('invalid mark')
            value = native * Decimal(position['quantity']) / (Decimal(10) ** decimals) * 10**9
            unrealized += int(value) - int(position['remaining_cost'])
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            marks_known = False
    position_ids = {p['id'] for p in positions}
    for order in trade_state.get('orders', []):
        if order.get('agent_id') not in instance_ids and order.get('position_id') not in position_ids:
            continue
        if order['status'] == 'failed':
            receipt = order.get('receipt')
            if not receipt or receipt.get('fee_lamports') is None:
                realized_known = False
                fees_known = False
            else:
                realized -= int(receipt['fee_lamports'])
            # Position fee totals already include failed sell receipts.
            if order.get('position_id') not in position_ids:
                if not receipt or receipt.get('fee_lamports') is None:
                    fees_known = False
                else:
                    fees += int(receipt['fee_lamports'])
    complete = realized_known and marks_known
    return dict(state='estimated' if complete else 'unavailable',
                total_lamports=str(realized + unrealized) if complete else None,
                realized_lamports=str(realized) if realized_known else None,
                unrealized_lamports=str(unrealized) if marks_known else None,
                fees_lamports=str(fees) if fees_known else None,
                open_positions=open_count,
                closed_positions=sum(p['status'] == 'closed' for p in positions),
                detail=('All-time realized plus unrealized live trade P&L; includes recorded fees, before future sell costs.'
                        if complete else 'Missing receipt basis or a fresh SOL price. No complete P&L total is available.'))


def pause(library, workspace, *, main_wallet_controller=None, background_service=None):
    """Stop all automatic orders and scans in this session; retain positions/funds."""
    from .manual_trades import TradeJournal
    from .agent_execution import cancel
    ids = library.session_workspace_instance_ids(workspace['id'])
    sender, network = workspace.get('wallet_sender'), workspace.get('wallet_network')
    if sender and background_service and background_service.signer:
        signer = background_service.signer
        current = signer.state()
        if (current.get('sender'), current.get('network')) == (sender, network) and current.get('session_id') in ids:
            signer.lock('Session paused from dashboard')
    if sender and main_wallet_controller:
        for ident in ids:
            for allowance in main_wallet_controller.status(sender, network, ident)['allowances']:
                if allowance['status'] in ('active', 'authorized', 'entry-paused'):
                    main_wallet_controller.lock(library, sender=sender, network=network, allowance_id=allowance['id'])
    journal = TradeJournal(library.path.parent)
    try:
        tables = {r[0] for r in journal.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'agent_orders' in tables:
            for ident in ids:
                for row in journal.db.execute("SELECT id FROM agent_orders WHERE agent_id=? AND status='review'", (ident,)).fetchall():
                    cancel(journal, row['id'])
        library.pause_workspace(workspace['id'])
    finally:
        journal.close()
    return dict(session_id=workspace['id'], paused_strategies=len(ids),
                notice='All session strategies paused. Monitoring and automatic buys/sells are off. Positions and allocated funds remain held; submitted transactions still settle.')
