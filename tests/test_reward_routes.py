"""Fee destinations across exact unsigned reviews and synthetic finalized exits."""

import base64
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest
from solders.message import Message

from pumpwizard_runner.manual_trades import ManualTradeService, TradeJournal
from pumpwizard_runner.pump_route import PUMP_PROGRAM, decode_curve, key, read_state
from pumpwizard_runner.pump_swap_route import ROUTES as SWAP_ROUTES
from pumpwizard_runner.transfers import TransferError
from pumpwizard_runner.wallet_vault import WalletVault
from test_manual_trades import FIXTURE, MINT, account
from test_pump_swap_route import SwapRpc
from test_token2022_route import Token2022Rpc

HOLDER_CREATOR = json.loads((Path(__file__).parent /
    'fixtures/pump-route/sdk-holder-instructions.json').read_text())['inputs']['creator']


def configure_rewards(rpc, *, holder=False, fee=0, editable=False):
    creator = bytes(key(HOLDER_CREATOR)) if holder else bytes([7]) * 32
    raw = bytearray(base64.b64decode(rpc.bank['curve']['data'][0]))
    raw[49:81] = creator
    raw[115:125] = struct.pack('<QBB', fee, editable, holder)
    rpc.bank['curve']['data'][0] = base64.b64encode(raw).decode()
    raw = bytearray(rpc.pool_raw)
    raw[211:243] = creator
    raw[261:271] = struct.pack('<QBB', fee, editable, holder)
    rpc.pool_raw = bytes(raw)


@pytest.fixture(params=[SwapRpc, Token2022Rpc], ids=['spl', 'token2022'])
def setup(tmp_path, request):
    wallet = WalletVault(tmp_path / 'vault').create('synthetic rewards password')
    rpc = request.param(wallet)
    journal = TradeJournal(tmp_path)
    yield wallet, rpc, journal, ManualTradeService(rpc, journal), tmp_path
    journal.close()


def finalize(wallet, rpc, service, preview):
    # Every reviewed buy/partial/full exit (both token programs and reward modes)
    # declares the same zero priority fee, without a duplicate budget instruction.
    message = Message.from_bytes(base64.b64decode(preview['message']))
    budget = [ix for ix in message.instructions if
              str(message.account_keys[ix.program_id_index]) == 'ComputeBudget111111111111111111111111111111']
    assert [bytes(ix.data).hex() for ix in budget] == ['02801a0600', '030000000000000000']
    assert all(not ix.accounts for ix in budget)
    service.submit(preview['id'], wallet)
    rpc.status = {'confirmationStatus': 'finalized', 'err': None}
    service.reconcile(wallet.address)


@pytest.mark.parametrize('holder', [False, True], ids=['creator', 'holders'])
@pytest.mark.parametrize('configurable', [False, True], ids=['standard', 'fee-fields'])
@pytest.mark.parametrize('graduated', [False, True], ids=['curve-exit', 'migration-exit'])
def test_rewards_buy_partial_restart_full_exit(setup, holder, configurable, graduated):
    wallet, rpc, journal, service, path = setup
    configure_rewards(rpc, holder=holder, fee=75 if configurable else 0, editable=configurable)
    initial_balance = rpc.balance
    preview = service.preview(wallet.address, MINT, 'buy', '0.001')
    assert preview['state']['reward_mode'] == ('holders' if holder else 'creator')
    assert preview['state']['can_edit_creator_fee'] is configurable
    assert preview['state']['creator_fee_bps'] == (75 if configurable else 0)
    if holder:
        assert preview['state']['creator'] == HOLDER_CREATOR
    assert not rpc.sent
    finalize(wallet, rpc, service, preview)
    position_id = preview['id']
    assert journal.position(position_id)['quantity'] == '10000000'
    rpc.graduated = graduated
    partial = service.preview(wallet.address, MINT, 'sell', '4', position_id=position_id)
    assert (partial['route'] in SWAP_ROUTES) is graduated
    assert partial['state']['reward_mode'] == preview['state']['reward_mode']
    finalize(wallet, rpc, service, partial)
    assert journal.position(position_id)['quantity'] == '6000000'
    reopened = TradeJournal(path)
    try:
        recovered = ManualTradeService(rpc, reopened)
        full = recovered.preview(wallet.address, MINT, 'sell', '6', position_id=position_id)
        finalize(wallet, rpc, recovered, full)
        recovered.reconcile(wallet.address)
        closed = reopened.position(position_id)
        assert closed['status'] == 'closed' and closed['quantity'] == '0'
        assert closed['realized_cost'] == closed['original_cost']
        assert int(closed['realized_proceeds']) - int(closed['realized_cost']) == rpc.balance - initial_balance
        assert not reopened.pending(wallet.address, rpc.genesis())
        assert len(rpc.sent) == 3
    finally:
        reopened.close()


