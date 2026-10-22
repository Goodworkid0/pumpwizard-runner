# Remaining funded lifecycle acceptance

**Current delivery priority, September 29, 2026:** follow the
[PumpSwap automatic-trading plan](ROADMAP.md). The separately
scoped [service acceptance capability](PUMPSWAP-SERVICE-ACCEPTANCE.md) is now
implemented through source fixtures, authenticated API/UI and an exact-package
no-order activation/restart exercise. The packaged executable also passed one
unsigned canonical standard-SPL PumpSwap buy simulation. Funded service-owned
buy/partial-sell/restart/final-sell evidence remains pending. The historical manual
curve buy below does not cover PumpSwap entry or service custody. Keep manual-route
and strategy-owned service acceptance distinct; a synthetic runtime flag or a
caller-authored acceptance file cannot authorize a live run.

Status, September 25, 2026: **manual buy verified; complete lifecycle pending**.
The user independently approved a Firefox Phantom 0.01 SOL buy, finalized at
slot 450369639: 352,986.344279 tokens received, a 0.01151884 SOL wallet debit and
a 0.000005 SOL transaction fee. See the retained receipt summary.
This is manual ownership evidence, not strategy-owned or automatic execution.
Partial sell, restart recovery and full sell remain unverified with real funds.

The original position can no longer continue that lifecycle. A read-only
mainnet comparison found that 223 seconds after the recorded buy, the wallet
signed a separate finalized transaction at slot 450370475 that removed the full
352,986.344279-token balance and increased the wallet by 0.007746725 SOL. It is
not present in Runner's trade journal. The current wallet owns zero atoms of the
mint, so Runner correctly rejects a sell rather than applying a receipt to a
stale local position. See the
external-position discrepancy record.
This external disposal does not satisfy partial-sell, restart or full-sell
acceptance. A later attempt requires a newly approved funded position and must
remain untouched by other wallet activity for the duration of the test.

The current-holdings Portfolio now omits this zero-balance token, as intended.
Runner still retains the externally closed discrepancy and the original/external
transactions in its local reconciliation and receipt data; removing historical
audit detail from the Portfolio UI does not convert the external transaction into
Runner acceptance or accounting. See [connected-wallet Portfolio](WALLET-PORTFOLIO.md).

The subsequent automation implementation plan
authorizes development and synthetic validation, not additional real transactions
or live session activation. Exact amounts and runtime authorization remain for
the user to approve after the implementation is concrete. Existing synthetic
tests and deployed unsigned samples have separate evidence scope in
[VERIFICATION-STATUS.md](VERIFICATION-STATUS.md).

The implementation is now concrete and documented in
[AUTOMATION-WALLET.md](AUTOMATION-WALLET.md). Its production gate requires both
this reviewed funded lifecycle and a separate strategy-owned funded lifecycle,
bound to the current signer/risk/route identity. Synthetic passes do not create
that acceptance record and do not permit live authorization.

## Browser manual lifecycle gate before Dev62

The [Dev61 source checkpoint](DEV61-REVIEWED-LIFECYCLE.md) passes synthetic browser
buy → partial sell → Python process restart → full sell with exact receipt
accounting. Those synthetic results are not funded evidence. Before continuing
the real browser run after the verified manual buy, the user
must explicitly select the wallet, exact token mint and maximum total SOL budget
including fees/account funding and exit attempts. No amount is inferred here.

Review each transaction separately. Retain the finalized buy's exact debit and
quantity, partial sell's net proceeds/fee and remaining basis, the restarted
journal's original IDs/signatures, and the final sell's closure and total balance
delta. Record failed or unresolved signatures without substituting explorer data
for Runner's validated finalized receipts. If a curve migrates, request a new
route review. Leave unobserved migration and wallet-extension cases pending.

The strategy-owned acceptance table below is a separate later gate for the
strategy-to-order bridge; a manual position is not agent ownership evidence.

## Preparation before any separately authorized funded run

