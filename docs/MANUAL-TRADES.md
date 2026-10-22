# Manual Pump trades — dev55

The browser starts with wallet sign-in. It can connect an installed Wallet
Standard wallet or create and unlock an encrypted browser wallet. After sign-in,
**Trade** opens the manual transaction workflow with that active wallet. The
legacy native client can still use Runner's older local vault. Monitoring agents
and AI chat cannot submit trades. Nothing signs until the user reviews the
network, mint, exact amount, minimum route output, fee and account-funding
allowance, then approves that one transaction.

## Supported routes

`pump-v2-native-sol-spl-v1` supports native SOL buys and token sells on active Pump
bonding curves, using `buy_exact_quote_in_v2` and `sell_v2`. It currently accepts
standard SPL Token mints with six decimals and revoked mint/freeze authorities.
Creator and holder-reward fee destinations are supported; see the current
[reward-route integration and evidence](REWARD-ROUTES.md). Non-SOL quotes and
mayhem/cashback modes are rejected. A completed curve redirects a fresh buy
review to the canonical PumpSwap route. Automatic service activation remains a
separate acceptance boundary.

`pumpswap-canonical-sol-spl-exit-v1` is the retained compatibility identifier for
direct exact-quote buys and exits in canonical standard-SPL pools after graduation.
It derives the canonical index-zero pool from Pump's
pool-authority PDA, verifies completed curve state again in the pool snapshot,
checks the pool's identity, mints, liquidity vaults, global configuration and fee
recipients, and rejects stale responses. Missing canonical pools,
non-SOL quotes, pools requiring an account upgrade, a nonpositive effective quote
reserve, and mayhem/cashback remain unsupported. Pricing uses the raw quote-vault
balance plus the pool's signed virtual quote reserve. Creator and holder rewards, including
explicitly decoded creator-fee settings, use the same reviewed exit path.

The corresponding Token-2022 routes are
`pump-v2-native-sol-token2022-metadata-v1` and
`pumpswap-canonical-sol-token2022-metadata-exit-v1`. They require six decimals,
revoked mint/freeze authorities, exactly MetadataPointer and TokenMetadata mint
extensions, revoked metadata/pointer authorities, and a pointer/metadata identity
matching the mint. Token accounts may have no extensions or ImmutableOwner only.
Malformed, duplicate, truncated and unknown extensions are rejected, as are
transfer fees, transfer hooks, permanent delegates, default frozen states,
confidential transfers, interest/scaled amounts, pausing, close authority and
non-transferable tokens. This is a subset of Token-2022, not general support.

Base-token account addresses include the actual token program. After discovering
a Token-2022 mint, the service rereads the complete account set at the same or a
later slot with the correct ATA. Quote currency remains standard wrapped SOL.
The mint/account policy is checked again before signing and on simulated token
accounts. Finalized Token-2022 balances must explicitly identify the token program.
Existing standard-SPL journal entries retain their original interpretation.
Metadata strings are validated as data; their URIs are never fetched by trading.

PumpSwap uses wrapped SOL. This route requires the wallet's wrapped-SOL ATA to be
absent and creates it with a **non-idempotent** SPL ATA instruction. A buy transfers
the exact reviewed quote cap into that account, synchronizes it, calls
`buy_exact_quote_in`, and closes the account so any rounding residue returns to the
same wallet. A sell receives its proceeds there and closes it to that same wallet.
Non-idempotent creation enforces the absence requirement on chain even if another
transaction creates it after review. An existing wrapped-SOL account is never spent
or adopted. Simulation and finalized receipts must also prove the account was closed.

The review identifies the venue. If a curve graduates after a quote is reviewed,
the old preview cannot sign: request a new quote and review the PumpSwap trade.
The same partial/full position accounting and wallet reservation apply to both routes.

Mainnet and devnet are explicitly selectable; localnet is available to the test
service. Fixed official RPC endpoints are the default. Optional `SOLANA_RPC_URL`
and `SOLANA_DEVNET_RPC_URL` environment variables select HTTPS providers; endpoints
are not imported from strategies. Each connection verifies the selected genesis.

Buy amounts are exact lamports, capped at 1 SOL per order. Sell amounts are exact
six-decimal token quantities taken from a finalized manual position. The initial
version accepts 1–500 basis points of slippage, has a 60-second preview lifetime,
a 100,000-lamport network-fee ceiling and a 0.02 SOL account-funding allowance.
Public RPC limits can prevent a quote; they never authorize a stale quote.

## Quote and authorization

The app derives all program, curve, creator-vault, associated-token and fee-config
addresses locally. Protocol recipients come from the owner/discriminator-checked
Pump global account or PumpSwap global configuration. The curve route constructs
only a compute-unit limit, an idempotent user ATA creation for buys, and one pinned
Pump instruction. The migration route adds the narrowly scoped wrapped-SOL
funding/synchronization for buys and creation/close described above. No externally constructed transaction,
approval/delegation or arbitrary transfer is accepted. `solders==0.29.0` provides
Solana address/message/signature primitives.

A coherent `getMultipleAccounts` response supplies the account snapshot. Two
unsigned simulations obtain an executable estimate and check the final slippage
instruction. Account reads and simulation carry context slots; current curve state,
wallet balances, route accounts, reward mode, creator-fee settings, network fee,
preview age and blockhash expiry are checked
again before signing. The reviewed message cannot be silently replaced. Cancelling
either dialog sends nothing.

