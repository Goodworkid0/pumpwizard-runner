# Source verification — 0.2.0.dev121

Validated on October 22, 2026, in an isolated source checkout on Debian 12 with Python 3.11 and Chromium.

| Check | Result |
| --- | --- |
| Python suite | 1,249 passed; 1 skipped |
| Python lint | Passed with Ruff 0.13.2 |
| Frontend production build | Passed |
| Frontend lint | Passed |
| Browser coverage | 122 distinct checks passed across the complete run and targeted reruns; 2 skipped |
| Prepared version note | Valid for 0.2.0.dev121; Community publication not checked |
| Secret scan | No findings after review of three non-secret prose/idempotency-label matches |
| Local documentation links | Passed |

The initial complete browser run passed 116 checks and exposed six stale UI expectations or test synchronization failures. The assertions were updated to the current labels, navigation and strategy-library layout. The synthetic sell test now waits for submission before asking the fixture to finalize its receipt. All six corrected checks and the updated welcome-screen link check passed in the final targeted run. The source update keeps the complete suite enabled in CI.

The skipped Python integration test requires an explicitly enabled local Solana validator. No funded transactions or live strategy acceptance were performed for this publication. Source tests do not establish profitability or sustained funded operation.

Publication preparation also fixed one Python lint violation without changing behavior and included an unsigned-message test fixture previously hidden by the broad wallet-file ignore pattern. Local output, agent instructions and development handoff records are excluded from the public source.

This is a source update, not a tagged binary release or service installation. The existing Community post remains a draft; tagged releases still require the matching published note and live release gate.
