# PumpWizard background service contract

Prepared September 28, 2026 for the first Linux service-foundation slice. This
contract is intentionally narrower than the six-month plan. It defines service
identity, read-only jobs, evidence coverage, and lifecycle integration. It does
not enable production trading or define a generic signing API.

## Components

The service engine owns recording, backend routes, migrations and lifecycle management. A bounded signer implements the custody interface; dashboard components consume the authenticated service API. Browser main-wallet Autopilot is a separate custody mode.

## Process and trust boundaries

One service process owns one resolved application-data directory. A Linux file
lock is the primary process exclusion; the durable service instance record and
monitor database lease provide recovery/audit and worker exclusion. A PID by
itself never establishes ownership. Port 8781 is the isolated default. The API
binds only to `127.0.0.1`, `::1`, or `localhost`.

The same-origin dashboard launch cookie or development request token grants
view/control access to this local API only. It does not prove wallet ownership,
unlock a service vault, authorize a strategy, resume expired trading authority,
or sign anything. Wallet ownership, encrypted-vault enrollment, transient
unlock and bounded trading authorization remain separate signer component contracts.

Service restart restores only explicitly enabled read-only jobs. Restart,
crash, shutdown and detected sleep/resume lock service signing. No password or
unlocked signer state is persisted. Submitted signatures remain recoverable;
stopping cannot cancel a transaction already sent to the chain.

Research Batch 3 adds a separate service-owned research/paper worker thread.
It processes at most one registered comparison at a time and bounded paper
pages without blocking the collector or receipt reconciliation loop. Browser
closure does not cancel that work. Restart resumes the durable job/attempt and
paper cursor without restoring signer authority. See the
[research contract](RESEARCH-DASHBOARD.md) and
[integrated review](RESEARCH-DASHBOARD.md).

## CLI lifecycle

The public command group is:

```text
pumpwizard --data-dir <absolute-data-dir> service start [--port 8781] [--web-root <build>]
pumpwizard --data-dir <absolute-data-dir> service status [--port 8781]
pumpwizard --data-dir <absolute-data-dir> service stop [--port 8781]
pumpwizard --data-dir <absolute-data-dir> service open-dashboard [--port 8781]
pumpwizard --data-dir <absolute-data-dir> service run --port 8781 [--expected-version <exact>]
pumpwizard --data-dir <absolute-data-dir> service install-user --executable <absolute> [--unit-dir <dir>]
pumpwizard --data-dir <absolute-data-dir> service remove-user [--unit-dir <dir>]
```

`run` is the foreground/systemd entry point. `start` reuses a service only after
an authenticated identity probe matches the selected data-directory digest; an
occupied port with no matching identity fails closed. `stop` uses the
authenticated shutdown route and never kills a PID inferred from a stale file.
`open-dashboard` reuses the verified service and opens its same-origin root; it
does not start a second scheduler. Installation writes a version-pinned user
unit with absolute executable/data paths but does not enable or start it. It
does not enable linger, boot policy, or logout survival.

## Read endpoints

All payloads use `schema: "pumpwizard.runner-web/v1"` as the outer API envelope.

### `GET /api/v1/service/status`

```json
{
  "service": {
    "schema": "pumpwizard.background-service-status/v1",
    "instance_id": "opaque restart identity",
    "instance_revision": 4,
    "data_directory_id": "sha256 digest, never a path",
    "version": "0.2.0.dev62",
    "state": "running",
    "started_at": 0,
    "heartbeat_at": 0,
    "current_activity": "idle",
    "last_success_at": null,
    "health": "healthy",
    "stop_reason": "",
    "production_trading": "blocked"
  },
  "jobs": [],
  "trading": {}
}
```

`instance_id` changes on every process start. `instance_revision` is monotonic
for the data directory and lets clients discard stale responses. A missing or
stale heartbeat reports `unknown`, `stale`, or `degraded`, never healthy.
Status and errors contain no data path, access token, address, ciphertext,
password, private key, transaction bytes or RPC credential.

### `GET /api/v1/service/jobs`

Returns `jobs`, each with:

```json
{
  "schema": "pumpwizard.service-job/v1",
  "id": "strategy-monitor",
  "kind": "read-only-monitor",
  "enabled": true,
  "interval_seconds": 15,
  "revision": 3,
  "next_at": 0,
  "consecutive_failures": 0,
  "current_activity": "idle",
  "last_started_at": null,
  "last_success_at": null,
  "last_error_at": null,
  "last_error": ""
}
```

The initial bounded job is `strategy-monitor`. Enabling it permits only public
market collection and local strategy evaluation for sessions whose existing
tracking setting is also opted in. It has no signer or order-submission access.

### `GET /api/v1/service/coverage`

Returns `coverage` rows using
`pumpwizard.collector-coverage/v1`. Each row identifies the bounded scan key,
source, cap band, routes/pools observed, request/start/receipt times, earliest
and latest source-event times, quote count, gap count/reasons, status,
consecutive failures and next retry. A backfilled observation retains its
original event/source time and records a later first-received time.

### `GET /api/v1/service/trading`

Returns `trading` using `pumpwizard.service-trading-state/v1`: custody mode,
locked/unlocked state, authorization identity/expiry where safe, entry pause,
recovery requirement and structured blockers. In the initial service engine fixture
it is locked with production activation blocked.

## Command endpoints

### `POST /api/v1/service/jobs/strategy-monitor`

Body:

```json
{"enabled": true, "interval_seconds": 15}
```

