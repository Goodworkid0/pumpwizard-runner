import { useEffect, useState } from 'react'
import { api, errorText } from './browserWallet'

export type WalletInventory = {
  sender: string; network: string; balance: string; sol_usd_price: number | null; checked_at: number
  assets: { mint: string; amount: string; decimals: number; symbol: string | null; name: string | null; program: string }[]
  coverage_gaps: string[]
}
export type InventoryResult = { data?: WalletInventory; error?: string }

export function useWalletInventories(addresses: string[], network: string) {
  const key = Array.from(new Set(addresses)).join(',')
  const [revision, setRevision] = useState(0)
  const [results, setResults] = useState<Record<string, InventoryResult>>({})
  useEffect(() => {
    let active = true
    setResults({})
    const queue = key.split(',').filter(Boolean)
    const worker = async () => {
      while (active && queue.length) {
        const address = queue.shift()!
        try {
          const query = new URLSearchParams({ sender: address, network, ...(revision ? { refresh: 'true' } : {}) })
          const data = await api<WalletInventory>(`/api/v1/wallet/inventory?${query}`)
          if (data.sender !== address || data.network !== network) throw new Error('Wallet balance identity changed. Refresh balances.')
          if (active) setResults((current) => ({ ...current, [address]: { data } }))
        } catch (reason) {
          if (active) setResults((current) => ({ ...current, [address]: { error: errorText(reason) } }))
        }
      }
    }
    void Promise.all(Array.from({ length: Math.min(3, queue.length) }, worker))
    return () => { active = false }
  }, [key, network, revision])
  return { results, refresh: () => setRevision((value) => value + 1) }
}

