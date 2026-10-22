# Website → local strategy onboarding

Dev41 includes this onboarding update and a [local evaluation workspace](LOCAL-EVALUATION.md).
The previous dev40 bundle is retained. The website repository is maintained
separately and has not been modified by this batch.

Import config, Strategies → Compatibility & evidence, and each agent's
Compatibility & evidence tab show locally computed support, hashes and evidence
boundaries. Import creates an immutable library record; the separate **Create
agent · entries disabled** action creates a monitoring agent without starting
tracking. Reimporting an exact package can open the existing agent without
changing its settings. A changed package retains previous agents unchanged.
The import result compares rule fields against saved packages with the same ID.

| Local classification | Available behavior | Evidence boundary |
| --- | --- | --- |
| Research · inspection | Store, inspect and export existing catalog packages; no monitoring agent | Missing engine contract or unsupported trial overlays |
| Research · monitoring | Independent local observations and advisory decisions | Monitoring compatibility does not establish portfolio parity or execution support |
| Reviewed eligible | Pinned replay, unsigned shadow/worker rehearsal and separately reviewed orders after setup | Exact `core-frequency` ID, bundled rules, `simulation_unverified` policy and no trial overlay; funded acceptance still pending |

Rehearsal is an operating mode, not a promotion to funded acceptance. Its `ready`
result is historical unsigned evidence, never an order or signing permission.
Every actual order requires current route and risk checks plus explicit approval.
Automatic execution remains disabled, including when an older agent has saved
`automatic` entry/exit preferences.

Agent trading → **State & recovery** explains tracking state/faults, recent local
decisions, setup blocks, expired reviews, unresolved signatures and manual-exit
positions. Current worker health is in Worker rehearsal; current authorization
is in Signing session. A stopped worker or expired session does not close holdings.

## Current website coordination contract — no schema change

Dev41 exports `solanamarket.local-evaluation/v1` as a local result artifact with
existing package/recording content, hashes, engine/build versions, input period,
modeled events and final state. It does not change the accepted strategy or
recording schemas and is not yet an agreed website ingestion/publication contract.
New unsigned shadow/worker report fields are additive provenance only.

Continue exporting `pumpwizard.strategy-handoff/v1` or the existing native
`pumpwizard.strategy/v2`. Catalog exports retain `strategy.id`, `strategy.label`,
the full `configuration`, and any `trial` metadata. Do not strip an overlay or
rename a definition to obtain eligibility. Native v2 monitoring support alone
does not confer reviewed agent execution support.

The desktop's package identity is SHA-256 of its existing canonical JSON:
UTF-8, sorted object keys, compact separators, unescaped Unicode, finite numbers.
It includes labels and all supplied metadata; it is not the raw download-file
hash. Native v2 includes a strategy version; legacy v1 has no required strategy
revision, so the desktop displays that absence and uses the content digest.
The displayed rules digest covers parsed `effective_rules` (`configuration` in
v1, `rules` in v2). Neither digest authenticates the publisher. Other-language
exporters should verify canonical bytes against shared fixtures before claiming
hash equality, especially for number serialization.

Catalog simulation performance/freshness stays outside `package_for` exports, so
refreshing results does not change imported rules. For downloads, retain stable
package bytes and present experiment provenance, observation period, costs,
coverage and paper/unsigned/funded evidence separately. The current importer does
not authenticate an evidence manifest, fetch linked evidence, or enforce arbitrary
exported route/capability claims. It derives eligibility locally. Adding metadata
inside a package changes its package identity even if the rules are unchanged.

The immutable configuration/evidence bundle in YEAR-ONE-PLAN remains a proposed
shared contract, not an implemented new schema. Coordinate a separate versioned
manifest and cross-language hash fixtures before changing the handoff. Agree on
experiment/revision ID, exact package and input hashes, engine requirements,
declared routes, evidence kind/time/coverage and validation rules. Unsupported
requirements must fail explicitly; website evidence cannot authorize local
wallet actions or silently update an authorized agent.

Existing route scope: active native-SOL Pump curves for entries/exits and
supported canonical native-SOL PumpSwap migration exits. Standard SPL and only
the validated immutable Token-2022 metadata subset are supported. Broader DEXs,
post-graduation entries and unsupported pool modes remain unavailable. Show this
scope alongside downloads; catalog size and unsigned route samples do not
establish compatibility for all definitions or tokens.
