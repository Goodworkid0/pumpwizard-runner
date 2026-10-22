# Order and chart UI — September 25, 2026

Source changes to the browser UI after the user's chart, graduated-buy and
pending/finalized screenshots. Version remains `0.2.0.dev60`. No packaged build,
Community publication, website edit or execution activation is part of this work.

## Visible behavior

- Historical pools use a dark, labeled selector with shortened addresses; the
  complete address and provider details remain in expandable Data & coverage.
  The selector only appears for price history, not local market-cap/liquidity
  snapshots. A pool's historical venue is not treated as its current buy route.
- Candles have a price scale, grid, volume section and visibly selected ranges.
  A single candle is centered and explicitly labeled as limited history. Flat
  prices remain renderable; missing volume displays Unknown. Time gaps remain
  shaded instead of being bridged with invented prices.
- The footer counts saved candles across the retained pools, rather than showing
  zero from the older unscoped collection. The selected pool has its own count.
  Candle values and longer coverage notes are expandable; history sync is compact.
- Graduated buy reviews show Buying unavailable for this token, explain that new
  buys after graduation are unsupported, and offer Back to market / Change token.
  Repeating the same known-unsupported buy is disabled. Changing the mint keeps
  the entered amount and returns focus to the mint input. Backend route checks
  and signing guards are unchanged.
- Available buy capacity is visible; allocation/buffer and wallet-limit details
  are grouped in an expandable Balance & limits section.
- Order banners follow the reconciled receipt. A background refresh changes
  pending to finalized without clicking Refresh status. The full signature is
  no longer embedded in a stale message or used as a multi-line link label.
  Explorer destinations still contain the exact saved signature; the full value
  remains available in the link's title and accessible label.
- Duplicate unresolved-signature warnings are consolidated while preserving
  other buy restrictions. Pending orders still block new submissions. Terminal
  receipts replace an earlier submission-uncertainty alert, including failed
  transactions with fees.

## Validation and evidence

Browser lint and production build passed. **29 distinct Playwright tests passed**
across the full run and focused reruns. The first full run passed 27 tests; the two
new chart fixtures initially failed because their Node-side intercepted fetch
received an origin-check rejection. Loading the fixture through the browser's
same-origin fetch fixed the fixture. Both chart widths and the two order flows
then passed. The final mobile text-spacing adjustment was checked separately.

The new coverage checks selected-pool counts, single-candle placement, flat
prices, unknown volume, gaps, metric switching and 390px/1280px layouts. Existing
lifecycle coverage now asserts pending-to-finalized status during background
recovery. Graduated-review coverage verifies no signing/submission and usable
recovery actions. Wallet mutation rejection, receipt recovery, strategy orders
and automation preparation also passed in the full run. No Python trading logic
changed in this UI batch; earlier Python test checkpoints are separate evidence.

Synthetic screenshots (not real wallet fills):

- Desktop pool chart
- Mobile token detail
- Mobile chart
- Graduated buy notice
- Finalized order

Separately, the user's real Firefox Phantom buy finalized; see the
retained receipt summary.
That confirms real buy signing/settlement, not the complete funded sell/restart
lifecycle. Read-only diagnostics confirmed one finalized trade and valid journal
integrity. This session did not sign or submit a real transaction.

The running source app serves `index-Dc0Wb3Xk.js`, `index-ChVXYCqg.css`.
No process restart was needed for this static browser update. Reload the browser
to use the new UI. The finalized buy and open position remain retained.
