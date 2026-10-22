"""Explicit manual swaps with durable wallet reservations and finality accounting.

Previews cannot execute. Signing requires an unlocked wallet supplied by an
explicit review workflow. Agent orders additionally require adapter validation;
imported configuration and the AI hub cannot sign on their own.
"""

from __future__ import annotations

import base64
import hashlib
from decimal import Decimal, InvalidOperation
import json
import sqlite3
import time
import uuid

from solders.signature import Signature
from solders.transaction import Transaction

from .pump_route import (
    MAX_ACCOUNT_FUNDING,
    MAX_FEE,
    NATIVE_MINT,
    ROUTES,
    TOKEN_PROGRAM,
    TOKEN_2022_PROGRAM,
    ata,
    build_message,
    key,
    read_state,
    token_amount,
    unsigned_transaction,
    wallet_balance,
)
from .transfers import SYSTEM_PROGRAM, TransferError, TransferJournal, TransferRpc, _uint, parse_sol
from .pump_swap_route import ROUTES as SWAP_ROUTES

ACTIVE = ("pending", "unknown", "confirmed")
PREVIEW_TTL = 60


class TradeRpc(TransferRpc):
    allowed_methods = TransferRpc.allowed_methods | {"getMultipleAccounts", "getTransaction"}


def parse_tokens(text: str) -> int:
    try:
        value = Decimal(text.strip()) * 1_000_000
        if not value.is_finite() or value != value.to_integral_value() or not 0 < value < 2**64:
            raise ValueError
        return int(value)
    except (InvalidOperation, ValueError, AttributeError):
        raise TransferError("Enter a positive token quantity with at most 6 decimals") from None


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class TradeJournal(TransferJournal):
    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS trade_previews (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY, genesis TEXT NOT NULL, sender TEXT NOT NULL,
                signature TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                created_at REAL NOT NULL, payload TEXT NOT NULL, detail TEXT NOT NULL,
                receipt TEXT
            );
            CREATE TABLE IF NOT EXISTS trade_positions (
                id TEXT PRIMARY KEY, genesis TEXT NOT NULL, sender TEXT NOT NULL,
                mint TEXT NOT NULL, opened_at REAL NOT NULL, quantity TEXT NOT NULL,
                initial_quantity TEXT NOT NULL, remaining_cost TEXT NOT NULL,
                original_cost TEXT NOT NULL, realized_proceeds TEXT NOT NULL,
                realized_cost TEXT NOT NULL, status TEXT NOT NULL, last_signature TEXT NOT NULL
            );
            INSERT OR IGNORE INTO wallet_intents SELECT genesis,sender,id,'trade'
                FROM trades WHERE status IN ('pending','unknown','confirmed');
        """)
        from .wallet_risk import SCHEMA as RISK_SCHEMA

        self.db.executescript(RISK_SCHEMA)
        from .signing_sessions import SCHEMA as SESSION_SCHEMA

        self.db.executescript(SESSION_SCHEMA)
        from .automation_runtime import SCHEMA as AUTOMATION_SCHEMA

        self.db.executescript(AUTOMATION_SCHEMA)

    def previews(self, ident):
        row = self.db.execute("SELECT payload FROM trade_previews WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise TransferError("Unknown trade preview; review a fresh quote")
        return json.loads(row[0])

    def trades(self, sender):
        rows = self.db.execute(
            "SELECT * FROM trades WHERE sender=? ORDER BY created_at DESC", (sender,)
        )
        return [
            dict(
                row,
                payload=json.loads(row["payload"]),
                receipt=json.loads(row["receipt"]) if row["receipt"] else None,
            )
            for row in rows
        ]

    def positions(self, sender, genesis=None):
        rows = self.db.execute(
            "SELECT * FROM trade_positions WHERE sender=? ORDER BY opened_at DESC", (sender,)
        )
        return [dict(row) for row in rows if genesis is None or row["genesis"] == genesis]

    def position(self, ident):
        row = self.db.execute("SELECT * FROM trade_positions WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise TransferError("Select a confirmed manual position to sell")
        return dict(row)

    def pending(self, sender, genesis):
        return (
            self.db.execute(
                "SELECT 1 FROM wallet_intents WHERE sender=? AND genesis=?", (sender, genesis)
            ).fetchone()
            is not None
        )

    def reserve_trade(self, preview, signature=None, *, revision=None, signer=None):
        from .reservations import require_revision

        recorded = dict(preview)
        recorded["message_sha256"] = hashlib.sha256(
            base64.b64decode(recorded.pop("message"))
        ).hexdigest()
        recorded.pop("blockhash")
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                require_revision(self, preview['genesis'], preview['sender'], revision)
                if preview.get('agent_order'):
                    from .agent_execution import check_binding

                    check_binding(self,preview)
                self.check_agent_reservations(preview, preview['state'])
                if self.pending(preview['sender'], preview['genesis']) or self.db.execute(
                    'SELECT 1 FROM trades WHERE id=?', (preview['id'],)
                ).fetchone():
                    raise TransferError('This trade was already submitted or a wallet operation is unresolved')
                if not 0 <= time.time() - preview['created_at'] <= PREVIEW_TTL:
                    raise TransferError('Quote expired while waiting for the wallet lock; review again')
                if signer is not None:
                    signature = signer()
                self.db.execute(
                    "INSERT INTO wallet_intents VALUES (?,?,?,?)",
                    (preview["genesis"], preview["sender"], preview["id"], "trade"),
                )
                self.db.execute(
                    "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (
                        preview["id"],
                        preview["genesis"],
                        preview["sender"],
                        signature,
                        "pending",
                        time.time(),
                        encode(recorded),
                        "Signature recorded before broadcast",
                    ),
                )
                self.db.execute(
                    "UPDATE trade_previews SET payload=? WHERE id=?",
                    (encode(recorded), preview["id"]),
                )
                if preview.get('agent_order'):
                    self.db.execute("UPDATE agent_orders SET status='submitted' WHERE id=?",(preview['id'],))
        except sqlite3.IntegrityError:
            raise TransferError(
                "This trade was already submitted or a wallet trade/withdrawal is unresolved"
            ) from None

    def check_agent_reservations(self, preview, state):
        from .reservations import require_available

        sell = preview['side'] == 'sell'
        position = self.position(preview['position_id']) if sell and preview['position_id'] else None
        if position and preview.get("position_snapshot") is not None and preview["position_snapshot"] != {
            name: position[name] for name in ("quantity", "remaining_cost", "last_signature")
        }:
            raise TransferError("Selected position changed after review; request a new quote")
        debit = preview['fee'] + MAX_ACCOUNT_FUNDING + (0 if sell else preview['amount'])
        tokens = preview['amount'] if sell else 0
        if preview.get('agent_order'):
            from .agent_execution import covered_resources

            debit,tokens = covered_resources(self,preview,state)
        require_available(
            self, preview['genesis'], preview['sender'], state['wallet_lamports'],
            debit,
            mint=preview['mint'], tokens=state['token_atoms'], sell=tokens,
            position_id=position['id'] if position else None,
            position_quantity=int(position['quantity']) if position else 0,
        )
        from .wallet_risk import check_buy

        check_buy(self, preview, state)

    def trade_status(self, ident, state, detail):
        if state not in ACTIVE:
            raise TransferError("Use receipt reconciliation for a terminal trade state")
        with self.db:
            self.db.execute(
                "UPDATE trades SET status=?,detail=? WHERE id=? AND status IN ('pending','unknown','confirmed')",
                (state, detail, ident),
            )

    def settle(self, row, receipt):
        """Position and final order state commit together; replay cannot duplicate a fill."""
        p = row["payload"]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.db.execute(
                "SELECT status FROM trades WHERE id=?", (row["id"],)
            ).fetchone()
            if current[0] not in ACTIVE:
                return
            state = "failed" if receipt["err"] is not None else "finalized"
            if state == "finalized":
                quantity, cash = receipt["token_delta"], receipt["wallet_delta"]
                if p["side"] == "buy":
                    cost = -cash
                    self.db.execute(
                        "INSERT INTO trade_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            row["id"],
                            p["genesis"],
                            p["sender"],
                            p["mint"],
                            receipt["block_time"],
                            str(quantity),
                            str(quantity),
                            str(cost),
                            str(cost),
                            "0",
                            "0",
                            "open",
                            row["signature"],
                        ),
                    )
                elif p["position_id"] is not None:
                    position = self.position(p["position_id"])
                    held, sold = int(position["quantity"]), -quantity
                    if position["status"] != "open" or sold > held:
                        raise TransferError(
                            "Position reconciliation conflict; wallet remains reserved"
                        )
                    allocated = (
                        int(position["remaining_cost"])
                        if sold == held
                        else int(position["remaining_cost"]) * sold // held
                    )
                    self.db.execute(
                        """UPDATE trade_positions SET quantity=?,remaining_cost=?,realized_proceeds=?,
                        realized_cost=?,status=?,last_signature=? WHERE id=?""",
                        (
                            str(held - sold),
                            str(int(position["remaining_cost"]) - allocated),
                            str(int(position["realized_proceeds"]) + cash),
                            str(int(position["realized_cost"]) + allocated),
                            "closed" if held == sold else "open",
                            row["signature"],
                            position["id"],
                        ),
                    )
            from .agent_execution import settle_agent

            settle_agent(self,row,receipt)
            self.db.execute(
                "UPDATE trades SET status=?,detail=?,receipt=? WHERE id=?",
                (
                    state,
                    "Finalized receipt reconciled with wallet/token balances",
                    encode(receipt),
                    row["id"],
                ),
            )
            self.db.execute("DELETE FROM wallet_intents WHERE id=?", (row["id"],))


class ManualTradeService:
    def __init__(self, rpc, journal: TradeJournal, *, agent_authorizer=None):
        self.rpc, self.journal = rpc, journal
        self.agent_authorizer = agent_authorizer

    def refresh_portfolio(self, sender, genesis, side):
        from .wallet_risk import policy
        from .portfolio_risk import current, refresh

        limits = policy(self.journal, genesis, sender)
        if side == 'buy' and limits and limits.get('portfolio_drawdown_lamports') is not None:
            refresh(self.journal, self.rpc, sender)
            current(self.journal, genesis, sender)

    def _authorize_agent(self,p):
        if p.get('agent_order'):
            if self.agent_authorizer is None:
                raise TransferError('Use the agent order review workflow to authorize this trade')
            self.agent_authorizer(p)

    def _position_check(self, p, state):
        positions = [
            x
            for x in self.journal.positions(p["sender"], p["genesis"])
            if x["mint"] == p["mint"] and x["status"] == "open"
        ]
        if sum(int(x["quantity"]) for x in positions) > state["token_atoms"]:
            raise TransferError(
                "Wallet tokens are below recorded holdings. Resolve the external balance change before trading this mint."
            )
        if p["side"] == "sell" and p["position_id"] is not None:
            pos = self.journal.position(p["position_id"])
            if (pos["genesis"], pos["sender"], pos["mint"], pos["status"]) != (
                p["genesis"],
                p["sender"],
                p["mint"],
                "open",
            ) or int(pos["quantity"]) < p["amount"]:
                raise TransferError(
                    "Position changed or sell quantity exceeds its confirmed allocation"
                )
            if p.get("position_snapshot") is not None and p["position_snapshot"] != {
                name: pos[name] for name in ("quantity", "remaining_cost", "last_signature")
            }:
                raise TransferError("Selected position changed after review; request a new quote")
        elif p["side"] == "sell":
            untracked = state["token_atoms"] - sum(int(position["quantity"]) for position in positions)
            if p["amount"] > untracked:
                raise TransferError(
                    "Sell quantity exceeds tokens held outside Runner positions"
                )

    def _simulate(self, p, state, message):
        return simulate_swap(self.rpc, p, state, message)

    def preview(self, sender, mint, side, amount, slippage_bps=100, position_id=None, *, agent_order=None, persist=True):
        if agent_order is not None and (not isinstance(agent_order,dict) or not
                {'agent_id','cash_hold','quote_usd','price_usd','stages'} <= set(agent_order)):
            raise TransferError('Incomplete agent review context')
        if self.rpc.network not in ("mainnet-beta", "devnet", "localnet") or side not in (
            "buy",
            "sell",
        ):
            raise TransferError("Unsupported network or trade side")
        if type(slippage_bps) is not int or not 1 <= slippage_bps <= 500:
            raise TransferError("Choose slippage between 1 and 500 basis points (0.01–5%)")
        quantity = parse_sol(amount) if side == "buy" else parse_tokens(amount)
        if side == "buy" and quantity > 1_000_000_000:
            raise TransferError("This manual alpha caps each buy at 1 SOL")
        genesis = self.rpc.genesis()
        if self.journal.pending(sender, genesis):
            raise TransferError(
                "Resolve the existing wallet trade or withdrawal before another trade"
            )
        self.refresh_portfolio(sender, genesis, side)
        state = read_state(self.rpc, sender, mint, side)
        p = {
            "id": uuid.uuid4().hex,
            "route": state["route"],
            "network": self.rpc.network,
            "genesis": genesis,
            "sender": sender,
            "mint": mint,
            "side": side,
            "amount": quantity,
            "decimals": 6,
            "slippage_bps": slippage_bps,
            "position_id": position_id,
            "state": state,
            "created_at": time.time(),
        }
        if agent_order is not None:
            p['agent_order'] = agent_order
        self._position_check(p, state)
        if side == "sell" and position_id is not None:
            position = self.journal.position(position_id)
            p["position_snapshot"] = {
                name: position[name] for name in ("quantity", "remaining_cost", "last_signature")
            }
        block = self.rpc.call(
            "getLatestBlockhash", [{"commitment": "confirmed", "minContextSlot": state["slot"]}]
        )["value"]
        p.update(
            blockhash=block["blockhash"],
            last_valid_height=_uint(block["lastValidBlockHeight"], "block height"),
        )
        trial = build_message(state, side, quantity, 1, p["blockhash"])
        fee = self.rpc.call(
            "getFeeForMessage", [base64.b64encode(trial).decode(), {"commitment": "confirmed"}]
        )["value"]
        p["fee"] = _uint(fee, "network fee")
        if p["fee"] > MAX_FEE or state["wallet_lamports"] < p["fee"] + MAX_ACCOUNT_FUNDING + (
            quantity if side == "buy" else 0
        ):
            raise TransferError(
                "Insufficient SOL reserve or network fee exceeds the manual route limit"
            )
        self.journal.check_agent_reservations(p, state)
        output, cash = self._simulate(p, state, trial)
        p["minimum_output"] = output * (10_000 - slippage_bps) // 10_000
        if p["minimum_output"] <= 0:
            raise TransferError("Trade is too small for a nonzero minimum output")
        final = build_message(state, side, quantity, p["minimum_output"], p["blockhash"])
        confirmed_output, cash = self._simulate(p, state, final)
        if confirmed_output < p["minimum_output"]:
            raise TransferError("Market moved during quoting; request a fresh quote")
        p.update(
            expected_output=confirmed_output,
            expected_wallet_delta=cash,
            max_wallet_debit=(quantity + MAX_ACCOUNT_FUNDING + p["fee"]) if side == "buy" else 0,
            message=base64.b64encode(final).decode(),
        )
        if persist:
            with self.journal.db:
                self.journal.db.execute("INSERT INTO trade_previews VALUES (?,?)", (p["id"], encode(p)))
        return p

    def _validated_submission(self, preview_id, sender):
        from .reservations import wallet_revision

        p = self.journal.previews(preview_id)
        self._authorize_agent(p)
        revision = wallet_revision(self.journal, p['genesis'], p['sender'])
        if (p["sender"] != sender or p["network"] != self.rpc.network or
                p["route"] not in (*ROUTES, *SWAP_ROUTES)):
            raise TransferError("Wallet, network, or route changed; review again")
        if not 0 <= time.time() - p["created_at"] <= PREVIEW_TTL:
            raise TransferError("Quote expired; review a fresh quote")
        if self.rpc.genesis() != p["genesis"] or self.journal.pending(sender, p["genesis"]):
            raise TransferError("Network changed or another wallet operation is unresolved")
        if self.journal.db.execute("SELECT 1 FROM trades WHERE id=?", (preview_id,)).fetchone():
            raise TransferError("This preview has already been submitted")
        self.refresh_portfolio(sender, p['genesis'], p['side'])
        state = read_state(self.rpc, sender, p["mint"], p["side"])
        if state["route"] != p["route"]:
            raise TransferError("Route changed after review; request a new quote")
        if any(state[name] != p["state"].get(name) for name in (
            "reward_mode", "creator_fee_bps", "can_edit_creator_fee",
        )):
            raise TransferError("Reward mode or creator-fee settings changed after review; request a new quote")
        if any(state.get(name) != p["state"].get(name) for name in (
            "curve_reserves", "amm_reserves",
        )):
            raise TransferError(
                "Executable on-chain curve or AMM reserves changed after review; request a new quote"
            )
        self._position_check(p, state)
        self.journal.check_agent_reservations(p, state)
        if (state["wallet_lamports"], state["token_atoms"]) != (
            p["state"]["wallet_lamports"],
            p["state"]["token_atoms"],
        ):
            raise TransferError("Wallet balance changed after review; request a new quote")
        message = build_message(state, p["side"], p["amount"], p["minimum_output"], p["blockhash"])
        if base64.b64encode(message).decode() != p["message"]:
            raise TransferError("Route accounts changed after review; request a new quote")
        output, cash = self._simulate(p, state, message)
        if output < p["minimum_output"] or (p["side"] == "buy" and -cash > p["max_wallet_debit"]):
            raise TransferError("Fresh simulation exceeds the approved quote limits")
        # Agent validation can perform additional unsigned exit simulations.
        # Check the reviewed message's lifetime after that work, before signing.
        self._authorize_agent(p)
        height = _uint(
            self.rpc.call("getBlockHeight", [{"commitment": "confirmed"}]), "block height"
        )
        fee = _uint(
            self.rpc.call("getFeeForMessage", [p["message"], {"commitment": "confirmed"}])["value"],
            "fee",
        )
        if (
            height > p["last_valid_height"]
            or fee > p["fee"]
            or not 0 <= time.time() - p["created_at"] <= PREVIEW_TTL
        ):
            raise TransferError("Quote or blockhash expired, or fee increased; review again")
        return p, revision, state, message

    def _broadcast_signed(self, p, state, raw, signature):
        try:
            received = self.rpc.call(
                "sendTransaction",
                [
                    base64.b64encode(raw).decode(),
                    {
                        "encoding": "base64",
                        "skipPreflight": False,
                        "preflightCommitment": "confirmed",
                        "minContextSlot": state["slot"],
                        "maxRetries": 3,
                    },
                ],
            )
            if received != str(signature):
                raise TransferError("Unexpected transaction signature returned")
        except Exception:
            self.journal.trade_status(
                p["id"],
                "unknown",
                "Submission uncertain. Checking the original signature; never re-sign automatically.",
            )
        return str(signature)

    def submit(self, preview_id, wallet):
        p, revision, state, message = self._validated_submission(preview_id, wallet.address)
        signed = signature = None

        def sign_checked():
            nonlocal signed, signature
            # Risk policy and balances cannot change in another local process
            # between the final checks, signing, and durable wallet exclusion.
            if not 0 <= time.time() - p['created_at'] <= PREVIEW_TTL:
                raise TransferError('Quote expired while waiting for the wallet lock')
            signed = wallet.sign(message)
            signature = Signature.from_bytes(signed)
            if not signature.verify(key(wallet.address), message):
                raise TransferError("Signer did not produce the approved wallet signature")
            return str(signature)

        self.journal.reserve_trade(p, revision=revision, signer=sign_checked)
        raw = b"\x01" + signed + message
        before_broadcast = getattr(wallet, "before_broadcast", None)
        if before_broadcast is not None:
            try:
                before_broadcast()
            except TransferError:
                self.journal.trade_status(
                    p["id"], "unknown",
                    "Signed and recorded, but automation stopped before broadcast. "
                    "Check only the original signature; never create a replacement automatically.",
                )
                raise
        return self._broadcast_signed(p, state, raw, signature)

    def submit_signed(self, preview_id, sender, signed_transaction):
        """Validate, reserve and broadcast one transaction signed by a browser wallet."""
        if not isinstance(signed_transaction, str) or not 1 <= len(signed_transaction) <= 4096:
            raise TransferError("Browser wallet returned an invalid signed transaction")
        p, revision, state, message = self._validated_submission(preview_id, sender)
        try:
            raw = base64.b64decode(signed_transaction, validate=True)
            transaction = Transaction.from_bytes(raw)
            signatures = list(transaction.signatures)
        except Exception:
            raise TransferError("Browser wallet returned an unreadable signed transaction") from None
        if (
            len(signatures) != 1
            or bytes(transaction.message) != message
            or raw != b"\x01" + bytes(signatures[0]) + message
            or not signatures[0].verify(key(sender), message)
        ):
            raise TransferError("Browser wallet did not sign the exact reviewed transaction")
        signature = str(signatures[0])
        self.journal.reserve_trade(p, signature, revision=revision)
        return self._broadcast_signed(p, state, raw, signatures[0])

    def reconcile(self, sender):
        genesis = self.rpc.genesis()
        for row in self.journal.trades(sender):
            if row["genesis"] != genesis or row["status"] not in ACTIVE:
                continue
            try:
                values = self.rpc.call(
                    "getSignatureStatuses", [[row["signature"]], {"searchTransactionHistory": True}]
                )["value"]
                if not isinstance(values, list) or len(values) != 1:
                    raise TransferError("Invalid signature status response")
                status = values[0]
                if status is None:
                    height = _uint(
                        self.rpc.call("getBlockHeight", [{"commitment": "finalized"}]), "height"
                    )
                    if height > row["payload"]["last_valid_height"]:
                        self.journal.trade_status(
                            row["id"],
                            "unknown",
                            "Blockhash expired; outcome unverified. The wallet remains reserved.",
                        )
                    continue
                if status.get("confirmationStatus") not in ("confirmed", "finalized"):
                    continue
                if status["confirmationStatus"] == "confirmed":
                    self.journal.trade_status(
                        row["id"], "confirmed", "Confirmed; awaiting finalized transaction balances"
                    )
                    continue
                transaction = self.rpc.call(
                    "getTransaction",
                    [
                        row["signature"],
                        {
                            "encoding": "base64",
                            "commitment": "finalized",
                            "maxSupportedTransactionVersion": 0,
                        },
                    ],
                )
                if transaction is None:
                    self.journal.trade_status(
                        row["id"],
                        "confirmed",
                        "Finalized signature; transaction receipt is not available yet",
                    )
                    continue
                receipt = validate_receipt(row, transaction)
                self.journal.settle(row, receipt)
            except (TransferError, KeyError, ValueError, TypeError, IndexError):
                self.journal.trade_status(
                    row["id"],
                    "unknown",
                    "Receipt unavailable or inconsistent; wallet remains reserved until evidence reconciles.",
                )
        return self.journal.trades(sender)


def validate_receipt(row, result):
    p = row["payload"]
    encoded = result["transaction"]
    if not isinstance(encoded, list) or encoded[1] != "base64":
        raise TransferError("Unsupported transaction receipt encoding")
    tx = Transaction.from_bytes(base64.b64decode(encoded[0], validate=True))
    message = bytes(tx.message)
    if (
        hashlib.sha256(message).hexdigest() != p["message_sha256"]
        or len(tx.signatures) != 1
        or str(tx.signatures[0]) != row["signature"]
        or not tx.signatures[0].verify(key(p["sender"]), message)
    ):
        raise TransferError("Receipt transaction differs from the authorized message")
    meta = result["meta"]
    keys = [str(k) for k in tx.message.account_keys]
    if (
        len(meta["preBalances"]) != len(keys)
        or len(meta["postBalances"]) != len(keys)
        or keys[0] != p["sender"]
    ):
        raise TransferError("Incomplete receipt balances")
    fee = _uint(meta["fee"], "receipt fee")
    if fee > p["fee"]:
        raise TransferError("Receipt fee exceeds approved limit")
    cash = _uint(meta["postBalances"][0], "post balance") - _uint(
        meta["preBalances"][0], "pre balance"
    )
    token_program = p["state"].get("token_program", TOKEN_PROGRAM)
    token_index = keys.index(ata(p["sender"], p["mint"], token_program))

    def balance(field, missing=False):
        entries = [x for x in meta[field] if x["accountIndex"] == token_index]
        if not entries and missing:
            return 0
        if len(entries) != 1:
            raise TransferError("Missing or duplicate receipt token balance")
        entry = entries[0]
        if (
            entry["mint"] != p["mint"]
            or entry["owner"] != p["sender"]
            or entry["uiTokenAmount"]["decimals"] != 6
            or entry.get("programId", token_program) != token_program
            or (token_program == TOKEN_2022_PROGRAM and "programId" not in entry)
        ):
            raise TransferError("Receipt token identity differs from approved mint/wallet")
        return _uint(int(entry["uiTokenAmount"]["amount"]), "token balance")

    err = meta["err"]
    if err is None:
        if p["route"] in SWAP_ROUTES:
            quote_index = keys.index(ata(p["sender"], NATIVE_MINT))
            if (meta["preBalances"][quote_index] != 0 or meta["postBalances"][quote_index] != 0 or
                    any(x["accountIndex"] == quote_index for field in
                        ("preTokenBalances", "postTokenBalances") for x in meta[field])):
                raise TransferError("Wrapped-SOL account was not newly created and closed")
        pre = balance(
            "preTokenBalances", missing=p["side"] == "buy" and not p["state"]["ata_exists"]
        )
        post = balance("postTokenBalances")
        tokens = post - pre
        if p["side"] == "buy":
            if tokens < p["minimum_output"] or not 0 < -cash <= p["max_wallet_debit"]:
                raise TransferError("Confirmed buy differs from approved limits")
        elif tokens != -p["amount"] or cash < p["minimum_output"] - fee - MAX_ACCOUNT_FUNDING:
            raise TransferError("Confirmed sell differs from approved limits")
    else:
        tokens = 0
        if cash != -fee:
            raise TransferError("Failed transaction did not reconcile to fee-only debit")
    return {
        "err": err,
        "fee_lamports": fee,
        "wallet_delta": cash,
        "token_delta": tokens,
        "slot": _uint(result["slot"], "receipt slot"),
        "block_time": result.get("blockTime") or time.time(),
        "signature": row["signature"],
        "route": p["route"],
        "cost_basis": "net wallet SOL cash debit including transaction fees and account funding",
    }


def closed_simulation_account(account):
    """RPC may retain an empty system account immediately after CloseAccount."""
    if account is None:
        return True
    return (isinstance(account, dict)
            and type(account.get('lamports')) is int and account['lamports'] == 0
            and account.get('owner') == SYSTEM_PROGRAM
            and account.get('executable') is False
            and account.get('data') == ['', 'base64']
            and type(account.get('space', 0)) is int and account.get('space', 0) == 0)


def simulate_swap(rpc, p, state, message):
    token_program = state.get("token_program", TOKEN_PROGRAM)
    addresses = [p["sender"], ata(p["sender"], p["mint"], token_program)]
    if state["route"] in SWAP_ROUTES:
        addresses.append(ata(p["sender"], NATIVE_MINT))
    result = rpc.call(
        "simulateTransaction",
        [
            base64.b64encode(unsigned_transaction(message)).decode(),
            {
                "encoding": "base64",
                "commitment": "confirmed",
                "sigVerify": False,
                "minContextSlot": state["slot"],
                "accounts": {
                    "encoding": "base64",
                    "addresses": addresses,
                },
            },
        ],
    )
    try:
        if _uint(result["context"]["slot"], "simulation slot") < state["slot"]:
            raise TransferError("Simulation is older than the account snapshot")
        value = result["value"]
        if "err" not in value or value["err"] is not None:
            raise TransferError(
                "Swap simulation failed. Refresh the quote; nothing was signed or submitted."
            )
        accounts = value["accounts"]
        if len(accounts) != len(addresses):
            raise TransferError("Simulation did not return wallet and token balances")
        if state["route"] in SWAP_ROUTES and not closed_simulation_account(accounts[2]):
            raise TransferError("Simulation did not unwrap the sale proceeds")
        cash = wallet_balance(accounts[0]) - state["wallet_lamports"]
        tokens = token_amount(accounts[1], p["mint"], p["sender"], token_program=token_program) - state["token_atoms"]
        if p["side"] == "buy":
            if (
                tokens <= 0
                or not 0 < -cash <= p["amount"] + MAX_ACCOUNT_FUNDING + p["fee"]
            ):
                raise TransferError("Simulated buy balance changes exceed route limits")
            output = tokens
        else:
            if tokens != -p["amount"] or cash <= 0:
                raise TransferError(
                    "Simulated sell has no positive net SOL proceeds or wrong token quantity"
                )
            # Conservative route payout estimate; account funding can reduce it.
            output = cash + p["fee"]
        return output, cash
    except (KeyError, TypeError, IndexError):
        raise TransferError("Incomplete swap simulation evidence") from None
