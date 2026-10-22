import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pumpwizard_runner.localnet import (
    LocalnetError,
    LocalnetRpc,
    _encode58,
    _load_test_keypair,
    _signed_transfer,
)


def _keypair_file(tmp_path: Path) -> tuple[Path, Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    secret = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                   serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    path = tmp_path / "localnet-test.json"
    path.write_text(json.dumps(list(secret + public)), encoding="utf-8")
    path.chmod(0o600)
    return path, private, _encode58(public)


def test_localnet_keypair_load_and_signed_transfer_do_not_expose_the_secret(tmp_path: Path):
    path, original_private, sender = _keypair_file(tmp_path)
    loaded_private, loaded_sender = _load_test_keypair(path)
    recipient = _encode58(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    blockhash = "So11111111111111111111111111111111111111112"
    transaction, signature = _signed_transfer(loaded_private, loaded_sender, recipient, 1234, blockhash)
    assert loaded_sender == sender
    assert transaction[0] == 1
    original_private.public_key().verify(transaction[1:65], transaction[65:])
    assert signature
    assert str(path) not in signature


def test_localnet_keypair_permissions_and_rpc_destination_are_strict(tmp_path: Path):
    path, _, _ = _keypair_file(tmp_path)
    path.chmod(0o644)
    with pytest.raises(LocalnetError, match="group or others"):
        _load_test_keypair(path)
    with pytest.raises(ValueError, match="loopback"):
        LocalnetRpc("https://api.mainnet-beta.solana.com")
    with pytest.raises(ValueError, match="loopback"):
        LocalnetRpc("http://127.0.0.1:8898")
    with pytest.raises(ValueError, match="loopback"):
        LocalnetRpc("http://localhost:8899")
