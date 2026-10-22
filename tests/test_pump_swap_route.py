"""Synthetic migration lifecycle plus an independent official-SDK instruction oracle."""

import base64
import copy
import json
from pathlib import Path
import struct

import pytest
from solders.hash import Hash
from solders.message import Message

from pumpwizard_runner.manual_trades import ManualTradeService, TradeJournal
from pumpwizard_runner.pump_route import (
    NATIVE_MINT, TOKEN_PROGRAM, PUMP_PROGRAM, ata, key, pda,
)
from pumpwizard_runner.pump_swap_route import (
    PROGRAM, ROUTE, TOKEN_2022_ROUTE, build_message, canonical_pool, route_accounts,
)
from pumpwizard_runner.transfers import SYSTEM_PROGRAM, TransferError
from pumpwizard_runner.wallet_vault import WalletVault
from test_manual_trades import MINT, Rpc, account, buy, token


def pool_bytes(mint, token_program=TOKEN_PROGRAM):
    pool, authority = canonical_pool(mint)
    raw = bytes([241, 154, 109, 4, 17, 177, 109, 188]) + struct.pack("<BH", 255, 0)
    for address in (authority, mint, NATIVE_MINT,
                    pda(PROGRAM, b"pool_lp_mint", bytes(key(pool))),
                    ata(pool, mint, token_program), ata(pool, NATIVE_MINT)):
        raw += bytes(key(address))
    return (raw + struct.pack("<Q", 1_000_000) + bytes([7]) * 32).ljust(300, b"\0")


def config_bytes():
    raw = bytes([149, 8, 156, 202, 160, 252, 176, 217])
    raw += bytes([3]) * 32 + struct.pack("<QQB", 20, 5, 0)
    raw += bytes([8]) * 256 + struct.pack("<Q", 5)
    raw += bytes(96) + bytes(1) + bytes(224) + bytes(1)
    raw += bytes([6]) * 256 + struct.pack("<Q", 5)
    return raw.ljust(949, b"\0")


class SwapRpc(Rpc):
    def __init__(self, wallet):
        super().__init__(wallet)
        self.pool_raw, self.config_raw = pool_bytes(MINT, self.token_program), config_bytes()
        self.pool_base_tokens = 500_000_000_000
        self.pool_quote_tokens = 30_000_000_000
        self.quote_exists, self.missing_pool, self.old_slot = False, False, False

    def decode_trade(self, raw):
        from solders.transaction import Transaction

        tx = Transaction.from_bytes(raw)
        ix = next(i for i in tx.message.instructions
                  if str(tx.message.account_keys[i.program_id_index]) in (PUMP_PROGRAM, PROGRAM))
        data = bytes(ix.data)
        amount, minimum = struct.unpack("<QQ", data[8:24])
        return tx, amount, minimum, data[:8] in (
            bytes([194, 171, 28, 70, 104, 77, 91, 47]),
            bytes([198, 46, 21, 82, 180, 217, 232, 112]),
        )

    def call(self, method, params):
        if method == "getMultipleAccounts" and params[0][0] == pda(PROGRAM, b"global_config"):
            pool, _ = canonical_pool(MINT)
            curve = copy.deepcopy(self.bank["curve"])
            raw = bytearray(base64.b64decode(curve["data"][0]))
            raw[48] = int(self.graduated)
            curve["data"][0] = base64.b64encode(raw).decode()
            quote = bytearray(base64.b64decode(token(pool, self.pool_quote_tokens)["data"][0]))
            quote[:32], quote[109:121] = bytes(key(NATIVE_MINT)), struct.pack("<IQ", 1, 2_039_280)
            accounts = [account(PROGRAM, self.config_raw),
                        None if self.missing_pool else account(PROGRAM, self.pool_raw),
                        self.bank["mint_account"], curve,
                        token(pool, self.pool_base_tokens, self.token_program),
                        account(TOKEN_PROGRAM, quote, self.pool_quote_tokens + 2_039_280)]
            if len(params[0]) == 9:
                accounts += [account(SYSTEM_PROGRAM, lamports=self.balance),
                             (token(self.wallet.address, self.tokens, self.token_program)
                              if self.tokens else None),
                             account(TOKEN_PROGRAM, quote) if self.quote_exists else None]
            return {"context": {"slot": 19 if self.old_slot else 20}, "value": accounts}
        result = super().call(method, params)
        if method == "simulateTransaction" and len(params[1]["accounts"]["addresses"]) == 3:
            result["value"]["accounts"].append(None)
        if method == "sendTransaction" and self.graduated:
            tx, _, _, _ = self.decode_trade(base64.b64decode(params[0]))
            index = list(tx.message.account_keys).index(key(ata(self.wallet.address, NATIVE_MINT)))
            self.receipt["meta"]["preBalances"][index] = 0
            self.receipt["meta"]["postBalances"][index] = 0
        return result


