"""Repeatable public-account and unsigned-simulation evidence, without a vault or journal."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib

from solders.transaction import Transaction
from solders.instruction import AccountMeta, Instruction
from solders.message import Message

from .manual_trades import parse_tokens, simulate_swap
from .pump_route import (
    ATA_PROGRAM, FEE_PROGRAM, MAX_ACCOUNT_FUNDING, MAX_FEE, PUMP_PROGRAM, SPEC,
    TOKEN_PROGRAM, TOKEN_2022_PROGRAM, ROUTES, ata, build_message, key, read_state,
    token_amount, unsigned_transaction, wallet_balance,
)
from .pump_swap_route import PROGRAM as SWAP_PROGRAM
from .transfers import RpcError, TransferError, TransferRpc, _uint, parse_sol


class ProbeRpcError(TransferError):
    pass


class RouteCheckRpc(TransferRpc):
    # Independent allowlist: never inherit sendTransaction from the trading RPC.
    allowed_methods = frozenset({
        "getGenesisHash", "getMultipleAccounts", "getLatestBlockhash",
        "getFeeForMessage", "getBlockHeight", "simulateTransaction",
    })

    def __init__(self, network, url=None):
        super().__init__(network, url)
        self.simulations = []
        self.snapshots = []
        self.rpc_errors = []

    def call(self, method, params):
        if method not in self.allowed_methods:
            raise ProbeRpcError("Route checks allow public reads and unsigned simulation only")
        if method == "simulateTransaction":
            tx = Transaction.from_bytes(base64.b64decode(params[0], validate=True))
            if any(bytes(s) != bytes(64) for s in tx.signatures) or params[1].get("sigVerify") is not False:
                raise ProbeRpcError("Route checks reject signed transactions")
        try:
            result = super().call(method, params)
        except RpcError as error:
            self.rpc_errors.append(error.details)
            raise ProbeRpcError(str(error)) from None
        except TransferError:
            raise ProbeRpcError(f"RPC unavailable or rejected {method}") from None
        if method == "simulateTransaction" and isinstance(result, dict):
            value = result.get("value") or {}
            self.simulations.append({
                "slot": result.get("context", {}).get("slot"), "err": value.get("err"),
                "units_consumed": value.get("unitsConsumed"),
            })
        if method == "getMultipleAccounts" and isinstance(result, dict):
            self.snapshots.append({
                "slot": result.get("context", {}).get("slot"),
                "accounts": [{"address": address, "owner": a.get("owner") if a else None,
                              "data_bytes": len(base64.b64decode(a["data"][0])) if a else None}
                             for address, a in zip(params[0], result.get("value") or [])],
            })
        return result


def check_routes(rpc, mints, *, sender=None, side="buy", amount=None, round_trip=False):
    """No key, signature, signed payload, user data directory, or real position is accessed."""
    mints = list(dict.fromkeys(mints))
    if not 1 <= len(mints) <= 20 or side not in ("buy", "sell"):
        raise TransferError("Check 1–20 mints and select buy or sell")
    for mint in mints:
        key(mint)
    if sender is not None and not key(sender).is_on_curve():
        raise TransferError("Simulation requires an on-curve public sender address")
    if (sender is None) != (amount is None):
        raise TransferError("Provide both public sender and amount for unsigned simulation")
    if round_trip and (sender is None or side != "buy"):
        raise TransferError("Round-trip probes require a public sender and a buy amount")
    quantity = (parse_sol(amount) if side == "buy" else parse_tokens(amount)) if amount is not None else None
    if side == "buy" and quantity is not None and quantity > 1_000_000_000:
        raise TransferError("Unsigned buy probes are capped at 1 SOL")
    report = {
        "schema": "solanamarket.route-check/v1",
        "checked_at": datetime.now(timezone.utc).isoformat(), "network": rpc.network,
        "mode": "unsigned_simulation" if sender else "account_inspection",
        "interface_commit": SPEC["commit"], "pump_idl_sha256": SPEC["idl_sha256"],
        "funded_acceptance": False, "submission_enabled": False, "results": [],
    }
    if round_trip:
        report["mode"] = "unsigned_round_trip"
    try:
        report["genesis"] = rpc.genesis()
        programs = [PUMP_PROGRAM, SWAP_PROGRAM, FEE_PROGRAM, TOKEN_PROGRAM, TOKEN_2022_PROGRAM, ATA_PROGRAM]
        snapshot = rpc.call("getMultipleAccounts", [programs, {
            "encoding": "base64", "commitment": "confirmed", "dataSlice": {"offset": 0, "length": 0},
        }])
        _uint(snapshot["context"]["slot"], "program slot")
        if len(snapshot["value"]) != len(programs) or any(
            not isinstance(a, dict) or a.get("executable") is not True for a in snapshot["value"]
        ):
            raise TransferError("Required executable programs are missing")
        report["programs"] = {"slot": snapshot["context"]["slot"], "addresses": programs}
    except (TransferError, KeyError, TypeError, IndexError):
        report.update(status="network_check_failed", detail="Network identity or executable-program check failed",
                      rpc_errors=rpc.rpc_errors)
        return report
    for mint in mints:
        result = {"mint": mint, "side": side, "status": "account_check_failed"}
        if sender is not None:
            result.update(sender=sender, amount=quantity)
        stage = "account_check"
        try:
            state = read_state(rpc, sender, mint, side)
            result.update(route=state["route"], token_program=state["token_program"],
                          slot=state["slot"], status="accounts_supported",
                          reward_mode=state["reward_mode"], creator_fee_bps=state["creator_fee_bps"],
                          can_edit_creator_fee=state["can_edit_creator_fee"])
            if sender is not None:
                stage = "simulation"
                if side == "sell" and state["token_atoms"] < quantity:
                    raise TransferError("Public sender does not hold the requested token quantity")
                block = rpc.call("getLatestBlockhash", [{
                    "commitment": "confirmed", "minContextSlot": state["slot"],
                }])["value"]
                trial = build_message(state, side, quantity, 1, block["blockhash"])
                fee = _uint(rpc.call("getFeeForMessage", [
                    base64.b64encode(trial).decode(), {"commitment": "confirmed"},
                ])["value"], "network fee")
                if fee > MAX_FEE or state["wallet_lamports"] < fee + MAX_ACCOUNT_FUNDING + (
                    quantity if side == "buy" else 0
                ):
                    raise TransferError("Public sender has insufficient SOL reserve or excessive fee")
                p = {"sender": sender, "mint": mint, "side": side, "amount": quantity, "fee": fee}
                output, _ = simulate_swap(rpc, p, state, trial)
                minimum = output * 9900 // 10000
                if minimum <= 0:
                    raise TransferError("Output is too small")
                final = build_message(state, side, quantity, minimum, block["blockhash"])
                output, cash = simulate_swap(rpc, p, state, final)
                if output < minimum or _uint(rpc.call("getBlockHeight", [
                    {"commitment": "confirmed"},
                ]), "height") > _uint(block["lastValidBlockHeight"], "last valid height"):
                    raise TransferError("Quote moved or blockhash expired during probe")
                result.update(status="unsigned_simulation_passed", sender=sender, amount=quantity,
                              minimum_output=minimum, expected_output=output, wallet_delta=cash,
                              fee_lamports=fee, message_sha256=hashlib.sha256(final).hexdigest())
                if round_trip:
                    result["round_trip"] = simulate_round_trip(rpc, state, final, output, quantity, block)
        except ProbeRpcError:
            result.update(status="rpc_unavailable", detail=f"RPC unavailable during {stage}")
        except TransferError as exc:
            result.update(status=f"{stage}_failed", detail=str(exc))
        except (ValueError, KeyError, TypeError, IndexError):
            result.update(status=f"{stage}_failed", detail="Malformed route or RPC evidence")
        report["results"].append(result)
    report["status"] = "passed" if all(r["status"] in (
        "accounts_supported", "unsigned_simulation_passed"
    ) for r in report["results"]) else "incomplete"
    report["snapshots"] = rpc.snapshots
    report["simulations"] = rpc.simulations
    report["rpc_errors"] = rpc.rpc_errors
    return report


def round_trip_message(state, buy_message, sell_amount, blockhash):
    """Compose only our pinned curve buy + sell builders, without signing capability."""
    if state["route"] not in ROUTES:
        raise TransferError("Round-trip probe supports active curves only")
    buy = Message.from_bytes(buy_message)
    sell = Message.from_bytes(build_message(state, "sell", sell_amount, 1, blockhash))

    def expand(message, compiled):
        def writable(index):
            header = message.header
            if index < header.num_required_signatures:
                return index < header.num_required_signatures - header.num_readonly_signed_accounts
            return index < len(message.account_keys) - header.num_readonly_unsigned_accounts

        return Instruction(message.account_keys[compiled.program_id_index], bytes(compiled.data), [
            AccountMeta(message.account_keys[i], message.is_signer(i), writable(i))
            for i in compiled.accounts
        ])

    instructions = [expand(buy, ix) for ix in buy.instructions]
    instructions.append(expand(sell, sell.instructions[-1]))
    result = Message.new_with_blockhash(instructions, key(state["sender"]), buy.recent_blockhash)
    raw = bytes(result)
    if result.header.num_required_signatures != 1 or len(raw) + 65 > 1232:
        raise TransferError("Round-trip probe exceeds transaction size or signer limits")
    return raw


def simulate_round_trip(rpc, state, buy_message, buy_output, spend, block):
    # Leave 10% to tolerate a small quote movement between independent simulations.
    # This is diagnostic only: a one-lamport sell floor is never offered for signing.
    sell_amount = buy_output * 9 // 10
    if not sell_amount:
        raise TransferError("Round-trip probe output is too small")
    message = round_trip_message(state, buy_message, sell_amount, block["blockhash"])
    fee = _uint(rpc.call("getFeeForMessage", [base64.b64encode(message).decode(), {
        "commitment": "confirmed",
    }])["value"], "round-trip fee")
    if fee > MAX_FEE or state["wallet_lamports"] < spend + fee + MAX_ACCOUNT_FUNDING:
        raise TransferError("Round-trip probe exceeds fee or SOL reserve bounds")
    result = rpc.call("simulateTransaction", [base64.b64encode(unsigned_transaction(message)).decode(), {
        "encoding": "base64", "commitment": "confirmed", "sigVerify": False,
        "minContextSlot": state["slot"], "accounts": {"encoding": "base64", "addresses": [
            state["sender"], ata(state["sender"], state["mint"], state["token_program"]),
        ]},
    }])
    slot = _uint(result["context"]["slot"], "round-trip slot")
    value = result["value"]
    if slot < state["slot"] or value.get("err", "missing") is not None:
        raise TransferError("Unsigned buy-then-sell simulation failed")
    if not isinstance(value.get("accounts"), list) or len(value["accounts"]) != 2:
        raise TransferError("Missing round-trip balance evidence")
    cash = wallet_balance(value["accounts"][0]) - state["wallet_lamports"]
    tokens = token_amount(value["accounts"][1], state["mint"], state["sender"],
                          token_program=state["token_program"]) - state["token_atoms"]
    # Do not allow a diagnostic partial sell to consume pre-existing tokens.
    if tokens <= 0 or not 0 < -cash <= spend + fee + MAX_ACCOUNT_FUNDING:
        raise TransferError("Round-trip simulation returned unexpected wallet/token changes")
    if _uint(rpc.call("getBlockHeight", [{"commitment": "confirmed"}]), "height") > block["lastValidBlockHeight"]:
        raise TransferError("Round-trip blockhash expired")
    return {"status": "unsigned_buy_then_sell_passed", "slot": slot,
            "sell_amount": sell_amount, "diagnostic_minimum_sell_lamports": 1,
            "wallet_delta": cash, "retained_token_atoms": tokens, "fee_lamports": fee,
            "message_sha256": hashlib.sha256(message).hexdigest()}
