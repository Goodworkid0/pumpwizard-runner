# Local RPC connection — September 25, 2026

The shared public Mainnet RPC returned HTTP 429 for `getTokenLargestAccounts`,
with `Retry-After: 10`. Genesis identity, current blockhash, signature history
and a transaction read passed in the same bounded diagnostic run. See the
credential-free evidence. Earlier
generic connection errors obscured this distinction.

The user chose to start with a free provider account. Helius's
[published plans](https://www.helius.dev/docs/billing/plans) currently list a
$0 browser Free plan with one million monthly credits and 10 RPC requests per
second. Its separate automated Agent signup requires a payment and is not the
selected route. A free account still has method and usage limits; no private
provider acceptance was claimed at the initial setup checkpoint. The user has
since supplied a Helius key: Mainnet identity/blockhash checks passed, settings
were saved locally, and the affected unsigned route probes passed. See the
[signing and connection follow-up](WALLET-SIGNING-COMPATIBILITY.md).

## Setup in the running browser app

1. Sign up at the [Helius dashboard](https://dashboard.helius.dev/) using the
   browser flow and choose Free. No paid subscription is needed for this step.
2. Copy the dashboard's complete **Mainnet RPC URL**, including its API key.
3. Refresh the local Runner at `http://127.0.0.1:8765/`, open **RPC connection**,
   and paste the URL into **Mainnet RPC URL → Test & save**. The button is also
   available before wallet sign-in.
4. Wait for Mainnet verification, then request a fresh order review. Never paste
   the key into chat, diagnostics, a screenshot or a repository file.

The setup test only reads genesis identity and the latest blockhash. It does not
use a wallet, sign, simulate or send a transaction, or consume SOL. This basic
check does not prove that every trading RPC method is available. An unsigned
route review is the next check after setup; funded acceptance remains separate.

## Implementation and limits

- The URL is stored atomically in `rpc-settings.json` inside Runner's data
  directory. On this Linux system it has mode `0600`. This is a plaintext local
  credential file, not an encrypted vault. It is not returned in API responses
  or diagnostic reports and is not stored in browser local/session storage.
- Only HTTPS URLs without user-info or fragments and with the default HTTPS
  port are accepted. Redirects are rejected. A wrong network, invalid response,
  failed authentication or failed health check leaves the prior file intact.
- Browser balances, reviewed manual/strategy orders, withdrawals, receipt
  reconciliation and strategy-allocation balance checks use the saved Mainnet
  endpoint. Each new operation resolves current settings; no restart is needed
  after saving. An operation already in progress retains its RPC instance.
- Mainnet precedence is saved settings, then `SOLANA_RPC_URL`, then Solana's
  public endpoint. Removing saved settings restores that fallback sequence.
  Devnet continues to use `SOLANA_DEVNET_RPC_URL` or its public endpoint.
- `route-check` uses these settings from the selected data directory unless
  `--rpc-env` explicitly selects another environment variable. It still opens no
  wallet, position database or order journal. Legacy native surfaces and other
  CLI execution commands retain their existing configuration paths.
- RPC errors now distinguish HTTP rate limits, denied access, unsupported
  methods and lagging nodes. Numeric HTTP/RPC codes and numeric retry delays
  are retained without provider messages or endpoint URLs. No automatic retry
  of a transaction broadcast has been added.
- Saving a provider does not grant signing permission, authorize a trade,
  enable the Automation Wallet, or change the separate chart-history source.

## Verified source checkpoint

Browser lint, TypeScript/Vite production build, **153 focused Python tests** and
**17 Playwright flows** passed. New tests cover credential redaction, file
permissions, network rejection, failure preservation, configuration precedence,
read-only health checks, provider error reporting and no automatic broadcast
retry. Browser setup/persistence tests use a synthetic provider, not a real
Helius account. The existing synthetic trading and restart flows also passed.

The source Runner on port 8765 was restarted after confirming empty trade,
transfer and strategy-order journals. Database backup:
`/tmp/pw-source-start-backup-mh4usvix`. Assets: `index-BS5OEdOH.js` and
`index-C9y9FSPC.css`. The live dialog screenshot
shows the initial public connection without a credential. This is the earlier
startup checkpoint; Helius is now configured as recorded in the follow-up above.
No real transaction has been sent. Version remains `0.2.0.dev60`;
there is no new packaged build, Community publication or website edit.
