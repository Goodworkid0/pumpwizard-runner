# Changelog

## 0.2.0.dev121

- Rebuild Activity Orders around matching-order, finalized, awaiting-receipt and exact recorded-fee cards, a buy/sell submission timeline and clickable settlement breakdown. Add time/side/strategy/status filters, search, sorting, 20-row pagination, expandable receipts, identifier copying, Explorer links, full token charts and filtered CSV export across pages.
- Combine scoped Decisions into one workspace with a saved cap/liquidity market map, selected strategy entry range, reason breakdown and summaries. Add search/time/strategy/reason filters, sorting, pagination, saved-input inspection, copying, token charts and CSV. Keep an inspected decision open during monitor refresh; label the API's 12-candidate-per-strategy snapshot limit and distinguish signals from orders.
- Use consistent headings, generated mint identity icons and labelled mobile record cards. Missing inputs, timestamps and fees remain explicit; BigInt preserves SOL atom precision. Frontend build/lint and 23 distinct selected Playwright checks passed after test corrections and focused reruns, including four final Activity checks. Package/install evidence is recorded separately in output/dev121-activity-verification.json. No backend accounting or execution changes, full suite or funded transaction are claimed. Community post remains an unpublished draft.

## 0.2.0.dev120

- Remove Positions, its open-position preview and shortcut from the wallet-wide dashboard. Keep Overview, Activity and Wallet; retain Positions and its preview for a selected session or strategy, and preserve the merged Performance presentation and historical results.
- Fix All wallet activity to clear selection and activate Overview. Route wallet-wide stale-price reviews and sidebar wallet links to Wallet. Keep manual holdings/sells there and make the holding name open full token details. Closing an attributed order returns to its strategy positions; manual orders return to Wallet.
- Frontend build/lint and 19 selected source-browser checks passed after fixes and a complete selected-set rerun. Package, backup and installed-service evidence is recorded separately in output/dev120-positions-verification.json. No accounting, strategy exchange or execution authorization changes, full repository suite or funded transaction are claimed. The Community post remains an unpublished draft.

## 0.2.0.dev119

- Merge Performance into Overview and remove its separate tab. Use Performance's realized, unrealized and total strategy P&L and closed-position cards, full comparison columns and total-P&L sorting as the primary presentation.
- Add compact monitoring, service trading and open-position status, deduplicated funding/other attention and position/recent-trade previews. Default to all current strategies, including paused ones; All history explicitly includes archived rows and Needs attention stays current. Retain historical results in since-start totals, selected scope and mobile cards.
- Frontend build/lint and 12 selected source-browser checks passed. Native package, backup and installed-service evidence is recorded separately in output/dev119-overview-verification.json. No accounting, strategy exchange or execution authorization changes, funded transaction or full repository suite are claimed. The Community post remains an unpublished draft.

## 0.2.0.dev118

- Limit Overview to current strategies with four comparison columns. Retain archived attribution in Performance and since-start results; show shared funding attention once per session with its existing funds dialog.
- Group saved strategy versions, distinguish session membership from compatible rules and expose real-wallet funding and approval prerequisites. Edit the strategy already attached to the session; collapse optional simulation history.
- Replace percentage-based trend placeholders with actual timestamped price graphs and hover inspection. Progressively load bounded public history, retain missing-history states, show exact-mint token artwork with static SVG sanitization and labelled generated fallbacks, and rename holding Evidence to Details.
- Frontend build/lint and 73 focused accounting, funding, wallet, market and artwork Python checks passed. Nineteen distinct source-browser checks passed after fixes and focused reruns. Package/install evidence is recorded separately in output/dev118-dashboard-verification.json. No real funded transaction, complete repository suite, profitable-strategy claim or public Community publication is claimed. Trading approval and strategy exchange formats remain unchanged.

## 0.2.0.dev117

- Reorganize Dashboard into Overview, Positions, Performance, Activity and Wallet, with New order as an action. Keep the balance in the header and focus Overview on active strategies, open wallet positions, attention and recent trades. Preserve session/strategy scope across operational tabs; Wallet returns to the whole selected wallet.
- Expose since-start realized/unrealized SOL P&L, recorded fees and closed positions per strategy using existing receipt and fresh-mark accounting. Retain archived attribution and shared session funds. Make position details expandable, mobile comparisons labelled cards and Activity filterable between orders and decisions.
- Source build/lint, 40 focused Python checks and 12 distinct source-browser checks passed after fixes and targeted reruns. Package/install verification is recorded separately. Stale or unknown valuations stay unavailable; period returns, drawdown, complete historical strategy curves and independent-strategy performance remain limited. The Community post is an unpublished draft; no full suite or real funded transaction is claimed.


## 0.2.0.dev116

- Show confirmed wallet SOL and available-to-allocate SOL prominently in Adjust trading funds, with a snapshot check time and the existing Refresh funds action. Keep session allocation, trade cash and positions separate, and preview unallocated wallet funds after changes. Explain over-allocation while preserving exact funding calculations and all pause, signing and stale-snapshot guards.
- Frontend build/lint, seven targeted session-funds Python checks and two focused offline browser scenarios passed, covering refresh, reservations from another session, top-up/release, overdraw, shortfall and mobile layout. The Community note remains an unpublished local draft; no full suite or funded transaction is claimed.

## 0.2.0.dev115

- Remove the Wallet overview / Dashboard heading, repeated wallet identity and manual Refresh button from Overview. Start the tab with the metric cards and retain automatic portfolio and trading-record refreshes, responsive layout and data warnings.
- Frontend build/lint and the two existing portfolio browser checks passed, including desktop/mobile layout, saved history and automatic stale-price updates without a Refresh button. No complete repository suite or funded transaction is claimed. The Community note is an unpublished local draft.

## 0.2.0.dev114

- Keep the wallet-approved auto-trading setup and automatic collector startup from dev113. Cap the proposed allowance's positions at the lower of the saved risk policy and the strategy's own position count, so an older, broader policy does not inflate the default exit reserve.
- Preserve saved policies, allocations, exact-build wallet approval, encryption, transaction validation and one-hour/restart locking. Dev113 validation passed 47 targeted Python checks, seven source browser scenarios and nine distinct packaged browser scenarios using offline/synthetic RPC. This follow-up reuses that backend; no real funded automatic lifecycle or public release is claimed. The Community note remains a local draft.

## 0.2.0.dev113

- Expose bounded auto-trading setup for imported private-key and app-generated local wallets: encrypted service enrollment, selected-session limits, wallet-signed approval of exact routes/build/duration, then a fresh service password and Start. Use all four supported routes by default within the existing allowance; preserve one-hour expiry, exact-ticket signing, accounting, pause/stop and restart locking.
- Report wallet approval required, approved and active states accurately. Keep unsigned starts blocked and source previews unable to authorize live signing. Bind the Start button to the selected session's allowance and display existing funding/exit-reserve preflight limits without changing allocations.
- Run monitor and service trading Start enable the background collector. Waiting monitors display Start scanning instead of claiming an active scanner. Monitoring alone does not authorize trading, and paper research remains monitoring-only.
- Forty-seven targeted signing/approval/service/monitor Python checks, seven source browser scenarios and frontend build/lint passed. Two wallet-approval browser scenarios require the exact packaged app and are skipped in a source preview. This local candidate has an unpublished Community draft; no complete repository suite or real funded auto-trading lifecycle is claimed.

## 0.2.0.dev112

- Restrict prospective paper research to monitoring sessions using the stored brief and owning workspace, including legacy briefs. Trading-session evidence views retain Evidence, Results and History without Forward paper or Campaigns tabs; paper campaign setup accepts only monitoring sources.
- Reject typed and legacy paper starts, paper resumes and campaign starts/resumes tied to trading sessions at the API. Retain existing research history and allow older runs to be paused or stopped. Stored briefs, real SOL allocations and trading authority are preserved.
- Seven focused synthetic browser scenarios, 35 targeted paper/research/campaign Python checks and frontend build/lint passed. This local Linux candidate has an unpublished Community draft; no complete repository suite or funded transaction is claimed.

## 0.2.0.dev111

- Hide the Paper trading shortcut in trading-strategy monitors and sidebar menus, and omit their Paper budget field. Retain actual session SOL allocations, execution mode, evidence review, order controls and signing/custody controls. Recognize older interactive/Autopilot briefs without the newer session-mode field.
- Keep paper research available for monitoring strategies. Four focused synthetic browser scenarios and frontend build/lint passed, covering funded interactive, service and older browser Autopilot controls plus explicit monitoring paper start/pause/stop with no submitted transaction. This local candidate has an unpublished Community draft; stored briefs, allocations and execution gates are unchanged. No complete repository suite or funded transaction is claimed.

## 0.2.0.dev110

- Make the inline wallet chart follow the main SOL/USD switch, including its scale, path and inspected values. Estimate lifetime USD values with cached historical hourly/daily SOL/USD opening prices; retain missing-price and receipt gaps and use the current quote for the latest point when available.
- Move Retry startup, Review session and other service recovery actions into System activity & AI chat, including its detached window. Retain the compact balance card and header height.
- Thirteen focused browser scenarios, 27 targeted native balance/history, historical-price and currency API Python checks, frontend build and lint passed. A read-only USD check priced the connected wallet's 122 listed transactions with zero missing price intervals. Provider/archive limits still apply. This is a local candidate with an unpublished Community draft; no complete repository suite or funded transaction is claimed.

## 0.2.0.dev109

- Return the unified header near its earlier height with a 58-pixel labelled inline chart, currency/History buttons on one row and tighter spacing. Keep the larger chart in History and retain native USD refresh, lifetime backfill and all range/inspection controls.
- Nine focused native balance/history and portfolio browser scenarios, frontend build and lint passed. Checks cover compact height and desktop/mobile overflow. No wallet/signing rule changes, full repository suite or funded transaction are claimed. This is a local candidate with an unpublished Community draft.

## 0.2.0.dev108

- Refresh native SOL/USD independently of token portfolio reads, with a fresh Pump quote, timestamp-checked Coinbase fallback and explicitly aged five-minute estimate. Keep SOL visible when prices fail; display quotes do not alter execution validation.
- Replace the tiny sparkline with a larger shaded SOL chart, date labels, balance scale, hover/keyboard inspection and 7d/1m/All ranges. Persist paced transaction backfill beyond 500 signatures and resume after rate limits or restarts, retaining missing-receipt gaps.
- Restore saved public wallet views with signing locked after proof expiry or temporary RPC failure. Native balance/history remain local-token-protected public data; private accounting and signing still require their existing authority.
- Nineteen focused browser scenarios, frontend build/lint and 75 targeted Python checks passed. A read-only mainnet check recovered all 122 currently listed transactions with zero missing receipts or balance gaps and a live USD estimate. Archive completeness and older USD rates are not inferred. This local candidate has an unpublished Community draft.

## 0.2.0.dev107