@pytest.mark.parametrize('graduated', [False, True])
@pytest.mark.parametrize('change', ['holder', 'fee', 'editable', 'creator'])
def test_reward_or_fee_change_requires_review_before_signing(setup, graduated, change):
    wallet, rpc, journal, service, _ = setup
    configure_rewards(rpc)
    if graduated:
        entry = service.preview(wallet.address, MINT, 'buy', '0.001')
        finalize(wallet, rpc, service, entry)
        rpc.graduated = True
        preview = service.preview(wallet.address, MINT, 'sell', '4', position_id=entry['id'])
    else:
        preview = service.preview(wallet.address, MINT, 'buy', '0.001')
    sent = len(rpc.sent)
    configure_rewards(rpc, holder=change == 'holder', fee=75 if change == 'fee' else 0,
                      editable=change == 'editable')
    if change == 'creator':
        if graduated:
            raw = bytearray(rpc.pool_raw)
            raw[211:243] = bytes([13]) * 32
            rpc.pool_raw = bytes(raw)
        else:
            raw = bytearray(base64.b64decode(rpc.bank['curve']['data'][0]))
            raw[49:81] = bytes([13]) * 32
            rpc.bank['curve']['data'][0] = base64.b64encode(raw).decode()
    def forbidden_sign(_message):
        pytest.fail('Changed review reached the signer')
    with pytest.raises(TransferError, match='changed after review'):
        service.submit(preview['id'], SimpleNamespace(address=wallet.address, sign=forbidden_sign))
    assert len(rpc.sent) == sent and not journal.pending(wallet.address, rpc.genesis())


@pytest.mark.parametrize('size', [115, 124, 125, 151])
def test_known_curve_layouts_default_only_absent_reward_fields(size):
    raw = bytearray(base64.b64decode(FIXTURE['curve']['data'][0]))
    raw[115:125] = struct.pack('<QBB', 75, True, True)
    decoded = decode_curve(account(PUMP_PROGRAM, raw[:size]))
    assert decoded['is_holder_reward'] is (size >= 125)
    assert decoded['creator_fee_bps'] == (75 if size >= 124 else 0)
    assert decoded['can_edit_creator_fee'] is (size >= 124)


@pytest.mark.parametrize('size', list(range(116, 124)))
def test_partial_curve_fee_layouts_are_rejected(size):
    raw = base64.b64decode(FIXTURE['curve']['data'][0])[:size]
    with pytest.raises(TransferError, match='truncated'):
        decode_curve(account(PUMP_PROGRAM, raw))


@pytest.mark.parametrize('offset,value', [(269, 2), (270, 2), (261, 10001)])
def test_invalid_pool_reward_fields_fail_before_preview(setup, offset, value):
    wallet, rpc, _, _, _ = setup
    rpc.graduated = True
    raw = bytearray(rpc.pool_raw)
    if offset == 261:
        raw[offset:offset + 8] = struct.pack('<Q', value)
    else:
        raw[offset] = value
    rpc.pool_raw = bytes(raw)
    with pytest.raises(TransferError, match='Invalid'):
        read_state(rpc, wallet.address, MINT, 'sell')
    assert not rpc.sent
