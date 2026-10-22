# Finite-capital portfolio replay and prospective paper evaluation

The Batch 4 replay is an offline research model. It never imports a wallet,
signer, RPC client, order journal or service controller and cannot submit a
transaction. Its output is modeled evidence, not funded performance.

## Versioned contracts

- `pumpwizard.portfolio-replay-policy/v2` declares integer starting cash/order
  size, quote currency, capacity, delays, quote ages and supported `ladder-v1`
  target/stop/hold behavior.
- `pumpwizard.portfolio-replay-costs/v2` declares fees, spread, slippage,
  size/liquidity impact, fixed costs, entry-only rent and failed-attempt fees.
- `pumpwizard.portfolio-replay-execution/v3` contains scenario-only explicit
  entry failures or a stable SHA-256 seed/ppm threshold, unavailable exits and
  partial-fill fractions. Future paper signal IDs use the same declared seed;
  these assumptions are never read from a production request switch.
- `pumpwizard.portfolio-replay-report/v3` contains reproducible order, fill,
  cash, position and equity ledgers plus costs, exclusions, coverage and
  conservative valuation under corrected source-time freshness, inclusive
  intent expiry and high-precision prices.
- `pumpwizard.portfolio-comparison/v3` compares cash, current rules, at most five
  predeclared variants and an eligible simple holding benchmark.
- `pumpwizard.research-replay-report/v1` is the registered-job adapter around
  the detailed comparison. It verifies the immutable dataset digest, experiment
  lineage, current package and five bounded candidate identities before replay;
  `pumpwizard-research-replay-adapter/2` identifies the corrected adapter.
- `pumpwizard.incremental-paper-state/v2` checkpoints the same pure ledger with
  idempotent observation IDs, decisions recorded before outcomes and durable
  gaps.
- `pumpwizard.session-review-replay-report/v3` is the Session Review adapter.
  Existing `pumpwizard.session-review-report/v1` records remain readable as
  **fixed-horizon per-signal modeled v1** and are never relabeled as portfolio
  replays. Existing Session Review portfolio v2 reports remain readable as
  historical evidence, but are labeled pre-correction and cannot be resumed as
  v3 checkpoints.

Cash, fees, credits, basis and aggregate market/liquidity values use integer
quote micros (1e-6 quote currency). Token prices use a separate bounded integer
scale of 1e-18 quote currency per whole token, and token inventory uses integer
atoms at the quote-declared 0–18 decimals. A positive price is accepted only if
it is exactly representable at 1e-18; it is never clamped upward. Aggregate
quote amounts are rounded half-even to micros when adapters ingest decimals;
entry token quantity and percentage costs round down, the quote-value portion
of an entry debit rounds up to micros, and any unused reserved order budget
remains cash. Exit gross value and credit round down. A zero-atom entry, an
exit whose gross value is below one micro, and quantities above the documented
1e48-atom bound fail closed without deleting inventory.

Every quote names its quote currency and token decimals. A USD display price is
not converted into SOL or treated as executable without an explicit registered
input. Equal-time batch events use the deterministic order quote, exit signal,
entry signal, clock, followed by mint and event ID.

Time has four separate meanings:

- `event_at` is when the observation or decision occurred;
- `source_at` is the market timestamp used for quote freshness;
- `first_known_at` is when the process could first use the record and therefore
  enforces availability/no-lookahead;
- fill time is the actual decision/quote dispatch time recorded in the ledger.

Freshness is inclusive: `fill_time - source_at <= max_quote_age_seconds` is
fresh, assuming the quote is already first-known. `independent`, `provider` and
explicit `inferred-observed-at` source bases are executable; `unknown` and
`unverified` bases remain evidence/exclusions, not prices. Legacy direct engine
fixtures without source fields normalize explicitly to `event_at`; production
adapters always carry their recorded basis. Entry validity is the opposite at
its boundary: an order fills only while `fill_time < expires_at`. At
`expires_at` it is expired, and reservation release occurs before quote, signal
or clock dispatch. Pending exits do not inherit entry expiry: they retain the
position and wait for a later fresh representable quote.

Drawdown, exposure and terminal metrics use every event. Reported equity curves
retain at most 256 deterministic points, always including the first, last,
minimum, maximum and largest-unknown-basis observations; the report declares
the original and retained counts. This bounds job artifacts without changing
the accounting metrics or ledgers.

## Supported semantics

The engine supports complete legacy `ladder-v1` packages with:

- finite shared cash, reservations, maximum simultaneous positions and fixed
  order sizing;
- point-in-time entry decisions carrying the exact first-known evidence IDs;
- submission delay and entry expiry;
- target partial exits, stop loss, trailing exit, maximum hold and explicit
  final exit;
- curve/AMM quotes and an explicit `migration_from_pool` transition;
- configurable fees, spread, slippage, liquidity-relative impact, failed-entry
  and failed-exit fees, entry-only rent, unavailable exits and partial fills;
- unknown/stale/unpriced open positions that stay in atom, basis, equity and
  drawdown accounting.

A route/pool change without explicit migration lineage, quote-currency change,
decimal mismatch, future-known evidence or reused observation ID with changed
content is rejected or recorded as an exclusion. The engine does not invent a
fill for missing evidence and does not silently liquidate a delisted token at
zero while removing its inventory. Conservative equity assigns an unknown exit
value of zero but separately exposes its retained basis and an at-cost upper
scenario. A stale quote is retained in processed evidence and exclusions but
cannot replace the current executable quote, migrate a pool, fill either side,
mark a position or raise the trailing-exit high-water price.

