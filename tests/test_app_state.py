"""Account persistence, lossless upgrades and authenticated API boundaries."""
import base64
import copy
from datetime import datetime, timezone
import hashlib
import json
import stat
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest
from solders.keypair import Keypair

from pumpwizard_runner.app_state import AppStateStore, DATABASE_NAME
from pumpwizard_runner.service_backup import create_backup, restore_backup
from pumpwizard_runner.web_api import serve


def wallet(seed=1):
    pair = Keypair.from_seed(bytes([seed]) * 32)
    salt, iv = bytes([3]) * 16, bytes([4]) * 12
    key = hashlib.pbkdf2_hmac('sha256', b'fixture wallet password', salt, 310_000)
    return {
        'schema': 'pumpwizard.browser-wallet/v1', 'address': str(pair.pubkey()),
        'created_at': datetime.now(timezone.utc).isoformat(),
        'kdf': {'name': 'PBKDF2', 'hash': 'SHA-256', 'iterations': 310_000,
                'salt': base64.b64encode(salt).decode()},
        'cipher': {'name': 'AES-GCM', 'iv': base64.b64encode(iv).decode(),
                   'ciphertext': base64.b64encode(AESGCM(key).encrypt(iv, bytes(pair), None)).decode()},
    }


def test_encrypted_accounts_persist_and_backup_restores(tmp_path):
    data = tmp_path / 'data'
    record = wallet()
    prefs = {
        'pumpwizard.research-mode/v1': 'enabled',
        'pumpwizard.portfolio-history/v1:mainnet-beta:' + record['address']:
            '[{"at":1000,"usd":25,"pnl":-2},{"at":2000,"usd":null,"pnl":null}]',
    }
    assert AppStateStore(data).migrate([record], prefs)['wallets'] == [record]
    assert AppStateStore(data).snapshot() == {'wallets': [record], 'preferences': prefs}
    assert stat.S_IMODE((data / DATABASE_NAME).stat().st_mode) == 0o600
    assert b'fixture wallet password' not in (data / DATABASE_NAME).read_bytes()
    backup = tmp_path / 'backup'
    manifest = create_backup(data, backup)
    assert any(row['path'] == DATABASE_NAME for row in manifest['entries'])
    restored = tmp_path / 'restored'
    restore_backup(backup, restored)
    assert AppStateStore(restored).snapshot() == AppStateStore(data).snapshot()
    key = hashlib.pbkdf2_hmac('sha256', b'fixture wallet password', bytes([3]) * 16, 310_000)
    saved = AppStateStore(restored).snapshot()['wallets'][0]
    assert len(AESGCM(key).decrypt(bytes([4]) * 12, base64.b64decode(saved['cipher']['ciphertext']), None)) == 64


def test_migration_is_atomic_idempotent_and_keeps_existing_preferences(tmp_path):
    record = wallet()
    store = AppStateStore(tmp_path)
    prefs = {'pumpwizard.research-mode/v1': 'enabled'}
    first = store.migrate([record], prefs)
    assert store.migrate([copy.deepcopy(record)], prefs) == first
    store.preference('pumpwizard.welcome-acknowledged-version/v1', '0.2.0.dev90')
    store.migrate([], {'pumpwizard.welcome-acknowledged-version/v1': '0.2.0.dev1'})
    assert store.snapshot()['preferences']['pumpwizard.welcome-acknowledged-version/v1'] == '0.2.0.dev90'
    conflicting = copy.deepcopy(record)
    conflicting['cipher']['ciphertext'] = base64.b64encode(bytes(80)).decode()
    before = store.snapshot()
    with pytest.raises(ValueError, match='existing account was kept'):
        store.migrate([wallet(2), conflicting], {})
    assert store.snapshot() == before
    store.remove_wallet(record['address'])
    assert store.snapshot()['wallets'] == []


@pytest.mark.parametrize('change', [
    {'password': 'must not persist'}, {'secretKey': [1] * 64},
    {'cipher': {'name': 'AES-GCM', 'iv': 'bad', 'ciphertext': 'bad'}},
    {'kdf': {'name': 'PBKDF2', 'hash': 'SHA-256', 'iterations': 1, 'salt': 'bad'}},
    {'address': '../account'}, {'created_at': False}, {'created_at': 'not-a-date'},
])
def test_invalid_wallets_are_rejected_without_partial_migration(tmp_path, change):
    record = wallet()
    record.update(change)
    store = AppStateStore(tmp_path)
    with pytest.raises((ValueError, RuntimeError)):
        store.migrate([wallet(2), record], {})
    assert store.snapshot()['wallets'] == []


