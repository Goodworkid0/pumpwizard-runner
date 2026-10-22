import { useMemo, useState } from 'react'
import { CandleChart } from './CandleChart'

export type TokenMetadata = {
  name?: string
  description?: string
  creator?: string
  website?: string
  twitter?: string
  telegram?: string
  verified?: boolean
  source?: string
}

export type MarketRow = {
  symbol: string
  mint: string
  price_usd: string
  market_cap_usd: string
  liquidity_usd: string
  observed_at: number
  created_at?: number
  metadata?: TokenMetadata
  market_cap_history?: number[]
}

export type HistoricalCandle = {
  source: string
  resolution_seconds: number
  opened_at: number
  open_usd: string
  high_usd: string
  low_usd: string
  close_usd: string
  volume_usd: string | null
}

export type HistoryBackfill = {
  status: 'not_started' | 'ready' | 'running' | 'complete' | 'error'
  before_at: number | null
  earliest_at: number | null
  latest_at: number | null
  row_count: number
  updated_at: number | null
  error: string
  coverage: string
}

export type CoinDetailData = {
  mint: string
  history: MarketRow[]
  next_before: number | null
  candles: HistoricalCandle[]
  next_candle_before: number | null
  pool_histories?: { address: string; dex: string; source: string; created_at: string; candles: HistoricalCandle[]; gaps: { after: number; before: number }[] }[]
  history_backfill: HistoryBackfill
  metadata: TokenMetadata
  activity: { at: number; kind: string; reason: string; label: string }[]
}

type Metric = 'price_usd' | 'market_cap_usd' | 'liquidity_usd'

const metrics: { id: Metric; label: string }[] = [
  { id: 'price_usd', label: 'Price' },
  { id: 'market_cap_usd', label: 'Market cap' },
  { id: 'liquidity_usd', label: 'Liquidity' },
]

function money(value: string | number | undefined) {
  const amount = Number(value)
  if (!Number.isFinite(amount)) return '—'
  return `$${amount.toLocaleString(undefined, {
    maximumFractionDigits: amount < 1 ? 8 : amount < 1000 ? 2 : 0,
  })}`
}

function timeAgo(value: number) {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - value))
  return seconds < 60 ? `${seconds}s ago` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : seconds < 86400 ? `${Math.floor(seconds / 3600)}h ago` : `${Math.floor(seconds / 86400)}d ago`
}

function Chart({ rows, candles, metric }: { rows: MarketRow[]; candles: HistoricalCandle[]; metric: Metric }) {
  const series = useMemo(() => {
    const local = rows.slice().reverse().map((row) => ({
    at: row.observed_at,
    value: Number(row[metric]),
    })).filter((point) => Number.isFinite(point.value) && point.value >= 0)
    if (metric !== 'price_usd') return local
    const oldestLocal = local[0]?.at
    const imported = candles.slice().reverse().map((candle) => ({
      at: candle.opened_at,
      value: Number(candle.close_usd),
    })).filter((point) => Number.isFinite(point.value) && point.value > 0 && (oldestLocal === undefined || point.at < oldestLocal))
    return [...imported, ...local]
  }, [candles, metric, rows])
  const values = series.map((point) => point.value)
  const stride = Math.max(1, Math.ceil(series.length / 240))
  const plotted = series.filter((_point, index) => index % stride === 0 || index === series.length - 1)
  const low = Math.min(...values)
  const high = Math.max(...values)
  const span = high - low || Math.max(high * .08, 1)
  const width = 760
  const height = 244
  const top = 18
  const bottom = 216
  const points = plotted.map((point) => {
    const x = plotted.length === 1 ? width / 2 : 18 + (point.at-plotted[0].at) / (plotted[plotted.length-1].at-plotted[0].at || 1) * (width - 36)
    const y = top + (high - point.value) / span * (bottom - top)
    return { ...point, x, y }
  })
  const first = values[0]
  const last = values[values.length - 1]
  const change = first > 0 && last !== undefined ? (last / first - 1) * 100 : null

  if (!series.length) return <div className="token-chart-empty">No retained observations for this metric.</div>
  return <>
    <div className="chart-summary">
      <span><small>LATEST</small><b>{money(last)}</b></span>
      <span><small>LOW</small><b>{money(low)}</b></span>
      <span><small>HIGH</small><b>{money(high)}</b></span>
      <span><small>PERIOD</small><b className={change !== null && change < 0 ? 'down' : 'up'}>{change === null ? '—' : `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`}</b></span>
    </div>
    <div className="token-chart-wrap">
      <svg className="token-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${metrics.find((item) => item.id === metric)?.label} history chart`}>
        <defs><linearGradient id="token-chart-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#8067ff" stopOpacity=".32" /><stop offset="1" stopColor="#8067ff" stopOpacity="0" /></linearGradient></defs>
        {[top, (top + bottom) / 2, bottom].map((y) => <line key={y} x1="18" y1={y} x2={width - 18} y2={y} className="chart-grid" />)}
        {points.slice(1).map((point, index) => point.at-points[index].at <= 90 ? <line key={point.at} x1={points[index].x} y1={points[index].y} x2={point.x} y2={point.y} className="chart-line" /> : null)}
        {points.map((point, index) => <circle key={`${point.at}-${index}`} cx={point.x} cy={point.y} r={points.length > 30 ? 2 : 3.5} className="chart-point"><title>{`${new Date(point.at * 1000).toLocaleString()} · ${money(point.value)}`}</title></circle>)}
      </svg>
      <div className="chart-axis"><span>{new Date(series[0].at * 1000).toLocaleString()}</span><span>{series.length} chart point{series.length === 1 ? '' : 's'}</span><span>{new Date(series[series.length - 1].at * 1000).toLocaleString()}</span></div>
    </div>
  </>
}

