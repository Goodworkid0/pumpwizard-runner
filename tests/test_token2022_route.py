"""Token-2022 policy, exact accounting and restart coverage; no live RPC."""

import base64
import copy
import json
from pathlib import Path
import struct

import pytest

from pumpwizard_runner.manual_trades import ManualTradeService, TradeJournal
from pumpwizard_runner.pump_route import (
    TOKEN_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_2022_ROUTE, ata, key, token_amount,
)
from pumpwizard_runner.pump_swap_route import TOKEN_2022_ROUTE as SWAP_ROUTE
from pumpwizard_runner.token_policy import extensions, validate_mint
from pumpwizard_runner.transfers import TransferError
from pumpwizard_runner.wallet_vault import WalletVault
from test_manual_trades import MINT, account, buy
from test_pump_swap_route import SwapRpc

FIXTURE = json.loads((Path(__file__).parent / "fixtures/pump-route/token2022-layouts.json").read_text())
MINT_RAW = base64.b64decode(FIXTURE["mint_base64"])


class Token2022Rpc(SwapRpc):
    token_program = TOKEN_2022_PROGRAM

    def __init__(self, wallet):
        super().__init__(wallet)
        assert MINT == FIXTURE["mint"]
        self.bank["mint_account"] = account(TOKEN_2022_PROGRAM, MINT_RAW)
        self.program_snapshots = 0
        self.stale_program_snapshot = False
        self.changed_program_snapshot = False

    def call(self, method, params):
        result = super().call(method, params)
        if (method == "getMultipleAccounts" and len(params[0]) == 5 and
                params[0][4] == ata(self.wallet.address, MINT, TOKEN_2022_PROGRAM)):
            self.program_snapshots += 1
            assert params[1]["minContextSlot"] == 20
            if self.stale_program_snapshot:
                result["context"]["slot"] = 19
            if self.changed_program_snapshot:
                result["value"][2] = account(TOKEN_PROGRAM, MINT_RAW[:82])
        return result


@pytest.fixture
def setup(tmp_path):
    wallet = WalletVault(tmp_path / "vault").create("synthetic token2022 password")
    rpc = Token2022Rpc(wallet)
    journal = TradeJournal(tmp_path)
    yield wallet, rpc, journal, ManualTradeService(rpc, journal), tmp_path
    journal.close()


def tlv(kind, value):
    return struct.pack("<HH", kind, len(value)) + value


def test_official_spl_layouts_are_decoded_without_metadata_network_access():
    validate_mint(MINT_RAW, MINT, TOKEN_2022_PROGRAM)
    raw = base64.b64decode(FIXTURE["token_base64"])
    assert token_amount(account(TOKEN_2022_PROGRAM, raw), MINT, FIXTURE["owner"],
                        token_program=TOKEN_2022_PROGRAM) == 123456789
    assert ata(FIXTURE["owner"], MINT) != ata(FIXTURE["owner"], MINT, TOKEN_2022_PROGRAM)


@pytest.mark.parametrize("kind", [1, 3, 4, 6, 9, 10, 12, 14, 20, 21, 22, 23, 25, 26, 28, 65535])
def test_other_mint_extensions_are_rejected_before_quote(setup, kind):
    wallet, rpc, journal, service, _ = setup
    rpc.bank["mint_account"] = account(TOKEN_2022_PROGRAM, MINT_RAW + tlv(kind, bytes(64)))
    with pytest.raises(TransferError, match="MetadataPointer and TokenMetadata only"):
        service.preview(wallet.address, MINT, "buy", "0.1")
    assert not rpc.sent
    assert journal.db.execute("SELECT COUNT(*) FROM trade_previews").fetchone()[0] == 0


@pytest.mark.parametrize("mutation", ["duplicate", "short", "padding", "type", "pointer_authority",
                                     "pointer_address", "metadata_authority", "metadata_mint",
                                     "metadata_length", "nonzero_after_end", "missing_metadata"])
def test_malformed_or_mutable_metadata_rejected(mutation):
    raw = bytearray(MINT_RAW)
    if mutation == "duplicate":
        raw += tlv(18, bytes(64))
    elif mutation == "short":
        raw = raw[:-1]
    elif mutation == "padding":
        raw[100] = 1
    elif mutation == "type":
        raw[165] = 2
    elif mutation == "pointer_authority":
        raw[170] = 1
    elif mutation == "pointer_address":
        raw[202] ^= 1
    elif mutation == "metadata_authority":
        raw[238] = 1
    elif mutation == "metadata_mint":
        raw[270] ^= 1
    elif mutation == "metadata_length":
        raw[302:306] = bytes([255]) * 4
    elif mutation == "nonzero_after_end":
        raw += bytes(4) + b"hidden"
    elif mutation == "missing_metadata":
        raw = raw[:234]
    with pytest.raises(TransferError):
        validate_mint(bytes(raw), MINT, TOKEN_2022_PROGRAM)


