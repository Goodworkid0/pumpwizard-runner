import { useState } from 'react'
import type { RefObject } from 'react'
import type { CoinDetailData } from './TokenDetail'
import type { WalletAsset } from './useWalletPortfolio'
import { holdingGraphPoints, holdingPriceSeries } from './holdingPriceHistory'

function usd(value: number) {
  return `$${value.toLocaleString(undefined, { maximumSignificantDigits: 6 })}`
}

function timeLabel(at: number) {
  return new Date(at * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export function HoldingPriceGraph({ asset, detail, poolAddress, loading, error, open, elementRef }: {
  asset: WalletAsset
  detail: CoinDetailData | null
  poolAddress: string
  loading: boolean
  error: string
  open: () => void
  elementRef: RefObject<HTMLDivElement | null>
}) {
  const [hovered, setHovered] = useState<number | null>(null)
  const label = asset.symbol || asset.name || asset.mint.slice(0, 7)
  const series = holdingPriceSeries(asset, detail, poolAddress)
  const points = holdingGraphPoints(series)
  const first = points[0]
  const last = points[points.length - 1]
  const tone = !first || !last || first.price_usd === last.price_usd ? 'flat' : last.price_usd > first.price_usd ? 'up' : 'down'
  const intervals = series.slice(1).map((row, index) => row.at - series[index].at).sort((a, b) => a - b)
  const candleIntervals = (detail?.pool_histories?.find((row) => row.address === poolAddress)?.candles || detail?.pool_histories?.[0]?.candles || detail?.candles || []).map((row) => row.resolution_seconds)
  const typical = Math.max(intervals[Math.floor(intervals.length / 2)] || 0, ...candleIntervals)
  const segments: typeof points[] = []
  for (const point of points) {
    const segment = segments[segments.length - 1]
    const previous = segment?.[segment.length - 1]
    if (!previous || (typical > 0 && point.at - previous.at > typical * 3)) segments.push([])
    segments[segments.length - 1].push(point)
  }
  const chosen = hovered === null ? last : points[Math.min(hovered, points.length - 1)]
  return <div className={`portfolio-price-graph ${tone}`} role="cell" data-label="Price history" ref={elementRef}>
    <button className="portfolio-price-graph-button" aria-label={`Open ${label} price chart`} onClick={open} title="Open historical price chart and holding details">
      {first ? <>
        <svg viewBox="0 0 200 70" preserveAspectRatio="none" role="img" aria-label={`${label} recorded price history`}
          onMouseMove={(event) => {
            const rect = event.currentTarget.getBoundingClientRect()
            const x = (event.clientX - rect.left) / rect.width * 200
            const nearest = points.reduce((best, row, index) => Math.abs(row.x - x) < Math.abs(points[best].x - x) ? index : best, 0)
            setHovered(nearest)
          }} onMouseLeave={() => setHovered(null)}>
          {[12, 35, 60].map((y) => <line className="price-grid" key={y} x1="5" x2="195" y1={y} y2={y} />)}
          {segments.map((segment, index) => <g key={index}>
            {segment.length > 1 && <>
              <polygon className="price-area" points={`${segment[0].x},65 ${segment.map((point) => `${point.x},${point.y}`).join(' ')} ${segment[segment.length - 1].x},65`} />
              <polyline points={segment.map((point) => `${point.x},${point.y}`).join(' ')} />
            </>}
            {segment.length === 1 && <circle cx={segment[0].x} cy={segment[0].y} r="2.5" />}
          </g>)}
          {hovered !== null && chosen && <><line className="price-cursor" x1={chosen.x} x2={chosen.x} y1="5" y2="65" /><circle cx={chosen.x} cy={chosen.y} r="3" /></>}
        </svg>
        <span className="portfolio-price-graph-readout"><b>{usd(chosen.price_usd)}</b><small>{hovered !== null ? timeLabel(chosen.at) : `${points.length} recorded price${points.length === 1 ? '' : 's'}`}</small></span>
        <span className="portfolio-price-graph-axis"><small>{timeLabel(first.at)}</small><small>{timeLabel(last.at)}</small></span>
      </> : <span className="portfolio-price-graph-empty"><span aria-hidden="true">⌁</span><b>{loading ? 'Loading history…' : 'No price history'}</b><small>{loading ? 'Reading public market records' : error ? 'History unavailable · retry chart' : 'Open chart to load history'}</small></span>}
    </button>
    {asset.price_change_24h !== null && Number.isFinite(asset.price_change_24h) && <span className={`portfolio-price-change ${asset.price_change_24h < 0 ? 'down' : 'up'}`}>{asset.price_change_24h >= 0 ? '+' : ''}{asset.price_change_24h.toFixed(2)}% <small>24h price</small></span>}
  </div>
}

export function HoldingTokenIcon({ asset }: { asset: Pick<WalletAsset, 'mint' | 'name' | 'symbol' | 'image_url'> }) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  const name = asset.name || asset.symbol || asset.mint.slice(0, 7)
  let hash = 2166136261
  for (const character of asset.mint) hash = Math.imul(hash ^ character.charCodeAt(0), 16777619) >>> 0
  const hue = hash % 360
  const cells = Array.from({ length: 15 }, (_, index) => (hash >>> (index % 24)) & 1)
  const artwork = asset.image_url && failedUrl !== asset.image_url
  return <span className="portfolio-token-icon" title={artwork ? name : `${name}: artwork unavailable; generated mint identifier`}>
    {artwork ? <img key={asset.image_url} src={`/api/v1/token-images/${encodeURIComponent(asset.mint)}?v=${encodeURIComponent(asset.image_url!)}`}
      alt={`${name} token artwork`} loading="lazy" decoding="async" onError={() => setFailedUrl(asset.image_url)} />
      : <svg viewBox="0 0 40 40" role="img" aria-label={`${name} generated token icon`}>
        <rect width="40" height="40" rx="10" fill={`hsl(${hue} 35% 18%)`} />
        {cells.flatMap((filled, index) => filled ? [...new Set([index % 3, 4 - index % 3])].map((x) =>
          <rect key={`${index}-${x}`} x={5 + x * 6} y={5 + Math.floor(index / 3) * 6} width="5" height="5" rx="1" fill={`hsl(${hue} 65% 70%)`} />) : [])}
      </svg>}
  </span>
}
