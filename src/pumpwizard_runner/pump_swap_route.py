"""Canonical Pump migration swaps between native SOL and supported SPL/Token-2022.

The user's wrapped-SOL ATA must be absent. Each transaction creates it, uses it
for exactly one buy or sell, and closes only that account back to the same user.
Existing wrapped SOL is never spent or unwrapped as a side effect.
"""

from __future__ import annotations

import json
from pathlib import Path
import struct

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.system_program import TransferParams, transfer

from .pump_route import (
    ATA_PROGRAM, FEE_PROGRAM, NATIVE_MINT, PUMP_PROGRAM, TOKEN_PROGRAM, TOKEN_2022_PROGRAM,
    account_bytes, ata, decode_account, key, pda, reward_settings, token_amount, wallet_balance,
)
from .transfers import SYSTEM_PROGRAM, TransferError, _uint
from .token_policy import validate_mint

SPEC = json.loads((Path(__file__).parent / "data/pump-swap-route.json").read_text())
PROGRAM = SPEC["program"]
ROUTE = "pumpswap-canonical-sol-spl-exit-v1"
TOKEN_2022_ROUTE = "pumpswap-canonical-sol-token2022-metadata-exit-v1"
ROUTES = (ROUTE, TOKEN_2022_ROUTE)


def decode(account, name, through):
    raw = account_bytes(account, PROGRAM)
    discriminator = next(a["discriminator"] for a in SPEC["accounts"] if a["name"] == name)
    if raw[:8] != bytes(discriminator):
        raise TransferError("Unexpected PumpSwap account discriminator")
    fields = next(a["type"]["fields"] for a in SPEC["types"] if a["name"] == name)
    offset = 8

    def read(kind):
        nonlocal offset
        if isinstance(kind, dict) and "array" in kind:
            return [read(kind["array"][0]) for _ in range(kind["array"][1])]
        size = {"bool": 1, "u8": 1, "u16": 2, "u64": 8, "i128": 16, "pubkey": 32}[kind]
        data = raw[offset:offset + size]
        offset += size
        if len(data) != size:
            raise TransferError("Unsupported truncated PumpSwap layout")
        if kind == "pubkey":
            from solders.pubkey import Pubkey

            return str(Pubkey.from_bytes(data))
        if kind == "bool":
            if data[0] not in (0, 1):
                raise TransferError("Invalid PumpSwap flag")
            return bool(data[0])
        return int.from_bytes(data, "little", signed=kind == "i128")

    result = {}
    for field in fields:
        result[field["name"]] = read(field["type"])
        if field["name"] == through:
            return result
    raise TransferError("Unsupported PumpSwap layout")


def canonical_pool(mint):
    authority = pda(PUMP_PROGRAM, b"pool-authority", bytes(key(mint)))
    pool = pda(PROGRAM, b"pool", bytes(2), bytes(key(authority)),
               bytes(key(mint)), bytes(key(NATIVE_MINT)))
    return pool, authority


