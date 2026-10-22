"""RPC boundary fixtures: unsigned quote, exact signed messages, final receipts.

These are deterministic synthetic bank responses, not claims of mainnet fills.
"""

import base64
import copy
import json
from pathlib import Path
import struct
import time

import pytest
from solders.hash import Hash
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from pumpwizard_runner.manual_trades import (
    ManualTradeService,
    TradeJournal,
    parse_tokens,
    validate_receipt,
)
from pumpwizard_runner.pump_route import (
    ATA_PROGRAM,
    PUMP_PROGRAM,
    SPEC,
    TOKEN_PROGRAM,
    TOKEN_2022_PROGRAM,
    TOKEN_2022_ROUTE,
    ata,
    key,
    pda,
)
from pumpwizard_runner.transfers import (
    SYSTEM_PROGRAM,
    TransferError,
    TransferJournal,
    WithdrawalPreview,
)
from pumpwizard_runner.wallet_vault import WalletVault

FIXTURE = json.loads((Path(__file__).parent / "fixtures/pump-route/accounts.json").read_text())
MINT = FIXTURE["mint"]


def account(owner, raw=b"", lamports=1_000_000):
    return {
        "owner": owner,
        "executable": False,
        "lamports": lamports,
        "data": [base64.b64encode(raw).decode(), "base64"],
    }


def token(owner, quantity, token_program=TOKEN_PROGRAM):
    # SPL Account layout, independent of route decoder.
    raw = bytearray(165)
    raw[:32], raw[32:64], raw[64:72] = (
        bytes(key(MINT)),
        bytes(key(owner)),
        struct.pack("<Q", quantity),
    )
    raw[108] = 1
    if token_program == TOKEN_2022_PROGRAM:
        raw += b"\x02\x07\x00\x00\x00"
    return account(token_program, raw, 2_039_280)


