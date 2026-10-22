# Security policy

Do not post private keys, seed phrases, wallet files, API keys, transaction signing material, or personal data in issues, pull requests, logs, screenshots, or discussions.

Until a private reporting channel is configured, report suspected vulnerabilities to the repository owner through GitHub's private reporting feature when available. Include a minimal reproduction and impact, but no secrets.

The current development build creates one local Solana wallet only after the user sets a vault password. Its private seed is encrypted locally and must never be copied to an issue, pull request, log, screenshot, discussion, strategy file, or source tree. Losing the vault password prevents recovery of the wallet. Manual trading now has a narrow, explicitly reviewed Pump transaction path. Imported configurations and AI chat cannot invoke its signer. See [manual trades](docs/MANUAL-TRADES.md) for transaction validation, reservations, recovery and route limitations.

Native SOL withdrawals are now available after explicit network/recipient/amount/fee review and password authorization. The transfer service builds only a System Program transfer, validates the network, simulates before signing, and saves the signature before broadcast. AI chat and strategies cannot invoke withdrawals. Uncertain submissions remain unresolved and are never automatically re-signed. Wallet backups contain only encrypted vault data; restore verifies them and preserves the old encrypted vault before replacement. See [wallet transfers](docs/WALLET-TRANSFERS.md) for recovery boundaries and local test coverage.
