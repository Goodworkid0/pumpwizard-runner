import base64
from dataclasses import replace
import struct

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from pumpwizard_runner.localnet import SYSTEM_PROGRAM, _decode58, _encode58
from pumpwizard_runner.transfers import (
    GENESIS, TransferError, TransferJournal, TransferRpc, WithdrawalService, parse_sol, transfer_message,
)
from pumpwizard_runner.wallet_vault import WalletVault


class Rpc:
    network = "localnet"

    def __init__(self):
        self.sent = []
        self.height = 10
        self.balance = 2_000_000_000
        self.fee = 5000
        self.status = None
        self.disconnect = False
        self.simulation_error = None
        self.owner = SYSTEM_PROGRAM
        self.executable = False
        self.recipient_missing = False

    def genesis(self):
        return "local-genesis"

    def call(self, method, params):
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": _encode58(bytes([3]) * 32), "lastValidBlockHeight": 100}}
        if method == "getFeeForMessage":
            return {"value": self.fee}
        if method == "getBalance":
            return {"value": self.balance}
        if method == "getAccountInfo":
            return {"value": None} if self.recipient_missing else {
                "value": {"owner": self.owner, "executable": self.executable}
            }
        if method == "getMinimumBalanceForRentExemption":
            return 890880
        if method == "getBlockHeight":
            return self.height
        if method == "simulateTransaction":
            assert base64.b64decode(params[0])[1:65] == bytes(64)
            return {"value": {"err": self.simulation_error}}
        if method == "sendTransaction":
            data = base64.b64decode(params[0])
            self.sent.append(data)
            if self.disconnect:
                raise TransferError("timeout after acceptance")
            return _encode58(data[1:65])
        if method == "getSignatureStatuses":
            assert params[1]["searchTransactionHistory"]
            return {"value": [self.status]}
        raise AssertionError(method)


@pytest.fixture
def setup(tmp_path):
    wallet = WalletVault(tmp_path / "sender").create("test withdrawal password")
    recipient = WalletVault(tmp_path / "recipient").create("test recipient password").address
    rpc = Rpc()
    journal = TransferJournal(tmp_path)
    service = WithdrawalService(rpc, journal)
    yield wallet, recipient, rpc, journal, service
    journal.close()


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity", "0.0000000001", "garbage", "18446744074"])
def test_rejects_invalid_amounts(amount):
    with pytest.raises(TransferError):
        parse_sol(amount)


def test_exact_lamports():
    assert parse_sol("0.123456789") == 123456789
    assert parse_sol("0.000000001") == 1


def test_preview_does_not_sign_or_send_and_submission_matches_review(setup):
    wallet, recipient, rpc, journal, service = setup
    preview = service.preview(wallet.address, recipient, "0.05")
    assert preview.fee == 5000
    assert not rpc.sent and not journal.rows()
    signature = service.submit(preview, wallet)
    data = rpc.sent[0]
    message = data[65:]
    Ed25519PublicKey.from_public_bytes(_decode58(wallet.address)).verify(data[1:65], message)
    assert message[:4] == b"\x01\x00\x02\x04"
    assert message[4:36] == _decode58(wallet.address)
    assert message[36:68] == _decode58(recipient)
    assert message[68:100] == bytes(32)
    assert struct.unpack("<IQ", message[-12:]) == (2, 50_000_000)
    assert journal.rows()[0]["signature"] == signature
    assert journal.rows()[0]["status"] == "pending"
    with pytest.raises(TransferError, match="already submitted"):
        service.submit(preview, wallet)
    assert len(rpc.sent) == 1


def test_timeout_is_recovered_after_restart_without_resubmission(setup, tmp_path):
    wallet, recipient, rpc, journal, service = setup
    preview = service.preview(wallet.address, recipient, "0.05")
    rpc.disconnect = True
    service.submit(preview, wallet)
    assert journal.rows()[0]["status"] == "unknown"
    reopened = TransferJournal(tmp_path)
    try:
        restored = WithdrawalService(rpc, reopened)
        another = restored.preview(wallet.address, recipient, "0.01")
        with pytest.raises(TransferError, match="unresolved"):
            restored.submit(another, wallet)
        rpc.status = {"err": None, "confirmationStatus": "finalized"}
        assert restored.reconcile(wallet.address)[0]["status"] == "finalized"
        assert len(rpc.sent) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("change", ["expired", "fee", "balance", "simulation", "wallet", "network"])
