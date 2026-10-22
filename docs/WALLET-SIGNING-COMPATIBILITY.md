# Wallet signing compatibility — September 25, 2026

## Latest acceptance: real Firefox Phantom buy finalized

The user subsequently approved a 0.01 SOL buy of a different active-curve token.
The retained finalized receipt
confirms 352,986.344279 tokens received, an 11,518,840-lamport wallet debit and a
5,000-lamport transaction fee at slot 450369639. This establishes real Firefox
Phantom signing and buy settlement after the explicit-price correction. The
local journal contains one finalized trade and passes its integrity check.
The debit includes more than the network fee; the summary does not attribute the
remaining account/protocol effects beyond the validated total.

The screenshot initially showed pending, then a finalized receipt while the top
message stayed pending. [Order and chart UI changes](ORDER-AND-CHART-UI.md) now
derive that banner from the current receipt and remove duplicate recovery text.
Funded partial sell, restart recovery and full sell remain unverified. No further
funded order was signed or submitted by this session.

## Earlier blocker: Firefox Phantom automatically added a priority fee

The earlier user-approved 0.0064 SOL signing attempt produced an exact unsigned
comparison. The sanitized diagnostic
shows a legacy transaction growing from 1,070 bytes / 3 instructions to 1,082
bytes / 4 instructions. Account ordering, privileges, blockhash and all original
instructions were unchanged. The one additional instruction was
`SetComputeUnitPrice(375000)` (`03d8b8050000000000`), adding 150,000 lamports
(0.00015 SOL) at the reviewed 400,000 compute-unit limit. The browser verified the
signature but rejected the changed message before submission. No Lighthouse
instruction or v0 conversion was present in this response. The user confirmed
they made no manual fee change in Firefox Phantom.

