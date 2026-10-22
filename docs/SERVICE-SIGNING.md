# Local service signing

Status: dev114 exposes bounded, wallet-approved auto trading in the packaged
app for imported private-key and app-generated wallets. Source previews keep
live activation blocked. This is not a claim of a completed real funded
auto-trading lifecycle; validation evidence remains labelled by its actual mode.

Use **Set up auto trading** in a service-custody strategy monitor. Enroll the
local wallet into the encrypted service vault, review the selected session's
existing allocation and limits, approve exact routes/build/duration with that
wallet, then enter the service password and explicit Start confirmation. The
historical acceptance request/approval schemas remain the exact wallet-signed
capability; importing a wallet or creating an allowance does not activate it.
Limits last at most one hour and cannot be renewed by replaying an approval.
Run monitor and trading Start also enable the background collector. Closing the
browser permits the approved service run to continue while the computer is awake;
Stop/lock, expiry, process restart and sleep/clock discontinuity remove signing.
The proposed allowance uses the lower of the saved risk-policy position cap and
the strategy's own position count. The UI displays funding and conservative
exit-reserve preflight blockers without changing allocations or saved policy.
A two-position allowance requires a 0.0402 SOL exit reserve, before entry funds;
the current 0.01 SOL allocation is below that reserve.

## Custody boundary

`main-wallet-service` is a separate, explicit custody mode for an existing
PumpWizard local wallet, whether generated or imported by private key. Enrollment preserves the Solana address and leaves
the browser backup unchanged. It does not transfer funds, replace the wallet, or
reinterpret a `main-wallet-browser` session.

The browser decrypts its own generated-wallet record only after the user enters
the browser-wallet password. It copies the 32-byte Ed25519 seed into a new
AES-256-GCM envelope protected by a separate service password, then clears its
temporary byte arrays on a best-effort basis. Only that encrypted envelope and
the transient service password may be sent to the authenticated loopback
enrollment route. The route must use a JSON request body; secrets must never be
placed in a URL, log, journal, status response, telemetry record, or persisted
request.

The local service authenticates the envelope with the transient password,
derives the public key, compares the exact wallet address and network, and writes
only ciphertext. It never reads or rewrites browser local storage or the legacy
`solanamarket-wallet.json` file. The service password is not persisted. Python
performs best-effort clearing of mutable seed buffers, but this is not a claim of
guaranteed memory zeroization.

Service files live under `<data-dir>/service-wallets/`. The directory is mode
`0700` and each address-named record is mode `0600` on POSIX. Enrollment uses a
completed temporary file and a no-replace hard link, rejects symlinks and unsafe
permissions, and never overwrites an address or reuses a vault ID.

## Encrypted envelope

The browser export is `pumpwizard.service-wallet-vault/v1`:

```json
{
  "schema": "pumpwizard.service-wallet-vault/v1",
  "vault_id": "32 lowercase hexadecimal characters",
  "custody": "main-wallet-service",
  "address": "Solana base58 public key",
  "network": "mainnet-beta",
  "created_at": "ISO-8601 timestamp",
  "kdf": {
    "name": "PBKDF2",
    "hash": "SHA-256",
    "iterations": 310000,
    "salt": "16 bytes as strict base64"
  },
  "cipher": {
    "name": "AES-256-GCM",
    "nonce": "12 bytes as strict base64",
    "ciphertext": "32-byte seed plus 16-byte tag as strict base64"
  }
}
```

AES-GCM additional authenticated data is compact canonical JSON with sorted
keys for `address`, `created_at`, `custody`, `network`, `schema`, and `vault_id`.
Changing any identity field invalidates authentication.

The frontend adapter is
`prepareServiceVaultEnrollment(address, walletPassword, serviceUnlockSecret,
network)` in `browserWallet.ts`. Its result contains ciphertext only. The background service's
route must pair it with the transient `service_unlock_secret`, authenticate the
loopback dashboard request, bind the already proven wallet identity, call
`ServiceSigningController.enroll`, and discard the request body.

## Session and allowance contract

Service custody requires an immutable `pumpwizard.session-brief/v4` with:

- `permissions.custody = "main-wallet-service"`
- `permissions.generated_wallet_required = true`
- `permissions.service_vault_required = true`
- `permissions.continues_without_browser = true`
- the existing explicit automatic-entry and managed-exit permission fields

Older briefs retain their exact meaning. Browser custody still requires a live
browser runtime and heartbeat. Extension wallets remain interactive and cannot
be enrolled by this adapter.

Review creates `pumpwizard.main-wallet-allowance-review/v2`. Authorization creates
`pumpwizard.main-wallet-allowance/v2`. Both bind the exact vault ID, wallet,
network and genesis, session, strategy digest, policy revision, existing cash
reservation, duration, fee-inclusive budgets, daily and total order counts,
position count, slippage, routes, wallet policy, and strategy policy. The
service reuses the existing cash reservation; enrollment and authorization do
not reserve it again.

Application budgets are checks performed before signing and within the same
SQLite transaction that reserves normal trade usage. They are not an on-chain
cap on every possible use of the wallet.

## Lifecycle methods

`ServiceSigningController` provides the signer interface injected into the
background service:

