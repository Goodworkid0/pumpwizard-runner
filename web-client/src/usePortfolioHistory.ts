import { useEffect, useMemo, useState } from 'react'
import { serverStorage } from './serverStorage'
import type { WalletSession } from './browserWallet'
import type { PortfolioTradeState } from './PortfolioWorkspace'
import type { WalletPortfolioState } from './useWalletPortfolio'
import { appendPortfolioSample, openPositionPnl, portfolioSamples, positionPnlReason, walletValuation, worthMovement } from './portfolioMetrics'
import type { PortfolioSample } from './portfolioMetrics'

export function usePortfolioHistory(wallet: WalletSession | null, portfolio: WalletPortfolioState | null, trades: PortfolioTradeState | null, unavailable: boolean, tradesUnavailable: boolean) {
  const key = wallet ? `pumpwizard.portfolio-history/v1:${wallet.network}:${wallet.address}` : ''
  const initial = useMemo(() => {
    if (!key) return []
    return portfolioSamples(serverStorage.getItem(key) ?? serverStorage.getItem(key.replace('pumpwizard.portfolio-history/v1:', 'pumpwizard.wallet-worth-history/v1:')))
  }, [key])
  const [saved, setSaved] = useState<{ key: string; rows: PortfolioSample[]; error: string }>({ key: '', rows: [], error: '' })
  const valuation = walletValuation(unavailable ? null : portfolio)
  const worth = valuation.total
  const pnl = tradesUnavailable ? null : openPositionPnl(trades)
  const worthReason = unavailable ? 'Wallet holdings refresh failed' : valuation.reason
  const pnlReason = tradesUnavailable ? 'Trading records refresh failed' : positionPnlReason(trades)
  const at = portfolio?.checked_at

  useEffect(() => {
    if (!key || !at) return
    const rows = portfolioSamples(serverStorage.getItem(key))
    const existing = rows.length ? rows : initial
    const latest = existing[existing.length - 1]
    if (latest && (latest.at > at || (latest.at === at && latest.usd === worth && latest.pnl === pnl))) {
      setSaved({ key, rows: existing, error: '' })
      return
    }
    const next = appendPortfolioSample(existing, { at, usd: worth, pnl })
    let active = true
    void serverStorage.setItemAsync(key, JSON.stringify(next)).then(() => {
      if (active) setSaved({ key, rows: next, error: '' })
    }).catch(() => {
      if (active) setSaved({ key, rows: existing, error: 'Chart history could not be saved to the local server.' })
    })
    return () => { active = false }
  }, [at, initial, key, pnl, worth])

  const rows = saved.key === key ? saved.rows : initial
  return { rows, worth, valuation, worthReason, pnl, pnlReason, movement: worthMovement(rows, at || Date.now() / 1000, worth), movementReason: worth === null ? 'Needs a complete current wallet value' : 'No complete snapshot from 24 hours ago', error: saved.key === key ? saved.error : '' }
}
