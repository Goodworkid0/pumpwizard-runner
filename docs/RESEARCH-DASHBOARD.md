# Research dashboard

Status: integrated Batch 4 workspace, September 28, 2026. This dashboard is a
local research surface. It does not allocate wallet funds, sign transactions,
submit orders or activate a strategy.

## Open and inspect evidence

Open a session monitor and choose **Review session evidence**. Opening the
workspace is read-only: it performs GET requests only and does not freeze a
dataset, start a comparison or begin paper monitoring.

The Evidence page distinguishes:

- event/source time from the time a record was first known to Runner;
- independently received observations from rows with unknown receipt time;
- complete periods from collection outages and other explicit gaps;
- compatible strategy families from evidence that can only be inspected; and
- fresh observations from stale, late, backfilled, duplicated, unpriced,
  ambiguous-pool or route-mismatched records.

Choose **Freeze current evidence** to create an immutable, content-addressed
dataset. Oversized input is rejected rather than silently truncated. A frozen
dataset does not imply that the evidence is eligible for comparison.

## Run and reopen a comparison

Before selecting **Start background comparison**, review the simulated capital,
purpose, fee, slippage, fixed-cost and embargo fields. The service registers the
exact experiment before queueing it. Jobs remain service-owned if the browser
closes. Returning to the same source session reopens the existing job or report;
polling never starts another experiment.

Queued, running, cancelling, interrupted, failed, cancelled and completed states
are displayed from the service record. Cancellation is cooperative. Retry adds
an attempt to the same immutable experiment history instead of erasing the
failed or cancelled attempt.

The History page shows dataset and package lineage, capital/cost/split policy,
code and replay versions, every attempt, and holdout state. Once a holdout is
exposed, an overlapping related experiment cannot claim it as unseen evidence.
Exploratory comparisons stop at validation and show `not_evaluated`; they do not
return holdout portfolios. Pre-Batch-4 reports remain stored unchanged, but a
conservative read-time integrity warning identifies inferred exposure and
prevents an affected old engine result from silently creating a new draft.

## Read results without turning them into promises

Results compare only the rows returned by the backend: cash, current rules,
bounded candidates and a holding benchmark when supported. Starting capital,
period and cost units remain visible. Net result, realized and unrealized
modeled values, drawdown, coverage, independent observations, uncertainty and
cost stress are shown only when the report provides them.

The equity chart stops at null or gap points. It never draws across an unpriced
interval. The accessible comparison table and expandable ledger retain unknown
exits, route changes, unsupported fills and exclusions with their explanations.
These retrospective models remain separate from prospective paper evidence and
actual wallet receipts.

The recommendation can be insufficient evidence, prefer cash, keep current or
an eligible inactive draft. Draft confirmation names the exact report and
candidate. A resulting successor is separate, paused and unallocated, with
entries disabled and no copied execution authorization. Repeated requests are
deduplicated by backend identity.

JSON and CSV links download backend-generated immutable artifacts. The backend
chooses the filename so the report identity and extension match the bytes.

## Forward paper research

Forward paper research is available only in monitoring sessions. Eligibility is
derived from the saved strategy brief and its owning session, including older
briefs. Trading-session evidence views retain Evidence, Results and History;
Forward paper and Campaigns are omitted. Paper campaign setup accepts only
monitoring sources. The API rejects typed/legacy starts and resumes tied to
trading sessions, including campaign arms, before new collection is started.
Older paper records remain readable and can be paused or stopped for cleanup.
Real wallet allocations and stored briefs are not rewritten.

The Forward paper page requires the explicit **START PAPER RESEARCH** action.
It freezes the strategy digest, simulated USD capital, source session and
observation start. It never reserves a wallet balance or gains a signer,
allowance, order or submission path.

Pause, resume and stop operate on the same durable run. A strategy or capital
change starts a new run instead of rewriting history. The page shows recorded
decisions, later outcomes, assumptions, independent observations and collection
gaps. Restarted collection resumes its durable cursor and retains outage gaps.

The start operation distinguishes delivery retry from a deliberate new run.
The browser keeps the same operation key when a response is lost, while the
backend assigns the start timestamp once and returns that original run on the
retry. After a confirmed start and later stop, **Review paper start** allocates
a fresh operation key. Keys are scoped to the source session as well as strategy
and capital. If browser session storage is blocked, a bounded in-memory fallback
keeps retry available and the workspace tells the operator to keep that page
open.

Pausing or restarting closes a decision-service operating interval. Evidence
that arrived while decisions were paused or the service was offline may still
be retained for provenance, but it is marked outside the operating interval and
cannot be turned into an old simulated trade after resume. The next fresh quote
begins from the resume cutoff. The History view shows operating intervals,
gaps, quality exclusions and persisted faults with a specific repair action.

Price precision is separate from cash-ledger precision. Supported token prices
use exact 1e-18 quote units, including `$0.000000001`; the paper adapter never
raises one to a one-micro price. Evidence below the supported precision, with
unverified or stale source time, or with invalid normalized fields remains in
the evidence ledger with an exclusion reason and advances the input cursor. It
does not repeatedly fault the run. Unexpected evaluator faults interrupt only
the affected run, preserve bounded retry state and do not starve a healthy run.

The bounded synthetic Batch 4 rehearsal processed 1,200 quotes in two pages:
1,000 in 16.429 seconds and 200 in 6.230 seconds. A reopened no-op cycle took
0.106 seconds. The run used a 1,000-row page cap, a 10,000-row decision-state
limit, 47,452 KiB peak RSS, a 1,377,733-byte evaluator checkpoint and a
204,874-byte evaluator report. This is a finite synthetic resource measurement,
not proof of indefinite throughput or profitability. Wallet allocation, signer
and order authority were all false.

## Holding evidence

Portfolio rows now expose a compact **Evidence** panel beside existing history.
It uses only retained wallet/market fields: purchase ownership, quote age,
liquidity and source, valuation state, declared coverage gaps and a link back to
the source session when a Runner strategy owns the position. A valuation is
explicitly not presented as an executable quote or sell-price promise.

## Compatibility and recovery

When a server does not expose the research workspace route, saved v1 Session
Review reports remain available through the earlier comparison view. API loss
keeps the workspace open with an actionable retry. Narrow layouts retain native
button, tab, table and form keyboard behavior; wide tables scroll without
changing their values.

The deterministic browser fixture and synthetic replay records demonstrate the
mechanics only. They are not evidence of profitability, production readiness or
funded deployment.
