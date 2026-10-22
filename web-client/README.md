# PumpWizard Runner browser client

This is the active React presentation layer for the local Runner library. It
does not contain trading rules, a local database, a wallet vault, a decrypted
signer, an AI key, an order service, or an execution worker. Python remains the
authoritative implementation.

## Local development

Install the JavaScript dependencies once:

```sh
npm install
```

Start both local processes with one fresh token. The API process accepts only
loopback addresses and the Vite development proxy supplies the token; do not
put the value in source, a `.env` file, or a browser URL.

```sh
export PUMPWIZARD_BROWSER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
PYTHONPATH=../src python -m pumpwizard_runner web-api --browser-token "$PUMPWIZARD_BROWSER_TOKEN"
```

In a second terminal:

```sh
PUMPWIZARD_BROWSER_TOKEN="$PUMPWIZARD_BROWSER_TOKEN" npm run dev -- --host 127.0.0.1
```

Open <http://127.0.0.1:5173>. Build, lint, and run the isolated browser acceptance flow with:

```sh
npm run build
npm run lint
npm run test:e2e
```

The end-to-end command builds the client, starts the real Python API against a
temporary library, and drives Chromium through encrypted wallet creation,
backup, lock/unlock, deposit, Wallet Standard ownership proof, manual-trade
signing, strategy/session operations, evaluation, replay, compact layout and
keyboard use. The local-wallet flow verifies the produced Ed25519 signature over
an exact synthetic legacy transaction. It uses a fixed test balance and never
opens the user's Runner data directory.

## Built local browser app

After a build, Python can serve the browser files and API from one loopback
origin. This does not need Vite at runtime. The launch token is set only as a
session-only, `HttpOnly`, `SameSite=Strict` cookie scoped to `/api`; it is not
in the browser bundle, page source, URL, local storage, or strategy library.

```sh
npm run build
PYTHONPATH=../src python -m pumpwizard_runner browser
```

Runner generates the per-launch credential internally, opens
<http://127.0.0.1:8765>, and keeps the server on loopback. Pass `--no-open` to
print and serve the URL without opening the system browser.

`web-client/dist` is the production browser output. The Python service hosts it
and the API at one loopback origin. Native packaging is a legacy delivery path;
the explicit `desktop` command starts the legacy native client. Release notes
and funded acceptance remain separate gates.

## Current browser scope

The client reads real local workspace, market, coin, session, strategy,
evaluation, replay, shadow-report, worker-rehearsal, and public wallet-policy
records from the Python API. It starts with wallet onboarding:

- connect Phantom or another compatible Wallet Standard extension;
- choose among connected extensions and every generated wallet saved in the
  browser;
- create multiple Solana wallets encrypted in browser storage with passwords;
- download and restore its encrypted JSON backup;
- prove ownership with a single-use signed message;
- load the selected network's live SOL balance; and
- show a deposit address, QR code, copy action and balance refresh.

After sign-in, its explicit commands include:

- importing an unchanged `pumpwizard.strategy-handoff/v1` package;
- creating a local research draft through the existing Python template;
- creating disabled/manual sessions and pausing or retrying local monitoring;
- bounded public market scans;
- offline modeled evaluations; and
- isolated virtual portfolio replays; and
- supported manual Pump/PumpSwap buy and sell review, signing, submission and
  receipt reconciliation through the Python trade service.

The browser cannot start a worker, automate wallet approval, withdraw, edit
wallet risk policy, or claim funded acceptance. Assistant provider requests
retain their existing one-time approval and bounded-context approval steps.
Real extension transaction acceptance, unattended trading, live provider
acceptance, and funded lifecycle acceptance remain pending. See
[`../docs/WEB-MIGRATION.md`](../docs/WEB-MIGRATION.md) for the complete plan and
current evidence.