- Widen the header balance card and show a small interactive SOL history chart directly beneath the main amount. Keep the currency switch, italic equivalent, Deposit/Withdraw, Guides and Sign out. Retain a single header row at 1600/2048 pixels and the narrow-screen layout.
- Load exact native pre/post balances from available confirmed transactions, starting at the earliest available activity and ending with the current confirmed balance. Include failed transaction fee effects, preserve missing-record and balance-continuity gaps, and add All to the expanded history view. Wallet key creation dates are not inferred from import dates or first activity.
- Bound reads to 500 signatures with a 20-second scan/detail budget and cache results for five minutes. Busy, incomplete or pruned RPC histories can show partial coverage. The inline chart remains labelled SOL history; older USD prices are not reconstructed using today's rate. Saved USD readings retain the existing seven-day observation history.
- Twenty distinct focused synthetic browser scenarios passed, including two inline-history checks rerun after correcting for an additional asynchronously confirmed reading in the test. Frontend build/lint and 64 wallet-history, portfolio, account/API and release-note/packaging checks passed. No full repository suite or funded transaction is claimed. This is a local candidate with an unpublished Community draft.

## 0.2.0.dev106

- Replace the two header values with one native wallet balance: SOL as the main amount and its smaller italic USD equivalent. The switch reverses them and saves the choice per wallet/network. Keep Deposit/Withdraw in the same card; token-inclusive complete/partial portfolio value remains in Overview.
- Add History beside the switch, opening an interactive native balance chart with SOL/USD and 24h/7d controls. Record exact lamports and the observed USD equivalent on the local server in 15-minute buckets, retaining seven days. Historical USD values retain their recorded rate; missing prices and unobserved intervals leave gaps.
- History begins with the first confirmed reading while the Dashboard is open. Older chain activity is not backfilled, and portfolio-worth snapshots are not treated as native SOL history. Deposits, withdrawals and fees affect the native balance; the chart excludes token holdings.
- Eighteen focused synthetic browser checks, frontend build/lint and 44 account/API, storage and release-note/packaging checks passed. Checks include unit persistence, historical USD inspection, missing FX, desktop/mobile layout, encrypted backup/restore and existing signing controls. No full repository suite or funded transaction is claimed. This is a local candidate with an unpublished Community draft.

## 0.2.0.dev105

- Make the wallet address the bold main line in the wallet identity card. Show eight characters from each end in a monospace font, retain the full address in its hover title and screen-reader text, and move the actual provider/name into the secondary network/signing details.
- Retain the pulsing Unlock signing action inside a locked card and walletless Connect wallet. Three focused synthetic browser checks passed for desktop/mobile layout, walletless keyboard access and Wallet Standard ownership, along with frontend build/lint. No complete repository suite or funded transaction is claimed.
- This is a local development candidate with an unpublished Community draft. Existing wallet, signing, provider approval and production execution limits remain unchanged.

## 0.2.0.dev104

- Add a gentle 2.4-second amber pulse to Unlock signing inside the locked wallet card. The glow does not change its size or position, and reduced-motion preferences disable it. The action and pulse disappear after the existing unlock flow succeeds.
- A synthetic browser preview verified the changing glow, stable dimensions, reduced-motion behavior and password unlock. Frontend build/lint passed; no real wallet or funded transaction was used for this preview.
- Integrate compact SOL balance and complete/partial known wallet value with Deposit and Withdraw into a header card immediately left of Guides. Add a larger Sign out tile to the right, retaining the existing wallet dialogs and lock behavior.
- Remove the four-metric header strip and the wallet action/version toolbar. Put Dashboard navigation beside the title, keep the actual version in System status, and retain P/L and 24-hour worth movement in Dashboard Overview/Performance.
- Keep partial-value explanations and accounting/history requirements. On narrow screens, stack the balance card and show Guides and Sign out together. Fifteen focused synthetic browser checks, frontend build/lint and six release-note/packaging checks passed. This local candidate has an unpublished Community draft.

## 0.2.0.dev103

- Move Unlock signing into the restored wallet card beside its name, address, network and signing-locked state. Remove the separate header signal. The card action opens the existing wallet unlock dialog and disappears after signing becomes available.
- Keep the card usable on narrow screens and retain walletless Connect wallet, provider identity and existing password/extension proof flows. Five focused synthetic browser checks passed for desktop/mobile layout, walletless keyboard access, startup failures, password unlock and Wallet Standard ownership. Frontend build/lint passed; the existing large-chunk advisory remains.
- This is a local development candidate with an unpublished Community draft. No full repository suite or funded transaction is claimed. Signing, provider approval and production execution limits remain unchanged.

## 0.2.0.dev102

- Condense disconnected AI setup to one sentence and Add API key. Remove the separate provider/footer strip and place New chat and AI settings icon controls beside the connected message field, including in the detached view. Provider details remain in status history/settings and sending still requires approval.
- Eight focused synthetic Chromium Dashboard/chat/window checks passed, including draft preservation, local settings, approval/cancellation, narrow-window fit and closure/return. Real Firefox 140.16.0 popup checks, frontend build/lint and six release-note/packaging checks passed. No real provider request or full repository suite is claimed.
- Open background service controls from the System status card. Remove the separate Background button and retain service health, offline/restart/mismatch warnings and the active-signing emergency action.
- Replace the header optional AI shortcut and the toolbar Guide button with a larger Guides tile. Match its height to the status/wallet cards on desktop and stack it on narrow screens. Keep AI connection and settings in System activity & AI chat.
- This local development candidate has an unpublished draft Community note. Fourteen focused synthetic browser checks, frontend build/lint and six release-note/packaging checks passed; signing/approval gates and production execution limits are unchanged.

## 0.2.0.dev101

- Fix the detached activity/chat window opening and immediately closing in Firefox. Keep its initial document instead of navigating to a second blank page, and restore the inline view only when the popup is actually closed or its document is replaced. Preserve drafts, approvals, native-close recovery and main-Dashboard lifecycle controls.
- Six focused Chromium window scenarios and a real Firefox 140.16.0 regression check passed for startup persistence, connected portal/styles/history, AI settings focus, return, collapsed detachment, native close and Dashboard reload. The Firefox check uses a new profile and isolated synthetic data.
- Move wallet identity beside System status after the PumpWizard title. Show the actual wallet provider/name as connected when signing is available, and a restored view with signing locked after reload. Keep confirmed SOL, wallet valuation, P/L and 24-hour movement in a full-width metrics row below.
- Replace Core systems available with the reported Runner version and running/checking/unavailable/service state. Show verified service health and RPC status, identify failed startup checks and flag restart recovery or version mismatches. Offline, stopped or unverified service states remain visible as needing attention.
- Retain the Dashboard holdings/tools, Background controls and detachable activity/chat view. This local development candidate keeps its Community note as a draft; it does not refresh stale prices, reconstruct missing purchase-time FX or change signing/approval gates.
- Eleven focused synthetic browser checks, frontend build/lint and six release-note/packaging checks passed. RPC verified describes the latest startup/configuration check, rather than a guarantee of fresh token prices or trade execution.

## 0.2.0.dev100

- Put the System status card directly after the PumpWizard mascot/title section. Keep actionable Background, recovery, signing and optional AI controls aligned on the right. On narrow screens, stack the status card beneath the title before the actions.
- Retain partial wallet values and their missing-price explanations, the Dashboard tabs and detachable activity/chat view. Eight focused synthetic browser checks, frontend build/lint and six release-note/packaging checks passed. This is a local development candidate; its Community note remains a draft.

## 0.2.0.dev99

- Add Open in new window beside System activity & AI chat, available while expanded or collapsed. Move only the live activity/chat section into a movable, resizable companion window, preserving its activity history, message draft and pending provider approval.
- Keep AI provider settings and their keyboard focus in the companion window. Service and strategy links focus their existing Dashboard controls. Return to Dashboard, Return here or native window closure restores the section; blocked pop-ups show a recovery message without losing the inline view.
- Closing or reloading the main Dashboard, signing out or changing wallet scope closes the companion window. Keep the main Dashboard open while using it. Detachment does not expand AI capabilities or change signing and trading permissions.
- Five focused window scenarios, seven existing dashboard/chat/wallet-isolation browser scenarios, frontend build/lint and six release-note/packaging checks passed. This is a local development candidate; no funded transaction, complete repository suite, public release or Community publication is claimed.

## 0.2.0.dev98

- Show known SOL and freshly priced holdings as an explicitly partial wallet value when a complete total cannot be calculated. Explain stale token prices, missing prices and coverage gaps. Complete worth history and 24-hour movement still require a complete valuation; partial subtotals are not recorded as full wallet values.
- Explain unavailable open-position USD P/L using the retained purchase cost, purchase-time SOL/USD rate and price freshness. Distinguish incomplete current valuation from missing 24-hour history.
- Replace the toolbar Service button with a clickable Background status indicator showing checking, idle, monitoring, trading active, needs review or offline. Preserve the existing service dialog, recovery controls and visible Pause or stop service action during active signing.
- Eight focused synthetic browser checks, frontend build/lint and six release-note/packaging checks passed. This local candidate does not refresh stale token marks into fresh quotes, reconstruct missing purchase-time FX, authorize execution or publish its Community draft.

## 0.2.0.dev97

- Combine the command board, connected-wallet identity, balance/valuation metrics and workspace actions in one full-width header. Keep system activity and optional AI chat in an expandable section.
- Move Portfolio into Dashboard's Holdings tab beside Overview, Positions, New Order and Trade History. Retain holding refresh, valuation context, purchase attribution, mint copying, explorer links, price history, evidence and reviewed sales. Session and strategy views remain available through the session tree.
- Wrap the unified header on small screens and show holdings as compact cards when the dashboard panel is narrow. Dashboard stays selected while inspecting holdings; the overview's View holdings action opens the same tab.
- Eleven focused synthetic browser checks, frontend build/lint and six release-note/packaging checks passed. This local candidate does not publish its Community draft or enable production execution; no funded transaction, complete suite or OS reboot is claimed.

## 0.2.0.dev96

- Open Dashboard on a portfolio overview with estimated wallet worth, confirmed SOL, open-position P&L, running-strategy counts and compact holdings. List every strategy with monitoring, entry/exit modes and its recorded positions; filter to running strategies or open its detailed controls.
- Add keyboard-inspectable wallet-value and open-position P&L charts with 24-hour and seven-day views. Store wallet/network-scoped observed snapshots on the local server, using fixed 15-minute buckets and seven-day retention. Reuse prior server worth history without inventing historical P&L; show missing/stale data and time gaps explicitly.
- Preserve detailed Positions, New Order and Trade History tools. Narrow the overview session sidebar and wrap its controls; stack the overview before sessions on mobile. Wallet-value movement includes transfers; P&L is unrealized for open Runner positions before exit costs, rather than an all-time profit claim.
- Focused storage and browser checks cover backup/restore, malformed history rejection, wallet switching, navigation, graph inspection, mobile layout and existing entry-pause controls. This local candidate does not enable production execution or publish its Community draft.

## 0.2.0.dev95

