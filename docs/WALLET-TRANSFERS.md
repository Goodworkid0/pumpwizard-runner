# Wallet transfers — dev23

The desktop can receive native SOL at its wallet address and submit an explicitly approved native SOL withdrawal. The separate dev28 manual Pump route is documented in [manual trades](MANUAL-TRADES.md). PumpSwap exits, token withdrawals, full deposit-history indexing and automated spending remain future work. The existing loopback-only test signer has not been widened.

## User workflow

1. Choose Withdraw beneath the wallet, or Settings → Wallet & backup.
2. Select mainnet-beta (real SOL), devnet (test SOL), or localnet (localhost:8899). The header balance always remains a mainnet balance; the wallet window labels its selected network balance separately.
3. Enter the full recipient wallet address and SOL amount. Review checks network identity, account type, fee, balance, and rent requirements. Amounts are converted exactly into integer lamports, never binary floating-point money.
4. The confirmation lists the network, full sender/recipient, amount, fee, and total. Canceling sends nothing. The vault password then authorizes this one transfer. A stale preview must be requested again.
5. The app simulates the unsigned transaction, signs locally, journals the signature durably, and submits that same transaction. The history distinguishes pending, unknown, confirmed, finalized, and failed. Double-click a row to copy the signature.

RPC endpoints are currently the fixed official mainnet/devnet endpoints and localhost; custom RPC configuration is not yet exposed in the UI. The transfer client refuses redirects and verifies the mainnet/devnet genesis identity. It can build only one System Program native SOL transfer. AI chat and imported strategy configurations have no path to invoke it.

## Backup and restore

Encrypted backup verifies the current password and writes only authenticated ciphertext, using a new filename and private file permissions. It will not overwrite another file. The backup still requires the same password. Keep both; a backup does not make a forgotten password recoverable.

Restore validates the backup before any replacement, checks the reviewed address again, and preserves the existing encrypted vault in a `wallet-before-restore-*.json` file beside the active vault. The app closes after switching; reopen with the restored wallet's password. Pending/unverified withdrawals must be resolved before switching. First launch also offers restoring a backup instead of creating a new wallet. On POSIX the backup file must have private permissions (0600), just like the active vault.

## Recovery and known boundaries

The separate local `wallet-transfers.sqlite` journal contains public transfer details and signatures, never keys, passwords, or broadcastable transaction payloads. A signature is recorded before any broadcast. One unsettled withdrawal per wallet/network is enforced in SQLite, including across simultaneous app processes. Confirmed transfers are tracked through finality before a subsequent withdrawal is allowed.

Timeouts can occur after a node accepted a transaction. The app checks the original signature instead of creating a replacement. A missing signature after blockhash expiry is not sufficient proof of nonpayment: it remains unknown and blocks further withdrawals until network evidence resolves it. This alpha does not include an override for that condition. Confirmation is reported separately from RPC acceptance.

No mainnet transfers were made during development. Local-validator integration proves native transfer encoding, signatures, fee handling, recipient account creation, backup/restore signing, and journal recovery; it does not establish production RPC availability or validate Pump trading routes.

## Local integration test

Start a validator in a new temporary ledger:

```bash
solana-test-validator --ledger /tmp/solanamarket-withdrawal-test --bind-address 127.0.0.1 --rpc-port 8899
```

Then run:

```bash
RUN_WITHDRAWAL_LOCALNET=1 PYTHONPATH=src python3 -m pytest tests/test_withdrawal_localnet.py
```

The test creates throwaway encrypted wallets, requests only localhost faucet SOL, restores a backup, submits one native SOL transfer, reopens the journal, waits for finality, and checks exact balances including the fee. The ordinary suite skips this test unless explicitly enabled.

Protocol references: [Solana fee lookup](https://solana.com/docs/rpc/http/getfeeformessage), [simulation](https://solana.com/docs/rpc/http/simulatetransaction), [submission](https://solana.com/docs/rpc/http/sendtransaction), [signature status](https://solana.com/docs/rpc/http/getsignaturestatuses).

Dev28 shares a wallet/network submission reservation between native withdrawals and manual trades. Either type of unresolved operation blocks another, and both must be resolved before wallet switching. Do not run an older executable concurrently with a wallet enabled in dev28.