@pytest.fixture
def setup(tmp_path):
    wallet = WalletVault(tmp_path / "vault").create("test migration password")
    journal = TradeJournal(tmp_path)
    rpc = SwapRpc(wallet)
    yield wallet, rpc, journal, ManualTradeService(rpc, journal), tmp_path
    journal.close()


@pytest.mark.parametrize("token2022", [False, True])
@pytest.mark.parametrize("holder", [False, True])
def test_sdk_oracle_matches_every_instruction_and_compiled_privilege(token2022, holder):
    fixture = json.loads((Path(__file__).parent / "fixtures/pump-route" /
                          f"swap-sdk-{'holder-' if holder else ''}{'token2022-' if token2022 else ''}instructions.json").read_text())
    state = dict(fixture["inputs"], route=TOKEN_2022_ROUTE if token2022 else ROUTE)
    message = Message.from_bytes(build_message(state, "sell", 400_000_000, 39_000_000, str(Hash.default())))
    # Compare all instructions after compute budget, including ATA creation and close-to-self.
    assert len(message.instructions) == 5
    privileges = {}
    for expected, ix in zip(fixture["instructions"], message.instructions[2:]):
        assert str(message.account_keys[ix.program_id_index]) == expected["program"]
        assert bytes(ix.data).hex() == expected["data"]
        assert [str(message.account_keys[i]) for i in ix.accounts] == [a["address"] for a in expected["accounts"]]
        for a in expected["accounts"]:
            signer, writable = privileges.get(a["address"], (False, False))
            privileges[a["address"]] = signer or a["signer"], writable or a["writable"]
    for address, (signer, writable) in privileges.items():
        index = list(message.account_keys).index(key(address))
        assert message.is_signer(index) == signer
        signed = message.header.num_required_signatures
        actual = (index < signed - message.header.num_readonly_signed_accounts if index < signed else
                  index < len(message.account_keys) - message.header.num_readonly_unsigned_accounts)
        assert actual == writable
    assert len(bytes(message)) + 65 <= 1232


@pytest.mark.parametrize("token2022", [False, True])
def test_exact_quote_buy_matches_official_sdk_account_contract(token2022):
    fixture = json.loads((Path(__file__).parent / "fixtures/pump-route" /
                          f"swap-sdk-{'token2022-' if token2022 else ''}instructions.json").read_text())
    state = dict(
        fixture["inputs"],
        route=TOKEN_2022_ROUTE if token2022 else ROUTE,
        ata_exists=False,
    )
    message = Message.from_bytes(
        build_message(state, "buy", 100_000_000, 900_000_000, str(Hash.default()))
    )
    assert len(message.instructions) == len(fixture["buy_instructions"]) + 2
    privileges = {}
    for expected_instruction, instruction in zip(
        fixture["buy_instructions"], message.instructions[2:]
    ):
        assert str(message.account_keys[instruction.program_id_index]) == expected_instruction[
            "program"
        ]
        assert bytes(instruction.data).hex() == expected_instruction["data"]
        assert [str(message.account_keys[index]) for index in instruction.accounts] == [
            expected_account["address"]
            for expected_account in expected_instruction["accounts"]
        ]
        for expected_account in expected_instruction["accounts"]:
            signer, writable = privileges.get(
                expected_account["address"], (False, False)
            )
            privileges[expected_account["address"]] = (
                signer or expected_account["signer"],
                writable or expected_account["writable"],
            )
    for address, (signer, writable) in privileges.items():
        index = list(message.account_keys).index(key(address))
        assert message.is_signer(index) == signer
        signed = message.header.num_required_signatures
        actual = (
            index < signed - message.header.num_readonly_signed_accounts
            if index < signed
            else index < len(message.account_keys) - message.header.num_readonly_unsigned_accounts
        )
        assert actual == writable
    programs = [str(message.account_keys[ix.program_id_index]) for ix in message.instructions]
    assert programs == [
        "ComputeBudget111111111111111111111111111111",
        "ComputeBudget111111111111111111111111111111",
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        SYSTEM_PROGRAM,
        TOKEN_PROGRAM,
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        PROGRAM,
        TOKEN_PROGRAM,
    ]
    assert bytes(message.instructions[3].data) == struct.pack("<IQ", 2, 100_000_000)
    assert bytes(message.instructions[4].data) == b"\x11"
    swap = message.instructions[6]
    assert bytes(swap.data) == (
        bytes([198, 46, 21, 82, 180, 217, 232, 112])
        + struct.pack("<QQ", 100_000_000, 900_000_000)
        + b"\x01"
    )
    expected = route_accounts(state)
    layout = next(x for x in json.loads(
        (Path(__file__).parents[1] / "src/pumpwizard_runner/data/pump-swap-route.json").read_text()
    )["instructions"] if x["name"] == "buy_exact_quote_in")
    expected_addresses = [expected[x["name"]] for x in layout["accounts"]] + [
        pda(PROGRAM, b"pool-v2", bytes(key(state["mint"]))),
        state["buyback"],
        ata(state["buyback"], NATIVE_MINT),
    ]
    assert [str(message.account_keys[i]) for i in swap.accounts] == expected_addresses
    assert len(bytes(message)) + 65 <= 1232