- Compact the wallet shortfall into a single collapsed line with balance details, Add SOL and Edit funds actions. Show each session’s recorded open positions and exit settings, with View, Review sells and other-wallet-position navigation.
- Pause new entries while retaining monitoring, exit modes, active exit authorizations and unsigned sell reviews. The sidebar now says Pause entries. Manual liquidation and allocation edits still explicitly stop strategies before transferring exit ownership or changing funds.
- Require a fresh successful funding check before resuming reviewed or automatic entries. Refilling keeps entries disabled. Cached buy tickets are discarded after a durable entry pause; position summaries retain removed-strategy attribution and tolerate missing market observations.
- Focused backend and desktop/mobile synthetic browser tests passed. Production execution remains blocked; no funded transaction, full repository suite or public Community publication is claimed.

## 0.2.0.dev94

Check confirmed wallet SOL against all remaining cash reservations every 15 seconds in the background service. An external spending shortfall pauses all trading sessions for that wallet/network, disables monitoring and automatic orders, cancels unsigned agent reviews, and invalidates signing authorizations without releasing allocations or positions. Sessions show the wallet balance, required reserved cash and shortfall. Restarting requires a fresh successful balance check; refilling never resumes activity automatically. Pending local transactions defer the comparison until receipt accounting settles; RPC failures show unavailable availability instead of a zero balance. Focused synthetic backend and browser checks are recorded separately; no funded trade or public release is claimed.

## 0.2.0.dev93

- Move Private key or recovery phrase beside Restore encrypted backup in the wallet-selection footer. Use matching text actions and wrap them on narrow screens.

## 0.2.0.dev92

- Rename recovery options to Advanced recovery — address doesn’t match? and explain when to use them. Keep the section collapsed initially.
- Select saved-wallet recovery accounts as Account 1 through Account 100, retaining the existing internal indices 0–99 and default account.
- Show the friendly account number during address review and place the raw derivation path in collapsed Advanced recovery details. Clarify that the original recovery passphrase and the later PumpWizard password have separate purposes.

## 0.2.0.dev91

- Add private-key and recovery-phrase sign-in with a public-address review before saving or connecting. Accept complete 64-byte base58/JSON Solana keypairs and checksum-valid English BIP39 phrases.
- Support common hardened Solana derivation paths, account indices 0–99 and an optional BIP39 recovery passphrase. Require a new encryption password for a new saved account.
- Save only the password-encrypted wallet envelope in the local server account database. Keep recovery phrases, private keys and passwords out of persisted browser storage and account API payloads. Preserve an existing account’s ciphertext and password when signing in with the same key.
- Clear secret entry fields after review and on cancellation, wipe transient key buffers on cancellation/failure/sign-out, retain locked signing after reload, and prevent the floating RPC button from covering onboarding actions on narrow screens.

## 0.2.0.dev90

- Store generated wallet encrypted backups and saved account metadata in the private local server database, independent of browser profile and loopback port. Passwords and decrypted signing keys remain transient.
- Move durable workspace preferences, worth history and retry identities to the same server store. Include accounts and preferences in existing service backup/restore.
- Migrate legacy browser accounts and preferences transactionally; remove browser copies only after the server commits. Keep existing accounts on conflicting or invalid imports and show storage failures without claiming a successful save.
- Preserve existing strategy formats, wallet ownership checks and execution gates. Restart does not unlock signing.

## Unreleased — 0.2.0.dev89

- Click the session’s allocated SOL or Edit session funds to top up its shared wallet reservation or release unused cash. Show allocated funds, available trading cash, managed position costs and a preview of the change.
- Revalidate the wallet balance and reservation revision while holding the wallet writer lock. Require paused strategies and inactive signing, retain spent/position funds, reject pending orders and transfers, and audit successful changes. Keep per-order and daily caps unchanged on top-up; reduce caps only when necessary to fit a smaller allocation.
- Add Observations beside Positions and Trade History for a selected strategy, and aggregate the selected session’s strategy observations. Adding a strategy selects its named dashboard panel automatically; creating a trading session selects its Overview.
- Preserve calculated P&L, receipt attribution, wallet isolation and existing authorization gates. No funded transaction or production activation was performed by this update.

## 0.2.0.dev88

- Selecting a session opens an Overview of its holdings and recent reviewed trades grouped by strategy, with Positions and Trade History tabs. Selecting a strategy opens only its positions and history; use Strategy controls for monitoring and configuration.
- Separate folder expansion from selection, highlight the selected session/strategy and add navigation back to its parent session or all wallet activity. Keep New Order in the wallet view and reset selection when the wallet or network changes.
- Attribute pending and failed orders by their recorded strategy ID, and retain attribution for manual sales of strategy positions. Filter by exact IDs rather than token mint or display name.
- Retain removed strategy labels and assignment membership for session history and all-time P&L. Archived strategies stay visible in history without active strategy controls. Active IDs and execution paths are unchanged.

## 0.2.0.dev87

- Widen the session sidebar and combine each session’s name, allocated SOL, all-time P&L, Pause all, Sell all and Add strategy controls into one row. Keep controls accessible when the folder is collapsed.
- Connect strategy and open-position rows with indented tree lines. Show recorded live positions under their owning strategy, including user-managed positions with retained purchase attribution; deduplicate separately tracked holdings.
- Clicking a position opens and focuses its dashboard details without requesting signing. Format live quantities using token decimals and show an unavailable quantity when decimals are unknown.
- Retain a single session row with local horizontal scrolling on narrow screens, and fit position details beside the wider desktop sidebar. Trading permissions and execution gates are unchanged.

## 0.2.0.dev86

- Add Pause all and Sell all controls inside each session folder. Pause stops strategy tracking and automatic entry/exit modes, locks matching runtimes and cancels unsigned strategy reviews while retaining positions and allocation.
- Show allocated SOL and all-time live session P&L, including realized and fresh retained unrealized results and recorded failed transaction fees. Missing basis or stale prices withhold the total; external/manual and simulated positions are excluded.
- Sell all lists only the selected session’s live positions, pauses strategies, explicitly hands managed positions to manual exit and opens a full-quantity reviewed sale. Every sale requires approval; pending receipts block the next sale. Purchase attribution persists.
- Retain wallet/network isolation and the direct strategy attachment/editor improvements prepared in dev85. No funded acceptance or production activation is added.

## 0.2.0.dev85

- Combine the strategy title, session allocation, source tabs, Create and Import in one compact header. Rename sources My Strategies and Public Lab Data.
- Add public and saved strategies directly to the selected session and immediately open their editor. Replace the two-step save-then-add action with Add to session & edit.
- Save rules and order limits back to the same newly attached inactive strategy; preserve source packages and omit inherited simulation results for edited rules. Reject changes after monitoring/order activity, wrong-wallet targeting and stale source revisions.
- Keep original public templates marked as already added after a session revision. Adding and editing do not start monitoring or trading.

## 0.2.0.dev84

- Split the saved strategy editor into Budget & trades, Entry rules, Confirmation, Exits & targets and Advanced. Increase field contrast and text size, remove number spinners and simplify the budget summary.
- For trading sessions, calculate strategy budget from the durable allocated SOL and current server-side SOL/USD reference. Replace editable template USD budget/order amounts with a trade percentage and visible SOL/USD results. Keep explicit simulated budgets for monitoring without wallet funds.
- Save allocation sizing context separately from the exchange package and apply its SOL amount as the initial maximum buy when adding the revision. Reject a higher submitted buy cap or changed allocation; retain percentage context across reloads.
- Keep USD strategy values as setup snapshots, with no automatic rebalance or new signing authority. Strategies still share the session allocation.

## 0.2.0.dev83

- Edit saved early-ladder strategies inside the wallet library using labeled entry, sizing, signal, exit, target, quote and simulation-cost fields. Display percentages and useful time units while preserving the existing package units.
- Save edits as a new wallet-scoped revision, preserving original packages and assigned session rules. Do not copy attributed simulation reports to changed rules. Validate edited packages before storing them.
- Edit initial SOL buy/loss limits, daily buy count and maximum open positions before adding the strategy. Apply them to the next strategy assignment without changing existing authorizations.
- Give the editor the full picker width, retain mobile scrolling and keep strategy-format identifiers read-only. Other package formats remain available through export.

## 0.2.0.dev82

- Store saved strategy membership by wallet and network. Keep known legacy ownership; leave unassigned packages in Research. Export/import explicitly copies a strategy into another wallet’s library without changing its exact package.
- Combine saved strategies, predefined simulations, creation and JSON import in one picker. Remove Current/History and keep attributed simulation evidence alongside exact rules.
- Mark strategies already added to the selected session and reject duplicate assignment atomically in the backend. The same strategy may still be added to another session.
- Preserve existing session records and duplicate historical assignments. Saving or adding a strategy does not enable trading.

## 0.2.0.dev81

- Scope dashboard sessions, assigned strategies, order-control lists and activity by wallet address and network. Monitoring sessions created while connected record that wallet as their owner without allocating funds.
- Store AI conversations per wallet/network and reject mismatched conversation reads, deletion and pending approvals. Keep the provider connection shared across this local installation.
- Reset wallet-specific views on switching and discard late responses from the previous wallet. Switching back restores its saved sessions and chats.
- Keep inactive review drafts with their known source wallet without copying funds or authority.
- Preserve older unowned sessions and chats in wallet-free Research; infer legacy strategy ownership only from existing wallet policies. Shared strategy packages remain reusable library templates.

## 0.2.0.dev80

- Finish new trading-session setup at allocation: mode, approval type, then balance. Create the empty funded session without automatically opening the strategy library.
- Show the session’s assigned SOL in the sidebar tree. Add saved strategies explicitly from that session’s existing library action.
- Remove the sizing, horizon, risk and recommendation steps from new trading setup. New trading sessions add saved strategies directly without the separate research-brief questionnaire or automatic monitor/service popup.
- Keep initial order limits inspectable in the library and editable through Order safety. Preserve shared allocation, exact strategy rules and separate activation gates; do not record removed questions as user preferences.

## 0.2.0.dev79

- Append session-created, strategy-added and entry-decision events to the command board without requiring an AI provider.
- Show strategy/token, signal or skip, reason and event time; strategy activity opens its monitor.
- Read retained entry decisions with an authenticated cursor API, page bursts in insertion order, retry failed reads and deduplicate immutable events across routine polls.
- Keep creation and activity updates scoped to the current dashboard visit; opening the board does not replay old setup as new activity or grant trading authority.

## 0.2.0.dev78

- Make background service the sole runtime choice in new trading setup. Interactive orders still require wallet approval; new automatic modes bind service custody and require separate enrollment and activation. Preserve existing browser-custody sessions.

- Replace session cards with a compact folder-style list: expand/collapse sessions, select strategies and add strategies inline.
- Remove repeated state, balance and empty-session guidance from the rail; retain status indicators, tooltips and existing strategy action menus.
- Preserve expansion and strategy selection across ordinary data refreshes; support keyboard disclosure controls and visible focus.

## 0.2.0.dev77

- Offer Automatic entries only in session setup: bounded strategy buys with user-directed sells, with no automatic stop-loss, profit-target, trailing or timed sells.
- Bind the same buy-only permissions in browser and service allowances and keep exits manual through start, pause and resume. Preserve existing exits-only records and Full Autopilot semantics.

