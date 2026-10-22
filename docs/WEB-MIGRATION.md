# Browser migration plan

Latest unreleased source status: browser source expansion.
It supersedes older capability limits below for strategy reviews, withdrawals,
automation-vault preparation, pool charts and monitor health. Funded acceptance
and unattended signing remain blocked.

The web client is a new presentation layer over the existing Runner services.
It must not duplicate trading rules in TypeScript or expose the SQLite files,
wallet vault, AI keys, signer, reservation journal, or execution worker to the
browser.

The browser client is the primary product interface from dev53. The native Tk
client is the legacy Linux interface for capabilities that have not crossed the
browser acceptance gates. New user-interface work belongs in `web-client/`;
the legacy client receives compatibility, recovery, and security fixes while
its remaining workflows are migrated.

## 1. Local API foundation

Add a loopback-only Python API process that opens the existing library and
returns versioned JSON for workspace state, market rows, sessions, positions,
monitor health, decisions, strategy records and retained coin history. Require
an origin allowlist and a per-launch random browser token. Later phase-specific
commands remain narrow and Python-validated; there is no generic mutation route.

## 2. Browser shell and live workspace

Replace placeholder React values with API queries. Reproduce the desktop header,
wallet summary, Active Sessions rail, Open Positions rail, Market table,
filters, selected-session state and stale-data labels at desktop-width parity.

## 3. Strategy library and onboarding

Move marketplace browsing, evidence status, compatibility detail, strategy
comparison, local JSON file selection and import inspection to the browser.
Keep the existing `pumpwizard.strategy-handoff/v1` contract unchanged. Import
continues to be validated by Python before persistence.

## 4. Sessions, monitoring, and recovery

Expose create/pause/retry monitoring actions through narrowly scoped API
commands that call the existing scheduler/library services. Render blocked
decisions, monitor history, stale scans, lease ownership and recovery guidance.
Do not allow a browser command to bypass local strategy compatibility checks.

## 5. Market and coin evidence

Connect live retained market rows, exact-mint coin details, local history,
session activity and market refresh. Preserve the existing public-source bounds,
data freshness handling and explicit distinction between retained evidence and
complete trading history.

## 6. Assistant and consented context

Move saved local chat history and the assistant setup flow behind an API that
keeps provider keys in the existing local key store. Browser requests may ask
for bounded strategy/coin context only through the existing single-use preview
and approval flow. Never send wallet data automatically.

## 7. Evaluation, replay, shadow, and worker rehearsal

Expose retained evaluation reports, portfolio replay inputs/results, unsigned
shadow reports and worker-rehearsal status/export. Run all replay, simulation
and worker logic in Python; the browser only starts approved local operations
and displays their immutable reports.

## 8. Wallet visibility and local policy

Add wallet address/balance display, wallet risk policy, reservations, portfolio
valuation and transaction recovery status. The Python API never receives a
created browser wallet's password, seed or decrypted signer, and it never serves
the raw local wallet database. The browser-created signer exists only in browser
memory after password unlock. Dev56 adds authenticated main-wallet allocation
controls for each strategy session. Python owns the durable cash reservation,
per-strategy buy/sell permissions, trade-size and daily limits, loss ceilings
and open-position capacity.

## 9. Reviewed browser wallet integration

Dev54 added Wallet Standard connect and a server-verified, single-use Sign In
With Solana style challenge. Dev55 makes that wallet sign-in the first screen
and adds an encrypted browser-created wallet, backup/restore, balance and deposit
views. The reviewed local API constructs one unsigned transaction after the same
policy, reservation and freshness checks. The active wallet signs that exact
transaction; Python revalidates it, records its signature and broadcasts it
through the existing recovery path. Automatic execution, browser withdrawals,
real-extension transaction acceptance and funded lifecycle acceptance remain
pending.

## 10. Release, parity, and funded acceptance

Run browser end-to-end tests against a temporary local library before platform
builds. Keep cross-client concurrency/lease tests, CSP/origin/token tests,
responsive keyboard coverage, required Windows/macOS/Linux browser assets, and
a versioned Community patch note in the release gate. Funded lifecycle
acceptance remains a separate final gate; do not claim it from simulation,
replay, wallet connection, or unsigned route tests.

## Current implementation status

