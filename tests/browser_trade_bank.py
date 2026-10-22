"""Offline RPC adapter shared by browser/API tests; never contacts Solana."""
from types import SimpleNamespace
import base64
import uuid
import struct
import time

from solders.hash import Hash
from solders.message import Message
from solders.transaction import Transaction
from solders.signature import Signature

from pumpwizard_runner.transfers import GENESIS, TransferError
from pumpwizard_runner.ownership import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from pumpwizard_runner.pump_route import ata
from test_pump_swap_route import SwapRpc
from test_manual_trades import MINT

PROCESS_RUN_ID = uuid.uuid4().hex


class BrowserBank(SwapRpc):
    network = "mainnet-beta"

    def __init__(self, address):
        super().__init__(SimpleNamespace(address=address))
        self.balance = 1_250_000_000
        self.receipts = {}
        self.statuses = {}
        self.failed = False

    def genesis(self):
        return GENESIS[self.network]

    def external_sell(self):
        amount = self.tokens
        if amount <= 0:
            raise AssertionError("Synthetic external sell requires a token balance")
        signature = str(Signature.from_bytes(uuid.uuid4().bytes * 4))
        proceeds = amount // 10 - self.fee

        def token(quantity):
            return {
                "accountIndex": 1, "mint": MINT, "owner": self.wallet.address,
                "programId": self.token_program,
                "uiTokenAmount": {"amount": str(quantity), "decimals": 6},
            }

        self.receipts[signature] = {
            "slot": 22, "blockTime": int(time.time()),
            "transaction": {"message": {"accountKeys": [
                {"pubkey": self.wallet.address, "signer": True, "writable": True},
            ]}},
            "meta": {
                "err": None, "fee": self.fee,
                "preBalances": [self.balance], "postBalances": [self.balance + proceeds],
                "preTokenBalances": [token(amount)], "postTokenBalances": [],
            },
        }
        self.statuses[signature] = {"confirmationStatus": "finalized", "err": None}
        self.balance += proceeds
        self.tokens = 0
        return signature

    def call(self, method, params):
        if method == 'getAccountInfo':
            return {'value': {'owner': '11111111111111111111111111111111', 'executable': False}}
        if method == 'getMinimumBalanceForRentExemption':
            return 890880
        if method in ('simulateTransaction', 'sendTransaction'):
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            transfers = [ix for ix in tx.message.instructions
                         if str(tx.message.account_keys[ix.program_id_index]) == '11111111111111111111111111111111']
            transfer_only = len(transfers) == 1 and all(
                str(tx.message.account_keys[ix.program_id_index]) in {
                    '11111111111111111111111111111111',
                    'ComputeBudget111111111111111111111111111111',
                } for ix in tx.message.instructions
            )
            if transfer_only:
                if method == 'simulateTransaction':
                    return {'value': {'err': 'synthetic error' if self.bad_sim else None}}
                signature = str(tx.signatures[0])
                amount = struct.unpack('<IQ', bytes(transfers[0].data))[1]
                self.balance -= self.fee + (0 if self.failed else amount)
                self.sent.append(base64.b64decode(params[0]))
                self.receipts[signature] = {'meta': {'err': 'synthetic error' if self.failed else None}}
                self.statuses[signature] = None
                if self.disconnect:
                    raise TransferError('Synthetic timeout after accepting a SOL transfer')
                return signature
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.new_unique()), "lastValidBlockHeight": 100}}
        if method == "getTokenAccountsByOwner":
            program = params[1]["programId"]
            values = []
            if program == self.token_program and self.tokens:
                values.append({
                    "pubkey": ata(self.wallet.address, MINT, program),
                    "account": {"owner": program, "data": {"parsed": {"info": {
                        "mint": MINT, "owner": self.wallet.address,
                        "tokenAmount": {"amount": str(self.tokens), "decimals": 6},
                    }}}},
                })
            assert program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)
            return {"context": {"slot": 21}, "value": values}
        if method == "getSignaturesForAddress":
            rows = []
            for signature, receipt in reversed(list(self.receipts.items())):
                status = self.statuses.get(signature)
                if status is None:
                    continue
                rows.append({
                    "signature": signature, "slot": receipt.get("slot", 21),
                    "blockTime": receipt.get("blockTime"), "err": status.get("err"),
                    "memo": None, "confirmationStatus": status.get("confirmationStatus"),
                })
            return rows[:params[1]["limit"]]
        if method == "getSignatureStatuses":
            return {"value": [self.statuses.get(signature) for signature in params[0]]}
        if method == "getTransaction":
            return self.receipts.get(params[0])
        if method != "sendTransaction":
            return super().call(method, params)
        # Always retain the synthetic node's receipt, even when its response is lost.
        disconnect = self.disconnect
        self.disconnect = False
        before_balance, before_tokens = self.balance, self.tokens
        try:
            signature = super().call(method, params)
        finally:
            self.disconnect = disconnect
        if self.failed:
            self.balance, self.tokens = before_balance - self.fee, before_tokens
            meta = self.receipt["meta"]
            meta["err"] = {"InstructionError": [1, "Custom"]}
            meta["postBalances"] = list(meta["preBalances"])
            meta["postBalances"][0] = self.balance
            meta["postTokenBalances"] = meta["preTokenBalances"]
        self.receipts[signature] = self.receipt
        self.statuses[signature] = None
        if disconnect:
            raise TransferError("Synthetic timeout after accepting the transaction")
        return signature