- Replace the funding receipt's Check confirmation button with automatic, sequential status checks and a loading indicator.
- Show confirmation/finality, failure or expiry without a manual refresh. Stop dialog polling on terminal results, close, or after a bounded two-minute wait; retain unresolved recovery controls.
- Keep receipt monitoring read-only with respect to transaction submission. No transfer is signed or resent by confirmation checks.

## 0.2.0.dev76

- Add an explicit expired-transfer recovery action beside blocked funding and uncertain transfer receipts.
- Release only the exact original SOL transfer intent after two independent RPC hosts verify network identity, finalized expiry, absent historical signature status and absent finalized transaction.
- Retain the original receipt with an expired state and an atomic recovery audit; preserve locks on incomplete checks, provider errors, existing signatures, identity mismatches or changed receipts.
- Return to a fresh user-reviewed funding action without signing, resending, or changing automation gates.

## 0.2.0.dev75

- Rename the funding action to Fund wallet, retain the review step, and show blocked-preview errors beside the action instead of below the visible modal.

- Treat automation funding input as a total spending budget, deduct the reviewed network fee and preserve at least 0.002 SOL (or a higher rent minimum) plus existing allocations in the source wallet. Max uses the same server-side calculation.
- Retain transfer amounts, fees, projected balance, signature and source/destination after submission. Add an explicit confirmation check and explorer link.
- Return the actual journal submission state instead of implying a returned signature means successful broadcast. Keep uncertain outcomes blocked from repeat funding; retain credential-free RPC error codes for future diagnostics.
- Preserve ordinary withdrawal amount semantics and all exact-message, balance-change, reservation and automation activation checks.

## 0.2.0.dev74

- Match automation wallet setup and transfer review to the session wizard, with clear progress and responsive wallet cards.
- Show saved and selected wallets' public SOL balances, SOL-only USD estimates, and expandable SPL / Token-2022 quantities. Missing RPC or price data remains visibly unavailable.
- Enter the funding amount directly in setup, with 25% and 50% balance shortcuts and a separate exact transfer review before signing.
- Keep public balance reads separate from wallet authentication, private journal/history and execution authorization. Preserve cancellation, exact signature checks and uncertain-submission protection.

## 0.2.0.dev73

- Offer an automation-wallet setup from the trading approval step for extension-wallet users.
- Create and back up a generated wallet or choose a saved one, verify destination access, optionally review a SOL transfer, and continue the same session wizard using that wallet.
- Keep funding signed by the source extension, separate from wallet selection and automation activation. Prevent repeat funding in the same setup after uncertain submission.
- Include explicit compute-unit limit and zero priority fee in SOL transfer reviews so extension fee autofill does not alter the reviewed message; retain exact signature/message checks.
- Reload allocation figures on wallet identity changes without overwriting session input on routine wallet polls.

## 0.2.0.dev72

- Choose Observations or Paper trading under Sessions → + → Monitor; retain the selection with the inactive session.
- Move saved entry decisions into the owning session monitor and add a session Paper trading action. Keep the main dashboard focused on positions, orders and history.
- Open new paper sessions directly at paper controls and use the exact current source strategy without requiring a frozen research dataset first.
- Preserve explicit paper start, pause and stop, zero wallet authority for monitoring, and generated-wallet-only automation restrictions.

## 0.2.0.dev71

- Keep the command board’s initial status snapshot and append timestamped rows for meaningful changes in the current tab.
- Ignore heartbeat, successful scan timestamp and quote-count changes; keep polling for real transitions.
- Stop rerunning startup checks when wallet polling supplies a new object with unchanged identity and signing state.
- Preserve earlier rows and the reader’s scroll position; keep the header summary and service action tied to current state.

## 0.2.0.dev70

- Simplify Service to readable monitoring and background-trading status with Start/Stop monitoring controls.
- Collapse trading setup, technical identifiers, provider diagnostics and service shutdown behind named sections.
- Keep active trading pause/stop controls, monitoring errors and unresolved order outcomes visible.
- Explain extension-wallet background-signing limits in plain language; retain all existing authorizations and confirmations.

## 0.2.0.dev69

- Restore the saved pre-redesign layout: command board on the left, wallet and Dashboard/Portfolio navigation on the right, sessions and trading tabs below.
- Keep AI chat and provider settings inside the persistent command board; remove the separate System & AI navigation and duplicate dialog.
- Keep Research & reports as a secondary link, with the current wallet valuation and background-service controls intact.
- Restore Positions on wallet reload and refresh retained market data on the dashboard. No signing or execution permissions change.

## 0.2.0.dev68

- Restore a connected-wallet Dashboard with visible SOL balance, wallet actions, session controls, AI chat and the notification board.
- Keep walletless Research available without making it the default for connected wallets.
- Show AI settings even without an active provider key and retain locked signing after reload.
- Default launch.sh to the background-service dashboard rather than the legacy browser-only server.
- Preserve all execution gates; this UI and local-service repair submits no funded transaction.

## 0.2.0.dev67

- Keep the bounded PumpSwap observation active through its deadline after the provider request ceiling stops new reads.
- Cancel newly prepared unsigned reviews when ticket issuance rejects them, preserving submitted intent and freeing shared cash reservations.
- Separate current strategy management from historical purchase attribution after handback without refunding unreturned principal.
- Remove three unreachable frontend components and their exclusive market-selection styles; preserve live recovery APIs.
- Package the September 30 repairs under a new identity. General production activation remains blocked; actual observation and funded acceptance are separate gates.

## 0.2.0.dev66

- Wire background-service Autopilot creation to the exact new session and keep walletless service health, jobs and coverage visible without granting wallet or signing authority.
- Attach backend-owned `pumpwizard.holding-valuation/v1` records to session and wallet trade positions; Activity and Holdings now withhold stale/unknown value instead of recomputing accounting in the browser.
- Show exact unsigned service reviews revoked by Pause/lock separately from submitted signatures that still require public reconciliation.
- Revalidate canonical PumpSwap curve/AMM reserves immediately before signing, so even favorable quote-state changes require a fresh review.
- Accept the documented shared workspace cash owner during service-acceptance validation while preserving exact hold, wallet, strategy, policy and allowance bindings.
- Label canonical PumpSwap observations as read-only and non-executable, distinguish route-unavailable from an empty universe, and keep the first pilot subset limited to native-SOL standard-SPL creator-reward pools.
- Extend deterministic browser acceptance through encrypted-wallet restore, service custody remaining locked, unsigned-review cancellation, browserless entry/full exit, unresolved-signature restart recovery and fresh authorization.
- Verify the settled 1,086-test Python collection with 1,085 passes and one intentional local-validator skip, repository Ruff/diff integrity, frontend lint/typecheck/build, focused authenticated API checks and all 54 serial browser journeys. The existing Vite main-chunk advisory remains.
- Production activation remains blocked. The 72-hour operation, funded manual/service lifecycles, real-key migration, installed-service replacement and Community publication were not authorized or performed.

## 0.2.0.dev65

- Create inactive monitoring workspaces without wallet authority and durable trading workspaces with one exact shared SOL allocation before strategy selection.
- Bind multiple strategies to the trading session's cash hold under independent trade, loss and position caps without reserving the same SOL again. Shared-pool settlement updates available cash while retaining per-strategy position ownership and evidence.
- Show total, available and open-position cost basis on Active Sessions; allow Add strategy to reopen the immutable trading plan for the exact owning wallet and network.
- Add canonical PumpSwap exact-quote buys alongside supported sells, including signed effective quote reserves, wrapped-SOL setup/close, standard-SPL and Token-2022 account variants and offline official-SDK instruction comparisons.
- Add wallet-signed, exact-build/route/allowance service-acceptance controls while leaving ordinary source and production service activation blocked.
- Validate one canonical standard-SPL PumpSwap buy from the exact packaged executable through two zero-error unsigned mainnet simulations with submission disabled and no wallet access.
- Exercise exact-package acceptance with a disposable generated wallet: authenticate, enroll, review/authorize, sign the build/route/allowance message, activate once with no service job or order, verify the empty audit, then restart locked.
- Verify the complete 1,048-test Python collection with 1,047 passes and one intentional local-validator skip, repository Ruff/diff integrity, frontend lint/typecheck/build and focused production-bundle monitoring/trading/service browser flows. The Vite main-chunk advisory remains.
- This is a local candidate. The packaged activation used a disposable unfunded wallet and placed no order. No funded transaction, real-key migration, installed-service replacement, public Community publication or production automatic-execution promotion was performed; the deployed buy result is one unsigned standard-SPL simulation, not a fill or general route claim.

## 0.2.0.dev64

- Let a launch-token-authenticated user enter the retained Research workspace without creating, restoring or connecting a wallet. The capability contract distinguishes local authentication, wallet identity, read-only research, optional AI and production execution; opening Research performs no wallet scan, provider request, collection start, signing request or submission.
- Add explicit monitoring workspaces and a task-first Research home with compact service, retained-data and session state. Research, Holdings and Activity are now the primary destinations; service controls, diagnostics and optional AI remain focused tools, and wallet connection is requested only at a wallet-dependent action.
- Add immutable 24-hour observation presets, resource ceilings, operational summaries and JSON exports with explicit pause, restart, stale, empty, partial, outage and stopped states. The deterministic rehearsal advances a simulated 24-hour clock in seconds and is not evidence of actually elapsed uptime or strategy edge.
- Bind evidence explanations to typed supported facts and content-hashed retained records. Holding valuation keeps missing basis, stale marks, unsupported precision and unknown executable value distinct instead of accepting provider prose or recomputing financial claims in the browser.
- Tighten local lifecycle and recovery: the launcher opens or reuses the authenticated dashboard without making browser close mean service shutdown; fresh state/token permissions are private; backups omit access tokens, logs, locks and plaintext wallet, automation, AI and settings artifacts.
- Preserve service-custody restart locking, explicit campaign Resume, manual reviewed trading and every production execution block. Existing workspace strategies retain monitor, research, order, rename and removal controls inside the new session grouping.
- Validate the merged 1,032-test Python collection with 1,031 passes and one intentional local-validator skip, repository Ruff, frontend lint/typecheck/build, deterministic recovery and all 54 production-bundle browser journeys used by the private pilot. The Vite bundle-size warning remains.
- This is a local Debian x86-64 private-pilot candidate, not a public release. No real novice study, actually elapsed 24-hour observation, paid provider call, real-key migration, funded action, production automatic trading, user-service installation, reboot, Community publication or other-platform acceptance was performed.

## 0.2.0.dev63

