"""Exact browser SOL reviews; no caller-supplied transaction construction."""
import base64
from dataclasses import asdict
import json

from . import web_trading
from .manual_trades import TradeJournal
from .pump_route import unsigned_transaction
from .reservations import require_revision, wallet_revision
from .transfers import GENESIS, TransferError, WithdrawalPreview, WithdrawalService


def command(directory, action, body):
    sender, network = web_trading._identity(body.get('sender'), body.get('network'))
    identity = {'sender', 'network'}
    journal = TradeJournal(directory)
    try:
        journal.db.execute('CREATE TABLE IF NOT EXISTS browser_withdrawal_reviews (id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL)')
        service = WithdrawalService(web_trading.rpc_for(network, directory), journal)
        if action == 'resolve-expired':
            if set(body) != identity | {'transfer_id', 'signature'} or not all(isinstance(body[k], str) for k in body):
                raise TransferError('Select the exact original transfer and signature for recovery')
            from .transfer_recovery import independent_rpc, resolve_expired
            return dict(transfer=resolve_expired(journal, service.rpc, independent_rpc(service.rpc, network),
                        sender, network, body['transfer_id'], body['signature']))
        if action == 'preview':
            required = identity | {'recipient', 'amount'}
            if (not required <= set(body) or set(body) - required - {'funding_budget'}
                    or not all(isinstance(body[k], str) for k in required)
                    or type(body.get('funding_budget', False)) is not bool):
                raise TransferError('Withdrawal review requires a recipient and SOL amount')
            if journal.pending(sender, GENESIS[network]):
                raise TransferError('Resolve the original wallet signature before another withdrawal')
            revision = wallet_revision(journal, GENESIS[network], sender)
            preview = service.preview(sender, body['recipient'].strip(), body['amount'], funding_budget=body.get('funding_budget', False))
            with journal.db:
                require_revision(journal, GENESIS[network], sender, revision)
                journal.db.execute('INSERT INTO browser_withdrawal_reviews VALUES (?,?,?)',
                                   (preview.id, json.dumps(asdict(preview)), revision))
            return dict(preview={**asdict(preview), **{k: str(getattr(preview, k)) for k in ('lamports','fee','balance','source_reserve','reserved_lamports')},
                        'funding_budget': None if preview.funding_budget is None else str(preview.funding_budget),
                        'expires_at': preview.created_at+90,
                        'unsigned_transaction': base64.b64encode(unsigned_transaction(preview.message())).decode()})
        if action == 'submit':
            if set(body) != identity | {'preview_id', 'signed_transaction'}:
                raise TransferError('Submit the exact original withdrawal review')
            row = journal.db.execute('SELECT * FROM browser_withdrawal_reviews WHERE id=?', (body['preview_id'],)).fetchone()
            if row is None:
                raise TransferError('Withdrawal review was not found')
            preview = WithdrawalPreview(**json.loads(row['payload']))
            if (preview.sender, preview.genesis) != (sender, GENESIS[network]):
                raise TransferError('Withdrawal review belongs to a different wallet or network')
            require_revision(journal, GENESIS[network], sender, row['revision'])
            signature = service.submit_signed(preview, sender, body['signed_transaction'])
            receipt = next(item for item in journal.rows(sender) if item["id"] == preview.id)
            return dict(signature=signature, state=receipt["status"], detail=receipt["detail"])
        raise TransferError('Unknown withdrawal action')
    finally:
        journal.close()
