# Strategy catalog

**Add strategy** now opens a focused choice instead of the former flat catalog:

1. **Create your own strategy** opens the local research-rule form.
2. **Import from web simulations** opens a separate interactive simulation
   library.

The choice screen also retains access to the local library and JSON import. The
simulation library searches by name, family and strategy ID; filters by family,
result and local compatibility; switches among all-time, 1-hour, 8-hour and
24-hour reported performance; and sorts or pages the bounded snapshot. Selecting
a row shows starting and ending equity, window P&L/ROI, all-time win rate,
closed trades, entries, fees, drawdown, source timestamp and side-by-side
reported-window bars. The detail panel also summarizes the exact entry range,
style, observation time, order/budget, position cap, hold time, exit loss rules
and staged targets before **Import exact strategy** is available.

These are attributed summary windows, not a trade-by-trade equity curve, so the
app does not draw invented time-series history. Import still resolves the
selected catalog ID on the Python side and parses the exact bundled/cached
package. Compatible definitions enter the local library; creating a disabled
session remains a separate action. Repeated imports reuse the immutable local
strategy identity.

The local library is a separate searchable master/detail view. Its list shows
compatibility and existing-session counts. Selecting a strategy shows its exact
entry range, signal, observation time, order size, paper budget, position cap,
hold time and staged-exit count, plus the stored compatibility reason and
package identity. **Create monitoring session** remains explicit and creates an
inactive session with entries disabled; funding and permissions are configured
later through the separate strategy controls.

## Retained market pulse and guided creation

The browser Trade workspace summarizes the same bounded local watchlist used by
the token explorer. It shows quote freshness, rising/falling breadth, median
retained market-cap change, median reported liquidity, compression breakouts,
recent launches and the largest retained moves. Rows need at least six retained
snapshots before they contribute to trend breadth. **Fresh** means the latest
quote is no more than 90 seconds old. These labels describe the loaded watchlist,
not the whole Pump.fun market, and they never replace a current route, executable
quote, volume, ownership or holder check.

The creation wizard offers four guided research goals plus the existing custom
path. These goals resolve to existing `early-ladder-v1` fields before ordinary
package validation; no strategy exchange field was added:

| Goal | Default local signal | Observation | Maximum hold | Important boundary |
| --- | --- | ---: | ---: | --- |
| Squeeze breakout | `squeeze` | 5 minutes | 6 hours | Detects price compression and breakout only; Runner has no short-interest, borrow or liquidation feed |
| Day trading | `momentum` | 1 minute | 4 hours | Fast retained momentum does not guarantee fill quality or continued liquidity |
| Longer hold | `slow_burn` | 30 minutes | 7 days | The watchlist does not establish fundamentals, holder quality or a genuinely long investment horizon |
| Unicorn hunter | `persistent` | 15 minutes | 10 days | Highly speculative; retained price/liquidity samples cannot predict a future winner |

The displayed best fit is deterministic and local. It uses current freshness,
history coverage, breadth, liquidity, recent launches and compression patterns,
then shows how many related catalog simulations have reported and positive
all-time P&L. Sparse data caps the fit score and keeps confidence low. Users can
review every generated field before saving. Saving creates an immutable
paper-only package; monitoring, wallet allocation, order review, signing and
funded execution remain separate actions.

## Coverage and observed results

The bundled September 17, 2026 public snapshot contains 211 independent forward simulations: 39 original/longer-observation cohorts, 100 research pilots, 56 V2 trials, and 16 V3 trials. The remaining 900 definitions from the 1,000-variant research matrix are also included, explicitly untested, with no invented returns.

The initial dev24 snapshot had four positive, 197 negative, and ten zero total equity P&L results. Results change with each publication; consult the app's per-row timestamp and selected window for the current bundled/cached figures. These counts describe a snapshot, not a strategy recommendation. Compare sample size and observation period as well as profitability. Per-strategy views show starting equity, current equity, net realized and unrealized P&L, closed-trade win rate, entries, open positions, fees, and drawdown where reported. Unavailable metrics remain unavailable. Missing sell quotes can mark open positions at zero in the source simulator.

Fourteen entry families are represented: momentum, breakout, pullback/reclaim, slow burn, persistent leader, higher lows, multiple-window confirmation, squeeze breakout, breakout retest, post-graduation survivor, liquidity growth, relative strength, second-wave re-entry, and recovery.

## Compatibility

1,039 original/staged configurations pass the existing local monitoring contract. This means they can be attached to the local decision engine, not that execution or behavioral parity has been verified. Only the exact pinned core-frequency definition without trial overlays is eligible for unsigned rehearsal and separately reviewed agent orders; funded acceptance is pending. Independent sampling, cohort-relative rankings, graduation discovery, modeled fill costs, and real execution differ from the simulation. A dedicated replay/parity pass is required before claiming identical behavior.

The source desktop's **Compatibility & evidence** tab shows package/rules hashes,
engine support, route scope and evidence limits. **Research · inspection**,
**Research · monitoring** and **Reviewed eligible** are distinct local labels;
unsigned rehearsal never authorizes an order. See the existing-format
[website/desktop handoff contract](STRATEGY-HANDOFF.md).

All 72 V2/V3 configurations preserve their separate trial metadata. They can be saved and exported, but cannot start as parent strategies. V2 adds ownership telemetry and/or a net-value profit trail; V3 adds delayed signal confirmation and/or break-even protection. These overlays need local engine and durable-state support. The importer also refuses unknown extra ladder rule keys rather than silently ignoring them.

## Offline data and refresh

The Performance selector switches between All time, Last hour, Last 8 hours, and Last 24 hours. Sorting uses descending P&L in the selected window; the Profitable filter uses that same window. ROI divides period P&L by opening equity, while win rate and closed-trade counts remain labeled all-time. Windows end at each source snapshot, not the device's current time. Missing values display as `—` and sort last, including after known negative results. Untested variants are never treated as zero.

The dev26 bundled snapshot includes `pnl_1h_usd`, `pnl_8h_usd`, and `pnl_24h_usd` for all 211 published simulations. The read-only public reporting update is deployed and verified over HTTPS. Older snapshots without a window still show it as unavailable; no historical values are extrapolated from 24h P&L. On upgrade the loader merges the bundle and cache by per-strategy source timestamp, so an old cache cannot hide new bundled hourly results.

Definitions and public performance are bundled in `src/pumpwizard_runner/data/strategies.json`. Opening the catalog requires no network connection. Update results optionally reads `https://pumpwizard.fun/api/public/summary` on a background thread and caches a validated snapshot in the app data folder. Failure retains the previous snapshot. Per-row source timestamps are preserved when older strategies are absent from a new publication; refreshed results never overwrite imported configurations or trigger orders. A changed rule set becomes a new immutable local configuration when added.

To rebuild the bundle, save the public summary to a JSON file and run:

```sh
PYTHONPATH=src python packaging/sync_strategy_catalog.py \
  --pumpwizard-root /path/to/pump-agent --snapshot /path/to/public-summary.json
```

The script reads the checked-out `paper/experiment_catalog.py` definitions and published normalized configurations. It does not instantiate simulation engines or open any ledger. The website's public summary supplies all performance values; none are inferred from strategy labels. The app does not need that checkout at runtime. Source and native release builds include the same catalog asset.
