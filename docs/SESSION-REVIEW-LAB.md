# Session Review Lab

> Batch 3 extends this v1 compatibility workflow with durable datasets,
> background experiments, finite-capital reports and prospective paper runs.
> See [Research dashboard](RESEARCH-DASHBOARD.md) and the
> [integrated Batch 3 review](RESEARCH-DASHBOARD.md). Saved v1 reports
> described below remain readable when the Batch 3 route is unavailable.

Status: integrated local checkpoint, September 27, 2026. The offline engine,
append-only producer storage, authenticated API and browser flow are wired in
unpublished `0.2.0.dev62` source.

## Safety boundary

Session Review is local, deterministic research. It imports no wallet, RPC,
signer, order, scheduler or execution service. A comparison cannot edit its
source session, change a running strategy, allocate funds, enable monitoring,
sign or submit a transaction.

The three evidence classes remain separate:

- retained quote outcomes are counterfactual price movements;
- Session Review results are modeled fixed-horizon replays with explicit costs;
- funded receipts are execution evidence and are not inputs to this first
  comparison engine.

Accepting a supported suggestion produces only a
`pumpwizard.session-review-draft/v1` request. It asks shared integration to
import the already validated candidate package and create a paused session with
monitoring off, entries disabled, manual exits, zero wallet allocation and no
copied execution authorization. Existing validation still owns the actual
import/session creation.

Main-wallet custody and runtime authorization are separate work. See
[Main-wallet Autopilot](MAIN-WALLET-AUTOPILOT.md); Session Review does not grant
or preserve an allowance.

## Implemented contracts

`src/pumpwizard_runner/session_review.py` exports:

- `SessionReviewStore(data_dir)`: a separate append-only
  `session-reviews.sqlite` store. It contains immutable datasets, completed
  reports and one holdout-search lock per dataset.
- `freeze_dataset(...)`: validates an exact source strategy digest, rejects
  evidence labelled for another session, canonicalizes retained observations,
  decisions, linked outcomes and decision-time feature records, and saves a
  content-addressed `pumpwizard.session-review-dataset/v1` payload.
- `capture_decision_features(...)`: creates a
  `pumpwizard.session-decision-features/v1` record during the producing decision
  cycle. It records the exact strategy digest, engine, normalized inputs,
  thresholds, route/pool and observation hash. It rejects future observations.
- `generate_candidates(package)`: accepts only the complete `ladder-v1` family
  and returns the current strategy plus no more than five bounded one-parameter
  variants. Every package passes through the existing `parse_package` parser;
  the strategy exchange format is unchanged.
- `compare_dataset(...)`: replays retained source-session observations with the
  decision engine, creates chronological 60/20/20 boundaries, embargoes signals
  whose outcome window crosses a boundary, selects only on validation, then
  shows holdout. Missing endpoints, gaps, route/pool changes and candidate
  liquidity-floor failures stay unknown.
- `create_inactive_draft_request(...)`: requires explicit acceptance, the
  report-selected candidate and an actual `create_inactive_draft`
  recommendation. It cannot be used for a collect-more-data or keep-current
  result.

`web-client/src/SessionReview.tsx` exports `SessionReview`,
`SessionReviewReport`, `SessionReviewResult` and `SessionReviewEvidence`. Its own
styles are imported from `session-review.css`. It presents current, candidate
and no-entry baseline rows across training, validation and holdout; sample and
coverage counts; modeled result, exposure and drawdown; exact costs; evidence
limits; Keep current; and a two-step inactive-draft confirmation.

`SessionMonitor` now accepts an optional `review: () => void` prop. It shows
**Review session evidence** only after shared integration supplies the handler,
so the current app is not wired to a nonexistent endpoint.

## Dataset, split and cost semantics

The dataset ID and data hash are the SHA-256 of canonical substantive inputs:
schema, capture engine, source session, exact package/digest, period,
observations, decisions, feature provenance, outcomes and evidence coverage.
Insertion time is not part of the payload. Re-freezing identical inputs returns
the byte-equivalent saved payload.

The caller must read all inputs from one shared-library database snapshot.
`freeze_dataset` validates the supplied boundary but does not independently read
or migrate `library.py`. Observations are unique by mint/timestamp and all input
records carrying `instance_id` or `session_id` must match the source session.
Outcomes must link to a frozen decision and retain the
`counterfactual_retained_quotes` basis. No outside-wallet activity is accepted.

Historical dev61 decisions are transition records: unchanged rejection reasons
were not saved on every scan, and feature vectors were not appended. Datasets
therefore default to
`eligible_decision_capture: historical_transition_records_only`; absent features
are retained as `feature_snapshot_not_recorded_at_decision_time`. The comparison
can explain these records but must return **collect more data**, even if its raw
quote replay has enough samples. Integration may set
`decision_capture_complete=true` only for a period covered by the new append-only
producer. It must never backfill historical features from later quotes.

Candidate generation is independent of outcomes. The current package and all
variants are frozen in the search hash before evaluation. Split boundaries use
ordered retained observation times. A signal belongs to a split only when both
its decision and fixed-horizon endpoint are inside that split; otherwise it is
embargoed. Ranking uses validation mean modeled return minus sequential
trade-result drawdown, with fixed sample/coverage gates and deterministic
tie-breaks that prefer the current package when substantive scores are equal.
Holdout metrics are never read by ranking.