Only those two exact fields are accepted. Interval is bounded to 5–3600
seconds. Disabling the job stops future scans but preserves evidence and does
not change session execution permissions. The previous explicit setting and
revision remain auditable.

### `POST /api/v1/service/pause-entries`

Requires the authenticated dashboard API context. It immediately invalidates
pending buy authorization through the injected signer and leaves eligible exit
recovery available. It cannot create or renew trading authority.

### `POST /api/v1/service/revoke-trading`

Requires the authenticated dashboard API context. It locks signing and revokes
the current bounded authorization. Submitted signatures remain in recovery.

### `POST /api/v1/service/resume-entries`

Delegates to signer component and succeeds only when that component verifies a still
valid, freshly unlocked, exact authorization. Dashboard authentication alone is
insufficient. The service engine blocked fixture always rejects this operation.

### `POST /api/v1/service/shutdown`

Invalidates signing first, requests read-only workers to stop, records whether
they actually stopped, flushes service state and then shuts down the loopback
server. The response says that already submitted transactions were not
cancelled.

There is deliberately no arbitrary message/transaction signing endpoint, no
caller-selected live capability flag, no raw key endpoint, and no generic job
executable endpoint.

### Service-custody endpoints

`GET /api/v1/service/signing?sender=<address>&network=<network>` and every
service-custody command require the existing wallet ownership proof in addition
to Dashboard API authentication. The typed commands are:

- `POST /api/v1/service/signing/enroll` with
  `{sender, network, envelope, service_unlock_secret}`;
- `POST /api/v1/service/signing/review` with
  `{sender, network, session_id, plan}`;
- `POST /api/v1/service/signing/authorize` with
  `{sender, network, session_id, review_id, confirmation}`; and
- `POST /api/v1/service/signing/acceptance/request` with
  `{sender, network, allowance_id, routes, duration_seconds}`;
- `POST /api/v1/service/signing/acceptance/install` with
  `{sender, network, payload, signature}`; and
- `POST /api/v1/service/signing/start` with
  `{sender, network, allowance_id, service_unlock_secret, confirmation}`.

`GET /api/v1/service/signing/acceptance/evidence` additionally requires
`sender`, `network` and `approval_id` query fields plus the same wallet ownership
proof. The response is read-only and returns redacted approval, allowance, exact
order, route and receipt correlation.

The transient unlock secret is consumed in memory and never appears in the
response, status, log or durable request state. Production `start` remains
blocked by the default service runtime until a matching packaged, wallet-signed,
single-use acceptance capability is installed. Only an explicitly injected
isolated test controller can activate the synthetic runtime.

## Engine and signer injection

service engine integrates an object with this fail-closed interface:

```text
state() -> typed non-secret trading state
pause_entries(reason) -> updated state
resume_entries(authorization) -> updated state or rejection
revoke(reason) -> updated state
lock(reason) -> updated state
reconcile() -> recovery summary
run_cycle() -> bounded synthetic/service execution result
```

The default implementation exposes a locked, production-blocked state and
cannot sign. The engine calls `lock` during startup before work restoration,
on shutdown before joining workers, and after detected sleep/resume. It calls
`reconcile` before any injected execution cycle. signer component remains responsible
for atomic allowance usage, exact-transaction validation, signer custody and
original-signature recovery.

## Recorder and retention

Every provider pass has a durable scan ID. Raw normalized quote payloads record
event (`observed_at`), provider (`source_at`) and local first-receipt
(`received_at`) times, plus source, route, pool and a stable payload fingerprint.
Repeated inserts are idempotent. Exact cap-band requests are shared by the
existing scheduler instead of starting competing collectors per strategy.

The first slice retains at most 50,000 raw rows and 5,000 scan runs, and removes
rows older than 30 days only after a successful checkpoint. Failures produce a
visible gap record and exponential retry delay capped at five minutes. Coverage
never infers a continuous interval across a provider error, restart or offline
period.

## Linux service semantics and deployment limit

The staged user unit uses `Restart=on-failure`, the exact Runner version, an
absolute executable and an absolute data directory. Installing it does not call
`systemctl`, enable the unit, start it, or enable linger. Starting after login
requires the user to explicitly enable/start the user unit. Surviving logout
requires a separately explained and authorized linger policy; PumpWizard does
not change that policy.

This source-phase contract and synthetic fixtures are not a production rollout.
Do not install over the user's current service, migrate a real wallet, reuse a
funded acceptance record, or present production service trading as available.

## Implemented checkpoint

The first source slice now implements this contract in `background_service.py`,
`service_recorder.py`, `service_lifecycle.py`, `web_api.py`, `scheduler.py` and
the Linux CLI. The default engine injects signer component's production-blocked service
signer only after acquiring the data-directory lock. Explicit v4 service-custody
brief creation is supported; existing v1–v3 briefs retain their stored content.

An isolated real process passed start, authenticated status, durable job
configuration, browser-independent heartbeat/job-success advancement, clean
stop and verified SIGKILL recovery. A 1,800.2-second soak crossed 18 injected
provider gaps, recovered, restarted and retained 100/100 unique evidence
fingerprints. Offline CLI backup/restore preserved four SQLite databases in a
new locked directory while omitting launch authority. The version-pinned user
unit was staged only under `/tmp` and was not enabled or started.

The integrated checkpoint passed 842 collected Python tests (841 passed, one
intentional skip), Ruff/diff checks, frontend lint/typecheck, an isolated
production Vite build and all 41 production-bundle Playwright flows. Production
service trading, funded acceptance, OS logout/reboot survival, automatic reboot
unlock and deployment remain blocked.