class Rpc:
    network = "localnet"
    token_program = TOKEN_PROGRAM

    def __init__(self, wallet):
        self.wallet = wallet
        self.balance, self.tokens, self.height, self.fee = 3_000_000_000, 0, 1, 5000
        self.bank = copy.deepcopy(FIXTURE)
        self.sent, self.receipt, self.status = [], None, None
        self.disconnect, self.bad_sim, self.graduated = False, False, False
        self.expire, self.rent = False, 2_039_280

    def genesis(self):
        return "test-bank"

    def decode_trade(self, raw):
        tx = Transaction.from_bytes(raw)
        ix = tx.message.instructions[-1]
        amount, minimum = struct.unpack("<QQ", bytes(ix.data)[8:])
        buy = bytes(ix.data)[:8] == bytes([194, 171, 28, 70, 104, 77, 91, 47])
        return tx, amount, minimum, buy

    def effects(self, amount, buy):
        return (
            (-amount - self.fee - (self.rent if self.tokens == 0 else 0), amount * 10)
            if buy
            else (amount // 10 - self.fee, -amount)
        )

    def call(self, method, params):
        if method == "getBalance":
            return {"value": self.balance}
        if method == "getMultipleAccounts":
            assert params[0][:4] == [
                pda(PUMP_PROGRAM, b"global"),
                pda(PUMP_PROGRAM, b"bonding-curve", bytes(key(MINT))),
                MINT,
                self.wallet.address,
            ]
            assert params[0][4] in (ata(self.wallet.address, MINT),
                                    ata(self.wallet.address, MINT, self.token_program))
            curve = copy.deepcopy(self.bank["curve"])
            if self.graduated:
                raw = bytearray(base64.b64decode(curve["data"][0]))
                raw[48] = 1
                curve["data"][0] = base64.b64encode(raw).decode()
            return {
                "context": {"slot": 20},
                "value": [
                    self.bank["global"],
                    curve,
                    self.bank["mint_account"],
                    account(SYSTEM_PROGRAM, lamports=self.balance),
                    token(self.wallet.address, self.tokens, self.token_program)
                    if self.tokens and params[0][4] == ata(self.wallet.address, MINT, self.token_program)
                    else None,
                ],
            }
        if method == "getLatestBlockhash":
            return {
                "value": {
                    "blockhash": str(Hash.from_bytes(bytes([4]) * 32)),
                    "lastValidBlockHeight": 100,
                }
            }
        if method == "getFeeForMessage":
            return {"value": self.fee}
        if method == "getBlockHeight":
            return 101 if self.expire else self.height
        if method == "simulateTransaction":
            raw = base64.b64decode(params[0])
            assert raw[1:65] == bytes(64)
            tx, amount, minimum, buy = self.decode_trade(raw)
            cash, tokens = self.effects(amount, buy)
            output = tokens if buy else cash + self.fee
            error = "slippage" if output < minimum or self.bad_sim else None
            return {
                "context": {"slot": 20},
                "value": {
                    "err": error,
                    "accounts": [
                        account(SYSTEM_PROGRAM, lamports=self.balance + cash),
                        token(self.wallet.address, self.tokens + tokens, self.token_program),
                    ],
                },
            }
        if method == "sendTransaction":
            raw = base64.b64decode(params[0])
            tx, amount, minimum, buy = self.decode_trade(raw)
            assert tx.verify_with_results() == [True]
            self.sent.append(raw)
            cash, delta = self.effects(amount, buy)
            keys = list(tx.message.account_keys)
            token_index = keys.index(key(ata(self.wallet.address, MINT, self.token_program)))
            pre, post = [1_000_000] * len(keys), [1_000_000] * len(keys)
            pre[0], post[0] = self.balance, self.balance + cash

            def tb(q):
                return {
                    "accountIndex": token_index,
                    "mint": MINT,
                    "owner": self.wallet.address,
                    "programId": self.token_program,
                    "uiTokenAmount": {"amount": str(q), "decimals": 6},
                }

            self.receipt = {
                "slot": 21,
                "blockTime": int(time.time()),
                "transaction": [params[0], "base64"],
                "meta": {
                    "err": None,
                    "fee": self.fee,
                    "preBalances": pre,
                    "postBalances": post,
                    "preTokenBalances": [tb(self.tokens)] if self.tokens else [],
                    "postTokenBalances": [tb(self.tokens + delta)],
                },
            }
            self.balance += cash
            self.tokens += delta
            if self.disconnect:
                raise TransferError("timeout after node accepted transaction")
            return str(tx.signatures[0])
        if method == "getSignatureStatuses":
            return {"value": [self.status]}
        if method == "getTransaction":
            return self.receipt
        raise AssertionError(method)


@pytest.fixture
def setup(tmp_path):
    wallet = WalletVault(tmp_path / "vault").create("test wallet password")
    journal = TradeJournal(tmp_path)
    rpc = Rpc(wallet)
    yield wallet, rpc, journal, ManualTradeService(rpc, journal), tmp_path
    journal.close()


def buy(setup):
    wallet, rpc, journal, service, _ = setup
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    signature = service.submit(p["id"], wallet)
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    service.reconcile(wallet.address)
    return p, signature


def test_quote_is_unsigned_and_pinned_idl_account_order_is_exact(setup):
    wallet, rpc, journal, service, _ = setup
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    assert rpc.sent == [] and journal.positions(wallet.address) == []
    msg = Message.from_bytes(base64.b64decode(p["message"]))
    assert msg.header.num_required_signatures == 1 and str(msg.account_keys[0]) == wallet.address
    assert len(bytes(msg)) + 65 <= 1232
    assert [str(msg.account_keys[i.program_id_index]) for i in msg.instructions] == [
        "ComputeBudget111111111111111111111111111111",
        "ComputeBudget111111111111111111111111111111",
        ATA_PROGRAM,
        PUMP_PROGRAM,
    ]
    ix = msg.instructions[-1]
    assert len(ix.accounts) == 27
    assert bytes(ix.data) == bytes([194, 171, 28, 70, 104, 77, 91, 47]) + struct.pack(
        "<QQ", 100_000_000, 990_000_000
    )
    # Known PDA from official Pump docs, independent of fixture encoding.
    assert p["state"]["global"] == "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
    assert journal.previews(p["id"]) == p


def test_buy_partial_sell_full_sell_and_restart_are_exact_and_idempotent(setup):
    wallet, rpc, journal, service, directory = setup
    p, sig = buy(setup)
    position = journal.positions(wallet.address)[0]
    assert int(position["quantity"]) == 1_000_000_000
    assert int(position["original_cost"]) == 102_044_280
    assert not journal.pending(wallet.address, rpc.genesis())
    service.reconcile(wallet.address)
    assert len(journal.positions(wallet.address)) == 1
    reopened = TradeJournal(directory)
    try:
        service = ManualTradeService(rpc, reopened)
        for quantity in ["400", "600"]:
            quote = service.preview(wallet.address, MINT, "sell", quantity, position_id=p["id"])
            service.submit(quote["id"], wallet)
            service.reconcile(wallet.address)
        position = reopened.positions(wallet.address)[0]
        assert position["quantity"] == "0" and position["status"] == "closed"
        assert position["remaining_cost"] == "0"
        assert position["realized_cost"] == position["original_cost"]
        assert position["realized_proceeds"] == str(100_000_000 - 2 * 5000)
        assert len(reopened.trades(wallet.address)) == 3
    finally:
        reopened.close()


def test_wallet_tokens_without_runner_position_can_be_sold_without_creating_false_basis(setup):
    wallet, rpc, journal, service, _ = setup
    rpc.tokens = 1_000_000_000
    preview = service.preview(wallet.address, MINT, "sell", "400", position_id=None)
    service.submit(preview["id"], wallet)
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    service.reconcile(wallet.address)
    assert rpc.tokens == 600_000_000
    assert journal.positions(wallet.address) == []
    row = journal.trades(wallet.address)[0]
    assert row["status"] == "finalized"
    assert row["payload"]["position_id"] is None


def test_untracked_sell_cannot_consume_a_runner_position(setup):
    wallet, rpc, journal, service, _ = setup
    buy(setup)
    rpc.tokens += 200_000_000
    with pytest.raises(TransferError, match="outside Runner"):
        service.preview(wallet.address, MINT, "sell", "201", position_id=None)
    service.preview(wallet.address, MINT, "sell", "200", position_id=None)


def test_unknown_submission_retains_wallet_lock_across_restart_and_expiry(setup):
    wallet, rpc, journal, service, directory = setup
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    rpc.disconnect = True
    service.submit(p["id"], wallet)
    assert journal.trades(wallet.address)[0]["status"] == "unknown"
    rpc.expire = True
    reopened = TradeJournal(directory)
    try:
        other = ManualTradeService(rpc, reopened)
        other.reconcile(wallet.address)
        assert reopened.pending(wallet.address, rpc.genesis())
        with pytest.raises(TransferError):
            other.submit(p["id"], wallet)
        assert len(rpc.sent) == 1
        rpc.status = {"confirmationStatus": "finalized", "err": None}
        other.reconcile(wallet.address)
        assert not reopened.pending(wallet.address, rpc.genesis())
        assert len(reopened.positions(wallet.address)) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "mutation", ["balance", "fee", "expiry", "simulation", "stale", "recipient"]
)
def test_revalidation_blocks_changed_review_before_signing(setup, mutation):
    wallet, rpc, journal, service, _ = setup
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    if mutation == "balance":
        rpc.balance -= 1
    if mutation == "fee":
        rpc.fee += 1
    if mutation == "expiry":
        rpc.expire = True
    if mutation == "simulation":
        rpc.bad_sim = True
    if mutation == "stale":
        p["created_at"] -= 61
        with journal.db:
            journal.db.execute(
                "UPDATE trade_previews SET payload=? WHERE id=?", (json.dumps(p), p["id"])
            )
    if mutation == "recipient":
        raw = bytearray(base64.b64decode(rpc.bank["global"]["data"][0]))
        raw[41:73] = bytes(Pubkey.new_unique())
        rpc.bank["global"]["data"][0] = base64.b64encode(raw).decode()
    with pytest.raises(TransferError):
        service.submit(p["id"], wallet)
    assert not rpc.sent and not journal.pending(wallet.address, rpc.genesis())


