import json

import pytest

from pumpwizard_runner.transfer_recovery import resolve_expired, independent_rpc
from pumpwizard_runner.transfers import GENESIS, TransferError, TransferJournal, WithdrawalService
from pumpwizard_runner.wallet_vault import WalletVault
from test_transfers import Rpc


class RecoveryRpc(Rpc):
    network = 'mainnet-beta'

    def __init__(self, hostname):
        super().__init__()
        self.url = f'https://{hostname}/'
        self.transaction = None
        self.calls = []

    def genesis(self):
        return GENESIS[self.network]

    def call(self, method, params):
        self.calls.append(method)
        if method == 'getSignatureStatuses':
            assert params[1]['searchTransactionHistory'] is True
            return {'context': {'slot': 500}, 'value': [self.status]}
        if method == 'getTransaction':
            assert params[1]['commitment'] == 'finalized'
            return self.transaction
        return super().call(method, params)


@pytest.fixture
def recovery(tmp_path):
    wallet = WalletVault(tmp_path / 'from').create('recovery test password')
    recipient = WalletVault(tmp_path / 'to').create('recipient test password').address
    journal = TransferJournal(tmp_path)
    primary, witness = RecoveryRpc('primary.example'), RecoveryRpc('witness.example')
    service = WithdrawalService(primary, journal)
    preview = service.preview(wallet.address, recipient, '0.11')
    primary.disconnect = True
    signature = service.submit(preview, wallet)
    primary.height = witness.height = 200
    primary.calls.clear()
    yield journal, primary, witness, wallet.address, primary.network, preview.id, signature
    journal.close()


def test_resolve_expired_keeps_receipt_and_audit_and_releases_only_its_intent(recovery, tmp_path):
    journal, primary, witness, sender, network, ident, signature = recovery
    resolved = resolve_expired(*recovery)
    assert resolved['status'] == 'expired'
    assert resolved['lamports'] == '110000000'
    assert resolved['signature'] == signature
    assert journal.db.execute('SELECT COUNT(*) FROM wallet_intents').fetchone()[0] == 0
    audit = journal.db.execute('SELECT * FROM transfer_recoveries').fetchone()
    assert len(json.loads(audit['evidence'])['checks']) == 2
    assert len(primary.sent) == 1 and not witness.sent
    assert 'sendTransaction' not in primary.calls
    primary.calls.clear()
    assert resolve_expired(*recovery)['status'] == 'expired'
    assert not primary.calls
    reopened = TransferJournal(tmp_path)
    try:
        assert reopened.rows()[0]['status'] == 'expired'
        assert reopened.db.execute('SELECT COUNT(*) FROM wallet_intents').fetchone()[0] == 0
        reopened.update(ident, 'unknown', 'A stale poll must not reopen a resolved transfer')
        assert reopened.rows()[0]['status'] == 'expired'
    finally:
        reopened.close()


@pytest.mark.parametrize('provider', [1, 2])
@pytest.mark.parametrize('failure', ['valid', 'status', 'transaction', 'network', 'unavailable'])
def test_ambiguous_or_unexpired_transfer_never_releases_lock(recovery, provider, failure):
    rpc = recovery[provider]
    if failure == 'valid':
        rpc.height = 100
    if failure == 'status':
        rpc.status = {'confirmationStatus': 'confirmed', 'err': None}
    if failure == 'transaction':
        rpc.transaction = {'slot': 99}
    if failure == 'network':
        rpc.network = 'devnet'
    if failure == 'unavailable':
        def unavailable(*args): raise TransferError('RPC unavailable')
        rpc.call = unavailable
    with pytest.raises(TransferError):
        resolve_expired(*recovery)
    journal = recovery[0]
    assert journal.rows()[0]['status'] == 'unknown'
    assert journal.db.execute('SELECT COUNT(*) FROM wallet_intents').fetchone()[0] == 1


def test_wrong_wallet_or_duplicate_provider_cannot_recover(recovery):
    with pytest.raises(TransferError):
        resolve_expired(*recovery[:3], recovery[3] + 'x', *recovery[4:])
    recovery[2].url = recovery[1].url
    with pytest.raises(TransferError, match='independent'):
        resolve_expired(*recovery)
    recovery[1].url = 'https://api.mainnet-beta.solana.com/?different=true'
    with pytest.raises(TransferError, match='independent'):
        independent_rpc(recovery[1], recovery[4])


def test_receipt_change_during_check_preserves_the_intent(recovery):
    journal, primary, witness, sender, network, ident, signature = recovery
    original = witness.call
    def changed(method, params):
        if method == 'getTransaction':
            journal.update(ident, 'confirmed', 'Confirmed meanwhile')
        return original(method, params)
    witness.call = changed
    with pytest.raises(TransferError, match='changed'):
        resolve_expired(*recovery)
    assert journal.rows()[0]['status'] == 'confirmed'
    assert journal.db.execute('SELECT COUNT(*) FROM wallet_intents').fetchone()[0] == 1


def test_recovery_api_binds_exact_original_and_returns_expired_receipt(recovery, tmp_path, monkeypatch):
    from pumpwizard_runner import web_transfers
    journal, primary, witness, sender, network, ident, signature = recovery
    monkeypatch.setattr('pumpwizard_runner.web_trading.rpc_for', lambda *args: primary)
    monkeypatch.setattr('pumpwizard_runner.transfer_recovery.independent_rpc', lambda *args: witness)
    result = web_transfers.command(tmp_path, 'resolve-expired', dict(sender=sender, network=network, transfer_id=ident, signature=signature))
    assert result['transfer']['status'] == 'expired'
    assert len(primary.sent) == 1
    with pytest.raises(TransferError, match='exact original'):
        web_transfers.command(tmp_path, 'resolve-expired', dict(sender=sender, network=network, transfer_id=ident))
