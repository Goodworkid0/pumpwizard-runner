# Browser wallets, market selection and reviewed trading

## Recovery guidance — dev92

**Advanced recovery — address doesn’t match?** is collapsed initially. Use the
default account and address format first. The account selector shows **Account 1**
through **Account 100**, corresponding to the existing internal indices 0–99.
The address review shows that friendly account number; its raw derivation path
is inside a separate collapsed **Advanced recovery details** section. The optional
recovery passphrase must be the original BIP39 passphrase; the PumpWizard encryption
password is entered later. These presentation changes retain the existing derivation,
server account storage and ownership checks.

## Private-key and recovery-phrase sign-in — dev91

Choose **Private key or recovery phrase** on the wallet sign-in screen. Private
keys must be a complete 64-byte Solana keypair in base58 or a JSON byte array.
Public addresses, bare 32-byte seeds and inconsistent keypair exports are rejected.

Recovery phrases use the English BIP39 wordlist and checksum (12, 15, 18, 21 or
24 words). The default path is `m/44'/501'/0'/0'`. Recovery options let you choose
account indices 0–99, `m/44'/501'/account'`, or legacy `m/44'/501'`, with an optional
BIP39 recovery passphrase. Unsupported languages and derivation schemes are not
automatically guessed or scanned. The public address appears for review before
anything is saved. If it differs from your original wallet, return to secret entry
and select the original recovery options, or import that wallet's private key.

A new account requires a password of at least ten characters. The browser performs
transient parsing, derivation, encryption and ownership-message signing; the local
server receives only the existing password-encrypted wallet envelope plus public
ownership-proof fields. Neither the phrase nor its recovery passphrase is sent to
the account API. Secret input fields are cleared after address review and cancellation;
owned key buffers are wiped when cancelled, failed or disconnected. JavaScript
strings and platform crypto internals cannot be guaranteed to be zeroized.

Signing in with a key for an already saved address keeps the original encrypted
record and password. New accounts save before authentication, so a failed ownership
or balance check can leave a successfully saved encrypted account available for
password unlock. Persistence errors do not fall back to browser storage. Reload
restores only an authorized public view with signing locked; no service enrollment,
allowance, transaction or automatic trading starts through this sign-in flow.

