# Dev61 reviewed browser lifecycle source checkpoint

Verified September 25, 2026. This is unversioned source preparation for Dev61;
the application still identifies as **0.2.0.dev60**. No funded transaction,
version bump, package release or Community publication was performed. The
existing dev60 draft validates locally; its live publication was not checked.

## Behavior now covered

Order review identifies the exact wallet, signer type, mint, side, spend/token
quantity, route, slippage, minimum route output, estimated network fee, configured
priority fee (zero), wallet delta, maximum buy debit and quote expiry. The
browser invalidates changed or expired context before and after a wallet prompt.
Wallet rejection discards the review. Locking invalidates the signer; generated
wallets clear their active secret byte buffer. Python still validates the exact
signed message and applies the shared route, balance, risk and reservation gates.
Expiry and the selected position snapshot are checked inside the wallet lock.

Portfolio and the order desk show every retained signature and its state:
pending, pending final receipt, unknown, finalized or failed. Receipt recovery
runs on sign-in, return to a visible tab, explicit refresh and every eight seconds
while unresolved or recovering from an error. Requests are serialized. No
unsigned, simulated or merely submitted order changes the position ledger.
An uncertain send is reported as unknown and retains its original signature.
The explorer link is for inspection; it is never an accounting source.

Finalized holdings show remaining quantity and basis, net sale proceeds,
transaction fees and realized SOL P/L. Closed positions remain visible. Fees
already included in basis/proceeds are not subtracted again; finalized failed
sell fees are included in realized loss. Browser quantities use integer strings
and retained token decimals. Current USD comparisons require retained entry FX
and a positive local quote no older than 90 seconds. Missing evidence is not
replaced with a zero cost or invented price.

## Verification

- **219 Python checks passed** across manual trades, browser trading, agent
  execution, reservations, signing sessions, wallet risk, PumpSwap and Token-2022.
- **17 web API checks passed** with loopback permission: **236 distinct focused
  Python checks total**, not the entire repository test suite.
- Browser lint and the production TypeScript/Vite build passed.
- **12 production-bundle Playwright flows passed**, including the full synthetic
  lifecycle and the existing onboarding, market, history and strategy flows.
- The final Portfolio screenshot was visually inspected and retained as
  synthetic Portfolio evidence.
- The evidence record contains
  source hashes, command results and exact synthetic accounting totals.

The end-to-end lifecycle uses the actual browser-generated signature, Python
transaction builder, durable journal and finalized-receipt validator. Only the
Solana RPC boundary is replaced by offline account/receipt fixtures. It proves:

1. a 0.1 synthetic SOL buy remains pending without a position until finalization;
2. the resulting 1,000-token holding enters a reviewed 400-token partial sell;
3. duplicate clicks produce one submission; a simulated send timeout retains an
   unknown order and the unreduced local quantity across lock/unlock;
4. Python is re-executed with the same journal and synthetic chain checkpoint,
   requiring a new browser sign-in; original-signature recovery leaves 600 tokens;
5. a fresh review sells the final 600 tokens and closes exactly one position; and
6. three finalized receipts agree with the final synthetic balance and fees.

| Synthetic accounting item | Exact value |
| --- | ---: |
| Starting balance | 1.25 SOL |
| Buy wallet debit, including fee/account funding | 0.10204428 SOL |
| Partial-sell net proceeds | 0.039995 SOL |
| Full-sell net proceeds | 0.059995 SOL |
| Total transaction fees | 0.000015 SOL |
| Ending balance | 1.24794572 SOL |
| Realized P/L including account funding | −0.00205428 SOL |
| Remaining token quantity and basis | 0 tokens / 0 SOL |

Additional browser cases cover rejection, account/network changes and locking
during approval, expiry before or during a prompt, stale balance, failed unsigned
simulation and a lost HTTP submission response followed by a fee-only failed
receipt. Python cases also cover insufficient SOL, blockhash expiry, modified
signed messages, selected-position changes including at lock acquisition, and
requiring a fresh review after curve-to-PumpSwap migration.

The first web API run hit four sandbox socket-denial failures; all 17 API tests
passed with the required local socket permission. The first browser restart
assertion incorrectly expected a different PID after `exec`. The test now checks
a new process-start identifier; the complete final browser run passed.

To reproduce from the repository, using a Python environment with the project
and test dependencies installed:

```sh
PYTHONPATH=src python -m pytest tests/test_web_trading.py tests/test_manual_trades.py tests/test_agent_execution.py tests/test_reservations.py tests/test_signing_sessions.py tests/test_wallet_risk.py tests/test_pump_swap_route.py tests/test_token2022_route.py tests/test_web_api.py
cd web-client
npm run lint
PUMPWIZARD_E2E_PYTHON=/path/to/project/python npm run test:e2e
```

The browser test server uses port 8766 by default, separately from the ordinary
8765 preview. `PUMPWIZARD_E2E_PORT` can override it. Synthetic controls and the
restart fixture exist only in `tests/`; they are not production API endpoints.

## Remaining gate

This establishes deterministic source behavior, not production trading
acceptance. The next funded run requires the user's explicit wallet, exact token
mint and maximum total SOL budget, followed by a fresh review for every buy or
sell. Retain real finalized receipts and balance deltas for buy → partial sell →
Runner restart → full sell, as described in [funded acceptance](FUNDED-ACCEPTANCE.md).

Real Phantom/Backpack/Solflare transaction prompts, owned-position mainnet
migration, browser withdrawal, unattended execution and Windows/macOS package
acceptance remain pending. Dev62's strategy-to-order bridge and Dev63's separate
bounded Automation Wallet remain later work. The strategy exchange formats and
the sibling website were not changed. The public preview process was not
restarted or claimed as validated against this source checkpoint.
