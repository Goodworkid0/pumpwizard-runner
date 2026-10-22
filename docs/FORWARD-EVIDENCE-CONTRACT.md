# Forward evidence contract

Status: Batch 5 shared contract, September 28, 2026. These contracts describe
observation and modeled reporting only. They add no wallet allocation, signer,
order, submission or provider-purchase authority.

## Campaign identity

`pumpwizard.forward-campaign-request/v1` is registered before any arm starts.
Its immutable manifest contains:

- a caller-generated `campaign_key` that distinguishes a deliberate new trial
  from retrying delivery, and an optional `supersedes_campaign_id`;
- one source universe: provider identity, market-cap band, bounded priority
  mints, result limit and cadence;
- one to three arms, each binding a source session, exact strategy package
  SHA-256 and declared simulated USD capital;
- modeled cost/execution assumptions, evidence coverage requirements, planned
  observation duration and hard request/runtime/storage/concurrency ceilings;
- provider cost knowledge split into known marginal request cost, known fixed
  subscription cost and unknown pricing. Unknown is never encoded as zero.

The canonical manifest digest plus `campaign_key` defines campaign identity.
An idempotency key may replay only the exact request. Changing any manifest
field creates a new campaign/version; stored campaigns and arm links are never
rewritten. Campaign creation and all linked `pumpwizard.paper-run/v1` rows share
one SQLite transaction, so a failed arm cannot leave a hidden partial campaign.

The exact confirmation for Start is `START FORWARD CAMPAIGN`. Stop confirms the
campaign ID. GET, navigation, polling, export and report generation are
read-only and never start, resume or widen a campaign.

## Time, evidence and shared collection

`start_at`, operating intervals, provider request/receipt times, evidence
`source_at`, `observed_at`, recorder `first_received_at`, and paper
`decided_at` remain distinct. Decisions use actual first receipt while an arm is
operating. Backlog before Start or Resume may be retained as excluded provenance
but can never be replayed as a live decision. Pause, service interruption,
provider outage, quota exhaustion and input caps are durable gaps.

Campaigns add demand to the single background-service scheduler. Matching cap
bands share one provider request with ordinary monitors and other campaigns.
The recorder stores that request once and links bounded consumer allocations;
each arm in a campaign reads only evidence from requests attributed to its
campaign. All arms therefore see the same ordered eligible evidence/window.
Cash and holding benchmarks use that same source lineage and operating window.

## Provider telemetry and budgets

Provider telemetry is `pumpwizard.provider-request/v1`. It retains only a
bounded source class and safe error text—never credentials, query strings,
wallet data or a full sensitive URL. Records include request/start/finish and
duration, status/error class, retry/backoff, result count, response bytes,
source age, first receipt, duplicate/excluded counts and campaign allocation.

Campaign ceilings are hard. The scheduler allows no more than four shared
provider requests globally and no more than the campaign's declared concurrency.
Cadence and exponential backoff are honored. Request, planned duration, active
runtime or attributable storage exhaustion atomically pauses the campaign and
its arms in durable `capped` state with a structured
`pumpwizard.forward-campaign-blocker/v1`. Unknown provider price produces
`cost_status: unknown`, never a fabricated fully-costed value.

Provider history is cursor-paginated and retained under the existing recorder
caps. Compaction cannot make paper/campaign rows retroactively decision-eligible.

## Campaign state and reports

`pumpwizard.forward-campaign/v1` states are `running`, `paused`, `degraded`,
`capped`, `completed`, and `stopped`. State transitions append immutable
`pumpwizard.forward-campaign-event/v1` rows. A view includes arm paper-run IDs,
actual progress/cursors, operating intervals, gaps, usage/cost status, source
freshness, provider history, structured blockers and the authority object:

```json
{"wallet_allocation":false,"signer":false,"orders":false,"submission":false}
```

Performance inputs and snapshots use
`pumpwizard.forward-performance-input/v1` and
`pumpwizard.forward-performance-report/v1`. They bind campaign/manifest digest,
arm run/report identities, matched evidence IDs, actual intervals/gaps, exact
integer cash/token units, 1e-18 prices, provider-cost status, as-of valuation,
engine versions and receipt lineage. Snapshots are append-only and content
addressed; corrections create a linked new version. Daily and whole-run views
must expose mismatched uptime, censored/unpriced positions, external cash flows,
unknown costs and readiness blockers before ranking.

Recorded receipts are read-only external evidence. Deduplication uses network,
signature and instruction/receipt identity. Pending, finalized success,
fee-only failure and unresolved states remain distinct. Ownership or a match to
paper expectations is asserted only when retained evidence supports it. Paper,
retrospective and recorded-execution evidence are never relabeled as each other.

## Local alerts and failures

`pumpwizard.evidence-alert/v1` IDs are deterministic over campaign, condition
and source identity. Conditions are provider outage/staleness, hard quota,
stalled/faulted arm, accounting mismatch and preregistered evidence milestone.
Records preserve first/last occurrence, count, severity, source link,
acknowledgment and resolution. Acknowledgment does not resolve the condition;
resolution followed by recurrence opens a new occurrence generation. The inbox
is local and bounded. It cannot send email, push, Slack/social messages, or
buy/sell signals.

Command failures use a bounded structured object with `code`, `message`,
`retryable`, `repair` and source identity. Durable provider/paper faults affect
only their request/run; healthy arms and comparison jobs continue. Backups copy
the SQLite stores and restore them into a new directory with trading/signing
authority locked and launch tokens excluded.
