import { useCallback, useEffect, useRef, useState } from 'react'
import { api, errorText, inspectWallet } from './browserWallet'
import type { WalletInspection, WalletSession } from './browserWallet'
import type { PortfolioTradeState } from './PortfolioWorkspace'

export function useTradeRecords(session: WalletSession | null, onBalance: (balance: string) => void) {
  const [records, setRecords] = useState<PortfolioTradeState | null>(null)
  const [funds, setFunds] = useState<WalletInspection | null>(null)
  const [error, setError] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const refreshRef = useRef<() => Promise<void>>(async () => {})
  const address = session?.address
  const network = session?.network
  const assertActive = session?.assertActive

  useEffect(() => {
    let cancelled = false
    let inFlight: Promise<void> | null = null
    let requestedGeneration = 0
    let completedGeneration = 0
    setRecords(null)
    setFunds(null)
    setError('')
    if (!address || !network || !assertActive) return
    const load = async () => {
      if (cancelled) return
      setRefreshing(true)
      const errors: string[] = []
      try {
        assertActive()
        try { await api('/api/v1/trades/reconcile', { sender: address, network }) }
        catch (failure) { errors.push(`Receipt check unavailable: ${errorText(failure)}`) }
        const query = new URLSearchParams({ sender: address, network })
        const state = await api<PortfolioTradeState>(`/api/v1/trades?${query}`)
        if (!cancelled) setRecords(state)
        try {
          const wallet = await inspectWallet(address, network)
          if (!cancelled) { onBalance(wallet.balance); setFunds(wallet) }
        } catch (failure) { errors.push(`Balance refresh unavailable: ${errorText(failure)}`) }
      } catch (failure) { errors.push(errorText(failure)) }
      finally {
        if (!cancelled) { setError(errors.join(' ')); setRefreshing(false) }
      }
    }
    const runRequested = async () => {
      while (!cancelled && completedGeneration < requestedGeneration) {
        const generation = requestedGeneration
        await load()
        completedGeneration = generation
      }
    }
    const refresh = (): Promise<void> => {
      if (cancelled) return Promise.resolve()
      requestedGeneration += 1
      if (!inFlight) inFlight = runRequested().finally(() => { inFlight = null })
      return inFlight
    }
    refreshRef.current = refresh
    void refresh()
    const timer = window.setInterval(() => {
      if (!inFlight && document.visibilityState === 'visible') void refresh()
    }, 8000)
    const visible = () => { if (document.visibilityState === 'visible' && !inFlight) void refresh() }
    document.addEventListener('visibilitychange', visible)
    return () => { cancelled = true; clearInterval(timer); document.removeEventListener('visibilitychange', visible) }
  }, [address, network, assertActive, onBalance])

  const refresh = useCallback(() => refreshRef.current(), [])
  return { funds: funds?.sender === address && funds?.network === network ? funds : null, trades: records?.sender === address && records?.network === network ? records : null, error, refreshing, refresh }
}
