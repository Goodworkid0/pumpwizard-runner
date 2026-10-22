import { useCallback, useEffect, useRef, useState } from 'react'
import { api, errorText } from './browserWallet'
import type { WalletSession } from './browserWallet'

export type HoldingValuation = {
  schema: 'pumpwizard.holding-valuation/v1'
  quantity: { state: 'known' | 'unknown'; amount: string | null; unit: string; atoms: string | null; decimals: number | null }
  basis: {
    state: 'known' | 'unknown'
    amount: string | null
    currency: string
    source: string
    native_state?: 'known' | 'unknown'
    native_amount?: string | null
    native_currency?: string | null
  }
  mark: {
    state: 'available' | 'unavailable'
    price: string | null
    currency: string
    per_unit: string
    kind: 'retained_estimate'
    source: string | null
    observed_at: number | null
    source_slot: number | null
    age_seconds: number | null
    slot_lag: number | null
    freshness: 'fresh' | 'stale' | 'future' | 'missing_time' | 'invalid_time'
    executable: false
  }
  current_value: { state: 'estimated' | 'unknown'; amount: string | null; currency: string; calculation: string }
  unrealized_result: { state: 'estimated_before_exit_costs' | 'unknown'; amount: string | null; percent: string | null; currency: string }
  realized_proceeds: { state: 'known' | 'unknown'; amount: string | null; currency: string; profit_treatment: string }
  executable_liquidation: { state: 'unavailable'; amount: null; currency: string; reason: string }
  ownership: { purchase_attribution: string; purchase_owner_id: string | null; current_management: 'session' | 'user' | 'unknown'; manager_id: string | null }
  fees: { state: string; amount: string | null; currency: string | null; exit_costs_included: false }
  external_flows: { strategy_profit_treatment: 'excluded' }
  blockers: string[]
  authority: { quote: false; signer: false; orders: false; submission: false }
}

export type WalletAsset = {
  mint: string
  amount: string
  decimals: number
  program: 'SPL Token' | 'Token-2022'
  token_program: string
  accounts: number
  runner_atoms: string
  untracked_atoms: string
  runner_position_ids: string[]
  symbol: string | null
  name: string | null
  website: string | null
  image_url: string | null
  creator: string | null
  metadata_source: string | null
  created_at: number | null
  price_usd: string | null
  observed_at: number | null
  price_source: string | null
  price_slot_lag: number | null
  liquidity_usd: number | null
  market_cap_usd: number | null
  price_change_24h: number | null
  volume_change_24h: number | null
  holder_count: number | null
  organic_score: number | null
  organic_score_label: string | null
  suspicious: boolean
  top_holders_percent: number | null
  trend_points: { at: number; price_usd: number }[]
  estimated_value_usd: string | null
  valuation_state: 'priced' | 'unpriced' | 'stale' | 'missing_time' | 'illiquid' | 'likely-worthless' | 'flagged'
  valuation_detail: string
  valuation: HoldingValuation
}

export type WalletReconciliation = {
  mint: string
  runner_atoms: string
  wallet_atoms: string
  difference_atoms: string
  position_ids: string[]
  status: 'matched' | 'externally closed' | 'wallet shortfall' | 'additional wallet tokens'
  sellable: boolean
}

export type WalletActivity = {
  signature: string
  slot: number | null
  block_time: number | null
  confirmation_status: string | null
  error: object | string | null
  source: 'Runner' | 'External wallet activity'
  runner_record: null | { kind: string; id: string; status: string }
  wallet_signer: boolean | null
  sol_delta: string | null
  fee: string | null
  token_deltas: { mint: string; decimals: number; amount: string }[]
  kind: string
  detail_available: boolean
}

export type WalletPortfolioState = {
  sender: string
  network: string
  balance: string
  sol_usd_price: number | null
  estimated_usd: number | null
  slot: number
  checked_at: number
  assets: WalletAsset[]
  reconciliations: WalletReconciliation[]
  activity: WalletActivity[]
  coverage_gaps: string[]
  history_limit: number
  history_complete: boolean
  coverage: string
}

export function useWalletPortfolio(session: WalletSession | null, enabled: boolean, refreshMs = 0) {
  const [portfolio, setPortfolio] = useState<WalletPortfolioState | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const generation = useRef(0)
  const address = session?.address
  const network = session?.network
  const assertActive = session?.assertActive

  const load = useCallback(async (fresh = false) => {
    if (!enabled || !address || !network || !assertActive) return
    const current = ++generation.current
    setLoading(true)
    setError('')
    try {
      assertActive()
      const query = new URLSearchParams({ sender: address, network, limit: '1' })
      if (fresh) query.set('refresh', 'true')
      const result = await api<WalletPortfolioState>(`/api/v1/wallet/portfolio?${query}`)
      if (generation.current === current) setPortfolio(result)
    } catch (failure) {
      if (generation.current === current) setError(errorText(failure))
    } finally {
      if (generation.current === current) setLoading(false)
    }
  }, [address, assertActive, enabled, network])

  useEffect(() => {
    if (!enabled) return
    void load(false)
    return () => { generation.current += 1 }
  }, [enabled, load])

  useEffect(() => {
    if (!enabled || refreshMs <= 0) return
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load(false)
    }, refreshMs)
    return () => window.clearInterval(timer)
  }, [enabled, load, refreshMs])

  useEffect(() => {
    setPortfolio(null)
    setError('')
  }, [address, network])

  const refresh = useCallback(() => load(true), [load])

  return {
    portfolio: portfolio?.sender === address && portfolio?.network === network ? portfolio : null,
    error,
    loading,
    refresh,
  }
}
