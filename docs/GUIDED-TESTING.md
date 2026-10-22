# Guided local evaluation and funded-test preparation — dev54

The browser app is the default Runner interface in dev54. It includes local
strategy import, monitoring controls, evaluation and replay. Start with
[offline evaluation](LOCAL-EVALUATION.md); see
[strategy onboarding](STRATEGY-HANDOFF.md) and the explicit
[remaining funded acceptance record](FUNDED-ACCEPTANCE.md).

The legacy native client exposes the reviewed agent workflow. You can complete setup
before depositing. Every actual trade still requires a fresh review, a typed
order confirmation, with a wallet password or explicitly authorized bounded
signing session. The optional execution worker continuously rehearses unsigned
assessments; automatic trading remains disabled.

## Open the test build

Use **SolanaMarket Wizard** in the Linux application menu after installing the
local shortcut, or launch the validated build from this checkout:

```bash
./launch.sh
```

That command starts the loopback browser app. The wallet, signing, withdrawal,
worker-control and reviewed-trade steps below still require the legacy client:

```bash
./launch.sh desktop
```

The launcher also accepts CLI arguments such as `--version` or `--data-dir`.
It resolves the bundle relative to itself, so launching from another working
directory opens the same build. Keep this checkout in place while using its
application-menu shortcut. The separately built Linux executable is:

```bash
./dist/dev54/SolanaMarketWizard/SolanaMarketWizard
```

Keep its `_internal` directory beside it. The versioned archive under
`release-artifacts/dev54` includes the complete folder. This build uses the
existing application data directory and retains the existing encrypted wallet.
It retains earlier builds under their existing paths. Windows and macOS package
launches have not been independently validated for this milestone.

To recreate the local application-menu shortcut:

```bash
python3 packaging/linux_launcher.py
desktop-file-validate build/desktop/solanamarket-wizard.desktop
install -Dm644 build/desktop/solanamarket-wizard.desktop "$HOME/.local/share/applications/solanamarket-wizard.desktop"
update-desktop-database "$HOME/.local/share/applications"
```

This shortcut uses the normal application data directory. It does not select a
new wallet or change tracking settings. The versioned downloadable archive can
still be launched directly without this checkout or shortcut.

## Before depositing

1. Open **Test setup** on the market screen. It reuses an existing pinned
   core-frequency instance or creates one with entries disabled. For an existing
   agent, select it and open **Agent trading**.
2. Open **Wallet & backup**, export an encrypted backup, and retain the password
   separately. The readiness screen does not pretend to verify your backup storage.
3. Choose the intended network. **Copy deposit address** copies the public address;
   the same address can appear on different networks, whose balances remain separate.
4. Enter an allocation in SOL and slippage in basis points; 100 bps means 1%.
   **Save setup** retains these draft inputs for this agent and wallet. It does
   not reserve or spend money. No allocation amount is silently chosen for you.
5. Open **Wallet limits**, enter and review your chosen SOL limits. These apply to
   all agents and manual buys on this wallet/network. No limits are preselected;
   see [wallet risk controls](WALLET-RISK.md) for exact accounting and scope.
   Dev40 adds **Portfolio drawdown including open positions**. Existing policies
   need an explicitly chosen ceiling before authorizing a new signing session.
   **Refresh portfolio value** inspects unsigned liquidation evidence; quotes
   refresh automatically before new buys. Unknown/stale valuations block entries.
6. Choose **Enable reviewed entries**, then **Start tracking** to collect signals.
   Tracking and enabling entry review do not authorize trades. **Pause entries**
   keeps held-position monitoring active.

7. Open **Worker rehearsal** and choose **Start unsigned worker** to repeatedly
   assess current signals and managed exits. The worker shows funding/setup blocks
   before an allocation exists. Stop it there or close the workspace; it does not
   resume automatically. See [worker controls and limits](EXECUTION-WORKER.md).

Setup displays strategy support, RPC identity, funding, entry controls, allocation,
market freshness and unresolved wallet operations. Zero balance shows **Awaiting
deposit**; an RPC failure shows an unknown balance. Local history remains visible.
These are individual checks, not a blanket “safe to trade” certification.