- Record source revision/build, exact package/rules hashes, engine version,
  selected wallet and network, approved mint/route and provider identity.
- Obtain an explicit total SOL budget covering entry, fees/account funding and
  exit attempts, with user-selected allocations, slippage and wallet limits.
  No funded amount is chosen by this checklist.
- Verify encrypted backup/recovery, sufficient fee reserve, no unresolved wallet
  operations, and fresh observations/valuation. Record limits and their scope:
  local recorded positions in SOL, not external inventory or USD loss protection.
- Replay the pinned configuration and inspect unsigned entry/exit evidence. Use
  the same exact strategy; do not change rules or fabricate signals to finish a test.
- For manual browser acceptance, review and sign each order freshly. For the
  separate service-custody pilot, sign the exact build/route/allowance acceptance
  once, record scope/expiry/caps, and let only exact reviewed tickets inside that
  non-replenishing allowance proceed; there is no per-order extension signature.

## Acceptance record to complete

| Step | Required observations | Current funded status |
| --- | --- | --- |
| Reviewed buy | Saved order ID/signature, finalized receipt, exact wallet debit/token delta, agent-owned position and cash/token holds | Pending |
| Unsigned exit assessment | Fresh route/net-value evidence for that actual owned position, trigger and rejection reasons | Pending |
| Reviewed partial exit | Finalized quantity/proceeds/fee, proportional remaining cost, target stage and allocation settlement | Pending |
| Restart | Original IDs, holds, remaining quantity/cost and P/L preserved; session locked and worker not auto-started; reconciliation idempotent | Pending |
| Reviewed full exit | Fresh signal/review, finalized remaining quantity, exact residual cost settlement and reconciled proceeds/holds | Pending |
| Migration | Supported curve → canonical PumpSwap exit, fresh review after route change; mint/program/pool mode recorded | Pending; do not infer from unsigned public-holder probes |

The verified manual buy above remains valid buy evidence, but its position is
externally closed and cannot supply any of the pending sell rows.

Retain redacted order IDs, signatures, finalized slots/receipts, package identity,
unsigned report exports, before/after journal totals and discrepancies. Never
include passwords, keys or signing capabilities. A completed path only establishes
the tested configuration, token program, route/mode, amounts and time window.
If migration is not observed, leave that row pending and schedule a separately
approved case; a normal curve lifecycle cannot close it.

## Recovery and authorization drills

Record synthetic/local drills separately from any observed funded incident. Do
not manufacture a dangerous network failure with real funds to obtain a pass.

- Submission uncertainty: retain original signature and full holds through
  pending/unknown/confirmed states; recover to finalized evidence without
  replacement or re-signing. Missing receipts/blockhash expiry are not failure.
- Expired unsigned review: cancel its binding explicitly, retain allocation,
  then prepare again. Duplicate clicks and repeat reconciliation must not duplicate fills.
- Entry pause, wallet risk blocks, stale/unknown portfolio values and RPC outage:
  entries fail closed as applicable, held positions remain visible, exits retain
  their independent route/ownership/fee/recovery checks. Limits do not liquidate.
- Session expiry/revocation/cap exhaustion/restart: show unmanaged authorization
  needs, reject unauthorized signing, and require a separate password review or
  fresh bounded session for exits. No renewal or fallback is automatic.
- Manual-exit handoff: explicitly release token allocation, separately review
  sell, retain owner attribution. Proceeds return outside the agent cash hold;
  this does not count as a strategy-triggered exit.
- Failed transaction: reconcile actual fee-only loss and preserve unsold tokens
  and target stages. An unobserved funded failure remains untested as a funded case.

Acceptance requires reconciled receipt/accounting evidence and no unresolved
defect. Any blocked or unobserved step stays open with its reason. Completion
does not enable automatic execution: that requires a separate implementation,
integration validation and explicit authorization, including the active PumpSwap
plan's route-matched shadow-operation and failure-drill gates.
