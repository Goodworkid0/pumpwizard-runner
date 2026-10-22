# Wallet entry limits — dev40

Open **Agent trading → Wallet limits**. Enter your own SOL limits, review the
wallet/network and save. No numerical limits are chosen automatically. An
unconfigured wallet keeps the existing per-trade approval workflow and displays
**Not configured**. This is not authorization for unattended trading.

Policies are keyed by wallet address and network genesis in the shared wallet
journal. Every current-version manual buy and agent buy uses the same checks.
Limits can be saved before depositing or while RPC is unavailable. Saving does
not allocate funds, open a signer session, or sign a transaction.

| Control | Meaning |
| --- | --- |
| Maximum buy cost | SOL amount plus the quoted network fee and conservative account-funding allowance. |
| Maximum open cost | Remaining recorded cost of all local open positions, plus pending buys and bound agent buy reviews, plus the proposed buy. |
| SOL reserve after buys | Minimum wallet SOL left after the proposed maximum buy debit. Existing allocations also remain enforced. |
| Daily realized loss | Sum of losing finalized sell P/L and failed-trade costs during the receipt's UTC calendar day. Profits do not offset this gross loss counter. |
| Realized P/L drawdown | Difference between the historical high and current cumulative finalized trade P/L, starting at zero. Profits can recover this drawdown. |
| Portfolio drawdown | Difference between the retained trading P/L high and current realized P/L plus conservative unrealized P/L on every local open position. |
| Pause new buys | Blocks every local buy on that wallet/network, while leaving exits and recovery available. |

Maximum cost and reserve boundaries are inclusive. Reaching a loss/drawdown
limit blocks subsequent buys. It does not liquidate positions or guarantee a
maximum eventual loss. Open positions can continue losing value. All existing
exit checks, including available fees, token ownership and unresolved-operation
exclusion, still apply.

## Accounting and recovery

Amounts use integer lamports. Partial sells allocate entry cost with the same
integer formula used by finalized position settlement; the final sell consumes
the exact remaining cost. Buy fees/rent are included in position cost, sell
proceeds are net wallet deltas, and failed-trade costs are counted directly.

Metrics derive from finalized receipts in journal insertion order. This includes
trades made before limits were configured. Changing a policy or reopening the
app does not reset history. At midnight UTC only the daily counter resets;
historical realized drawdown remains. A late receipt belongs to its original
block-time day. A missing required receipt blocks new buys when limits are
configured instead of treating missing accounting as zero.

Pending, confirmed-but-not-finalized and unknown buys retain their maximum
committed cost. Expired unsigned agent reviews also retain exposure until
explicit cancellation. An unbound manual preview does not reserve exposure;
submission checks the latest total under the wallet write lock. Finalized buys
replace their commitment with actual recorded cost. Receipt replay cannot
duplicate P/L because it reads the same uniquely recorded trade.

Checks run during quoting, during agent review binding, and immediately before
signing. Policy updates increment the wallet revision. Final policy, allocation
and revision checks, signing, and durable order reservation share one SQLite
write transaction. Another current-version process cannot change a policy or
consume that wallet between these steps. Policy changes are recorded in
`wallet_risk_changes`; signing material is not stored there.

## Portfolio valuation

**Refresh portfolio value** performs public reads and unsigned sell simulations;
it never creates an order, consumes an agent allocation, unlocks a wallet or signs.
New buys refresh it at quote time and again before submission. The worker also
refreshes it each cycle, even while entries are paused. Final checks under the
wallet signing lock require a matching position/accounting fingerprint, wallet
revision, available SOL balance and a snapshot no older than 30 seconds, measured
from the start of the entire collection. A large/slow portfolio that cannot be
valued within that interval remains blocked for new buys.

Positions sharing a mint are quoted together so liquidity is not counted twice.
Each supported route must pass both unsigned sell simulations. Value uses a
fixed 5% output margin, minus the network fee and the existing 0.02 SOL worst-case
account-funding allowance per mint; a negative floor is clamped to zero. The
fixed margin prevents changing an order's slippage from resetting the valuation
basis. Failed simulations, missing tokens, unsupported migration routes,
unresolved wallet operations and stale/inconsistent snapshots block new buys.
Unknown values are not silently treated as zero or replaced by an old quote.

Unrealized P/L is aggregate net liquidation value minus remaining recorded cost.
Portfolio P/L adds finalized realized P/L. Its peak starts at the larger of zero
and historical realized peak, then retains higher valid observed portfolio values.
Deposits and withdrawals are not trading profits or losses. Closed/partial
positions and failed-trade fees enter through normal finalized accounting.
Historical open-position peaks from before valuation was enabled cannot be
reconstructed. The separate `portfolio-risk.sqlite` evidence store preserves the
peak through restart, policy edits and failed valuations. Shadow collection does
not write the wallet or strategy journals. The desktop labels displayed amounts
as the last estimate and shows stale/blocked entry reasons separately.

Reaching the ceiling blocks new buys; it does not force liquidation or prevent
further losses. Every sell retains its existing route, ownership, fee and receipt
checks independently of the portfolio entry gate.

Existing saved policies remain readable without an invented portfolio limit.
Saving a new policy requires every field, including the new ceiling. Previously
configured policies retain their old guided-trading behavior until updated;
authorizing a new signing session requires the portfolio ceiling. No automatic
trading mode is enabled by this upgrade.

## Scope and remaining work

These controls cover **local recorded positions and trading P/L in SOL**, not
the entire on-chain wallet. Untracked/external positions, external transaction
fees, withdrawal fees and SOL/USD changes are outside these metrics. The reserve
is enforced on buys; it does not prohibit a user-directed
withdrawal. Older executable versions do not enforce these new policies.

Before unattended operation, still complete funded acceptance and automatic
order scheduling/exit integration with operational health controls. A future
automatic session must require an explicitly configured policy; the current
unconfigured guided mode must not silently become an automatic mode.

Tests use synthetic receipts and throwaway wallets. No funded acceptance or
live-loss protection claim follows from those tests.

Bounded reviewed [signing sessions](SIGNING-SESSIONS.md) are available in dev38. They require configured wallet limits and do not enable an automatic worker.