@pytest.mark.parametrize("extension", [tlv(1, b""), tlv(2, bytes(8)), tlv(8, b"\0"),
                                       tlv(11, b"\0"), tlv(15, b"\0"), tlv(7, b"x"), tlv(999, b"")])
def test_unsupported_token_account_extensions_rejected(extension):
    raw = base64.b64decode(FIXTURE["token_base64"])
    with pytest.raises(TransferError):
        token_amount(account(TOKEN_2022_PROGRAM, raw[:166] + extension), MINT, FIXTURE["owner"],
                     token_program=TOKEN_2022_PROGRAM)


@pytest.mark.parametrize("offset,value", [(108, 2), (72, 1), (129, 1), (109, 1), (0, 8), (32, 8)])
def test_frozen_delegated_native_or_wrong_identity_account_rejected(offset, value):
    raw = bytearray(base64.b64decode(FIXTURE["token_base64"]))
    raw[offset] = value
    with pytest.raises(TransferError):
        token_amount(account(TOKEN_2022_PROGRAM, raw), MINT, FIXTURE["owner"],
                     token_program=TOKEN_2022_PROGRAM)


@pytest.mark.parametrize("graduated", [False, True])
def test_buy_partial_sell_restart_full_sell(setup, graduated):
    wallet, rpc, journal, service, path = setup
    p, _ = buy(setup)
    assert p["route"] == TOKEN_2022_ROUTE
    assert rpc.program_snapshots >= 2
    rpc.graduated = graduated
    preview = service.preview(wallet.address, MINT, "sell", "400", position_id=p["id"])
    assert preview["route"] == (SWAP_ROUTE if graduated else TOKEN_2022_ROUTE)
    service.submit(preview["id"], wallet)
    reopened = TradeJournal(path)
    try:
        recovered = ManualTradeService(rpc, reopened)
        recovered.reconcile(wallet.address)
        recovered.reconcile(wallet.address)
        position = reopened.position(p["id"])
        assert position["quantity"] == "600000000"
        final = recovered.preview(wallet.address, MINT, "sell", "600", position_id=p["id"])
        recovered.submit(final["id"], wallet)
        recovered.reconcile(wallet.address)
        position = reopened.position(p["id"])
        assert position["status"] == "closed"
        assert position["remaining_cost"] == "0"
        assert position["realized_cost"] == position["original_cost"]
        assert not reopened.pending(wallet.address, rpc.genesis())
        assert len(rpc.sent) == 3
    finally:
        reopened.close()


@pytest.mark.parametrize("mutate", ["missing", "wrong"])
def test_receipt_must_prove_token_program_before_applying_fill(setup, mutate):
    wallet, rpc, journal, service, _ = setup
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    service.submit(p["id"], wallet)
    valid_receipt = copy.deepcopy(rpc.receipt)
    if mutate == "missing":
        rpc.receipt["meta"]["postTokenBalances"][0].pop("programId")
    else:
        rpc.receipt["meta"]["postTokenBalances"][0]["programId"] = TOKEN_PROGRAM
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    service.reconcile(wallet.address)
    assert not journal.positions(wallet.address) and journal.pending(wallet.address, rpc.genesis())
    rpc.receipt = valid_receipt
    service.reconcile(wallet.address)
    assert len(journal.positions(wallet.address)) == 1
    assert not journal.pending(wallet.address, rpc.genesis())


@pytest.mark.parametrize("mutation", ["stale", "owner", "extension"])
def test_revalidation_blocks_stale_changed_or_unsupported_state(setup, mutation):
    wallet, rpc, journal, service, _ = setup
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    if mutation == "stale":
        rpc.stale_program_snapshot = True
    elif mutation == "owner":
        rpc.changed_program_snapshot = True
    else:
        rpc.bank["mint_account"] = account(TOKEN_2022_PROGRAM, MINT_RAW + tlv(14, bytes(64)))
    with pytest.raises(TransferError):
        service.submit(p["id"], wallet)
    assert not rpc.sent and not journal.pending(wallet.address, rpc.genesis())


def test_standard_program_cannot_decode_token2022_account():
    with pytest.raises(TransferError):
        token_amount(account(TOKEN_2022_PROGRAM, base64.b64decode(FIXTURE["token_base64"])),
                     MINT, FIXTURE["owner"])


def test_only_zero_padding_can_follow_tlv_records():
    validate_mint(MINT_RAW + bytes(4), MINT, TOKEN_2022_PROGRAM)
    assert set(extensions(MINT_RAW, account_type=1, base_length=82)) == {18, 19}


def test_sdk_layout_mint_matches_public_key():
    assert bytes(key(MINT)) == MINT_RAW[202:234]
