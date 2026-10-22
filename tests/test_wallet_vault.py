from pathlib import Path
import os

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from pumpwizard_runner.localnet import _decode58
from pumpwizard_runner.wallet_vault import (
    VAULT_FILENAME,
    WalletAuthenticationError,
    WalletVault,
    WalletVaultError,
)


PASSWORD = "safe local vault password"


def test_vault_creates_an_encrypted_solana_keypair_and_reopens_it(tmp_path: Path):
    vault = WalletVault(tmp_path)
    wallet = vault.create(PASSWORD)
    stored = (tmp_path / VAULT_FILENAME).read_text(encoding="utf-8")
    reopened = vault.unlock(PASSWORD)

    assert wallet.address == reopened.address
    assert PASSWORD not in stored
    assert '"ciphertext"' in stored
    message = b"only a future reviewed executor can submit a signed transaction"
    signature = reopened.sign(message)
    Ed25519PublicKey.from_public_bytes(_decode58(reopened.address)).verify(signature, message)


def test_vault_rejects_wrong_password_and_insecure_permissions(tmp_path: Path):
    vault = WalletVault(tmp_path)
    vault.create(PASSWORD)
    with pytest.raises(WalletAuthenticationError, match="password"):
        vault.unlock("wrong password value")
    if os.name != "nt":
        vault.path.chmod(0o644)
        with pytest.raises(WalletVaultError, match="group or others"):
            vault.unlock(PASSWORD)


def test_vault_requires_a_long_password_and_does_not_overwrite(tmp_path: Path):
    vault = WalletVault(tmp_path)
    with pytest.raises(WalletVaultError, match="at least 12"):
        vault.create("short")
    vault.create(PASSWORD)
    with pytest.raises(WalletVaultError, match="already exists"):
        vault.create(PASSWORD)


def test_encrypted_backup_restores_same_key_and_preserves_replaced_wallet(tmp_path):
    original = WalletVault(tmp_path / "original")
    original_wallet = original.create(PASSWORD)
    backup = tmp_path / "backup.json"
    original.export_backup(backup, PASSWORD)
    assert PASSWORD not in backup.read_text()
    other = WalletVault(tmp_path / "other")
    other_wallet = other.create("another wallet password")
    restored = other.restore_backup(backup, PASSWORD)
    assert restored.address == original_wallet.address
    assert restored.sign(b"test") == original_wallet.sign(b"test")
    archive = next((tmp_path / "other").glob("wallet-before-restore-*.json"))
    archived = WalletVault(archive.parent)
    archived.path = archive
    assert archived.unlock("another wallet password").address == other_wallet.address
    with pytest.raises(WalletVaultError):
        original.export_backup(backup, PASSWORD)


def test_restore_wrong_password_or_changed_backup_preserves_active_vault(tmp_path):
    original = WalletVault(tmp_path / "original")
    original.create(PASSWORD)
    backup = tmp_path / "backup.json"
    original.export_backup(backup, PASSWORD)
    active = WalletVault(tmp_path / "active")
    active.create(PASSWORD)
    before = active.path.read_bytes()
    with pytest.raises(WalletAuthenticationError):
        active.restore_backup(backup, "incorrect password value")
    with pytest.raises(WalletVaultError, match="changed"):
        active.restore_backup(backup, PASSWORD, expected_address="different wallet")
    assert active.path.read_bytes() == before
