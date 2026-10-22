"""Encrypted local Solana wallet storage.

The vault creates one Ed25519 keypair for the local application. Its public
key is a standard base58 Solana address. The private seed is encrypted at rest
and exists only in the unlocked wallet object for the running process.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


VAULT_FILENAME = "solanamarket-wallet.json"
VAULT_SCHEMA = "solanamarket-wizard.wallet/v1"
_KDF_NAME = "scrypt"
_KDF_N = 2 ** 15
_KDF_R = 8
_KDF_P = 1
_SALT_BYTES = 16
_NONCE_BYTES = 12
_SEED_BYTES = 32
_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class WalletVaultError(RuntimeError):
    """The local wallet vault is missing, malformed, or unsafe to use."""


class WalletAuthenticationError(WalletVaultError):
    """The provided vault password could not decrypt the wallet."""


@dataclass(frozen=True)
class LocalSolanaWallet:
    """An unlocked local signer. The private seed is intentionally not public API."""

    address: str
    _seed: bytes = field(repr=False, compare=False)

    def sign(self, message: bytes) -> bytes:
        """Sign bytes in memory; callers enforce transaction construction and authorization."""
        if not isinstance(message, bytes):
            raise TypeError("Wallet signing input must be bytes")
        return Ed25519PrivateKey.from_private_bytes(self._seed).sign(message)


class WalletVault:
    """Create and unlock a single encrypted local Solana wallet."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / VAULT_FILENAME

    def exists(self) -> bool:
        return self.path.exists()

    def create(self, password: str) -> LocalSolanaWallet:
        """Create an encrypted wallet without ever writing the seed in plaintext."""
        if self.path.exists():
            raise WalletVaultError("A local wallet already exists")
        password_bytes = _password_bytes(password)
        private = Ed25519PrivateKey.generate()
        seed = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                     serialization.NoEncryption())
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        wallet = LocalSolanaWallet(address=_encode58(public), _seed=seed)
        salt = os.urandom(_SALT_BYTES)
        nonce = os.urandom(_NONCE_BYTES)
        key = _derive_key(password_bytes, salt)
        ciphertext = AESGCM(key).encrypt(nonce, seed, VAULT_SCHEMA.encode("ascii"))
        self._write_record({
            "schema": VAULT_SCHEMA,
            "address": wallet.address,
            "kdf": {
                "name": _KDF_NAME, "n": _KDF_N, "r": _KDF_R, "p": _KDF_P,
                "salt": _base64(salt),
            },
            "cipher": {"name": "aes-256-gcm", "nonce": _base64(nonce), "ciphertext": _base64(ciphertext)},
        })
        return wallet

    def unlock(self, password: str) -> LocalSolanaWallet:
        """Decrypt and validate the local wallet using its vault password."""
        return self._unlock_record(self._read_record(), password)

    @staticmethod
    def _unlock_record(record: dict[str, Any], password: str) -> LocalSolanaWallet:
        password_bytes = _password_bytes(password)
        salt, nonce, ciphertext = _validated_material(record)
        try:
            seed = AESGCM(_derive_key(password_bytes, salt)).decrypt(
                nonce, ciphertext, VAULT_SCHEMA.encode("ascii"))
        except InvalidTag as exc:
            raise WalletAuthenticationError("Wallet password did not unlock this vault") from exc
        if len(seed) != _SEED_BYTES:
            raise WalletVaultError("Wallet vault contained an invalid private seed")
        try:
            private = Ed25519PrivateKey.from_private_bytes(seed)
            public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        except ValueError as exc:
            raise WalletVaultError("Wallet vault contained an invalid private seed") from exc
        address = record["address"]
        if not isinstance(address, str) or address != _encode58(public):
            raise WalletVaultError("Wallet vault address does not match its encrypted key")
        return LocalSolanaWallet(address=address, _seed=seed)

    def export_backup(self, destination: Path, password: str) -> str:
        """Export only authenticated ciphertext, exclusively to a new private file."""
        record = self._read_record()
        wallet = self._unlock_record(record, password)
        self._export_record(destination, record)
        return wallet.address

    def restore_backup(self, source: Path, password: str, expected_address: str | None = None) -> LocalSolanaWallet:
        """Validate before replacing; retain the previous encrypted vault beside it."""
        incoming = WalletVault(source.parent)
        incoming.path = source
        record = incoming._read_record()
        wallet = self._unlock_record(record, password)
        if expected_address is not None and wallet.address != expected_address:
            raise WalletVaultError("Backup changed after review; select it again")
        if self.exists():
            if source.resolve() == self.path.resolve():
                raise WalletVaultError("Select a backup file, not the active wallet vault")
            import uuid
            archive = self.path.with_name(f"wallet-before-restore-{uuid.uuid4().hex}.json")
            self._export_record(archive, self._read_record())
        self._write_record(record)
        return wallet

    @staticmethod
    def _export_record(destination: Path, record: dict[str, Any]) -> None:
        encoded = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise WalletVaultError("Choose a new writable backup filename; existing files are never overwritten") from exc

    def _read_record(self) -> dict[str, Any]:
        try:
            details = self.path.lstat()
        except OSError as exc:
            raise WalletVaultError("Local wallet vault was not found") from exc
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise WalletVaultError("Local wallet vault must be a regular file")
        if details.st_size > 16_384:
            raise WalletVaultError("Local wallet vault is too large")
        if os.name != "nt" and details.st_mode & 0o077:
            raise WalletVaultError("Local wallet vault must not be readable by group or others")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WalletVaultError("Local wallet vault could not be read") from exc
        if not isinstance(value, dict) or value.get("schema") != VAULT_SCHEMA:
            raise WalletVaultError("Local wallet vault uses an unsupported format")
        return value

    def _write_record(self, record: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor, temporary = tempfile.mkstemp(prefix=".solanamarket-wallet-", suffix=".tmp", dir=self.path.parent)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(encoded)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                self.path.chmod(0o600)
        except OSError as exc:
            raise WalletVaultError("Could not save the encrypted local wallet") from exc
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _password_bytes(password: str) -> bytes:
    if not isinstance(password, str) or len(password) < 12:
        raise WalletVaultError("Wallet password must contain at least 12 characters")
    try:
        return password.encode("utf-8")
    except UnicodeError as exc:
        raise WalletVaultError("Wallet password could not be encoded") from exc


def _derive_key(password: bytes, salt: bytes) -> bytes:
    try:
        return hashlib.scrypt(password, salt=salt, n=_KDF_N, r=_KDF_R, p=_KDF_P, dklen=32,
                              maxmem=64 * 1024 * 1024)
    except ValueError as exc:
        raise WalletVaultError("Could not derive the local wallet encryption key") from exc


def _validated_material(record: dict[str, Any]) -> tuple[bytes, bytes, bytes]:
    kdf = record.get("kdf")
    cipher = record.get("cipher")
    if not isinstance(kdf, dict) or not isinstance(cipher, dict):
        raise WalletVaultError("Local wallet vault is incomplete")
    if (kdf.get("name") != _KDF_NAME or kdf.get("n") != _KDF_N or kdf.get("r") != _KDF_R or
            kdf.get("p") != _KDF_P or cipher.get("name") != "aes-256-gcm"):
        raise WalletVaultError("Local wallet vault uses unsupported encryption settings")
    salt = _decode_base64(kdf.get("salt"), _SALT_BYTES, "salt")
    nonce = _decode_base64(cipher.get("nonce"), _NONCE_BYTES, "nonce")
    ciphertext = _decode_base64(cipher.get("ciphertext"), _SEED_BYTES + 16, "ciphertext")
    return salt, nonce, ciphertext


def _base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_base64(value: object, expected_length: int, label: str) -> bytes:
    if not isinstance(value, str):
        raise WalletVaultError(f"Local wallet vault has no valid {label}")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise WalletVaultError(f"Local wallet vault has no valid {label}") from exc
    if len(decoded) != expected_length:
        raise WalletVaultError(f"Local wallet vault has no valid {label}")
    return decoded


def _encode58(value: bytes) -> str:
    number = int.from_bytes(value, "big")
    text = ""
    while number:
        number, remainder = divmod(number, 58)
        text = _BASE58[remainder] + text
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + text
