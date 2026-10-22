"""Check real wallet SOL against reservations; never resume entries automatically."""
from contextlib import closing
import time

from .manual_trades import TradeJournal
from .reservations import wallet_revision
from .transfers import GENESIS, TransferError, TransferRpc, _uint


CHECK_INTERVAL = 15


def apply_entry_pause(library, db, workspace, detail):
    """Caller holds the library/journal transaction; exits retain their scope."""
    ids = library.session_workspace_instance_ids(workspace['id'])
    for ident in ids:
        if library.instance(ident)['entry_mode'] != 'disabled':
            db.execute("UPDATE instances SET entry_mode='disabled' WHERE id=?", (ident,))
            db.execute('UPDATE monitor_settings SET revision=revision+1,next_at=0 WHERE instance_id=?', (ident,))
        # Retire the previous full-stop funding fault without enabling a scan
        # or restoring an authorization that was already locked.
        db.execute("""UPDATE monitor_settings SET fault='' WHERE instance_id=?
            AND fault IN ('Insufficient wallet funds; session paused','Wallet funds restored; restart manually')""", (ident,))
        db.execute("UPDATE funding_wallet.agent_orders SET status='cancelled' WHERE agent_id=? AND side='buy' AND status='review'", (ident,))
        db.execute("""UPDATE funding_wallet.automation_authorizations
            SET entries_paused=1,detail=? WHERE status IN ('authorized','running','paused')
            AND EXISTS (SELECT 1 FROM json_each(scope,'$.strategies') WHERE key=?)""", (detail, ident))
        if db.execute("SELECT 1 FROM funding_wallet.sqlite_master WHERE type='table' AND name='main_wallet_allowances'").fetchone():
            db.execute("""UPDATE funding_wallet.main_wallet_allowances
                SET entries_paused=1,status=CASE WHEN status='active' THEN 'entry-paused' ELSE status END,detail=?
                WHERE session_id=? AND genesis=? AND sender=? AND status IN ('authorized','active','entry-paused')""",
                (detail, ident, GENESIS[workspace['wallet_network']], workspace['wallet_sender']))
    return ids


def pause_entries(library, workspace):
    """Pause every new entry in a funded workspace without stopping exits."""
    # Ensure the journal schema before the attached writer transaction.
    journal = TradeJournal(library.path.parent)
    journal.close()
    db = library._connection
    db.execute('ATTACH DATABASE ? AS funding_wallet', (str(library.path.parent / 'wallet-transfers.sqlite'),))
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            ids = apply_entry_pause(library, db, workspace, 'Entries paused by the user; existing exit settings remain unchanged.')
    finally:
        db.execute('DETACH DATABASE funding_wallet')
    return dict(session_id=workspace['id'], paused_strategies=len(ids),
                notice='New entries paused. Monitoring and existing exit settings are unchanged; positions remain held.')


def status(journal, sender, network):
    row = journal.db.execute(
        'SELECT * FROM session_wallet_funding WHERE genesis=? AND sender=?',
        (GENESIS[network], sender),
    ).fetchone()
    return dict(row) if row else dict(
        state='checking', balance_lamports=None, required_lamports='0',
        shortfall_lamports='0', checked_at=None, paused_at=None,
        detail='Waiting for the wallet funds check.',
    )