- Restore usable Pump.fun curve discovery after the provider's selected exact-mint detail reads began returning HTTP 404. Normalize the complete supported listing snapshots directly while retaining exact mint, curve, decimals, reserves, provider `updated_at`, SOL/USD time and the existing freshness/liquidity/quality gates.
- Add bounded provider-stage diagnostics for listing, selection, normalization, rejection, request, payload-byte, elapsed-time and concurrency outcomes. Retain them with service coverage and campaign provider history; status reads remain inert and partial, stale, empty, failing and quota-exhausted attempts stay distinct.
- Keep forward-campaign source identity frozen. Campaigns share a provider read only when provider, market-cap band, priority universe and limit match exactly; agent defaults or another campaign can no longer silently widen retained evidence.
- Exercise the existing evaluator with an isolated keyless live campaign: five healthy three-quote scans produced 15 eligible observations per frozen arm, bounded `entry_blocked` decisions, no evaluator gaps/faults/exclusions and an explicit stop. The 7.2-second sample is exploratory only; provider price remains unknown and no profitability claim is supported.
- Add a local evidence assistant for exact retained campaign/report/decision/holding/receipt records with content-hashed citations, known/unknown state, bounded local explanations and adversarial numeric/citation checks. Optional AI remains previewed, frozen, explicitly approved and without tools or wallet authority.
- Add opaque source/package build identity, an isolated Linux candidate workflow, exact executable desktop entry, version/data/service mismatch refusal and backup/restore rehearsal without installing a user service, enabling linger or replacing the current app.
- Preserve strategy exchange formats and every trading boundary. No paid provider, signer, order, funded transaction, public Community publication or unattended production activation is authorized by this build.

## Unreleased — 0.2.0.dev62

- Replace the mandatory market-selection screen after wallet sign-in with a skippable, version-aware welcome flow. First use shows What's new, practical safety tips and project links; a changed version shows only What's new; ordinary returns open Dashboard. Pump.fun remains the internal supported context and Guide reopens the introduction.
- Read What's new from the exact bundled Community note and label draft/local development status accurately. Acknowledgement stores only the version and a storage failure cannot block Dashboard.
- Add local Session Review for complete `ladder-v1` evidence: append every evaluated opportunity and its decision-time features atomically with monitor cycles, freeze exact-digest datasets, compare bounded one-parameter candidates with chronological train/validation/holdout splits and explicit modeled costs, and preserve sparse or historical evidence as collect-more-data.
- Allow an explicitly selected supported review candidate to create a new paused inactive session through existing package validation. The source remains unchanged; monitoring stays off, entries stay disabled, exits stay manual, wallet allocation is zero and execution authorization is not copied.
- Integrate the generated main-wallet Autopilot allowance with authenticated browser API routes, immutable v3 automatic-action scopes, exact policy/runtime revisions and a constrained browser signing-ticket path. Exit-only keeps user-signed entries and automatic managed exits; full Autopilot permits bounded entries and exits. An exit-first scheduler is wired only through the explicitly injected synthetic acceptance runtime; normal production activation remains hard-blocked.
- Add an explicit v4 `main-wallet-service` custody choice backed by a separate encrypted service vault, bounded allowance identity and restart-locked signer. Existing v1–v3/browser custody remains unchanged and extension wallets stay interactive.
- Add a single-owner authenticated loopback background service with start/status/stop/open-dashboard commands, persistent opt-in monitoring, opaque instance health, verified-service reuse, foreign-port refusal and staged version-pinned Linux user-unit installation/removal without enabling linger or starting a user service.
- Move service-owned periodic scans out of Dashboard refreshes and retain bounded raw evidence with event, provider and first-receipt times, source/route/pool attribution, idempotent fingerprints, explicit gaps, backoff and retention. Dashboard polling is status-only.
- Integrate wallet-authenticated service enrollment, allowance review/authorize/start, pause/exact-resume/revoke and shutdown routes. The default runtime cannot start production signing; the synthetic engine test reconciles and submits only an exact reviewed ticket without a browser heartbeat.
- Reconcile already recorded service signatures after restart without reconstructing signing authority. Cover a finalized synthetic entry, 40% partial exit, final exit, lost broadcast response, locked restart, fresh review/authorization/password Start, exact order usage and zero residual strategy position.
- Add offline integrity-checked service backup/restore using SQLite snapshots and ciphertext-only vault records. Restore is same-version and new-directory-only, omits access tokens/logs/locks and starts signing locked.
- Tighten the real-service browser acceptance so the fixture gates entry until every browser context is closed, finalizes entry and exit without browser polling or heartbeat, reopens the same receipts, restarts locked and requires fresh authorization before Start.
- Preserve legacy session briefs byte-for-byte and keep manual/funded trading gates unchanged. Wallet lock or disconnect invalidates the ephemeral allowance runtime before the wallet session closes.
- Refresh durable trade reconciliation before rebuilding Portfolio holdings, preventing a stale post-restart position from briefly disabling an otherwise covered manual exit.
- Verify the complete 842-test Python collection (841 passed, one intentional skip), repository Ruff/diff integrity, frontend lint/typecheck, the production web build and all 41 production-bundle Playwright flows. A 1,800.2-second isolated soak recorded 100 successful scans, 18 explicit outage gaps, two service instances and 100/100 unique evidence fingerprints; isolated SIGKILL recovery, CLI backup/restore and version-pinned `/tmp` unit staging also passed.
- No funded mainnet transaction, real extension approval, production Autopilot/service activation, user-service installation, public Community publication or dev62 binary package was completed.

## 0.2.0.dev61

- Replace command-board service/session counters with compact, actionable state signals for local reload, RPC repair, signing unlock, session review and optional AI setup. Keep New chat and AI settings available in the low-priority footer.
- Run a visible read-only startup sequence after sign-in. Report Runner/database readiness, initial Mainnet RPC identity and blockhash, connected and known wallet counts, retained market state, loaded/running/starting/problem session counts and active automation-run records. Remove generic Automation Wallet, Diagnostics and RPC controls from Portfolio; expose RPC repair only when its startup test fails.
- Classify exact-mint Portfolio valuation evidence as priced, unpriced, stale, illiquid, suspicious or likely worthless. The strongest warning requires a sub-cent holding value, under $250 reported liquidity and at least one additional weak market-quality signal; missing data never becomes zero.
- Fill held-token identity gaps from bounded exact-mint public profiles while preserving locally retained Pump.fun/PumpSwap metadata precedence, same-origin image proxying and copyable mint addresses.
- Add an inline **Trade History** Dashboard tab for durable Runner-reviewed buys and sells, including requested amount, attributed user/session source, receipt settlement, fee, slot, state, timestamp and explorer link. External wallet activity remains outside this accounting view.
- Add four retained-data strategy lanes to **Observations**: Short-squeeze Watch, Day Trading, Longer Hold and Unicorn Hunter. Each shows its bounded score and evidence reason and opens the existing paper-only strategy wizard with that profile selected.
- Keep low-coverage suggestions explicitly low confidence. Short-squeeze Watch remains a price-compression breakout screen because Runner has no short-interest or liquidation feed.
- Preserve review and authorization boundaries: strategy creation remains paper-only, restored signing requires the wallet password, manual orders require review/signing and automatic execution is not enabled.
- Verify 775 Python tests with one intentional skip, browser lint, the production build, a zero-vulnerability production dependency audit and all 34 production-bundle browser flows. The browser suite covers the Dashboard/Portfolio workspace, enriched holdings, populated Trade History, strategy-profile handoff, command signals, restored signing, responsive charts and transaction-integrity safeguards.
- No funded mainnet transaction, real extension approval, unattended submission, public Community publication or dev61 binary package was completed.

## 0.2.0.dev60

- Remove the general token-market monitor, including its search, filters, sorting, watchlist rows and sparklines. Trade now contains the live Open Positions dashboard, direct **+ Buy Order** action and an Observations view limited to the current sessions' saved enter or do-not-enter decisions.
- Rework the wallet panel to fill the command-board header height. Order its navigation as Dashboard, Portfolio, Deposit, Withdraw and Lock; make the balance card read-only; and add bounded USD, comparable Runner P/L and local 24-hour worth-movement summaries that remain unavailable without complete evidence. Remove the generic Wallet tools menu from Portfolio.
- Keep token detail with selectable retained price, market-cap and liquidity charts, exact mint copy, current metrics and local strategy activity, opened directly from an actual position.
- Refresh public market data every 20 seconds while the visible Trade workspace is open, reload local rows and session state every 5 seconds, refresh wallet trade records every 8 seconds, and enforce a 20-second server throttle so overlapping tabs do not duplicate provider scans.
- Page through all retained token snapshots from the detail chart. Live public scans replace the current five-minute bucket; after seven days retained history is reduced to hourly points and after 90 days to daily points to bound database growth.
- Automatically load resumable, attributed 4-hour USD price history from a keyless public source while token detail is open. There is no account, credential, provider setup or manual paging control for the remote import.
- Merge historical closes into the price chart before recent local observations while keeping market-cap and liquidity charts local. Public candles never become execution quotes, route evidence or strategy-risk inputs.
- Retain a sanitized subset of Pump.fun and PumpSwap project metadata: name, description, creator, verification state and HTTPS project links. Arbitrary metadata documents and remote images remain unfetched.
- Redesign manual trading as an order ticket with connected-wallet context, explicit SOL/token/basis-point units, open-position selection, recent-order status and a concise wallet-signing summary.
- Restore a compact Active Sessions rail beside the trading dashboard. Each row keeps its state, latest scan age, open-position count and saved entry-candidate count visible without opening the action menu.
- Replace the flat strategy marketplace list with a clear choice between creating local rules and importing from attributed PumpWizard web simulations.
- Add an interactive simulation library with search, family/result/compatibility filters, performance windows, sorting, pagination, attributed evidence metrics, reported-window comparison bars, exact rule summaries and review-before-import.
- Redesign the local strategy library as a searchable master/detail workspace with readiness filters, session counts, exact entry and risk-rule summaries, a three-step inactive-session explanation, and consolidated identity, export and session actions.
- Replace the sparse session-activity dialog with an operational strategy monitor showing explicit Run/Stop and Scan controls, the watched market-cap band and entry signal, wallet and strategy loss ceilings, current positions, latest observed tokens, scan history and recent decisions. Add the same status, watch range, loss and position summary to each Trade-rail strategy menu.
- Expand each live open-position card with full mint identity, explicit user-or-session purchaser, open age, confirmed quantity, original and remaining basis, mark value, comparable P/L, retained price/trend, market cap, liquidity, quote freshness, next staged action, active protection summary and direct detail/management actions. Missing or non-comparable financial data remains unavailable rather than being coerced to zero.
- Derive each session's latest enter or do-not-enter stance per coin from its durable decision log and attach only the saved observation at or before that decision. Open holdings are excluded from entry observations; complete observation, decision and fixed-horizon outcome history remains in the session monitor for later review. This evidence does not yet retrain or silently modify a strategy.
- Expand Portfolio into a holdings workspace that combines finalized wallet positions with locally recorded strategy positions and shows owner, entry time, entry basis, remaining quantity and latest retained value.
- Show the imported strategy's staged market-cap targets, stop loss, trailing exit, maximum hold and approval mode on each strategy-owned holding, including completed exit-stage count.
- Carry a wallet holding directly into the existing manual sell dialog with its full recorded quantity selected for review.
- Attach local strategy ownership and exit-plan metadata to wallet-scoped position reads without exposing strategy packages or wallet key material.
- Use explicit Dashboard and Portfolio controls in the wallet panel instead of making the balance summary a navigation target. Dashboard remains the default and Portfolio includes a direct return.
- Move SOL valuation detail into Portfolio: show the confirmed SOL balance, bounded public SOL/USD quote and estimated USD value without hiding holdings when the quote is unavailable.
- Replace the browser Strategy Assistant chat header with a setup-gated Agent Monitor. Before connection it presents the provider setup; after connection it reports real local session state, scan counts, warnings and timestamps, with direct links to each session's full monitor. The provider connection does not authorize trades or change local monitoring and execution controls.
- Run a read-only startup sequence in the Agent Monitor on every signed-in load. It shows per-check loading state, validates local storage and Mainnet RPC identity/current blockhash, counts connected and known wallets, restores session state, reports loaded/running/starting/problem sessions and summarizes active automation runs. RPC configuration appears only when the RPC test fails; diagnostics and automation internals are no longer generic Portfolio controls.
- Add first-class Anthropic Claude and DeepSeek assistant presets. Claude uses native Messages API authentication, content and tool-result blocks; DeepSeek uses its official Chat Completions endpoint with a non-thinking local-tool continuation.
- Reopen assistant settings on the active provider and model, retain the active in-memory key for same-endpoint model changes, and require a new key when switching provider endpoints.
- Redesign Deposit SOL around the QR code, address, balance and selected network, remove mock-oriented “real value” labels, and identify locally created accounts as PumpWizard Generated Wallets.
- Add password-gated private-key export for PumpWizard Generated Wallets in base58 and Solana CLI JSON formats. Decryption stays in the browser, the exported secret never reaches Python, and extension wallets expose no key-export action.
- Verify browser lint, the production build, focused Python market, storage and API coverage and all four production-bundle Playwright flows. Visually inspect Market, token detail, manual order, Trade and Portfolio at 1440×900.
- Reverify the current production bundle across all 32 Playwright flows and the 45 focused assistant/profile/browser API checks; no paid-provider request was made.
- Keep execution boundaries unchanged. Latest value depends on a retained local quote, wallet positions without strategy ownership have no automatic exit plan, and no funded transaction, unattended submission or Linux package was completed.

