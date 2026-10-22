# Route verification — dev65

**Current source update, September 29, 2026:** [creator and holder-reward
support](REWARD-ROUTES.md) adds both reward modes to the existing SOL routes.
The current source also routes graduated buys through canonical PumpSwap
`buy_exact_quote_in`, supports signed virtual quote reserves through a positive
effective-reserve check, and uses the same reviewed journal/receipt path for
direct buy and partial/full exits. Offline fixtures from official PumpSwap SDK
1.20.0 now compare every buy/sell instruction byte, account, signer and writable
privilege in all four supported standard/Token-2022 creator/holder variants. The
pinned IDL SHA-256 is
`2091433899b07d003d98118ae6cd3c628960fd393b40710b6e15bce6d0e7f2d1`; the SDK
tarball SHA-256 is
`c558b4fb8ca7f5cdcd7dfaa23a9b026212380ecf2ba54efe651f264745a7a9f1`.

## Dev65 deployed canonical PumpSwap buy — September 29, 2026

The exact packaged dev65 executable passed a fresh unsigned mainnet buy simulation
for the canonical standard-SPL SOL pool of
`7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump`. The probe used the same public
address retained with the historical sell evidence; no control of that address
is claimed. It simulated 0.001 SOL with a 1% minimum-output bound twice, at slots
**451722931 / 451722932**, with no instruction error and 97,946 compute units.

The expected output was 5,038,412,616 token atoms, the minimum was 4,988,028,489
atoms and the wallet delta was -1,005,000 lamports including the 5,000-lamport
fee. The saved report retains program/account
snapshots and the message hash. Its RPC allowlist excluded submission; the
transaction had zero signatures, and no local wallet, key, vault, journal or
position was opened.

This closes the fresh deployed unsigned buy gate for one canonical standard-SPL
pool. It is not a fill and does not establish Token-2022 buy coverage, general
pool compatibility, a wallet-signed packaged service activation or funded
acceptance. The historical rejection statements below describe older builds and
samples, not the current source support matrix.

## Dev40 deployed Token-2022 PumpSwap sell — September 20, 2026

The supported canonical SOL pool for
`BCdwQBAn8dYB5YjTsoB6TdHAWokxv28k2oZUodERpump` passed both independent unsigned
sell simulations at slots **448799551 / 448799553**. The mint is owned by
Token-2022; the canonical pool is `2DVbU5h8JCd37gaXAJUZ4t77HsjJW22LLduTZk7GSa43`.
The public Pump creator address `49tH7FH5HdTz7orWsLGVbHgtUgdFDGusXBP6CYCrQjLk`
held the simulated tokens and fee reserve. No control of that address is claimed.

The diagnostic quantity was 1,000,000 tokens. Expected net proceeds were
137.820881655 SOL; the 1% minimum-output quote had a conservative net floor of
136.422672788 SOL after the 5,000-lamport fee and 0.02 SOL account-funding bound.
These are historical simulation values, not prices or guarantees for a future
trade. The saved report includes
account owners/sizes, slots, zero-error simulation results, message hash and
public-address provenance. No vault, local position, signed message or submitted
transaction was created.

The initial discovery sample and
paced market-cap sample retain
missing pools, unsupported modes and RPC failures. Many inspected pools use
nonzero virtual quote reserves; those and holder-reward modes remain rejected.
Dev40 improves rejection details without loosening any support gate. These
convenience samples do not measure market-wide route coverage.

This closes the deployed unsigned Token-2022 PumpSwap route-evidence gap for one
supported pool. Funded agent lifecycle acceptance remains separate.

```bash
solanamarket-wizard route-check BCdwQBAn8dYB5YjTsoB6TdHAWokxv28k2oZUodERpump \
  --side sell --sender 49tH7FH5HdTz7orWsLGVbHgtUgdFDGusXBP6CYCrQjLk --amount 1000000
```

`route-check` checks real public account state using the same decoders, derived
addresses, transaction builders and unsigned balance checks as manual trading.
Its RPC allowlist excludes submission. It opens no wallet, private key, vault,
position database or order journal. It never creates a position.

## Run it

```bash
# Account inspection only; no wallet or funds needed.
solanamarket-wizard route-check TOKEN_MINT

# Inspect the canonical exit pool of a graduated mint.
solanamarket-wizard route-check TOKEN_MINT --side sell

# Unsigned buy simulation using a public wallet with sufficient SOL.
solanamarket-wizard route-check TOKEN_MINT --sender PUBLIC_ADDRESS --amount 0.001

# Unsigned sell simulation requires the public address to actually hold tokens.
solanamarket-wizard route-check TOKEN_MINT --side sell --sender PUBLIC_ADDRESS --amount 1000

# Exercise both curve instructions without previously holding this token.
solanamarket-wizard route-check TOKEN_MINT --sender PUBLIC_ADDRESS --amount 0.001 --round-trip

# Optional HTTPS provider, supplied by environment; the URL is not in the report.
solanamarket-wizard route-check TOKEN_MINT --rpc-env SOLANA_RPC_URL
```