| Phase | Status in this source tree | Acceptance boundary |
| --- | --- | --- |
| 1. Local API | Implemented for the documented versioned read routes and narrow Python-validated commands. | Loopback, origin, token, and CORS checks are covered by `tests/test_web_api.py`. |
| 2. Browser shell | Implemented for the desktop-width header, sessions rail, positions rail, market filters, stale local-data labels, focus-visible controls, polite state announcements, and labelled modal close controls. | Automated compact-viewport overflow and keyboard-operation checks are present; manual assistive-technology acceptance is still pending. |
| 3. Strategies | Implemented with the unchanged handoff format, local import validation, identity/evidence review, export, and research drafts. | No website contract was changed. |
| 4. Sessions | Implemented for disabled/manual sessions, monitor state, retry, lease visibility, and one explicit bounded scan. | It cannot override a blocked decision or create an execution request. |
| 5. Market evidence | Implemented for retained market data, explicit public refresh, and bounded coin/session history. | Retained observations are not complete trading history. |
| 6. Assistant | Implemented with local chat history, provider setup, token estimates, and separate approval for model and local-context requests. | Live-provider acceptance is pending. |
| 7. Local evidence | Implemented for explicit offline evaluation and virtual replay plus read-only shadow/worker reports and local evidence JSON export. | Browser cannot start a worker, shadow assessment, RPC simulation, or funded action. |
| 8. Wallet visibility | Implemented for signed-in public address, live SOL balance, network-labelled deposit address/QR, retained portfolio headlines, local policy/reservation/recovery metadata, and editable per-strategy SOL allocations and execution limits. | Wallet-wide policy editing, withdrawal, complete deposit history and browser manual-exit handoff remain pending. |
| 9. Browser signing | Implemented for Wallet Standard ownership proof and exact manual transaction signing, plus a password-encrypted browser-created wallet with backup/restore and in-memory signing. | Real installed-extension transaction acceptance and funded buy/sell lifecycle acceptance remain pending. Automatic submission is disabled. |
| 10. Release and acceptance | Four Playwright flows drive the built client against the real Python API and a temporary library through created-wallet custody, Wallet Standard ownership proof, deposit, cryptographically verified local legacy transaction signing, workspace operations, compact viewport and keyboard use. | Manual assistive-technology review, live Community publication, real extension transaction approval, withdrawals and funded lifecycle acceptance remain pending. |

## Client ownership during migration

| Workflow | Current owner | Promotion requirement |
| --- | --- | --- |
| Market research, strategy import/drafts, disabled sessions, monitoring controls, assistant consent, offline evaluation and virtual replay | Browser app | Maintain browser E2E coverage and Python validation. |
| Wallet connect/create/unlock, live balance, deposit address, and reviewed manual buys/sells | Browser app | Complete real-extension and funded lifecycle acceptance; add withdrawal only after the same recovery controls are exposed. |
| Per-strategy SOL allocation, buy/sell permissions and strategy loss limits | Browser app | Keep Python enforcement at assessment, review and final submission; expose the managed-position manual-exit handoff next. |
| Wallet-wide risk-policy edits, bounded agent signing sessions and withdrawals | Legacy native client | Migrate each workflow with browser-specific review and acceptance. |
| Automatic execution and funded lifecycle claims | Unavailable | Complete the separate execution and funded-acceptance gates. |

The packaged executable opens the browser app by default. `desktop` explicitly
opens the legacy native client. A browser feature is considered migrated only
when its full user flow runs against a temporary real Python library, reloads
durably where applicable, preserves the security boundary, and has keyboard and
relevant compact-viewport coverage.

The browser application is the active product interface: versioned workspace,
market, sessions, strategy records, retained coin evidence, stored evaluation,
shadow/worker/replay evidence and public local wallet-control metadata are served
from the existing local stores. The API binds only to loopback, permits only the
local development browser origin, and requires a per-launch token. It never
returns a vault password, seed, decrypted signer, raw database file or AI API
key. The manual trade endpoint returns one short-lived, fully reviewed unsigned
transaction to the authenticated wallet that requested it.

The first narrowly scoped commands are also available behind the same controls:
import a `pumpwizard.strategy-handoff/v1` package through the existing Python
validator; create a session with entries disabled and manual exits; and change
local monitoring state. They can also run an explicit offline modeled evaluation
for an existing imported strategy. Those research commands cannot create orders,
sign, move funds, start a worker or bypass strategy compatibility checks. Separate authenticated
wallet routes now issue and verify ownership challenges, inspect live balance,
prepare a reviewed transaction, accept only its exact signed bytes, broadcast it
and reconcile its original signature. Real installed-extension transactions,
live-provider acceptance and funded lifecycle acceptance remain pending.

The browser Strategy Assistant now reuses the existing local provider-profile,
chat-history and bounded-context code. Its connection form sends an API key
only to this loopback process and keeps it session-only in the browser app.
Every provider request creates an input/output-token estimate and needs an
explicit one-time approval. If the model requests strategy or coin evidence,
the frozen bounded context becomes a separate approval. No wallet information
is attached to either request.

The browser Activity view reads compact completed-evaluation reports, shadow
assessment counts, worker-rehearsal status/counts and virtual-portfolio
checkpoints. Detailed shadow and worker payloads remain in their local Python
evidence stores. The
view can export those already-saved summaries as a local JSON evidence file;
the export explicitly excludes wallet secrets, signing material, raw
transactions, and provider keys, and does not establish funded acceptance. The
wallet-state route reads public local risk-policy, reservation, recovery and
signing-session metadata only, plus already-recorded public-address balance
snapshots and compact saved portfolio-valuation headlines. Per-asset quote and
route data remain in Python's local portfolio store. Separate authenticated trade
routes can inspect a connected address, prepare one supported manual order, accept
only the exact wallet-signed transaction, broadcast it and reconcile its receipt.
They cannot unlock a vault or start a worker. Separate authenticated strategy
control routes can create or edit local cash allocations and limits after a live
balance check; Python rechecks those controls before reviewed strategy orders.
Browser extension signing remains attended and automatic execution is unavailable.