@pytest.mark.parametrize("reserve", ["base", "quote"])
def test_pumpswap_reserve_change_after_review_requires_a_fresh_quote(setup, reserve):
    from test_pump_swap_route import SwapRpc

    wallet, _, journal, _, _ = setup
    rpc = SwapRpc(wallet)
    rpc.graduated = True
    service = ManualTradeService(rpc, journal)
    preview = service.preview(wallet.address, MINT, "buy", "0.1")
    assert preview["route"].startswith("pumpswap-canonical-")
    if reserve == "base":
        rpc.pool_base_tokens += 1
    else:
        rpc.pool_quote_tokens += 1
    with pytest.raises(TransferError, match="reserves changed after review"):
        service.submit(preview["id"], wallet)
    assert not rpc.sent and not journal.pending(wallet.address, rpc.genesis())


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_signature",
        "wrong_message",
        "missing_tokens",
        "wrong_mint",
        "wrong_owner",
        "overspend",
        "wrong_fee",
    ],
)
def test_untrusted_receipt_never_creates_position_or_releases_wallet(setup, mutation):
    wallet, rpc, journal, service, _ = setup
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    service.submit(p["id"], wallet)
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    meta = rpc.receipt["meta"]
    if mutation in ("wrong_signature", "wrong_message"):
        raw = bytearray(base64.b64decode(rpc.receipt["transaction"][0]))
        raw[1 if mutation == "wrong_signature" else 90] ^= 1
        rpc.receipt["transaction"][0] = base64.b64encode(raw).decode()
    if mutation == "missing_tokens":
        meta["postTokenBalances"] = []
    if mutation == "wrong_mint":
        meta["postTokenBalances"][0]["mint"] = SYSTEM_PROGRAM
    if mutation == "wrong_owner":
        meta["postTokenBalances"][0]["owner"] = SYSTEM_PROGRAM
    if mutation == "overspend":
        meta["postBalances"][0] -= 30_000_000
    if mutation == "wrong_fee":
        meta["fee"] += 1
    service.reconcile(wallet.address)
    assert journal.trades(wallet.address)[0]["status"] == "unknown"
    assert not journal.positions(wallet.address)
    assert journal.pending(wallet.address, rpc.genesis())


