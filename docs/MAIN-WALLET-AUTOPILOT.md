# Main-wallet Autopilot foundation

Dev114 adds a private, bounded service flow for imported and app-generated local
wallets. **Set up auto trading** opens encrypted enrollment, selected-session
allowance review, wallet approval tied to the exact packaged build/routes/duration,
and explicit password-backed Start. The existing acceptance capability implements
this wallet-approved authority; an unsigned request cannot enable it. Runs remain
limited to one hour and lock on stop, expiry, restart or sleep. Run monitor and
trading Start now enable the collector. Allocations, reserve limits, strategy
formats and monitoring-only paper eligibility are retained. The proposed position
cap is the lower of the strategy's position count and its saved risk-policy cap.
The earlier checkpoint
notes below describe their historical boundaries; no real funded automatic
lifecycle is claimed for this local candidate.

Integrated checkpoint: September 27, 2026. The allowance foundation is wired
into unpublished `0.2.0.dev62` source. The current post-checkpoint working tree
also wires the exit-first synthetic scheduler and browser runtime. Production
activation and funded trading remain blocked.

## Implemented boundary

`main_wallet_autopilot.py` adds an allowance model for the existing generated
main wallet. It does not copy the separate Automation Wallet custody design and
does not place a key in Python. The browser-created wallet seed and password stay
inside the unlocked browser session; only public allowance scope, conservative
usage and audit events are durable.

Allowance review binds all of the following:

- wallet address, network genesis, session ID and exact strategy digest;
- the current wallet/session risk-policy hashes and a policy-only revision;
- the session's existing cash reservation, without creating a second hold;
- total and UTC-day fee-inclusive debit caps, maximum order debit, order count,
  open positions, slippage, routes, duration and conservative exit headroom; and
- server-derived requested and granted permissions.

The policy-only revision is derived from the wallet and strategy risk-change
rows. Ordinary wallet intents do not invalidate an allowance after every order;
exact policy changes do. The existing transaction path still rechecks current
wallet/session risk, fresh portfolio valuation for entries, allocation ownership,
route state, simulation and the prepared message.

Creating a session, selecting a strategy and reviewing limits do not authorize
or activate signing. Explicit authorization creates an inactive durable record.
The browser runtime is another step and is synthetic-only in this checkpoint.
The production `RuntimeManager` rejects activation regardless of request data.
Tests must construct it with the private test-harness option explicitly.

## Browser runtime and exact tickets

The runtime token is random, held only in browser/Python process memory and not
written to browser storage or SQLite. It is bound to the allowance revision,
wallet and genesis. A heartbeat is due every 20 seconds and expires after a
45-second grace period. Lock, refresh, disconnect, expiry, explicit revocation or
backend restart stops new signing. Best-effort `pagehide` locking supplements the
heartbeat; it is not the primary guarantee.

The browser signer has no generic transaction argument. It accepts only a typed
`pumpwizard.main-wallet-signing-ticket/v1` containing the exact existing reviewed
order, allowance revision, sender, unsigned wire hash and message hash. It rejects
extensions, restored view-only sessions, expired heartbeats, wrong revisions,
non-single-signer transactions, nonzero placeholder signatures and changed
messages. Python independently checks the signed message and signature.

Allowance usage is inserted by `AllowanceTradeJournal` inside the existing
`TradeJournal.reserve_trade` transaction. The trade signature, wallet intent,
agent order state and allowance debit therefore commit or roll back together.
The normal submission path broadcasts once and retains an uncertain original
signature for receipt reconciliation. Failed, unknown and lower-cost results do
not replenish allowance usage.

An entry pause is separate from signer revocation. Pausing rejects buys while the
runtime may continue issuing eligible exit tickets. Revocation removes the
runtime and blocks both entries and exits. A locked or closed browser cannot
promise a protective exit; already signed/submitted transactions still reconcile.

UTC accounting uses `[00:00, 24:00)` boundaries based on `authorized_at`. Buy
usage includes amount, reviewed network fee and the full conservative account-
funding allowance. Sells consume the same conservative fee/account-funding
allowance even when expected proceeds are positive. Shared wallet loss, realized
drawdown and portfolio drawdown remain enforced by `wallet_risk`; per-session
daily loss, count, position and trade-size checks remain enforced by
`strategy_risk`. Missing or stale portfolio liquidation evidence blocks entries
through those existing checks and is never converted into an executable estimate.

