"""Locally constructed Pump V2 swaps: native SOL, supported SPL/Token-2022 mint.

No remote transaction blob is ever accepted. Account order and flags are pinned
from the official IDL; every address is derived locally or read from Pump's global.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import struct

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from .ownership import PUMP_PROGRAM, TOKEN_PROGRAM, TOKEN_2022_PROGRAM
from .token_policy import validate_mint, validate_token_extensions
from .transfers import SYSTEM_PROGRAM, TransferError, _uint

SPEC = json.loads((Path(__file__).parent / "data/pump-route-v2.json").read_text())
FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
NATIVE_MINT = "So11111111111111111111111111111111111111112"
ROUTE = "pump-v2-native-sol-spl-v1"
TOKEN_2022_ROUTE = "pump-v2-native-sol-token2022-metadata-v1"
ROUTES = (ROUTE, TOKEN_2022_ROUTE)
MAX_FEE = 100_000
MAX_ACCOUNT_FUNDING = 20_000_000


def key(value: str) -> Pubkey:
    try:
        return Pubkey.from_string(value)
    except (ValueError, TypeError):
        raise TransferError("Invalid Solana address") from None


def pda(program: str, *seeds: bytes) -> str:
    return str(Pubkey.find_program_address(list(seeds), key(program))[0])


def ata(owner: str, mint: str, token_program: str = TOKEN_PROGRAM) -> str:
    if token_program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise TransferError("Unsupported associated token program")
    return pda(ATA_PROGRAM, bytes(key(owner)), bytes(key(token_program)), bytes(key(mint)))


def account_bytes(account: dict, owner: str) -> bytes:
    if (
        not isinstance(account, dict)
        or account.get("owner") != owner
        or account.get("executable") is not False
    ):
        raise TransferError("Unexpected account owner or executable account")
    try:
        data = account["data"]
        if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
            raise ValueError
        return base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError, KeyError):
        raise TransferError("Invalid account encoding") from None


def decode_account(account: dict, name: str, through: str) -> dict:
    raw = account_bytes(account, PUMP_PROGRAM)
    layout = next(x for x in SPEC["types"] if x["name"] == name)["type"]["fields"]
    disc = bytes(next(x for x in SPEC["accounts"] if x["name"] == name)["discriminator"])
    if raw[:8] != disc:
        raise TransferError("Unexpected Pump account discriminator")
    offset = 8

    def read(kind):
        nonlocal offset
        if isinstance(kind, dict) and "array" in kind:
            return [read(kind["array"][0]) for _ in range(kind["array"][1])]
        length = {"bool": 1, "u64": 8, "pubkey": 32}[kind]
        chunk = raw[offset : offset + length]
        offset += length
        if len(chunk) != length:
            raise TransferError("Unsupported truncated Pump account layout")
        if kind == "bool":
            if chunk[0] not in (0, 1):
                raise TransferError("Invalid Pump account flag")
            return bool(chunk[0])
        return (
            str(Pubkey.from_bytes(chunk)) if kind == "pubkey" else int.from_bytes(chunk, "little")
        )

    result = {}
    for field in layout:
        result[field["name"]] = read(field["type"])
        if field["name"] == through:
            return result
    raise TransferError("Unsupported Pump layout")


def decode_curve(account: dict) -> dict:
    """Decode complete fee/reward fields, retaining known pre-extension layouts.

    The 115-byte SOL/quote layout predates configurable fees; the 124-byte
    layout predates holder rewards. Partial fields and non-Borsh bools fail.
    See the pinned official HOLDER_REWARDS_README and BondingCurve IDL.
    """
    size = len(account_bytes(account, PUMP_PROGRAM))
    if size == 115:
        return decode_account(account, "BondingCurve", "quote_mint") | {
            "creator_fee_bps": 0, "can_edit_creator_fee": False, "is_holder_reward": False,
        }
    if size == 124:
        return decode_account(account, "BondingCurve", "can_edit_creator_fee") | {
            "is_holder_reward": False,
        }
    return decode_account(account, "BondingCurve", "is_holder_reward")


def reward_settings(decoded: dict) -> dict:
    # The effective fee also depends on the protocol's schedule/global config;
    # quote actual wallet deltas through simulation, not this field alone.
    # Retain this field and the edit flag to invalidate changed reviews.
    fee_bps = _uint(decoded["creator_fee_bps"], "creator fee basis points")
    if fee_bps > 10_000:
        raise TransferError("Invalid creator fee basis points")
    return {
        "reward_mode": "holders" if decoded["is_holder_reward"] else "creator",
        "creator_fee_bps": fee_bps,
        "can_edit_creator_fee": decoded["can_edit_creator_fee"],
    }


def token_amount(account: dict | None, mint: str, owner: str, *, missing=False,
                 token_program: str = TOKEN_PROGRAM) -> int:
    if account is None and missing:
        return 0
    raw = account_bytes(account, token_program)
    validate_token_extensions(raw, token_program)
    if (
        len(raw) < 165
        or raw[:32] != bytes(key(mint))
        or raw[32:64] != bytes(key(owner))
        or raw[108] != 1
        or raw[72:76] != bytes(4)
        or raw[129:133] != bytes(4)
        or raw[109:113] != bytes(4)
    ):
        raise TransferError("Unsupported, frozen, delegated, or mismatched token account")
    return int.from_bytes(raw[64:72], "little")


def wallet_balance(account: dict) -> int:
    if account_bytes(account, SYSTEM_PROGRAM):
        raise TransferError("Trading requires an ordinary native SOL wallet")
    return _uint(account["lamports"], "wallet balance")


def read_state(rpc, sender: str | None, mint: str, side: str = "buy") -> dict:
    """Read a coherent snapshot; a missing sender permits account inspection only."""
    if (sender is not None and not key(sender).is_on_curve()) or mint in (
        sender, SYSTEM_PROGRAM, NATIVE_MINT
    ):
        raise TransferError("Use a wallet and a supported Pump token mint")
    curve = pda(PUMP_PROGRAM, b"bonding-curve", bytes(key(mint)))
    global_address = pda(PUMP_PROGRAM, b"global")
    addresses = [global_address, curve, mint]
    if sender is not None:
        addresses += [sender, ata(sender, mint)]
    result = rpc.call(
        "getMultipleAccounts", [addresses, {"encoding": "base64", "commitment": "confirmed"}]
    )
    try:
        slot = _uint(result["context"]["slot"], "account slot")
        accounts = result["value"]
        if len(accounts) != len(addresses):
            raise TransferError("Incomplete account snapshot")
        program = accounts[2]["owner"]
        if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            raise TransferError("Unsupported mint token program")
        validate_mint(account_bytes(accounts[2], program), mint, program)
        if sender is not None and program == TOKEN_2022_PROGRAM:
            addresses[4] = ata(sender, mint, program)
            result = rpc.call("getMultipleAccounts", [addresses, {
                "encoding": "base64", "commitment": "confirmed", "minContextSlot": slot,
            }])
            next_slot = _uint(result["context"]["slot"], "token-program snapshot slot")
            accounts = result["value"]
            if next_slot < slot or len(accounts) != len(addresses):
                raise TransferError("Stale or incomplete token-program account snapshot")
            slot = next_slot
        validate_mint(account_bytes(accounts[2], program), mint, program)
        if decode_account(accounts[1], "BondingCurve", "complete")["complete"]:
            from .pump_swap_route import read_state as read_swap_state

            return read_swap_state(
                rpc, sender, mint, side=side, min_slot=slot, token_program=program
            )
        global_state = decode_account(accounts[0], "Global", "buyback_fee_recipients")
        curve_state = decode_curve(accounts[1])
        if curve_state["quote_mint"] != SYSTEM_PROGRAM:
            raise TransferError("This route supports native SOL curves only")
        if curve_state["is_mayhem_mode"] or curve_state["is_cashback_coin"]:
            raise TransferError("Mayhem and cashback curves are not supported by this route")
        creator = curve_state["creator"]
        fee = global_state["fee_recipient"]
        buyback = next(
            (x for x in global_state["buyback_fee_recipients"] if x != SYSTEM_PROGRAM), None
        )
        if creator == SYSTEM_PROGRAM or fee == SYSTEM_PROGRAM or buyback is None:
            raise TransferError("Missing creator or protocol fee recipient")
        return {
            "slot": slot,
            "sender": sender,
            "mint": mint,
            "curve": curve,
            "global": global_address,
            "creator": creator,
            "fee": fee,
            "buyback": buyback,
            "wallet_lamports": wallet_balance(accounts[3]) if sender is not None else None,
            "token_atoms": (
                token_amount(accounts[4], mint, sender, missing=True, token_program=program)
                if sender is not None else None
            ),
            "ata_exists": accounts[4] is not None if sender is not None else None,
            "decimals": 6,
            "route": TOKEN_2022_ROUTE if program == TOKEN_2022_PROGRAM else ROUTE,
            "token_program": program,
            **reward_settings(curve_state),
            "curve_reserves": {name: curve_state[name] for name in (
                "virtual_token_reserves", "virtual_quote_reserves", "real_quote_reserves", "token_total_supply")},
        }
    except (KeyError, TypeError, IndexError):
        raise TransferError("Incomplete Pump account snapshot") from None


def route_accounts(state: dict) -> dict[str, str]:
    user, mint = state["sender"], state["mint"]
    program = state.get("token_program", TOKEN_PROGRAM)
    creator_vault = pda(PUMP_PROGRAM, b"creator-vault", bytes(key(state["creator"])))
    user_volume = pda(PUMP_PROGRAM, b"user_volume_accumulator", bytes(key(user)))
    return {
        "global": state["global"],
        "base_mint": mint,
        "quote_mint": NATIVE_MINT,
        "base_token_program": program,
        "quote_token_program": TOKEN_PROGRAM,
        "associated_token_program": ATA_PROGRAM,
        "fee_recipient": state["fee"],
        "associated_quote_fee_recipient": ata(state["fee"], NATIVE_MINT),
        "buyback_fee_recipient": state["buyback"],
        "associated_quote_buyback_fee_recipient": ata(state["buyback"], NATIVE_MINT),
        "bonding_curve": state["curve"],
        "associated_base_bonding_curve": ata(state["curve"], mint, program),
        "associated_quote_bonding_curve": ata(state["curve"], NATIVE_MINT),
        "user": user,
        "associated_base_user": ata(user, mint, program),
        "associated_quote_user": ata(user, NATIVE_MINT),
        "creator_vault": creator_vault,
        "associated_creator_vault": ata(creator_vault, NATIVE_MINT),
        "sharing_config": pda(FEE_PROGRAM, b"sharing-config", bytes(key(mint))),
        "global_volume_accumulator": pda(PUMP_PROGRAM, b"global_volume_accumulator"),
        "user_volume_accumulator": user_volume,
        "associated_user_volume_accumulator": ata(user_volume, NATIVE_MINT),
        "fee_config": pda(FEE_PROGRAM, b"fee_config", bytes(key(PUMP_PROGRAM))),
        "fee_program": FEE_PROGRAM,
        "system_program": SYSTEM_PROGRAM,
        "event_authority": pda(PUMP_PROGRAM, b"__event_authority"),
        "program": PUMP_PROGRAM,
    }


def build_message(state: dict, side: str, amount: int, minimum: int, blockhash: str) -> bytes:
    if state.get("route", ROUTE) not in ROUTES:
        from .pump_swap_route import build_message as build_swap_message

        return build_swap_message(state, side, amount, minimum, blockhash)
    if (
        side not in ("buy", "sell")
        or not 0 < _uint(amount, "amount")
        or not 0 < _uint(minimum, "minimum output")
    ):
        raise TransferError("Invalid trade instruction")
    names = route_accounts(state)
    layout = next(
        i
        for i in SPEC["instructions"]
        if i["name"] == ("buy_exact_quote_in_v2" if side == "buy" else "sell_v2")
    )
    metas = [
        AccountMeta(key(names[a["name"]]), a.get("signer", False), a.get("writable", False))
        for a in layout["accounts"]
    ]
    # Explicitly encode the reviewed zero priority fee. An absent price lets
    # extension wallets fill one in, invalidating the exact reviewed message.
    instructions = [set_compute_unit_limit(400_000), set_compute_unit_price(0)]
    if side == "buy":
        # Idempotent ATA creation; no close, approve, delegate, or arbitrary transfer.
        instructions.append(
            Instruction(
                key(ATA_PROGRAM),
                b"\x01",
                [
                    AccountMeta(key(state["sender"]), True, True),
                    AccountMeta(key(names["associated_base_user"]), False, True),
                    AccountMeta(key(state["sender"]), False, False),
                    AccountMeta(key(state["mint"]), False, False),
                    AccountMeta(key(SYSTEM_PROGRAM), False, False),
                    AccountMeta(key(names["base_token_program"]), False, False),
                ],
            )
        )
    instructions.append(
        Instruction(
            key(PUMP_PROGRAM),
            bytes(layout["discriminator"]) + struct.pack("<QQ", amount, minimum),
            metas,
        )
    )
    message = Message.new_with_blockhash(
        instructions, key(state["sender"]), Hash.from_string(blockhash)
    )
    from .solana_message import wallet_message
    raw = wallet_message(message)
    if message.header.num_required_signatures != 1 or len(raw) + 65 > 1232:
        raise TransferError("Unsupported transaction size or signer set")
    return raw


def unsigned_transaction(message: bytes) -> bytes:
    return bytes(Transaction.new_unsigned(Message.from_bytes(message)))
