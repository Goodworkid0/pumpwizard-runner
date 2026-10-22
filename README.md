# PumpWizard Runner

PumpWizard Runner is an open-source, local-first desktop application for running a supported PumpWizard strategy configuration with the user's own market data, research history, wallet, and position library.

This repository is independent at runtime. Importing a configuration is a local paste or file operation: the app does not need a PumpWizard account, signal feed, hosted service, or license server to operate.

The companion [PumpWizard website](https://github.com/Goodworkid0/pumpwizard-website) provides hosted market research, paper-trading dashboards and replay tools. This repository contains the independent local runner.

## Current build: 0.1.0

The first development build provides a local strategy library, compatibility inspection, a native desktop shell, and a command-line interface. It does **not** yet monitor markets, connect a wallet, or submit a transaction. Those capabilities will be added in public, versioned milestones; the app never presents an unimplemented strategy as executable.

Download a ready-to-run build from [Releases](https://github.com/Goodworkid0/pumpwizard-runner/releases). Extract the archive and run PumpWizard. The build includes Python and its required files.

To run from source:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .[dev]
.venv/bin/pumpwizard desktop
```

## Importing a strategy

The app can import an existing `pumpwizard.strategy-handoff/v1` JSON handoff or a native `pumpwizard.strategy/v2` package. Version 0.1.0 stores legacy handoffs for inspection and reports their execution compatibility as unavailable; it does not quietly reinterpret their simulation fields as a live strategy.

```bash
pumpwizard import examples/xyz-local-strategy.json
pumpwizard strategies
pumpwizard status
```

Local application data defaults to a platform-specific user directory. Override it for testing or a portable install with `--data-dir PATH`. It contains the strategy library and later releases will add the encrypted wallet material and position ledger. It is never stored in the Git repository.

## Design commitments

- The app has its own data collection, research, strategy evaluation and position library.
- A copied strategy config is data, never arbitrary executable code.
- Strategies cannot enable real execution, choose a wallet, alter the RPC endpoint, or expand the user's limits.
- The strategy engine and its data semantics are versioned. An unsupported config cannot run as a near match.
- A user can choose automatic or manual position management when execution support lands.
- A release is retained as a runnable download even when a newer build fails.

The roadmap and compatibility rules are in [docs/ROADMAP.md](docs/ROADMAP.md). See [SECURITY.md](SECURITY.md) before reporting a vulnerability or using a future wallet feature.

## Contributing

Install the development extras and run:

```bash
pytest
ruff check .
```

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

