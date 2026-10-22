# PumpSwap service acceptance capability

Status, September 29, 2026: the source tree contains the bounded acceptance
boundary, authenticated API/UI request-sign-install flow, direct PumpSwap
route-backed lifecycle coverage and synthetic/fault coverage. General production
activation remains blocked. A disposable exact dev66 Linux candidate has passed
packaged service startup, static-client/build-identity matching and blocked-
production diagnostics. Its actual service also accepted a throwaway wallet's
exact build/route/allowance signature, activated once with no job enabled or order,
retained a verified empty audit, and restarted locked. No funded transaction or
live trading acceptance is claimed by this document.

## Authority model

`allow_synthetic=True` remains an isolated test-harness capability. It cannot be
promoted into acceptance mode. The normal `ServiceRuntimeManager()` remains
blocked until it receives a verified `ServiceAcceptanceCapability` in process.
A JSON object, caller boolean, confirmation string, or caller-authored file is
not a capability.

The selected wallet must sign the domain-separated
`pumpwizard.service-acceptance-approval/v1` payload. The signed payload binds:

| Binding | Exact value |
| --- | --- |
| Implementation | packaged build ID, version, source checkpoint, package timestamp and kind |
| Custody | `main-wallet-service` and the allowance's enrolled vault identity |
| Wallet | sender, network and genesis hash |
| Strategy | session ID and immutable strategy digest |
| Authorization | allowance ID, authorization revision and SHA-256 of every immutable allowance authority field |
| Rules and limits | requested/granted permissions and full scope, including policy hashes, cash hold, routes, slippage, budget/order/day/open-position caps and expiry, through the allowance hash |
| Acceptance scope | one to four explicitly selected routes and a maximum 60-minute approval window that cannot outlive the allowance |

Source-checkout identity (`package_kind=source`) is rejected. A different build,
route, wallet, strategy, policy, limit, allowance, vault or expiry invalidates
the approval. The capability is installed only in memory and is consumed on its
first successful activation. Its approval/allowance IDs are also unique in the
durable audit table, so process-local replacement cannot replay it.

## Lifecycle

1. Create and authorize a normal v2 service-custody allowance after the existing
   reservation, wallet-policy and strategy-policy checks pass.
2. `ServiceSigningController.acceptance_request(...)` derives the exact payload
   and non-transaction message from that durable row. It refuses unresolved
   wallet intent, non-service custody, an unapproved route, an expiring allowance
   or a source build.
3. The selected wallet signs the returned message. This signature spends no SOL
   and is not a transaction signature.
4. `install_acceptance(payload, signature)` verifies the wallet signature, build
   and allowance/vault binding before installing process-local authority. It
   also rechecks the current strategy digest, immutable session permissions,
   wallet/session policy hashes and revisions, and exact held cash allocation.
5. `start(...)` repeats the checks before decrypting the service vault. Activation
   atomically consumes the approval, marks the allowance active and records the
   exact signed payload, signature and message digest in
   `service_acceptance_approvals`, plus a redacted allowance event.
6. Exact-ticket validation additionally rejects any prepared order whose route
   is outside the signed acceptance route list. Existing scheduler, risk,
   reservation, non-replenishing allowance, signing, broadcast and receipt code
   remains the only execution path.
7. Pause still disables entries while retaining eligible exits. Revoke, expiry,
   service stop, clock discontinuity and restart remove signing authority.
   Restart does not reload the capability; public receipt reconciliation remains
   available and a new allowance, approval and unlock are required.

`ServiceSigningController.acceptance_evidence(approval_id)` produces
`pumpwizard.service-acceptance-evidence/v1`. It verifies and correlates the
retained signed approval with the immutable allowance binding, exact ticket/order IDs,
non-replenishing debit usage, route and review payload, transaction signature,
receipt and reconciliation state. An unresolved or reserved order reports
`recovery-required`; finalized and failed receipts report `reconciled` without
turning either outcome into a pass claim. The report is read-only and contains
no password, seed or runtime token. Export re-verifies the archived wallet
signature and message digest and fails closed if the durable approval was
altered. It returns only the signature digest, never the reusable raw approval
signature; the exact signature remains in the local audit database.

`state()` keeps `production_activation=blocked` and reports the separate
`acceptance_activation` state (`blocked`, `available`, or `active`). It now sets
`recovery_required=true` whenever an unresolved wallet intent belongs to a
service-custody wallet, including after process restart when no signer identity
is held in memory.

## Integration contract

The authenticated service API exposes the two-step flow through
`/api/v1/service/signing/acceptance/request` and `/install`, plus read-only evidence
at `/api/v1/service/signing/acceptance/evidence`. Wallet ownership is required for
all three. Dashboard selects one exact authorized route, requests the build-bound
base64 message, checks the returned wallet/network/allowance/route binding, asks
the active wallet to sign that exact message, and installs the signature. A source
checkout or mismatched Dashboard/service build cannot approve it.

The transient service-vault password remains exclusive to the later `start` call
and is not combined with, persisted in, or returned by approval. Dashboard renders
acceptance separately from production promotion; `production_activation` remains
blocked throughout.

## Verification and remaining dependency

The focused fixture covers signed request installation, pre-unlock mismatch
failure, request-only rejection, exact route rejection, one consumed/audited
approval, entry, paused-entry partial exit, full exit, three non-replenishing
allowance orders, a reconciled evidence export, and an unresolved export that
survives restart while signing stays locked. The broader service/main-wallet set
also covers restart locking, idempotent original-signature reconciliation and
fee-only failure accounting.

The lifecycle fixture now covers direct canonical PumpSwap buy, paused-entry
partial sell, final sell, exact route receipts and reconciled acceptance evidence.
Authenticated API tests cover ownership on request, install and evidence access;
the production frontend typechecks and bundles with exact-message wallet signing.
The exact dev66 package also starts its real service process, serves the bundled
client under token authentication, reports the matching embedded runtime/frontend
identity, and stays signer-locked with production activation blocked. Remaining
package acceptance used a disposable generated wallet to sign the exact returned
message, installed and consumed that authority once, verified its empty audit, and
proved restart locking while the service job remained disabled. A fresh packaged
standard-SPL buy separately passed unsigned mainnet simulation. Remaining gates are
broader/Token-2022 deployed-route evidence and explicitly authorized funded
buy/partial-sell/restart/final-sell acceptance. Neither the no-order activation nor
the unsigned simulation is a fill or authority to use a real wallet.
