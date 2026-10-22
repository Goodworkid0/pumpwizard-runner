import { useState } from 'react'
import type { ActivityRange } from './activityData'

export function ActivityRangeControl({ value, change, label }: { value: ActivityRange; change: (range: ActivityRange) => void; label: string }) {
  return <div className="activity-segments" role="group" aria-label={label}>{(['24h', '7d', 'all'] as const).map((range) => <button key={range} aria-pressed={value === range} onClick={() => change(range)}>{range === 'all' ? 'All recorded' : range}</button>)}</div>
}

export function ActivityPager({ page, count, size, change }: { page: number; count: number; size: number; change: (page: number) => void }) {
  const pages = Math.max(1, Math.ceil(count / size))
  return <div className="activity-pagination"><span>{count ? `${page * size + 1}–${Math.min(count, (page + 1) * size)} of ${count}` : '0 records'}</span>{pages > 1 && <div><button aria-label="Previous activity page" disabled={page === 0} onClick={() => change(page - 1)}>← Previous</button><span>Page {page + 1} / {pages}</span><button aria-label="Next activity page" disabled={page + 1 >= pages} onClick={() => change(page + 1)}>Next →</button></div>}</div>
}

export function CopyActivityValue({ value, kind }: { value: string; kind: string }) {
  const [result, setResult] = useState('')
  return <button className="activity-copy" aria-label={`Copy ${kind}`} onClick={() => {
    if (!navigator.clipboard) { setResult('Copy unavailable'); return }
    void navigator.clipboard.writeText(value).then(() => setResult('Copied')).catch(() => setResult('Copy unavailable'))
  }}>{result || `Copy ${kind}`}</button>
}
