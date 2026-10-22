# Reviewed agent orders — dev38

One pinned core-frequency configuration can now prepare an unsigned real-route
order from its current local action. Each order still needs an explicit review,
interactive confirmation and either a wallet password or a bounded session. No scheduler, replay engine or AI
component can sign automatically. Tests use synthetic bank receipts; this build
does not establish funded mainnet acceptance.

## Workflow

Use the desktop's data directory for every command. The instance must contain the
exact bundled core-frequency configuration. Entry mode must be `manual` or the
saved future `automatic` preference, and entries must be unpaused; neither mode
enables unattended signing. Start tracking to build fresh observations and an
entry recommendation. Reserve a dedicated SOL amount for this agent using the
existing `reserve-agent` command.

```bash
solanamarket-wizard prepare-agent-order INSTANCE WALLET MINT \
  --cash-reservation CASH_HOLD
solanamarket-wizard review-agent-order ORDER
solanamarket-wizard agent-orders INSTANCE
solanamarket-wizard agent-positions INSTANCE

# Derive a partial/full exit from a finalized agent position and current rules.
solanamarket-wizard prepare-agent-order INSTANCE WALLET MINT \
  --cash-reservation CASH_HOLD --position-id POSITION

# Cancellation is only for unsigned reviews; funds stay allocated to the agent.
solanamarket-wizard cancel-agent-order ORDER

# Read-only recovery; never signs or replaces an uncertain transaction.
solanamarket-wizard reconcile-agent-orders WALLET
```

Preparation and reconciliation default to mainnet; `--network devnet` selects
devnet. `--rpc-env NAME` selects an HTTPS RPC URL through an environment variable.
Review uses the order's saved network and can also take `--rpc-env`. Preparation
accepts `--slippage-bps` within the existing manual route limits.

Review displays the network, wallet, mint, reason, exact spend/quantity, minimum
output, network fee, account funding allowance and estimated wallet change. It
requires typing `SIGN` followed by that order's ID, then entering the vault
password. Noninteractive submission is rejected. Declining leaves an unsigned
order that can be cancelled. An expired review must be cancelled and freshly
prepared; expiry never silently frees funds or replaces a signature.

## Eligibility and budgets

Buys require a fresh local recommendation and still-valid entry screen. The
adapter rechecks the actual curve's current cap and liquidity at preparation
and submission. It rejects configuration changes, paused/disabled entries,
stale observations, changed recommendations and changed SOL/USD budget inputs.
Open positions and reviewed/pending entries consume the configured position
capacity. Once an entry is submitted, a failed or completed outcome cannot
enable another entry for that mint in the same agent.

The configured USD order size is a **maximum total cash budget**. Conversion uses
the fresh local SOL/USD price; the maximum route fee and account-funding allowance
are subtracted before computing the integer SOL spend. Actual debit can be less
than this budget when funding costs are lower. A dedicated cash hold must cover
the spend plus fee/funding allowance. There is no conversion from replay cash or
modeled token quantities into real funds.

Sells take their exact quantity from a finalized position owned by this agent.
Target fractions apply to original token atoms; stops/trails use the remainder.
Token holds are created automatically from finalized buys. Other agents' holds,
other positions and user-recorded monitor positions cannot fund the order.

Reviewed exits now require a full-position unsigned executable quote. The
versioned net-risk policy compares the conservative payout floor, after approved
slippage, network fee and account-funding allowance, with remaining entry-FX
cost. Stops and trails use this net value; market-cap targets remain target gates
and require a separate simulation for the exact partial quantity. Submission
rechecks the current trigger and quantity before signing. Old exit reviews
without net evidence must be cancelled and prepared again.

Desktop monitoring still produces advisory observed-price recommendations. Its
spot high-water history is separate from the persistent executable net high-water
history. These policies do not establish live parity with the offline replay.
See [unsigned assessment and net-risk policy](AGENT-SHADOW.md) for the formula,
evidence history and read-only CLI workflow.

## Atomic allocation and recovery

An unsigned review binds its cash and token holds exclusively. Cancellation
removes that binding, without releasing the underlying agent allocation. Other
reviews cannot reuse the bound holds. Manual trading and withdrawals continue to
respect their reserved balances.

Submission atomically records the original signature, wallet exclusion and agent
order state before broadcast. Pause/configuration/observation changes are locked
out during the explicit submission, and the existing wallet revision check
rejects stale balance snapshots. Holds retain their full amounts while the
transaction is pending, unknown or merely confirmed.

Finalized receipt settlement updates the real position, agent cash allocation,
token allocation, target stages, fill attribution and order status in the same
SQLite transaction. Buys debit actual wallet cost and allocate actual received
tokens. Sells credit actual wallet proceeds and decrement only the sold tokens.
Fee-only failures debit the fee, preserving token holdings and unsold targets.
Receipt replay is idempotent; a failed settlement rolls everything back and
retains the wallet exclusion.

Missing receipts and expired blockhashes never establish failure or free funds.
The desktop's existing reconciliation and `reconcile-agent-orders` both recover
the original signature without unlocking a wallet. After explicit token-hold
release, a normal manual exit is an override; its receipt remains attributed to
the position's owning agent. It is not treated as a strategy target fill.

Finalized agent positions are read by monitoring without opening a vault or
creating a writable wallet ledger. Held mints remain scheduled when entries are
paused. Advisory monitoring high-water marks derive from retained observations, and exit
recommendations reference the actual position IDs. Monitoring still only
recommends exits; it never authorizes them.

The desktop **Agent trading** workspace now exposes setup, allocation, unsigned
assessment, reviewed authorization, cancellation and recovery alongside the CLI.
The manual sell form still respects token reservations; an explicit **Manual exit**
handoff releases the selected token allocation first. See [guided testing](GUIDED-TESTING.md).
Wallet-wide buy, recorded-cost exposure, SOL reserve and realized-loss limits
now apply to manual and agent buys when configured. Final checks and signing
share the wallet write lock. See [wallet risk controls](WALLET-RISK.md).
Funded acceptance, unrealized portfolio drawdown and unattended signing remain
future work.

The desktop can use an explicitly authorized [bounded signing session](SIGNING-SESSIONS.md) for reviewed orders. Session use does not enable automatic execution.
