# Forward campaign performance and receipt evidence

Status: Batch 5 local reporting contract. This feature reports observation and
retained evidence only. It grants no wallet allocation, signer, order,
submission, provider-purchase or live-trading authority.

## Schemas and lineage

`pumpwizard.forward-performance-input/v1` binds an immutable campaign ID and
manifest SHA-256 to:

- each arm's paper-run and portfolio-report identity;
- exact integer quote-micro cash, token atoms at declared decimals, and integer
  1e-18 quote-currency prices;
- actual operating intervals, durable gaps and matched evidence IDs;
- an explicit as-of time, preregistered readiness policy and selection
  hypothesis;
- provider costs separated into observed charges, configured estimates, shared
  fixed cost and unknown components; and
- optional, separate receipt and accounting reconciliation reports.

`pumpwizard.forward-performance-report/v1` is deterministic and content
addressed. The same canonical input produces the same report ID. A correction
has a higher revision and links `correction_of_report_id`; changing a selection
hypothesis requires preserved prior report IDs. Merely viewing a saved report
does not evaluate evidence or create another trial.

The report contains whole-run and UTC-day summaries, bounded fill/cash/open-
position drilldowns, engine identities and per-metric provenance. The campaign
store persists snapshots append-only. JSON and CSV export read those saved
snapshots rather than recomputing them.

## Fair comparison and readiness

Cash, the declared holding benchmark and paper arms must share the same evidence
IDs, operating interval shape and declared capital to be ranked. A coverage,
uptime or capital mismatch produces `withhold_ranking`; full-history metrics may
remain visible, but they are not ranked as if matched. If every comparable
trading alternative has a non-positive modeled result, the result explicitly
prefers cash.

Reported arm metrics come from the immutable v3 finite-capital replay:
conservative ending equity, modeled realized and known unrealized P/L, unknown
basis, exposure, drawdown, opportunities and modeled costs. Turnover is the sum
of absolute replay fill cash deltas. Stress reports are separate immutable
replays and never rewrite the prospective history.

Readiness uses the preregistered minimum episode/token counts, observation
duration, coverage, stable groups, maximum gap and exact replay engine identity.
Small or correlated samples stay exploratory. A normal-approximation interval is
shown only when the declared independent return count matches the replay's
independent episode count and clears the preregistered minimum. Evidence
readiness is never execution or live-trading readiness.

A provider with unknown or partial pricing is not treated as free. Modeled arms
may still be compared, but `fully_costed` profitability remains unsupported.

## Accounting reconciliation

`pumpwizard.accounting-snapshot-input/v1` is a read-only reporting ledger. It
checks starting cash plus strategy fills/fees and explicit deposits/withdrawals
against ending cash. It independently conserves token atoms and allocated cost
basis across entries, partial/final exits and transferred inventory.

Deposits, withdrawals and known transferred-in/out basis are external capital
flows and are subtracted from strategy return. Unknown transferred basis,
unattributed inventory, a stale/missing mark, changed asset identity, missing FX
or a value below quote-micro precision keeps net performance unknown. None is
converted to zero.

## Supported receipt coverage

`pumpwizard.receipt-evidence-report/v1` reads only the existing
`wallet-transfers.sqlite` journal using SQLite read-only/query-only mode. It does
not initialize tables, call RPC, scan a wallet, reconcile a signature or write a
reservation.

Trade identities use genesis/network, signature and the authorized transaction-
effects scope. Withdrawal identities use genesis/network, signature and System
Program transfer instruction 0. Exact duplicate identities collapse; conflicting
content fails closed. The adapter distinguishes:

- finalized success with retained validated trade effects;
- finalized fee-only failure only when token delta is zero and wallet delta is
  exactly the negative retained fee;
- pending/confirmed evidence awaiting a finalized transaction; and
- unknown, missing or inconsistent outcomes.

An agent owner is asserted only from a retained `agent_order.agent_id` binding;
otherwise a trade remains manual. Exact expectation comparisons never change an
`observed_paper` or `retrospective_scenario` evidence class into
`recorded_execution`.

Current limits are deliberate: arbitrary wallet history, unretained deposits,
external swaps and finalized withdrawal balance receipts are not discovered.
Withdrawal finality is supported from the retained status plus the fee validated
at submission, but it is labeled separately from a retained transaction-balance
receipt. Native SOL lamports are never silently converted to USD or another quote
currency.
