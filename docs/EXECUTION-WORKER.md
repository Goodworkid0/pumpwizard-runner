# Continuous unsigned worker rehearsal — dev39

The worker repeatedly assesses the pinned core-frequency agent using the existing
Pump.fun curve and supported canonical SOL PumpSwap exit paths. It accepts no
wallet key or signing session, prepares no orders and cannot broadcast. Actual
trades still require a separate fresh review and explicit authorization.

## Desktop

Open **Test setup / Agent trading → Worker rehearsal**. Start market tracking for
fresh observations, choose the active cash allocation and slippage, then set the
interval (5–300 seconds) and candidates per cycle (1–10). **Start unsigned worker**
captures these settings for that run. Stop and start again to apply changes.
Without an allocation, eligible candidates report a funding/setup block.

The tab displays current health, next check, consecutive RPC errors and the latest
candidate results. **Export latest cycle** saves a JSON report. A `ready` result
is historical simulation evidence, not a reserved quote or trade authorization.

**Pause entries** leaves managed positions eligible for exit assessment. **Stop
worker** stops both entry and exit assessment by this worker. Market tracking is
separate. Changing network, switching agent, closing this workspace or closing
the application requests a stop; an active public RPC finishes before shutdown,
and its late result is discarded. Opening the app again never restarts a worker.

## Cycle behavior

1. Verify the RPC network and worker lease.
2. Reconcile existing wallet trade/withdrawal signatures using the normal receipt
   checks. An unresolved signature blocks further assessment. Recovery may update
   existing financial records; it never signs, replaces or resubmits a transaction.
3. Select actual agent-owned managed positions first. Rotate batches so positions
   beyond the batch limit are still assessed. Use spare batch slots for eligible
   entry signals; exclude previously attempted entries and manual-exit handoffs.
4. Check an available matching cash allocation, fresh strategy observations,
   executable route/simulation and existing risk rules through the shared agent
   service. Persist unsigned results and separate net-exit risk evidence.
5. Wait for the configured interval. RPC failures use exponential retry backoff
   capped at 300 seconds. Lease heartbeats continue while waiting. Other fatal
   failures stop the worker and require an explicit restart.

A SQLite lease prevents concurrent workers for the same agent, wallet and network.
A crashed worker's lease expires after 90 seconds; a replacement still requires
explicit start. The worker saves its latest 500 cycle reports per scope in
`execution-worker.sqlite`, and exposes the latest 30 through status/history.
Ordinary assessments open wallet and strategy journals read-only. The separate
worker and risk evidence stores are writable. No allocations are created.

## CLI

```bash
./launch.sh rehearse-agent AGENT_ID PUBLIC_WALLET_ADDRESS --network mainnet-beta
./launch.sh rehearse-agent AGENT_ID PUBLIC_WALLET_ADDRESS --network mainnet-beta --watch --interval 15 --limit 3
./launch.sh worker-status AGENT_ID PUBLIC_WALLET_ADDRESS --network mainnet-beta
```

The first command runs one cycle. `--watch` continues until Ctrl+C, which requests
a graceful stop. Optional `--cash-reservation ID` pins an allocation; otherwise
the largest available matching allocation is selected. `--rpc-env ENV_NAME`
selects an RPC URL from the environment. `worker-status` reads saved history
without creating a store or contacting RPC. Never put wallet secrets in commands.

## Acceptance and remaining work

Synthetic tests exercise repeated unsigned cycles without financial mutations,
pending/finalized receipt recovery, paused exits, PumpSwap migration, stale inputs,
RPC failures, duplicate/expired leases, long idle heartbeats and Stop during RPC.
Desktop acceptance covers start, stop, saved results and close/reopen behavior.
These checks do not establish funded mainnet execution or deployed Token-2022
PumpSwap exit acceptance.

Dev40 adds wallet portfolio valuation on every cycle when its limit is configured,
including with paused entries. It stores evidence separately, does not write
financial journals, and does not block managed exits when valuation fails.
See [portfolio controls](WALLET-RISK.md). Deployed Token-2022 PumpSwap unsigned
sell evidence is now recorded in [route verification](ROUTE-VERIFICATION.md).

Complete the funded reviewed lifecycle before enabling automatic signing.
Future DEX adapters should reuse the shared agent,
risk, authorization and receipt paths; this scheduler contains no new route
builder or DEX-specific accounting. Pump.fun remains the current scope.