def check(library, sender, network, *, rpc=None):
    """Sample outside locks, then reject samples raced by local wallet writers.

    Pauses, authorizations and the displayed shortfall share one transaction
    across the library and journal. Submitted transactions keep their intents
    and reservations until normal receipt reconciliation finishes.
    """
    genesis = GENESIS[network]
    with closing(TradeJournal(library.path.parent)) as journal:
        revision = wallet_revision(journal, genesis, sender)
        pending = journal.pending(sender, genesis)
    balance = None
    state = 'pending' if pending else 'sufficient'
    detail = 'Waiting for the pending wallet transaction to settle.'
    if not pending:
        try:
            if rpc is None:
                from .web_trading import rpc_for
                rpc = rpc_for(network, library.path.parent)
                if isinstance(rpc, TransferRpc):
                    # Two bounded reads keep the dedicated watcher responsive
                    # to shutdown without changing the trading RPC timeout.
                    rpc.timeout = 2
            if rpc.genesis() != genesis:
                raise TransferError('Wallet balance network mismatch')
            balance = _uint(rpc.call('getBalance', [sender, {'commitment': 'confirmed'}])['value'], 'wallet balance')
            detail = 'Wallet balance covers the remaining reserved SOL.'
        except Exception:
            state = 'unavailable'
            detail = 'Wallet balance could not be verified. Retry before restarting.'

    db = library._connection
    db.execute('ATTACH DATABASE ? AS funding_wallet', (str(library.path.parent / 'wallet-transfers.sqlite'),))
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            current_revision = db.execute(
                'SELECT revision FROM funding_wallet.wallet_revisions WHERE genesis=? AND sender=?',
                (genesis, sender),
            ).fetchone()
            current_revision = int(current_revision[0]) if current_revision else 0
            if current_revision != revision or db.execute(
                'SELECT 1 FROM funding_wallet.wallet_intents WHERE genesis=? AND sender=?',
                (genesis, sender),
            ).fetchone():
                state, balance = 'pending', None
                detail = 'Wallet activity changed during the check. Waiting for settlement and a fresh check.'
            required = sum(_uint(int(row[0]), 'reserved SOL') for row in db.execute(
                "SELECT lamports FROM funding_wallet.agent_reservations WHERE genesis=? AND sender=? AND status='held'",
                (genesis, sender),
            ))
            shortfall = max(0, required - balance) if balance is not None else 0
            if state == 'sufficient' and shortfall:
                state = 'insufficient'
                detail = 'Insufficient wallet funds. New entries are paused for every trading session in this wallet. Existing exit settings are unchanged; sales still require enough SOL for fees. Add SOL or reduce allocations, then resume entries manually.'
                for workspace in library.session_workspaces():
                    if (workspace['mode'], workspace['wallet_sender'], workspace['wallet_network']) != ('trade', sender, network):
                        continue
                    apply_entry_pause(library, db, workspace, detail)
            now = time.time()
            db.execute("""INSERT INTO funding_wallet.session_wallet_funding
                (genesis,sender,state,balance_lamports,required_lamports,shortfall_lamports,checked_at,paused_at,detail)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(genesis,sender) DO UPDATE SET
                state=excluded.state,balance_lamports=excluded.balance_lamports,
                required_lamports=excluded.required_lamports,shortfall_lamports=excluded.shortfall_lamports,
                checked_at=excluded.checked_at,detail=excluded.detail,
                paused_at=coalesce(session_wallet_funding.paused_at,excluded.paused_at)""",
                (genesis, sender, state, str(balance) if balance is not None else None,
                 str(required), str(shortfall), now, now if shortfall else None, detail))
    finally:
        db.execute('DETACH DATABASE funding_wallet')
    with closing(TradeJournal(library.path.parent)) as journal:
        return status(journal, sender, network)


def require_ready(library, instance_id):
    """A fresh check gates resuming entries in funded sessions."""
    instance = library.instance(instance_id)
    workspace_id = instance.get('workspace_id')
    if not workspace_id:
        return
    workspace = library.session_workspace(workspace_id)
    if workspace['mode'] != 'trade' or not workspace['wallet_sender']:
        return
    with closing(TradeJournal(library.path.parent)) as journal:
        row = journal.db.execute("SELECT 1 FROM session_workspace_allocations WHERE workspace_id=? AND status='held'", (workspace_id,)).fetchone()
    if row:
        result = check(library, workspace['wallet_sender'], workspace['wallet_network'])
        if result['state'] != 'sufficient':
            raise TransferError(result['detail'])
