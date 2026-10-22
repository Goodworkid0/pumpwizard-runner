import type { CoinDetailData } from './TokenDetail'
import type { WalletAsset } from './useWalletPortfolio'

type PricePoint = { at: number; price_usd: number }

export function holdingPriceSeries(asset: Pick<WalletAsset, 'trend_points'>, detail: CoinDetailData | null, poolAddress = '') {
  const pools = detail?.pool_histories || []
  const pool = pools.find((row) => row.address === poolAddress) || pools[0]
  const candles = pool?.candles || detail?.candles || []
  const local = detail?.history.map((row) => ({ at: row.observed_at, price_usd: Number(row.price_usd) })) || []
  const valid = (row: PricePoint) => Number.isFinite(row.at) && row.at > 0 && row.at <= Date.now() / 1000
    && Number.isFinite(row.price_usd) && row.price_usd > 0
  const snapshots = local.filter(valid).sort((a, b) => a.at - b.at)
  const oldestLocal = snapshots[0]?.at
  const imported = candles.map((row) => ({ at: row.opened_at, price_usd: Number(row.close_usd) }))
    .filter((row) => valid(row) && (oldestLocal === undefined || row.at < oldestLocal))
  const recorded = [...imported, ...snapshots]
  const rows = recorded.length ? recorded : asset.trend_points
  const byTime = new Map(rows.filter(valid).map((row) => [row.at, row]))
  return [...byTime.values()].sort((a, b) => a.at - b.at)
}

export function holdingGraphPoints(series: PricePoint[]) {
  if (!series.length) return []
  const values = series.map((row) => row.price_usd)
  const low = Math.min(...values)
  const high = Math.max(...values)
  const range = high - low || high * .05
  const first = series[0].at
  const last = series[series.length - 1].at
  return series.map((row) => ({ ...row,
    x: series.length === 1 ? 100 : 5 + (row.at - first) / (last - first || 1) * 190,
    y: high === low ? 35 : 8 + (high - row.price_usd) / range * 52,
  }))
}