Winner handoff is visibly unavailable. It is a later management transition, not
a granted permission or working profit policy in this slice.

## Automatic action modes and scheduling

New session briefs use `pumpwizard.session-brief/v3` (browser) or v4 (service).
The setup offers `entries-only`: bounded automatic buys with user-directed sells,
no automatic protective/profit/trailing/maximum-hold exits. `entries-and-exits`
continues to allow both automatic entries and managed exits. Existing `exits-only`
briefs remain supported with their original interactive-entry and automatic-exit
meaning; they are never migrated into buy authority. All modes retain separate
allowance authorization and activation; production activation remains blocked.
Positions retain their session allocation and accounting. For a discretionary
sell, use the existing position takeover action to release the token reservation,
then review and sign the manual sell in Portfolio.
Historical v1/v2 briefs remain byte-for-byte readable, and a v2 Autopilot brief
retains its original entries-and-exits meaning.

`main_wallet_autotrader.py` is the unsigned/signerless scheduling layer. On each
browser poll it:

1. reconciles any original pending or uncertain signature and refuses a
   replacement while that signature is unresolved;
2. revalidates or prepares eligible managed exits before considering entries;
3. refreshes portfolio liquidation evidence before an entry;
4. delegates the exact quote, simulation, strategy checks and durable order to
   `AgentTradeService`; and
5. asks `RuntimeManager` for one hash-bound signing ticket.

The browser signs that ticket only with the unlocked generated main wallet and
submits it through the existing atomic trade path. A browser lock, refresh,
disconnect, heartbeat lapse, explicit stop, revocation, process restart or
allowance expiry removes the signing runtime. Restart and shutdown also return
the affected session to non-automatic execution modes.

## Exported contracts

Python:

- `main_wallet_autopilot.review(...)` creates a 60-second, non-authorizing review.
- `authorize(...)` creates an inactive durable allowance after exact revalidation.
- `RuntimeManager.activate/heartbeat/pause_entries/lock` owns ephemeral presence.
- `RuntimeManager.issue_ticket/next_ticket/validate_signed_ticket/reserve_order`
  constrains one reviewed transaction and accounts for it atomically.
- `submit_signed_ticket(...)` reuses `AgentTradeService` and
  `ManualTradeService.submit_signed`; no alternate builder or broadcaster exists.
- `main_wallet_autotrader.prepare_next_ticket(...)` reconciles first and prepares
  at most one exit-first reviewed ticket per browser poll.
- `web_main_wallet_autopilot.MainWalletAutopilotController` supplies authenticated
  browser-facing methods without registering shared routes itself.

Browser:

- `signMainWalletAutopilotTicket(session, runtime, ticket)` in `browserWallet.ts`
  is the only automatic signing adapter.
- `MainWalletAutopilotBrowserRuntime` owns heartbeat, ticket polling, signing,
  signed-result submission and best-effort page-close locking without persistence.
- `MainWalletAllowanceDialog` reviews and explicitly authorizes inactive scope;
  it starts the synthetic acceptance runtime only when the server was explicitly
  constructed with that test capability. Production still shows blockers and
  no activation control.
- `SessionPlanDialog` now describes generated main-wallet custody, disables
  Autopilot for extension wallets and labels winner handoff as unimplemented.

## Shared-file integration record for dev62

The original checklist below defined the shared integration boundary. Dev62
completed the controller lifecycle, authenticated routes, versioned brief
metadata and allowance-dialog wiring without lowering manual/funded gates:

1. Create one `MainWalletAutopilotController` with the browser server lifecycle.
   Its first journal access converts stale `authorized`, `active` and
   `entry-paused` rows to `locked`; call `shutdown()` on server shutdown.
2. Register authenticated routes under `/api/v1/main-wallet-autopilot`:
   `GET status`, then POST `review`, `authorize`, `activate`, `heartbeat`,
   `pause`, `revoke`, `lock`, `ticket`, `next` and `signed`. `next` should wrap
   `next_ticket()` as `{ticket, detail}`. `ticket` accepts an existing order ID,
   never caller transaction bytes. `signed` accepts only the returned ticket ID
   plus signed wire. Keep the controller's default production manager, so
   `activate` remains blocked.
