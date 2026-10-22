import { useState } from 'react'
import type { WalletSession } from './browserWallet'
import { HistoryChart } from './PortfolioDashboard'
import type { useWalletBalanceHistory } from './useWalletBalanceHistory'

export function WalletBalanceHistory({ wallet, history, close }: {
  wallet: WalletSession
  history: ReturnType<typeof useWalletBalanceHistory>
  close: () => void
}) {
  const [period, setPeriod] = useState(86_400)
  const now = Math.max(history.at || 0, Date.now() / 1000)
  const rows = period === 0 ? history.lifetimeRows : history.rows.map((row) => ({ at: row.at, sol: Number(row.lamports) / 1_000_000_000, usd: row.usd }))
  const range = period || Math.max(86400, now - (rows[0]?.at || now))
  const current = history.unit === 'SOL' ? history.lamports ? Number(history.lamports) / 1_000_000_000 : null : history.usd
  return <div className="modal-backdrop wallet-balance-history-layer" role="presentation" onMouseDown={close}>
    <section className="strategy-modal wallet-balance-history-dialog" role="dialog" aria-modal="true" aria-label="Wallet balance history" onMouseDown={(event) => event.stopPropagation()}>
      <button type="button" className="close" aria-label="Close wallet balance history" onClick={close}>×</button>
      <small>WALLET BALANCE HISTORY</small><h2>{wallet.name} balance</h2><p>{wallet.address.slice(0, 5)}…{wallet.address.slice(-5)} · {wallet.network}</p>
      <div className="wallet-history-controls">
        <div role="group" aria-label="History currency">{(['SOL', 'USD'] as const).map((unit) => <button key={unit} type="button" aria-pressed={history.unit === unit} onClick={() => history.chooseUnit(unit)}>{unit}</button>)}</div>
        <div role="group" aria-label="Wallet history range"><button type="button" aria-pressed={period === 86_400} onClick={() => setPeriod(86_400)}>24h</button><button type="button" aria-pressed={period === 7 * 86_400} onClick={() => setPeriod(7 * 86_400)}>7d</button><button type="button" aria-pressed={period === 0} onClick={() => setPeriod(0)}>All</button></div>
      </div>
      {history.error && <p role="status" className="error">{history.error}</p>}
      {history.chain.error && <p role="status">Earlier history unavailable. <button type="button" onClick={history.chain.refresh}>Retry history</button></p>}
      <HistoryChart key={`${history.unit}:${period}`} title="Wallet balance" field={history.unit === 'SOL' ? 'sol' : 'usd'} rows={rows} now={now} period={range} current={current} detail={`Native SOL balance${history.unit === 'USD' ? ' valued at the recorded SOL/USD rate' : ''} · includes deposits, withdrawals and fees`} emptyDetail={history.unit === 'SOL' ? 'The first confirmed balance reading starts this wallet’s history.' : 'A SOL/USD price is needed to record the USD equivalent.'} />
      <p className="wallet-balance-history-note">All shows balances from the earliest available confirmed transaction to today. USD history estimates those balances using historical hourly or daily SOL/USD opening prices from Coinbase; the latest point uses the current quote when available. Missing receipts or price intervals leave gaps. Wallet creation dates are not supplied by transaction history. Recent USD readings are also saved while the Dashboard is open. Token holdings are tracked separately in Overview.</p>
      {rows.length > 0 && <p className="wallet-balance-history-start">History starts {new Date(rows[0].at * 1000).toLocaleString()}.</p>}
    </section>
  </div>
}