def test_changed_or_invalid_preview_never_submits(setup, change):
    wallet, recipient, rpc, journal, service = setup
    preview = service.preview(wallet.address, recipient, "0.05")
    if change == "expired":
        rpc.height = 101
    elif change == "fee":
        rpc.fee = 10000
    elif change == "balance":
        rpc.balance = 1
    elif change == "simulation":
        rpc.simulation_error = {"InstructionError": [0, "InsufficientFunds"]}
    elif change == "wallet":
        preview = replace(preview, sender=recipient)
    elif change == "network":
        preview = replace(preview, network="mainnet-beta")
    with pytest.raises(TransferError):
        service.submit(preview, wallet)
    assert not rpc.sent and not journal.rows()


def test_missing_history_after_expiry_is_not_treated_as_failed(setup):
    wallet, recipient, rpc, journal, service = setup
    service.submit(service.preview(wallet.address, recipient, "0.05"), wallet)
    rpc.height = 101
    assert service.reconcile(wallet.address)[0]["status"] == "unknown"
    assert "unverified" in journal.rows()[0]["detail"]


def test_chain_error_only_releases_reservation_after_finality(setup):
    wallet, recipient, rpc, journal, service = setup
    service.submit(service.preview(wallet.address, recipient, "0.05"), wallet)
    rpc.status = {"err": {"InstructionError": [0, "failure"]}, "confirmationStatus": "confirmed"}
    assert service.reconcile(wallet.address)[0]["status"] == "pending"
    rpc.status["confirmationStatus"] = "finalized"
    assert service.reconcile(wallet.address)[0]["status"] == "failed"


def test_rejects_program_recipient_and_invalid_addresses(setup):
    wallet, recipient, rpc, journal, service = setup
    rpc.owner = "TokenProgram"
    with pytest.raises(TransferError, match="wallet address"):
        service.preview(wallet.address, recipient, "0.1")
    for address in ("bad", SYSTEM_PROGRAM, wallet.address):
        with pytest.raises(TransferError):
            transfer_message(wallet.address, address, 1000, recipient)


def test_new_recipient_rent_and_sender_remainder_boundaries(setup):
    wallet, recipient, rpc, _, service = setup
    rpc.recipient_missing = True
    with pytest.raises(TransferError, match="new recipient wallet"):
        service.preview(wallet.address, recipient, "0.0001")
    assert service.preview(wallet.address, recipient, "0.001").lamports == 1_000_000

    rpc.recipient_missing = False
    with pytest.raises(TransferError, match="Leave at least"):
        service.preview(wallet.address, recipient, "1.999994999")
    exact = service.preview(wallet.address, recipient, "1.999995")
    assert exact.lamports + exact.fee == exact.balance


def test_executable_recipient_and_changed_fee_never_reach_signing(setup):
    wallet, recipient, rpc, journal, service = setup
    rpc.executable = True
    with pytest.raises(TransferError, match="not a token or program account"):
        service.preview(wallet.address, recipient, "0.05")
    rpc.executable = False
    preview = service.preview(wallet.address, recipient, "0.05")
    rpc.fee += 1
    with pytest.raises(TransferError, match="Balance or fee changed"):
        service.submit(preview, wallet)
    assert not rpc.sent and not journal.rows()


def test_rpc_checks_network_identity_and_transport(monkeypatch):
    rpc = TransferRpc("mainnet-beta")
    monkeypatch.setattr(rpc, "call", lambda *args: GENESIS["devnet"])
    with pytest.raises(TransferError, match="network"):
        rpc.genesis()
    for network, url in (("mainnet-beta", "http://example.com"), ("localnet", "https://example.com"),
                         ("devnet", "https://user:password@example.com")):
        with pytest.raises(TransferError):
            TransferRpc(network, url)


@pytest.mark.parametrize("network,genesis", [
    ("mainnet-beta", "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"),
    ("devnet", "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"),
])
def test_full_official_network_genesis_hashes_are_accepted(monkeypatch, network, genesis):
    # Literal official endpoint responses: do not derive expected values from GENESIS.
    rpc = TransferRpc(network)
    monkeypatch.setattr(rpc, "call", lambda *_: genesis)
    assert rpc.genesis() == genesis
    monkeypatch.setattr(rpc, "call", lambda *_: genesis[:30])
    with pytest.raises(TransferError, match="network"):
        rpc.genesis()


