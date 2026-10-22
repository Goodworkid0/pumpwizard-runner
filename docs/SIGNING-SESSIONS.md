# Bounded signing sessions — dev38

Pump.fun remains the first end-to-end target. The session layer authorizes orders
through the existing agent execution service; it does not discover markets,
select trades, build swap instructions, or run an automatic trading loop.

## Use a session

1. Configure **Wallet limits** and allocate SOL to the pinned core-frequency agent.
2. In **Agent trading → Signing session**, enter a total spending cap, duration
   of 1–60 whole minutes, maximum 1–100 orders and slippage ceiling of 1–500 bps.
   Values are not preselected. The cap cannot exceed the selected cash allocation.
3. Choose **Review & authorize session**, check the wallet/network/agent/allocation,
   type the displayed `SESSION …` confirmation and enter the vault password.
4. Prepare orders normally. With **Use this session** selected, each order still
   requires its economics review and typed `SIGN …` approval. The bounded session
   replaces that order's password prompt; no automatic worker starts.
5. **Revoke session** disables local session authorization immediately and persists
   revocation after any current wallet operation releases its lock. The button
   remains available during background revalidation. Already signed or submitted
   transactions cannot be recalled; recover their original signatures normally.

Closing the workspace, changing its network or closing the app locks its local
session. Reopening requires fresh authorization. If a session expires or runs
out of budget, uncheck **Use this session** to explicitly authorize a needed exit
with the normal per-order password flow. There is no silent fallback or renewal.

## Scope and accounting

A session binds one wallet, genesis/network, agent configuration, cash allocation
and explicit route allowlist. Current routes are the supported Pump curve trades
and canonical PumpSwap exits. Manual orders, withdrawals, another agent/allocation,
and broader DEX routes cannot use this capability.

Each submitted order consumes one count and its conservative debit allowance:
buy amount (for buys) plus quoted fee and maximum account-funding allowance.
Sell allowances also consume budget even when the expected net proceeds are
positive. Proceeds, lower actual fees, failed transactions and cancellation after
submission never replenish the session cap. Unknown transactions retain their
usage while normal wallet exclusion prevents replacement or duplicate submission.

Usage and the original signature/order reservation commit atomically under the
wallet write lock. A failed check or failed signing/verification rolls back both.
Fresh market checks, agent ownership, receipt settlement and recovery use the
existing execution path unchanged.

Wallet limits must already be configured and must match those reviewed when
authorizing the session. Dev40 requires an explicitly chosen portfolio drawdown
ceiling before authorizing any new session; old policies are not silently given
a new limit. Buys enforce both the current policy and the original
session policy: loosening wallet limits cannot expand an already authorized
session. Tightening limits and pausing wallet entries take effect through the
normal checks. A session authorized while entries are paused retains that
restriction; authorize a new session to permit entries after unpausing.

## Key lifetime and revocation

Only public scope, expiry, status and conservative order usage are written to
`signing_sessions` and `signing_session_orders` in the wallet journal. An unlocked
signer reference remains in the authorizing process. No password, plaintext key
or serialized signing capability is stored in these tables or session reports.
Losing process state always loses session authorization. A historical row marked
`active` does not unlock a session in a new process.

Both wall-clock bounds and an in-process monotonic deadline are checked before
signing. An observed expired/revoked session loses its signer reference. Explicit
replacement revokes previous active authorizations for that wallet/network,
including another process's session at its next signing check. Revocation during
session creation also prevents the new capability from becoming available.

These controls constrain the application's trusted execution paths. Python
reference removal is not a guarantee of physical memory erasure or isolation
from malicious code inside the same process; the desktop's ordinary unlocked
wallet lifecycle remains separate.

## Verification and next work

Synthetic tests exercise scoped buy/partial/full exit, total cap and order-count
exhaustion, wrong allocation/manual orders, expiry, wall-clock jumps, revocation,
replacement, process restart, uncertain submission, rollback and retained policy
ceilings. Desktop checks cover session confirmation, reviewed trades without
repeated password prompts, revoke, reopen locked and an explicit password exit.

No real wallet session was authorized during development. Dev40 adds portfolio
drawdown including unrealized losses. Funded acceptance and automatic execution
remain pending. Finish and validate the Pump.fun lifecycle first. Later DEX adapters
must reuse these authorization/risk/accounting boundaries and receive their own
transaction validation and acceptance coverage before being added to the route
allowlist.
