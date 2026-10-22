"""Explicit SOL withdrawals with a durable, restart-safe signature journal.

Only one System Program transfer plus explicit compute-budget settings can be built. Strategies and AI chat do
not call this module. Submission requires a preview approved by the desktop user.
No private keys or signed transaction payloads are persisted in the journal.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .localnet import SYSTEM_PROGRAM, _decode58, _encode58
from .wallet_vault import LocalSolanaWallet


ENDPOINTS = {
    "mainnet-beta": "https://api.mainnet-beta.solana.com",
    "devnet": "https://api.devnet.solana.com",
    "localnet": "http://127.0.0.1:8899",
}
GENESIS = {
    "mainnet-beta": "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
    "devnet": "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG",
}
METHODS = {
    "getGenesisHash", "getBalance", "getAccountInfo", "getLatestBlockhash",
    "getFeeForMessage", "getBlockHeight", "simulateTransaction", "sendTransaction",
    "getSignatureStatuses", "getMinimumBalanceForRentExemption",
}
LAMPORTS = 1_000_000_000


class TransferError(RuntimeError):
    pass


class RpcError(TransferError):
    """Fixed, credential-free RPC diagnostics. Never include a provider's text."""
    def __init__(self, method, *, http_status=None, rpc_code=None, retry_after=None):
        self.details = dict(method=method, http_status=http_status, rpc_code=rpc_code,
                            retry_after_seconds=retry_after)
        code = f'HTTP {http_status}' if http_status is not None else f'RPC code {rpc_code}'
        if http_status == 429 or rpc_code == 429:
            delay = f' Wait {retry_after} seconds before retrying.' if retry_after is not None else ' Wait before retrying.'
            message = f'RPC rate limit reached during {method} ({code}).{delay} A dedicated RPC connection can provide separate limits.'
        elif http_status in (401, 403):
            message = f'RPC access denied during {method} ({code}). Check the provider account and API key in RPC connection.'
        elif rpc_code == -32601:
            message = f'The RPC provider does not support {method}. Use a provider that supports this method.'
        elif rpc_code == -32005:
            message = 'The RPC node is behind the network. Wait or choose a healthy RPC connection.'
        else:
            message = f'RPC request {method} failed ({code}). Retry later or check the RPC connection.'
        super().__init__(message)


def parse_sol(text: str) -> int:
    try:
        amount = Decimal(text.strip())
        if not amount.is_finite() or not 0 < amount <= Decimal(2**64 - 1) / LAMPORTS:
            raise ValueError
        raw = amount * LAMPORTS
        if raw != raw.to_integral_value():
            raise ValueError
        return int(raw)
    except (InvalidOperation, ValueError, AttributeError):
        raise TransferError("Enter a positive SOL amount with at most 9 decimal places") from None


def sol_text(lamports: int) -> str:
    return format(Decimal(lamports) / LAMPORTS, "f")


def public_key(address: str) -> bytes:
    if not isinstance(address, str) or not 32 <= len(address) <= 44:
        raise TransferError("Enter a valid Solana address")
    try:
        return _decode58(address)
    except RuntimeError:
        raise TransferError("Enter a valid Solana address") from None


def transfer_message(sender: str, recipient: str, amount: int, blockhash: str) -> bytes:
    if sender == recipient or recipient == SYSTEM_PROGRAM or sender == SYSTEM_PROGRAM:
        raise TransferError("Recipient must differ from your wallet and the System Program")
    if type(amount) is not int or not 0 < amount < 2**64:
        raise TransferError("Invalid transfer amount")
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer

    # Explicit zero prevents wallet fee autofill from changing the reviewed wire.
    # Both preview fee estimation and submission use this same exact message.
    payer = Pubkey.from_bytes(public_key(sender))
    destination = Pubkey.from_bytes(public_key(recipient))
    message = Message.new_with_blockhash([
        set_compute_unit_limit(200_000), set_compute_unit_price(0),
        transfer(TransferParams(from_pubkey=payer, to_pubkey=destination, lamports=amount)),
    ], payer, Hash.from_bytes(public_key(blockhash)))
    return bytes(message)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TransferError("RPC redirects are not allowed")


