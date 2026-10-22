import { useEffect, useMemo, useState } from 'react'
import type { WalletSession } from './browserWallet'
import type { WalletPortfolioState } from './useWalletPortfolio'
import { serverStorage } from './serverStorage'
import { appendBalanceSample, balanceSamples, nativeUsd, validLamports } from './walletBalanceHistory'
import type { BalanceUnit, WalletBalanceSample } from './walletBalanceHistory'
import { useWalletChainHistory } from './useWalletChainHistory'
import { useWalletNativeBalance } from './useWalletNativeBalance'

export function useWalletBalanceHistory(wallet: WalletSession | null, portfolio: WalletPortfolioState | null, unavailable: boolean) {
  const native = useWalletNativeBalance(wallet)
  const identity = wallet ? `${wallet.network}:${wallet.address}` : ''
  const key = identity ? `pumpwizard.wallet-balance-history/v1:${identity}` : ''
  const unitKey = identity ? `pumpwizard.wallet-balance-unit/v1:${identity}` : ''
  const initial = useMemo(() => balanceSamples(key ? serverStorage.getItem(key) : null), [key])
  const [saved, setSaved] = useState<{ key: string; rows: WalletBalanceSample[]; error: string }>({ key: '', rows: [], error: '' })
  const [display, setDisplay] = useState<{ key: string; unit: BalanceUnit }>({ key: '', unit: 'SOL' })
  const unit: BalanceUnit = display.key === unitKey ? display.unit : serverStorage.getItem(unitKey) === 'USD' ? 'USD' : 'SOL'
  const chain = useWalletChainHistory(wallet, unit)
  const portfolioReading = !unavailable && portfolio?.sender === wallet?.address && portfolio?.network === wallet?.network && portfolio && validLamports(portfolio.balance) ? portfolio : null
  const observed = native.data && validLamports(native.data.balance) ? native.data : portfolioReading
  const at = observed?.checked_at
  const lamports = observed?.balance
  const usd = observed && Date.now() / 1000 - observed.checked_at <= 300 ? nativeUsd(observed.estimated_usd) : null

  useEffect(() => {
    if (!key || !at || !Number.isFinite(at) || at <= 0 || !lamports) return
    const rows = balanceSamples(serverStorage.getItem(key))
    const existing = rows.length ? rows : initial
    const latest = existing[existing.length - 1]
    if (latest && (latest.at > at || latest.at === at && latest.lamports === lamports && latest.usd === usd)) {
      setSaved({ key, rows: existing, error: '' })
      return
    }
    const next = appendBalanceSample(existing, { at, lamports, usd })
    let active = true
    void serverStorage.setItemAsync(key, JSON.stringify(next)).then(() => {
      if (active) setSaved({ key, rows: next, error: '' })
    }).catch(() => {
      if (active) setSaved({ key, rows: existing, error: 'Wallet balance history could not be saved.' })
    })
    return () => { active = false }
  }, [at, initial, key, lamports, usd])

  const chooseUnit = (next: BalanceUnit) => {
    setDisplay({ key: unitKey, unit: next })
    if (unitKey) void serverStorage.setItemAsync(unitKey, next).catch(() => {
      setSaved((current) => current.key === key ? { ...current, error: 'The display unit could not be saved.' } : current)
    })
  }
  const rows = saved.key === key ? saved.rows : initial
  const lifetimeRows = [
    ...(chain.data?.rows || []).map((row) => ({ at: row.at, sol: row.lamports === null ? null : Number(row.lamports) / 1e9, usd: row.usd as number | null, continuous: row.continuous })),
    ...rows.filter((row) => row.at > (chain.data?.checked_at || 0)).map((row) => ({ at: row.at, sol: Number(row.lamports) / 1e9, usd: row.usd, continuous: undefined })),
  ]
  return { rows, lifetimeRows, chain, native, lamports: lamports ?? wallet?.balance ?? '', usd, at: at ?? null, unit, chooseUnit, error: saved.key === key ? saved.error : '' }
}
