import type { PortfolioTradeState } from './PortfolioWorkspace'
import type { WalletPortfolioState } from './useWalletPortfolio'

export function finiteAmount(value: unknown): number | null {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export function walletWorth(portfolio: WalletPortfolioState | null) {
  return walletValuation(portfolio).total
}

export function walletValuation(portfolio: WalletPortfolioState | null) {
  if (!portfolio) return { total: null, known: null, partial: false, reason: 'Wallet holdings not loaded' }
  const native = finiteAmount(portfolio?.estimated_usd)
  let known = native
  let stale = 0
  let unpriced = 0
  for (const asset of portfolio.assets) {
    const value = asset.valuation.current_value
    const amount = finiteAmount(value.amount)
    if (asset.valuation.mark.freshness === 'fresh' && value.state === 'estimated' && value.currency === 'USD' && amount !== null) known = (known ?? 0) + amount
    else if (asset.valuation.mark.freshness === 'stale') stale += 1
    else unpriced += 1
  }
  const reasons = [
    native === null ? 'SOL/USD price unavailable' : '',
    stale ? `${stale} token price${stale === 1 ? ' is' : 's are'} stale` : '',
    unpriced ? `${unpriced} token${unpriced === 1 ? '' : 's'} without a usable price` : '',
    portfolio.coverage_gaps.length ? `${portfolio.coverage_gaps.length} wallet account coverage gap${portfolio.coverage_gaps.length === 1 ? '' : 's'}` : '',
  ].filter(Boolean)
  const partial = reasons.length > 0
  return { total: partial ? null : known, known, partial, reason: reasons.join(' · ') || 'Complete retained estimate · before exit costs' }
}

export function positionPnlReason(trades: PortfolioTradeState | null) {
  if (!trades) return 'Trading records not loaded'
  const open = trades.positions.filter((position) => position.status === 'open')
  if (!open.length) return 'No open Runner-recorded positions'
  const reasons = new Set<string>()
  for (const position of open) {
    const value = position.valuation
    if (!value) { reasons.add('Position valuation unavailable'); continue }
    if (value.basis.source === 'missing_entry_fx') reasons.add('Purchase-time SOL/USD rate missing')
    else if (value.basis.state !== 'known') reasons.add('Purchase cost unavailable')
    if (value.mark.freshness === 'stale') reasons.add('Open-position price is stale')
    else if (value.mark.freshness !== 'fresh' || value.mark.state !== 'available') reasons.add('Fresh position price unavailable')
  }
  return [...reasons].join(' · ') || (openPositionPnl(trades) === null ? 'USD result unavailable' : 'Estimate before exit costs')
}

// This is the result of currently open recorded positions, before exit costs.
// Closed-position results and external wallet flows are separate accounting.
export function openPositionPnl(trades: PortfolioTradeState | null) {
  if (!trades) return null
  let total = 0
  for (const position of trades.positions.filter((row) => row.status === 'open')) {
    const result = position.valuation?.unrealized_result
    const amount = finiteAmount(result?.amount)
    if (!result || result.state !== 'estimated_before_exit_costs' || result.currency !== 'USD' || position.valuation?.mark.freshness !== 'fresh' || amount === null) return null
    total += amount
  }
  return total
}

export function dollars(value: number | null, signed = false) {
  if (value === null) return 'Unavailable'
  const prefix = signed && value > 0 ? '+' : value < 0 ? '−' : ''
  return `${prefix}$${Math.abs(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export type PortfolioSample = { at: number; usd: number | null; pnl: number | null }
export const HISTORY_INTERVAL = 900
export const HISTORY_RETENTION = 7 * 86_400

export function portfolioSamples(value: string | null): PortfolioSample[] {
  try {
    const rows: unknown = JSON.parse(value || '[]')
    if (!Array.isArray(rows)) return []
    return rows.flatMap((row): PortfolioSample[] => {
      const at = finiteAmount(row?.at)
      const usd = finiteAmount(row?.usd)
      const pnl = finiteAmount(row?.pnl)
      return at !== null && at > 0 && (usd === null || usd >= 0) ? [{ at, usd, pnl }] : []
    }).sort((a, b) => a.at - b.at)
  } catch { return [] }
}

export function appendPortfolioSample(rows: PortfolioSample[], sample: PortfolioSample) {
  // Fixed time buckets allow frequent refreshes without continually moving the
  // previous sample forward and losing the history.
  const bucket = Math.floor(sample.at / HISTORY_INTERVAL)
  return [...rows.filter((row) => row.at >= sample.at - HISTORY_RETENTION && row.at <= sample.at && Math.floor(row.at / HISTORY_INTERVAL) !== bucket), sample].slice(-700)
}

export function worthMovement(rows: PortfolioSample[], at: number, usd: number | null) {
  if (usd === null) return null
  const previous = rows.filter((row) => row.usd !== null && row.at <= at - 86_400 && row.at >= at - 86_400 - 6 * 3600).slice(-1)[0]
  if (!previous || previous.usd === null) return null
  return { usd: usd - previous.usd, percent: previous.usd > 0 ? (usd / previous.usd - 1) * 100 : null }
}
