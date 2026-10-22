# Dashboard

## Orders and Decisions in dev121

**Activity → Orders** shows matching submission, finalized and awaiting-receipt
counts and exact known recorded fees. Its buy/sell timeline counts submitted
orders by recorded time, with zero-count intervals and explicit missing times.
The settlement breakdown filters the records. Search token/mint, strategy,
signature or order ID; filter time, side, strategy and status; sort and paginate.
Expand a receipt for actual fees, wallet/token changes, slot, timestamps and full
identifiers. Copy the mint/signature, open Explorer or the full token chart. CSV
exports all matching records across pages with raw atom fields.

**Activity → Decisions** merges the selected strategies into one workspace. The
candidate market map uses saved market cap and liquidity, with a logarithmic
liquidity axis and a shaded entry-cap range when the selected rules provide one.
Enter signals and do-not-enter decisions have distinct markers. Missing usable
inputs are counted and omitted from the plot. Selecting a point or token opens
saved-input details; later token chart data is explicitly separate. An inspected
token remains selected when its latest saved decision refreshes.

Reason counts, search, time/strategy/reason filters, decision filters, sorting,
pagination and CSV apply to the latest API snapshots: at most 12 candidates per
strategy, excluding open held tokens. They do not imply lifetime decision totals,
a complete historical curve or a filled order. Orders retain archived attribution.
Both views reset filters when dashboard scope changes and use labelled mobile
record cards. Refresh activity uses the existing portfolio/order refresh paths.

The dev120 wallet-wide tabs and scoped Positions placement are retained. These
changes affect presentation; backend accounting and execution gates are unchanged.

## Wallet and selected scope in dev120

The wallet-wide dashboard has **Overview**, **Activity** and **Wallet**. It has
no Positions tab, global open-position preview or position shortcut. Overview
retains the merged Performance cards, full strategy comparison and per-strategy
open counts, current/history filters, attention and recent trades from dev119.

Selecting a session or strategy adds **Positions** and the scoped Overview
preview. Positions still show purchase attribution, current management and exit
rules for that scope. **All wallet activity** clears the selection and returns to
Overview, including when leaving Positions. If a selected scope disappears, the
visible panel falls back to Overview instead of exposing a global Positions view.

Manual holdings, purchase sources, price history and reviewed sell actions live
in **Wallet**. Holding names open full token details. Wallet-wide stale-price
review and sidebar wallet shortcuts open Wallet. Closing a manual order returns
to Wallet; an attributed strategy order returns to its strategy positions.
Wallet still clears selected session/strategy scope when opened.

This placement supersedes the wallet-wide four-tab layout below. Accounting,
archived attribution, holdings graphs/artwork and trading authorization remain.

## Merged Overview in dev119

Dashboard now has **Overview**, **Positions**, **Activity** and **Wallet**, with
**New order** as the existing action. Overview uses the former Performance
presentation: realized P&L, unrealized P&L, total strategy P&L and closed-position
cards, plus the full per-strategy comparison of state, positions, results and fees.
Results retain their existing since-start SOL accounting and archived attribution.

A compact status row shows monitoring, authorized service trading and open wallet
positions below those cards. Shared funding, monitor, pending-order and stale-price
alerts remain actionable. Open-position and recent-trade previews follow the
comparison; empty previews stay compact. The native balance and wallet charts
remain in the header and Wallet.

The table defaults to **Current**, including paused current strategies. **All
history** adds explicitly labelled archived rows; **Needs attention** includes only
current strategies requiring review. Filtering the table does not discard archived
results from the since-start summary. Selecting an archived strategy shows its
history automatically. Sorting by total P&L, session/strategy scope and mobile
labelled cards are retained. There is no separate Performance tab.

The dev119 merge supersedes the Overview/Performance placement described below.
Holding graphs, artwork, Details and the strategy setup checklist from dev118
remain in their existing views. No accounting or trading authorization changes.

## Current strategies and trading setup in dev118

Overview lists only current, non-archived strategies in a compact four-column
table: strategy/session, operating state, open positions and total SOL result.
Paused current strategies remain visible. Archived strategies and the full
realized/unrealized comparison remain in Performance; archived realized results
still contribute to since-start accounting. Shared funding warnings appear once
per session with a direct Review session funds action.