The pinned configuration has a $10 maximum total entry cash budget, converted
using fresh SOL/USD data. The setup screen estimates that entry budget plus a
conservative exit funding/fee reserve when fresh quotes are available. The USD
budget is not a promise of token value or profit. An allocation reserves capital;
it is not a cumulative-loss limit, and settled proceeds can be reused after a
new, separately authorized review.

## After funding

1. **Refresh & recover** and confirm the selected network and balance.
2. **Allocate SOL** creates the agent's dedicated local cash reservation. Other
   agents, manual trades and withdrawals respect that reservation.
3. Under **Candidates**, select a signal or an open managed position and choose
   **Assess unsigned**. The result appears in **Shadow reports** as ready, hold
   or blocked, with an explanation and available executable net-value evidence.
   Reports are historical evidence; a ready result does not authorize a trade.
4. **Prepare order review** runs fresh checks and binds the selected allocation.
   Under **Orders**, select the review and choose **Review & authorize**. Inspect
   wallet, network, token, reason, quantity, minimum output, fee, funding allowance
   and net bound; type the displayed order confirmation and enter the password
   unless you are using an explicitly authorized bounded session. See
   [signing sessions](SIGNING-SESSIONS.md) for optional session setup and revocation.
5. Follow the saved signature to finality. Pending or unknown outcomes keep the
   wallet reserved. **Refresh & recover** and reopening the app reconcile the
   original signature without re-signing it. An expired unsigned review remains
   bound until explicitly cancelled; cancel it before preparing another.
6. **Positions** shows finalized quantities, remaining SOL cost and realized
   SOL P/L. Assess exits through the same unsigned workflow. Market-cap targets
   can produce partial exits; stops/trails use the remaining position's
   conservative executable value.

The first funded acceptance remains: reviewed buy → finalized agent position →
unsigned exit assessment → reviewed partial exit → app restart → reviewed full
exit. Record the order IDs, signatures and exported unsigned reports. Confirm
that quantities, cost and allocations agree with finalized receipts after
restart. Do not force a strategy signal merely to finish the checklist.

## Manual exit and recovery controls

**Manual exit…** explicitly releases the selected position's token allocation
and opens the manual sell form with its full remaining quantity selected. You
can enter a smaller amount for a partial sale. This handoff does not sell tokens;
the manual form still requires review and authorization. Remaining tokens stay
under manual exit control, marked **manual exit** in the agent workspace.
Proceeds return to the wallet outside the agent cash allocation; finalized fills
remain attributed to the position owner. This tests the override path, not a
strategy-triggered exit. Bound reviews or unresolved transactions block release.

**Cancel unsigned review** removes its order binding while retaining the underlying
allocation. **Release selected allocation** frees that local reservation for
other wallet operations; it never broadcasts a transaction. Submitted, pending
or unknown orders cannot be cancelled or silently replaced.

The main sidebar labels finalized positions with their owning agent. Older
user-recorded positions and simulated orders are labeled **Recorded positions**
and **Practice orders**; they do not become wallet holdings.

## Verification and limits

Dev38 passes 448 unit/integration tests, with one local-validator withdrawal test
skipped. The isolated desktop checks cover setup with zero funds, allocation,
wallet-limit review/save/reload, bounded session authorization/revocation,
locked restart and exits with wallet entries paused,
unsigned assessment with unchanged wallet state, declined and mistyped
authorization, finalized buy, paused partial exit, restart/full exit, manual
override and owner attribution. Existing SPL and Token-2022 graduation lifecycles
also pass with synthetic RPC and throwaway wallets. No real funds were used.

Linux bundle checks cover its version/help commands and desktop startup. These
checks do not establish funded chain acceptance. The earlier independent live
unsigned curve/PumpSwap probes are described in [route verification](ROUTE-VERIFICATION.md).
Dev40 adds deployed Token-2022 PumpSwap unsigned sell evidence and configurable
portfolio drawdown including unrealized losses. Broader strategy support,
funded acceptance and automatic execution remain future work.