def test_buy_graduate_partial_exit_restart_full_exit(setup):
    wallet, rpc, journal, service, path = setup
    p, _ = buy(setup)
    original = journal.position(p["id"])
    rpc.graduated = True
    preview = service.preview(wallet.address, MINT, "sell", "400", position_id=p["id"])
    assert preview["route"] == ROUTE
    service.submit(preview["id"], wallet)
    reopened = TradeJournal(path)
    try:
        recovered = ManualTradeService(rpc, reopened)
        recovered.reconcile(wallet.address)
        recovered.reconcile(wallet.address)
        assert reopened.position(p["id"])["quantity"] == "600000000"
        assert reopened.trades(wallet.address)[0]["receipt"]["route"] == ROUTE
        preview = recovered.preview(wallet.address, MINT, "sell", "600", position_id=p["id"])
        recovered.submit(preview["id"], wallet)
        recovered.reconcile(wallet.address)
        position = reopened.position(p["id"])
        assert position["status"] == "closed" and position["quantity"] == "0"
        assert position["remaining_cost"] == "0"
        assert position["realized_cost"] == original["original_cost"]
        assert not reopened.pending(wallet.address, rpc.genesis())
        assert len(rpc.sent) == 3
    finally:
        reopened.close()


def test_graduation_after_review_requires_new_review_before_signing(setup):
    wallet, rpc, journal, service, _ = setup
    p, _ = buy(setup)
    preview = service.preview(wallet.address, MINT, "sell", "400", position_id=p["id"])
    rpc.graduated = True
    with pytest.raises(TransferError, match="Route changed"):
        service.submit(preview["id"], wallet)
    assert len(rpc.sent) == 1 and not journal.pending(wallet.address, rpc.genesis())


@pytest.mark.parametrize('mutation', [None, 'lamports', 'owner', 'data', 'executable', 'space', 'bool_lamports', 'missing_owner'])
def test_live_closed_account_representation_and_invalid_variants(setup, monkeypatch, mutation):
    wallet, rpc, journal, service, _ = setup
    p, _ = buy(setup)
    rpc.graduated = True
    fixture = json.loads((Path(__file__).parent / 'fixtures/pump-route/closed-simulation-account.json').read_text())
    closed = fixture['closed_wrapped_sol_account']
    changes = {'lamports': 1, 'owner': TOKEN_PROGRAM, 'data': ['AA==', 'base64'],
               'executable': True, 'space': 165, 'bool_lamports': False}
    if mutation == 'missing_owner':
        del closed['owner']
    elif mutation:
        closed['lamports' if mutation == 'bool_lamports' else mutation] = changes[mutation]
    call = rpc.call

    def response(method, params):
        result = call(method, params)
        if method == 'simulateTransaction':
            result['value']['accounts'][2] = copy.deepcopy(closed)
        return result

    monkeypatch.setattr(rpc, 'call', response)
    if mutation:
        with pytest.raises(TransferError, match='did not unwrap'):
            service.preview(wallet.address, MINT, 'sell', '400', position_id=p['id'])
        assert len(rpc.sent) == 1
    else:
        preview = service.preview(wallet.address, MINT, 'sell', '400', position_id=p['id'])
        service.submit(preview['id'], wallet)
        service.reconcile(wallet.address)
        assert journal.position(p['id'])['quantity'] == '600000000'
        assert len(rpc.sent) == 2


@pytest.mark.parametrize("mutation", ["pool", "quote", "stale", "wrong_mint", "disabled", "cashback", "creator"])
def test_bad_migration_state_never_signs_or_submits(setup, mutation):
    wallet, rpc, journal, service, _ = setup
    p, _ = buy(setup)
    rpc.graduated = True
    if mutation == "pool":
        rpc.missing_pool = True
    elif mutation == "quote":
        rpc.quote_exists = True
    elif mutation == "stale":
        rpc.old_slot = True
    elif mutation == "disabled":
        raw = bytearray(rpc.config_raw)
        raw[56] = 16
        rpc.config_raw = bytes(raw)
    else:
        raw = bytearray(rpc.pool_raw)
        offset = {"wrong_mint": 43, "cashback": 244, "creator": 211}[mutation]
        if mutation == "creator":
            raw[offset:offset + 32] = bytes(32)
        else:
            raw[offset] ^= 1
        rpc.pool_raw = bytes(raw)
    with pytest.raises(TransferError):
        service.preview(wallet.address, MINT, "sell", "400", position_id=p["id"])
    assert len(rpc.sent) == 1 and not journal.pending(wallet.address, rpc.genesis())