def test_confirmation_without_final_receipt_is_not_a_position(setup):
    wallet, rpc, journal, service, _ = setup
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    service.submit(p["id"], wallet)
    rpc.status = {"confirmationStatus": "confirmed", "err": None}
    service.reconcile(wallet.address)
    assert not journal.positions(wallet.address) and journal.pending(wallet.address, rpc.genesis())
    rpc.status["confirmationStatus"] = "finalized"
    rpc.receipt = None
    service.reconcile(wallet.address)
    assert not journal.positions(wallet.address) and journal.pending(wallet.address, rpc.genesis())


def test_failed_receipt_records_fee_and_releases_lock_without_position(setup):
    wallet, rpc, journal, service, _ = setup
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    service.submit(p["id"], wallet)
    rpc.status = {"confirmationStatus": "finalized", "err": {"InstructionError": [2, "Custom"]}}
    rpc.receipt["meta"]["err"] = rpc.status["err"]
    rpc.receipt["meta"]["postBalances"][0] = rpc.receipt["meta"]["preBalances"][0] - rpc.fee
    service.reconcile(wallet.address)
    row = journal.trades(wallet.address)[0]
    assert row["status"] == "failed" and row["receipt"]["fee_lamports"] == 5000
    assert not journal.positions(wallet.address) and not journal.pending(
        wallet.address, rpc.genesis()
    )


