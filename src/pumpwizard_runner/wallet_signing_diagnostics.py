"""Compare unsigned wallet-returned messages. No RPC, signer or submission path."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from solders.message import MessageV0, from_bytes_versioned, to_bytes_versioned

from .transfers import TransferError

LIGHTHOUSE = 'L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95'
COMPUTE_BUDGET = 'ComputeBudget111111111111111111111111111111'


def describe(raw):
    message = from_bytes_versioned(raw)
    if to_bytes_versioned(message) != raw:
        raise ValueError('Noncanonical message')
    keys = [str(key) for key in message.account_keys]
    lookups = message.address_table_lookups if isinstance(message, MessageV0) else []
    loaded = sum(len(row.writable_indexes) + len(row.readonly_indexes) for row in lookups)
    h = message.header
    if not 1 <= h.num_required_signatures <= len(keys) or h.num_readonly_signed_accounts >= h.num_required_signatures:
        raise ValueError('Invalid signer header')
    if h.num_readonly_unsigned_accounts > len(keys) - h.num_required_signatures:
        raise ValueError('Invalid account header')
    def address(index):
        if not 0 <= index < len(keys) + loaded:
            raise ValueError('Invalid account index')
        return keys[index] if index < len(keys) else f'lookup-index:{index - len(keys)}'
    instructions = [dict(program=address(ix.program_id_index), accounts=[address(i) for i in ix.accounts],
                         data_hex=bytes(ix.data).hex()) for ix in message.instructions]
    return dict(version='v0' if isinstance(message, MessageV0) else 'legacy',
        bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), required_signatures=h.num_required_signatures,
        readonly_signed=h.num_readonly_signed_accounts, readonly_unsigned=h.num_readonly_unsigned_accounts,
        account_count=len(keys), accounts=keys, lookup_tables=len(lookups),
        blockhash=str(message.recent_blockhash), instructions=instructions)


def capture(directory, body):
    fields = {'sender', 'network', 'preview_id', 'diagnostic'}
    if not isinstance(body, dict) or set(body) != fields:
        raise TransferError('Signing diagnostics require the original review and unsigned message details')
    from .web_trading import _identity
    sender, network = _identity(body['sender'], body['network'])
    if not isinstance(body['preview_id'], str) or not 1 <= len(body['preview_id']) <= 100:
        raise TransferError('Choose the original review')
    detail = body['diagnostic']
    if not isinstance(detail, dict) or set(detail) != {'wire_bytes', 'signature_count', 'message', 'valid_signature', 'value_type'}:
        raise TransferError('Signing diagnostics accept unsigned message details only')
    if (type(detail['wire_bytes']) is not int or not 0 <= detail['wire_bytes'] <= 16_384
            or (detail['signature_count'] is not None and
                (type(detail['signature_count']) is not int or not 0 <= detail['signature_count'] <= 32))
            or detail['value_type'] not in ('typed-array', 'array-buffer', 'array', 'other')
            or (detail['valid_signature'] is not None and type(detail['valid_signature']) is not bool)
            or (detail['message'] is not None and
                (not isinstance(detail['message'], str) or len(detail['message']) > 22_000))):
        raise TransferError('Invalid unsigned signing diagnostic')
    directory = Path(directory)
    db = sqlite3.connect((directory / 'wallet-transfers.sqlite').resolve().as_uri() + '?mode=ro', uri=True)
    try:
        row = db.execute('SELECT payload FROM trade_previews WHERE id=?', (body['preview_id'],)).fetchone()
    finally:
        db.close()
    p = json.loads(row[0]) if row else {}
    if (p.get('sender'), p.get('network')) != (sender, network) or 'message' not in p:
        raise TransferError('Choose an unsubmitted review belonging to this wallet')
    expected_raw = base64.b64decode(p['message'], validate=True)
    expected = describe(expected_raw)
    received = None
    raw = None
    if detail['message'] is not None:
        try:
            raw = base64.b64decode(detail['message'], validate=True)
            if len(raw) > 16_384:
                raise ValueError()
            received = describe(raw)
            if detail['signature_count'] != received['required_signatures']:
                raise ValueError()
            # Every supported signature count fits one compact-length byte.
            if detail['wire_bytes'] != 1 + detail['signature_count'] * 64 + len(raw):
                raise ValueError()
        except Exception:
            raise TransferError('Diagnostic message is malformed; no diagnostic was saved') from None
    same = raw == expected_raw
    added = [] if received is None else list(dict.fromkeys(
        row['program'] for row in received['instructions'] if row not in expected['instructions']))
    if received is None:
        summary = 'The wallet did not return a readable transaction message.'
    elif same and detail['valid_signature'] is True:
        summary = 'The wallet signed the exact reviewed transaction. This diagnostic did not submit it.'
    elif same:
        summary = 'The returned message matches, but its signature was not valid for the connected wallet.'
    else:
        summary = (f"Wallet returned {received['version']} ({detail['wire_bytes']} bytes, "
                   f"{len(received['instructions'])} instructions); review was {expected['version']} "
                   f"({65 + len(expected_raw)} bytes, {len(expected['instructions'])} instructions).")
        if LIGHTHOUSE in added:
            summary += ' A Lighthouse instruction was added or changed.'
        if COMPUTE_BUDGET in added:
            summary += ' A compute-budget or priority-fee instruction was added or changed.'
    report = dict(schema='pumpwizard.wallet-signing-diagnostic/v1', created_at=time.time(),
        preview_id=body['preview_id'], network=network, source='browser-reported-unsigned-message',
        submission_enabled=False, status='matches' if same and detail['valid_signature'] is True else 'mismatch',
        summary=summary, wire_bytes=detail['wire_bytes'], signature_count=detail['signature_count'],
        value_type=detail['value_type'], browser_verified_signature=detail['valid_signature'],
        expected=expected, received=received, changed_instruction_programs=added)
    descriptor, temporary = tempfile.mkstemp(prefix='.wallet-signing-', dir=directory)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(report, stream, indent=2)
        os.replace(temporary, directory / 'wallet-signing-diagnostic.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return dict(diagnostic={k: report[k] for k in ('status', 'summary', 'created_at', 'submission_enabled')})