## 0.2.0.dev59

- Reduce signed-in primary navigation to two real workspaces: **Trade** and **Portfolio**.
- Make Trade the full-width Pump.fun discovery and manual-order entry workspace, with direct Buy or sell actions in the page heading and token detail.
- Add a Portfolio workspace that loads wallet-scoped finalized positions and submitted orders alongside strategy monitors, status, allocation controls and monitoring actions.
- Remove Strategies, Activity, Evaluate and Replay from the primary header. Strategy setup and limits now live in Portfolio; offline research endpoints and retained records remain unchanged.
- Remove duplicate Trade and Allocate actions from the wallet card. Retain Deposit and Lock wallet as compact account actions.
- Verify browser lint, the production build and all four production-bundle Playwright flows, including the exact two-item navigation and Portfolio order/strategy workflow. Visually inspect both workspaces at 1440×900.
- Keep automatic submission disabled. No real extension approval, funded transaction, unattended execution or Linux package was completed.

## 0.2.0.dev58

- Present the browser product as **PumpWizard Terminal** in wallet onboarding, the signed-in workspace and the browser title while retaining Runner as the local Python/package identity.
- Replace the allocation-tree onboarding illustration with direct product language for live Solana markets, strategy sessions, risk controls and exact reviewed execution.
- Add a full-screen market wizard after every wallet sign-in. Pump.fun opens the supported live workspace; Raydium CPMM is shown as planned and remains unavailable until reviewed trading routes and funded acceptance exist.
- Carry the selected Pump.fun context into the signed-in header and market title. Market choice is deliberately session-only and returns after every new sign-in.
- Verify wallet creation, lock/unlock and Wallet Standard extension sign-in all pass through market selection. Browser lint, the production build and all four production-bundle Playwright flows pass, and the market wizard was visually inspected from the E2E build.
- Keep automatic submission disabled. No Raydium trade, real extension approval, funded transaction or Linux package was completed.

## 0.2.0.dev57

- Replace the split full-screen wallet splash with a centered product onboarding surface that explains the main-wallet-to-strategy allocation model and uses the available desktop space more effectively.
- Replace the installed-wallet dropdown with a live Wallet Standard list showing every compatible detected extension, its icon and readiness state, with direct Connect actions, automatic registration updates, explicit rescanning and a useful empty state.
- Use the current PumpWizard website logo in browser onboarding, the signed-in workspace header, the favicon and the mobile home-screen icon.
- Remove Devnet from browser wallet onboarding. New browser sessions now sign into Solana Mainnet without a network selector.
- Verify two simultaneously registered compatible test wallets in the extension sign-in E2E flow. Browser lint, the production build and all four production-bundle Playwright flows pass, and the revised 1440×900 screen was inspected from the served build.
- Keep wallet connection user initiated. No funded transaction, real extension approval, automatic submission or Linux package was completed.

## 0.2.0.dev56

- Add authenticated browser controls that divide the main wallet's available SOL into durable local strategy reservations without creating addresses or moving funds on chain.
- Add per-strategy reviewed-buy and managed-sell permissions, minimum and maximum buy size, daily buy and order ceilings, daily realized-loss ceilings in SOL and percentage terms, maximum loss per trade, and open-position capacity.
- Recheck strategy policy in Python during candidate assessment, reviewed-order creation and final submission validation; apply the configured per-trade loss percentage to executable-net managed exits.
- Show each strategy's remaining SOL allocation, UTC-day activity, realized loss, open positions and current entry blocks in the browser allocation view.
- Verify the focused Python suite (**39 passed**), browser lint, production build, zero-vulnerability production dependency audit and four production-bundle Playwright flows. The E2E allocation flow stores a 0.2 SOL strategy reservation and reads the exact saved limits through the authenticated API.
- Keep automatic submission disabled. Browser withdrawal, real extension transaction approval, funded mainnet lifecycle acceptance and the browser manual-exit handoff remain pending. No dev56 Linux package was built; the native Linux client is legacy.

## 0.2.0.dev55

- Make wallet sign-in the browser app entry point, with Wallet Standard connection for Phantom-compatible extensions and a new encrypted browser wallet option.
- Generate local Solana keys in the browser, encrypt secret bytes with PBKDF2-SHA-256 and AES-GCM, require an encrypted backup acknowledgement, and support backup restore plus password unlock without sending a private key to Python.
- Keep the authenticated wallet available across dashboard, deposit, and manual trade views; add a network-labelled deposit address, QR code, copy control, live balance refresh, and explicit lock action.
- Reuse the signed-in wallet for reviewed manual buys and sells while preserving exact transaction signing, Python revalidation, durable submission, and receipt recovery.
- Add restrictive CSP, resource isolation, and browser permissions headers to production static responses.
- Verify four Playwright flows against the production bundle and isolated Python API: local wallet create/backup/lock/unlock/deposit, cryptographically checked legacy transaction signing, Wallet Standard ownership proof, authenticated workspace actions, and compact keyboard use. Browser lint, the production build and a zero-vulnerability production dependency audit also pass.
- Keep Linux native UI as legacy. No dev55 Linux package was built. Real extension transaction approval, funded mainnet lifecycle acceptance, browser wallet withdrawals, browser risk-policy editing, and automatic execution remain pending.

## 0.2.0.dev54

- Discover Phantom and other compatible Solana wallets through Wallet Standard, with explicit connection, network selection and a single-use server-verified wallet sign-in challenge in the browser app.
- Add live wallet-balance inspection and reviewed manual Pump curve buys, partial/full sells, transaction signing, durable submission and receipt recovery to the browser interface.
- Keep transaction construction, fresh route/balance/fee/risk checks, signature verification, broadcast and finalized position accounting in the existing Python trade service; browser wallets receive no vault or key access.
- Explain the unattended execution design around a dedicated encrypted bot wallet and bounded local signing session. Browser-extension automation and automatic worker submission remain disabled.
- Compiled the production browser bundle locally. Browser-wallet acceptance with installed extensions and funded mainnet lifecycle acceptance remain pending.

## 0.2.0.dev53

- Make the packaged local browser app the default Runner interface; retain the native Tk client as the explicit `desktop` legacy command for wallet, signing, withdrawal and reviewed-trade workflows.
- Add Playwright browser acceptance for strategy import, disabled-session creation, monitoring controls, offline evaluation, virtual replay, reload persistence, responsive layout and keyboard operation against an isolated real Python API.
- Generate the browser credential inside Runner, serve it only through the existing loopback/HttpOnly-cookie boundary, and add a `browser` command with automatic system-browser launch.
- Require browser assets in every native package and gate platform builds on the browser end-to-end workflow.
- Render the browser version and exact Community patch-note link from the Python API instead of hard-coded React text.
- Browser wallet connection, balance refresh, risk-policy editing, signing, withdrawals and trade execution remain in the legacy client; automatic execution and funded acceptance remain unavailable.

## 0.2.0.dev52

- Rewrote the unconfigured assistant prompt around the immediate Setup action, API key/model choice, starting a chat and approval for local evidence reads.

## 0.2.0.dev51

- Skip incomplete public market-listing records instead of stopping the shared local monitor scheduler.
- Explain the saved legacy timestamp fault and direct the user to Retry monitoring; clarify when another Runner window owns the local monitoring lease.

## 0.2.0.dev50

- Added provider-specific selectable assistant models, while retaining a custom model entry for compatible endpoints.

## 0.2.0.dev49

- Replaced the AI profile page with a provider/model/API-key connection modal from Strategy Assistant Setup.
- Added OpenAI, Local Ollama and compatible Chat API connection presets; updating the active connection keeps credential handling separate from the wallet and starts a separate chat.

## 0.2.0.dev48

- Added a + button beside Active Sessions to open the strategy chooser.
- Added Create your own strategy: a paper-only local draft form that uses the existing handoff format and opens the same compatibility review as imports.

## 0.2.0.dev47

- Removed the duplicate Strategies action from Active Sessions; the top navigation remains the single place to browse or import strategies.
- Integrated saved-chat selection, New chat and Delete directly into Strategy Assistant and removed the History / limits toggle.
- Replaced the empty unconfigured chat explanation with a direct Setup prompt.

## 0.2.0.dev46

- Added monitoring health states, latest decisions, attention counts, persistent scheduler warnings and setup/activity recovery links to Active Sessions.
- Added Market search, sorting, freshness filtering, precise small prices, cached-data warnings and usable scrolling at compact sizes.
- Added exact-mint coin details with bounded retained history, gap-aware price samples and linked session activity.
- Added a coin-to-assistant handoff with frozen data/token preview and explicit one-request approval. Existing drafts are preserved; no automatic provider call occurs.
- Verified the published dev45 Community note and updated the launcher to dev45. Prepared dev46 with its own website handoff; publication remains separate.

## 0.2.0.dev45