Phantom documents [automatic priority-fee insertion](https://docs.phantom.com/developer-powertools/solana-priority-fees).
Runner previously encoded a compute-unit limit but omitted the price, while
reviewing zero priority fee. Both curve and canonical PumpSwap exit builders now
include **an explicit zero compute-unit price before simulation and review**.
The review still shows zero priority fee; normal network fees still apply to a
submitted transaction. Server and browser exact-message guards remain unchanged.
If Phantom changes the explicit price, Runner still rejects it. No signed
transaction is repaired or accepted with an unreviewed fee.

The reviewed-order UI now offers **Check wallet compatibility** for extensions.
It invokes signing once, strips every signature in the browser, and sends only
the unsigned message and bounded format metadata to an authenticated local
diagnostic endpoint. That endpoint has no RPC or submission capability. It binds
the diagnostic to the connected wallet's unsubmitted review, compares legacy/v0
messages and instruction details, and atomically saves a mode-0600
`wallet-signing-diagnostic.json` in the data directory. Neither the signed wire
transaction nor its signatures are saved or sent to the diagnostic endpoint.
The report is browser-reported evidence, never an execution authorization.

Normal signing failures now also save this unsigned comparison automatically.
The new diagnostic names version, byte/instruction counts, and changed Lighthouse
or compute-budget instructions where present. Invalid wallet signatures and
unreadable responses remain rejected. Exact-message submission guards remain.

Validation after the explicit-price change: **254 focused Python tests**,
browser lint/build and **26 Playwright tests** passed. The synthetic extension
now models the observed fee insertion when price is absent: the independent SDK
test reproduces the exact 12-byte addition and preserves an explicit zero. Buy,
partial sell and migrated full sell pass through that extension; a valid
signature changing zero to 375,000 is rejected by browser and backend before any
submission. Reward lifecycle tests verify the budget instructions on every buy
and exit across both token programs, both reward modes and restart. Diagnostic
tests still verify no submission, no persisted signatures and unchanged balances.

The unsigned Mainnet recheck
of `k1jnShqQ15xBupKaQGKz6CHkcWc8zCi3g9ReBeWpump` stopped at account validation:
the curve had graduated, so this mint no longer supports new buys in Runner.
No simulation ran for that probe, no transaction was signed or sent, and no SOL
was spent. This is not live acceptance of the new fee instruction.

The source Runner was restarted with database backup
`/tmp/pw-source-start-backup-whqb9uwr`. Assets remain `index-BAIvM2FS.js` and
`index-C9y9FSPC.css`; this change is in the Python builders. Post-restart
diagnostics show empty order/transfer journals and valid integrity.

At this checkpoint, a no-send compatibility check was requested before another
funded attempt. The later user-initiated finalized buy above supersedes the
pending Firefox acceptance status. The compatibility check remains available
for future mismatches and never submits. Helius remains configured and verified.

## Earlier account-ordering checkpoint

The user reported `Browser wallet did not sign the exact reviewed transaction`
after approving a 0.001 SOL buy in Phantom. That rejection occurs before
reservation and broadcast. Read-only inspection confirmed one retained unsigned
review, zero trades and zero wallet intents. The UI's generic "check submission
status" help was misleading for this rejection: Runner had sent no transaction.

## Reproduced mismatch and correction

The saved unsigned review used Rust/Solders account ordering. Rebuilding it with
`@solana/web3.js@1.99.0` changed the account table and compiled indices, despite
preserving the instruction meaning. Simply deserializing/reserializing without
rebuilding preserved its bytes. The SDK's
[legacy compiler](https://raw.githubusercontent.com/solana-labs/solana-web3.js/maintenance/v1.x/src/transaction/legacy.ts)
sorts equal-privilege keys with English, lowercase-first string collation.

`solana_message.wallet_message` now applies that ordering to both curve and
canonical PumpSwap builders **before** quoting, unsigned simulation, fee review
or wallet signing. It retains the fee payer, privilege groups, blockhash, program
IDs, instruction data, instruction order and ordered/repeated instruction
accounts. The retained reviewed message now survives a wallet's SDK rebuild.
Existing unsigned reviews must be refreshed. Existing receipt hashes are not
rewritten, and strict server signature/message comparisons are unchanged.

The browser takes an independent copy before calling the wallet, then verifies
the returned message and Ed25519 signature before making any submission request.
In-place input mutations, changed contents, format changes and bad signatures
fail with **Order was not submitted**. The user must request a fresh review.
There is no automatic re-signing, repair of signed messages, additional signer,
fee increase or automatic transaction retry.

The original rejected signed response was not persisted, so the exact change
made by that particular Phantom response is not established. The ordering
compatibility defect is reproducible and corrected; real Phantom acceptance
still requires a new user-approved attempt. Future changed wallet responses
produce a specific pre-submission failure instead of an ambiguous outcome.

## Verification

- **251 distinct focused Python tests passed**, including SDK instruction
  fixtures, both token programs and reward modes, receipt/restart paths,
  strategy orders, and the new message-layout cases.
- Eight independent offline web3.js message fixtures cover case collation,
  multiple privilege groups, repeated accounts and idempotence. Regenerate with
  `PUMP_SDK_NODE_MODULES=... node packaging/capture_wallet_message_fixture.cjs`.
- Browser lint/build passed. **21 distinct Playwright flows passed** across the
  full run and a focused rerun: the new SDK lifecycle test initially finalized
  its synthetic sell before submission finished; adding the missing pending
  receipt wait fixed the test. The other 20 flows passed in the full run.
- The synthetic extension now rebuilds its messages using the actual pinned
  JavaScript SDK before signing. The new lifecycle covers buy, partial sell and
  migrated full sell with finalized receipts. Three rejection cases verify zero
  submission requests for changed messages, in-place mutations and bad signatures.
- Independent backend tests still reject valid signatures over changed account
  ordering, extra priority-fee instructions and changed blockhashes before any
  reservation or broadcast.

The SDK is a development-only browser test dependency; it is not imported by
the production browser app. Its `rpc-websockets` dependency is pinned to 9.2.0
for the current local Node 20.12 CommonJS test runner. CI uses Node 22. The local
install reports engine warnings and five moderate development-dependency audit
findings (SDK RPC/JSON/UUID utilities); these offline tests only rebuild synthetic
messages and do not instantiate RPC clients. No production dependency was added.

## Helius and deployed unsigned checks

The user supplied a Helius key. It was entered with terminal echo disabled and
saved through the local RPC API after successful Mainnet identity/blockhash
checks. The saved local settings file is mode 0600. No key or endpoint URL is
included in repository evidence or diagnostics. Helius is now the active saved
browser connection; the previous "private endpoint pending" status is obsolete.

The screenshot mint's 0.001 SOL buy
passed twice in unsigned simulation at slot 450356048 using the corrected
message. The reported fee was 5,000 lamports. This is historical probe evidence,
not a current executable quote or a funded fill.

The previously blocked holder-reward PumpSwap exit
also passed unsigned simulation at slots 450355139 and 450355140 through Helius.
Its sender was found in public token-account data, not the user's wallet. No
control of that sender or funded sell is claimed. Public RPC reads and unsigned
simulation spent no SOL. Real buy → partial sell → restart → full sell acceptance
remains pending.

Version stays `0.2.0.dev60`. This is source work, with no packaged release,
Community publication, website change or automatic signing activation.

The local source Runner was restarted with backup
`/tmp/pw-source-start-backup-s4k4f7fu`. Served assets are `index-BsQN9uM0.js` and
`index-C9y9FSPC.css`. Post-restart diagnostics show empty trade/transfer/agent
journals and valid database integrity. The live connection view
confirms Helius is saved without exposing its URL. Refresh the browser and
reconnect Phantom before requesting the next review.