def read_state(rpc, sender, mint, *, side="sell", min_slot, token_program=TOKEN_PROGRAM):
    if side not in ("buy", "sell"):
        raise TransferError("Unsupported PumpSwap trade side")
    pool, authority = canonical_pool(mint)
    global_address = pda(PROGRAM, b"global_config")
    curve = pda(PUMP_PROGRAM, b"bonding-curve", bytes(key(mint)))
    addresses = [global_address, pool, mint, curve, ata(pool, mint, token_program), ata(pool, NATIVE_MINT)]
    if sender is not None:
        addresses += [sender, ata(sender, mint, token_program), ata(sender, NATIVE_MINT)]
    result = rpc.call("getMultipleAccounts", [addresses, {
        "encoding": "base64", "commitment": "confirmed", "minContextSlot": min_slot,
    }])
    try:
        slot = _uint(result["context"]["slot"], "PumpSwap account slot")
        accounts = result["value"]
        if slot < min_slot or len(accounts) != len(addresses):
            raise TransferError("Incomplete or stale PumpSwap snapshot")
        if not decode_account(accounts[3], "BondingCurve", "complete")["complete"]:
            raise TransferError("Pump curve has not completed migration")
        if accounts[1] is None:
            raise TransferError("Canonical PumpSwap pool is absent; migration exit is unavailable")
        mint_data = account_bytes(accounts[2], token_program)
        validate_mint(mint_data, mint, token_program)
        config = decode(accounts[0], "GlobalConfig", "buyback_basis_points")
        pool_state = decode(accounts[1], "Pool", "is_holder_reward")
        if len(account_bytes(accounts[1], PROGRAM)) < 300:
            raise TransferError("PumpSwap pool requires an account upgrade before this route")
        disabled_flag = 8 if side == "buy" else 16
        if config["disable_flags"] & disabled_flag:
            raise TransferError(f"PumpSwap {side}s are disabled by the protocol")
        expected = {
            "index": 0, "creator": authority, "base_mint": mint, "quote_mint": NATIVE_MINT,
            "pool_base_token_account": ata(pool, mint, token_program),
            "pool_quote_token_account": ata(pool, NATIVE_MINT),
            "lp_mint": pda(PROGRAM, b"pool_lp_mint", bytes(key(pool))),
        }
        if any(pool_state[k] != v for k, v in expected.items()):
            raise TransferError("Pool is not the canonical native-SOL migration pool")
        unsupported = [k for k in ("is_mayhem_mode", "is_cashback_coin") if pool_state[k]]
        if unsupported:
            raise TransferError("Unsupported PumpSwap pool mode: " + ", ".join(unsupported))
        base_reserves = token_amount(accounts[4], mint, pool, token_program=token_program)
        if base_reserves == 0:
            raise TransferError("Canonical pool has no base liquidity")
        # Native token accounts carry a rent reserve; validate it separately from SPL base accounts.
        quote = account_bytes(accounts[5], TOKEN_PROGRAM)
        if (len(quote) != 165 or quote[:32] != bytes(key(NATIVE_MINT)) or
                quote[32:64] != bytes(key(pool)) or quote[108] != 1 or
                quote[72:76] != bytes(4) or quote[129:133] != bytes(4) or
                quote[109:113] != bytes([1, 0, 0, 0]) or
                int.from_bytes(quote[64:72], "little") == 0):
            raise TransferError("Invalid or empty native-SOL pool vault")
        quote_reserves = int.from_bytes(quote[64:72], "little")
        effective_quote_reserves = quote_reserves + pool_state["virtual_quote_reserves"]
        if effective_quote_reserves <= 0:
            raise TransferError("PumpSwap effective quote reserves are not positive")
        fee = next((x for x in config["protocol_fee_recipients"] if x != SYSTEM_PROGRAM), None)
        buyback = next((x for x in config["buyback_fee_recipients"] if x != SYSTEM_PROGRAM), None)
        if not fee or not buyback or pool_state["coin_creator"] == SYSTEM_PROGRAM:
            raise TransferError("Missing PumpSwap creator or fee recipient")
        if sender is not None and accounts[8] is not None:
            raise TransferError(
                "Existing wrapped-SOL account: this trade requires an absent wrapped-SOL ATA"
            )
        return {
            "route": TOKEN_2022_ROUTE if token_program == TOKEN_2022_PROGRAM else ROUTE,
            "token_program": token_program, "slot": slot, "sender": sender, "mint": mint,
            "pool": pool, "global": global_address, "creator": pool_state["coin_creator"],
            "fee": fee, "buyback": buyback, "decimals": 6,
            "base_reserves": base_reserves,
            "quote_reserves": quote_reserves,
            "virtual_quote_reserves": pool_state["virtual_quote_reserves"],
            "effective_quote_reserves": effective_quote_reserves,
            "amm_reserves": {
                "base_reserves": base_reserves,
                "quote_reserves": quote_reserves,
                "virtual_quote_reserves": pool_state["virtual_quote_reserves"],
                "effective_quote_reserves": effective_quote_reserves,
                "token_total_supply": int.from_bytes(mint_data[36:44], "little"),
            },
            **reward_settings(pool_state),
            "wallet_lamports": wallet_balance(accounts[6]) if sender is not None else None,
            "token_atoms": (token_amount(
                accounts[7], mint, sender, missing=side == "buy", token_program=token_program
            )
                            if sender is not None else None),
            "ata_exists": accounts[7] is not None if sender is not None else None,
        }
    except (KeyError, TypeError, IndexError):
        raise TransferError("Incomplete PumpSwap account snapshot") from None


