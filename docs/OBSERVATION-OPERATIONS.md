# Observation operations

The Batch 7 observation workflow is paper-only and bounded. Preparing a preset
does not read the network; starting it requires the existing exact confirmation
`START FORWARD CAMPAIGN`. Closing the browser does not stop a service-owned
campaign, but a background-service restart pauses it and requires explicit
Resume. Nothing in this workflow grants wallet, signer, order, submission or
provider-purchase authority.

## Frozen 24-hour preset

`observation-prepare-24h` stores an immutable
`pumpwizard.forward-campaign-preset/v1` revision. The reviewed request freezes:

- two exact strategy packages (`observed` and `strict-comparator`) and their
  SHA-256 identities;
- `CompositePumpMarketSource`, the USD 1,000–150,000 market-cap band, no priority
  mints, a three-record limit and a 900-second cadence;
- a 120-second maximum source age, two-token interval minimum, 96-observation
  minimum and 1,800-second maximum gap;
- a 24-hour planned duration, 96 provider attempts, one concurrent scan,
  86,400 active seconds and 64 MiB attributable-storage ceilings;
- three expected/maximum HTTP reads per provider attempt, estimated and maximum
  provider bytes, normalized-record sizing, all stop conditions, and unknown
  provider billing.

The dry run reports 96 scheduled intervals, at most 288 HTTP reads and zero
network reads performed by preparation. Unknown provider prices remain `null`;
they block a fully-costed profitability statement. Reusing the same idempotency
key with changed input is rejected. Changed input under a new key creates the
next preset revision and never mutates an earlier revision.

Prepare and inspect without starting collection:

```bash
python -m pumpwizard_runner.cli --data-dir DATA observation-prepare-24h \
  --idempotency-key OPERATOR_UNIQUE_PREPARE_KEY
python -m pumpwizard_runner.cli --data-dir DATA observation-presets
```

Save the returned preset ID, request SHA-256, identity SHA-256, revision and dry
run. A preset is preparation only; it is not evidence that a live run occurred.

## Start, observe and stop

Before an actual long run, confirm that the persistent background service owns
the intended data directory, disk headroom is greater than the frozen ceiling,
the public provider is usable without a purchase, and the operator accepts that
billing is unpriced. Additional duration never authorizes higher cadence or more
requests.

Start the exact preset:

```bash
python -m pumpwizard_runner.cli --data-dir DATA observation-start PRESET_ID \
  --idempotency-key OPERATOR_UNIQUE_START_KEY \
  --confirm 'START FORWARD CAMPAIGN'
```

Repeated delivery of that start key returns the same campaign. It does not create
a second campaign. During collection, use these read-only views:

```bash
python -m pumpwizard_runner.cli --data-dir DATA observation-summary CAMPAIGN_ID
python -m pumpwizard_runner.cli --data-dir DATA campaign-provider CAMPAIGN_ID
```

Always stop intentionally at the end or when abandoning a run:

```bash
python -m pumpwizard_runner.cli --data-dir DATA campaign-command \
  CAMPAIGN_ID stop --idempotency-key OPERATOR_UNIQUE_STOP_KEY \
  --confirm CAMPAIGN_ID
python -m pumpwizard_runner.cli --data-dir DATA observation-export CAMPAIGN_ID
```

Hash and retain the final export beside the immutable manifest. Verify that the
final campaign and every arm are stopped. A capped campaign can still receive the
explicit Stop command, but it cannot resume or widen its ceilings.

## Reading the operational summary

The `pumpwizard.observation-operational-summary/v1` contract keeps unlike
denominators separate:

- `time.wall_seconds` is elapsed campaign wall time and freezes at a terminal
  state; `active_seconds` contains only open collection intervals;
  `inactive_wall_seconds` is their difference.
- each arm's `evaluator.exposure_seconds` comes from its own durable operating
  intervals. Processing lag comes from persisted evaluator cycle boundaries and
  the aligned receipt watermark. If no cycle measurement exists, it is
  unavailable—not zero.
- interval classification is exclusive, in this order: quota exhausted, outage,
  stale, empty, sparse, usable. Missing scheduled intervals are separate.
- source points define a usable receipt span, not continuous market coverage.
  `continuous_coverage_seconds` therefore remains unknown.
- arm alignment uses distinct eligible evidence-ID intersection/union. Shared
  scans are not multiplied by the number of arms.
- logical provider attempts, shared allocated request equivalents and physical
  HTTP reads are different counts. HTTP reads or provider bytes are `null` when
  any contributing request lacks that measurement; a measured lower bound and
  unknown-request count remain visible.
- attributable storage is normalized evidence plus paper records. Physical
  SQLite growth includes shared databases and is labeled as shared rather than
  assigned solely to one campaign.
- per-arm portfolio outcomes and fills are factual evaluator output. They remain
  paper results under the frozen cost/execution assumptions.

The digest-bound `pumpwizard.observation-operational-export/v1` is the transfer
object for API/UI and evidence review. The evidence service consumes the facts without
reinterpreting unknown cost; the dashboard should render the provided states rather
than recalculate coverage or allocations.

## Failure recovery

- **Provider failure:** the recorder retains the failed request, error class,
  diagnostics and bounded retry time. The campaign is degraded. Do not force an
  immediate retry around the durable backoff; confirm recovery in later healthy
  intervals.
- **Empty or sparse feed:** preserve the interval as empty/sparse. Do not turn it
  into a zero-valued quote or a continuous-coverage claim.
- **Stale feed:** source and receipt times remain distinct. Rows beyond the
  frozen source-age gate are retained as excluded evidence and never made
  eligible by changing the strategy during a run.
- **Service restart:** startup calls `recover_running`, closes the prior active
  interval, records a service interruption and pauses campaign arms. Use the
  existing `campaign-command CAMPAIGN_ID resume` only after inspection. Missed
  time is a gap and is never backfilled as a live decision.
- **Quota cap:** request, runtime or attributable-storage exhaustion creates a
  non-retryable blocker and pauses all arms. Create and review a new immutable
  version/preset if different ceilings are justified; never rewrite the run.
- **Evaluator fault:** keep the evidence and fault record. Repair the named issue
  and use the bounded existing retry/resume path. Do not discard the failed
  interval or fabricate evaluator exposure.

The service integration must persist a `record_evaluation_cycle` after each real
paper-worker pass. Use `evaluation_snapshot` immediately before and after the
pass for exact deltas and the aligned receipt watermark. The service scheduler
must receive the engine clock so deterministic lifecycle tests and production
time use the same boundary.

## Verification harnesses

The deterministic harness advances a fake clock across the exact 96-slot plan
and uses the real scheduler, recorder and paper evaluator. It exercises browser
absence, duplicate Start, pause, restart-required Resume, outage/recovery,
empty/sparse/stale input, quota cap and Stop. It is mechanics evidence only:

```bash
PYTHONPATH=src python packaging/observation_24h_rehearsal.py \
  --data-dir /tmp/pumpwizard-batch7-observation-rehearsal \
  --output output/batch7-observation-24h-rehearsal.json
sha256sum output/batch7-observation-24h-rehearsal.json
```

The keyless live smoke remains deliberately smaller: no more than five provider
attempts, fifteen HTTP reads and sixty wall seconds. Its `finally` path stops the
campaign and closes local resources. It cannot satisfy the 24-hour acceptance:

```bash
PYTHONPATH=src python packaging/forward_live_campaign.py \
  --output output/batch7-observation-live-smoke.json
```

Do not leave either harness, the app, or a newly started 24-hour campaign running
unattended by default. A true 24-hour result remains outstanding until the
wall-clock receipts, final Stop and export are actually observed.
