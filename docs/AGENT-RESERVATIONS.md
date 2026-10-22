# Agent wallet reservations — dev33

An agent can hold an exact SOL amount and/or part of a finalized manual-trade
position. Two agents cannot reserve the same units. Manual buys, sells and SOL
withdrawals can use only the remaining resources. All commands below use the
same `--data-dir` as the desktop; no vault password or private key is needed.

```bash
# INSTANCE is an existing local agent ID; WALLET is a public address.
solanamarket-wizard reserve-agent INSTANCE WALLET --sol 0.12
solanamarket-wizard reserve-agent INSTANCE WALLET --position-id POSITION --tokens 400
solanamarket-wizard agent-reservations --sender WALLET
solanamarket-wizard release-agent-reservation RESERVATION
```

`reserve-agent` defaults to mainnet; `--network devnet` and `--rpc-env NAME`
select the network and an HTTPS RPC URL stored in an environment variable.
SOL and tokens can be held together in one command. Include the intended fee
and account-funding allowance in a SOL allocation; it is not added automatically.
Token quantities have at most six decimals; SOL has at most nine. Token holds
require a supported curve or PumpSwap route and a confirmed position belonging
to the selected wallet and network. User-recorded monitor positions are separate
and cannot establish on-chain ownership for a hold.

Each call creates a new hold with its own ID; it does not replace an earlier
hold for the same agent. List the holds after an interrupted command before
retrying. Release is idempotent and retains the original record. Holds survive
restarts and never expire automatically. Pausing/deleting a monitor or cancelling
a dry-run order does not release them. Reservation controls are CLI-only in this
build; the desktop enforces holds through its existing transaction services.

The `agent_reservations` table lives beside trades and withdrawals in
`wallet-transfers.sqlite`. Integer quantities are stored as decimal text and
summed with Python integers. Network identity and sender scope each allocation;
token checks apply to both the mint's wallet balance and the particular position.
Allocation takes a SQLite write lock before checking pending wallet operations
and fetching fresh balances. Manual execution checks available resources again
in the transaction that persists the signature, before broadcast. This closes
the race where another process adds a hold after quote review.
A wallet revision counter also invalidates submission if another local operation
starts and finishes during quote validation, even if its pending lock has already
been released. The old balance snapshot cannot be used to spend held funds.

An unresolved wallet operation blocks creation and release of holds. Missing
receipts or expired blockhashes never release resources. A decrease in external
wallet balances blocks operations that cannot cover the remaining holds; another
wallet app cannot be prevented from spending on-chain. Use only current-version
processes against this data directory; older binaries do not implement these
checks.

These are **allocations**, not automatic execution authorization or portfolio
exposure limits. Dev33's [reviewed agent orders](AGENT-EXECUTION.md) can bind a
dedicated SOL hold and an agent-owned token hold. Submission retains those holds
until finalized receipt accounting consumes actual costs/tokens or credits actual
proceeds. Unsigned reviews must be cancelled before their holds can be released.
Releasing an unbound hold permits the ordinary manual workflow to use its funds.

Dev32 verifies the pinned strategy's [offline portfolio transitions](PORTFOLIO-REPLAY.md).
Those virtual cash reservations remain separate from the exact on-chain unit
holds described here; replaying a modeled fill never consumes wallet resources.

Synthetic tests cover concurrent connections competing for insufficient SOL,
allocation races at signing, restart/release, wallet balance deficits, shared-mint
position isolation, partial sells, unresolved submission and CLI validation.
They do not constitute funded mainnet acceptance.