The saved strategy picker groups versions of the same named strategy. The
version used by the selected session comes first; other saved versions expand
on demand. Added to this session describes membership, while Can add describes
an available compatible version. Edit session strategy opens the current attached
configuration. The real-wallet setup checklist separately shows rules, fresh
session funding and trading approval. Interactive orders require wallet signing;
automatic trading requires the existing exact-build service authorization.
Simulation history is optional, collapsed supporting information and never
confers trading authority or demonstrates a profitable strategy.

Wallet holdings now show small price-history graphs using recorded timestamps
and prices, with hover inspection and the full chart available on click. Visible
rows progressively load cached history and at most one bounded public history
page when insufficient data is retained. A 24-hour percentage never fabricates
a graph; missing history stays explicit. Token icons use exact-mint stored
artwork through the bounded, approved-host proxy, including sanitized static SVG.
Missing or failed artwork uses a labelled generated mint identifier.

The former Evidence action is now Details. It shows current wallet ownership,
purchase attribution, valuation/basis, public price history and linked Runner
records. It is not a paper-trading position or a simulation result. Sales still
require a separate fresh quote, review and signature. No execution or strategy
exchange format is changed by these display improvements.

The dev118 behavior above supersedes dev117's Overview filters and its older
holdings/picker presentation; the five-tab organization remains the same.

## Operations dashboard in dev117

Dashboard has **Overview**, **Positions**, **Performance**, **Activity** and
**Wallet**. **New order** is a wallet-wide action instead of a tab. The native
wallet balance remains in the header; Overview does not repeat it or the full
wallet asset list.

Overview shows actionable attention signals, separate monitoring and trading
counts, open wallet positions, since-start strategy P&L, an active-strategy
comparison and recent orders. Active includes monitoring strategies and paused
strategies with recorded open wallet positions. All retains stopped and archived
strategies. Needs attention finds monitor faults and insufficient session funds.

Strategy rows link to their existing sidebar selection. Overview, Positions,
Performance and Activity preserve that selection across tabs. Session selection
includes its current and archived strategies; Wallet clears strategy selection
and always shows the whole current wallet/network. Funding remains session-level:
multiple strategies share one allocation, which is not duplicated per strategy.

Performance compares per-strategy realized, unrealized and total SOL results,
recorded fees and closed positions using the existing backend receipt/basis and
fresh-mark calculation. Archived strategies remain attributed, including manual
exits of their purchased positions. Fees are shown separately for inspection and
are not subtracted a second time. Missing costs, receipts or fresh prices remain
unavailable; known realized results can remain visible when open marks are missing.
Manual or independent positions outside the session comparison prevent a complete
wallet-wide strategy summary rather than disappearing from that summary silently.
Period returns, return percentages, drawdown and full historical strategy curves
are not implemented. Observation/paper records never become live trading P&L.

Monitoring is not trading. The trading count requires fresh, healthy, matching
service identity, wallet/network, strategy, unlocked signer and unexpired active
authorization. It is explicitly a service-authorization count; legacy browser-only
execution is not counted. Inspect existing controls for signing and exit authority.

Positions starts with compact cost, value, unrealized result and management rows.
Expand Position details for mint, entry time, quote age, liquidity and exit rules.
Local monitor records are labelled separately from wallet receipts. Retained
estimates never replace the fresh reviewed quote needed for a sale. Activity has
Orders and Decisions filters within the selected scope; external wallet activity
remains in Wallet. On narrow screens comparison tables become labelled cards.

Wallet contains the full existing holdings tools and the valuation charts moved
from Overview. Wallet value movement includes deposits, withdrawals, outside
trades and market changes. Its open-position USD series is a snapshot of the
currently open set, excluding closed positions and future exit costs, rather than
a strategy return curve. History retains its existing seven-day observed snapshots
and gaps. The header's confirmed native SOL/history controls remain available.

The navigation and accounting descriptions below record earlier dashboard builds;
the dev117 structure above supersedes their Overview/tab placement.