Native-v2 packages, extra unrecognized ladder rules, implicit FX conversions,
arbitrary route migration, leverage, borrowing and onchain execution are not
supported. The older pinned `portfolio.py` reference replay remains unchanged
for its historical fixtures.

## Comparisons and evidence policy

Every alternative receives the same registered dataset lineage, capital,
period and base/adverse scenario structure. Cash is an eligible zero-return,
zero-drawdown outcome. The holding benchmark is explicitly “enter the first
eligible retained quote and exit the last”; a mint is excluded when it lacks two
quotes or has ambiguous currency, decimals or pool migration.

The conservative default evidence policy requires 20 independent episodes,
80% completed-position coverage, three dependence groups, drawdown no greater
than 30%, and nonnegative performance under the adverse scenario. These are
draft-eligibility defaults, not proof of edge. Tiny or synthetic samples remain
exploratory. An uncertainty interval is omitted below 30 declared independent
episodes; above that threshold it is labeled as a normal approximation and
states its independence assumption.

Selection uses validation only. Exploratory comparisons return no holdout rows.
For an eligible registered attempt, only cash, current rules, the holding
baseline and the selected candidate receive holdout output from the pure
comparison. The experiment registry atomically reserves holdout access
and counts failed/cancelled attempts across overlapping datasets. Changing
costs, policy, candidate identity or lineage changes the reproducibility digest
and constitutes a new registered trial.

Recommendation outcomes are:

- `insufficient_evidence` when coverage, independence, stability, valuation or
  stress policy is not adequate;
- `prefer_cash` when every evidence-measurable trading alternative loses to
  cash, including a gross price winner reversed by modeled costs;
- `keep_current` when current rules remain the best eligible validation result;
- `eligible_inactive_draft` only for a predeclared bounded variant that passes
  the registered base/adverse policy.

The research-job adapter partitions evidence by registered event-time ranges
and admits a quote only if its real first-receipt time is also inside the split
boundary. It rebuilds entry transitions from the exact decision engine with
prior-split observations used only as warm-up. New signals in the declared
embargo interval are suppressed. A position that still crosses the boundary is
left censored/unpriced and lowers coverage; future-split proceeds are never
borrowed into validation. The adverse run uses the same evidence lineage while
stressing declared spread, slippage, size impact and submission delay.

Draft creation still requires explicit acceptance and uses the existing package
parser. It requests paused monitoring, disabled entries, manual exits, zero
wallet allocation and no copied execution authorization.

## Hand-calculated integration fixture

The primary fixture starts with 200,000,000 USD micros and spends 100,000,000 at
1,000,000 micros per whole zero-decimal token, acquiring exactly 100 atoms. A
20,000,000,000-micro market-cap target sells 40 atoms (40%) for 40,000,000
micros. The explicit final exit sells the remaining 60 atoms for 60,000,000.
With zero costs, final cash is exactly 200,000,000, open atoms and reserved cash
are zero, and the two exit fills reconcile to the one 100-atom entry. Dashboard
can use these values directly; it must not reproduce the calculation in React.

Focused fixtures also cover equal-time cash contention, fee-only entry failure,
delayed entry, partial execution, unavailable/stale exit, explicit pool
migration, decimal mismatch, no signals, future-data rejection, embargoed and
censored episodes, cancellation, cash preference, cost reversal, restart,
durable gaps and batch/incremental parity.

Two low-price fixtures are also normative:

1. At **$0.000000001/token**, a 100,000,000-micro order with zero costs buys
   100,000,000,000 whole zero-decimal atoms. Selling them at the same price
   credits exactly 100,000,000 micros, so 200,000,000 starting micros returns to
   200,000,000. The same economic quantity is multiplied by 1e6, 1e9 or 1e18
   atoms for tokens with 6, 9 or 18 decimals.
2. At the same entry price with a 1% fee, 100,000,000 micros buys
   99,000,000,000 atoms after a 1,000,000-micro fee. Two equal exits at
   **$0.000000002/token** each sell 49,500,000,000 atoms, have 99,000,000 gross
   micros, deduct 990,000 fee micros and credit 98,010,000. Final cash is
   296,020,000 micros, total modeled fees are 2,980,000 and realized P/L is
   96,020,000 micros. Entry atoms equal summed exit atoms exactly.

## Prospective paper reuse

`IncrementalPaperEvaluator` v2 consumes the same immutable events as batch replay.
An entry signal must reference quote IDs already stored by that paper run. The
decision is appended before the replay can fill or later score it. Exact duplicate
IDs are idempotent; changed duplicates and backfilled earlier observations fail.
Checkpoints contain simulated portfolio state, zero wallet allocation,
`signing_authority: false` and `submission_enabled: false`.

Incremental and batch replay output are identical for the same complete ordered
fixture, including seeded failures, unavailable and partial exits, exclusions,
cash/position ledgers and restart after every event. Failure sampling is a pure
SHA-256 threshold of the registered seed plus signal ID, so signals created
after evaluator construction receive the same result before and after restart.
Real prospective operation may legitimately differ when observations were missed;
gaps stay recorded and later data never repairs prior paper history.

## Focused verification

```sh
PYTHONPATH=src pytest -q \
  tests/test_research_pipeline.py \
  tests/test_research_replay.py \
  tests/test_portfolio_replay.py \
  tests/test_portfolio_replay_correctness.py \
  tests/test_paper_evaluation.py \
  tests/test_paper_research_batch4.py \
  tests/test_session_review_replay_adapter.py \
  tests/test_session_review.py
```

No port, network provider, wallet or paid service is required.