The first report on a dataset atomically records a holdout lock. An identical
rerun returns the saved report. A different horizon, engine, candidate set,
cost/ranking rule or split identity is rejected after holdout exposure; the user
must freeze new future evidence rather than tune repeatedly against the old
holdout.

The round-trip model deducts configured entry rent once, configured fixed cost
on entry and exit, and configured variable fee and adverse/slippage fraction on
both sides. It requires continuous same-route/same-pool quotes and the
candidate's route-specific liquidity floor. It is intentionally not the pinned
production `PortfolioEngine`, an executable quote, an unsigned chain simulation
or a funded fill. `evaluation.py` and its exact Core Frequency eligibility gate
are unchanged.

## Shared integration record for dev62

### Library migration and read hook

Dev62 serializes schema 14 in `library.py` and adds append-only storage for
decision-time features and every evaluated entry opportunity, including repeated
rejections whose reason did not transition. The implemented shapes follow:

```text
decision_features(
  instance_id, decision_key, captured_at, schema_name, strategy_digest,
  engine_version, payload_json,
  PRIMARY KEY(instance_id, decision_key)
)

evaluated_opportunities(
  instance_id, evaluation_key, at, mint, decision_key, eligible, reason,
  strategy_digest, feature_key, payload_json,
  PRIMARY KEY(instance_id, evaluation_key)
)
```

The monitor producer should call `capture_decision_features` in the same cycle
that produced the decision and commit observations, opportunity records,
decisions and features atomically. Do not call it to reconstruct old decisions.
If the producer needs additional internal fields to represent every unchanged
rejection, extend its emitted local record; do not change the exported strategy
package.

Add one read method that opens a consistent transaction and returns:

```text
instance id + exact strategy digest/package
period first_at/last_at
all source-session observations in the period
all eligible opportunity decisions/rejections with decision_key
all decision_outcomes joined back to decision_key
all decision feature payloads
whether the whole period has complete evaluated-opportunity capture
```

Pass those values directly to `freeze_dataset`. Do not infer a missing digest,
feature record or outcome. Ensure outcome payloads include their `decision_key`;
the existing public `decision_outcomes()` payload omits that join key.

### Implemented API hooks

Suggested local-only endpoints and response contracts:

```text
POST /api/v1/sessions/{instance_id}/reviews
  body: { horizon_seconds: 1800 }
  response: { dataset: { id, evidence }, report }

GET /api/v1/sessions/{instance_id}/reviews
  response: { reports: [...] }

POST /api/v1/session-reviews/{report_id}/draft
  body: { candidate_id, accepted: true }
  response: { draft_request, session }
```

The first endpoint reads/finalizes the shared evidence snapshot, freezes it,
then calls `compare_dataset`. Cancellation must stop before `save_report`; retry
may reuse the immutable dataset. The draft endpoint must reload the completed
report from `SessionReviewStore`, call `create_inactive_draft_request`, import
the exact returned package through existing validation, and create a new safe
brief/session. Do not mutate the source instance, inherit its reservation or
authorization, or auto-start monitoring. **Keep current** is a client-side close
or acknowledgement only and needs no mutation endpoint.

Return `409` for stale strategy identity or a holdout-lock conflict, `422` for an
unsupported family/insufficient input contract, and a cancellation response
that is not listed as a completed report. Keep detailed local errors free of
wallet secrets and raw authorization material.

### App wiring and browser coverage

Import `SessionReview` in the shared `App.tsx`, keep one review state keyed by
source session ID, and pass `review={() => openSessionReview(id)}` into
`SessionMonitor`. The component expects the API's `report` and dataset
`evidence`, plus `run`, `keepCurrent`, `createDraft`, `close`, `busy` and `error`
callbacks. A successful draft response should refresh strategy/session lists and
open the new session only as an inactive draft; it must not close over stale
wallet/session identity.

The dedicated browser flow still needs to be added against test port 8772 after
these shared hooks exist. Cover sparse evidence/collect-more-data, a selected
candidate, exact costs and coverage, two-step acceptance, zero allocation,
entries/monitoring disabled, source session unchanged, API failure/retry,
keyboard focus and a 390-pixel viewport. Do not mock a funded receipt as a Lab
result.

## Verification at this checkpoint

Completed:

```text
PYTHONPATH=src python3 -m pytest -q tests/test_session_review.py
13 passed

PYTHONPATH=src /tmp/pumpwizard-review-20260927/bin/pytest -q \
  tests/test_session_review.py tests/test_evaluation.py \
  tests/test_decision.py tests/test_strategies.py
35 passed

npm run lint
npx tsc -b --pretty false
npx vite build --outDir /tmp/pumpwizard-session2-web --emptyOutDir
passed; isolated production assets built (one existing >500 kB chunk warning)

git diff --check -- [owned implementation paths]
passed
```

The system Python lacks `solders`, so the broader Python subset was run
successfully with the prepared `/tmp/pumpwizard-review-20260927` environment.
Ruff is not installed in either available Python environment at this checkpoint.

Final shared integration passed the complete 802-test Python collection (801
passed, one intentional skip), Ruff across source/tests/packaging, frontend
lint/typecheck and production build, and all 38 production-bundle Playwright
flows. The new browser scenario covers failure/retry, sparse collect-more-data,
cost and evidence text, a selected candidate, two-step confirmation, source
immutability and a paused zero-allocation successor at 390 px. Dev62 has a local
draft Community note; no package or Community publication was completed.
