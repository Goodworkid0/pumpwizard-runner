# Main-wallet strategy allocations

Dev56 divides one signed-in wallet into durable local SOL allocations for
individual strategy sessions. The main wallet remains the only on-chain address.
Saving an allocation creates or updates a local cash reservation; it does not
derive a new key, create an account or transfer SOL.

## Allocation model

- The sum of active strategy cash reservations cannot exceed the wallet's live
  confirmed SOL balance when a policy is saved.
- Manual buys, withdrawals and other strategies cannot consume locally reserved
  SOL through Runner's reviewed transaction paths.
- A successful strategy buy reduces its cash reservation by the exact wallet
  debit. A successful managed sell credits proceeds back to that reservation.
- Open token positions receive a separate strategy-owned token reservation.
- Changing limits is blocked while the strategy has a reviewed/submitted order
  or active signing session. Saving also increments the wallet revision so stale
  reviews fail validation.

## Per-strategy controls

| Control | Enforcement |
| --- | --- |
| Reviewed buys | Blocks new managed entry reviews when disabled. |
| Managed strategy sells | Blocks strategy-owned exit orders when disabled. |
| Minimum / maximum buy | Bounds the reviewed SOL buy amount. |
| Maximum buys per UTC day | Caps accumulated reviewed buy amounts. |
| Maximum trades per UTC day | Stops new entries after the strategy's daily order count reaches the limit. |
| Daily loss in SOL and percent | Stops new entries at the lower limit. The percentage uses the configured allocation. |
| Maximum loss per trade | Supplies the executable-net stop threshold for managed exits. |
| Maximum open positions | Stops new entries when owned finalized positions reach capacity. |

The Python service applies these checks during unsigned candidate assessment,
reviewed-order persistence and final submission validation. TypeScript collects
and displays settings but is not the authority for execution.

## Manual exits and automation

A strategy-owned token reservation prevents the regular manual sell flow from
silently taking over that position. The existing reviewed handoff can release
the managed allocation for a manual exit; its browser interface is still
pending.

Automatic submission remains disabled. Extension wallets continue to require
attended approval. An unattended executor still needs a dedicated bounded
signer, wallet-wide ceilings, revocation and restart behavior, and funded
buy/partial-sell/restart/full-sell acceptance.