def route_accounts(state):
    user, mint, pool = state["sender"], state["mint"], state["pool"]
    token_program = state.get("token_program", TOKEN_PROGRAM)
    creator_vault = pda(PROGRAM, b"creator_vault", bytes(key(state["creator"])))
    return {
        "pool": pool, "user": user, "global_config": state["global"],
        "base_mint": mint, "quote_mint": NATIVE_MINT,
        "user_base_token_account": ata(user, mint, token_program),
        "user_quote_token_account": ata(user, NATIVE_MINT),
        "pool_base_token_account": ata(pool, mint, token_program),
        "pool_quote_token_account": ata(pool, NATIVE_MINT),
        "protocol_fee_recipient": state["fee"],
        "protocol_fee_recipient_token_account": ata(state["fee"], NATIVE_MINT),
        "base_token_program": token_program, "quote_token_program": TOKEN_PROGRAM,
        "system_program": SYSTEM_PROGRAM, "associated_token_program": ATA_PROGRAM,
        "event_authority": pda(PROGRAM, b"__event_authority"), "program": PROGRAM,
        "coin_creator_vault_ata": ata(creator_vault, NATIVE_MINT),
        "coin_creator_vault_authority": creator_vault,
        "global_volume_accumulator": pda(PROGRAM, b"global_volume_accumulator"),
        "user_volume_accumulator": pda(
            PROGRAM, b"user_volume_accumulator", bytes(key(user))
        ),
        "fee_config": pda(FEE_PROGRAM, b"fee_config", bytes(key(PROGRAM))),
        "fee_program": FEE_PROGRAM,
    }


def build_message(state, side, amount, minimum, blockhash):
    if (state["route"] not in ROUTES or side not in ("buy", "sell") or
            not _uint(amount, "amount") or not _uint(minimum, "minimum output")):
        raise TransferError("Invalid PumpSwap trade instruction")
    names = route_accounts(state)
    layout = next(
        instruction for instruction in SPEC["instructions"]
        if instruction["name"] == ("buy_exact_quote_in" if side == "buy" else "sell")
    )
    metas = [AccountMeta(key(names[a["name"]]), a.get("signer", False), a.get("writable", False))
             for a in layout["accounts"]]
    # Official SDK 1.20.0 appends these accounts for non-cashback canonical pools.
    metas += [
        AccountMeta(key(pda(PROGRAM, b"pool-v2", bytes(key(state["mint"])))), False, False),
        AccountMeta(key(state["buyback"]), False, False),
        AccountMeta(key(ata(state["buyback"], NATIVE_MINT)), False, True),
    ]
    user, quote = key(state["sender"]), key(names["user_quote_token_account"])
    # Non-idempotent creation makes the absent-WSOL requirement atomic on chain.
    # If another transaction creates it after review, fail instead of unwrapping it.
    # Explicit price prevents wallets filling in an unreviewed priority fee.
    instructions = [set_compute_unit_limit(400_000), set_compute_unit_price(0), Instruction(key(ATA_PROGRAM), b"", [
        AccountMeta(user, True, True), AccountMeta(quote, False, True),
        AccountMeta(user, False, False), AccountMeta(key(NATIVE_MINT), False, False),
        AccountMeta(key(SYSTEM_PROGRAM), False, False), AccountMeta(key(TOKEN_PROGRAM), False, False),
    ])]
    if side == "buy":
        instructions += [
            transfer(TransferParams(from_pubkey=user, to_pubkey=quote, lamports=amount)),
            Instruction(key(TOKEN_PROGRAM), b"\x11", [AccountMeta(quote, False, True)]),
        ]
        if not state.get("ata_exists", False):
            instructions.append(Instruction(key(ATA_PROGRAM), b"\x01", [
                AccountMeta(user, True, True),
                AccountMeta(key(names["user_base_token_account"]), False, True),
                AccountMeta(user, False, False), AccountMeta(key(state["mint"]), False, False),
                AccountMeta(key(SYSTEM_PROGRAM), False, False),
                AccountMeta(key(state.get("token_program", TOKEN_PROGRAM)), False, False),
            ]))
    data = bytes(layout["discriminator"]) + struct.pack("<QQ", amount, minimum)
    if side == "buy":
        # Anchor OptionBool is a one-field tuple struct; track this purchase volume.
        data += b"\x01"
    instructions += [Instruction(key(PROGRAM), data, metas),
        Instruction(key(TOKEN_PROGRAM), b"\x09", [
            AccountMeta(quote, False, True), AccountMeta(user, False, True), AccountMeta(user, True, False),
        ])]
    message = Message.new_with_blockhash(instructions, user, Hash.from_string(blockhash))
    from .solana_message import wallet_message
    raw = wallet_message(message)
    if message.header.num_required_signatures != 1 or len(raw) + 65 > 1232:
        raise TransferError("Unsupported PumpSwap transaction size or signer set")
    return raw
