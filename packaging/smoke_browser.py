"""Start an actual packaged executable against disposable data; never funds it."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('executable', type=Path)
    args = parser.parse_args()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='pumpwizard-package-smoke-') as directory:
        executable = str(args.executable.resolve())
        lifecycle = [executable, '--data-dir', directory, 'service']
        try:
            subprocess.run(
                [*lifecycle, 'start', '--port', str(port)],
                check=True, capture_output=True, text=True, timeout=45,
            )
            origin = f'http://127.0.0.1:{port}'
            token = (Path(directory) / 'background-service-access-token').read_text().strip()
            headers = {
                'Origin': origin,
                'Sec-Fetch-Site': 'same-origin',
                'X-PumpWizard-Browser-Token': token,
            }

            def read(path):
                request = urllib.request.Request(origin + path, headers=headers)
                with urllib.request.urlopen(request, timeout=3) as response:
                    return response.read()

            assert b'<html' in read('/').lower()
            result = json.loads(read('/api/v1/diagnostics'))
            assert result['report']['automatic_signing'] is False
            frontend_identity = json.loads(read('/build-identity.json'))
            service = json.loads(read('/api/v1/service/status'))
            runtime_identity = service['build_identity']
            identity_fields = {
                'schema', 'build_id', 'version', 'source_checkpoint',
                'packaged_at', 'package_kind',
            }
            assert {name: frontend_identity[name] for name in identity_fields} == {
                name: runtime_identity[name] for name in identity_fields
            }
            assert runtime_identity['package_kind'] != 'source'
            capabilities = json.loads(read('/api/v1/capabilities'))['capabilities']
            assert capabilities['production_execution']['state'] == 'blocked'
            assert capabilities['production_execution']['automatic_execution'] is False
            assert service['trading']['production_activation'] == 'blocked'
            assert service['trading']['signer_state'] == 'locked'
            print(
                'Executable service startup, static client, exact build identity '
                'and blocked production diagnostics passed.'
            )
        finally:
            subprocess.run(
                [*lifecycle, 'stop', '--port', str(port)],
                capture_output=True, text=True, timeout=15,
            )


if __name__ == '__main__':
    main()
