import { useEffect, useState } from 'react'
import { api, errorText } from './browserWallet'
import type { WalletSession } from './browserWallet'

export type NativeBalance = {
  sender: string; network: string; balance: string; estimated_usd: number | null; checked_at: number
  price_at: number | null; price_source: string | null; price_stale: boolean
}

export function useWalletNativeBalance(wallet: WalletSession | null) {
  const sender = wallet?.address, network = wallet?.network
  const scope = `${network}:${sender}`
  const [state, setState] = useState<{ scope: string; data: NativeBalance | null; error: string; loading: boolean }>({ scope: '', data: null, error: '', loading: true })
  useEffect(() => {
    if (!sender || !network) return
    let active = true, busy = false
    const load = async () => {
      if (busy) return
      busy = true
      try {
        const query = new URLSearchParams({ sender, network })
        const data = await api<NativeBalance>(`/api/v1/wallet/balance?${query}`)
        if (data.sender !== sender || data.network !== network) throw new Error('Balance belongs to another wallet.')
        if (active) setState({ scope, data, error: '', loading: false })
      } catch (error) {
        if (active) setState((previous) => ({ scope, data: previous.scope === scope ? previous.data : null, error: errorText(error), loading: false }))
      } finally { busy = false }
    }
    void load()
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load() }, 30_000)
    return () => { active = false; window.clearInterval(timer) }
  }, [network, scope, sender])
  return state.scope === scope ? state : { scope, data: null, error: '', loading: Boolean(wallet) }
}
