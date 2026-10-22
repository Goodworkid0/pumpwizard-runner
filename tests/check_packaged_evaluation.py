"""Exercise frozen import/replay and a retained dev41 library with temporary data."""

import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import zipfile

from pumpwizard_runner import __version__
from pumpwizard_runner.release_notes import note_for, note_url
from pumpwizard_runner.evaluation import example_recording
from pumpwizard_runner.library import DATABASE_NAME
from pumpwizard_runner.portfolio import CONFIG
from pumpwizard_runner.strategies import content_hash


ROOT = Path(__file__).parents[1]
BUILD = __version__.split('.')[-1]
NEW = ROOT / f'dist/{BUILD}/SolanaMarketWizard/SolanaMarketWizard'
OLD = ROOT / 'dist/dev41/SolanaMarketWizard/SolanaMarketWizard'


def main():
    assert subprocess.check_output([str(NEW), '--version'], text=True).strip() == f'PumpWizard Runner {__version__}'
    assert 'evaluate-recording' in subprocess.check_output([str(NEW), '--help'], text=True)
    with tempfile.TemporaryDirectory(prefix='wizard-packaged-evaluation-') as temporary:
        directory = Path(temporary)
        package = {'schema': 'pumpwizard.strategy-handoff/v1',
                   'strategy': {'id': 'core-frequency', 'label': 'Retained dev41 agent'}, 'configuration': CONFIG}
        path = directory / 'strategy.json'
        path.write_text(json.dumps(package))
        recording = directory / 'recording.json'
        recording.write_text(json.dumps(example_recording()))

        def run(executable, *args, success=True):
            result = subprocess.run([str(executable), '--data-dir', str(directory), *map(str, args)],
                                    capture_output=True, text=True, timeout=30)
            if success:
                assert result.returncode == 0, result.stderr
            else:
                assert result.returncode == 2, result.stdout
            return result.stdout

        run(OLD, 'import', path)
        created = run(OLD, 'create-instance', content_hash(package), '--entry-mode', 'disabled', '--exit-mode', 'manual')
        agent = created.splitlines()[0].split(': ')[1]
        run(OLD, 'pause', agent)

        def dump():
            db = sqlite3.connect(directory / DATABASE_NAME)
            try:
                return list(db.iterdump())
            finally:
                db.close()

        before = dump()
        first = json.loads(run(NEW, 'evaluate-recording', agent, recording))
        second = json.loads(run(NEW, 'evaluate-recording', agent, recording))
        assert first == second
        assert first['identity']['package_sha256'] == content_hash(package)
        assert first['summary']['event_counts']['fill_partial_exit'] == 1
        assert first['input_origin'] == 'bundled synthetic example'
        assert not first['submission_enabled'] and not first['funded_acceptance']
        run(NEW, 'import', path)
        assert dump() == before, 'Existing imported configuration or agent settings changed'
        bad = example_recording()
        bad['configuration']['order_usd'] += 1
        recording.write_text(json.dumps(bad))
        run(NEW, 'evaluate-recording', agent, recording, success=False)
        assert dump() == before and not list(directory.glob('*wallet*'))
        print('PASS: packaged import, exact replay/report, process reopen/idempotency, rejected mismatch; dev41 library preserved')

    archive = next((ROOT / f'release-artifacts/{BUILD}').glob('*.zip'))
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.testzip() is None
        assert note_url(__version__) in zipped.read('SolanaMarketWizard/PATCH-NOTES.txt').decode()
        assert note_for(__version__) in json.loads(zipped.read('SolanaMarketWizard/_internal/pumpwizard_runner/data/runner-community-posts.json'))
        assert json.loads((ROOT / f'release-artifacts/{BUILD}/community-post-{__version__}.json').read_text()) == [note_for(__version__)]
        assert (zipped.getinfo('SolanaMarketWizard/SolanaMarketWizard').external_attr >> 16) & 0o111
        assert 'SolanaMarketWizard/_internal/pumpwizard_runner/data/core-frequency-replay.json' in zipped.namelist()
        assert not any('.sqlite' in name or Path(name).name == '.env' or 'solanamarket-wallet.json' in name
                       for name in zipped.namelist())
    print('PASS: archive integrity, executable permissions, bundled recording; no wallet/database files')

    # A fresh packaged startup must reach setup without creating or unlocking a wallet.
    with tempfile.TemporaryDirectory(prefix=f'wizard-{BUILD}-startup-') as temporary:
        child = subprocess.Popen([str(NEW), '--data-dir', temporary], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise AssertionError(child.communicate()[1].decode())
                tree = subprocess.check_output(['xwininfo', '-root', '-tree'], text=True)
                if 'Set up wallet' in tree:
                    assert not list(Path(temporary).glob('*wallet*'))
                    print('PASS: packaged desktop reaches isolated setup without creating a wallet')
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Packaged desktop setup did not appear')
        finally:
            child.terminate()
            child.wait(timeout=10)


if __name__ == '__main__':
    main()
