"""Local RPC credentials; public API views never return endpoint URLs."""
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

from .transfers import ENDPOINTS, TransferError, TransferRpc

NETWORK = 'mainnet-beta'


def validate_url(value):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(c.isspace() for c in value):
            raise ValueError()
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                parsed.password or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError()
    except ValueError:
        raise TransferError('Enter a complete HTTPS RPC URL without a username, password or fragment') from None
    return value


def _path(directory):
    return Path(directory) / 'rpc-settings.json'


def endpoint(network, directory=None):
    if network not in ('mainnet-beta', 'devnet'):
        raise TransferError('Choose mainnet-beta or devnet')
    if directory is not None and network == NETWORK:
        path = _path(directory)
        try:
            if path.exists():
                if path.is_symlink() or path.stat().st_size > 12_000:
                    raise ValueError()
                record = json.loads(path.read_text())
                if record.get('schema') != 'pumpwizard.rpc-settings/v1' or record.get('network') != NETWORK:
                    raise ValueError()
                return validate_url(record['url']), 'saved'
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise TransferError('Saved RPC settings could not be read; replace them in RPC connection') from None
    variable = 'SOLANA_RPC_URL' if network == NETWORK else 'SOLANA_DEVNET_RPC_URL'
    configured = os.environ.get(variable)
    return (validate_url(configured), 'environment') if configured else (ENDPOINTS[network], 'public')


def overview(directory):
    url, source = endpoint(NETWORK, directory)
    host = urlsplit(url).hostname
    provider = ('Solana public RPC' if url == ENDPOINTS[NETWORK] else
                'Helius' if host == 'mainnet.helius-rpc.com' else 'Custom RPC')
    return dict(network=NETWORK, source=source, provider=provider)


class ConnectionProbe(TransferRpc):
    allowed_methods = frozenset({'getGenesisHash', 'getLatestBlockhash'})


def check(url):
    rpc = ConnectionProbe(NETWORK, validate_url(url))
    rpc.genesis()
    block = rpc.call('getLatestBlockhash', [{'commitment': 'confirmed'}])
    try:
        from .pump_route import key
        from .transfers import _uint
        key(block['value']['blockhash'])
        _uint(block['value']['lastValidBlockHeight'], 'block height')
    except (KeyError, TypeError):
        raise TransferError('RPC returned an invalid latest blockhash') from None


def save(directory, url):
    url = validate_url(url)
    check(url)  # Failed authentication or wrong chain never replaces a working configuration.
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.rpc-settings-', dir=directory)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(dict(schema='pumpwizard.rpc-settings/v1', network=NETWORK, url=url), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _path(directory))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return overview(directory)


def command(directory, body):
    if not isinstance(body, dict):
        raise TransferError('Choose an RPC connection action')
    if set(body) == {'action', 'url'} and body['action'] == 'save':
        return dict(connection=save(directory, body['url']), notice='Mainnet connection verified and saved. Request a fresh order review.')
    if body == {'action': 'test'}:
        url, _ = endpoint(NETWORK, directory)
        check(url)
        return dict(connection=overview(directory), notice='Mainnet identity and current blockhash verified. No transaction was sent.')
    if body == {'action': 'reset'}:
        _path(directory).unlink(missing_ok=True)
        return dict(connection=overview(directory), notice='Saved RPC connection removed. Request a fresh order review.')
    raise TransferError('Choose save, test or reset for the RPC connection')
