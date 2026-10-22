"""Service custody uses isolated fixture keys and never a real browser vault."""

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest
from solders.hash import Hash
from solders.message import Message
from solders.pubkey import Pubkey

from pumpwizard_runner.pump_route import unsigned_transaction
from pumpwizard_runner.service_wallet_vault import (
    KDF_ITERATIONS,
    SERVICE_CUSTODY,
    SERVICE_VAULT_SCHEMA,
    ServiceVaultAuthenticationError,
    ServiceVaultError,
    ServiceWalletVault,
)
from pumpwizard_runner.wallet_vault import WalletVault


PASSWORD = "fixture service password"


def envelope(wallet, *, password=PASSWORD, network="mainnet-beta", vault_id="12" * 16):
    identity = {
        "schema": SERVICE_VAULT_SCHEMA,
        "vault_id": vault_id,
        "custody": SERVICE_CUSTODY,
        "address": wallet.address,
        "network": network,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    salt, nonce = bytes(range(16)), bytes(range(12))
    key = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, KDF_ITERATIONS, 32)
    aad = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ciphertext = AESGCM(key).encrypt(nonce, wallet._seed, aad)
    return {
        **identity,
        "kdf": {
            "name": "PBKDF2",
            "hash": "SHA-256",
            "iterations": KDF_ITERATIONS,
            "salt": base64.b64encode(salt).decode(),
        },
        "cipher": {
            "name": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        },
    }


def fixture_wallet(tmp_path, name="source"):
    return WalletVault(tmp_path / name).create("fixture browser password")


def ticket(wallet, allowance_id="34" * 16, authorization_revision=1):
    message = bytes(Message.new_with_blockhash(
        [], Pubkey.from_string(wallet.address), Hash.default()
    ))
    wire = unsigned_transaction(message)
    return {
        "schema": "pumpwizard.main-wallet-signing-ticket/v1",
        "id": "56" * 16,
        "allowance_id": allowance_id,
        "order_id": "78" * 16,
        "sender": wallet.address,
        "authorization_revision": authorization_revision,
        "wire_sha256": hashlib.sha256(wire).hexdigest(),
        "message_sha256": hashlib.sha256(message).hexdigest(),
        "unsigned_transaction": base64.b64encode(wire).decode(),
        "created_at": time.time(),
        "expires_at": time.time() + 30,
    }


def test_enroll_persists_only_ciphertext_and_unlocks_exact_ticket_signer(tmp_path):
    wallet = fixture_wallet(tmp_path)
    vault = ServiceWalletVault(tmp_path)
    metadata = vault.enroll(
        envelope(wallet),
        PASSWORD,
        expected_address=wallet.address,
        expected_network="mainnet-beta",
    )
    path = tmp_path / "service-wallets" / f"{wallet.address}.json"
    persisted = path.read_text()
    assert metadata == {
        "schema": SERVICE_VAULT_SCHEMA,
        "vault_id": "12" * 16,
        "custody": SERVICE_CUSTODY,
        "address": wallet.address,
        "network": "mainnet-beta",
        "created_at": json.loads(persisted)["created_at"],
        "state": "locked",
    }
    assert base64.b64encode(wallet._seed).decode() not in persisted
    assert "fixture service password" not in persisted
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700

    signer = vault.unlock(
        wallet.address,
        "mainnet-beta",
        PASSWORD,
        duration_seconds=60,
        allowance_id="34" * 16,
        authorization_revision=1,
    )
    reviewed = ticket(wallet)
    signed = base64.b64decode(signer.sign_ticket(reviewed))
    unsigned = base64.b64decode(reviewed["unsigned_transaction"])
    assert signed[0] == 1 and signed[65:] == unsigned[65:]
    assert wallet.sign(signed[65:]) == signed[1:65]
    with pytest.raises(ServiceVaultError, match="another service allowance"):
        signer.sign_ticket(ticket(wallet, allowance_id="90" * 16))
    signer.lock()
    with pytest.raises(ServiceVaultAuthenticationError, match="locked"):
        signer.sign_ticket(ticket(wallet))


def test_wrong_password_tampering_address_and_network_are_rejected(tmp_path):
    wallet = fixture_wallet(tmp_path)
    vault = ServiceWalletVault(tmp_path)
    record = envelope(wallet)
    with pytest.raises(ServiceVaultAuthenticationError, match="password"):
        vault.enroll(
            record,
            "incorrect service password",
            expected_address=wallet.address,
            expected_network="mainnet-beta",
        )
    changed = json.loads(json.dumps(record))
    changed["cipher"]["ciphertext"] = base64.b64encode(bytes(48)).decode()
    with pytest.raises(ServiceVaultAuthenticationError, match="password"):
        vault.enroll(
            changed,
            PASSWORD,
            expected_address=wallet.address,
            expected_network="mainnet-beta",
        )
    other = fixture_wallet(tmp_path, "other")
    with pytest.raises(ServiceVaultError, match="wallet or network changed"):
        vault.enroll(
            record,
            PASSWORD,
            expected_address=other.address,
            expected_network="mainnet-beta",
        )
    with pytest.raises(ServiceVaultError, match="wallet or network changed"):
        vault.enroll(
            record,
            PASSWORD,
            expected_address=wallet.address,
            expected_network="devnet",
        )


