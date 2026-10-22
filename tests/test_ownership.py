from typing import Any

import pytest

from pumpwizard_runner.ownership import (
    JsonRpcClient,
    PUMP_PROGRAM,
    TOKEN_PROGRAM,
    OwnershipPolicy,
    _curve_address,
    check_mint,
)


MINT = "So11111111111111111111111111111111111111112"


class RecordingRpc:
    def __init__(self, *, mint_authority: str | None = None):
        self.mint_authority = mint_authority
        self.calls: list[str] = []

    def call(self, method: str, params: list[Any]) -> Any:
        self.calls.append(method)
        if method == "getAccountInfo" and params[0] == MINT:
            return {
                "context": {"slot": 123},
                "value": {
                    "owner": TOKEN_PROGRAM,
                    "data": {"parsed": {"type": "mint", "info": {
                        "supply": "1000", "mintAuthority": self.mint_authority, "freezeAuthority": None,
                    }}},
                },
            }
        if method == "getAccountInfo" and params[0] == _curve_address(MINT):
            return {"value": {"owner": PUMP_PROGRAM}}
        if method == "getProgramAccounts":
            holders = [_holder("curve-token-account", _curve_address(MINT), 200)]
            holders.extend(_holder(f"holder-{index}", f"wallet-{index}", 20) for index in range(40))
            return {"context": {"slot": 123}, "value": holders}
        raise AssertionError((method, params))


def _holder(pubkey: str, owner: str, amount: int) -> dict[str, Any]:
    return {
        "pubkey": pubkey,
        "account": {
            "owner": TOKEN_PROGRAM,
            "data": {"parsed": {"type": "account", "info": {
                "mint": MINT, "owner": owner, "state": "initialized", "delegate": None,
                "tokenAmount": {"amount": str(amount)},
            }}},
        },
    }


def test_local_ownership_check_can_clear_a_reconciled_distributed_mint():
    rpc = RecordingRpc()
    evidence = check_mint(MINT, rpc, OwnershipPolicy(), now=100)
    assert evidence["status"] == "clear"
    assert evidence["holder_accounts_checked"] == 41
    assert evidence["curve_vault_tokens"] == "200"
    assert evidence["top10_fraction"] == 0.25
    assert set(rpc.calls) == {"getAccountInfo", "getProgramAccounts"}


def test_local_ownership_check_blocks_an_active_mint_authority():
    evidence = check_mint(MINT, RecordingRpc(mint_authority="authority"), OwnershipPolicy(), now=100)
    assert evidence["status"] == "blocked"
    assert "Mint authority" in evidence["reasons"][0]


def test_ownership_rpc_rejects_non_https_or_embedded_credentials():
    with pytest.raises(ValueError, match="HTTPS"):
        JsonRpcClient("http://localhost:8899")
    with pytest.raises(ValueError, match="credentials"):
        JsonRpcClient("https://token@example.test/rpc")
