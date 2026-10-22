# Active Sessions, Market and coin evidence — dev46

Active Sessions lists local strategy instances. It does not list wallet signing
authorizations. A green **Monitoring** state means a recent scan returned quotes;
it grants no trading permission and says nothing about execution compatibility.

| State | Meaning and next action |
| --- | --- |
| Ready / Starting | Configuration saved / first scan pending. Inspect setup or activity. |
| Monitoring | Recent completed scan with observations. Inspect recent decisions. |
| Paused / Exits only | Entries paused; recorded holdings remain scheduled for exit monitoring. |
| Retrying / Retrying exits | A scan failed; monitoring will retry. Inspect the provider error and activity. |
| Stale | Last completed scan is older than 90 seconds or has an invalid/future timestamp. Check connectivity and activity. |
| No data | Last scan returned no supported quotes. Inspect configuration and later scans. |
| Blocked | Monitoring fault; inspect configuration before explicitly retrying monitoring. |

The sidebar counts sessions needing attention. The selected session shows its
latest decision and links to **View activity** or **Review setup**. Scheduler
warnings remain visible across refreshes. **Retry monitoring** uses the existing
tracking action; it does not authorize execution. Reviewed trading and the
unsigned worker retain their own separate status, controls and evidence.

Market searches the newest retained row for up to 500 mints. Sort by observation
time, liquidity or market cap; filter by cap, liquidity or **Fresh only (90s)**.
Filters immediately affect retained rows. The cap band also applies to subsequent
periodic scans; **Refresh market** requests a new scan. Search and sorting do not
fetch history or request AI calls. Cached rows remain available after scan errors.
Each row has its own age; the newest sample does not make the whole table fresh.
The 90-second UI threshold does not change execution freshness or risk checks.

Double-click a coin or choose **Coin details** to view its exact mint, latest
price/cap/liquidity and up to 100 retained samples and 100 monitoring decisions.
The price chart uses real observation spacing and leaves gaps over 90 seconds
unconnected. These are samples, not candles or complete historical coverage.
Activity links back to its session; a decision is not a transaction receipt.
Missing samples or decisions do not mean zero on-chain activity.

**Ask Assistant** stages up to 50 retained snapshots for the exact mint in the
header assistant. The review shows the data, selected provider/model, estimated
input and output cap. Declining sends nothing; approval permits one request only.
No wallet data is attached. Existing unsent drafts or pending requests must be
finished first. Additional model-requested context still needs separate approval.

This batch adds no schema migration or strategy export changes. Funded lifecycle
acceptance remains [pending](FUNDED-ACCEPTANCE.md), and automatic execution remains
disabled. UI tests use temporary data, fake providers and synthetic funds only.
