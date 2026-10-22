import { useEffect, useState } from 'react'
import { api, errorText } from './browserWallet'
import type { WalletSession } from './browserWallet'
import type { BalanceUnit } from './walletBalanceHistory'

export type ChainBalanceHistory = {
  sender: string; network: string; checked_at: number; history_complete: boolean
  transactions: number; missing_transactions: number; limited: boolean
  loaded_transactions?: number; sync_pending?: boolean; retry_after?: number; sync_error?: string
  usd_history?: { source: string; interval_seconds: number; resolution: string; unpriced_readings: number; sync_pending: boolean; error: string }
  rows: { at: number; lamports: string | null; usd: number | null; continuous: boolean }[]
}

export function useWalletChainHistory(wallet: WalletSession | null, currency: BalanceUnit) {
  const sender = wallet?.address
  const network = wallet?.network
  const scope = `${network}:${sender}`
  const [state, setState] = useState<{ scope: string; data: ChainBalanceHistory | null; loading: boolean; error: string }>({ scope: '', data: null, loading: true, error: '' })
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    if (!sender || !network) return
    let active = true
    let timer: number | undefined
    const load = async () => {
      setState((previous) => ({ scope, data: previous.scope === scope ? previous.data : null, loading: true, error: '' }))
      try {
        const query = new URLSearchParams({ sender, network, currency })
        if (revision) query.set('refresh', 'true')
        const data = await api<ChainBalanceHistory>(`/api/v1/wallet/balance-history?${query}`)
        if (data.sender !== sender || data.network !== network) throw new Error('Wallet history belongs to another account.')
        if (active) {
          setState({ scope, data, loading: false, error: '' })
          timer = window.setTimeout(() => { void load() }, data.sync_pending ? Math.max(3, data.retry_after || 3) * 1000 : 300_000)
        }
      } catch (error) {
        if (active) {
          setState((previous) => ({ scope, data: previous.scope === scope ? previous.data : null, loading: false, error: errorText(error) }))
          timer = window.setTimeout(() => { void load() }, 30_000)
        }
      }
    }
    void load()
    return () => { active = false; window.clearTimeout(timer) }
  }, [currency, network, revision, scope, sender])
  return { data: state.scope === scope ? state.data : null, loading: state.scope === scope ? state.loading : Boolean(wallet), error: state.scope === scope ? state.error : '', refresh: () => setRevision((value) => value + 1) }
}