Cryptography references: [BIP39 implementation](https://github.com/paulmillr/scure-bip39/tree/1.6.0),
[SLIP-0010 standard and Ed25519 vectors](https://github.com/satoshilabs/slips/blob/master/slip-0010.md),
and [Trezor BIP39 vectors](https://github.com/trezor/python-mnemonic/blob/master/vectors.json).

## Saved account storage — dev90

Generated wallets are now password-encrypted records in `app-state.sqlite` in
Runner's local data directory. The server owns saved accounts, public wallet-view
metadata, workspace preferences, worth snapshots and operation retry identities.
A new browser profile or a different loopback port reads the same saved accounts
when connected to that same local data directory. No app account or preference
is written to browser localStorage/sessionStorage. The existing HttpOnly launch
cookie still authenticates requests to the local API.

The browser reads legacy storage once for migration. An authenticated migration
merges validated encrypted backups atomically and keeps existing server preferences.
It removes legacy values only after the server commits, and only when those values
still match the migrated copy. Invalid or conflicting records produce a visible
error and remain available for recovery. The startup screen can retry migration or
continue with accounts already saved in the app without deleting the legacy copy.
If an old wallet was created at another address, first open that original address
with this build so its legacy browser copy can migrate, or restore its encrypted
backup. The app cannot read another origin's browser storage automatically.

The existing offline service backup includes this private account database.
Passwords and decrypted keys remain transient; seeing an account is not wallet
ownership or signing authority. Restored public view metadata must pass the existing
backend authorization check, and signing requires a fresh password unlock or
extension connection. If authorization expired or a service restart invalidated it,
choose the saved account and sign in again. Server account storage does not enroll
a service vault, start an allowance, or enable automatic execution.

The sections below retain historical UI and custody details. References to wallets
"saved in this browser" or tab-only records describe the pre-dev90 implementation;
server-owned account storage above supersedes those persistence details.

Latest unreleased source status: browser source expansion.
It supersedes older capability limits below for strategy reviews, withdrawals,
automation-vault preparation, pool charts and monitor health. Funded acceptance
and unattended signing remain blocked.

Dev61 source preparation; the current source version is `0.2.0.dev61`.

The browser app starts with one wallet-selection screen for Solana Mainnet. It
shows every encrypted PumpWizard wallet saved in this browser beside every
compatible registered extension, including an extension account that is already
connected. Choosing a saved wallet opens its password form; choosing an
extension requests its ownership proof. Discovery never selects a wallet by
itself.

After ownership proof, PumpWizard Terminal shows a market wizard on every
sign-in. Pump.fun opens the supported live discovery and reviewed execution
workspace. Raydium CPMM is visible as a planned market but cannot be selected;
Runner does not yet have an accepted Raydium trading route. The choice is kept
only for the current tab session. A page refresh restores that wallet identity
and market while the existing 30-minute Python authorization remains valid.
The tab record contains only the wallet kind, name, public address, network and
market. It contains no password, private key or transaction-signing capability.
After a refresh the workspace is immediately usable for viewing, but signing is
locked until the generated-wallet password is entered again or the extension is
reconnected. **Lock wallet** clears this tab record and revokes the Python
authorization, so it still returns to wallet selection.
After market selection, the signed-in app has two primary workspaces:

- **Dashboard** for Open Positions, session-only entry observations and
  wallet-reviewed buy or sell preparation, with Active Sessions in the sidebar;
- **Portfolio** for current token holdings, exact mints/project links, purchase
  attribution, estimated USD value, inline trend/market-quality state,
  source-specific Sell actions and expandable historical price charts.

Portfolio combines confirmed wallet balances with Runner ownership records.
Named strategies, manual PumpWizard trades and outside-app tokens remain
distinct even when they share a mint. Each source opens the reviewed sell form;
outside-app tokens are limited to atoms above all Runner positions and do not
receive a fabricated cost basis.

Dashboard, Portfolio, Deposit, Withdraw and Lock remain in the full-height
wallet panel. Portfolio has no generic Wallet tools menu. Read-only database,
RPC, wallet, market and session startup checks run in the Agent Monitor; RPC
configuration appears there only if its initial sync test fails.
Offline evaluation, virtual replay and research evidence are no longer primary
browser navigation destinations.

## Token and project details

The general token explorer is not shown in the signed-in dashboard. Token
detail opens from an actual open position or a saved session entry observation.
Price, market-cap and liquidity charts use snapshots already retained in the
local Runner database; opening the detail view does not make another market
request. **Load earlier history** pages through the rest of those local records
in batches of up to 500. The chart reduces rendered SVG points as more pages are
loaded, while its period values continue to use the full loaded series.

While the visible Dashboard workspace is open, the browser requests a bounded
public Pump.fun scan every 20 seconds and rereads the local database every 5
seconds. Hidden tabs pause both timers and refresh on return. The Python service
allows at most one provider scan per 20 seconds for a data directory, so several
tabs cannot multiply public requests.

Live scans replace the latest point inside each five-minute bucket. After seven
days the local library keeps one point per hour, and after 90 days one point per
day. This preserves a useful long-range series without growing at raw polling
frequency. It is retained observation history, not every trade or an exchange
candle archive.

PumpWizard cannot derive observations from before it began watching a mint from
the current public listing response. Opening token detail therefore discovers
the token's leading tracked Solana pool and automatically loads attributed
4-hour USD candles from the keyless GeckoTerminal public service. There is no
account, credential or provider setup. Runner imports bounded 1,000-row pages,
persists the cursor and continues automatically while token detail remains open.
It can resume after a public-data error or application restart.

Historical public candles live in their own research-only table. They extend
the price chart before recent local observations and never supply market cap,
liquidity, execution quotes, route evidence or risk checks. Coverage may start
after token creation or change when the leading pool changes. The UI shows the
retained row count, loading error and earliest coverage instead of calling it
complete onchain history.

Exact trade-by-trade history still requires paginating archival Solana
transactions and decoding both Pump curve and PumpSwap activity, with historical
SOL/USD conversion and route-aware deduplication. That canonical transaction
decoder is not part of dev61.

The bounded Pump.fun and PumpSwap reads also retain a sanitized subset of their
public project record: name, description, creator address, verification flag,
supported image and HTTPS website, X or Telegram links when supplied. Portfolio
adds one bounded Jupiter Tokens batch for exact held mints to fill identity gaps
and display price, liquidity, holder and market-quality context. Exact-mint
lookups merge missing fields without replacing Pump's supplied identity.
Runner does not request the token's arbitrary metadata URI. The Portfolio image
is fetched only by the loopback proxy from a fixed provider/CDN host allowlist,
must pass raster magic-byte and 1 MB checks and is never loaded directly by the
browser. These fields are descriptive and never become trade-route or policy
input. Token creators can supply inaccurate content, so the exact mint remains
visible and copyable.

## Connect an existing wallet

Runner discovers wallets through Wallet Standard and requires
`standard:connect`, `solana:signMessage` and `solana:signTransaction`. Phantom,
Backpack, Solflare and other compatible wallets can therefore connect without a
wallet-specific adapter.

The selection screen lists every compatible wallet currently registered in the
browser, including its supplied icon and whether it already exposes a Mainnet
account. Registrations and unregistrations update the list automatically.
**Scan again** rereads the local Wallet Standard registry after the user
installs, enables or unlocks an extension. Choosing a wallet remains an explicit
action so discovery never opens a connection prompt by itself.

Runner issues a single-use Sign In With Solana style message bound to the
same-origin loopback site, public address, selected network, nonce, issue time
and five-minute expiry. A valid signature authorizes that address/network in
the Python process for 30 minutes or until **Lock wallet**. The extension owns its key
and prompts for transaction signatures; Runner receives only the public address
and signatures.

## Create an encrypted browser wallet

The create flow generates a Solana Ed25519 keypair in the browser. It derives a
256-bit AES-GCM key from the password with PBKDF2-SHA-256 and 310,000 iterations,
then stores only the ciphertext, salt, IV, public address and creation time in
local storage. The temporary creation key is cleared after encryption. Plain
secret bytes return to browser memory only when that wallet is explicitly
unlocked for a signed-in session and are never included in an API request.

After creation, the user must acknowledge saving the encrypted JSON backup. The
flow then returns to wallet selection instead of automatically signing in with
the new wallet, so Phantom or another listed wallet can be chosen immediately.
**Restore encrypted backup** validates and adds that format to the chooser;
**Unlock & sign in** decrypts the selected vault, verifies the derived public
address and signs a new ownership challenge. **Back to wallet selection** leaves
the vault locked. **Lock** invalidates the active signing capability, clears its
secret byte buffer and returns to the chooser.

Reloading the page also destroys the in-memory secret byte buffer, but it does
not discard the safe tab session described above. The terminal resumes in a
view-capable, signing-locked state. **Unlock signing** decrypts the stored vault
again after password entry; a trade or withdrawal action opens the same prompt
and continues that requested action after successful unlock.

The password cannot be recovered. Clearing browser site data without a usable
backup loses the locally created wallets. An attacker who copies a backup can
attempt password guesses offline, so each wallet password needs high entropy.
The browser collection stores up to 20 encrypted generated wallets. Existing
single-wallet `pumpwizard.browser-wallet/v1` storage remains readable and is
migrated into the collection when another wallet is created or restored.

## Balance and deposits

After sign-in, the dashboard shows the current RPC balance for the selected
network. **Deposit** shows:

- the full public address;
- a copy control and `solana:<address>` QR code;
- the selected Solana network and wallet type; and
- an explicit live balance refresh.

The QR code and address are receive instructions. Runner does not submit a
deposit transaction or index complete deposit history. Only Solana assets on
the selected network should be sent to that address.

A locally created account is labelled **PumpWizard Generated Wallet**. Its
**Export key** action requires the wallet password again before revealing a
base58 private key or downloading the same 64-byte keypair as a Solana CLI JSON
file. Password verification and decryption happen in browser code; neither the
password nor plaintext key is sent to Python. The plaintext export must be
stored securely because anyone holding it controls the wallet. Wallet Standard
extensions keep their keys inside the extension and do not show this action.

## Manual buy and sell flow

1. Sign in, choose Pump.fun and use **New order** in the Trade workspace or
   **Buy or sell** from an opened token.
2. For a buy, enter a supported mint, exact SOL spend and 1–500 basis points of
   slippage. For a sell, select a finalized local position and optionally reduce
   its quantity for a partial exit.
3. **Review order** runs the existing Python Pump/PumpSwap account
   reads, route validation and unsigned simulation. The review shows amount,
   minimum route output, estimated network fee, configured priority fee (zero),
   wallet-change estimate, signer address/type, route, network and expiry countdown.
4. **Sign & submit** asks the active wallet to sign only that serialized
   transaction. An extension opens its approval prompt; a created browser wallet
   signs from the decrypted in-memory key.
5. Python reconstructs the expected message, repeats route, balance, fee,
   blockhash, risk and reservation checks, verifies the signature over the exact
   reviewed bytes, records it under the wallet lock, and broadcasts it.
6. Trade's **Refresh status** and automatic recovery follow the original
   signature to finality. Portfolio's **Refresh holdings** also completes a
   receipt pass before rereading balances and attribution. Recovery runs on sign-in, return to
   the visible tab and every eight seconds while unresolved. Only a validated
   finalized receipt changes a position. An uncertain submission remains visibly
   unknown and reserved; it is never silently replaced or re-signed.

An account/network change, wallet lock, changed position/balance or expired
quote invalidates a review. The browser checks again after the wallet prompt;
Python revalidates before broadcast and checks expiry inside the wallet lock.
Wallet rejection discards the review. Generated-wallet approval signs here;
an installed extension asks for its own per-transaction approval.

Runner retains exact receipt fees and wallet/token deltas, mainnet signatures,
remaining basis, net sale proceeds and realized SOL P/L. Portfolio intentionally
does not render historical accounting or closed holdings; current order receipts
remain visible in Trade's order ticket. Explorer links never supply accounting
evidence.

The route and token limits remain those in [manual trades](MANUAL-TRADES.md).
A review expires after 60 seconds. Mainnet uses real SOL. The browser cannot
supply an arbitrary program, recipient, route, RPC URL or extra signer.

## Main wallet and strategy allocations

**Add session** now asks for the session mode first. A **Monitoring** session
collects learning preferences but reserves no SOL and has no order authority. A
**Trade** session next asks for **Interactive** or **Autopilot**, then shows the
live main balance, existing assignments and remaining balance before collecting
position-size, time-horizon, risk and minimum reward-to-risk preferences.
Abandoning the wizard writes nothing. Final Trade creation rechecks the live
balance and durably creates the local reservation, risk policy and immutable
permission manifest together with the disabled/manual session. Monitoring and
execution remain off, and creation does not sign or submit a transaction.

Interactive sessions may prepare exact reviewed orders, but the connected main
wallet must sign every entry and exit. Autopilot setup records requested behavior
only. It does not enable unattended execution: the isolated Automation Wallet
must still be backed up, funded, explicitly authorized and started separately.
The optional **Winner handoff** policy permits bounded automatic entries and
protective exits after authorization while leaving profitable exits to the user;
it is not offered as an Interactive capability because that mode has no
unattended signer.

**Trade → strategy actions → Order safety** can later inspect or edit those
limits. The signed-in main wallet's spendable SOL is divided into local cash
reservations for individual strategy sessions. An allocation does not create a
new address or submit an on-chain transfer. Other local strategies, manual buys
and withdrawals cannot spend SOL covered by an active reservation.

Each strategy policy controls reviewed buys, managed strategy sells, minimum and
maximum buy size, total buys and order count per UTC day, daily realized loss in
SOL and as a percentage of the assigned amount, maximum loss per trade, and open
position count. The effective daily loss stop is the lower of the SOL and
percentage limits. The maximum per-trade loss becomes the executable-net stop
for a managed position.

Python applies the policy during candidate assessment, reviewed-order creation
and final submission validation. Editing an allocation is blocked while that
strategy has an active reviewed/submitted order or signing session. The total
cash allocations cannot exceed the live main-wallet balance. Managed token
positions stay reserved to their strategy until the reviewed handoff releases a
position for manual exit. The browser provides that handoff for both main-wallet
reviewed exits and stopped Automation Wallet recovery.

## Unattended execution

An extension wallet is an attended signer and requires its own approval for
each transaction. The browser-created wallet can sign while unlocked, but the
current UI still requires a human to request, review and submit each order.
Keeping an unlocked hot key in a long-running tab is not the automation design.

Runner now implements this with a separate encrypted Automation Wallet and a
Python-owned bounded signer/supervisor:

1. Use a dedicated low-balance wallet, separate from the user's main wallet.
2. Protect its signer with OS keychain, hardware-backed custody or a reviewed
   server vault instead of browser local storage.
3. Apply wallet-wide buy, open-cost, reserve, daily-loss and drawdown ceilings.
   Dev56 now persists and enforces the per-strategy allocation and loss layer.
4. Route every candidate through the existing fresh quote, ownership, policy,
   reservation, signature journal and receipt reconciliation checks.
5. Stop on revocation, session expiry, limit exhaustion, route/configuration
   change, unresolved signature, stale valuation or repeated RPC failure.
6. Restart locked and require explicit reauthorization unless the custody policy
   deliberately supports unattended restart.

The existing rehearsal worker remains unsigned. A separate production supervisor
uses the normal reviewed agent-order and receipt path, and production authorization
stays blocked until funded buy → partial sell → restart → full sell and
strategy-owned execution acceptance are recorded against the current code. See
[the bounded Automation Wallet](AUTOMATION-WALLET.md) for implemented behavior,
recovery and remaining limits.

## Verification boundary

The production React build and browser lint pass. Thirty-one Playwright flows run the
built client against an isolated Python API and temporary library. They cover:

- multi-wallet generation, encryption, backup acknowledgement, chooser return,
  legacy storage migration, explicit selection, lock and unlock;
- Wallet Standard discovery and a standards-compatible Ed25519 ownership proof;
- deterministic balance display, deposit QR and refresh, plus a locally created
  wallet signature verified against an exact synthetic legacy transaction;
- generated-wallet export with incorrect-password rejection, a valid 64-byte
  base58 secret and a JSON keypair download;
- existing strategy/session/evaluation/replay actions and an authenticated
  0.2 SOL strategy allocation with exact persisted limits;
- the two-item primary navigation plus the restored Active Strategies and Open
  Positions trade rail and strategy exit metadata returned by the local API;
- the retained token metadata contract and selectable price, market-cap and
  liquidity charts in the token detail view;
- automatic resumable historical-candle storage, cursor and source attribution
  with a deterministic keyless source and no browser setup; and
- compact viewport overflow and keyboard focus;
- the real builder and receipt journal's synthetic buy → partial sell → Python
  process restart → full sell, including exact cash/fee reconciliation;
- rejection, duplicate clicks, account/network changes during approval, locking,
  expired reviews, stale balance, failed simulations and HTTP submission loss;
- unknown and fee-only failed receipts without premature position changes.

The tests use offline synthetic RPC accounts and receipts, a test Wallet
Standard provider and deterministic historical-candle source. They do not prove
a real Phantom transaction prompt, funded trade, browser withdrawal, automatic
execution or mainnet safety.
One live read through the automatic keyless history source returned two
attributed Solana candles on September 25, 2026.
The current focused Python suite has 236 distinct passing checks across nine
files. See [the Dev61 source checkpoint](DEV61-REVIEWED-LIFECYCLE.md).
