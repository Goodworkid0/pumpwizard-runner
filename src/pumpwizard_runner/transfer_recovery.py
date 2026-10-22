"""Explicit, independently checked recovery of expired SOL transfer intents.

Never broadcasts, signs, deletes receipts, or resolves trade intents. A missing
signature alone is insufficient: both independent RPCs must verify final expiry
and return no historical signature status or finalized transaction.
"""
import json
import time
from urllib.parse import urlsplit

from .transfers import GENESIS, ENDPOINTS, TransferError, _uint


def independent_rpc(primary, network):
    from .manual_trades import TradeRpc
    witness = TradeRpc(network, ENDPOINTS[network])
    if urlsplit(primary.url).hostname == urlsplit(witness.url).hostname:
        raise TransferError('Recovery needs an independent RPC check. Configure a dedicated RPC first.')
    return witness


def resolve_expired(journal, primary, witness, sender, network, transfer_id, signature):
    genesis = GENESIS[network]
    def original():
        row = journal.db.execute('SELECT * FROM transfers WHERE id=?', (transfer_id,)).fetchone()
        if row is None or (row['sender'], row['network'], row['genesis'], row['signature']) != (sender, network, genesis, signature):
            raise TransferError('Recovery does not match the original wallet, network and signature')
        return dict(row)
    row = original()
    if row['status'] == 'expired':
        return row
    if row['status'] not in ('pending', 'unknown'):
        raise TransferError('Only an unconfirmed transfer can be checked for expiry; refresh its receipt')
    if primary is witness:
        raise TransferError('Recovery requires two independent RPC checks')
    if hasattr(primary, 'url') and hasattr(witness, 'url') and urlsplit(primary.url).hostname == urlsplit(witness.url).hostname:
        raise TransferError('Recovery requires two independent RPC checks')
    evidence = []
    for label, rpc in [('configured', primary), ('independent', witness)]:
        if rpc.network != network or rpc.genesis() != genesis:
            raise TransferError('Recovery RPC network identity changed')
        height = _uint(rpc.call('getBlockHeight', [{'commitment': 'finalized'}]), 'finalized block height')
        if height <= row['last_valid_height']:
            raise TransferError('The original transfer can still land. Wait and check its confirmation again.')
        result = rpc.call('getSignatureStatuses', [[signature], {'searchTransactionHistory': True}])
        if not isinstance(result, dict) or result.get('value') != [None]:
            raise TransferError('The original signature is present or its status is incomplete. Refresh its receipt; the lock remains.')
        context = result.get('context')
        if not isinstance(context, dict):
            raise TransferError('Incomplete signature status context; the lock remains')
        slot = _uint(context.get('slot'), 'signature status context')
        transaction = rpc.call('getTransaction', [signature, {'commitment': 'finalized', 'encoding': 'json', 'maxSupportedTransactionVersion': 0}])
        if transaction is not None:
            raise TransferError('A transaction was found for the original signature. Refresh its receipt; the lock remains.')
        evidence.append(dict(provider=label, genesis=genesis, finalized_height=height,
                             status_context_slot=slot, historical_status=None, finalized_transaction=None))
    with journal.db:
        journal.db.execute('BEGIN IMMEDIATE')
        current = original()
        if current['status'] == 'expired':
            return current
        if current['status'] not in ('pending', 'unknown') or current['last_valid_height'] != row['last_valid_height']:
            raise TransferError('The receipt changed during recovery. Refresh its status.')
        intent = journal.db.execute('SELECT id,kind FROM wallet_intents WHERE sender=? AND genesis=?', (sender, genesis)).fetchone()
        if intent is None or tuple(intent) != (transfer_id, 'withdrawal'):
            raise TransferError('The wallet intent changed. Recovery did not release anything.')
        journal.db.execute('CREATE TABLE IF NOT EXISTS transfer_recoveries (transfer_id TEXT PRIMARY KEY, signature TEXT NOT NULL, evidence TEXT NOT NULL, resolved_at REAL NOT NULL)')
        journal.db.execute('INSERT INTO transfer_recoveries VALUES (?,?,?,?)',
                           (transfer_id, signature, json.dumps(dict(last_valid_height=row['last_valid_height'], checks=evidence)), time.time()))
        journal.db.execute("UPDATE transfers SET status='expired', detail=? WHERE id=?", (
            'Expired without a transaction found by two independent RPC checks. Local lock released; no transfer was resent.', transfer_id))
        journal.db.execute("DELETE FROM wallet_intents WHERE id=? AND kind='withdrawal'", (transfer_id,))
    return original()
