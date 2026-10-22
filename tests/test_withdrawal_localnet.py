"""Opt-in real transaction test; only connects to localhost and creates test wallets."""
import json
import os
import time
import urllib.request

import pytest

from pumpwizard_runner.transfers import TransferJournal, TransferRpc, WithdrawalService
from pumpwizard_runner.wallet_vault import WalletVault


@pytest.mark.skipif(os.environ.get("RUN_WITHDRAWAL_LOCALNET") != "1", reason="requires localhost Solana validator")
def test_receive_withdraw_restore_and_restart(tmp_path):
    password = "localnet throwaway password"
    vault = WalletVault(tmp_path / "first")
    wallet = vault.create(password)
    backup = tmp_path / "encrypted-backup.json"
    vault.export_backup(backup, password)
    restored = WalletVault(tmp_path / "restored").restore_backup(backup, password)
    recipient = WalletVault(tmp_path / "recipient").create(password)
    rpc = TransferRpc("localnet")
    assert rpc.call("getBalance", [wallet.address])["value"] == 0
    # Test-only faucet method is deliberately absent from the production client.
    request = urllib.request.Request("http://127.0.0.1:8899", data=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "requestAirdrop", "params": [wallet.address, 1_000_000_000],
    }).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert "result" in json.loads(response.read())
    deadline = time.monotonic() + 30
    while rpc.call("getBalance", [wallet.address, {"commitment": "confirmed"}])["value"] != 1_000_000_000:
        assert time.monotonic() < deadline, "local faucet did not confirm"
        time.sleep(.25)
    journal = TransferJournal(tmp_path / "journal")
    service = WithdrawalService(rpc, journal)
    preview = service.preview(restored.address, recipient.address, "0.05")
    signature = service.submit(preview, restored)
    journal.close()
    # Restart loses all in-memory submission state; recover only from the durable signature.
    journal = TransferJournal(tmp_path / "journal")
    try:
        service = WithdrawalService(rpc, journal)
        deadline = time.monotonic() + 45
        while True:
            rows = service.reconcile(restored.address)
            assert len(rows) == 1 and rows[0]["signature"] == signature
            if rows[0]["status"] == "finalized":
                break
            assert rows[0]["status"] != "failed"
            assert time.monotonic() < deadline, "local transfer did not finalize"
            time.sleep(.4)
        assert rpc.call("getBalance", [recipient.address, {"commitment": "finalized"}])["value"] == 50_000_000
        assert rpc.call("getBalance", [wallet.address, {"commitment": "finalized"}])["value"] == 950_000_000 - preview.fee
    finally:
        journal.close()
