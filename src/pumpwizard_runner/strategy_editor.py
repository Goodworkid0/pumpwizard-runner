"""Build exact edited packages and resolve allocation-based sizing."""
from .strategies import parse_package


def allocation_sizing(library, scope, workspace_id, trade_percent, market_source):
    """Resolve setup sizing from the durable allocation, not a client USD estimate."""
    from decimal import Decimal, InvalidOperation
    import math
    from .manual_trades import TradeJournal
    from .session_allocations import allocation
    from .transfers import GENESIS, sol_text
    from .workspace_scope import workspace_matches

    if not isinstance(workspace_id, str) or not workspace_id:
        raise ValueError('Choose a trading session before sizing its strategy.')
    workspace = library.session_workspace(workspace_id)
    if not scope or not scope[0] or not workspace_matches(workspace, scope) or workspace['mode'] != 'trade':
        raise ValueError('Choose a trading session owned by this wallet before sizing its strategy.')
    try:
        percent = Decimal(str(trade_percent))
    except InvalidOperation as exc:
        raise ValueError('Trade size must be a percentage greater than zero and at most 100.') from exc
    if not percent.is_finite() or not 0 < percent <= 100:
        raise ValueError('Trade size must be a percentage greater than zero and at most 100.')
    journal = TradeJournal(library.path.parent)
    try:
        held = allocation(journal, workspace_id)
        if not held or held['status'] != 'held' or (held['sender'], held['genesis']) != (scope[0], GENESIS[scope[1]]):
            raise ValueError('This session no longer has an allocated wallet budget.')
        lamports = int(held['allocation_lamports'])
    finally:
        journal.close()
    trade = int(Decimal(lamports) * percent / 100)
    if trade < 1:
        raise ValueError('This percentage is smaller than one lamport. Increase the trade size.')
    try:
        price = float(market_source.sol_usd_price())
        if not math.isfinite(price) or price <= 0:
            raise ValueError('invalid price')
    except Exception as exc:
        raise ValueError('Current SOL/USD is unavailable. Refresh sizing before saving; no template budget will be substituted.') from exc
    return dict(workspace_id=workspace_id, allocation_lamports=str(lamports),
                budget_sol=sol_text(lamports), trade_budget_sol=sol_text(trade),
                trade_percent=str(percent), sol_usd_price=price,
                budget_usd=float(Decimal(lamports) / 10**9 * Decimal(str(price))),
                order_usd=float(Decimal(trade) / 10**9 * Decimal(str(price))))


def revised_package(source, body):
    if set(body) != {'label', 'configuration'} or not isinstance(body['configuration'], dict):
        raise ValueError('Supply a strategy name and all configuration fields.')
    original = parse_package(source)
    if original.profile != 'ladder-v1':
        raise ValueError('This editor requires a supported early-ladder strategy. Export other formats for editing.')
    config = body['configuration']
    for field in ('version', 'mode'):
        if config.get(field) != source['configuration'][field]:
            raise ValueError('The strategy format and research mode cannot be changed by editing rules.')
    # Do not copy source simulation reports, trial overlays or attributed results
    # onto different rules. The digest itself is the revision identity.
    package = {'schema': source['schema'],
               'strategy': {'id': original.strategy_id, 'label': body['label']},
               'configuration': config}
    strategy = parse_package(package)
    if strategy.compatibility != 'monitoring-ready':
        raise ValueError('Check the rules: minimum market cap must be below maximum; budget must cover trade size; '
                         'counts must be positive whole numbers; percentages must be in range; expiry must exceed fill delay; '
                         'exit targets must increase above entry cap and sell no more than 100% in total.')
    return strategy
