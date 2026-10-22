from typing import Any

import pytest

from pumpwizard_runner.ownership import TOKEN_PROGRAM
from pumpwizard_runner.wallet import SolanaWalletSource, WalletDataError


ADDRESS = "So11111111111111111111111111111111111111112"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class RecordingRpc:
    def __init__(self, *, frozen: bool = False):
        self.calls: list[str] = []
        self.frozen = frozen

    def call(self, method: str, params: list[Any]) -> Any:
        self.calls.append(method)
        if method == "getBalance":
            assert params[0] == ADDRESS
            return {"context": {"slot": 123}, "value": 2_500_000_000}
        if method == "getTokenAccountsByOwner":
            assert params[0] == ADDRESS
            assert params[1] == {"mint": MINT}
            return {"context": {"slot": 124}, "value": [{
                "pubkey": "token-account-1", "account": {"owner": TOKEN_PROGRAM, "data": {
                    "parsed": {"type": "account", "info": {
                        "owner": ADDRESS, "mint": MINT,
                        "state": "frozen" if self.frozen else "initialized",
                        "tokenAmount": {"amount": "1234567", "decimals": 6},
                    }},
                }},
            }]}
        raise AssertionError((method, params))


def test_wallet_snapshot_reads_only_balance_and_explicit_tracked_token_accounts():
    source = SolanaWalletSource("https://rpc.example.test")
    rpc = RecordingRpc()
    source.rpc = rpc
    snapshot = source.snapshot(ADDRESS, {MINT}, now=100)
    assert snapshot["sol_lamports"] == "2500000000"
    assert snapshot["sol"] == 2.5
    assert snapshot["token_balances"] == {
        MINT: {"amount": "1234567", "decimals": 6, "accounts": 1, "slot": 124},
    }
    assert rpc.calls == ["getBalance", "getTokenAccountsByOwner"]
    assert "private key" not in str(snapshot).lower()


def test_wallet_snapshot_rejects_malformed_or_wrong_wallet_token_account():
    source = SolanaWalletSource("https://rpc.example.test")
    rpc = RecordingRpc()
    source.rpc = rpc
    with pytest.raises(ValueError, match="Tracked position"):
        source.snapshot(ADDRESS, {"not-a-mint"}, now=100)

    class WrongOwnerRpc(RecordingRpc):
        def call(self, method: str, params: list[Any]) -> Any:
            response = super().call(method, params)
            if method == "getTokenAccountsByOwner":
                response["value"][0]["account"]["data"]["parsed"]["info"]["owner"] = "other-wallet"
            return response

    source.rpc = WrongOwnerRpc()
    with pytest.raises(WalletDataError, match="does not match"):
        source.snapshot(ADDRESS, {MINT}, now=100)
