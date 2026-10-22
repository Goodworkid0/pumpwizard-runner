# Creator and holder rewards — source integration, September 25, 2026

The user requested support for both fee destinations after a holder-reward token
was rejected during an unsigned browser review. The previous blanket rejection
has been removed for the existing native-SOL routes. Version remains
`0.2.0.dev60`; this is a source update, not a published release.

## Contract and implementation

Pump's [holder-reward contract documentation](https://github.com/pump-fun/pump-public-docs/blob/81091419e4457566469d4e2a27f64ed84d42419c/docs/HOLDER_REWARDS_README.md)
states that trade instructions, account ordering and arguments are unchanged.
The on-chain curve creator / pool coin creator supplies the fee destination,
including the holder-rewards address. Existing local creator-vault derivations
therefore continue to apply. Pump distributes holder rewards; this integration
does not create reward payments, claim them, or count them as trading proceeds.
The verified current official documentation revision matches the pinned IDLs.

- Active-curve buys and sells support creator and holder rewards for standard
  SPL and the existing metadata-only Token-2022 subset.
- Canonical PumpSwap exits from recorded positions support both reward modes.
  New buys after graduation are still outside this route.
- Decoders read creator-fee bps, edit permission and reward mode explicitly.
  Curve layouts of 115 and 124 bytes default only their absent trailing fields.
  Partial fee fields, invalid boolean bytes and fee values above 10,000 bps fail.
  PumpSwap's existing minimum 300-byte pool requirement remains.
- Nonzero creator-fee fields and edit permission no longer trigger a blanket
  rejection. Fees are quoted through actual unsigned simulation, not inferred
  from that field alone. No new non-SOL/custom-quote route was added.
- The browser review names the fee destination. Reward-mode, fee-bps or edit-flag
  changes require a fresh review before signing/broadcast. Destination-account
  changes remain caught by exact message reconstruction. Old retained reviews
  without the newly captured settings also require a fresh review.
- Existing mint, balance, slippage, fee/account-funding, reservation, signature,
  receipt and restart checks apply. Mayhem, cashback, unsupported Token-2022
  extensions and nonzero PumpSwap virtual reserves remain outside support.

## Validation

The full Python run passed 685 tests and skipped one; four localhost API tests
were blocked by sandbox socket permissions. Those same four passed with local
socket access: **689 distinct tests passed, one skipped**. Browser lint,
TypeScript/Vite production build and **16 Playwright flows** passed.

New cases cover both token programs and reward modes, standard and nonzero
editable fee fields, buy → partial sell → journal restart → full sell on both
curve and migrated routes, malformed fields, legacy layouts, changed fee
settings before signing, and already-signed browser messages rejected before
broadcast when settings change. Browser tests exercise both reward destinations,
receipt recovery through an actual Runner process restart, and a final migrated
exit. All signed test transactions use synthetic banks.

Offline official `@pump-fun/pump-sdk@2.0.0` and
`@pump-fun/pump-swap-sdk@1.20.0` fixtures compare holder-mode buy/sell instruction
bytes, accounts and privileges for both token programs. Fixtures use the
official SDK's `holderRewardsPda(mint)` and the recorded on-chain creator input;
no network or signer is available to fixture generation. Reproduce with
`PUMP_HOLDER_REWARD=1` and optionally `PUMP_TOKEN_2022=1` in the existing
`packaging/capture_pump*_sdk_fixture.cjs` scripts, using an isolated SDK install.

The exact reported mint `HJAsy4WTRxq7vMnThKj9MVPg5kJKvnedWGzEX7U3oZeq`
passed a deployed unsigned curve probe:
0.001 SOL buy simulations at slots 450346538 and 450346539, then an unsigned
buy plus 90% partial sell at slot 450346541. All returned null instruction errors;
the combined probe consumed 180,728 compute units within the 400,000 limit.
The public sender came from earlier public-account evidence; no control of that
address is claimed and the user's Phantom wallet was not accessed. Account costs
and outputs from this public sender are historical evidence, not a current quote
for the user. The diagnostic one-lamport sell floor is never offered for signing;
normal reviews retain the configured slippage floor.

The public holder-reward PumpSwap sample
passed account validation at slot 450347649. Public RPC requests for transaction
history and then largest token accounts were unavailable/rejected, so no live
holder-reward PumpSwap sell simulation is claimed. This exit variant has SDK
instruction and synthetic lifecycle coverage; the deployed sell check remains
an evidence gap at that checkpoint. A subsequent user-authorized Helius connection
resolved the blocked public reads: that evidence file now records successful
unsigned holder-reward PumpSwap sells at slots 450355139 and 450355140. See the
[connection and signing follow-up](WALLET-SIGNING-COMPATIBILITY.md). This does not
establish funded settlement.

No funded transaction was submitted. Separate real buy/sell settlement across
restart remains a funded acceptance step, with each transaction reviewed and
approved by the user in their wallet.

## Running local source

The local source Runner was restarted after confirming empty trade, transfer
and strategy-order journals, with database backup
`/tmp/pw-source-start-backup-s8b432hq`. Current assets are `index-DRcFcF9n.js`
and `index-0HAjEZEm.css`. Refresh the browser and reconnect Phantom to request
a fresh review. The review screenshot
uses synthetic test funds. The packaged launcher is unchanged; no version bump,
Community publication or website edit was made.
