# Local evaluation — dev41

Select an imported agent and choose **Local evaluation**. This connects the
existing strategy import, offline replay and unsigned assessment workflows.
Only the exact pinned core-frequency configuration without trial overlays can
run this replay. Other monitoring agents remain research-only; the workspace
explains their support boundary and disables replay/shadow actions.

1. Read **Readiness & next steps** for package identity, engine support, route
   limits and evidence scope.
2. Choose **Run synthetic example** to learn the workflow without network or
   wallet access. It bundles the unchanged `examples/core-frequency-replay.json`.
   Its small synthetic 1970 input timeline and modeled profit are not market evidence.
3. Choose **Run recording…** for an existing
   `pumpwizard.portfolio-recording/v1` file. Its configuration must match the
   imported strategy. Files are limited to 16 MiB and 20,000 batches, with strictly
   increasing finite timestamps. Empty/duplicate/backward batches are rejected
   where applicable; empty quote arrays remain valid recording gaps.
4. Inspect **Replay reports** and **Events & inputs**. Every evaluation starts at
   the configured virtual budget, independently of existing CLI replay runs.
   Cancel or close the workspace to stop an unfinished replay. Only completed
   runs are published; a run that already committed remains available.
5. **Export selected report…** saves full configuration, recording, checkpoint,
   events, sampled equity and summary. The export contains private research inputs;
   inspect it before any optional sharing. It contains no wallet keys or signing capability.
6. **Open unsigned assessment** opens Agent trading → Candidates; **Open worker
   rehearsal** opens its worker tab. These buttons do not start assessment, start
   tracking, allocate funds or authorize signing. Inspect setup and collect fresh
   observations before explicitly starting an unsigned assessment/worker.

Shadow entry checks need an actual matching allocation; exit checks need actual
finalized agent-owned positions. Offline modeled holdings cannot supply either.
A missing allocation or unsupported/stale route remains a visible block, not a
reason to manufacture balances. Every real order remains separately reviewed.

## Durable report identity

`local-evaluations.sqlite` is separate from strategy, wallet and existing
`strategy-replays.sqlite` journals. Reports persist across closing/reopening the
app and are listed only for the exact imported package. The last 100 reports are
shown. Identical package/input/engine/build/schema identity reuses the saved
report, including its original completion time. Different recording content or
package metadata creates a separate report; no existing agent settings change.

The local report schema is `solanamarket.local-evaluation/v1`. It is a desktop
output artifact, **not a new strategy import format or an agreed public evidence
bundle**. It binds the existing canonical package hash, parsed-rules hash,
canonical recording hash, engine version, application version and input period.
Whitespace in the downloaded file is not part of canonical identity. Exports
carry the full inputs so another local run can reproduce the modeled result with
the same engine/build. Input source authenticity and collection completeness are
not established by a successful replay.

Summary equity/drawdown/fees are modeled values at supplied batch times. Missing
sell quotes may mark open positions at zero. There is no performance threshold
that promotes a strategy to execution support. Replay does not establish live
timing, profitability, route availability or funded acceptance.

The existing CLI remains unchanged. An additional command produces the same
bound report for an already imported agent:

```bash
./launch.sh --data-dir /path/to/local/data evaluate-recording INSTANCE_ID recording.json
```

## Unsigned report provenance

New shadow and worker reports add `identity` (package/rules hashes and engine/build
versions), risk policy, slippage, assessment time bounds, and per-candidate
`local_input` observation/hash. Their existing report schemas and fields remain
readable. Older shadow reports explicitly show that configuration identity was
not recorded; it is never retroactively inferred from the current agent.

These observation snapshots and RPC slot/message evidence explain an unsigned
assessment, but they are **not a complete offline recording of live RPC state**.
Local observations can change during collection; reported snapshots are provenance,
not a claim that the whole live assessment is deterministically reproducible.
Reports without a reachable local agent may lack identity; no identity is invented.

Funded lifecycle acceptance remains [pending](FUNDED-ACCEPTANCE.md). Automatic
execution remains disabled. See [the shared handoff coordination note](STRATEGY-HANDOFF.md).
