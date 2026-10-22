"""Optional encrypted AI credentials, separate from the trading wallet."""

import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .agent_hub import AgentHubError, _validate_profile


class AIKeys:
    def __init__(self, directory):
        self.directory = Path(directory) / 'ai-credentials'
        self.session = {}

    def path(self, profile):
        _validate_profile(profile)
        return self.directory / (profile.id + '.json')

    def available(self, profile):
        return bool(self.get(profile))

    def exists(self, profile):
        return self.path(profile).exists()

    @staticmethod
    def aad(profile):
        return ('solanamarket.ai-key/v1|' + profile.id + '|' + profile.endpoint).encode()

    @staticmethod
    def derive(password, salt):
        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise AgentHubError('Use an AI-key password of 12–1024 characters')
        return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64*1024*1024)

    def set(self, profile, token, password=None):
        path = self.path(profile)
        if not isinstance(token, str) or not 1 <= len(token.strip()) <= 8192 or '\n' in token or '\r' in token:
            raise AgentHubError('Enter a valid API key')
        token = token.strip()
        if password is not None:
            salt, nonce = os.urandom(16), os.urandom(12)
            encrypted = AESGCM(self.derive(password, salt)).encrypt(nonce, token.encode(), self.aad(profile))
            record = dict(schema='solanamarket.ai-key/v1', salt=base64.b64encode(salt).decode(),
                          nonce=base64.b64encode(nonce).decode(), ciphertext=base64.b64encode(encrypted).decode())
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary = tempfile.mkstemp(dir=self.directory, prefix='.ai-key-')
            try:
                with os.fdopen(descriptor, 'w') as stream:
                    json.dump(record, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        else:
            path.unlink(missing_ok=True)  # Session-only replacement must not revive an older saved key.
        self.session[profile.id] = (profile.endpoint, token)

    def unlock(self, profile, password):
        path = self.path(profile)
        try:
            if path.is_symlink() or path.stat().st_size > 20_000:
                raise ValueError()
            record = json.loads(path.read_text())
            if not isinstance(record, dict) or record.get('schema') != 'solanamarket.ai-key/v1':
                raise ValueError()
            salt, nonce, ciphertext = (base64.b64decode(record[k], validate=True) for k in ('salt', 'nonce', 'ciphertext'))
            if len(salt) != 16 or len(nonce) != 12:
                raise ValueError()
            token = AESGCM(self.derive(password, salt)).decrypt(nonce, ciphertext, self.aad(profile)).decode()
        except (OSError, ValueError, KeyError, TypeError, InvalidTag):
            raise AgentHubError('Could not unlock the saved AI key. Check the AI-key password.') from None
        self.session[profile.id] = (profile.endpoint, token)
        return token

    def get(self, profile):
        endpoint, token = self.session.get(profile.id, ('', ''))
        return token if endpoint == profile.endpoint else ''

    def forget(self, profile):
        self.session.pop(profile.id, None)
        self.path(profile).unlink(missing_ok=True)

    def lock(self):
        self.session.clear()
