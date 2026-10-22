import { useId, useState } from 'react'
import type { useWalletBalanceHistory } from './useWalletBalanceHistory'
import { solAmount } from './walletBalanceHistory'
import { dollars } from './portfolioMetrics'

export function WalletBalanceSparkline({ history }: { history: ReturnType<typeof useWalletBalanceHistory> }) {
  const gradient = useId().replace(/:/g, '')
  const [selected, setSelected] = useState<number | null>(null)
  const [period, setPeriod] = useState(0)
  const field = history.unit === 'SOL' ? 'sol' : 'usd'
  const amount = (value: number | null) => history.unit === 'SOL' ? solAmount(value) : value === null ? 'Unavailable' : dollars(value)
  const all = history.lifetimeRows
  const now = Math.max(history.at || 0, Date.now() / 1000)
  const start = period ? now - period : all[0]?.at || now
  const rows = all.filter((row) => row.at >= start)
  const values = rows.flatMap((row) => row[field] === null ? [] : [row[field]])
  const high = values.reduce((maximum, value) => Math.max(maximum, value), 0)
  const max = high > 0 ? high * 1.12 : 1
  const left = 5, right = 400, top = 6, bottom = 39
  const x = (at: number) => left + (at - start) / Math.max(1, now - start) * (right - left)
  const y = (value: number) => bottom - value / max * (bottom - top)
  const segments: { path: string; first: number; last: number }[] = []
  let previous: typeof rows[number] | null = null
  rows.forEach((row) => {
    if (row[field] === null) { previous = null; return }
    const connect = previous && (row.continuous ?? row.at - previous.at <= 1800)
    if (connect && segments.length) {
      segments[segments.length - 1].path += ` H${x(row.at).toFixed(2)}V${y(row[field]!).toFixed(2)}`
      segments[segments.length - 1].last = x(row.at)
    } else segments.push({ path: `M${x(row.at).toFixed(2)},${y(row[field]!).toFixed(2)}`, first: x(row.at), last: x(row.at) })
    previous = row
  })
  const inspected = selected === null ? null : rows[Math.min(selected, rows.length - 1)]
  const shortDate = (at: number) => new Date(at * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', ...(now - start > 365 * 86400 ? { year: 'numeric' } : {}) })
  const axisValue = (value: number) => `${history.unit === 'USD' ? '$' : ''}${value.toLocaleString(undefined, { maximumFractionDigits: history.unit === 'USD' ? 2 : value < .01 ? 6 : 3, notation: value >= 1000 ? 'compact' : 'standard' })}`
  const data = history.chain.data
  const syncing = history.chain.loading || data?.sync_pending
  const usdHistory = history.unit === 'USD' ? data?.usd_history : undefined
  const coverage = usdHistory?.error ? 'USD prices paused · retrying automatically' : history.chain.error ? 'Earlier history unavailable' : data?.sync_error ? 'History paused · retrying automatically' : usdHistory?.sync_pending && data?.history_complete ? 'Loading historical USD prices…' : syncing ? data ? `Syncing ${data.loaded_transactions ?? data.transactions - data.missing_transactions}/${data.transactions} transactions…` : 'Loading wallet history…' : usdHistory?.unpriced_readings ? 'Partial USD history · missing prices' : data && !data.history_complete ? 'Partial history · missing receipts' : data ? `${data.transactions} transactions · confirmed` : 'Waiting for a confirmed reading'
  const description = inspected ? `${new Date(inspected.at * 1000).toLocaleString()} · ${amount(inspected[field])}` : coverage
  return <div className="wallet-balance-sparkline" role="region" aria-label="Wallet balance mini chart">
    <div className="wallet-chart-heading"><span><i aria-hidden="true" />{history.unit} history</span><div role="group" aria-label="Inline wallet history range">{[{ label: '7d', value: 7 * 86400 }, { label: '1m', value: 30 * 86400 }, { label: 'All', value: 0 }].map((range) => <button key={range.label} type="button" aria-pressed={period === range.value} onClick={() => { setPeriod(range.value); setSelected(null) }}>{range.label}</button>)}</div></div>
    <div className="wallet-sparkline-plot" tabIndex={0} role="img" aria-label={`Native ${history.unit} balance from first available activity to today; ${rows.length} readings. Use left and right arrows to inspect.`}
      onFocus={() => setSelected(rows.length - 1)} onBlur={() => setSelected(null)} onMouseLeave={() => setSelected(null)}
      onKeyDown={(event) => { if (rows.length && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) { event.preventDefault(); setSelected((current) => Math.max(0, Math.min(rows.length - 1, (current ?? rows.length - 1) + (event.key === 'ArrowLeft' ? -1 : 1)))) } }}
      onMouseMove={(event) => { if (!rows.length) return; const box = event.currentTarget.getBoundingClientRect(); const at = start + Math.max(0, Math.min(1, ((event.clientX - box.left) / box.width * 460 - left) / (right - left))) * (now - start); setSelected(rows.reduce((best, row, index) => Math.abs(row.at - at) <= Math.abs(rows[best].at - at) ? index : best, 0)) }}>
      <svg viewBox="0 0 460 58" preserveAspectRatio="none" aria-hidden="true">
        <defs><linearGradient id={gradient} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#a98aff" stopOpacity=".24" /><stop offset="100%" stopColor="#a98aff" stopOpacity=".015" /></linearGradient></defs>
        {[0, max / 2, max].map((value) => <g key={value}><path className="wallet-sparkline-grid" d={`M${left} ${y(value)}H${right}`} /><text className="wallet-chart-axis" x="412" y={y(value) + 3}>{axisValue(value)}</text></g>)}
        {segments.map((segment, index) => <path key={index} fill={`url(#${gradient})`} d={`${segment.path} L${segment.last},${bottom}H${segment.first}Z`} />)}
        {values.length > 0 && <path className="wallet-sparkline-line" d={segments.map((segment) => segment.path).join(' ')} vectorEffect="non-scaling-stroke" />}
        {values.length === 1 && rows.map((row, index) => row[field] !== null && <circle key={index} cx={x(row.at)} cy={y(row[field]!)} r="3" />)}
        {inspected && inspected[field] !== null && <g><path className="wallet-chart-crosshair" d={`M${x(inspected.at)} ${top}V${bottom}`} /><circle className="wallet-sparkline-cursor" cx={x(inspected.at)} cy={y(inspected[field]!)} r="3.5" /></g>}
        <text className="wallet-chart-axis" x={left} y="55">{rows.length ? shortDate(start) : 'First activity'}</text>
        <text className="wallet-chart-axis" x={(left + right) / 2} y="55" textAnchor="middle">{rows.length && now - start > 86400 ? shortDate((start + now) / 2) : ''}</text>
        <text className="wallet-chart-axis" x={right} y="55" textAnchor="end">Today</text>
      </svg>
      {values.length === 0 && <span className="wallet-chart-empty">{syncing ? history.unit === 'USD' ? 'Loading historical USD prices…' : 'Fetching confirmed balances…' : 'History will appear after the next successful sync'}</span>}
      {inspected && <div className="wallet-chart-tooltip"><b>{amount(inspected[field])}</b><small>{new Date(inspected.at * 1000).toLocaleString()}</small></div>}
    </div>
    <div className="wallet-sparkline-caption" aria-live="polite"><span title={usdHistory?.error || (usdHistory ? `USD estimates use ${usdHistory.resolution} historical SOL/USD prices from ${usdHistory.source}.` : '') || history.chain.error || data?.sync_error || 'Starts at first available confirmed activity. Missing receipts remain gaps.'}>{description}</span>{history.chain.error && <button type="button" onClick={history.chain.refresh}>Retry</button>}</div>
  </div>
}
