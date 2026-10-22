import { useState } from 'react'
import type { WalletSession } from './browserWallet'
import { dollars } from './portfolioMetrics'
import type { useWalletBalanceHistory } from './useWalletBalanceHistory'
import { solBalance } from './walletBalanceHistory'
import { WalletBalanceHistory } from './WalletBalanceHistory'
import { WalletBalanceSparkline } from './WalletBalanceSparkline'

export function WalletSummary({ wallet, history, deposit, withdraw }: {
  wallet: WalletSession
  history: ReturnType<typeof useWalletBalanceHistory>
  deposit: () => void
  withdraw: () => void
}) {
  const [historyOpen, setHistoryOpen] = useState(false)
  const sol = solBalance(history.lamports)
  const usd = history.usd === null ? history.native.loading ? 'Fetching USD price…' : 'USD price offline' : `≈${dollars(history.usd)}`
  const mainSol = history.unit === 'SOL' || history.usd === null
  const otherUnit = history.unit === 'SOL' ? 'USD' : 'SOL'

  return <section className="wallet-compact-summary" aria-label="Wallet balance and valuation summary">
    <div className="wallet-balance-row">
      <div className="wallet-balance-display" title={history.usd === null ? history.native.error || 'Waiting for a public SOL/USD quote. Balance refreshes automatically.' : `Native SOL balance · ${history.native.data?.price_source || 'public'} USD estimate${history.native.data?.price_stale ? ' · last available quote' : ''}. Token holdings are shown in Wallet.`}>
        <small>WALLET BALANCE</small><span className="wallet-balance-amounts"><b>{mainSol ? sol : usd}</b><em>{mainSol ? usd : sol}{history.native.data?.price_stale && history.usd !== null ? ' · recent estimate' : ''}</em></span>
      </div>
      <div className="wallet-balance-tools">
        <button type="button" className="wallet-unit-toggle" aria-label={`Show wallet balance in ${otherUnit}`} title={`Make ${otherUnit} the main amount`} onClick={() => history.chooseUnit(otherUnit)}><span aria-hidden="true">⇄</span> {otherUnit}</button>
        <button type="button" className="wallet-history-button" aria-label="Open wallet balance history" onClick={() => setHistoryOpen(true)}><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3 3v14h14M5.5 12l3-4 3 2 4-5" /></svg>History</button>
      </div>
    </div>
    <WalletBalanceSparkline history={history} />
    {historyOpen && <WalletBalanceHistory wallet={wallet} history={history} close={() => setHistoryOpen(false)} />}
    <div className="wallet-funding-actions" role="group" aria-label="Wallet actions">
      <button type="button" onClick={deposit}>Deposit</button><button type="button" onClick={withdraw}>Withdraw</button>
    </div>
  </section>
}