Without `--rpc-env`, the command now reads the selected data directory's
[saved RPC connection](RPC-CONNECTION.md), then the network's environment
configuration, then its public endpoint. It reads only connection settings;
wallet and order journals remain unopened. Provider errors retain safe numeric
HTTP/RPC codes and retry delays without exposing credential URLs or raw provider
messages.

Default network: mainnet-beta; `--network devnet` selects devnet. Up to 20 mints
can be checked. Amounts are SOL for buys and six-decimal tokens for sells. Both
`--sender` and `--amount` are required for simulations. Buy probes retain the
manual route's 1 SOL cap; simulations use a 1% minimum-output margin and the
same fee/funding bounds as the manual service. These amounts are never submitted.

`--round-trip` additionally composes the reviewed unsigned curve buy and a sell
of 90% of its simulated output in a single unsigned transaction. It uses the same
pinned instruction builders. The sell has a **diagnostic one-lamport floor**, not
a user-approved executable quote. The combined message cannot enter the manual
signing/journal path and is never persisted; the report contains only its hash.
The probe verifies successful execution of both instructions, bounded SOL debit
and positive retained tokens without consuming pre-existing holdings. It requires
`--sender`, a buy amount and an active curve; it does not simulate graduation.

The command prints JSON. Exit 0 means all requested checks passed **at the
requested evidence level**, 1 means incomplete checks, and 2 means invalid input.
`accounts_supported` is not `unsigned_simulation_passed`. A passed unsigned
simulation is still not proof of a funded fill. Every report sets
`funded_acceptance: false` and `submission_enabled: false`.

## Dev35 deployed sell evidence — September 18, 2026

Independent sell transactions now pass unsigned mainnet simulation on both an
active Token-2022 curve and a canonical standard-SPL PumpSwap pool. Each probe
uses a public holder with actual tokens and SOL, zero signatures, and the same
route builder and balance validation as manual trading. Neither a vault nor a
local position was opened or created.

| Saved report | Route | Simulated token quantity | Simulation slots | Expected net SOL | Conservative net floor SOL |
| --- | --- | ---: | --- | ---: | ---: |
| Token-2022 curve sell | `E2rCtq…QZFqupump` active curve | 1,000,000 | 448155082 / 448155083 | 0.032233325 | 0.011910941 |
| SPL PumpSwap sell | `7LSsEo…vWMpump` canonical pool | 200,000 | 448154923 / 448154923 | 0.038749524 | 0.018361978 |

Both initial and 1% minimum-output simulations had no instruction error. The
network fee was 5,000 lamports each. The conservative floor subtracts that fee and
20,000,000 lamports of account-funding allowance from minimum output, as in the
[net-risk policy](AGENT-SHADOW.md). This records route-level net evidence; it does
not establish an agent trigger, guaranteed future payout or funded acceptance.

The curve sender was the public creator returned by Pump's coin endpoint. The
PumpSwap sender was obtained from a public finalized transaction; its signature
and slot are retained as provenance. No control of either address is claimed.
The reports contain message hashes, not transaction messages or signing material.

The first PumpSwap probe failed local validation
after the deployed instructions succeeded. RPC returned the newly closed
wrapped-SOL account as an empty, zero-lamport, nonexecutable system account rather
than null. The SPL program's [on-chain account deletion helper](https://github.com/solana-program/token/blob/main/program/src/processor.rs)
assigns the system owner and resizes account data to zero. Dev35 accepts this
strict representation and null; nonzero lamports, token ownership, residual data,
executable flags or inconsistent space still reject the simulation. Existing
wrapped-SOL accounts remain blocked before transaction construction, and receipt
reconciliation still requires zero pre/post lamports and no wrapped token balance.
A captured account fixture reproduces the live response in offline tests.

A separate small Token-2022 holding
remained blocked because its simulated sell did not produce positive net SOL.
The route guard was retained. Unsupported modes in other sampled curves also
remained rejected; public listings were only discovery hints.

These results establish independent deployed curve and SPL PumpSwap sell
simulation at the recorded slots. They do not prove Token-2022 PumpSwap deployment,
funded buys/sells, agent reservation accounting, or restart recovery on chain.
The local wallet journal had no trade positions or agent allocations, preventing
a real agent shadow run. Creating fake local fills would not satisfy that gate.
The next acceptance step requires an explicitly budgeted, user-authorized funded
agent lifecycle, with fresh quotes and separate interactive review for each order.

Repeat the public probes while those holdings and routes remain available:

```bash
solanamarket-wizard route-check E2rCtqU72FjMZ5GSH5B4giMrgRRwaJRNbgXQZFqupump \
  --side sell --sender Au5tKDhCUrN2ijzW49KLbVdL5cFX9A4wEZUb5L3znZ2N --amount 1000000
solanamarket-wizard route-check 7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump \
  --side sell --sender F7U3MrnsoZ3umLTmH9Wtae6VGhnWQPRj4Z1Vtv2QSRFs --amount 200000
```

