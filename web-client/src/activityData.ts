import type { PortfolioSession, PortfolioTradeState, SessionEntryConsideration } from './PortfolioWorkspace'

export type ActivityOrder = PortfolioTradeState['orders'][number]
export type ActivityRange = '24h' | '7d' | 'all'
export type DecisionRow = SessionEntryConsideration & { id: string; strategyId: string; strategyLabel: string }

export function numberValue(value: unknown): number | null {
  if ((typeof value !== 'number' && typeof value !== 'string') || (typeof value === 'string' && !value.trim())) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

export function validTime(value: number) {
  return Number.isFinite(value) && value > 0 && Number.isFinite(new Date(value * 1000).getTime())
}

export function activityTime(at: number, compact = false) {
  if (!validTime(at)) return 'Time unavailable'
  return new Date(at * 1000).toLocaleString(undefined, compact
    ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
    : undefined)
}

export function inActivityRange(at: number, range: ActivityRange, now: number) {
  return range === 'all' || (validTime(at) && at <= now && at >= now - (range === '24h' ? 86400 : 604800))
}

export function awaitingReceipt(status: string) {
  return !['finalized', 'failed', 'cancelled', 'expired'].includes(status)
}

export function exactSol(atoms: string | bigint | null | undefined) {
  try {
    if (atoms == null || !/^-?\d+$/.test(String(atoms))) return 'Unavailable'
    const value = BigInt(atoms), absolute = value < 0n ? -value : value
    const fraction = (absolute % 1_000_000_000n).toString().padStart(9, '0').replace(/0+$/, '')
    return `${value < 0n ? '−' : ''}${(absolute / 1_000_000_000n).toLocaleString()}${fraction ? `.${fraction}` : ''} SOL`
  } catch { return 'Unavailable' }
}

export function recordedFees(orders: ActivityOrder[]) {
  let atoms = 0n, known = 0
  for (const order of orders) {
    const fee = order.receipt?.fee_lamports
    if (fee != null && /^\d+$/.test(fee)) { atoms += BigInt(fee); known++ }
  }
  return { atoms, known, total: orders.length }
}

export function latestDecisions(sessions: PortfolioSession[]): DecisionRow[] {
  return sessions.flatMap((session) => {
    const held = new Set(session.positions.filter((position) => position.status === 'open').map((position) => position.mint))
    return session.entry_considerations.filter((decision) => !held.has(decision.mint)).map((decision) => ({
      ...decision, id: `${session.instance.id}:${decision.mint}`, strategyId: session.instance.id, strategyLabel: session.instance.label,
    }))
  })
}

export function decisionReason(row: Pick<DecisionRow, 'reason' | 'kind'>) {
  if (row.kind === 'entry_recommended') return 'Enter signal'
  const reason = row.reason.replace(/_/g, ' ').toLowerCase()
  if (/warm|continuous observations|insufficient.*samples/.test(reason)) return 'Warming up'
  if (/market.?cap|entry.*band/.test(reason)) return 'Market cap'
  if (/liquidity/.test(reason)) return 'Liquidity'
  if (/stale|missing|unavailable|invalid|fresh/.test(reason)) return 'Data quality'
  return 'Other rules'
}

export function decisionLabel(row: DecisionRow) {
  return row.observation?.symbol || row.payload.symbol || `${row.mint.slice(0, 6)}…${row.mint.slice(-4)}`
}

export function decisionAmount(row: DecisionRow, field: 'price_usd' | 'market_cap_usd' | 'liquidity_usd') {
  return numberValue(row.observation?.[field] ?? (field === 'liquidity_usd' ? undefined : row.payload[field]))
}

export function activityUsd(value: unknown) {
  const amount = numberValue(value)
  return amount === null ? 'Unavailable' : `$${amount.toLocaleString(undefined, Math.abs(amount) < 1 ? { maximumSignificantDigits: 6 } : { maximumFractionDigits: 2 })}`
}

export function orderTimeline(orders: ActivityOrder[], range: ActivityRange, now: number) {
  const dated = orders.filter((order) => validTime(order.created_at))
  const end = range === 'all' ? Math.max(now, ...dated.map((order) => order.created_at)) : now
  const start = range === 'all' ? Math.min(end - 3600, ...dated.map((order) => order.created_at)) : end - (range === '24h' ? 86400 : 604800)
  const count = range === '24h' ? 24 : range === '7d' ? 7 : 12
  const width = (end - start) / count
  const buckets = Array.from({ length: count }, (_, i) => ({ at: start + i * width, end: start + (i + 1) * width, buy: 0, sell: 0 }))
  for (const order of dated) {
    if (order.created_at < start || order.created_at > end) continue
    buckets[Math.min(count - 1, Math.floor((order.created_at - start) / width))][order.side]++
  }
  return { buckets, start, end, undated: orders.length - dated.length }
}

export function csvText(headers: string[], rows: unknown[][]) {
  const cell = (value: unknown) => {
    const raw = String(value ?? '')
    const safe = /^[\s]*[=+\-@]|^[\t\r]/.test(raw) ? `'${raw}` : raw
    return `"${safe.replace(/"/g, '""')}"`
  }
  return [headers, ...rows].map((row) => row.map(cell).join(',')).join('\r\n') + '\r\n'
}

export function downloadCsv(name: string, text: string) {
  const url = URL.createObjectURL(new Blob(['\ufeff', text], { type: 'text/csv;charset=utf-8;' }))
  const link = document.createElement('a')
  link.href = url; link.download = name; link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