class SyntheticChain:
    def __init__(self):
        self.banks = {}

    def bank(self, address):
        if address not in self.banks:
            self.banks[address] = BrowserBank(address)
        return self.banks[address]

    def rpc(self, network, directory=None):
        assert network == "mainnet-beta"
        chain = self

        class Connection:
            def __init__(self):
                self.network = network
                self.selected = None

            def genesis(self):
                return GENESIS[network]

            def call(self, method, params):
                if method == 'getLatestBlockhash' and self.selected is None:
                    return {'value': {'blockhash': str(Hash.new_unique()), 'lastValidBlockHeight': 100}}
                if method == 'getBlockHeight' and self.selected is None:
                    return 1
                if method == "getBalance":
                    self.selected = chain.bank(params[0])
                elif method in ("getTokenAccountsByOwner", "getSignaturesForAddress"):
                    self.selected = chain.bank(params[0])
                elif method == 'getFeeForMessage':
                    message = Message.from_bytes(base64.b64decode(params[0]))
                    self.selected = chain.bank(str(message.account_keys[0]))
                elif method == "getMultipleAccounts" and len(params[0]) == 5:
                    self.selected = chain.bank(params[0][3])
                elif method in ("simulateTransaction", "sendTransaction"):
                    tx = Transaction.from_bytes(base64.b64decode(params[0]))
                    self.selected = chain.bank(str(tx.message.account_keys[0]))
                elif method in ("getSignatureStatuses", "getTransaction"):
                    signature = params[0][0] if method == "getSignatureStatuses" else params[0]
                    self.selected = next(bank for bank in chain.banks.values() if signature in bank.receipts)
                assert self.selected is not None, method
                return self.selected.call(method, params)

        return Connection()

    def control(self, address, changes):
        bank = self.bank(address)
        for name, value in changes.items():
            if name == "finalize":
                for signature, receipt in bank.receipts.items():
                    bank.statuses[signature] = {"confirmationStatus": "finalized", "err": receipt["meta"]["err"]}
            elif name in ("unsupported_cashback", "holder_rewards", "creator_fee_bps", "editable_creator_fee"):
                raw = bytearray(base64.b64decode(bank.bank["curve"]["data"][0]))
                raw.extend(bytes(max(0, 125 - len(raw))))
                pool = bytearray(bank.pool_raw)
                if name == "creator_fee_bps":
                    raw[115:123] = pool[261:269] = struct.pack("<Q", value)
                else:
                    curve_offset, pool_offset = {"unsupported_cashback": (82, 244),
                        "holder_rewards": (124, 270), "editable_creator_fee": (123, 269)}[name]
                    raw[curve_offset] = pool[pool_offset] = int(value)
                if name == "holder_rewards":
                    from pumpwizard_runner.pump_route import PUMP_PROGRAM, key, pda
                    creator = bytes(key(pda(PUMP_PROGRAM, b"holder-rewards", bytes(key(MINT))))) if value else bytes([7]) * 32
                    raw[49:81] = pool[211:243] = creator
                bank.bank["curve"]["data"][0] = base64.b64encode(raw).decode()
                bank.pool_raw = bytes(pool)
            elif name in ("disconnect", "bad_sim", "expire", "graduated", "failed", "balance", "tokens"):
                setattr(bank, name, value)
            elif name == "external_sell" and value is True:
                bank.external_sell()
            else:
                raise ValueError(f"Unknown synthetic control: {name}")
        return {"mint": MINT, "sent": len(bank.sent), "balance": str(bank.balance), "tokens": str(bank.tokens), "process_id": PROCESS_RUN_ID}