For a buy, the exact quote input includes Pump's trading fees. Wallet debit also
includes the network fee and newly funded accounts. For a sell, the instruction's
minimum is the route's SOL payout; **network fees and account funding reduce net
wallet proceeds**. Review separately shows the simulated wallet change and a
conservative net floor using the full funding allowance. A negative floor means
account costs could exceed proceeds; it is not presented as a positive net return.

## Durable execution and recovery

The existing `wallet-transfers.sqlite` journal now also stores public trade previews,
orders and exact manual positions. A shared unique wallet/network reservation
coordinates trades and native withdrawals across current-version app processes.
Dev31 also protects [per-agent resource holds](AGENT-RESERVATIONS.md): manual
operations must leave the reserved SOL and position quantities available. Review
and atomic submission both enforce this, including holds added after review.
The signed message's signature is journaled before broadcast. Private keys,
passwords and broadcastable signed transaction payloads are never stored there;
the unsigned preview message and blockhash are replaced with a message digest when the signature is journaled. That digest validates transaction receipts.

RPC acceptance is `pending`, timeout is `unknown`, and confirmation is `confirmed`.
No position is invented from a recommendation, simulation or successful submission.
Finalized `getTransaction` evidence must contain the identical signed message,
signature, payer, mint, token-owner/decimal identities and bounded wallet/token
changes. Position updates, final order state and reservation release commit in one
SQLite transaction. Replaying a receipt cannot apply the same fill twice.

On restart the desktop checks unresolved signatures without unlocking/signing again.
The Trade window also reconciles while open. Missing receipts and missing signatures
after blockhash expiry remain unresolved and reserve the wallet; expiry alone does
not prove nonexecution. There is no automatic re-sign, replacement transaction,
blind retry or operator override. Finalized failed transactions retain their fee and
create no position. Resolve existing operations before restoring another wallet.

Partial sells allocate the remaining cash cost using integer arithmetic; the final
sale receives the entire remainder. Holdings below the sum of recorded allocations
block further trades in that mint as an external balance discrepancy. Tokens owned
before the app's confirmed buy are not silently adopted. Manual positions appear in
the sidebar and the Trade window, and remain separate from agent-managed external
fill records. Dev33's [reviewed agent adapter](AGENT-EXECUTION.md) can attribute
explicitly authorized orders and finalized positions to an agent. Ordinary manual
positions are not automatically assigned, and no unattended signer is enabled.

Cash accounting is denominated in SOL. Costs include **net wallet cash debit,
network fees and account funding**; realized results are cash-based and do not treat
recoverable account rent as a separate asset. Failed-order fees remain visible on
those orders. No USD price, aggregate investment return or mark-to-market is invented.

## Evidence and remaining release gate

The protocol interface is pinned to official Pump documentation commit
`81091419e4457566469d4e2a27f64ed84d42419c` (September 14, 2026); the bundled interface
subset records its source and original IDL SHA-256.

- [Official Pump IDL at the pinned revision](https://github.com/pump-fun/pump-public-docs/blob/81091419e4457566469d4e2a27f64ed84d42419c/idl/pump.json)
- [Unified native-SOL interface](https://github.com/pump-fun/pump-public-docs/blob/81091419e4457566469d4e2a27f64ed84d42419c/README.md)
- [Solana unsigned simulation](https://solana.com/docs/rpc/http/simulatetransaction)
- [Solana transaction receipts](https://solana.com/docs/rpc/http/gettransaction)

`tests/test_manual_trades.py` covers instruction identity, exact units, buy/partial/full
sell, atomic receipt replay, restart, duplicate submission, wallet reservations,
expiry, simulation failure, changed accounts/fees, graduation during review, external
balance deficits, malformed receipts and finalized failure fees. Raw account fixtures
are explicitly synthetic. An independent offline fixture from `@pump-fun/pump-sdk@2.0.0`
compares instruction bytes, every account address, and signer/write flags for both sides;
`packaging/capture_pump_sdk_fixture.cjs` regenerates it without RPC or keys. `tests/check_manual_trade_ui.py` exercises cancel, review,
password authorization, the position sidebar and reopening history on an isolated
display with throwaway keys, synthetic RPC, and real connections blocked. It now
graduates the token between buy and partial/full PumpSwap exits.

`tests/test_pump_swap_route.py` covers exact-quote buy contracts, direct buy and
partial/full exit accounting, signed virtual reserves, migration lifecycle,
restart, canonical pool rejection, existing wrapped SOL, disabled/unsupported
pools and unwrapping receipt mismatch. `packaging/capture_pump_swap_sdk_fixture.cjs`
captures buy and sell instructions from `@pump-fun/pump-swap-sdk@1.20.0` with
networking disabled. The one intentional
SDK wrapper change uses the official SPL non-idempotent ATA builder to enforce
the absent-wrapped-SOL requirement atomically. The swap itself, remaining accounts,
and close instruction match the SDK. The PumpSwap IDL is pinned to the same official
documentation revision as Pump.

No funded mainnet swap has been performed or certified by this implementation.
Fixtures and UI tests do not establish deployed-route availability. A separately
approved, bounded chain acceptance exercise remains necessary before enabling
automatic trading or claiming live-route readiness. See [route verification](ROUTE-VERIFICATION.md)
for the real account-inspection report and remaining simulation/acceptance gaps.
