# Connected-wallet Portfolio

Status, September 27, 2026: implemented in unreleased source. The version remains
`0.2.0.dev60`; this work did not submit a funded transaction.

## Product surface

Portfolio is a current-holdings page. It shows every nonzero standard SPL Token
and Token-2022 owner balance for the connected wallet and intentionally omits
wallet controls, strategy controls, historical activity, receipts, closed
positions, accounting cards and exit-plan detail. Those workflows remain in the
global wallet area or the Trade workspace.

The normal inventory is a dense seven-column table rather than a stack of cards.
Artwork/name, balance, estimated value, trend, mint/copy control, purchase
source, History and Sell stay on one approximately 58 px row so about ten
holdings remain visible in a desktop viewport. Only an explicitly expanded
historical chart adds row height.

Each holding exposes:

- token icon when bounded public metadata supplies a supported raster image;
- minted name and ticker, metadata source/known launch date, full contract
  address, one-click CA copy, Solscan token link and project website;
- exact integer-backed wallet quantity, last credible USD price and estimated
  holding value when a reliable public price exists;
- an always-visible mini trend from retained snapshots or the provider's 24-hour
  change, with an explicit no-trend/no-recent-trades state when evidence is too
  thin;
- holder count and compact warnings for low value, stale price, thin liquidity,
  suspicious metadata or low market quality; and
- purchase attribution plus a prominent reviewed Sell action for each
  attributed source; and
- an expandable USD price-history chart with OHLC/volume candles, range
  controls and pool selection when more than one retained pool exists.

Exact-mint metadata reads merge Pump.fun, PumpSwap/Dexscreener and a single
bounded Jupiter Tokens batch field by field. Pump remains authoritative for
fields it supplies; the pair/token indexes fill identity, artwork or site gaps
and add display-only price/liquidity/holder/quality context. Project URLs remain
sanitized HTTPS links. The browser never requests the remote icon directly: the
loopback server proxies only PNG, JPEG, GIF or WebP files up to 1 MB from an
explicit provider/CDN host allowlist. Missing or rejected images fall back to
token initials.

## Historical charts

The small trend in every row uses already-retained points or the bounded token
profile; it does not claim a chart when only one or no credible price exists.
The full price history loads only when the user expands that holding, so a
wallet with many tokens does not create a burst of public-history calls. The
panel first reads locally retained snapshots and saved candles. If the bounded public
history job has not started or has more work ready, it requests one resumable
page through the same history service used by token detail. Errors remain local
to the opened holding and expose Retry history.

Pool OHLC candles retain their source identity and are never combined across
pools. When separately attributed pools exist, the user chooses which pool to
chart. Older legacy imported candles remain chartable without an invented pool
label. If no candles exist, the chart falls back to Runner's locally saved price
snapshots. Every panel labels historical USD data as informational rather than
an executable quote.

## Purchase attribution

Runner compares the aggregate onchain balance with open finalized positions for
the same mint:

- a position owned by an agent is shown under that strategy's local label;
- a Runner position without an agent owner is shown as **Manual trade in
  PumpWizard**; and
- atoms above all recorded positions are shown as **Outside PumpWizard**.

A single mint can show multiple sources. Extra wallet atoms never inherit a
Runner cost basis or strategy identity. If the actual wallet balance is below
Runner's recorded quantity, the affected Runner sell actions remain disabled
instead of guessing which lot changed.

## Selling

Every Sell action opens the existing reviewed Trade ticket with the mint and
source quantity prefilled. Runner positions remain bound to their retained
position ID. Strategy positions use the strategy-order review path and keep
their reservation. Outside-PumpWizard tokens use no local position ID and may
sell only the current untracked portion of the balance.

External-token sales still receive the normal unsigned simulation, exact wallet
review, signature validation, durable pending lock and finalized receipt check.
They do not create a fabricated position or zero-cost basis. Agent reservations
are counted against the aggregate token balance, so an external sell cannot
borrow strategy-held atoms.

## Remaining limits

- A listed token is not a promise of an executable route. Unsupported programs,
  token decimals or venues fail safely during review.
- Metadata may be unavailable or a project may provide no accepted icon/site;
  the mint and onchain balance remain the authoritative identity.
- A missing provider price is rendered as **Unpriced**, not zero. Estimated
  value, quality labels and mini trends are informational public-market context,
  not an executable sell quote or proof that sufficient exit liquidity exists.
- Historical coverage depends on retained observations and public pool data.
  It may start after launch, contain gaps or be absent, and is not canonical
  trade-by-trade onchain history.
- The inventory does not discover compressed assets, stake accounts or assign
  economic value to LP/NFT-like balances.
- Portfolio intentionally shows current holdings only. Local receipts and
  transaction history remain retained in Runner and available to Trade/recovery
  services, but are not rendered on this page.