def test_cross_process_trade_and_withdrawal_exclusion(setup):
    wallet, rpc, journal, service, directory = setup
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    service.submit(p["id"], wallet)
    transfer = TransferJournal(directory)
    withdrawal = WithdrawalPreview(
        "withdrawal",
        "localnet",
        rpc.genesis(),
        wallet.address,
        MINT,
        1000,
        5000,
        rpc.balance,
        p["blockhash"],
        100,
        time.time(),
    )
    try:
        with pytest.raises(TransferError, match="unresolved"):
            transfer.reserve(withdrawal, "signature")
        rpc.status = {"confirmationStatus": "finalized", "err": None}
        service.reconcile(wallet.address)
        transfer.reserve(withdrawal, "signature")
        with pytest.raises(TransferError, match="wallet trade or withdrawal"):
            service.preview(wallet.address, MINT, "buy", "0.1")
        transfer.update("withdrawal", "finalized", "done")
        assert not journal.pending(wallet.address, rpc.genesis())
    finally:
        transfer.close()


def test_external_token_deficit_and_oversell_block_new_intents(setup):
    wallet, rpc, journal, service, _ = setup
    p, _ = buy(setup)
    with pytest.raises(TransferError, match="allocation"):
        service.preview(wallet.address, MINT, "sell", "1001", position_id=p["id"])
    rpc.tokens -= 1
    with pytest.raises(TransferError, match="external balance change"):
        service.preview(wallet.address, MINT, "sell", "1", position_id=p["id"])


@pytest.mark.parametrize(
    "amount", ["0", "-1", "NaN", "Infinity", "0.0000001", "18446744073709.551616"]
)
def test_exact_token_amount_rejects_rounding_and_overflow(amount):
    with pytest.raises(TransferError):
        parse_tokens(amount)


def test_failed_atomic_settlement_can_be_retried_without_duplicate_position(setup):
    wallet, rpc, journal, service, _ = setup
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    service.submit(p["id"], wallet)
    row = journal.trades(wallet.address)[0]
    receipt = validate_receipt(row, rpc.receipt)
    journal.db.executescript(
        "CREATE TRIGGER crash_settle BEFORE UPDATE ON trades BEGIN SELECT RAISE(ABORT, 'test crash'); END;"
    )
    with pytest.raises(Exception, match="test crash"):
        journal.settle(row, receipt)
    assert not journal.positions(wallet.address) and journal.pending(wallet.address, rpc.genesis())
    journal.db.execute("DROP TRIGGER crash_settle")
    journal.settle(row, receipt)
    journal.settle(row, receipt)
    assert len(journal.positions(wallet.address)) == 1


def test_fixture_pins_official_protocol_revision():
    assert SPEC["commit"] == "81091419e4457566469d4e2a27f64ed84d42419c"
    assert FIXTURE["provenance"]["kind"] == "synthetic-accounts"


@pytest.mark.parametrize(
    "side,amount,minimum", [("buy", 100_000_000, 990_000_000), ("sell", 400_000_000, 39_000_000)]
)
@pytest.mark.parametrize("token2022", [False, True])
@pytest.mark.parametrize("holder", [False, True])
def test_route_matches_independent_official_sdk_instructions(side, amount, minimum, token2022, holder):
    from pumpwizard_runner.pump_route import build_message, route_accounts

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/pump-route" /
         f"sdk-{'holder-' if holder else ''}{'token2022-' if token2022 else ''}instructions.json").read_text()
    )
    inputs = fixture["inputs"]
    state = {
        "sender": inputs["user"],
        "mint": inputs["mint"],
        "creator": inputs["creator"],
        "fee": inputs["feeRecipient"],
        "buyback": inputs["buybackFeeRecipient"],
        "global": pda(PUMP_PROGRAM, b"global"),
        "curve": pda(PUMP_PROGRAM, b"bonding-curve", bytes(key(inputs["mint"]))),
        "token_program": inputs["tokenProgram"],
    }
    if token2022:
        state["route"] = TOKEN_2022_ROUTE
    expected = fixture[side]
    msg = Message.from_bytes(build_message(state, side, amount, minimum, str(Hash.default())))
    ix = msg.instructions[-1]
    assert bytes(ix.data).hex() == expected["data"]
    assert str(msg.account_keys[ix.program_id_index]) == expected["program"]
    assert [str(msg.account_keys[i]) for i in ix.accounts] == [
        a["address"] for a in expected["accounts"]
    ]
    layout = next(
        i
        for i in SPEC["instructions"]
        if i["name"] == ("buy_exact_quote_in_v2" if side == "buy" else "sell_v2")
    )
    names = route_accounts(state)
    assert [
        {
            "address": names[a["name"]],
            "signer": a.get("signer", False),
            "writable": a.get("writable", False),
        }
        for a in layout["accounts"]
    ] == expected["accounts"]