3. Require the existing authenticated wallet identity on every route and derive
   sender/network from it. Treat wallet kind as trusted in-process UI capability
   metadata, not a free request override. The browser signer must remain the
   enforcement point that rejects extension wallets.
4. Wire `MainWalletAllowanceDialog` from the session detail for new Autopilot
   briefs. Do not expose a Start control while production activation is blocked.
   The later scheduler must prepare through `AgentTradeService`, call the
   controller's `ticket` method, and prioritize exits before entries.
5. Version new session metadata because existing `pumpwizard.session-brief/v1`
   records say `automation-wallet`. Preserve those historical records byte-for-
   byte. New briefs should derive `automation_wallet_required: false`,
   `generated_wallet_required: true` and `custody: main-wallet-browser`. Update
   shared `PortfolioWorkspace` types and session copy accordingly.
6. Keep `App.tsx` responsible for stopping the browser runtime before wallet
   lock/sign-out and when its invalidation callback fires. Do not put the runtime
   token in React persistence, `localStorage` or `sessionStorage`.
7. Add route-level tests for cross-wallet/session review IDs, wrong typed
   confirmation, stale auth revisions, missing authentication, restart locking,
   page-close lock, and an uncertain signed submission recovered by its original
   signature. The existing dedicated source tests are not a substitute for the
   final shared API suite.
8. Do not bump the version or edit the Community draft until integrated behavior
   and final suite results are known. No strategy exchange format changes are
   required.

## Verification at this checkpoint

Post-checkpoint wiring verification in the current working tree:

- the complete 803-test Python collection passed with 802 passes and one
  intentional skip, including immutable exit-only scope and scheduler-created
  entry tickets;
- Ruff, frontend ESLint and the isolated production build passed; and
- the dedicated production-bundle Playwright flow passed through session scope,
  allowance review, authorization, synthetic activation, automatic order
  preparation, exact browser signing, atomic usage, submission and stop.

These are synthetic fixtures only and are not a funded acceptance result.

The final integrated checkpoint passed the complete 802-test Python collection
(801 passed, one intentional skip), Ruff across source/tests/packaging, frontend
lint/typecheck and production build, and all 38 production-bundle Playwright
flows. The browser allowance flow verifies the exact scope, inactive
authorization and visible production blockers without a Start/Activate control.

Independent foundation results retained below:

- `PYTHONPATH=src /tmp/pumpwizard-review-20260927/bin/pytest -q tests/test_main_wallet_autopilot.py`
  — 10 passed.
- `ruff check src/pumpwizard_runner/main_wallet_autopilot.py tests/test_main_wallet_autopilot.py`
  — passed.
- `npm run lint` — passed.
- `npm run build -- --outDir /tmp/pumpwizard-main-wallet-autopilot-dist --emptyOutDir`
  — passed; Vite retained its existing large-chunk warning.
- Isolated Playwright on port 8771,
  `e2e/main-wallet-autopilot.spec.ts` — 1 passed.
- Complete isolated production-bundle Playwright regression on port 8771 —
  37 passed, including the dedicated allowance flow.

All wallet funds, signatures, receipts and balances in the Python tests are
synthetic fixtures. No real transaction, funded acceptance record, source-app
restart, Community publication or binary build was performed.

## Remaining limits

There is no production activation, funded lifecycle, extension unattended
signing, entry-only mode, or winner-handoff transition. The automatic scheduler
and browser activation control are available only to an explicitly injected
synthetic acceptance controller; the normal server cannot enable them through
request data. Legacy separate Automation Wallet records and recovery paths are
preserved and untouched.

## New-session runtime default (dev78)

The wizard no longer offers browser-only automatic signing. Both automatic
entries and Full Autopilot request `main-wallet-service` custody and persist
service-vault/continuation requirements in the plan. Existing browser-custody
briefs remain readable and keep their original authority. Interactive monitoring
also runs in the background service, but trades still require a connected-wallet
signature; it does not enroll or unlock a service signer. The dashboard is the
current control surface. Enrollment and explicit service activation remain
separate; production automation activation stays blocked.
