# Forward campaign operator guide

Forward campaigns collect bounded prospective paper evidence. They do not
reserve wallet funds and do not grant wallet, signer, order or submission
authority. The local background service, rather than an open browser page, owns
collection and evaluation.

## Before starting

1. Keep the Runner's one local background service healthy.
2. Save at least one `monitoring-ready` strategy session. Campaign start binds
   the session ID and retained strategy SHA-256; a changed identity is rejected.
3. Confirm that the configured source is `CompositePumpMarketSource` and review
   its exact universe, cadence and result limit.
4. Declare simulated capital, cost and execution assumptions, evidence targets,
   planned duration, request/runtime/storage ceilings and concurrency.
5. Treat an unknown provider price as unknown. It is never displayed as free and
   prevents a fully-costed profitability claim.

Open the session monitor, choose **Review session evidence**, then **Campaigns**.
The review screen shows the exact immutable request before **Start forward
campaign**. If the response is lost, use **Retry exact Start** without editing;
the same campaign key and idempotency identity are retained. An edit intentionally
creates a new start identity.

## Operating a campaign

- **Running/degraded:** the service may collect only within the declared source
  and remaining ceilings. Pause closes the current operating interval.
- **Paused:** reads, refresh, polling and another browser tab remain read-only.
  Resume reopens the same campaign and never widens a ceiling.
- **Service restart:** a previously running campaign recovers paused. Inspect the
  service and the durable restart gap, then explicitly resume the same campaign.
- **Capped:** the reached request, runtime or storage ceiling is immutable. Use
  **Configure new bounded version** to review a successor; Resume cannot widen
  the old trial.
- **Stop:** review the action and type the complete campaign ID. A stopped trial
  remains available for reports and export but cannot resume.

The Operation view distinguishes campaign state, arm state and service health.
It shows actual operating time, provider request/storage use, source age,
errors/retries, arm evidence and durable gaps. Unknown source age, latency or
cost stays labeled unknown. Missed time is not replayed as prospective evidence.

## Reading performance and receipts

Saving a report is an explicit append-only snapshot. Merely opening Performance,
refreshing or exporting does not evaluate evidence. Compare cash, current,
holding and campaign arms only where the backend marks their source coverage and
uptime comparable. Curves break at declared gaps and unpriced points.

Keep these limits visible when interpreting a report:

- readiness reasons can withhold ranking even when P/L is shown;
- uneven uptime, incomplete coverage, censored positions or missing marks remain
  visible rather than being filled with zero;
- unknown fees or provider prices keep the result from being fully costed; and
- actual operating duration, not the requested wall-clock duration, describes
  what was observed.

The Receipts view keeps modeled paper, retrospective and recorded execution
records in separate groups. Deposits, withdrawals and transferred inventory are
external flows, not trading P/L. Ownership, fees and unresolved conservation
differences come from retained backend evidence. Portfolio worth movement is not
used as a substitute.

JSON and CSV exports read a saved report ID. They retain its campaign and manifest
identity, prior-report lineage, model/engine versions and source evidence links.

## Local evidence inbox

The inbox is local-only. It has no email, Slack, social, push or buy/sell delivery.
It covers provider outages/stale evidence, exhausted quotas, stalled or faulted
arms, accounting mismatches and preregistered evidence milestones.

An alert keeps deterministic campaign/condition/generation identity, severity,
local source link, first/last occurrence and occurrence count. **Acknowledge
review** records operator review only; it does not resolve the condition. The
evaluator resolves the generation after the evidence clears. If the same
condition returns later, a new generation opens with no inherited acknowledgment.
Repeated reads and identical observations do not increment occurrences.

Use **Inspect provider/campaign/accounting** to open the relevant local campaign
or receipt view and follow its repair text. If the bounded inbox reaches capacity,
resolved history is removed before new alerts; active conditions are never
silently discarded.

## Longer observation and current limits

The Batch 6 keyless check on September 29, 2026 returned three supported curve
quotes in three HTTP reads, while the bounded campaign retained 15 observations
per arm over 7.2299 seconds. All 12 decisions per arm were `entry_blocked`; no
actual opportunity or outcome was observed. Provider billing remained unknown.
These artifacts establish corrected access, normalization and evaluator wiring
only—not uptime, capacity, opportunity frequency, strategy edge or profitability.
They are retained at `/tmp/pumpwizard-batch6-worker1-public-smoke-final.json` and
`/tmp/pumpwizard-batch6-worker1-live-campaign-v4.json` for the local handoff.

For a longer local observation, keep the machine and background service running,
verify keyless/provider availability and price status, select immutable supported
arms, preregister a realistic duration and evidence target, and set reviewed hard
ceilings before starting. Periodically inspect service health, source freshness,
gaps and storage use. Restart recovery always requires explicit resume.

Automated acceptance uses a short deterministic fixture. It proves persistence,
controls, evaluator integration, reporting and alert semantics; it is not a
completed multi-day study and does not establish live-trading readiness. No paid
provider, external messaging, production deployment or funded execution is part
of this workflow.