export function TokenDetail({ coin, close, trade, loadEarlier, retryHistory, historyImporting }: { coin: CoinDetailData; close: () => void; trade: (mint: string) => void; loadEarlier: () => Promise<void>; retryHistory: () => void; historyImporting: boolean }) {
  const [metric, setMetric] = useState<Metric>('price_usd')
  const [pool, setPool] = useState('')
  const pools = coin.pool_histories || []
  const selectedPool = pools.find((item) => item.address === pool) || pools[0]
  const candles = selectedPool?.candles || coin.candles || []
  const totalCandles = pools.length ? pools.reduce((count, item) => count + item.candles.length, 0) : (coin.candles || []).length
  const venue = (dex: string) => dex === 'pump-fun' ? 'Pump.fun' : dex === 'pumpswap' ? 'PumpSwap' : dex
  const [copied, setCopied] = useState(false)
  const [loadingHistory, setLoadingHistory] = useState(false)
  const latest = coin.history[0]
  const metadata = coin.metadata || {}
  const symbol = latest?.symbol || coin.mint.slice(0, 8)
  const name = metadata.name || symbol
  const links = [
    metadata.website && { label: 'Website', href: metadata.website },
    metadata.twitter && { label: 'X', href: metadata.twitter },
    metadata.telegram && { label: 'Telegram', href: metadata.telegram },
  ].filter((item): item is { label: string; href: string } => Boolean(item))
  const copyMint = () => navigator.clipboard.writeText(coin.mint).then(() => {
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1400)
  }).catch(() => undefined)

  return <div className="modal-backdrop token-detail-backdrop" role="presentation" onMouseDown={close}>
    <section className="token-detail" role="dialog" aria-modal="true" aria-label="Token details" onMouseDown={(event) => event.stopPropagation()}>
      <button className="close" aria-label="Close dialog" onClick={close}>×</button>
      <header className="token-detail-header">
        <span className="token-avatar">{symbol.slice(0, 2).toUpperCase()}</span>
        <div className="token-heading"><div><small>PUMP.FUN TOKEN</small>{metadata.verified && <span className="verified-badge">VERIFIED</span>}</div><h2>{name}</h2><p>${symbol}</p></div>
        <button className="mint-copy" onClick={copyMint} title={coin.mint}><span>MINT ADDRESS</span><code>{coin.mint}</code><b>{copied ? 'Copied' : 'Copy'}</b></button>
      </header>

      <div className="token-metrics">
        <article><small>PRICE</small><b>{money(latest?.price_usd)}</b><span>{latest ? `Updated ${timeAgo(latest.observed_at)}` : 'No snapshot'}</span></article>
        <article><small>MARKET CAP</small><b>{money(latest?.market_cap_usd)}</b><span>Latest retained value</span></article>
        <article><small>LIQUIDITY</small><b>{money(latest?.liquidity_usd)}</b><span>Latest retained value</span></article>
        <article><small>LAUNCHED</small><b>{latest?.created_at ? new Date(latest.created_at * 1000).toLocaleDateString() : '—'}</b><span>{metadata.source || 'Public market data'}</span></article>
      </div>

      <div className="token-detail-grid">
        <section className="token-chart-panel">
          <div className="token-panel-heading"><div><small>MARKET HISTORY</small><h3>{metrics.find((item) => item.id === metric)?.label}</h3></div><div className="chart-tabs" role="group" aria-label="Chart metric">{metrics.map((item) => <button key={item.id} aria-pressed={metric === item.id} onClick={() => setMetric(item.id)}>{item.label}</button>)}</div></div>
          {metric === 'price_usd' && pools.length > 0 && <div className="history-pool-toolbar"><label htmlFor="history-pool">Historical pool</label><select id="history-pool" title={selectedPool?.address} value={selectedPool?.address || ''} onChange={(e) => setPool(e.target.value)}>{pools.map((item) => <option key={item.address} value={item.address}>{venue(item.dex)} · {item.address.slice(0, 6)}…{item.address.slice(-6)}</option>)}</select><span>{candles.length} candle{candles.length === 1 ? '' : 's'} in this pool</span></div>}
          {metric === 'price_usd' && (selectedPool || candles.length) ? <CandleChart key={selectedPool?.address || 'imported'} candles={candles} /> : <Chart rows={coin.history} candles={[]} metric={metric} />}
          <details className="chart-details history-coverage"><summary>Data &amp; coverage <span>Public market history</span></summary><div>
            {selectedPool && metric === 'price_usd' ? <dl><div><dt>Historical venue</dt><dd>{venue(selectedPool.dex)}</dd></div><div><dt>Pool address</dt><dd><code>{selectedPool.address}</code></dd></div><div><dt>Provider created date</dt><dd>{selectedPool.created_at || 'Unknown'}</dd></div></dl> : <p>{metric === 'price_usd' ? 'Older imported candles have no retained pool identity.' : 'This metric uses locally saved market snapshots.'}</p>}
            <p>Each pool is charted separately, with up to 500 saved candles per pool. Shaded intervals have missing candles; missing volume is unknown. Historical pools do not identify the current trading route.</p>
            <p>Prices are historical, not executable quotes. Complete onchain history and bonding-curve launch coverage are not available.</p>
            {coin.history_backfill?.coverage && <p>{coin.history_backfill.coverage}</p>}
          </div></details>
          <div className="history-pagination">
            <span>{totalCandles} saved candle{totalCandles === 1 ? '' : 's'}{pools.length > 0 ? ` across ${pools.length} pool${pools.length === 1 ? '' : 's'}` : ''} · {coin.history.length} local snapshot{coin.history.length === 1 ? '' : 's'}</span>
            {(coin.next_before != null || coin.next_candle_before != null) && <button disabled={loadingHistory} onClick={() => {
              setLoadingHistory(true)
              void loadEarlier().finally(() => setLoadingHistory(false))
            }}>{loadingHistory ? 'Loading…' : 'Load earlier chart data'}</button>}
          </div>
          <div className={`history-import ${coin.history_backfill?.status || 'not_started'}`}>
            <div><small>HISTORY SYNC</small><b>{historyImporting ? 'Loading earlier candles…' : coin.history_backfill?.status === 'error' ? 'History needs another try' : coin.history_backfill?.status === 'complete' ? 'Available history loaded' : 'Earlier history loads automatically'}</b>{coin.history_backfill?.error && <p>{coin.history_backfill.error}</p>}</div>
            {coin.history_backfill?.status === 'error' && <button disabled={historyImporting} onClick={retryHistory}>{historyImporting ? 'Retrying…' : 'Retry history'}</button>}
          </div>
        </section>

        <aside className="token-about">
          <div><small>ABOUT</small><h3>{name}</h3></div>
          <p>{metadata.description || 'No project description was included in the public token record.'}</p>
          {metadata.creator && <dl><dt>CREATOR</dt><dd><code title={metadata.creator}>{metadata.creator}</code></dd></dl>}
          {links.length > 0 && <nav aria-label="Token links">{links.map((link) => <a href={link.href} target="_blank" rel="noreferrer" key={link.label}>{link.label} ↗</a>)}</nav>}
          <p className="metadata-note">Project details come from the public market record. Verify links and the mint before trading.</p>
        </aside>
      </div>

      <section className="token-activity">
        <div className="token-panel-heading"><div><small>YOUR STRATEGIES</small><h3>Recent activity</h3></div><span>{coin.activity.length} record{coin.activity.length === 1 ? '' : 's'}</span></div>
        {coin.activity.length === 0 ? <div className="token-empty"><b>No strategy activity for this token</b><p>Your monitors have not recorded a decision for this mint.</p></div> : coin.activity.slice(0, 8).map((entry) => <article key={`${entry.at}-${entry.kind}`}><span><b>{entry.label}</b><small>{timeAgo(entry.at)}</small></span><strong>{entry.kind.replace(/_/g, ' ')}</strong><p>{entry.reason}</p></article>)}
      </section>

      <footer className="token-detail-actions"><button onClick={close}>Back to positions</button><button className="primary" onClick={() => trade(coin.mint)}>Buy or sell ${symbol}</button></footer>
    </section>
  </div>
}
