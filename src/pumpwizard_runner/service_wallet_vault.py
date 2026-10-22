"""Explicit service custody for a generated PumpWizard main wallet.

The browser prepares an authenticated encrypted seed envelope.  Enrollment
validates that envelope with a transient unlock secret, writes only ciphertext
to a distinct private directory, and never reads or rewrites browser/legacy
vault files.  Unlock returns a bounded exact-ticket signer; there is no generic
message-signing API.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .transfers import TransferError, public_key


SERVICE_VAULT_SCHEMA = "pumpwizard.service-wallet-vault/v1"
SERVICE_CUSTODY = "main-wallet-service"
SERVICE_VAULT_DIRECTORY = "service-wallets"
KDF_ITERATIONS = 310_000
_SALT_BYTES = 16
_NONCE_BYTES = 12
_SEED_BYTES = 32
_MAX_RECORD_BYTES = 16_384
_HEX_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ServiceVaultError(RuntimeError):
    """A service-vault enrollment is missing, malformed, duplicated, or unsafe."""


class ServiceVaultAuthenticationError(ServiceVaultError):
    """The transient unlock secret did not authenticate the encrypted seed."""


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: object, length: int, label: str) -> bytes:
    if not isinstance(value, str):
        raise ServiceVaultError(f"Service wallet enrollment has no valid {label}")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise ServiceVaultError(f"Service wallet enrollment has no valid {label}") from exc
    if len(decoded) != length:
        raise ServiceVaultError(f"Service wallet enrollment has no valid {label}")
    return decoded


def _secret(value: str) -> bytes:
    if not isinstance(value, str) or len(value) < 12:
        raise ServiceVaultError("Service wallet unlock secret must contain at least 12 characters")
    try:
        return value.encode("utf-8")
    except UnicodeError as exc:
        raise ServiceVaultError("Service wallet unlock secret could not be encoded") from exc


def _key(secret: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", _secret(secret), salt, iterations, dklen=32)


def _aad(record: dict[str, Any]) -> bytes:
    identity = {
        "address": record["address"],
        "created_at": record["created_at"],
        "custody": record["custody"],
        "network": record["network"],
        "schema": record["schema"],
        "vault_id": record["vault_id"],
    }
    return json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validated(record: object) -> tuple[dict[str, Any], bytes, bytes, bytes]:
    if not isinstance(record, dict) or set(record) != {
        "schema", "vault_id", "custody", "address", "network", "created_at", "kdf", "cipher",
    }:
        raise ServiceVaultError("Service wallet enrollment has an unsupported shape")
    if record.get("schema") != SERVICE_VAULT_SCHEMA or record.get("custody") != SERVICE_CUSTODY:
        raise ServiceVaultError("Service wallet enrollment uses an unsupported custody format")
    if not isinstance(record.get("vault_id"), str) or not _HEX_ID.fullmatch(record["vault_id"]):
        raise ServiceVaultError("Service wallet enrollment has no valid vault identity")
    try:
        public_key(record.get("address"))
    except TransferError as exc:
        raise ServiceVaultError("Service wallet enrollment has no valid Solana address") from exc
    if record.get("network") not in ("mainnet-beta", "devnet"):
        raise ServiceVaultError("Service wallet enrollment has no supported Solana network")
    if not isinstance(record.get("created_at"), str) or len(record["created_at"]) > 64:
        raise ServiceVaultError("Service wallet enrollment has no valid creation time")
    try:
        datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ServiceVaultError("Service wallet enrollment has no valid creation time") from exc
    kdf, cipher = record.get("kdf"), record.get("cipher")
    if not isinstance(kdf, dict) or set(kdf) != {"name", "hash", "iterations", "salt"}:
        raise ServiceVaultError("Service wallet enrollment has unsupported key derivation")
    if (
        kdf.get("name") != "PBKDF2"
        or kdf.get("hash") != "SHA-256"
        or kdf.get("iterations") != KDF_ITERATIONS
    ):
        raise ServiceVaultError("Service wallet enrollment has unsupported key derivation")
    if not isinstance(cipher, dict) or set(cipher) != {"name", "nonce", "ciphertext"}:
        raise ServiceVaultError("Service wallet enrollment has unsupported encryption")
    if cipher.get("name") != "AES-256-GCM":
        raise ServiceVaultError("Service wallet enrollment has unsupported encryption")
    return (
        dict(record),
        _decode(kdf["salt"], _SALT_BYTES, "salt"),
        _decode(cipher["nonce"], _NONCE_BYTES, "nonce"),
        _decode(cipher["ciphertext"], _SEED_BYTES + 16, "ciphertext"),
    )


def _address(seed: bytes | bytearray) -> str:
    private = Ed25519PrivateKey.from_private_bytes(bytes(seed))
    raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    # solders performs the canonical Solana base58 encoding and validation.
    from solders.pubkey import Pubkey

    return str(Pubkey.from_bytes(raw))


@dataclass
class ServiceTicketSigner:
    """Bounded in-memory signer for exact reviewed allowance tickets only."""

    vault_id: str
    address: str
    network: str
    allowance_id: str
    authorization_revision: int
    expires_at: float
    _seed: bytearray = field(repr=False)
    _locked: bool = field(default=False, init=False, repr=False)

    @property
    def active(self) -> bool:
        return not self._locked and time.time() < self.expires_at

    def public(self) -> dict[str, object]:
        return {
            "vault_id": self.vault_id,
            "address": self.address,
            "network": self.network,
            "state": "unlocked" if self.active else "locked",
            "expires_at": self.expires_at,
            "custody": SERVICE_CUSTODY,
        }

    def lock(self) -> None:
        self._locked = True
        self._seed[:] = b"\0" * len(self._seed)

    def sign_ticket(self, ticket: object) -> str:
        """Sign one exact allowance ticket and return a Solana transaction wire."""
        if not self.active:
            self.lock()
            raise ServiceVaultAuthenticationError("Service wallet signer is locked or expired")
        fields = {
            "schema",
            "id",
            "allowance_id",
            "order_id",
            "sender",
            "authorization_revision",
            "message_sha256",
            "wire_sha256",
            "unsigned_transaction",
            "created_at",
            "expires_at",
        }
        if not isinstance(ticket, dict) or set(ticket) != fields or (
            ticket.get("schema") != "pumpwizard.main-wallet-signing-ticket/v1"
        ):
            raise ServiceVaultError("Service signer requires an exact reviewed signing ticket")
        if (
            ticket.get("sender") != self.address
            or ticket.get("allowance_id") != self.allowance_id
            or ticket.get("authorization_revision") != self.authorization_revision
        ):
            raise ServiceVaultError("Signing ticket belongs to another service allowance")
        if (
            not isinstance(ticket.get("id"), str)
            or not _HEX_ID.fullmatch(ticket["id"])
            or not isinstance(ticket.get("order_id"), str)
            or not _HEX_ID.fullmatch(ticket["order_id"])
            or not isinstance(ticket.get("wire_sha256"), str)
            or not _SHA256.fullmatch(ticket["wire_sha256"])
            or not isinstance(ticket.get("message_sha256"), str)
            or not _SHA256.fullmatch(ticket["message_sha256"])
            or not isinstance(ticket.get("unsigned_transaction"), str)
            or not 1 <= len(ticket["unsigned_transaction"]) <= 4096
        ):
            raise ServiceVaultError("Signing ticket has invalid reviewed identity or hashes")
        created_at = ticket.get("created_at")
        expires_at = ticket.get("expires_at")
        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or created_at > time.time() + 1
            or expires_at <= created_at
            or time.time() >= expires_at
        ):
            raise ServiceVaultError("Signing ticket expired")
        try:
            unsigned = base64.b64decode(ticket["unsigned_transaction"], validate=True)
        except (KeyError, ValueError, binascii.Error):
            raise ServiceVaultError("Signing ticket has no valid transaction") from None
        if (
            len(unsigned) <= 65
            or len(unsigned) > 1232
            or unsigned[0] != 1
            or any(unsigned[1:65])
        ):
            raise ServiceVaultError("Signing ticket is not a single-signer unsigned transaction")
        message = unsigned[65:]
        if not hmac.compare_digest(
            hashlib.sha256(unsigned).hexdigest(), str(ticket.get("wire_sha256"))
        ):
            raise ServiceVaultError("Signing ticket wire changed after review")
        if not hmac.compare_digest(
            hashlib.sha256(message).hexdigest(), str(ticket.get("message_sha256"))
        ):
            raise ServiceVaultError("Signing ticket message changed after review")
        signature = Ed25519PrivateKey.from_private_bytes(bytes(self._seed)).sign(message)
        return _b64(b"\x01" + signature + message)


class ServiceWalletVault:
    """Private multi-wallet service-vault collection with create-only enrollment."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / SERVICE_VAULT_DIRECTORY

    def _root(self) -> Path:
        if not self.root.exists():
            self.root.mkdir(parents=True, mode=0o700)
            if os.name != "nt":
                self.root.chmod(0o700)
        details = self.root.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise ServiceVaultError("Service wallet vault directory must be a private directory")
        if os.name != "nt" and details.st_mode & 0o077:
            raise ServiceVaultError(
                "Service wallet vault directory must not be accessible by group or others"
            )
        return self.root

    def _path(self, address: str) -> Path:
        try:
            public_key(address)
        except TransferError as exc:
            raise ServiceVaultError("Choose a valid generated wallet address") from exc
        return self._root() / f"{address}.json"

    def _read(self, address: str) -> dict[str, Any]:
        path = self._path(address)
        try:
            details = path.lstat()
        except OSError as exc:
            raise ServiceVaultError("Service wallet enrollment was not found") from exc
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ServiceVaultError("Service wallet enrollment must be a regular file")
        if details.st_size > _MAX_RECORD_BYTES:
            raise ServiceVaultError("Service wallet enrollment is too large")
        if os.name != "nt" and details.st_mode & 0o077:
            raise ServiceVaultError(
                "Service wallet enrollment must not be readable by group or others"
            )
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            if os.name != "nt":
                flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ServiceVaultError("Service wallet enrollment must be a regular file")
            if opened.st_size > _MAX_RECORD_BYTES:
                raise ServiceVaultError("Service wallet enrollment is too large")
            if os.name != "nt" and opened.st_mode & 0o077:
                raise ServiceVaultError(
                    "Service wallet enrollment must not be readable by group or others"
                )
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = None
                record = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise ServiceVaultError("Service wallet enrollment could not be read") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return _validated(record)[0]

    @staticmethod
    def _decrypt(record: dict[str, Any], unlock_secret: str) -> bytearray:
        record, salt, nonce, ciphertext = _validated(record)
        try:
            seed = AESGCM(_key(unlock_secret, salt, record["kdf"]["iterations"])).decrypt(
                nonce, ciphertext, _aad(record),
            )
        except InvalidTag as exc:
            raise ServiceVaultAuthenticationError(
                "Service wallet password did not unlock this enrollment"
            ) from exc
        if len(seed) != _SEED_BYTES:
            raise ServiceVaultError("Service wallet enrollment contained an invalid private seed")
        value = bytearray(seed)
        if _address(value) != record["address"]:
            value[:] = b"\0" * len(value)
            raise ServiceVaultError("Service wallet address does not match its encrypted key")
        return value

    def _atomic_create(self, target: Path, record: dict[str, Any]) -> None:
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        descriptor, temporary = tempfile.mkstemp(
            prefix=".service-wallet-", suffix=".tmp", dir=target.parent
        )
        created = False
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            # Hard-linking a completed private temporary file is atomic and
            # refuses to replace a concurrently created enrollment.
            os.link(temporary, target)
            created = True
            if os.name != "nt":
                target.chmod(0o600)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError as exc:
            raise ServiceVaultError("This wallet is already enrolled for service custody") from exc
        except OSError as exc:
            if created:
                try:
                    target.unlink()
                except OSError:
                    pass
            raise ServiceVaultError("Could not save the encrypted service wallet") from exc
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def enroll(
        self,
        envelope: object,
        unlock_secret: str,
        *,
        expected_address: str,
        expected_network: str,
    ) -> dict[str, object]:
        """Validate and create one distinct encrypted service-custody record."""
        record, *_ = _validated(envelope)
        if (record["address"], record["network"]) != (expected_address, expected_network):
            raise ServiceVaultError("Enrollment wallet or network changed after review")
        target = self._path(expected_address)
        if target.exists():
            raise ServiceVaultError("This wallet is already enrolled for service custody")
        for candidate in self._root().glob("*.json"):
            existing = self._read(candidate.stem)
            if existing["vault_id"] == record["vault_id"]:
                raise ServiceVaultError(
                    "Service vault identity already belongs to another enrollment"
                )
        seed = self._decrypt(record, unlock_secret)
        seed[:] = b"\0" * len(seed)
        self._atomic_create(target, record)
        return self.metadata(expected_address)

    def metadata(self, address: str) -> dict[str, object]:
        record = self._read(address)
        return {
            "schema": record["schema"],
            "vault_id": record["vault_id"],
            "custody": record["custody"],
            "address": record["address"],
            "network": record["network"],
            "created_at": record["created_at"],
            "state": "locked",
        }

    def unlock(
        self,
        address: str,
        network: str,
        unlock_secret: str,
        *,
        duration_seconds: int,
        allowance_id: str,
        authorization_revision: int,
    ) -> ServiceTicketSigner:
        if type(duration_seconds) is not int or not 1 <= duration_seconds <= 3600:
            raise ServiceVaultError("Service signer duration must be 1–3600 seconds")
        if not isinstance(allowance_id, str) or not _HEX_ID.fullmatch(allowance_id):
            raise ServiceVaultError("Service signer requires an exact allowance identity")
        if type(authorization_revision) is not int or authorization_revision < 1:
            raise ServiceVaultError("Service signer requires an authorization revision")
        record = self._read(address)
        if record["network"] != network:
            raise ServiceVaultError("Service wallet enrollment belongs to another network")
        seed = self._decrypt(record, unlock_secret)
        return ServiceTicketSigner(
            vault_id=record["vault_id"],
            address=record["address"],
            network=record["network"],
            allowance_id=allowance_id,
            authorization_revision=authorization_revision,
            expires_at=time.time() + duration_seconds,
            _seed=seed,
        )
