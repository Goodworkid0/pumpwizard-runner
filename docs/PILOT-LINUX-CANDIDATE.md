# Local Linux pilot candidate

This procedure operates an isolated PumpWizard Runner candidate. It does not
install over the current application, enable a user service, change linger,
open real keys, perform a funded action or publish a release.

## Supported and untested environments

The current dev66 candidate is built and accepted on Debian GNU/Linux 12, x86-64,
with the system Chromium browser. The archive is a PyInstaller one-directory
application: keep `SolanaMarketWizard` and `_internal` together. Other Linux
distributions, ARM, Windows, macOS, OS logout/reboot and suspend/resume remain
untested until their own package acceptance is run.

## Inspect the candidate

Use the paths printed in the monthly acceptance record. The examples deliberately
use `/tmp` and final-integration port `8865`, not the user's installed service or
live data.

```sh
candidate_path=/tmp/pumpwizard-month4-dev66-final-stage/dist/dev66/SolanaMarketWizard/SolanaMarketWizard
artifact_path=/tmp/pumpwizard-month4-dev66-final-artifacts
"$candidate_path" --version
(cd "$artifact_path" && sha256sum -c SHA256SUMS-linux-x86_64.txt)
```

Inspect `BUILD-IDENTITY.json`, `SOURCE-MANIFEST.json`, `START-HERE.txt` and the
external candidate manifest before use. Their identifiers are non-secret and
contain relative input/bundle paths only. The Dashboard shows shortened build,
service-instance and data-directory identities; it never renders the private
filesystem path. A build mismatch is a stop-and-repair condition.

## Start, reopen and stop

Choose a new absolute data directory. `service start` creates one private local
access token, starts one detached authenticated loopback service and reuses it
only when version and opaque data identity match. A foreign listener or an older
verified Runner on the selected port is refused.

```sh
data_path=/tmp/pumpwizard-month4-dev66-package-data
"$candidate_path" --data-dir "$data_path" service start --port 8865
"$candidate_path" --data-dir "$data_path" service status --port 8865
"$candidate_path" --data-dir "$data_path" service open-dashboard --port 8865
```

Closing the browser does not stop explicitly enabled read-only collection or a
campaign owned by the service. Reopen with `open-dashboard`; do not start another
source server. Dashboard sign-out is also separate from service shutdown.

Stop explicitly when the isolated run is finished:

```sh
"$candidate_path" --data-dir "$data_path" service stop --port 8865
"$candidate_path" --data-dir "$data_path" service status --port 8865
```

Shutdown locks in-memory signing before the service exits. A process restart
never restores a signer or client-side approval. Persisted read-only jobs may be
present, but campaign restart gaps and explicit Resume requirements remain
visible.

## Backup and restore rehearsal

Stop the verified service first. The backup uses SQLite's backup API, verifies
integrity and copies encrypted service-wallet ciphertext without launch tokens,
logs or lock files. Both backup and restore destinations must be new paths.

```sh
backup_path=/tmp/pumpwizard-month4-dev66-package-backup
restored_path=/tmp/pumpwizard-month4-dev66-package-restored
"$candidate_path" --data-dir "$data_path" service backup --destination "$backup_path"
"$candidate_path" --data-dir "$restored_path" service restore --backup "$backup_path"
"$candidate_path" --data-dir "$restored_path" service start --port 8865
"$candidate_path" --data-dir "$restored_path" service status --port 8865
"$candidate_path" --data-dir "$restored_path" service stop --port 8865
```

Verify the restored SQLite files with `PRAGMA quick_check`, then inspect paper
runs, campaigns, provider records, alerts, exposures and retained receipts. The
restored directory has a different opaque data identity and a new access token;
service signing starts locked.

## Staged upgrade and rollback

Before an upgrade, stop the old verified service and create a backup with the
old executable. Rehearse the new candidate on a restored copy first. Only after
that isolated check should a later deliberate install point the new executable
at selected user data.

Do not run old code against a directory already opened by newer schema code.
Rollback means stopping the new candidate and restoring the pre-upgrade backup
into another new directory, then starting the old executable against that
restored directory. Keep the failed/newer data directory for inspection.

The bundled `.desktop` file points to the exact candidate executable. Copying or
enabling it is outside this handoff; no systemd unit is installed or started.

## Readiness and evidence boundaries

Provider readiness distinguishes usable quotes, an empty eligible universe,
partial rejection, stale evidence, failure and exhausted quota. Expanding the
provider detail shows bounded stage counts and sanitized reasons, never endpoint
URLs or credentials. Reading status does not trigger collection or a paid call.

Local evidence explanations must remain usable without an AI provider. Optional
AI requests retain the existing frozen preview and explicit approval. Evidence
links identify retained campaign/report/decision/holding records; they grant no
wallet, signing, provider-purchase or execution authority.

This candidate is local acceptance evidence only. A Community draft is not a
published note, and the live release-note check is still required before any
future public announcement.