def test_invalid_secret_preferences_and_symlinks_are_rejected(tmp_path):
    store = AppStateStore(tmp_path)
    with pytest.raises(ValueError):
        store.preference('password', 'secret')
    with pytest.raises(ValueError):
        store.preference('pumpwizard.wallet-session/v1', json.dumps({'password': 'secret'}))
    with pytest.raises(ValueError):
        store.preference('pumpwizard.wallet-worth-history/v1:mainnet-beta:'+wallet()['address'], '[{"at":1,"usd":NaN}]')
    other = tmp_path / 'other'
    other.mkdir()
    (other / DATABASE_NAME).symlink_to(store.path)
    with pytest.raises(ValueError, match='private regular'):
        AppStateStore(other)


def test_concurrent_account_saves_do_not_lose_records(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    records = [wallet(seed) for seed in range(1, 5)]
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda record: AppStateStore(tmp_path).migrate([record], {}), records))
    assert {record['address'] for record in AppStateStore(tmp_path).snapshot()['wallets']} == {record['address'] for record in records}


def test_portfolio_history_preserves_unknowns_losses_and_wallet_scope(tmp_path):
    store = AppStateStore(tmp_path)
    key = 'pumpwizard.portfolio-history/v1:mainnet-beta:' + wallet()['address']
    other = 'pumpwizard.portfolio-history/v1:devnet:' + wallet(2)['address']
    rows = [{'at': 1000, 'usd': 12.5, 'pnl': -3.5},
            {'at': 2000, 'usd': None, 'pnl': None}]
    store.preference(key, json.dumps(rows))
    store.preference(other, '[]')
    restored = AppStateStore(tmp_path).snapshot()['preferences']
    assert json.loads(restored[key]) == rows
    assert restored[other] == '[]'
    assert AppStateStore(tmp_path).snapshot()['wallets'] == []


@pytest.mark.parametrize('rows', [
    [{'at': 1, 'usd': -1, 'pnl': 0}],
    [{'at': True, 'usd': 1, 'pnl': 0}],
    [{'at': 1, 'usd': 1, 'pnl': float('nan')}],
    [{'at': 1, 'usd': float('inf'), 'pnl': 0}],
    [{'at': 1, 'usd': 1, 'pnl': True}],
    [{'at': 1, 'usd': 1, 'pnl': 0, 'secret': 'no'}],
    [{'at': 2, 'usd': 1, 'pnl': 0}, {'at': 1, 'usd': 1, 'pnl': 0}],
    [{'at': 1, 'usd': 1, 'pnl': 0}] * 701,
])
def test_invalid_portfolio_history_does_not_change_saved_state(tmp_path, rows):
    store = AppStateStore(tmp_path)
    key = 'pumpwizard.portfolio-history/v1:mainnet-beta:' + wallet()['address']
    store.preference(key, '[{"at":1,"usd":2,"pnl":-1}]')
    before = store.snapshot()
    with pytest.raises(ValueError, match='portfolio history'):
        store.preference(key, json.dumps(rows))
    assert store.snapshot() == before


def test_legacy_timestamp_and_activity_preferences_do_not_block_account_migration(tmp_path):
    prefs = {'pumpwizard.welcome.last-shown': '1789420828423', 'pumpwizard.activity-muted': 'false'}
    record = wallet()
    assert AppStateStore(tmp_path).migrate([record], prefs) == {'wallets': [record], 'preferences': prefs}


