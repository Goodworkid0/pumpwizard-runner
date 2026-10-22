import { useEffect, useRef, useState } from 'react'
import type { HistoricalCandle } from './TokenDetail'

function price(value: number) {
  return `$${value.toLocaleString(undefined, { maximumSignificantDigits: 5 })}`
}

function date(value: number) {
  return new Date(value * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export function CandleChart({ candles }: { candles: HistoricalCandle[] }) {
  const [days, setDays] = useState(7)
  const [chartWidth, setChartWidth] = useState(760)
  const chartRef = useRef<SVGSVGElement>(null)
  const hasCandles = candles.length > 0
  useEffect(() => {
    if (!chartRef.current) return
    const observer = new ResizeObserver(([entry]) => setChartWidth(Math.max(260, Math.round(entry.contentRect.width))))
    observer.observe(chartRef.current)
    return () => observer.disconnect()
  }, [hasCandles])
  const all = candles.slice().sort((a, b) => a.opened_at - b.opened_at)
  const latest = all[all.length - 1]?.opened_at || 0
  const rows = all.filter((row) => !days || row.opened_at >= latest - days * 86400)
  if (!rows.length) return <div className="token-chart-empty">No candles are retained for this pool.</div>
  const first = rows[0], last = rows[rows.length - 1]
  const start = first.opened_at, end = last.opened_at + last.resolution_seconds
  const low = Math.min(...rows.map((row) => Number(row.low_usd)))
  const high = Math.max(...rows.map((row) => Number(row.high_usd)))
  const padding = Math.max((high - low) * .12, high * .015, Number.EPSILON)
  const floor = Math.max(0, low - padding), ceiling = high + padding
  const volume = Math.max(1, ...rows.map((row) => Number(row.volume_usd)))
  const plotRight = chartWidth - 88
  const plotWidth = plotRight - 14
  const x = (at: number) => 14 + (at - start) / (end - start) * plotWidth
  const y = (value: number) => 16 + (ceiling - value) / (ceiling - floor) * 174
  const gaps = rows.slice(1).flatMap((row, index) => {
    const previousEnd = rows[index].opened_at + rows[index].resolution_seconds
    return row.opened_at > previousEnd ? [{ start: previousEnd, end: row.opened_at }] : []
  })
  const resolution = first.resolution_seconds % 3600 === 0 ? `${first.resolution_seconds / 3600}h` : `${first.resolution_seconds / 60}m`

  return <>
    <div className="candle-toolbar">
      <span className="chart-interval">{resolution} candles <span>· USD</span></span>
      <div className="chart-tabs" role="group" aria-label="Candle time range">{[1, 7, 30, 0].map((value) => <button key={value} aria-pressed={value === days} onClick={() => setDays(value)}>{value ? `${value}D` : 'All loaded'}</button>)}</div>
    </div>
    <div className="chart-summary candle-summary">
      <span><small>LAST CLOSE</small><b>{price(Number(last.close_usd))}</b></span>
      <span><small>PERIOD LOW</small><b>{price(low)}</b></span>
      <span><small>PERIOD HIGH</small><b>{price(high)}</b></span>
      <span><small>LAST VOLUME</small><b>{last.volume_usd == null ? 'Unknown' : price(Number(last.volume_usd))}</b></span>
    </div>
    {rows.length === 1 && <div className="chart-limited"><span aria-hidden="true">◷</span><div><b>Only one candle available</b><p>This pool has limited history. One candle does not show a price trend.</p></div></div>}
    <div className="token-chart-wrap candle-chart-wrap">
      <svg ref={chartRef} className="token-chart candle-chart" viewBox={`0 0 ${chartWidth} 268`} role="img" aria-label="Price candlestick history chart and USD volume">
        <title>Pool OHLC candles and volume, scaled by time. Shaded regions contain missing intervals.</title>
        {[0, 1, 2, 3].map((step) => {
          const value = ceiling - (ceiling - floor) * step / 3
          return <g key={step}><line x1="14" x2={plotRight} y1={y(value)} y2={y(value)} className="chart-grid" /><text x={plotRight + 14} y={y(value) + 4} className="chart-price-label">{price(value)}</text></g>
        })}
        {gaps.map((gap) => <rect key={gap.start} x={x(gap.start)} width={x(gap.end) - x(gap.start)} y="16" height="238" className="chart-gap"><title>Missing candles: {date(gap.start)} to {date(gap.end)}</title></rect>)}
        <line x1="14" x2={plotRight} y1="210" y2="210" className="chart-grid" />
        <text x={plotRight + 14} y="232" className="chart-price-label">Volume</text><text x={plotRight + 14} y="248" className="chart-price-label">USD</text>
        {rows.map((row) => {
          const color = Number(row.close_usd) >= Number(row.open_usd) ? '#52d9ad' : '#ff8394'
          const center = x(row.opened_at + row.resolution_seconds / 2)
          const width = Math.max(1, Math.min(14, (plotWidth * row.resolution_seconds / (end - start)) * .65))
          const volumeHeight = row.volume_usd == null ? null : Number(row.volume_usd) / volume * 34
          return <g key={row.opened_at} fill={color} stroke={color}>
            <title>{date(row.opened_at)} · Open {price(Number(row.open_usd))} · High {price(Number(row.high_usd))} · Low {price(Number(row.low_usd))} · Close {price(Number(row.close_usd))} · Volume {row.volume_usd == null ? 'unknown' : price(Number(row.volume_usd))}</title>
            <line x1={center} x2={center} y1={y(Number(row.high_usd))} y2={y(Number(row.low_usd))} />
            <rect x={center - width / 2} width={width} y={Math.min(y(Number(row.open_usd)), y(Number(row.close_usd)))} height={Math.max(1, Math.abs(y(Number(row.open_usd)) - y(Number(row.close_usd))))} rx="1" />
            {volumeHeight !== null && <rect x={center - width / 2} width={width} y={254 - volumeHeight} height={volumeHeight} opacity=".4" stroke="none" />}
          </g>
        })}
      </svg>
      <div className="chart-axis candle-axis"><span>{date(start)}</span><span>{rows.length === 1 ? '1 candle' : `${rows.length} candles`}</span><span>{date(end)}</span></div>
    </div>
    <div className="chart-footnote"><span><i className="chart-key candle-key" />Price &amp; volume</span>{gaps.length > 0 && <span><i className="chart-key gap-key" />{gaps.length} gap{gaps.length === 1 ? '' : 's'}</span>}<span>Ends at latest saved candle</span></div>
    <details className="chart-details candle-values"><summary>View candle values <span>Latest {Math.min(20, rows.length)}</span></summary><div className="candle-table-scroll"><table><caption>USD open, high, low, close and volume</caption><thead><tr>{['Time', 'Open', 'High', 'Low', 'Close', 'Volume'].map((label) => <th scope="col" key={label}>{label}</th>)}</tr></thead><tbody>{rows.slice(-20).reverse().map((row) => <tr key={row.opened_at}><th scope="row">{date(row.opened_at)}</th>{[row.open_usd, row.high_usd, row.low_usd, row.close_usd, row.volume_usd].map((value, i) => <td key={i}>{value == null ? 'Unknown' : `$${value}`}</td>)}</tr>)}</tbody></table></div></details>
  </>
}
