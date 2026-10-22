# Roadmap

The current source includes local research, a browser dashboard, durable background jobs, wallet workflows, shared session allocations and recorded order/decision views. See [CHANGELOG.md](../CHANGELOG.md) for the version history.

Further validation focuses on:

1. Reproducible route checks and complete receipt/accounting reconciliation.
2. Restart, interruption, recovery and long-running observation behavior.
3. Exact-package acceptance for each supported operating system.
4. Separately authorized, bounded funded lifecycles with explicit wallet, route, budget and duration limits.
5. Prospective strategy evaluation using recorded inputs and clearly stated cost assumptions.

Passing source tests does not establish sustained funded operation or profitability. New routes and strategy semantics require fixtures and parity tests; source publication does not relax execution gates or authorize trading.

See [funded acceptance](FUNDED-ACCEPTANCE.md), [service signing](SERVICE-SIGNING.md), [verification status](VERIFICATION-STATUS.md) and [release-note requirements](PATCH-NOTES.md).
