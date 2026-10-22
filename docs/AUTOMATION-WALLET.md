# Bounded Automation Wallet

Implemented in source on September 25, 2026. The source version remains
`0.2.0.dev60`; this is not a release and live automation is still gated.

## What is implemented

The browser can create or restore a separate encrypted Automation Wallet, verify
and download its ciphertext backup, show its independently refreshed balance,
fund it through the ordinary Phantom-reviewed SOL transfer, and show its own
positions, trade receipts, withdrawals and unresolved-operation state.

An authorization review now calculates the pinned Core Frequency strategy's
fixed $10 entry debit from a fresh SOL/USD observation. It checks the wallet
balance, minimum reserve, equal per-session allocations, maximum buy debit,
daily caps, position capacity and one conservative full-exit allowance per
configured position. The current total authorization cap remains 0.1 SOL, the
duration remains 1–60 minutes and the priority-fee ceiling is zero. Incompatible
values are reported as exact lamport shortfalls; the strategy order is never
silently resized.

After all acceptance gates pass, the owner can unlock the local vault for one
reviewed authorization. The decrypted signer stays only in the Python server
process. Durable records contain public scope, configuration and policy hashes,
cash-hold bindings, expiry, state and conservative usage. Every buy or sell uses
the existing `AgentTradeService` and `ManualTradeService` path. Usage and the
original transaction signature are committed atomically. Sales, profits, lower
fees and failed submissions do not restore the authorization cap.

The production supervisor reconciles an original pending signature first,
prioritizes managed exits, assesses fresh strategy actions, prepares the exact
order, invokes the bounded signer and broadcasts once. The older execution
worker remains unsigned. Multiple selected strategies share one wallet-level
cap and one position/exit-headroom limit.

Emergency Stop signals the signer before waiting for journal work, stops the
supervisor and persists revocation. Entry pause still permits eligible exits.
Closing the browser tab does not stop an authorized run, but stopping Runner,
restarting the process or reaching expiry removes the in-memory signer and never
auto-starts it again. A transaction already signed or sent at the stop boundary
can still land and remains tied to its original recorded signature.

Manual recovery is available while automation is stopped:

- review and password-sign one exact SOL withdrawal back to the funding owner;
- explicitly release automation management and cash allocations for an open
  position, review a full-position sell and password-sign only that sell; and
- reconcile the retained original trade or withdrawal receipt without retrying.

The browser never receives the seed, decrypted key, password, signed payload or
an unrestricted signing endpoint. Automation endpoints do not accept caller
transaction bytes.

## Current gate and limits

Production authorization reads `automation-funded-acceptance.json` from the
Runner data directory. No such acceptance record is created by setup, tests or
the browser. A valid record must separately mark the reviewed funded manual
lifecycle and reviewed strategy-owned execution as verified, and it must match
the current signer/risk/route implementation hash, pinned strategy configuration
hash and route list. Material changes invalidate earlier acceptance.

Those funded checks are still pending. The verified Firefox Phantom buy proves
only a manual buy. It does not authorize a sell, a deposit into the Automation
Wallet or a live automatic run. The UI can create, back up, fund, recover and
preflight the wallet while activation remains disabled with the specific
blockers shown.

Limits enforced by the application are not an onchain smart-wallet policy. The
separate wallet limits the capital exposed to this process, but cannot guarantee
a loss ceiling when RPC, liquidity, the computer or execution is unavailable.
Runner must remain open on an awake computer. Only pinned Core Frequency
sessions and the already supported Pump curve / canonical PumpSwap routes are
eligible. Graduated-token buys, unsupported token extensions and unsupported
pool modes remain blocked.

## Synthetic validation

`tests/test_automation_runtime.py` covers:

- exact rejection of the former 0.01/0.05 SOL defaults for a $10 order;
- the production acceptance gate and absence of a retained signer on failure;
- bounded buy → partial sell → full sell accounting with non-replenishing usage;
- two strategy sessions consuming one shared authorization record and cap;
- supervisor preparation, signing and broadcast through the shared services;
- stop and process-restart locking without broadcast;
- managed-position handoff and password-reviewed full sell; and
- exact password-reviewed SOL withdrawal, wrong-password rejection and replay
  rejection.

The broader signing-session, order, reservation, risk, portfolio, withdrawal and
unsigned-worker suites remain the supporting validation. These are synthetic
tests and make no funded-mainnet claim.

The current source checkpoint passed the complete Python test suite with one
expected skip, Ruff, browser lint/build, and all 31 Playwright browser flows. The
running source app was restarted only after a read-only journal check found one
finalized trade and no unresolved operation. Its diagnostics continue to report
automatic signing as inactive while funded acceptance is pending.