def test_duplicate_wallet_and_vault_identity_collision_never_overwrite(tmp_path):
    first = fixture_wallet(tmp_path, "first")
    second = fixture_wallet(tmp_path, "second")
    vault = ServiceWalletVault(tmp_path)
    vault.enroll(
        envelope(first),
        PASSWORD,
        expected_address=first.address,
        expected_network="mainnet-beta",
    )
    original = (tmp_path / "service-wallets" / f"{first.address}.json").read_bytes()
    with pytest.raises(ServiceVaultError, match="already enrolled"):
        vault.enroll(
            envelope(first),
            PASSWORD,
            expected_address=first.address,
            expected_network="mainnet-beta",
        )
    assert (tmp_path / "service-wallets" / f"{first.address}.json").read_bytes() == original
    with pytest.raises(ServiceVaultError, match="identity already belongs"):
        vault.enroll(
            envelope(second),
            PASSWORD,
            expected_address=second.address,
            expected_network="mainnet-beta",
        )


def test_interrupted_write_and_unsafe_paths_fail_closed(tmp_path, monkeypatch):
    wallet = fixture_wallet(tmp_path)
    vault = ServiceWalletVault(tmp_path)

    def interrupted(*_args):
        raise OSError("fixture interruption")

    monkeypatch.setattr(os, "link", interrupted)
    with pytest.raises(ServiceVaultError, match="Could not save"):
        vault.enroll(
            envelope(wallet),
            PASSWORD,
            expected_address=wallet.address,
            expected_network="mainnet-beta",
        )
    assert not (tmp_path / "service-wallets" / f"{wallet.address}.json").exists()
    assert not list((tmp_path / "service-wallets").glob("*.tmp"))

    unsafe = tmp_path / "unsafe"
    (unsafe / "service-wallets").mkdir(parents=True, mode=0o755)
    (unsafe / "service-wallets").chmod(0o755)
    with pytest.raises(ServiceVaultError, match="group or others"):
        ServiceWalletVault(unsafe)._root()


@pytest.mark.parametrize("field", ["vault_id", "created_at", "address", "network"])
def test_encrypted_identity_mutation_and_corrupt_records_never_unlock(tmp_path, field):
    wallet = fixture_wallet(tmp_path)
    vault = ServiceWalletVault(tmp_path)
    vault.enroll(
        envelope(wallet),
        PASSWORD,
        expected_address=wallet.address,
        expected_network="mainnet-beta",
    )
    path = tmp_path / "service-wallets" / f"{wallet.address}.json"
    record = json.loads(path.read_text())
    if field == "vault_id":
        record[field] = "90" * 16
    elif field == "created_at":
        record[field] = "2026-09-28T00:00:00+00:00"
    elif field == "address":
        record[field] = fixture_wallet(tmp_path, "other").address
    else:
        record[field] = "devnet"
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ServiceVaultError):
        vault.unlock(
            wallet.address,
            "mainnet-beta",
            PASSWORD,
            duration_seconds=60,
            allowance_id="34" * 16,
            authorization_revision=1,
        )
    assert PASSWORD not in path.read_text()


def test_disposable_ciphertext_restore_is_locked_until_fresh_unlock(tmp_path):
    wallet = fixture_wallet(tmp_path, "source-wallet")
    source = ServiceWalletVault(tmp_path / "source-service")
    source.enroll(
        envelope(wallet),
        PASSWORD,
        expected_address=wallet.address,
        expected_network="mainnet-beta",
    )
    restored_directory = tmp_path / "restored-service"
    restored_directory.mkdir()
    shutil.copytree(source.root, restored_directory / "service-wallets")
    restored = ServiceWalletVault(restored_directory)
    assert restored.metadata(wallet.address)["state"] == "locked"
    with pytest.raises(ServiceVaultAuthenticationError):
        restored.unlock(
            wallet.address,
            "mainnet-beta",
            "incorrect service password",
            duration_seconds=60,
            allowance_id="34" * 16,
            authorization_revision=1,
        )
    signer = restored.unlock(
        wallet.address,
        "mainnet-beta",
        PASSWORD,
        duration_seconds=60,
        allowance_id="34" * 16,
        authorization_revision=1,
    )
    assert "_seed" not in repr(signer)
    assert PASSWORD not in repr(signer)
    signer.sign_ticket(ticket(wallet))
    signer.lock()
    with pytest.raises(ServiceVaultAuthenticationError, match="locked"):
        signer.sign_ticket(ticket(wallet))


def test_corrupt_json_symlink_and_relaxed_file_permissions_fail_closed(tmp_path):
    wallet = fixture_wallet(tmp_path)
    vault = ServiceWalletVault(tmp_path)
    vault.enroll(
        envelope(wallet),
        PASSWORD,
        expected_address=wallet.address,
        expected_network="mainnet-beta",
    )
    path = tmp_path / "service-wallets" / f"{wallet.address}.json"
    original = path.read_bytes()
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ServiceVaultError, match="could not be read"):
        vault.metadata(wallet.address)
    path.write_bytes(original)
    path.chmod(0o644)
    with pytest.raises(ServiceVaultError, match="group or others"):
        vault.metadata(wallet.address)
    path.chmod(0o600)
    target = tmp_path / "outside.json"
    target.write_bytes(original)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ServiceVaultError, match="regular file"):
        vault.metadata(wallet.address)