- Made the version badge open the exact build's PumpWizard Community patch note; removed the repeated startup popup.
- Require a versioned Community post for every build, export the website handoff JSON, and gate tagged release publication on matching live article content.
- Prepared dev44/dev45 posts in the existing website format. Website publication is tracked separately; preparing/building is not publishing.

## 0.2.0.dev44

- Restyled the desktop identity as PumpWizard Runner, with Solana Markets Trading Bot and a visible version badge.
- Renamed Research to Market and Active Agents to Active Sessions; Market returns to the market view even when a strategy session was selected.
- Unified strategy browsing/import behind a centered, keyboard-accessible choice modal. Existing import inspection and execution gates remain unchanged.
- Retained the persistent header assistant, existing data locations, command aliases and strategy formats.

## 0.2.0.dev43

- Moved Strategy Assistant back into the navigation header, available alongside Research, Strategies and Settings. Removed the separate Assistant page.
- History and token limits expand in place. Existing saved chats, credentials and explicit context approvals are retained.

## 0.2.0.dev42

- Added a full Assistant workspace with provider/model profiles, masked API-key entry, optional separately encrypted credentials, local conversation history and provider-reported token usage.
- Added bounded local strategy and recorded coin-history reads. Every additional provider request requires a data/token preview and single-use approval; large conversation requests also pause for review. No automatic retries or trading tools.
- Refined dark styling, header navigation, library search/table spacing and responsive chat layout.
- Existing strategy import/export formats and execution authorization requirements are unchanged. Live provider acceptance and funded lifecycle acceptance remain pending.

## 0.2.0.dev41

- Add guided local evaluation using the existing pinned replay engine and recording format, with a bundled synthetic example, cancellation, persisted per-package reports, modeled results/events and full-input JSON export.
- Bind reports to configuration/rules/input hashes, engine/build versions and time period. Add explicit navigation to unsigned assessment and worker rehearsal without starting either automatically.
- Include configuration/route/evidence summaries and import comparisons; create monitoring agents with entries disabled and retain existing agents on changed imports.
- Share reviewed-eligibility checks across UI, orders, sessions and worker; reject unsupported trial overlays. Expose state, blocked decisions and recovery guidance.
- Add configuration identity and observation provenance to new shadow/worker reports while labeling older missing identities. Preserve existing strategy and recording schemas.
- Build into versioned directories without deleting retained packages. Funded acceptance remains pending; automatic execution remains disabled.

## Unreleased — 0.2.0.dev40

- Add conservative unsigned liquidation valuation across every local open position, aggregating same-mint holdings across agents/manual trades. Persist combined realized/unrealized P/L peaks independently of wallet journals.
- Add an explicitly configured portfolio drawdown ceiling, desktop valuation refresh, and continuous worker evidence. Refresh before buy review and submission; recheck freshness, wallet revision, balances and the ceiling under the final signing lock. Unavailable routes, missing tokens, failed simulations and stale evidence block new buys while exits remain independent.
- Retain legacy wallet policies without inventing a portfolio ceiling; new policies and newly authorized signing sessions require it. Original session limits continue to apply when a current policy is loosened.
- Extend independent pinned core-frequency reference coverage from 24 to 31 scenarios / 400 frames, including multi-mint streams, age/gap boundaries, FX losses and migration gaps, with continuous and restart comparisons.
- Capture successful deployed Token-2022 canonical PumpSwap unsigned sell evidence and retain unsuccessful discovery evidence. Improve unsupported-pool diagnostics without widening route support. No funded trade or automatic execution is enabled.

## Unreleased — 0.2.0.dev39

- Add a continuous unsigned Pump.fun agent worker with existing-signature recovery before assessment, managed-exit priority, rotating batches and RPC retry backoff.
- Protect each agent/wallet/network worker with a durable lease; retain cycle evidence and health across restart without automatically resuming.
- Add desktop Worker rehearsal start/stop, status, candidate results and JSON export; stop on network changes and workspace/application close.
- Add `rehearse-agent` (one cycle or `--watch`) and read-only `worker-status` commands. The worker accepts no signer or session and cannot prepare or broadcast orders.
- Verify unchanged financial journals during assessment, uncertain-receipt recovery, paused exits, migration, duplicate-worker exclusion and late-RPC cancellation using synthetic funds.
- Portfolio drawdown completion, automatic signing, deployed Token-2022 PumpSwap evidence and funded acceptance remain pending.

## Unreleased — 0.2.0.dev38

- Add bounded in-memory signing sessions scoped to a wallet, network, agent, configuration, cash allocation and supported Pump route allowlist.
- Enforce expiry, total non-replenishing spending allowance, order count, slippage ceiling and the originally authorized wallet risk limits.
- Commit session usage with the existing signature/order reservation; retain usage for unknown or failed submissions and lock sessions on restart.
- Add desktop session review, typed/password authorization, per-order session use and revocation during background revalidation. Each trade still requires review and typed approval.
- Keep Pump.fun completion first; broader DEX adapters remain a later expansion. Automatic execution and funded acceptance remain pending.

## Unreleased — 0.2.0.dev37

- Add per-wallet/network buy-cost, open-cost, reserve, daily realized-loss and realized-drawdown limits shared by all agents and manual buys.
- Add the Wallet limits desktop tab, policy review, durable change history and wallet entry pause while retaining exits and recovery.
- Count historical finalized receipts, partial-exit cost, failed fees and bound/pending buys without resetting accounting on restart or policy edits.
- Hold the wallet write lock through final checks, signing and durable reservation; policy edits invalidate in-flight snapshots.
- Limits require explicit configuration. Unrealized portfolio drawdown, limited signer sessions, autonomous execution and funded acceptance remain pending.

## Unreleased — 0.2.0.dev36

- Add desktop Test setup and Agent trading: readiness before deposits, saved draft inputs, enable/pause tracking, local cash allocation/release, unsigned shadow assessment, reviewed order preparation/authorization/cancellation and receipt recovery.
- Display actual agent-owned positions, quantities, realized SOL P/L and historical shadow evidence; export unsigned reports. Require per-order typed confirmation and a password, and disable expired or finalized reviews.
- Add explicit manual-exit handoff with token-allocation release, exact position selection and retained owner attribution. Pending/unknown operations remain protected.
- Correct sidebar ownership and paused-held-position labels; replace writable sidebar journal initialization with a read-only query. Update obsolete capability text, distinguish recorded/practice views, and polish dark forms and tables.
- Add zero-funding, RPC failure, network/agent isolation, draft expiry, uncertain submission, restart and read-during-write coverage; exercise the full guided and manual-override desktop lifecycles using synthetic funds.
- Build an isolated Linux dev36 test bundle; funded acceptance and unattended execution remain pending.

## Unreleased — 0.2.0.dev35

- Capture successful independent mainnet unsigned Token-2022 curve and standard-SPL canonical PumpSwap sell simulations using public holders. Save slot, fee, output, net-floor and public-address provenance evidence; no wallet was unlocked or transaction broadcast.
- Fix PumpSwap simulation validation to accept a closed wrapped-SOL account represented as a zero-lamport, empty, nonexecutable system account, as well as null. Reject nonzero balances, token ownership, residual data and malformed closure fields; keep pre-existing wrapped SOL blocked.
- Add a mainnet-derived closed-account fixture and regression coverage through quote, synthetic submission and finalized accounting, plus invalid response variants. Retain sender and amount in failed route-check reports for reproducibility.
- Record blocked small curve sells and the original closure failure. Local agent shadow acceptance remains unavailable without a finalized agent-owned position and reservations; funded lifecycle, Token-2022 PumpSwap deployment evidence and unattended signing remain pending.

## Unreleased — 0.2.0.dev34

- Evaluate reviewed agent stops and trails from fresh full-position executable net proceeds after conservative slippage, fee and account-funding bounds. Persist versioned net high-water values separately from observed-price monitoring. Recheck the trigger before signing.
- Add `shadow-agent` and `shadow-reports`: unsigned route simulations, ready/hold/blocked explanations, quote evidence and message hashes. Strategy and wallet journals open read-only; only the separate risk/evidence store is writable.
- Independently simulate partial target quantities after the full-position risk assessment. Reject nonpositive net floors, stale observations, failed simulations, insufficient cash/liquidity and unavailable migration routes.
- Cover net stops/trails, restart, graduated PumpSwap exits, read-only enforcement, signed-simulation rejection and changed triggers before submission with synthetic tests. No funded execution or unattended signer is enabled.

## Unreleased — 0.2.0.dev33

- Add an explicitly reviewed adapter for the pinned core-frequency configuration: current entry/exit validation, agent reservation binding, conservative USD entry budgeting and fresh chain cap/liquidity checks.
- Atomically bind submitted signatures to holds, debit/credit actual receipt amounts, create protected token allocations on finalized buys and attribute partial/full sells and fees to their agent. Unknown submissions retain their holds; replayed receipts cannot double-apply fills.
- Add CLI prepare/review/cancel/history/reconcile commands. Interactive review and password authorization are required for every submission; no automatic signer is connected.
- Include finalized agent positions in shared monitoring and held-mint priority even when entries are paused. Live holdings use observed price marks, explicitly separate from the replay model's hypothetical reserve offsets; executable net-value risk parity remains a future gate.
- Test partial/full lifecycle and restart, pending/unknown recovery, failed buy/sell fees, settlement rollback, cross-agent isolation, stale actions, cancellation, pinned configuration rejection and paused exit monitoring.

## Unreleased — 0.2.0.dev32

- Add the versioned core-frequency portfolio state machine for one pinned configuration, with virtual cash/capacity reservations, delayed entries, expiry/cancellation, partial targets, risk precedence, deterministic failed attempts and attempted-mint re-entry suppression.
- Capture 24 full transition sequences independently from the original LadderRunner, checking complete state and events continuously, through checkpoint restore and through SQLite reopen after every frame.
- Add isolated atomic replay persistence, explicit execution ordering across canonical JSON serialization, duplicate replay protection, concurrent writer tests and rollback tests.
- Add offline `portfolio-replay`/`portfolio-status` commands and a synthetic example recording. Reject unverified configuration changes; do not attach virtual fills to monitoring or wallet execution.
- Fix the shared reserve-growth screen to use the latest accepted history sample's FX basis when duplicate observations carry a different FX value. An independently captured regression case demonstrates the discrepancy.
- Automatic reservation consumption, confirmed agent fills, strict ownership equivalence and funded chain acceptance remain separate gates.

## Unreleased — 0.2.0.dev31

- Add exact-unit per-agent SOL and confirmed-position token holds in the shared wallet journal, with CLI reserve/list/release commands and read-only balance validation.
- Protect held resources in manual trade/withdrawal review and submission, including an atomic check before persisting each signature. Serialize allocations across processes; preserve holds across restarts and block allocation changes during unresolved wallet operations.
- Invalidate a submission's balance snapshot if another local wallet operation completes during validation, using durable wallet revisions alongside submission locks.
- Test competing allocations, post-review reservation races, partial sell/restart/release, balance deficits, position isolation and CLI validation. Holds do not expire or become signed orders automatically.
- Extend independently captured momentum exit parity fixtures to four supplied partial-position scenarios. Full portfolio, pending-order timing and fill parity remain pending; no strategy is promoted to automatic execution.

