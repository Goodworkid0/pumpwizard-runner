import base64
import json
import sqlite3

import pytest
from solders.instruction import AccountMeta, Instruction
from solders.message import Message, MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.compute_budget import set_compute_unit_price

from pumpwizard_runner import web_trading
from pumpwizard_runner.transfers import TransferError
from pumpwizard_runner.wallet_signing_diagnostics import LIGHTHOUSE, capture
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.web_api import command
from test_web_trading import browser as shared_browser, review  # noqa: F401


@pytest.fixture
def browser(request):
    return request.getfixturevalue('shared_browser')


def diagnostic_body(browser, raw=None):
    path, wallet, _ = browser
    p = review(browser)
    message = base64.b64decode(p['unsigned_transaction'])[65:] if raw is None else raw
    return dict(sender=str(wallet.pubkey()), network='mainnet-beta', preview_id=p['id'],
        diagnostic=dict(wire_bytes=65+len(message), signature_count=1,
                        message=base64.b64encode(message).decode(), valid_signature=True, value_type='typed-array'))


def instructions(message):
    h = message.header
    def writable(i):
        return i < h.num_required_signatures-h.num_readonly_signed_accounts if i < h.num_required_signatures else i < len(message.account_keys)-h.num_readonly_unsigned_accounts
    return [Instruction(message.account_keys[ix.program_id_index], ix.data,
        [AccountMeta(message.account_keys[i], message.is_signer(i), writable(i)) for i in ix.accounts]) for ix in message.instructions]


@pytest.mark.parametrize('kind', ['exact', 'lighthouse', 'priority_fee', 'v0', 'invalid_signature', 'empty'])
def test_diagnosis_never_uses_rpc_or_reserves_or_submits(browser, monkeypatch, kind):
    path, wallet, bank = browser
    body = diagnostic_body(browser)
    detail = body['diagnostic']
    raw = base64.b64decode(detail['message'])
    m = Message.from_bytes(raw)
    rows = instructions(m)
    if kind == 'lighthouse':
        rows.append(Instruction(Pubkey.from_string(LIGHTHOUSE), b'\x01', [AccountMeta(wallet.pubkey(), False, False)]))
    if kind == 'priority_fee':
        rows[1] = set_compute_unit_price(375_000)
    if kind in ('lighthouse', 'priority_fee'):
        raw = bytes(Message.new_with_blockhash(rows, wallet.pubkey(), m.recent_blockhash))
    if kind == 'v0':
        raw = to_bytes_versioned(MessageV0.try_compile(wallet.pubkey(), rows, [], m.recent_blockhash))
    detail.update(message=base64.b64encode(raw).decode(), wire_bytes=65+len(raw))
    if kind == 'empty':
        detail.update(message=None, wire_bytes=0, signature_count=None, valid_signature=None, value_type='other')
    if kind == 'invalid_signature':
        detail['valid_signature'] = False

    def forbidden(*args, **kwargs):
        raise AssertionError('Diagnostics must not use an RPC')

    monkeypatch.setattr(web_trading, 'rpc_for', forbidden)
    response = capture(path, body)['diagnostic']
    assert response['submission_enabled'] is False
    assert response['status'] == ('matches' if kind == 'exact' else 'mismatch')
    if kind == 'lighthouse':
        assert 'Lighthouse' in response['summary']
    if kind == 'priority_fee':
        assert 'priority-fee' in response['summary']
    if kind == 'v0':
        assert 'v0' in response['summary']
    saved = json.loads((path/'wallet-signing-diagnostic.json').read_text())
    assert saved['source'] == 'browser-reported-unsigned-message'
    assert 'message' not in saved and 'signed_transaction' not in saved and 'signature' not in saved
    assert (path/'wallet-signing-diagnostic.json').stat().st_mode & 0o777 == 0o600
    assert bank.sent == []
    db=sqlite3.connect(path/'wallet-transfers.sqlite')
    try:
        assert db.execute('SELECT count(*) FROM trades').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM wallet_intents').fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize('kind', ['wrong_wallet','signed_payload','signature_field','oversize','bad_message','bad_count'])
def test_diagnostic_rejects_unbound_or_disallowed_input(browser, kind):
    path, _, _ = browser
    body = diagnostic_body(browser)
    if kind == 'wrong_wallet':
        body['sender'] = str(Pubkey.new_unique())
    if kind == 'signed_payload':
        body['signed_transaction'] = 'not allowed'
    if kind == 'signature_field':
        body['diagnostic']['signature'] = 'not allowed'
    if kind == 'oversize':
        body['diagnostic']['wire_bytes'] = 100_000
    if kind == 'bad_message':
        body['diagnostic']['message'] = 'invalid'
    if kind == 'bad_count':
        body['diagnostic']['signature_count'] = 100
    with pytest.raises(TransferError):
        capture(path, body)
    assert not (path/'wallet-signing-diagnostic.json').exists()


def test_diagnostic_api_requires_wallet_proof(browser):
    path, _, _ = browser
    body = diagnostic_body(browser)
    library = StrategyLibrary(path)
    try:
        with pytest.raises(TransferError, match='Wallet sign-in'):
            command(library, '/api/v1/trades/signing-diagnostic', body)
        with pytest.raises(TransferError):
            command(library, '/api/v1/trades/signing-diagnostic', body, web_trading.WalletAuthorizer())
    finally:
        library.close()
