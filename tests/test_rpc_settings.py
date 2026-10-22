import io
import json
import os
import urllib.error
from unittest.mock import Mock

import pytest

from pumpwizard_runner import rpc_settings, web_trading
from pumpwizard_runner.library import StrategyLibrary
from pumpwizard_runner.route_check import ProbeRpcError, RouteCheckRpc
from pumpwizard_runner.transfers import ENDPOINTS, GENESIS, RpcError, TransferError, TransferRpc
from pumpwizard_runner.web_api import command, payload


URL = 'https://mainnet.helius-rpc.com/?api-key=synthetic-secret'


@pytest.fixture
def connection(monkeypatch):
    monkeypatch.delenv('SOLANA_RPC_URL', raising=False)
    monkeypatch.delenv('SOLANA_DEVNET_RPC_URL', raising=False)
    calls = []

    def call(self, method, params):
        calls.append((self.url, method))
        assert method in {'getGenesisHash', 'getLatestBlockhash'}
        if method == 'getGenesisHash':
            return GENESIS['mainnet-beta']
        return {'value': {'blockhash': '1' * 32, 'lastValidBlockHeight': 100}}

    monkeypatch.setattr(rpc_settings.ConnectionProbe, 'call', call)
    return calls


def test_local_api_saves_private_url_and_tests_without_wallet(tmp_path, connection):
    library = StrategyLibrary(tmp_path)
    try:
        assert payload(library, '/api/v1/rpc', {})['connection']['source'] == 'public'
        result = command(library, '/api/v1/rpc', {'action': 'save', 'url': URL})
        assert result['connection'] == {'network': 'mainnet-beta', 'source': 'saved', 'provider': 'Helius'}
        assert 'synthetic-secret' not in json.dumps(result)
        assert connection == [(URL, 'getGenesisHash'), (URL, 'getLatestBlockhash')]
        assert rpc_settings.endpoint('mainnet-beta', tmp_path) == (URL, 'saved')
        assert web_trading.rpc_for('mainnet-beta', tmp_path).url == URL
        assert rpc_settings.endpoint('devnet', tmp_path) == (ENDPOINTS['devnet'], 'public')
        assert not (tmp_path / 'wallet-transfers.sqlite').exists()
        if os.name == 'posix':
            assert (tmp_path / 'rpc-settings.json').stat().st_mode & 0o777 == 0o600
        assert 'synthetic-secret' not in json.dumps(payload(library, '/api/v1/rpc', {}))
        assert 'verified' in command(library, '/api/v1/rpc', {'action': 'test'})['notice']
        assert command(library, '/api/v1/rpc', {'action': 'reset'})['connection']['source'] == 'public'
        assert not (tmp_path / 'rpc-settings.json').exists()
    finally:
        library.close()


def test_saved_choice_overrides_environment_until_removed(tmp_path, connection, monkeypatch):
    configured = 'https://example.com/rpc?key=another-synthetic-secret'
    monkeypatch.setenv('SOLANA_RPC_URL', configured)
    assert rpc_settings.endpoint('mainnet-beta', tmp_path) == (configured, 'environment')
    rpc_settings.save(tmp_path, URL)
    assert rpc_settings.endpoint('mainnet-beta', tmp_path) == (URL, 'saved')
    rpc_settings.command(tmp_path, {'action': 'reset'})
    assert rpc_settings.endpoint('mainnet-beta', tmp_path) == (configured, 'environment')


@pytest.mark.parametrize('url', ['http://example.com', 'https://user:secret@example.com',
    'https://example.com/#secret', 'https://example.com:99', 'https://', 'https://example.com/ secret', {}, None])
def test_invalid_url_never_connects_or_writes(tmp_path, connection, url):
    with pytest.raises(TransferError, match='HTTPS RPC URL'):
        rpc_settings.save(tmp_path, url)
    assert connection == []
    assert not (tmp_path / 'rpc-settings.json').exists()


@pytest.mark.parametrize('failure', ['wrong-network', 'bad-block', 'denied'])
def test_failed_check_preserves_working_connection(tmp_path, connection, monkeypatch, failure):
    rpc_settings.save(tmp_path, URL)
    previous = (tmp_path / 'rpc-settings.json').read_bytes()

    def call(self, method, params):
        if failure == 'denied':
            raise RpcError(method, http_status=401)
        if method == 'getGenesisHash':
            return GENESIS['devnet' if failure == 'wrong-network' else 'mainnet-beta']
        return {'value': {}}

    monkeypatch.setattr(rpc_settings.ConnectionProbe, 'call', call)
    with pytest.raises(TransferError) as error:
        rpc_settings.save(tmp_path, 'https://example.com/?key=bad-secret')
    assert 'secret' not in str(error.value)
    assert (tmp_path / 'rpc-settings.json').read_bytes() == previous


def test_corrupt_saved_record_fails_without_public_fallback(tmp_path, connection):
    (tmp_path / 'rpc-settings.json').write_text('{synthetic-secret')
    with pytest.raises(TransferError, match='Saved RPC settings could not be read') as error:
        rpc_settings.endpoint('mainnet-beta', tmp_path)
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('status', [401, 403, 429, 503])
def test_http_diagnostics_redact_url_and_do_not_retry_broadcast(monkeypatch, status):
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError(URL, status, 'synthetic-secret',
        {'Retry-After': '10'}, io.BytesIO(b'synthetic-secret'))
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: opener)
    rpc = TransferRpc('mainnet-beta', URL)
    for method in ('getGenesisHash', 'sendTransaction'):
        with pytest.raises(RpcError) as error:
            rpc.call(method, [])
        assert 'synthetic-secret' not in str(error.value)
        assert URL not in str(error.value)
        assert error.value.details['http_status'] == status
        assert error.value.details['method'] == method
        if status == 429:
            assert '10 seconds' in str(error.value)
    assert opener.open.call_count == 2


def test_json_rpc_error_never_includes_provider_text(monkeypatch):
    opener = Mock()
    opener.open.return_value.__enter__ = Mock(return_value=io.BytesIO(json.dumps({
        'error': {'code': -32601, 'message': URL, 'data': 'synthetic-secret'}
    }).encode()))
    opener.open.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: opener)
    with pytest.raises(RpcError, match='does not support getGenesisHash') as error:
        TransferRpc('mainnet-beta', URL).call('getGenesisHash', [])
    assert 'synthetic-secret' not in str(error.value)
    assert error.value.details['rpc_code'] == -32601


def test_unsigned_probe_preserves_safe_rate_limit_evidence(monkeypatch):
    def call(self, method, params):
        raise RpcError(method, http_status=429, retry_after=10)

    monkeypatch.setattr(TransferRpc, 'call', call)
    rpc = RouteCheckRpc('mainnet-beta', URL)
    with pytest.raises(ProbeRpcError, match='10 seconds'):
        rpc.call('getGenesisHash', [])
    assert rpc.rpc_errors == [dict(method='getGenesisHash', http_status=429,
                                  rpc_code=None, retry_after_seconds=10)]
