# PumpWizard Runner

A local-first application for Solana strategy research, market monitoring and wallet workflows. A Python service manages local data and background jobs; a React and TypeScript interface runs in your browser.

The companion [PumpWizard Website](https://github.com/Goodworkid0/pumpwizard-website) provides hosted market research, paper-trading dashboards and reproducible replay. The runner operates independently: importing a strategy is a local file or paste operation and does not require a website account or hosted signal service.

## Current source: 0.2.0.dev121

This development version includes:

- Monitoring and trading workspaces, saved strategies and shared session allocations.
- Background observation, research jobs, paper evaluation and replay that continue independently of the browser.
- Wallet, holdings and position views, recorded balance history and token charts.
- An Activity workspace with order receipts, decision explanations, filters, pagination and CSV exports.
- Encrypted local wallet storage, explicit signing authorization, bounded allowances and recovery workflows.
- An optional AI assistant with request previews and separate approval before provider calls or expanded context access.

Source publication is separate from a packaged release. See [Releases](https://github.com/Goodworkid0/pumpwizard-runner/releases) for published downloads and [CHANGELOG.md](CHANGELOG.md) for version history. Publishing source does not establish funded-trading reliability or profitable strategies; route, authorization and execution checks remain enforced by the application.

## Technology

Python 3.11+, SQLite, React, TypeScript, Vite, Solana RPC/solders, cryptography, pytest and Playwright. PyInstaller packaging is provided for standalone builds.

## Run from source

Install Python 3.11+ and Node.js 22, then run from the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cd web-client
npm ci
npm run build
cd ..
.venv/bin/solanamarket-wizard --data-dir /tmp/pumpwizard-local-evaluation browser
```

On Windows, use the corresponding executables under `.venv/Scripts/`. Use a dedicated data directory for evaluation. The local API binds to loopback; wallet unlock, collection and signing are separate operations.

For background operation, use the [service guide](docs/BACKGROUND-SERVICE.md). Review [backup and restore procedures](docs/PILOT-LINUX-CANDIDATE.md) before opening existing data with a newer version.

## Architecture

- **Local service:** owns the data directory, authenticated loopback API, background jobs and recovery state.
- **Browser client:** displays account, research and strategy state and submits explicit user actions.
- **Strategy library:** validates versioned configuration data; imported strategies are not arbitrary executable code.
- **Wallet and execution:** keeps monitoring, wallet ownership, signing permission and spending limits separate.
- **Research evidence:** records inputs, decisions and receipts; missing observations or valuations remain visible.

See the [documentation index](docs/README.md), [security policy](SECURITY.md) and [contribution guide](CONTRIBUTING.md).

## Development checks

```sh
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/python packaging/check_release_note.py
cd web-client
npm run build
npm run lint
npx playwright install chromium
PUMPWIZARD_E2E_PYTHON=../.venv/bin/python npx playwright test
```

Browser tests use synthetic fixtures and a separate local test server. Results for this source publication are recorded in [verification status](docs/VERIFICATION-STATUS.md). Tagged releases require a matching published Community note, as described in [release-note requirements](docs/PATCH-NOTES.md).

## License

MIT. See [LICENSE](LICENSE).
