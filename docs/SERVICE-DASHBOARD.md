# Background-service dashboard contract

Prepared September 28, 2026 for the first local background-service milestone.
This document records the frontend contract actually consumed by Dashboard. It
does not claim production trading acceptance.

## Trust and continuation model

Dashboard is an authenticated control and inspection client. Loading it, owning
the launch cookie, signing into a wallet, or unlocking the browser signer does
not authorize the background service to trade.

The user chooses custody when creating an Autopilot session:

- `main-wallet-browser` retains the existing behavior. The browser holds the
  signer and refresh, tab closure, sign-out, disconnect, or browser lock stops
  signing.
- `main-wallet-service` creates an immutable v4 session brief with
  `service_vault_required` and `continues_without_browser`. It is available only
  to a PumpWizard Generated Wallet. The same address and browser backup remain;
  a second encrypted envelope is enrolled in the local service.
- Extension wallets remain interactive. Dashboard never asks for or extracts
  their private key.

Service custody is whole-wallet software control while its signer is explicitly
unlocked. Duration, budget, order, route, slippage, position and daily limits are
application checks rather than on-chain caps. The computer and user service must
remain awake. Service restart, crash, shutdown, worker fault, or detected
sleep/clock discontinuity locks signing. Already submitted transactions remain
eligible for reconciliation and cannot be cancelled by a stop action.

## API contracts consumed by Dashboard

All responses use the existing `pumpwizard.web/v1` envelope. The typed client is
implemented in `web-client/src/serviceControl.ts`.

Read-only status:

- `GET /api/v1/service/status` returns `service`, `jobs`, `coverage`, and the
  redacted `trading` state. Missing or invalid status is displayed as
  disconnected/state unknown, never healthy.
- `GET /api/v1/service/signing?sender=<address>&network=<network>` requires the
  already authenticated wallet identity and returns the redacted trading state,
  encrypted enrollment metadata, and allowances for that exact wallet.

Commands use only the exact server fields:

- `POST /api/v1/service/jobs/strategy-monitor` with
  `{enabled, interval_seconds}`.
- `POST /api/v1/service/signing/enroll` with
  `{sender, network, envelope, service_unlock_secret}`. The browser creates the
  `pumpwizard.service-wallet-vault/v1` envelope with
  `prepareServiceVaultEnrollment`; the browser-wallet password is never sent.
- `POST /api/v1/service/signing/review`, `/authorize`, and `/start` are the
  published bounded-allowance lifecycle. Dashboard recognizes and displays
  their allowance/status output; production Start remains unavailable while
  `production_activation` is `blocked`.
- `POST /api/v1/service/pause-entries` and
  `/api/v1/service/revoke-trading` use `{}`.
- `POST /api/v1/service/resume-entries` uses
  `{allowance_id, confirmation}` and the exact `RESUME <SESSION8>` phrase.
- `POST /api/v1/service/shutdown` uses `{}`.

There is no generic job-execution, arbitrary-signing, key-export, or
caller-selected live-capability endpoint.

## Frontend state and restart behavior

`useServiceControl(wallet)` performs one read-only status poll every five seconds
while a dashboard wallet is signed in. It never starts a collector or trading
job. Explicit user clicks are the only source of command POSTs.

The hook retains:

- connection phase and checked time;
- service identity, monotonic instance revision, health and activity;
- the single persisted strategy-monitor job;
- source/time-linked collector coverage and gaps;
- redacted signer, custody, authorization, pause, expiry and blocker state;
- wallet-authenticated enrollment metadata and allowances.

Responses older than the latest in-tab request or service revision are ignored.
The last instance identity/revision is stored in tab session storage. A new
instance or higher revision clears the client approval marker and raises a
restart warning. It does not restore authorization. Different tabs only inspect
the same service; they do not create per-tab workers.

## User-visible controls

`ServiceControlDialog` shows the actual Runner version, opaque instance identity,
health, heartbeat, collector cadence/last success/failures, source coverage,
service-custody state, signer lock, authorization expiry, entries state and
production blockers.

Enrollment uses three uncontrolled password inputs so secrets are not retained
in React state, URLs, browser storage, logs, or status. The fields are cleared
after both success and failure. Enrollment requires:

1. an unlocked generated browser wallet;
2. its existing wallet password;
3. a distinct service password and confirmation; and
4. explicit acknowledgement of the whole-wallet software-control boundary.

Operational actions are deliberately separate:

- **Pause entries** preserves eligible exits.
- **Resume entries** requires an unexpired, unlocked allowance and its exact
  confirmation phrase; it cannot renew or widen authority.
- **Stop & lock trading** has a consequence confirmation and revokes the bounded
  authorization.
- **Sign out of dashboard** removes the current browser/dashboard session. It
  does not stop service-owned read-only work or revoke separate service custody.
- **Shut down background service** has its own confirmation, locks signing first,
  stops read-only workers, and flushes state.

The command board exposes the same service as one actionable status card and
shows source, quote/gap coverage and last successful scan. Degraded/stale/restart
states become an attention signal. Disconnected service status remains an
informational unknown state so the ordinary browser-owned Runner is not falsely
reported as healthy or broken.

Existing v1-v3 briefs keep their original custody. Only an explicit new
background-service choice writes a v4 service-custody brief. Session Monitor
renders the custody recorded in that brief and links service-custody sessions to
the service controls.

## Verification performed

- `npm run lint` passed. TypeScript and the isolated Vite production build at
  `/tmp/pumpwizard-batch2-final-build-20260928` passed with the existing
  chunk-size advisory.
- All 41 production-bundle Playwright flows passed in 1.5 minutes on port 8794;
  output is `/tmp/pumpwizard-batch2-final-playwright-20260928`.
- The principal workflow uses real authenticated service routes plus an injected
  synthetic market/chain. A test-runner API observer releases the entry signal
  only after every browser context closes. Entry and exit both finalize during
  that browser-closed interval; observer reads never drive execution or provide
  a heartbeat.
- Reopening shows the same service/session and durable receipts without another
  order. Process restart preserves enrollment and job state but displays locked
  custody. The old allowance cannot Start; a fresh review, exact authorization
  and password-confirmed Start succeed only in the injected synthetic runtime.
- Fixture teardown removes its generated service session and financial rows, so
  the remaining 40 browser cases run against clean state. Multi-tab, API loss,
  narrow-layout, direct-monitoring, browser-custody and exact-ticket regressions
  remain covered.

## Remaining release limits

- Production service Start is intentionally hard-blocked pending separately
  reviewed funded entry/exit/restart acceptance and a versioned release.
- Dashboard exposes the bounded review and inactive-authorization steps for an
  explicit v4 service-custody session. Authorization requires the exact
  `ALLOW SERVICE <SESSION8>` phrase; production Start remains unavailable.
- Browser acceptance uses a real isolated service and authenticated routes with
  deterministic synthetic market/chain dependencies. It is not funded or live-
  provider acceptance.
- No real wallet was migrated, no funded transaction was sent, no service was
  deployed or restarted, and no version or Community publication state changed.
