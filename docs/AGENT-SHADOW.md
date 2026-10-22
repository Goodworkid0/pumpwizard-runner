# Unsigned agent assessment — dev34

`shadow-agent` evaluates the pinned core-frequency agent against current local
observations, real holdings and unsigned route simulations. It never unlocks a
vault, signs, broadcasts, reserves funds or creates orders. Strategy and wallet
SQLite connections enforce read-only access. The separate `agent-risk.sqlite`
stores assessment reports and executable net high-water values.

## Run an assessment

Use the desktop's existing data directory, a pinned agent and its dedicated cash
reservation. Keep desktop tracking active so observations and SOL/USD inputs are
fresh. Exits require a finalized position owned by this agent; simulated fills
cannot create holdings.

```bash
solanamarket-wizard --data-dir /path/to/data shadow-agent INSTANCE WALLET \
  --cash-reservation CASH_HOLD

# Assess a particular held position.
solanamarket-wizard --data-dir /path/to/data shadow-agent INSTANCE WALLET \
  --cash-reservation CASH_HOLD --mint MINT --position-id POSITION

solanamarket-wizard --data-dir /path/to/data shadow-reports INSTANCE
```

The command runs one assessment cycle. Repeat it for fresh evidence; it does not
start a background trader. `--network devnet` changes the default mainnet network;
`--rpc-env NAME` selects an HTTPS RPC URL from an environment variable.
`--slippage-bps` uses the existing manual-route limits. Without a selected mint,
held positions precede current entry recommendations. `--limit` defaults to 10,
accepts 1–20 and reports deferred candidates. Each candidate is assessed
independently: several ready entries do not establish a simultaneously affordable
batch or reserve their combined spend.

Results are `ready`, `hold` or `blocked`, with a reason and available RPC evidence.
Ready means the candidate passed this assessment; submission still requires a
separate review and authorization. Hold means a fresh net quote produced no exit
trigger. Blocked explains failures such as stale data, insufficient resources,
simulation errors or an unavailable migration route. A completed report returns
CLI status zero even when candidates are blocked; inspect each result's status.
Operational errors return status two.

## Executable net-risk policy

`executable-net-exit-v1` first simulates selling the entire remaining position.
Expected net proceeds are the simulated wallet SOL change, already including
simulated fees and account costs. The conservative floor uses the receipt bound:

```
net_floor_lamports = minimum_output - network_fee - account_funding_allowance
```

`minimum_output` already includes the approved slippage bound. The current maximum
account-funding allowance is 20,000,000 lamports. Applying that allowance remains
deliberately conservative even when simulated output already reflects account
costs. Reports expose expected proceeds and the floor separately. A nonpositive
floor blocks the assessment. The bound does not guarantee a future market fill.

Stops compare the floor at current SOL/USD with remaining actual cost at entry
SOL/USD. Trails compare net USD per remaining token atom with its persisted peak,
once a target has sold. Peaks are keyed by policy version, network genesis,
wallet, agent, position and slippage. The first successful probe establishes the
baseline; earlier spot highs cannot reconstruct historical executable highs.
Peaks survive restart and are shared by shadow assessment and reviewed orders.

Ownership flags and holding limits precede net stops and trails. Unsold market-cap
targets follow them. A partial target gets its own exact-quantity simulation and
must retain a positive net floor. Before signing a reviewed exit, the adapter
reassesses its trigger, route and quantity. Failed or unavailable simulations
never turn into invented zero-value marks or fills.

This policy is separate from the offline portfolio replay and the desktop's
advisory observed-price exit recommendations. Their parity claims do not extend
to this executable policy. Quotes and local observations must remain fresh;
graduation requires a supported canonical native-SOL PumpSwap route.

## Evidence and boundaries

Reports include quote timestamps, route, quantities, fee/slippage bounds,
simulation slots/errors and an unsigned-message hash. They do not store signing
material or the transaction message. The RPC wrapper rejects broadcasting and
signed simulations; the shadow services reject order preparation and submission.
Existing application databases must have the current schema: shadow assessment
does not migrate them.

Synthetic tests cover stops despite profitable spot marks, trails across restart,
partial targets, graduation, stale quotes, simulation failures, insufficient
cash/liquidity and unchanged strategy/wallet journals. This milestone adds no
funded execution or new deployed exit proof. Real unsigned exit evidence and an
explicitly approved funded lifecycle remain acceptance work.

Dev35 adds [independent deployed sell evidence](ROUTE-VERIFICATION.md#dev35-deployed-sell-evidence--september-18-2026)
for one Token-2022 curve and one standard-SPL PumpSwap pool. Those public-holder
route probes do not run the agent adapter or create owned positions. The local
journal inspected during acceptance contained no trade positions, agent positions
or reservations; it was not migrated or seeded to manufacture an agent shadow run.
