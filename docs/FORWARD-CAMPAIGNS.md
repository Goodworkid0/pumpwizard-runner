# Forward observation campaigns

Forward campaigns coordinate one to three frozen paper strategies over the same
read-only market evidence. They never allocate a wallet, unlock a signer, build
an order, submit a transaction or buy provider access. Starting, resuming and
stopping are deliberate commands; viewing a campaign is inert.

The shared schema and time semantics are defined in
[FORWARD-EVIDENCE-CONTRACT.md](FORWARD-EVIDENCE-CONTRACT.md).

## Prepare a multi-day manifest

First list the local strategy sessions and copy the exact session IDs and full
strategy digests that the operator has reviewed:

```sh
pumpwizard --data-dir /absolute/path/to/data instances
```

Save the following as `forward-campaign.json`, replacing only the two
`REPLACE_*` pairs with identities from that command. The CLI resolves and
verifies each frozen strategy package from its session. This three-day example
uses a one-minute cadence, one provider request at a time and immutable hard
ceilings; changing a limit requires a new campaign version.

```json
{
  "schema": "pumpwizard.forward-campaign-request/v1",
  "campaign_key": "operator-three-day-observation-2026-09-28",
  "supersedes_campaign_id": null,
  "source": {
    "provider": "CompositePumpMarketSource",
    "minimum_cap": 1000,
    "maximum_cap": 150000,
    "priority_mints": [],
    "limit": 24,
    "cadence_seconds": 60
  },
  "arms": [
    {
      "id": "arm-a",
      "source_session_id": "REPLACE_SESSION_A",
      "strategy_sha256": "REPLACE_64_HEX_DIGEST_A",
      "simulated_capital_usd": 500
    },
    {
      "id": "arm-b",
      "source_session_id": "REPLACE_SESSION_B",
      "strategy_sha256": "REPLACE_64_HEX_DIGEST_B",
      "simulated_capital_usd": 500
    }
  ],
  "assumptions": {
    "cost_model": "exact frozen strategy costs plus separately declared provider cost",
    "execution_model": "actual paper portfolio replay v3; no funded fills"
  },
  "coverage": {
    "max_source_age_seconds": 60,
    "minimum_independent_tokens": 25,
    "minimum_observations": 1000,
    "maximum_gap_seconds": 300
  },
  "duration_seconds": 259200,
  "quotas": {
    "max_requests": 4320,
    "max_active_runtime_seconds": 259200,
    "max_storage_bytes": 134217728,
    "max_concurrency": 1
  },
  "provider_cost": {
    "currency": "USD",
    "marginal_cost_per_request": null,
    "fixed_subscription_cost": null,
    "price_basis": "unknown"
  }
}
```

Unknown provider cost is deliberately `null`, not zero. Review the provider's
current terms and lower the request ceiling or cadence if they require it.

## Start and operate

Use a unique delivery key. An exact retry of the same Start returns the same
campaign; reusing the key with changed content is rejected.

```sh
pumpwizard --data-dir /absolute/path/to/data service start --port 8781
pumpwizard --data-dir /absolute/path/to/data campaign-start forward-campaign.json \
  --idempotency-key operator-three-day-start-20260928 \
  --confirm 'START FORWARD CAMPAIGN'
pumpwizard --data-dir /absolute/path/to/data campaigns
pumpwizard --data-dir /absolute/path/to/data campaign-status CAMPAIGN_ID
pumpwizard --data-dir /absolute/path/to/data campaign-provider CAMPAIGN_ID --limit 100
pumpwizard --data-dir /absolute/path/to/data campaign-alerts CAMPAIGN_ID --state open
```

Pause and resume use new idempotency keys. A process restart always leaves an
active campaign paused; it does not resume or backfill missed time on its own.

```sh
pumpwizard --data-dir /absolute/path/to/data campaign-command CAMPAIGN_ID pause \
  --idempotency-key CAMPAIGN_ID-pause-1
pumpwizard --data-dir /absolute/path/to/data campaign-command CAMPAIGN_ID resume \
  --idempotency-key CAMPAIGN_ID-resume-1
```

## Snapshot, export and stop

Create an append-only JSON report and retain its output. A corrected snapshot
must name the prior report rather than replace it. The local Dashboard can
export a selected snapshot as JSON or CSV.

```sh
pumpwizard --data-dir /absolute/path/to/data campaign-report CAMPAIGN_ID
pumpwizard --data-dir /absolute/path/to/data campaign-report CAMPAIGN_ID \
  --previous-report-id PRIOR_REPORT_ID
pumpwizard --data-dir /absolute/path/to/data campaign-alert-ack ALERT_ID
pumpwizard --data-dir /absolute/path/to/data campaign-command CAMPAIGN_ID stop \
  --idempotency-key CAMPAIGN_ID-stop-1 --confirm CAMPAIGN_ID
pumpwizard --data-dir /absolute/path/to/data campaign-status CAMPAIGN_ID
pumpwizard --data-dir /absolute/path/to/data service stop --port 8781
```

Before drawing comparisons, inspect mismatched arm uptime, gaps, source age,
unknown costs, unpriced positions and receipt mismatches. Paper fills remain
modeled evidence; retained transaction receipts stay a separate read-only class.