class TransferRpc:
    allowed_methods = METHODS
    timeout = 12

    def __init__(self, network: str, url: str | None = None):
        if network not in ENDPOINTS:
            raise TransferError("Unsupported Solana network")
        self.network = network
        self.url = url or ENDPOINTS[network]
        parsed = urllib.parse.urlsplit(self.url)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.fragment or
                (network == "localnet" and self.url != ENDPOINTS["localnet"]) or
                (network != "localnet" and parsed.scheme != "https")):
            raise TransferError("Use HTTPS for mainnet/devnet, or the fixed localhost validator")

    def call(self, method: str, params: list):
        if method not in self.allowed_methods:
            raise TransferError("Unsupported transfer RPC method")
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        request = urllib.request.Request(self.url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response:
                body = response.read(1_000_001)
                if len(body) > 1_000_000:
                    raise TransferError("RPC response too large")
                result = json.loads(body)
        except urllib.error.HTTPError as error:
            retry = error.headers.get('Retry-After', '') if error.headers else ''
            seconds = min(int(retry), 86400) if retry.isdecimal() and len(retry) <= 6 else None
            raise RpcError(method, http_status=error.code, retry_after=seconds) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise TransferError("Solana connection unavailable; retry when connected") from None
        if isinstance(result, dict) and result.get('error'):
            error = result['error']
            code = error.get('code') if isinstance(error, dict) else None
            raise RpcError(method, rpc_code=code if type(code) is int else None)
        if not isinstance(result, dict) or "result" not in result:
            raise TransferError(f"Solana RPC rejected {method}")
        return result["result"]

    def genesis(self) -> str:
        value = self.call("getGenesisHash", [])
        if not isinstance(value, str) or not value:
            raise TransferError("RPC returned no network identity")
        if self.network in GENESIS and value != GENESIS[self.network]:
            raise TransferError("RPC network does not match the selected network")
        return value


def _uint(value, label: str) -> int:
    if type(value) is not int or not 0 <= value < 2**64:
        raise TransferError(f"Invalid {label} from Solana RPC")
    return value


@dataclass(frozen=True)
class WithdrawalPreview:
    id: str
    network: str
    genesis: str
    sender: str
    recipient: str
    lamports: int
    fee: int
    balance: int
    blockhash: str
    last_valid_height: int
    created_at: float
    funding_budget: int | None = None
    source_reserve: int = 0
    reserved_lamports: int = 0

    def message(self) -> bytes:
        return transfer_message(self.sender, self.recipient, self.lamports, self.blockhash)


class TransferJournal:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data_dir / "wallet-transfers.sqlite", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS wallet_intents (
                genesis TEXT NOT NULL, sender TEXT NOT NULL, id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL, PRIMARY KEY(genesis,sender)
            );
            CREATE TABLE IF NOT EXISTS transfers (
                id TEXT PRIMARY KEY, network TEXT NOT NULL, genesis TEXT NOT NULL,
                sender TEXT NOT NULL, recipient TEXT NOT NULL, lamports TEXT NOT NULL,
                fee TEXT NOT NULL, signature TEXT NOT NULL UNIQUE,
                last_valid_height INTEGER NOT NULL, created_at REAL NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_unsettled_withdrawal
            ON transfers(genesis,sender) WHERE status IN ('pending','unknown','confirmed');
            INSERT OR IGNORE INTO wallet_intents SELECT genesis,sender,id,'withdrawal'
                FROM transfers WHERE status IN ('pending','unknown','confirmed');
        """)
        from .reservations import SCHEMA

        self.db.executescript(SCHEMA)
        from .agent_execution import SCHEMA as AGENT_SCHEMA

        self.db.executescript(AGENT_SCHEMA)
        from .strategy_risk import SCHEMA as STRATEGY_RISK_SCHEMA

        self.db.executescript(STRATEGY_RISK_SCHEMA)
        from .session_allocations import SCHEMA as SESSION_ALLOCATION_SCHEMA

        self.db.executescript(SESSION_ALLOCATION_SCHEMA)

    def close(self):
        self.db.close()

    def rows(self, sender: str | None = None) -> list[dict]:
        if sender is None:
            rows = self.db.execute("SELECT * FROM transfers ORDER BY created_at DESC")
        else:
            rows = self.db.execute("SELECT * FROM transfers WHERE sender=? ORDER BY created_at DESC", (sender,))
        return [dict(row) for row in rows]

    def reserve(self, preview: WithdrawalPreview, signature: str, *, balance: int | None = None,
                revision: int | None = None):
        from .reservations import require_available, require_revision

        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                if not 0 <= time.time() - preview.created_at <= 90:
                    raise TransferError('Withdrawal review expired while waiting for the wallet lock')
                require_revision(self, preview.genesis, preview.sender, revision)
                require_available(self, preview.genesis, preview.sender,
                                  preview.balance if balance is None else balance, preview.lamports + preview.fee + preview.source_reserve)
                self.db.execute("INSERT INTO wallet_intents VALUES (?,?,?,?)",
                                (preview.genesis, preview.sender, preview.id, "withdrawal"))
                self.db.execute("INSERT INTO transfers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                    preview.id, preview.network, preview.genesis, preview.sender, preview.recipient,
                    str(preview.lamports), str(preview.fee), signature, preview.last_valid_height,
                    preview.created_at, "pending", "Awaiting network confirmation",
                ))
        except sqlite3.IntegrityError:
            raise TransferError("This withdrawal was already submitted or a wallet trade/withdrawal is unresolved") from None

    def update(self, transfer_id: str, status: str, detail: str):
        if status not in {"pending", "unknown", "confirmed", "finalized", "failed"}:
            raise TransferError("Invalid transfer state")
        with self.db:
            self.db.execute("UPDATE transfers SET status=?,detail=? WHERE id=? AND status NOT IN ('finalized','failed','expired')",
                            (status, detail, transfer_id))
            if status in ('finalized', 'failed'):
                self.db.execute("DELETE FROM wallet_intents WHERE id=?", (transfer_id,))


class WithdrawalService:
    def __init__(self, rpc: TransferRpc, journal: TransferJournal):
        self.rpc = rpc
        self.journal = journal

    def preview(self, sender: str, recipient: str, amount: str, *, funding_budget: bool = False) -> WithdrawalPreview:
        if type(funding_budget) is not bool:
            raise TransferError("Funding budget flag must be boolean")
        lamports = parse_sol(amount)
        requested = lamports if funding_budget else None
        public_key(sender)
        public_key(recipient)
        genesis = self.rpc.genesis()
        block = self.rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]
        blockhash = block["blockhash"]
        height = _uint(block["lastValidBlockHeight"], "block height")
        message = transfer_message(sender, recipient, lamports, blockhash)
        fee = _uint(self.rpc.call("getFeeForMessage", [base64.b64encode(message).decode(),
                    {"commitment": "confirmed"}])["value"], "fee (preview may have expired)")
        balance = _uint(self.rpc.call("getBalance", [sender, {"commitment": "confirmed"}])["value"], "balance")
        rent = _uint(self.rpc.call("getMinimumBalanceForRentExemption", [0]), "rent minimum")
        reserve = max(2_000_000, rent) if funding_budget else 0
        reserved = 0
        if funding_budget:
            from .reservations import rows
            reserved = sum(int(row["lamports"]) for row in rows(self.journal, sender)
                           if row["genesis"] == genesis and row["status"] == "held")
            budget = min(lamports, max(0, balance - reserved - reserve))
            lamports = budget - fee
            if lamports <= 0:
                raise TransferError("Not enough unreserved SOL after the funding fee and source wallet reserve")
            final_message = transfer_message(sender, recipient, lamports, blockhash)
            final_fee = _uint(self.rpc.call("getFeeForMessage", [base64.b64encode(final_message).decode(),
                              {"commitment": "confirmed"}])["value"], "funding fee")
            if final_fee != fee:
                raise TransferError("Funding fee changed; prepare a new review")
        if balance < lamports + fee:
            raise TransferError("Insufficient SOL for the amount plus network fee")
        from .reservations import require_available

        require_available(self.journal, genesis, sender, balance, lamports + fee + reserve)
        recipient_account = self.rpc.call("getAccountInfo", [recipient, {"encoding": "base64", "commitment": "confirmed"}])["value"]
        if recipient_account is not None and (recipient_account.get("owner") != SYSTEM_PROGRAM or
                                              recipient_account.get("executable") is not False):
            raise TransferError("Withdraw SOL to a wallet address, not a token or program account")
        # Account creation and a partially drained sender must remain rent exempt.
        if recipient_account is None and lamports < rent:
            raise TransferError(f"A new recipient wallet needs at least {sol_text(rent)} SOL")
        remainder = balance - lamports - fee
        if 0 < remainder < rent:
            raise TransferError(f"Leave at least {sol_text(rent)} SOL after the fee, or withdraw the exact available balance")
        return WithdrawalPreview(uuid.uuid4().hex, self.rpc.network, genesis, sender, recipient,
                                 lamports, fee, balance, blockhash, height, time.time(), requested, reserve, reserved)

    def submit(self, preview: WithdrawalPreview, wallet: LocalSolanaWallet) -> str:
        return self._submit(preview, wallet.address, wallet.sign)

    def submit_signed(self, preview: WithdrawalPreview, sender: str, transaction: str) -> str:
        from solders.signature import Signature
        from solders.pubkey import Pubkey

        try:
            raw = base64.b64decode(transaction, validate=True)
            if len(raw) != 65 + len(preview.message()) or raw[0] != 1 or raw[65:] != preview.message():
                raise ValueError
            signature = Signature.from_bytes(raw[1:65])
            if not signature.verify(Pubkey.from_string(sender), preview.message()):
                raise ValueError
        except (ValueError, TypeError):
            raise TransferError('Wallet signature or exact withdrawal message does not match the review') from None
        return self._submit(preview, sender, lambda message: raw[1:65])

    def _submit(self, preview: WithdrawalPreview, sender: str, sign) -> str:
        from .reservations import wallet_revision

        revision = wallet_revision(self.journal, preview.genesis, preview.sender)
        if preview.sender != sender or preview.network != self.rpc.network:
            raise TransferError("Wallet or network changed; request a new preview")
        if not 0 <= time.time() - preview.created_at <= 90:
            raise TransferError("Preview expired; review a new fee preview")
        if self.rpc.genesis() != preview.genesis:
            raise TransferError("Network identity changed")
        height = _uint(self.rpc.call("getBlockHeight", [{"commitment": "confirmed"}]), "block height")
        if height > preview.last_valid_height:
            raise TransferError("Blockhash expired; review a new fee preview")
        message = preview.message()
        fee = _uint(self.rpc.call("getFeeForMessage", [base64.b64encode(message).decode(),
                    {"commitment": "confirmed"}])["value"], "fee")
        balance = _uint(self.rpc.call("getBalance", [sender, {"commitment": "confirmed"}])["value"], "balance")
        if fee != preview.fee or balance != preview.balance or balance < preview.lamports + fee:
            raise TransferError("Balance or fee changed; review a new fee preview")
        from .reservations import require_available

        require_available(self.journal, preview.genesis, preview.sender, balance, preview.lamports + preview.fee + preview.source_reserve)
        # Simulate unsigned: do not expose a broadcastable signature until the intent is journaled.
        unsigned = base64.b64encode(b"\x01" + bytes(64) + message).decode()
        result = self.rpc.call("simulateTransaction", [unsigned, {
            "encoding": "base64", "commitment": "confirmed", "sigVerify": False,
        }])
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, dict) or "err" not in value or value["err"] is not None:
            raise TransferError("Transfer simulation failed; nothing was submitted")
        signed = sign(message)
        signature = _encode58(signed)
        self.journal.reserve(preview, signature, balance=balance, revision=revision)
        transaction = base64.b64encode(b"\x01" + signed + message).decode()
        try:
            received = self.rpc.call("sendTransaction", [transaction, {
                "encoding": "base64", "skipPreflight": False,
                "preflightCommitment": "confirmed", "maxRetries": 3,
            }])
            if received != signature:
                raise TransferError("RPC returned an unexpected signature")
        except Exception as error:
            # A disconnect may happen after acceptance. Never create a replacement automatically.
            # RpcError text is credential-free; never retain arbitrary provider payloads.
            diagnostic = (f" (HTTP {error.details['http_status']}, RPC code {error.details['rpc_code']})"
                          if isinstance(error, RpcError) else "")
            self.journal.update(preview.id, "unknown", f"Submission uncertain; checking the recorded signature{diagnostic}")
        return signature

    def reconcile(self, sender: str) -> list[dict]:
        genesis = self.rpc.genesis()
        rows = [row for row in self.journal.rows(sender) if row["genesis"] == genesis and
                row["status"] in {"pending", "unknown", "confirmed"}]
        for row in rows:
            result = self.rpc.call("getSignatureStatuses", [[row["signature"]], {"searchTransactionHistory": True}])
            values = result.get("value") if isinstance(result, dict) else None
            if not isinstance(values, list) or len(values) != 1:
                raise TransferError("Incomplete signature status response")
            status = values[0]
            if isinstance(status, dict) and status.get("confirmationStatus") in {"confirmed", "finalized"}:
                if "err" not in status:
                    raise TransferError("Incomplete transaction status")
                if status["err"] is not None:
                    # Wait for finality before allowing a replacement of a failed fork's transaction.
                    if status["confirmationStatus"] == "finalized":
                        self.journal.update(row["id"], "failed", "Network finalized the transaction with an error")
                else:
                    state = status["confirmationStatus"]
                    self.journal.update(row["id"], state, f"{state.title()} on Solana")
            elif status is None:
                height = _uint(self.rpc.call("getBlockHeight", [{"commitment": "finalized"}]), "block height")
                if height > row["last_valid_height"]:
                    detail = row["detail"] if row["detail"].startswith("Blockhash expired;") else (
                        "Blockhash expired; outcome unverified. Do not repeat this payment."
                        + (" " + row["detail"] if row["detail"].startswith("Submission uncertain") else ""))
                    self.journal.update(row["id"], "unknown", detail)
        return self.journal.rows(sender)