def test_transfer_review_pins_compute_budget_and_single_exact_payment(setup):
    from solders.message import Message
    from solders.system_program import decode_transfer
    from solders.instruction import AccountMeta, Instruction

    wallet, recipient, rpc, journal, service = setup
    preview = service.preview(wallet.address, recipient, '0.01')
    message = Message.from_bytes(preview.message())
    assert message.header.num_required_signatures == 1
    assert len(message.instructions) == 3
    limit, price, payment = message.instructions
    for ix in (limit, price):
        assert str(message.account_keys[ix.program_id_index]) == 'ComputeBudget111111111111111111111111111111'
        assert not ix.accounts
    assert bytes(limit.data) == struct.pack('<BI', 2, 200_000)
    assert bytes(price.data) == struct.pack('<BQ', 3, 0)
    assert str(message.account_keys[payment.program_id_index]) == SYSTEM_PROGRAM
    decoded = decode_transfer(Instruction(
        message.account_keys[payment.program_id_index], bytes(payment.data),
        [AccountMeta(message.account_keys[i], i == 0, True) for i in payment.accounts],
    ))
    assert str(decoded['from_pubkey']) == wallet.address
    assert str(decoded['to_pubkey']) == recipient
    assert decoded['lamports'] == 10_000_000


def test_funding_budget_deducts_fee_and_keeps_source_reserve(setup):
    wallet, recipient, rpc, journal, service = setup
    rpc.balance = 121858763
    preview = service.preview(wallet.address, recipient, '0.11', funding_budget=True)
    assert preview.lamports == 109995000
    assert preview.lamports + preview.fee == 110000000
    assert preview.source_reserve == 2000000
    assert preview.balance - preview.lamports - preview.fee == 11858763
    assert not rpc.sent
    service.submit(preview, wallet)
    assert struct.unpack('<IQ', rpc.sent[0][-12:]) == (2, 109995000)


def test_funding_max_protects_reservations_and_reserve_at_submission(setup):
    wallet, recipient, rpc, journal, service = setup
    with journal.db:
        journal.db.execute('INSERT INTO agent_reservations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            ('held', 'agent', rpc.genesis(), wallet.address, '500000000', None, None, '0', 1, None, 'held'))
    preview = service.preview(wallet.address, recipient, '2', funding_budget=True)
    assert preview.reserved_lamports == 500000000
    assert preview.lamports == 1497995000
    assert preview.balance - preview.lamports - preview.fee == 502000000
    with journal.db:
        journal.db.execute("UPDATE agent_reservations SET lamports='500000001'")
    with pytest.raises(TransferError, match='reserved'):
        service.submit(preview, wallet)
    assert not rpc.sent


def test_funding_refuses_budget_below_fee_and_new_wallet_rent(setup):
    wallet, recipient, rpc, journal, service = setup
    rpc.balance = 2005000
    with pytest.raises(TransferError, match='reserve'):
        service.preview(wallet.address, recipient, '1', funding_budget=True)
    rpc.balance = 10000000
    rpc.recipient_missing = True
    with pytest.raises(TransferError, match='new recipient'):
        service.preview(wallet.address, recipient, '0.000895879', funding_budget=True)
    assert not rpc.sent


def test_uncertain_rpc_diagnostic_survives_repeated_expiry_checks(setup):
    from pumpwizard_runner.transfers import RpcError
    wallet, recipient, rpc, journal, service = setup
    preview = service.preview(wallet.address, recipient, '0.11', funding_budget=True)
    original = rpc.call
    def rejected(method, params):
        if method == 'sendTransaction':
            raise RpcError(method, rpc_code=-32002)
        return original(method, params)
    rpc.call = rejected
    service.submit(preview, wallet)
    assert journal.rows()[0]['status'] == 'unknown'
    assert '-32002' in journal.rows()[0]['detail']
    rpc.height = 101
    service.reconcile(wallet.address)
    service.reconcile(wallet.address)
    assert '-32002' in journal.rows()[0]['detail']
    assert journal.rows()[0]['detail'].count('Blockhash expired') == 1
