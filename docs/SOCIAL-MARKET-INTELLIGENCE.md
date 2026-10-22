# Social and market intelligence — future research track

Planned September 27, 2026, following the user's request for X/Twitter monitoring
and recognition of patterns between social attention and token price movements.
This is a roadmap, not an implemented collector or a claim of predictive results.

## Product objective

Help users find, evaluate and manage opportunities with a measurable advantage
after costs and risk. Profitability is an outcome to test prospectively, not a
guarantee conditional on using the product correctly. Correctly followed rules
can still lose money. Useful outcomes also include avoiding poor entries,
recognizing deteriorating holdings and deciding not to trade.

## Signals to investigate

- Mention acceleration relative to each token's usual activity and time of day.
- Independent-author breadth, original posts versus reposts, repeated text,
  concentration among a few accounts and synchronized promotion patterns.
- Emerging narrative clusters and overlap between narratives and held assets.
- Project announcements, claims, counterclaims and changes in discussion topics.
- Whether social changes precede price, volume and liquidity changes or merely
  react to a move already underway; whether attention fades while price rises.
- Account-level historical signal quality, estimated only from information
  available at the time. Follower counts and verification are not proof of skill.

These are hypotheses, not established predictors. Pattern candidates include
attention-led continuation, late hype after a price surge, attention/liquidity
divergence and narrative decay. Suspicious coordination is a probabilistic data
quality flag, not a definitive accusation about an account.

## Data foundation

Use an authorized read-only X API adapter. Official filtered-stream and search
APIs provide potential collection paths; verify access, cost, coverage and current
terms before implementation. Do not assume complete historical or platform-wide
trend coverage. No paid subscription, credential setup, posting, liking, following
or other account action is authorized by this planning document.

Store post IDs/source links, publication time, first receipt time, edit/version
identity, matching rule, permitted content, and engagement snapshots with their
observation times. Historical final engagement totals must never become features
for an earlier prediction. Backfilled posts become known at collection time.
Record outages, rule changes, missing data, deduplication and collection latency.
Implement retention/deletion handling consistent with the selected data access.

Resolve mentions to exact token mints using explicit contract addresses and
corroborated project links. Tickers, names and copied branding alone are ambiguous.
Retain unresolved mentions without inventing a token association. Version entity
resolution and source features so later corrections do not rewrite what a model
could have known. Align with timestamped market/route and wallet evidence.

## AI and user experience

AI may classify topics, extract attributed claims, cluster similar posts,
summarize changes and propose bounded research rules. Numerical models estimate
outcomes; deterministic code controls risk and execution. Treat social content
as untrusted input, never as instructions to the assistant or signer. Generated
research code runs without credentials, wallet access or live-execution authority.

Add an optional Social context panel to a token/holding: attention change,
independent-source breadth, narrative, representative source links, freshness,
coverage and market confirmation. Alerts should explain what changed and whether
the move predates the signal. Unknown coverage must not look like quiet sentiment.
User alert preferences are separate from any execution authorization.

## Prove incremental value

Compare the same strategy with market-only features, social-only features and
combined features on the same point-in-time token universe and cost assumptions.
Include failed/dead tokens and negative outcomes, not just successful pushes.
Measure lead time, attainable execution after arrival latency, net result,
drawdown, tail losses, turnover, capacity, calibration and false-alert burden.
Account for data/model costs as well as transaction costs. Correlation alone does
not establish causation or a tradable advantage.

Predeclare hypotheses, horizons, parameters and promotion criteria. Use
chronological evaluation, embargo overlapping outcomes, track all experiments,
and prevent reuse of exposed holdouts across overlapping dataset versions. Group
related accounts/campaigns/tokens where needed to avoid near-duplicate leakage.
Stress missing feeds, fabricated engagement and liquidity loss. Show uncertainty
and insufficient-evidence states; do not invent confidence percentages.

## Delivery sequence and acceptance

1. Point-in-time market collector and realistic portfolio replay foundations.
2. Bounded social collection with identity, deduplication, latency and cost tests.
3. Read-only holding context and alerts with inspectable source evidence.
4. Offline pattern research proving incremental value against market-only rules.
5. Forward paper monitoring with frozen rules and all predictions logged before
   outcomes. Publish failures and regime-dependent degradation alongside successes.
6. Only evidence-backed candidates may enter the existing separately authorized
   funded acceptance and bounded execution workflow. Social signals cannot grant
   an allowance, change a running strategy or loosen loss limits.

Integrate with [Session Review Lab](SESSION-REVIEW-LAB.md) and
the session learning plan. Do not change strategy
exchange formats, build versions or release status merely to add this roadmap.

## Provider references checked September 27, 2026

- [X filtered stream](https://docs.x.com/x-api/posts/filtered-stream/introduction)
- [X post search](https://docs.x.com/x-api/posts/search/introduction)
- [X API pricing and credits](https://docs.x.com/x-api/getting-started/pricing)

Access and pricing must be rechecked when implementing; no cost estimate or live
provider acceptance is established by this plan.