def test_signature_is_durable_before_broadcast_and_crash_retains_reservation(setup):
    wallet, rpc, journal, service, directory = setup
    p = service.preview(wallet.address, MINT, "buy", "0.1")
    call = rpc.call

    def crash(method, params):
        if method == "sendTransaction":
            independent = TradeJournal(directory)
            try:
                row = independent.trades(wallet.address)[0]
                assert row["signature"] and row["status"] == "pending"
                assert independent.pending(wallet.address, rpc.genesis())
            finally:
                independent.close()
            raise KeyboardInterrupt("crash before transport")
        return call(method, params)

    rpc.call = crash
    with pytest.raises(KeyboardInterrupt):
        service.submit(p["id"], wallet)
    assert not rpc.sent and journal.pending(wallet.address, rpc.genesis())
    rpc.call = call
    rpc.expire = True
    service.reconcile(wallet.address)
    assert journal.trades(wallet.address)[0]["status"] == "unknown"
    assert not journal.positions(wallet.address)


@pytest.mark.parametrize(
    "change",
    [
        "token2022",
        "mint_authority",
        "freeze_authority",
        "cashback",
        "mayhem",
        "quote_mint",
        "invalid_creator_fee",
        "invalid_edit_flag",
        "invalid_holder_flag",
        "curve_owner",
        "discriminator",
    ],
)
def test_unsupported_account_contracts_fail_before_preview(setup, change):
    wallet, rpc, journal, service, _ = setup
    if change == "token2022":
        rpc.bank["mint_account"]["owner"] = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    elif change in ("mint_authority", "freeze_authority"):
        raw = bytearray(base64.b64decode(rpc.bank["mint_account"]["data"][0]))
        raw[0 if change == "mint_authority" else 46] = 1
        rpc.bank["mint_account"]["data"][0] = base64.b64encode(raw).decode()
    elif change == "curve_owner":
        rpc.bank["curve"]["owner"] = SYSTEM_PROGRAM
    else:
        raw = bytearray(base64.b64decode(rpc.bank["curve"]["data"][0]))
        raw.extend(bytes(max(0, 125 - len(raw))))
        if change == "invalid_creator_fee":
            raw[115:123] = struct.pack("<Q", 10001)
        elif change in ("invalid_edit_flag", "invalid_holder_flag"):
            raw[123 if change == "invalid_edit_flag" else 124] = 2
        else:
            raw[{"discriminator": 0, "mayhem": 81, "cashback": 82, "quote_mint": 83}[change]] ^= 1
        rpc.bank["curve"]["data"][0] = base64.b64encode(raw).decode()
    with pytest.raises(TransferError, match={
        "invalid_creator_fee": "Invalid creator fee basis points",
        "invalid_edit_flag": "Invalid Pump account flag",
        "invalid_holder_flag": "Invalid Pump account flag",
    }.get(change)):
        service.preview(wallet.address, MINT, "buy", "0.1")
    assert not rpc.sent and not journal.positions(wallet.address)
