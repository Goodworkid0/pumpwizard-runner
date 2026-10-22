import { finiteAmount, HISTORY_INTERVAL, HISTORY_RETENTION } from './portfolioMetrics'

export type WalletBalanceSample = { at: number; lamports: string; usd: number | null }
export type BalanceUnit = 'SOL' | 'USD'

export function validLamports(value: unknown): value is string {
  return typeof value === 'string' && /^(0|[1-9]\d{0,19})$/.test(value) && BigInt(value) <= 18_446_744_073_709_551_615n
}

export function balanceSamples(value: string | null): WalletBalanceSample[] {
  try {
    const rows: unknown = JSON.parse(value || '[]')
    if (!Array.isArray(rows) || rows.length > 700) return []
    let previous = 0
    const result: WalletBalanceSample[] = []
    for (const row of rows) {
      if (!row || typeof row !== 'object' || Object.keys(row).sort().join(',') !== 'at,lamports,usd') return []
      const { at, lamports, usd } = row
      if (typeof at !== 'number' || !Number.isFinite(at) || at <= previous || !validLamports(lamports) || usd !== null && (typeof usd !== 'number' || !Number.isFinite(usd) || usd < 0)) return []
      result.push({ at, lamports, usd })
      previous = at
    }
    return result
  } catch { return [] }
}

export function appendBalanceSample(rows: WalletBalanceSample[], sample: WalletBalanceSample) {
  const bucket = Math.floor(sample.at / HISTORY_INTERVAL)
  return [...rows.filter((row) => row.at >= sample.at - HISTORY_RETENTION && row.at <= sample.at && Math.floor(row.at / HISTORY_INTERVAL) !== bucket), sample].slice(-700)
}

export function solAmount(value: number | null) {
  return value === null ? 'Unavailable' : `${value.toLocaleString(undefined, { maximumFractionDigits: 9 })} SOL`
}

export function solBalance(lamports: string) {
  if (!validLamports(lamports)) return 'Unavailable'
  const amount = BigInt(lamports)
  const whole = amount / 1_000_000_000n
  const fraction = (amount % 1_000_000_000n).toString().padStart(9, '0').replace(/0+$/, '')
  return `${whole.toLocaleString()}${fraction ? `.${fraction}` : ''} SOL`
}

export function nativeUsd(value: unknown) {
  const amount = finiteAmount(value)
  return amount !== null && amount >= 0 ? amount : null
}