## Dev30 deployed evidence — September 18, 2026

The supported Token-2022 mint `DAiA1LvAPAwxxsnZCaHQXiQrMtwqJrrARBBcmqampump`
passed the unsigned buy probe on mainnet.
Both the initial quote simulation and final 1% minimum-output simulation returned
no instruction error; reported slots were 448070940 and 448070943.

The unsigned buy-then-partial-sell probe
also passed at slot 448072454. It simulated a 0.001 SOL buy followed by selling
90% of the acquired tokens, returned the expected positive token remainder and
bounded SOL debit, and consumed 185,041 compute units within the 400,000 limit.
The public sender was obtained from that coin's public Pump creator field; no
user wallet was opened and no signature was generated or transaction broadcast.

These reports establish that the supported Token-2022 curve instructions executed
in unsigned simulation at those slots. They do **not** establish funded fills,
independently submitted sell transactions, cross-restart on-chain settlement, or
deployed PumpSwap execution. The three Token-2022 migration pools sampled during
dev30 were rejected for unsupported pool modes/reserve/fee extensions. Offline
and desktop tests cover the supported migration subset; its deployed execution
evidence remains pending.

## Dev29 findings — September 18, 2026

- Official mainnet/devnet RPCs returned full genesis hashes. The previous stored
  constants were truncated, so legitimate connections failed identity validation.
  Dev29 fixes both and tests literal independent endpoint values.
- The bundled Pump instruction subset still matches the official pinned IDL at
  `81091419e4457566469d4e2a27f64ed84d42419c`. PumpSwap uses that revision plus the
  remaining-account order independently checked against SDK 1.20.0.
- Eight of the first ten mints sampled from Pump's public recent-trade listing
  were Token-2022. The other two had 49-byte legacy curve accounts and were
  already complete on chain despite the listing's `complete=false` filter.
  Listings are discovery hints; executable routing uses on-chain state.
- The standard-SPL mint `7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump` and its
  canonical pool `GseMAnNDvntR5uFePZ51yZBXzNSn7GdFPkfHwfr6d77J`, also used in the
  official PumpSwap documentation, passed the live account check. See the
  saved account report.
- A probe using the **public example creator address from that documentation**
  stopped before simulation because it did not hold the requested token quantity.
  No user wallet was accessed. The saved blocked report
  retains that failure rather than claiming execution success.

The sample does not estimate market-wide coverage. Dev29 established account
support only; the dev30 reports above add unsigned curve execution evidence.
Funded acceptance remains a separately approved lifecycle.

## Offline and desktop evidence

Tests exercise curve buy → graduation → partial PumpSwap sell → restart → full
sell, exact allocation accounting, changed routes, SDK instruction/account parity,
unsupported pools, existing wrapped SOL, stale state and inconsistent receipts.
The desktop smoke runs that lifecycle on an isolated display with throwaway
keys, synthetic RPC and real connections blocked. No local-validator Pump program
harness or cross-platform package verification is claimed by these tests.

Dev30 adds SDK instruction fixtures for Token-2022 buys, sells and migration
exits, and official SPL layout/metadata codec fixtures. The corresponding capture
scripts are `packaging/capture_pump_sdk_fixture.cjs`,
`packaging/capture_pump_swap_sdk_fixture.cjs` (set `PUMP_TOKEN_2022=1` for each),
and `packaging/capture_token2022_layouts.cjs`; they disable network connections.
`PUMP_SDK_NODE_MODULES` selects an isolated installation of Pump SDK 2.0.0,
PumpSwap SDK 1.20.0 and the recorded SPL codec versions. Tests reject unknown or
malformed extensions, mutable/mismatched metadata, token-program changes between
reads and incorrect/missing program identity in receipts. The desktop smoke runs
both standard-SPL and Token-2022 buy → graduation → partial/full exit lifecycles.

## Protocol sources

- [Pinned Pump interface](https://github.com/pump-fun/pump-public-docs/blob/81091419e4457566469d4e2a27f64ed84d42419c/idl/pump.json)
- [Pinned PumpSwap interface](https://github.com/pump-fun/pump-public-docs/blob/81091419e4457566469d4e2a27f64ed84d42419c/idl/pump_amm.json)
- [Canonical pool and account documentation](https://github.com/pump-fun/pump-public-docs/blob/81091419e4457566469d4e2a27f64ed84d42419c/docs/PUMP_SWAP_README.md)
- [Official PumpSwap SDK 1.20.0](https://www.npmjs.com/package/@pump-fun/pump-swap-sdk/v/1.20.0)
- [Solana unsigned simulation](https://solana.com/docs/rpc/http/simulatetransaction)
- [Solana Token-2022 metadata layout and behavior](https://solana.com/docs/tokens/extensions/metadata)