def test_native_balance_history_and_unit_preserve_exact_amounts_and_wallet_scope(tmp_path):
    store = AppStateStore(tmp_path / 'data')
    record = wallet()
    store.migrate([record], {})
    scope = 'mainnet-beta:' + record['address']
    key = 'pumpwizard.wallet-balance-history/v1:' + scope
    unit = 'pumpwizard.wallet-balance-unit/v1:' + scope
    other = 'pumpwizard.wallet-balance-history/v1:devnet:' + record['address']
    rows = [{'at': 1000, 'lamports': '0', 'usd': 0},
            {'at': 2000, 'lamports': '2000000', 'usd': None},
            {'at': 3000, 'lamports': '18446744073709551615', 'usd': 123.45}]
    store.preference(key, json.dumps(rows))
    store.preference(other, '[]')
    store.preference(unit, 'USD')
    snapshot = AppStateStore(tmp_path / 'data').snapshot()
    assert json.loads(snapshot['preferences'][key]) == rows
    assert snapshot['preferences'][other] == '[]'
    assert snapshot['preferences'][unit] == 'USD'
    assert snapshot['wallets'] == [record]
    backup = tmp_path / 'balance-backup'
    create_backup(tmp_path / 'data', backup)
    restored = tmp_path / 'balance-restored'
    restore_backup(backup, restored)
    assert AppStateStore(restored).snapshot() == snapshot
    store.preference(unit, 'SOL')
    store.preference(key, None)
    assert key not in store.snapshot()['preferences']
    assert store.snapshot()['preferences'][unit] == 'SOL'


@pytest.mark.parametrize('changes', [
    {'lamports': '-1'}, {'lamports': '01'}, {'lamports': '1.5'},
    {'lamports': 2000000}, {'lamports': '18446744073709551616'},
    {'lamports': '٢'}, {'usd': -1}, {'usd': True},
    {'usd': float('nan')}, {'usd': float('inf')}, {'at': True},
    {'at': 0}, {'at': float('nan')}, {'secret': 'never persist'},
])
def test_invalid_native_balance_history_keeps_prior_state(tmp_path, changes):
    store = AppStateStore(tmp_path)
    key = 'pumpwizard.wallet-balance-history/v1:mainnet-beta:' + wallet()['address']
    row = {'at': 1000, 'lamports': '2000000', 'usd': .24}
    store.preference(key, json.dumps([row]))
    before = store.snapshot()
    with pytest.raises(ValueError, match='wallet balance history'):
        store.preference(key, json.dumps([{**row, **changes}]))
    assert store.snapshot() == before


def test_native_balance_history_rejects_bad_scope_order_size_and_unit(tmp_path):
    store = AppStateStore(tmp_path)
    address = wallet()['address']
    key = 'pumpwizard.wallet-balance-history/v1:mainnet-beta:' + address
    row = {'at': 1000, 'lamports': '2000000', 'usd': None}
    for rows in ([row, row], [{**row, 'at': 2000}, row], [row] * 701):
        with pytest.raises(ValueError):
            store.preference(key, json.dumps(rows))
    for prefix in ('pumpwizard.wallet-balance-history/v1:', 'pumpwizard.wallet-balance-unit/v1:'):
        for scope in ('testnet:' + address, 'devnet:../wallet', 'devnet:' + address + ':extra'):
            with pytest.raises((ValueError, RuntimeError)):
                store.preference(prefix + scope, None)
    with pytest.raises(ValueError, match='wallet balance unit'):
        store.preference('pumpwizard.wallet-balance-unit/v1:mainnet-beta:' + address, 'EUR')
    assert store.snapshot() == {'wallets': [], 'preferences': {}}


def test_account_api_requires_authentication_and_survives_server_restart(tmp_path):
    token = 'server-account-test-token-with-enough-length'
    record = wallet()
    for iteration in range(2):
        server = serve(tmp_path, token, port=0)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_address[1]}/api/v1/app-state'
        headers = {'Origin': 'http://127.0.0.1:5173', 'X-PumpWizard-Browser-Token': token}
        try:
            for extra in ({}, {'Origin': 'https://untrusted.example', 'X-PumpWizard-Browser-Token': token}):
                with pytest.raises(HTTPError) as rejected:
                    urlopen(Request(url, headers=extra))
                assert rejected.value.code == 403
                with pytest.raises(HTTPError) as rejected_write:
                    urlopen(Request(url+'/wallets', data=json.dumps({'wallet':record}).encode(), headers=extra))
                assert rejected_write.value.code == 403
            if iteration == 0:
                with urlopen(Request(url+'/wallets', data=json.dumps({'wallet':record}).encode(), headers=headers)) as response:
                    assert json.load(response)['wallets'] == [record]
            with urlopen(Request(url, headers=headers)) as response:
                assert response.headers['Cache-Control'] == 'no-store'
                assert json.load(response)['wallets'] == [record]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