The browser can explicitly start an **offline modeled evaluation** for an
already imported strategy. It sends either the bundled recording or one local
`pumpwizard.portfolio-recording/v1` JSON file to Python, which checks the exact
pinned configuration and saves the existing immutable report. The browser
receives only its compact report summary; the package and supplied recording
remain in the local report store. This cannot run RPC simulation, shadow
assessment, a worker rehearsal, a signing session, an order, or a funded action.

It can also advance the existing durable **virtual portfolio replay** journal
with a user-selected recording and replay ID. Python enforces the existing
pinned portfolio configuration, bounded input size and run-ID rules before it
writes frames. A replay remains isolated from wallet positions, route
simulation, reservations, signing, workers and funded performance claims.

Selecting an Active Session opens its local monitor state, retained monitor
runs, decisions, current local monitor-lease state, and the same calculated
recovery guidance used by the desktop view. A saved fault changes the browser
control to **Retry monitoring**, matching the desktop recovery action. It
cannot override a block or produce an execution request. After tracking is
enabled, **Scan once** invokes one existing bounded scheduler pass explicitly;
it collects public observations and recommendations only.

**Scan public market** is an explicit browser action, not a background worker.
It runs the existing bounded public Pump market collector once (at most 100
quotes) and persists only normalized market observations. A failed scan retains
the prior local records. It does not touch a wallet, RPC node, monitor, order,
signing session, or execution service.

The browser strategy chooser first shows the existing bundled/cached
marketplace catalog and its attributed simulation snapshots. It does not fetch
the website merely to open. Importing a marketplace item passes the unchanged
`pumpwizard.strategy-handoff/v1` package through Python validation; no shared
website contract was changed.

The local strategy library exposes the same Python-derived package identity,
rules identity, capability label, route/evidence description and existing
compatibility reason used by Runner. An explicit export downloads the exact
unchanged handoff package. The retired `web/` static mock is not a browser
entrypoint; `web-client/` is the only active browser client.

The browser may also create a local research draft from the existing Runner
template. Python validates and imports the resulting
`pumpwizard.strategy-handoff/v1` package before it appears in the library. A
draft has no execution eligibility, wallet reservation, signing permission, or
funded-acceptance status merely because it was created.

The client source includes a restrictive CSP and the API tests cover
loopback binding, origin rejection, token rejection for both reads and commands,
CORS preflight, and the same-origin HttpOnly-cookie static preview. The source
also has responsive rail/header/filter styles, focus-visible controls, polite
state announcements, and Playwright checks at desktop and compact viewports.
Manual visual-device and assistive-technology acceptance remain pending. Dev56
has its own draft Community note; publishing and live verification remain
separate from source and package validation.

### Local development preview

Use one fresh random value for both processes; it is a development-session
credential and must not be committed:

```bash
export PUMPWIZARD_BROWSER_TOKEN="replace-with-a-new-random-value-of-24-or-more-characters"
PYTHONPATH=src pumpwizard web-api --browser-token "$PUMPWIZARD_BROWSER_TOKEN"
```

In another terminal:

```bash
cd web-client
PUMPWIZARD_BROWSER_TOKEN="$PUMPWIZARD_BROWSER_TOKEN" npm run dev -- --host 127.0.0.1
```

The Vite proxy supplies the token to the loopback API. The token is not put in
the browser bundle or written to the local strategy library.

### Built same-origin local app

After `cd web-client && npm run build`, Runner can generate the launch token,
serve the static client and API together, and open the system browser:

```bash
PYTHONPATH=src pumpwizard browser
```

Open <http://127.0.0.1:8765>. The served `index.html` establishes a
session-only `HttpOnly`, `SameSite=Strict` cookie scoped to `/api`; the browser
uses it for same-origin API requests while JavaScript cannot read the token.
The server remains loopback-only. Wallet connection, ownership proof, balance,
deposit and reviewed manual signing use the same origin and authenticated API.
Automatic execution remains disabled.

## Phase-one API contract

Start with these read-only endpoints:

- `GET /api/v1/workspace`
- `GET /api/v1/market?limit=…&query=…&sort=…`
- `GET /api/v1/sessions`
- `GET /api/v1/sessions/{id}`
- `GET /api/v1/coins/{mint}`
- `GET /api/v1/strategies`

Every response includes the Runner build version and a schema version. The
browser treats responses as display data; only later phase-specific commands
may mutate state, and all mutations remain validated in Python.