| Operation | Required input | Result |
| --- | --- | --- |
| `enroll` | encrypted envelope, transient service password, proven address/network | locked ciphertext metadata |
| `review` | enrolled address/network, v4 session, complete plan | non-authorizing v2 review |
| `authorize` | fresh review and exact `ALLOW SERVICE <SESSION8>` confirmation | inactive durable allowance |
| `start` | authorized allowance, fresh service password, exact `START <SESSION8>` confirmation | bounded in-memory signer |
| `state` | none | redacted service-trading state; never a token or secret |
| `pause_entries` | authenticated dashboard action | pending buy tickets removed; exits remain eligible |
| `resume_entries` | allowance ID and exact `RESUME <SESSION8>` confirmation | entries restored within the same allowance |
| `revoke` | authenticated dashboard action | allowance revoked and signer removed |
| `lock` | service stop, fault, restart, or clock discontinuity | signer removed; fresh review/authorization/unlock required |
| `reconcile` | enrolled service identity with a recorded pending signature | public receipt evidence reconciled without a key, replacement or unlocked allowance |
| `run_cycle` | active signer context | reconcile, exit-first selection, at most one exact synthetic submission |

The runtime token is internal to the service process and never appears in public
state. There is no browser heartbeat after service activation and no arbitrary
message or raw-transaction signing method. `ServiceTicketSigner` accepts only a
fresh `pumpwizard.main-wallet-signing-ticket/v1`, verifies the sender, unsigned
wire and message hashes, and signs that exact transaction message.

Each cycle uses `main_wallet_autotrader.prepare_next_ticket` and
`main_wallet_autopilot.submit_signed_ticket`. This preserves reconcile-first and
exit-before-entry ordering, fresh route simulation, wallet and strategy checks,
atomic allowance usage, one broadcast, and normal receipt recovery. Pausing
entries removes pending buy tickets but does not remove the signer needed for an
eligible exit. Stop, revoke, expiry, process restart, or sleep/clock detection
removes access for both sides. Already submitted signatures remain in the normal
journal for reconciliation.

## Acceptance capability and production blockers

The constructor-only `allow_synthetic=True` capability exists for isolated
tests. Request data cannot enable it. The default runtime rejects activation.
The separate [service acceptance capability](PUMPSWAP-SERVICE-ACCEPTANCE.md) now
provides a wallet-signed, build/route/wallet/session/allowance-bound path for one
acceptance run without promoting general production. It is process-local,
single-use and checked before vault decryption; activation records its exact
signed approval durably. Source builds and unsigned/caller-authored records are
rejected.

Production is still blocked. The boundary now passes a deterministic direct
PumpSwap buy/partial-sell/full-sell lifecycle and is exposed through authenticated
request/sign/install API and Dashboard controls. It has not passed the exact
packaged-candidate browser lifecycle, deployed unsigned entry probes or a funded
service run. Existing browser, manual or separate-wallet acceptance does not
satisfy those gates.

Background-service integration must:

1. Instantiate one `ServiceSigningController` in the single-owner background
   service and inject it through `ServiceSigner`.
2. Add authenticated loopback routes for enrollment, review, authorization,
   start, pause/resume, revoke, and redacted status. Route code must never echo
   or persist the transient password.
3. Serialize allowance table installation with shared migrations and keep the
   existing wallet-intent exclusion as the broadcast authority.
4. Call `lock` before shutdown and on restart, worker fault, or clock/sleep
   discontinuity. Do not restore a signer from durable state.
5. Keep production `ServiceRuntimeManager()` as the default. Synthetic injection
   belongs only in isolated tests. The two-step acceptance request and signed
   installation contract must remain wallet-authenticated and must never accept
   an activation boolean or caller-authored file.

Dashboard integration must:

1. Offer service enrollment only for an unlocked PumpWizard Generated Wallet and
   explain that the same address is retained.
2. Collect separate browser-wallet and service passwords without storing either
   in component, URL, browser storage, or logs.
3. Display service enrollment, authorization, signer lock, entry pause, expiry,
   recovery, and production-blocked states separately from browser unlock.
4. Require the exact confirmation strings and never imply that opening the
   dashboard, connecting a wallet, or enabling monitoring authorizes signing.

## Batch 2 verification

The direct synthetic service lifecycle now finalizes entry, an exact 40% partial
exit and the remaining final exit. It asserts receipt token deltas and fee
ceilings, three atomic fills, a zero-quantity closed strategy position, updated
cash/token holds and three non-replenishing allowance orders. The deterministic
accounting check records wallet deltas of -81,944,280, +31,955,000 and
+47,935,000 lamports, a final 197,945,720-lamport shared cash hold, 79,890,000
lamports of realized proceeds and 139,915,000 lamports of fee-inclusive
allowance usage.

A separate lost-response case records the original signature as unknown,
constructs a restarted controller that locks durable authorization, and then
finalizes that signature through public RPC reconciliation without unlocking a
key. The original allowance cannot Start again. A fresh current-cash review,
exact `ALLOW SERVICE <SESSION8>` authorization, service password and exact
`START <SESSION8>` confirmation are required before another signature could be
created. The acceptance records one submission throughout recovery.

Separate direct restart cases retain the original identity, wallet intent, full
cash hold and consumed allowance order while the RPC outcome stays unresolved;
they also reconcile a finalized failure to an exact 5,000-lamport fee-only debit
without creating a position or restoring signing authority. The follow-up owned
execution set passes 28 tests, and 137 shared accounting/contention cases pass.
These focused results supplement rather than replace the earlier immutable-source
complete regression.

The complete exact-source regression collected 842 Python tests (841 passed and
one intentional local-validator skip) and all 41 production-bundle browser flows
passed. The normal `ServiceRuntimeManager()` still reports unavailable and
rejects activation. No real key, funded transaction or production authority was
used. No version was changed and the dev62 Community post remains a local draft.