The command board, wallet identity, wallet metrics and workspace actions share
one full-width header. System activity and optional AI chat can be collapsed
without hiding the wallet or system status. Startup/session action buttons live
inside System activity & AI chat and follow it into the detached window.
System status appears beside the PumpWizard title, followed by wallet identity
then compact wallet balance and Deposit/Withdraw, **Guides**, and a larger **Sign out**
tile on the right. System status reports the running Runner version, service health
and verified RPC status, with explicit checking, unavailable, restart and mismatch
states. The wallet address is the bold main line in its card, with the actual
provider/name, network and signing state below. The shortened address shows
eight characters from each end; its hover title and screen-reader text retain
the full address. A connected provider still appears as, for example,
**Phantom wallet connected** in the supporting details.
RPC verified reflects the most recent startup or configuration test.
Reloaded wallet views show that signing is locked. The header has no separate
metrics or action/version strip. Dashboard navigation sits beside the app title.
P/L remains in Overview, and 24-hour worth movement sits above Performance charts.
The compact card shows one native SOL balance with its smaller italic USD
equivalent. The currency switch reverses the main and supporting amounts and
remembers the choice for each wallet/network on the local server. Both amounts
refer to the same native SOL balance; token-inclusive portfolio value and partial
valuation details remain in Overview. Native balance refresh is independent of
token inventory and pricing. Display-only SOL/USD quotes use Pump, then a
timestamp-checked [Coinbase ticker](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker).
An explicitly aged estimate can remain visible for five minutes. When both
providers fail, SOL remains the main amount and the USD status retries automatically.
Narrow screens stack the cards and
keep Guides and Sign out side by side.
**Unlock signing** appears inside the restored wallet card beside its details.
It opens the existing password or extension proof dialog and disappears after
signing becomes available. A walletless card retains **Connect wallet**.
The locked action uses a gentle amber glow pulse; reduced-motion settings keep
it still.

Use **Open in new window** beside **System activity & AI chat** to move just
that section into a movable, resizable window. The button works while the section
is collapsed. Activity continues updating, and the same chat history, unsent
message and pending provider approval follow the view. AI settings open there;
service and strategy links bring you back to their controls in the main Dashboard.
Use **Return to Dashboard**, **Return here**, or close the detached window to put
the section back. If your browser blocks it, allow pop-ups for the local app.
Keep the main Dashboard open: closing or reloading it, signing out, or changing
wallet scope closes the detached view. This is a companion view of the existing
Dashboard; it does not add AI capabilities or grant new trading permissions.

Without a provider, chat uses one compact sentence and **Add API key**. Once
connected, the message field includes **New chat** and **AI settings** icon
buttons. The separate provider/footer strip is removed; provider details remain
in the status history and settings, and each request retains its approval step.

Dev101 fixes Firefox opening and immediately closing the companion window. The
regression check uses a new Firefox profile and the isolated browser fixture;
it requires local Firefox and geckodriver, without installing either. Run it
with the same Python environment used for browser acceptance:

```sh
PUMPWIZARD_E2E_PYTHON=/path/to/python python3 tests/check_detached_firefox.py
```

Use `--web-root /path/to/packaged/web-client` to check an exact bundled client.
**System status** opens the existing background service controls. Service problems
appear in that card; there is no separate Background button. During active signing,
Trading active appears in the status detail. Pause or stop service, Retry startup,
Review session and RPC recovery actions appear inside System activity & AI chat.
The **Guides** tile opens the welcome and usage tips. AI connection and settings
remain in System activity & AI chat.

Dashboard contains Overview, Holdings, Positions, New Order and Trade History.
Holdings includes the former Portfolio's full token list, valuation context,
purchase sources, mint copying, explorer links, price history, holding evidence
and reviewed sales. On narrow panels each holding becomes a card. Selecting a
session or strategy retains its scoped views, controls and observations.

Trading strategies show their actual session SOL allocation and execution mode
in the monitor. They do not show the Paper trading shortcut or Paper budget field;
the strategy's sidebar menu also omits that shortcut. Monitoring strategies retain
their separate paper research controls. Their saved brief and owning workspace
determine eligibility, including older briefs without the newer session-mode
field. Trading-session evidence views retain Evidence, Results and History but
omit Forward paper and Campaigns. The API also rejects paper starts/resumes and
campaign starts/resumes tied to trading sessions. Older paper history remains
readable, and pause/stop remain available for cleanup. Stored briefs, allocations
and trading authority are preserved.

For a service-custody automatic strategy, **Set up auto trading** opens encrypted
enrollment for an imported or app-generated local wallet, exact allowance limits,
wallet approval and service-password Start. Approval binds the installed build,
session, routes and duration; runs last at most one hour. Existing allocation and
exit-reserve checks can block Start and are shown explicitly. Run monitor starts
the background scanner without trading authority. A waiting monitor shows
**Start scanning**; starting approved auto trading also starts the scanner.

The wallet Dashboard opens its Overview tab. It shows complete estimated wallet
worth, confirmed SOL, the unrealized result of open Runner-recorded positions,
and how many strategies are monitoring. SOL balance includes allocated funds;
session funding controls still determine whether new entries are affordable.
If token prices or account coverage are incomplete, the current metric instead
shows only known SOL and freshly priced holdings, labelled partial. Its reason
identifies missing/stale prices or coverage gaps. A partial value never becomes
a complete worth snapshot or a 24-hour worth movement. Unavailable position P/L
explains missing purchase-time SOL/USD, unknown costs or stale position prices.