def test_unwrapped_receipt_mismatch_keeps_wallet_reserved(setup):
    wallet, rpc, journal, service, _ = setup
    p, _ = buy(setup)
    rpc.graduated = True
    preview = service.preview(wallet.address, MINT, "sell", "400", position_id=p["id"])
    service.submit(preview["id"], wallet)
    tx, _, _, _ = rpc.decode_trade(rpc.sent[-1])
    index = list(tx.message.account_keys).index(key(ata(wallet.address, NATIVE_MINT)))
    rpc.receipt["meta"]["preBalances"][index] = 1
    service.reconcile(wallet.address)
    assert journal.pending(wallet.address, rpc.genesis())
    assert journal.position(p["id"])["quantity"] == "1000000000"


def test_graduated_exact_quote_buy_creates_and_reconciles_position(setup):
    wallet, rpc, journal, service, _ = setup
    rpc.graduated = True
    preview = service.preview(wallet.address, MINT, "buy", "0.1")
    assert preview["route"] == ROUTE and preview["amount"] == 100_000_000
    message = Message.from_bytes(base64.b64decode(preview["message"]))
    instruction = next(
        ix for ix in message.instructions
        if str(message.account_keys[ix.program_id_index]) == PROGRAM
    )
    amount, minimum = struct.unpack("<QQ", bytes(instruction.data)[8:24])
    assert amount == 100_000_000 and minimum == preview["minimum_output"]
    assert bytes(instruction.data)[-1:] == b"\x01"
    service.submit(preview["id"], wallet)
    rpc.status = {"confirmationStatus": "finalized", "err": None}
    service.reconcile(wallet.address)
    position = journal.position(preview["id"])
    assert position["status"] == "open" and int(position["quantity"]) > 0


def test_graduated_buy_reuses_existing_base_ata_but_not_wrapped_sol(setup):
    wallet, rpc, _, service, _ = setup
    rpc.graduated = True
    rpc.tokens = 25_000_000
    preview = service.preview(wallet.address, MINT, "buy", "0.1")
    message = Message.from_bytes(base64.b64decode(preview["message"]))
    programs = [
        str(message.account_keys[instruction.program_id_index])
        for instruction in message.instructions
    ]
    assert programs.count("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL") == 1


def test_protocol_buy_disable_flag_blocks_graduated_entry(setup):
    wallet, rpc, _, service, _ = setup
    rpc.graduated = True
    raw = bytearray(rpc.config_raw)
    raw[56] = 8
    rpc.config_raw = bytes(raw)
    with pytest.raises(TransferError, match="buys are disabled"):
        service.preview(wallet.address, MINT, "buy", "0.1")
    assert not rpc.sent


@pytest.mark.parametrize("virtual", [-1_000_000_000, 1_000_000_000])
def test_signed_virtual_quote_reserves_are_supported_and_validated(setup, virtual):
    wallet, rpc, _, _, _ = setup
    rpc.graduated = True
    raw = bytearray(rpc.pool_raw)
    raw[245:261] = virtual.to_bytes(16, "little", signed=True)
    rpc.pool_raw = bytes(raw)
    from pumpwizard_runner.pump_swap_route import read_state as swap_state

    state = swap_state(rpc, wallet.address, MINT, side="buy", min_slot=20)
    assert state["virtual_quote_reserves"] == virtual
    assert state["effective_quote_reserves"] == 30_000_000_000 + virtual
    raw[245:261] = (-30_000_000_000).to_bytes(16, "little", signed=True)
    rpc.pool_raw = bytes(raw)
    with pytest.raises(TransferError, match="effective quote reserves"):
        swap_state(rpc, wallet.address, MINT, side="buy", min_slot=20)


def test_curve_complete_is_rechecked_in_pool_snapshot(setup):
    wallet, rpc, _, _, _ = setup
    from pumpwizard_runner.pump_swap_route import read_state as swap_state

    with pytest.raises(TransferError, match="not completed"):
        swap_state(rpc, wallet.address, MINT, min_slot=20)


def test_read_only_market_inspection_needs_no_wallet(setup):
    _, rpc, _, _, _ = setup
    rpc.graduated = True
    from pumpwizard_runner.pump_swap_route import read_state as swap_state

    state = swap_state(rpc, None, MINT, min_slot=20)
    assert state["route"] == ROUTE and state["wallet_lamports"] is None
