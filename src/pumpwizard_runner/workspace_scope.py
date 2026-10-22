"""Wallet/network workspace selection; unowned legacy records remain in Research."""
from .transfers import GENESIS
from .web_strategy_controls import _identity


def read_scope(query):
    if 'sender' in query or 'network' in query:
        return _identity(query.get('sender', [''])[0], query.get('network', [''])[0])
    return ('', '') if query.get('scope') == ['research'] else None


def chat_scope(query):
    scope = read_scope(query or {})
    return ':'.join(scope) if scope and scope[0] else ''


def workspace_matches(workspace, scope):
    return scope is None or (workspace.get('wallet_sender') or '', workspace.get('wallet_network') or '') == scope


def scoped_instance_ids(library, scope):
    if scope is None:
        return None
    from .manual_trades import TradeJournal
    journal = TradeJournal(library.path.parent)
    try:
        policies = list(journal.db.execute('SELECT agent_id,genesis,sender FROM strategy_risk_policies'))
    finally:
        journal.close()
    owners = {}
    for row in policies:
        owners.setdefault(row['agent_id'], set()).add((row['sender'], row['genesis']))
    workspaces = {row['id']: row for row in library.session_workspaces()}
    result = set()
    for instance in library.instances():
        workspace = workspaces.get(instance.get('workspace_id'))
        if workspace is not None:
            matches = workspace_matches(workspace, scope)
        else:
            identities = owners.get(instance['id'], set())
            matches = (scope[0], GENESIS.get(scope[1])) in identities if scope[0] else not identities
        if matches:
            result.add(instance['id'])
    return result


def instance_owner(library, instance):
    """Resolve a retained owner without guessing for ambiguous legacy instances."""
    if instance.get('workspace_id'):
        workspace = library.session_workspace(instance['workspace_id'])
        if workspace.get('wallet_sender'):
            return workspace['wallet_sender'], workspace['wallet_network']
        return None
    from .manual_trades import TradeJournal
    journal = TradeJournal(library.path.parent)
    try:
        owners = {(row['sender'], row['genesis']) for row in journal.db.execute(
            'SELECT sender,genesis FROM strategy_risk_policies WHERE agent_id=?', (instance['id'],),
        )}
    finally:
        journal.close()
    if len(owners) == 1:
        sender, genesis = next(iter(owners))
        network = next((name for name, value in GENESIS.items() if value == genesis), None)
        if network:
            return sender, network
    return None


def strategy_digests(library, scope):
    """Preserve known legacy ownership and keep unassigned packages in Research."""
    if scope is None:
        return None
    from .manual_trades import TradeJournal
    journal = TradeJournal(library.path.parent)
    try:
        policies = list(journal.db.execute('SELECT agent_id,sender,genesis FROM strategy_risk_policies'))
    finally:
        journal.close()
    networks = {genesis: network for network, genesis in GENESIS.items()}
    with library._connection as db:
        db.execute("""INSERT OR IGNORE INTO strategy_wallets
            SELECT i.strategy_digest,w.wallet_sender,w.wallet_network FROM instances i
            JOIN session_workspace_instances a ON a.instance_id=i.id
            JOIN session_workspaces w ON w.id=a.workspace_id WHERE w.wallet_sender IS NOT NULL""")
        for row in policies:
            if row['genesis'] in networks:
                db.execute("""INSERT OR IGNORE INTO strategy_wallets
                    SELECT strategy_digest,?,? FROM instances WHERE id=?""",
                    (row['sender'], networks[row['genesis']], row['agent_id']))
        db.execute("""INSERT OR IGNORE INTO strategy_wallets
            SELECT digest,'','' FROM strategies s WHERE NOT EXISTS (
                SELECT 1 FROM strategy_wallets w WHERE w.digest=s.digest)""")
    return {row[0] for row in library._connection.execute(
        'SELECT digest FROM strategy_wallets WHERE sender=? AND network=?', scope,
    )}


def require_strategy(library, digest, scope):
    visible = strategy_digests(library, scope)
    if visible is not None and digest not in visible:
        raise ValueError('Import this strategy into the selected wallet first')