The compact strategy list includes paused strategies and strategies needing
attention. Running filters to enabled monitoring without a tracking fault.
Entry pauses and exit modes appear separately from monitoring status. Recorded
wallet positions are listed under their purchasing strategy, retaining current
session/user management. Additional local monitor positions are labelled local
and do not contribute to wallet totals or the wallet P&L chart. Selecting a row
opens the existing strategy position view and its detailed controls. The holdings
list opens the Holdings tab within Dashboard.

## Charts and accounting scope

The wider balance card includes a labelled **SOL history** or **USD history**
chart directly below the amount, following the main currency switch. It shows
confirmed native balances from the earliest available
transaction to today, including deposits, withdrawals and fees. Hover or use
arrow keys to inspect it, with date ticks, a balance scale, shaded confirmed
segments and 7d/1m/All ranges. An external wallet's original key creation date is
not inferred from its import date or first transaction.

**History** opens the larger chart with SOL/USD and 24h/7d/**All** controls.
All in either currency uses balances from available confirmed transactions.
SOL uses exact pre/post native amounts,
including failed transactions' actual fee effects. The reader pages backward
in batches of at most 500 discovered signatures; this is no longer a lifetime
limit. It persists exact public receipts in `wallet-balance-history.sqlite`,
paces up to 12 receipt requests within an eight-second work budget per read,
and continues automatically across API calls and restarts. Confirmed progress
survives provider failures and rate limits. Missing receipts/timestamps and
inconsistent balance continuity leave gaps and report partial coverage. A pruned
RPC history is not presented as proof of the wallet's original creation date.
Public native balance/history are scoped by address/network and protected by
the local browser token. They grant no ownership proof or signing authority.
Saved metadata restores a view-only dashboard when proof expires or a temporary
RPC error occurs. Private Portfolio/journal reads and signing retain their
existing ownership and approval requirements.

USD history estimates native balances using historical SOL/USD opening prices
from [Coinbase candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles).
Histories up to 12 days use hourly prices; longer histories use daily prices.
Prices are cached in `wallet-usd-history.sqlite` and load progressively across
requests and restarts. Price changes add readings during confirmed intervals
with an unchanged native balance. Missing receipts or rates remain gaps, with
partial coverage or retry status. The latest point uses the current quote when
available. The inline chart's label, axis, inspected values and path follow the
selected currency; the larger All chart also uses this history. Recent 24h/7d
USD views retain observed rates in 15-minute buckets for seven days. Past balances
are not repriced with today's rate. Portfolio-worth snapshots are kept separate
and token holdings are excluded from native balance history.

RPC fields follow the Solana [signature-history](https://solana.com/docs/rpc/http/getsignaturesforaddress)
and [transaction](https://solana.com/docs/rpc/http/gettransaction) references.

Wallet holdings charts the complete estimated USD value of confirmed SOL and
token holdings. It includes deposits, withdrawals, external trading and market
price movement. Missing SOL FX, undecoded accounts or an unusable token mark
make the complete total unavailable. Individual known holdings can still display
their values without being represented as a complete portfolio.

Open positions P&L charts the sum of backend-typed USD unrealized results for
currently open Runner-recorded positions before exit costs. It is not all-time
or realized P&L. Unknown basis/FX, stale marks or unavailable trade reads produce
an unavailable result. With a successful trade read and no open positions, the
result is zero. Closed-position results, outside positions and wallet transfers
are excluded. A sale or position closure changes which positions contribute to
this current-position series.

Snapshots are observed as the open app refreshes; they are not a reconstruction
of historical balances. The private local server account database stores each
wallet/network's history in fixed 15-minute buckets, retaining seven days.
Refreshing updates the current bucket without discarding older buckets. Previous
server-saved worth history can seed wallet values with unknown historical P&L.
The app does not create points for periods it did not observe. Charts break at
unknown values and at gaps longer than 30 minutes, and start with a single point
until enough observations exist. Keyboard arrows or hovering inspect values.

The history uses server-owned preferences and existing service backup/restore.
Browser local/session storage does not retain chart history. Invalid identity,
secret fields, non-finite values, negative worth, unsorted timestamps and excessive
rows are rejected. Recording or inspecting history grants no signing authority
and submits no orders.
