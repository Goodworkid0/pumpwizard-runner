import { useState } from 'react'
import type { StrategyMonitorPlan } from './PortfolioWorkspace'
import { activityTime, activityUsd, decisionAmount, decisionLabel, orderTimeline } from './activityData'
import type { ActivityOrder, ActivityRange, DecisionRow } from './activityData'

export function OrderActivityChart({ orders, range, now }: { orders: ActivityOrder[]; range: ActivityRange; now: number }) {
  const [hovered, setHovered] = useState<number | null>(null)
  const { buckets, start, end, undated } = orderTimeline(orders, range, now)
  const maximum = Math.max(1, ...buckets.map((row) => row.buy + row.sell))
  const chosen = hovered === null ? null : buckets[hovered]
  return <section className="activity-chart-card" aria-label="Order submissions chart">
    <header><div><h4>Order activity</h4><p>Submitted buys and sells · matching filters</p></div><span className="activity-chart-legend"><i className="buy" />Buys <i className="sell" />Sells</span></header>
    {orders.length ? <>
      <svg className="activity-timeline" viewBox="0 0 700 170" role="img" aria-label="Order submissions over time">
        <title>Recorded order counts by submission time. Empty intervals have zero submissions.</title>
        {[...new Set([0, Math.ceil(maximum / 2), maximum])].map((count) => <g key={count}><line x1="35" x2="692" y1={140 - count / maximum * 115} y2={140 - count / maximum * 115} /><text x="25" y={144 - count / maximum * 115} textAnchor="end">{count}</text></g>)}
        {buckets.map((row, i) => {
          const x = 40 + i * 650 / buckets.length, width = 650 / buckets.length - 7
          const buys = row.buy / maximum * 115, sells = row.sell / maximum * 115
          return <g key={row.at} tabIndex={0} role="button" aria-label={`${activityTime(row.at, true)}: ${row.buy} buys, ${row.sell} sells`} onClick={() => setHovered(i)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setHovered(i) } }} onFocus={() => setHovered(i)} onBlur={() => setHovered(null)} onMouseEnter={() => setHovered(i)} onMouseLeave={() => setHovered(null)}>
            <title>{activityTime(row.at)} – {activityTime(row.end)} · {row.buy} buys, {row.sell} sells</title>
            <rect x={x - 3} y="16" width={width + 6} height="128" fill="transparent" stroke="none" />
            <rect className="activity-buy-bar" x={x} y={140 - buys} width={width} height={buys} rx="2" />
            <rect className="activity-sell-bar" x={x} y={140 - buys - sells} width={width} height={sells} rx="2" />
          </g>
        })}
      </svg>
      <div className="activity-chart-axis"><time>{activityTime(start, true)}</time><span>Orders</span><time>{activityTime(end, true)}</time></div>
      <p className="activity-chart-readout" role="status">{chosen ? `${activityTime(chosen.at, true)} · ${chosen.buy} buys · ${chosen.sell} sells` : 'Hover or focus a bar to inspect that interval.'}{undated > 0 && ` ${undated} records have no valid submission time.`}</p>
    </> : <div className="activity-chart-empty">No submitted orders match these filters.</div>}
  </section>
}

export function DecisionMarketChart({ rows, plan, selected, choose }: { rows: DecisionRow[]; plan: StrategyMonitorPlan | null; selected: string | null; choose: (id: string) => void }) {
  const [hovered, setHovered] = useState<string | null>(null)
  const plotted = rows.filter((row) => (decisionAmount(row, 'market_cap_usd') ?? 0) > 0 && decisionAmount(row, 'liquidity_usd') !== null && decisionAmount(row, 'liquidity_usd')! >= 0)
  const band = plan?.entry_cap_min != null && plan.entry_cap_max != null && Number.isFinite(plan.entry_cap_min) && Number.isFinite(plan.entry_cap_max) && plan.entry_cap_max >= plan.entry_cap_min && plan.entry_cap_min >= 0 ? [plan.entry_cap_min, plan.entry_cap_max] : null
  const ceiling = Math.max(1, band?.[1] || 0, ...plotted.map((row) => decisionAmount(row, 'market_cap_usd')!)) * 1.08
  const liquidityTop = Math.max(1, ...plotted.map((row) => decisionAmount(row, 'liquidity_usd')!))
  const x = (value: number) => 65 + value / ceiling * 545
  const y = (value: number) => 165 - Math.log1p(value) / Math.log1p(liquidityTop) * 135
  const active = rows.find((row) => row.id === hovered || (!hovered && row.id === selected))
  return <section className="activity-chart-card decision-market-chart" aria-label="Saved candidate market map">
    <header><div><h4>Candidate market map</h4><p>Saved market cap vs liquidity · select a token</p></div><span className="activity-chart-legend"><i className="buy" />Enter <i className="blocked" />Do not enter</span></header>
    {plotted.length ? <>
      <svg viewBox="0 0 660 210" role="img" aria-label="Saved market cap and liquidity by candidate">
        <title>Saved decision inputs. Liquidity uses a logarithmic scale. The shaded band is the selected strategy's entry market-cap range.</title>
        {band && <rect className="activity-entry-band" x={x(band[0])} y="24" width={x(band[1]) - x(band[0])} height="141" />}
        {[0, .5, 1].map((step) => {
          const amount = Math.expm1(Math.log1p(liquidityTop) * step), at = y(amount)
          return <g key={step}><line x1="65" x2="610" y1={at} y2={at} /><text x="57" y={at + 4} textAnchor="end">{activityUsd(amount)}</text></g>
        })}
        {[0, .5, 1].map((step) => <text key={step} x={x(ceiling * step)} y="187" textAnchor={step === 0 ? 'start' : step === 1 ? 'end' : 'middle'}>{activityUsd(ceiling * step)}</text>)}
        <text x="65" y="13">Liquidity (USD · log scale)</text><text x="610" y="205" textAnchor="end">Market cap (USD)</text>
        {plotted.map((row) => <g key={row.id} tabIndex={0} role="button" aria-label={`Inspect ${decisionLabel(row)} saved decision`} onClick={() => choose(row.id)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); choose(row.id) } }} onMouseEnter={() => setHovered(row.id)} onMouseLeave={() => setHovered(null)} onFocus={() => setHovered(row.id)} onBlur={() => setHovered(null)}>
          <title>{decisionLabel(row)} · {activityUsd(decisionAmount(row, 'market_cap_usd'))} cap · {activityUsd(decisionAmount(row, 'liquidity_usd'))} liquidity · {row.reason.replace(/_/g, ' ')}</title>
          <circle className={`activity-candidate ${row.kind === 'entry_recommended' ? 'enter' : 'blocked'} ${row.id === selected ? 'selected' : ''}`} cx={x(decisionAmount(row, 'market_cap_usd')!)} cy={y(decisionAmount(row, 'liquidity_usd')!)} r={row.id === selected ? 7 : 5} />
        </g>)}
      </svg>
      <p className="activity-chart-readout" role="status">{active ? `${decisionLabel(active)} · ${activityUsd(decisionAmount(active, 'market_cap_usd'))} cap · ${activityUsd(decisionAmount(active, 'liquidity_usd'))} liquidity` : band ? `Shaded entry range: ${activityUsd(band[0])}–${activityUsd(band[1])}` : 'Entry ranges vary by strategy. Select a strategy to show its range.'}{rows.length > plotted.length && ` · ${rows.length - plotted.length} missing usable cap/liquidity`}</p>
    </> : <div className="activity-chart-empty">Saved cap and liquidity are unavailable for these candidates.</div>}
  </section>
}