## Unreleased — 0.2.0.dev30

- Support a narrow Token-2022 subset for manual curve buys/sells and canonical SOL PumpSwap exits: six decimals, revoked mint/freeze authorities, immutable self-pointing MetadataPointer and TokenMetadata only; token accounts may additionally use ImmutableOwner. Reject transfer fees/hooks, delegates, scaling, pausing and all other extensions.
- Derive base-token ATAs with the actual token program, refresh coherent account snapshots after program discovery, and require Token-2022 program identity in finalized receipts. Preserve standard-SPL orders and positions.
- Add independent official SDK instruction fixtures and SPL metadata/account codec fixtures, malicious-layout rejection tests, exact accounting/restart tests, and both token programs in the desktop graduation lifecycle smoke.
- Add read-only `route-check --round-trip`: an unsigned curve buy followed by a partial sell in one diagnostic transaction, without a vault, order journal or submission access.
- Save successful mainnet Token-2022 buy and atomic buy-then-sell simulation evidence. This does not certify funded fills, independently submitted sells or PumpSwap exits. Automatic trading and funded acceptance remain pending.

## Unreleased — 0.2.0.dev29

- Fix truncated mainnet/devnet genesis hashes that rejected legitimate RPC connections before quoting or withdrawals.
- Add `route-check` for public account inspection and optional unsigned simulations, with an independent RPC allowlist, no wallet/vault access, no order journal, and JSON evidence distinguishing account support from execution acceptance.
- Add manual standard-SPL exits through the canonical native-SOL PumpSwap migration pool. Re-review is required if graduation changes the route after quoting; retain shared wallet reservations and finalized receipt accounting.
- Create and close a new wrapped-SOL ATA atomically; reject existing wrapped SOL, unsupported pool modes, Token-2022, missing canonical pools and stale/mismatched state. Check SOL unwrapping in simulation and receipts.
- Add an official SDK instruction oracle, migration/restart tests and a desktop buy → graduation → partial/full exit smoke test. Mainnet account inspection passed on the documented sample pool; successful chain simulation and funded acceptance remain pending. Automatic execution remains disabled.

## Unreleased — 0.2.0.dev28

- Add a manual Trade window for the pinned Pump V2 native-SOL / standard-SPL active-curve route: exact-amount quote, explicit review/password authorization, locally built transaction, submission and receipt recovery.
- Add exact SOL/token manual positions, partial/full sells and durable order history; apply finalized receipts atomically and idempotently. Keep rent/account-funding cash costs explicit.
- Coordinate withdrawals and trades with a shared wallet/network reservation, including across current-version processes; unresolved outcomes stay reserved after expiry/restart.
- Add synthetic RPC and desktop tests for cancellation, success, failure, stale/changed quotes, mismatched receipts, duplicate submission, crash recovery and external balance discrepancies.
- Pin solders 0.29.0 and the official Pump interface revision. Token-2022, PumpSwap/graduation exits and automatic execution remain unavailable; no funded mainnet route acceptance is claimed.

## Unreleased — 0.2.0.dev27

- Replace selected-agent-only desktop tracking with shared background scans and independently persisted agent state. Save start/pause preferences and resume enabled agents when the app reopens.
- Keep exit monitoring for recorded holdings when entries are paused. Isolate provider failures, retry temporary outages, and expose per-agent tracking/error states.
- Commit observations, decisions, position marks, checkpoints, and scheduling state atomically. Reject in-flight batches after pause or lease takeover; coordinate desktop and CLI monitors with a database lease.
- Capture 22 reference cases against the original momentum engine, including entry rejection/history windows, fee-adjusted exit estimates, stops, targets, trails, stale quotes, and restart state. Fix quote normalization and loss of precision on reload. This verifies a bounded decision-screen contract, not fills, scheduling, all strategy families, or live execution.
- Upgrade the local database to schema 9 without enabling previously untracked agents. Swap execution and wallet-wide trading reservations remain pending.

## Unreleased — 0.2.0.dev26

- Bundle verified 1h, 8h, and 24h results for all 211 published simulations after deploying the read-only hourly reporting update.
- Keep the newest simulation snapshot per strategy when merging the bundled catalog with an older local cache, so upgrades do not hide newly available hourly results.
- Document the next milestones: strategy parity, shared monitoring and reservations, confirmed manual swaps, then automatic execution and AI-assisted configuration.

## Unreleased — 0.2.0.dev25

- Added All time / Last hour / Last 8 hours / Last 24 hours performance selection. Rankings, the Profitable filter, table P&L/ROI, and detail metrics use the same window.
- Missing window results display as unavailable and sort after known losses. Period ROI uses opening equity; closed-trade counts and win rate remain explicitly all-time.
- Short-window source fields are supported on refresh. Older public snapshots remain usable without inventing hourly results.

## Unreleased — 0.2.0.dev24

- Added an offline strategy browser with 211 public simulation snapshots and 900 untested research definitions, search/family/version/result filters, and a large performance/configuration panel.
- Add strategy pins exact configurations and creates compatible local monitoring agents; repeated adds reuse them. V2/V3 metadata is preserved and unsupported overlays cannot start as parent strategies.
- Optional public-results refresh preserves local agent rules and retains the last snapshot on failure. Native builds bundle the complete catalog.

## Unreleased — 0.2.0.dev23

- Added real, user-authorized native SOL withdrawals with network selection, exact lamport amounts, fee preview, simulation, and vault-password authorization.
- Added a durable signature journal before submission, duplicate/in-flight protection, and read-only reconciliation after timeouts and app restarts. Unverified expired transfers stay unresolved rather than being retried as new payments.
- Added encrypted wallet export and restore, first-launch restore, and automatic preservation of the previous encrypted vault when switching wallets.
- Added Wallet & backup in Settings, Close settings, and a withdrawal-history window. Pump buys/sells and automated trading remain unavailable.
- Validated the receive → encrypted backup/restore → withdrawal → restart/reconcile flow against a local Solana validator using throwaway wallets and test SOL.

## Unreleased — 0.2.0.dev22

- Added a Withdraw control immediately before Settings below the wallet card. It accurately reports that transfers are not available in this development build.

## Unreleased — 0.2.0.dev21

- Removed the Trading button because Trading is always the default workspace. Moved Settings directly below the compact wallet card in the application header.

## Unreleased — 0.2.0.dev20

- Reworked the wallet card into a compact balance, SOL amount, address, and copy control. Public-address balance sync now starts automatically and repeats every minute; a small indicator turns warning-colored only if a read-only sync fails. Removed the header's idle-monitoring message and moved live tracking feedback into the selected-agent view.

## Unreleased — 0.2.0.dev19

- Replaced the former Workspace/Marketplaces/AI Agent navigation with Trading and Settings. Marketplace selection and local AI profile management are now Settings tabs, while a compact Strategy Assistant chat occupies the full-width header area for strategy and configuration discussions.

## Unreleased — 0.2.0.dev18

- Added market-cap and minimum-liquidity selectors to the automatic monitor. It now collects up to 60 candidates within the chosen cap range every 15 seconds, while filters apply to retained local coin data immediately.

## Unreleased — 0.2.0.dev17

- Removed the workspace monitor title, descriptive copy, and manual refresh control. The feed now refreshes itself every 30 seconds, retains discovered coins in the local library across restarts, and keeps their local market-cap samples even when they leave a later public listing.

## Unreleased — 0.2.0.dev16

- Added public token age plus a compact session market-cap trend and percentage to each market-monitor row. Trends use only successive local scans and remain marked as baseline-pending until there is enough local data.

## Unreleased — 0.2.0.dev15

- Replaced the empty Workspace welcome card with a Pump.fun market monitor that lists recent candidates, their price, market capitalization, liquidity, and whether a local agent has actually recorded an observation for each token.

## Unreleased — 0.2.0.dev14

- Grouped product identity, project link, monitoring status, navigation, and wallet controls into one full-width top application section. The sidebar now contains only active agents and open positions.

- Moved the full SolanaMarket Wizard identity row, project website link, and monitoring status into the left navigation rail so the workspace starts at the top of the window.

- Reworked the left rail into full-width vertical navigation and wired the wallet Sync action to read a confirmed SOL balance from the fixed public Solana mainnet RPC, then calculate a local USD estimate from the bounded Pump SOL/USD feed.

- Moved the wallet summary into the left navigation with a styled Solana mark, abbreviated public address, copy action, and reserved SOL/USD estimate fields. Patch notes now appear when the desktop app opens.

- Added category navigation for Workspace, Marketplaces, and AI Agent. Pump.fun is the first selectable marketplace card, with a local capsule mark and persisted selection.
- Added user-configured OpenAI-compatible AI-agent profiles and bounded chat. Profiles support a loopback HTTP or HTTPS `/chat/completions` endpoint, model, and optional API-key environment-variable name; messages never include wallet signing material.

- Added first-launch creation and password unlock of one local Solana wallet. The private seed is encrypted in a 0600 local vault using AES-GCM with an scrypt-derived key; its public address is shown and can be copied from the desktop workspace.

- Renamed the user-facing application, command, platform artifacts, and release metadata to SolanaMarket Wizard. The in-app header links to it.pumpwizard.fun.

- Added a local `early-ladder-v1` compatibility profile, deterministic entry/exit recommendations, and direct public Pump market collection.
- Added durable local monitoring instances, normalized observation and decision history, and a user-confirmed position and exit ledger.
- Added continuous local monitoring, pause/resume controls, and durable success/failure health history with bounded retry for public market-data outages.
- Added user-selected, read-only HTTPS Solana RPC ownership evidence: mint authority, token-extension, holder-account reconciliation, verified curve-vault exclusion, and concentration checks.
- Added fixed read-only PumpSwap pair snapshots for exit monitoring of already-recorded, graduated positions; AMM entry discovery and swap routing remain unavailable.
- Added durable, public-address-only SOL and tracked-position balance snapshots through fixed read-only RPC calls; no key, signer, or transaction payload is accepted.
- Added bounded dry-run executor configuration and durable draft/simulated/cancelled orders tied to fresh local decisions and observations. Dry-run remains entirely local and cannot build, sign, or submit a transaction.
- Added a recorded loopback-only signer check using one hard-coded local-validator System Program transfer. It rejects every non-local RPC destination and cannot build a trading instruction.
- Rebuilt the desktop shell as a dark agent workspace with a left rail for active agents and open positions, welcome/patch notes, metrics, position/activity/order views, background live tracking, and confirmed external-close recording.
- No executable trading route, transaction construction, or transaction submission is present.

## 0.1.1 — 2026-09-14

- Use a unique checksum filename for every platform artifact so one tagged release can attach all downloads.

## 0.1.0 — 2026-09-14

- Created the standalone local PumpWizard Runner repository.
- Added a versioned strategy-library database, legacy-handoff inspection, CLI, and Tk desktop shell.
- Added cross-platform development-build and release workflows.
- No wallet, market monitoring, position opening, or transaction submission is included in this release.
