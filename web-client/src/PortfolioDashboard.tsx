import { useState } from 'react'
import type { PortfolioSession, PortfolioTradeState, SessionWorkspace } from './PortfolioWorkspace'
import type { TradingFocus } from './OpenPositionsTerminal'
import type { MarketRow } from './TokenDetail'
import type { HoldingValuation, WalletPortfolioState } from './useWalletPortfolio'
import { dollars, finiteAmount, HISTORY_INTERVAL } from './portfolioMetrics'
import { solAmount } from './walletBalanceHistory'
import type { usePortfolioHistory } from './usePortfolioHistory'
import { receiptStatus, tokenQuantity } from './tradeFormatting'
import { aggregatePerformance, isTradingResult, orderOwner, scopeStrategyIds, signedSol, strategyOperatingState, strategyResults } from './dashboardData'
import type { ServiceConnectionState } from './serviceControl'
import './portfolio-dashboard.css'

function markedValue(valuation: HoldingValuation | undefined) {
  return valuation?.mark.freshness === 'fresh' && valuation.current_value.state === 'estimated' && valuation.current_value.currency === 'USD' ? finiteAmount(valuation.current_value.amount) : null
}

function markedPnl(valuation: HoldingValuation | undefined) {
  return valuation?.mark.freshness === 'fresh' && valuation.unrealized_result.state === 'estimated_before_exit_costs' && valuation.unrealized_result.currency === 'USD' ? finiteAmount(valuation.unrealized_result.amount) : null
}

function tone(value: number | null) { return value === null ? '' : value < 0 ? 'negative' : 'positive' }
function stamp(at: number) { return new Date(at * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) }
function short(value: string) { return `${value.slice(0, 5)}…${value.slice(-4)}` }

export function HistoryChart({ title, field, rows, now, period, current, detail, emptyDetail }: {
  title: string; field: 'usd' | 'pnl' | 'sol'; rows: { at: number; usd: number | null; pnl?: number | null; sol?: number | null; continuous?: boolean }[]; now: number; period: number; current: number | null; detail: string; emptyDetail?: string
}) {
  const [selected, setSelected] = useState<number | null>(null)
  const start = now - period
  const points = rows.filter((row) => row.at >= start && row.at <= now)
  const values = points.flatMap((row) => row[field] === null || row[field] === undefined ? [] : [row[field]!])
  const low = Math.min(...values, ...(field === 'pnl' ? [0] : []))
  const high = Math.max(...values, ...(field === 'pnl' ? [0] : []))
  const padding = Math.max((high - low) * .15, Math.abs(high) * .02, field === 'sol' ? .000000001 : .01)
  const min = field === 'pnl' ? low - padding : Math.max(0, low - padding)
  const max = high + padding
  const left = field === 'sol' ? 112 : 64
  const width = 690 - left
  const x = (at: number) => left + (at - start) / period * width
  const y = (value: number) => 16 + (max - value) / (max - min) * 150
  let previous: { at: number } | null = null
  const path = points.map((point) => {
    const value = point[field]
    if (value === null || value === undefined) { previous = null; return '' }
    const command = previous && (point.continuous ?? point.at - previous.at <= HISTORY_INTERVAL * 2) ? 'L' : 'M'
    previous = point
    return `${command}${x(point.at).toFixed(2)},${y(value).toFixed(2)}`
  }).join(' ')
  const inspected = selected === null ? null : points[selected]
  const inspectedValue = inspected?.[field]
  const readout = inspected ? inspectedValue ?? null : current
  const chartTone = field === 'pnl' ? tone(current) : 'worth'
  const format = (value: number | null) => field === 'sol' ? solAmount(value) : dollars(value, field === 'pnl')
  return <section className={`dashboard-chart ${chartTone}`} aria-label={`${title} chart`}>
    <header><div><h3>{title}</h3><p>{detail}</p></div><b className={field === 'pnl' ? tone(readout) : ''}>{format(readout)}</b></header>
    {values.length ? <div className="dashboard-chart-plot" tabIndex={0} role="img" aria-label={`${title}: ${points.length} observed snapshots. Use left and right arrows to inspect values.`}
      onMouseLeave={() => setSelected(null)} onBlur={() => setSelected(null)} onFocus={() => setSelected(points.length - 1)}
      onKeyDown={(event) => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); setSelected((previous) => Math.max(0, Math.min(points.length - 1, (previous ?? points.length - 1) + (event.key === 'ArrowLeft' ? -1 : 1)))) } }}
      onMouseMove={(event) => {
        const rect = event.currentTarget.getBoundingClientRect()
        const at = start + ((event.clientX - rect.left) / rect.width * 710 - left) / width * period
        const nearest = points.reduce((best, point, index) => Math.abs(point.at - at) < Math.abs(points[best].at - at) ? index : best, 0)
        setSelected(nearest)
      }}>
      <svg viewBox="0 0 710 194" aria-hidden="true">
        {[max, (max + min) / 2, min].map((value, index) => <g key={index}><line x1={left} x2="690" y1={y(value)} y2={y(value)} className="chart-grid" /><text x={left - 10} y={y(value) + 4} textAnchor="end">{format(value)}</text></g>)}
        {field === 'pnl' && <line x1="64" x2="690" y1={y(0)} y2={y(0)} className="chart-zero" />}
        <path d={path} fill="none" stroke="currentColor" strokeWidth="2" />
        {points.filter((point) => point[field] !== null && point[field] !== undefined).map((point, index) => <circle key={index} cx={x(point.at)} cy={y(point[field]!)} r={values.length === 1 ? 4 : 2} fill="currentColor" />)}
        {inspected && inspectedValue !== null && inspectedValue !== undefined && <g><line className="chart-cursor" x1={x(inspected.at)} x2={x(inspected.at)} y1="16" y2="166" /><circle cx={x(inspected.at)} cy={y(inspectedValue)} r="5" fill="currentColor" /></g>}
        <text x={left} y="189">{stamp(start)}</text><text x="690" y="189" textAnchor="end">{stamp(now)}</text>
      </svg>
    </div> : <div className="dashboard-chart-empty"><span aria-hidden="true">⌁</span><b>No usable snapshots yet</b><p>{emptyDetail || (field === 'usd' ? 'A complete, fresh valuation is needed to record wallet value.' : 'Fresh marks and known cost basis are needed for open-position P&L.')}</p></div>}
    <footer aria-live="polite">{inspected ? `${stamp(inspected.at)} · ${format(inspectedValue ?? null)}` : values.length === 1 ? 'First snapshot · history builds as the wallet refreshes' : values.length ? `${values.length} snapshots · hover or use arrow keys to inspect` : 'History is saved on the local server as the app refreshes.'}</footer>
  </section>
}

type DashboardProps = {
  sessions: PortfolioSession[]
  workspaces: SessionWorkspace[]
  trades: PortfolioTradeState | null
  market: MarketRow[]
  error: string
  focus: TradingFocus | null
  service: ServiceConnectionState
  selectFocus: (focus: TradingFocus) => void
  viewPosition: (id: string, strategyId: string | null) => void
  viewPositions: () => void
  viewWallet: () => void
  viewActivity: () => void
  reviewFunds: (workspace: SessionWorkspace) => void
  createSession: () => void
}

export function PortfolioDashboard({ sessions, workspaces, trades, market, error, focus, service, selectFocus, viewPosition, viewPositions, viewWallet, viewActivity, reviewFunds, createSession }: DashboardProps) {
  const [sortByPnl, setSortByPnl] = useState(false)
  const rows = strategyResults(sessions, workspaces, focus)
  const [filter, setFilter] = useState<'current' | 'all' | 'attention'>(focus?.kind === 'strategy' && rows.some((row) => row.archived) ? 'all' : 'current')
  const ids = scopeStrategyIds(focus, workspaces)
  const open = trades?.positions.filter((position) => position.status === 'open' && (!ids || Boolean(position.agent_id && ids.has(position.agent_id)))) || []
  const orders = (trades?.orders || []).filter((order) => !ids || Boolean(orderOwner(order, trades) && ids.has(orderOwner(order, trades)!))).sort((a, b) => b.created_at - a.created_at)
  const monitoring = rows.filter((row) => row.session?.tracking.enabled && !row.session.tracking.fault).length
  const trading = rows.filter((row) => strategyOperatingState(row, service) === 'Trading').length
  const currentRows = rows.filter((row) => !row.archived)
  const faults = currentRows.filter((row) => row.session?.tracking.fault)
  const fundingAttention = [...new Map(currentRows.filter((row) => row.workspace?.funding?.state === 'insufficient').map((row) => [row.workspace!.id, row.workspace!])).values()]
  const attention = currentRows.filter((row) => row.session?.tracking.fault || row.workspace?.funding?.state === 'insufficient')
  const pending = orders.filter((order) => !['finalized', 'failed', 'cancelled', 'expired'].includes(order.status))
  const stale = open.filter((position) => position.valuation?.mark.freshness !== 'fresh')
  const result = aggregatePerformance(rows, error ? null : trades, ids)
  const positionsFor = (id: string) => open.filter((position) => position.agent_id === id).length
  const visible = rows.filter((row) => filter === 'all' ? true : filter === 'attention' ? attention.includes(row) : !row.archived)
    .sort((a, b) => {
      if (sortByPnl) {
        const av = a.performance?.total_lamports, bv = b.performance?.total_lamports
        if (av == null || bv == null) return av == null ? bv == null ? a.label.localeCompare(b.label) : 1 : -1
        return BigInt(av) > BigInt(bv) ? -1 : BigInt(av) < BigInt(bv) ? 1 : a.label.localeCompare(b.label)
      }
      return Number(Boolean(b.session?.tracking.enabled || positionsFor(b.id))) - Number(Boolean(a.session?.tracking.enabled || positionsFor(a.id))) || a.label.localeCompare(b.label)
    })
  const marketByMint = new Map(market.map((row) => [row.mint, row]))
  const pnlTone = (value: string | null | undefined) => value == null ? '' : BigInt(value) < 0n ? 'negative' : 'positive'
  return <div className="portfolio-dashboard operations-dashboard overview">
    {error && <p className="dashboard-data-warning" role="status">{error}</p>}
    <div className="dashboard-metrics">
        <div><small>Realized P&amp;L</small><b className={pnlTone(result.realized)}>{signedSol(result.realized)}</b><span>Confirmed sales and recorded costs</span></div>
        <div><small>Unrealized P&amp;L</small><b className={pnlTone(result.unrealized)}>{signedSol(result.unrealized)}</b><span>Fresh estimates · before exit costs</span></div>
        <div><small>Total strategy P&amp;L</small><b className={pnlTone(result.total)}>{signedSol(result.total)}</b><span>{result.detail}</span></div>
        <div><small>Closed positions</small><b>{rows.filter(isTradingResult).every((row) => row.performance) && trades && !error ? rows.reduce((n, row) => n + (row.performance?.closed_positions || 0), 0) : 'Unavailable'}</b><span>Includes archived strategies</span></div>
    </div>
    <section className="dashboard-operation-summary" aria-label="Current strategy status">
      <span title="Scanning enabled; monitoring does not authorize trading"><b>{monitoring}</b> monitoring</span>
      <span title="Verified active service authorization"><b>{trading}</b> trading</span>
      {focus && <button onClick={viewPositions}><b>{trades && !error ? open.length : 'Unavailable'}</b> open positions →</button>}
      <small>Current strategies · monitoring and trading are separate</small>
    </section>
    {Boolean(fundingAttention.length || faults.length || pending.length || stale.length) && <section className="dashboard-attention" aria-label="Needs attention">
      <div className="dashboard-attention-items"><b>Needs attention</b>
        {fundingAttention.map((workspace) => <div key={workspace.id} className="dashboard-attention-item"><span>{workspace.label} needs funds{workspace.funding?.shortfall_lamports ? ` · short ${signedSol(workspace.funding.shortfall_lamports).replace(/^\+/, '')}` : ''}</span><button onClick={() => reviewFunds(workspace)}>Review session funds →</button></div>)}
        {faults.length > 0 && <div className="dashboard-attention-item"><span>{faults.length} current {faults.length === 1 ? 'strategy needs' : 'strategies need'} review</span><button onClick={() => selectFocus({ kind: 'strategy', id: faults[0].id })}>Review strategy →</button></div>}
        {pending.length > 0 && <div className="dashboard-attention-item"><span>{pending.length} orders awaiting resolution</span><button onClick={viewActivity}>Review orders →</button></div>}
        {stale.length > 0 && <div className="dashboard-attention-item"><span>{stale.length} positions need fresh prices</span><button onClick={focus ? viewPositions : viewWallet}>{focus ? 'Review positions →' : 'Review wallet →'}</button></div>}
      </div>
    </section>}
    <p className="dashboard-history-note">Since-start results in SOL. Recorded transaction costs are included; future exit costs are excluded. Observations and paper results are separate. Period returns and drawdown are unavailable.</p>
    <section className="dashboard-section" aria-label="Strategy comparison">
      <header><div><h3>Strategy performance</h3><p>{filter === 'all' ? `${rows.length} strategies · includes ${rows.length - currentRows.length} archived` : filter === 'attention' ? `${visible.length} current strategies need attention` : `${currentRows.length} current strategies`} · since start · session funds are shared</p></div>
        <div className="dashboard-comparison-tools"><button className="dashboard-result-sort" aria-pressed={sortByPnl} onClick={() => setSortByPnl((value) => !value)} aria-label="Sort by total P&L">Total P&amp;L {sortByPnl ? '↓' : '↕'}</button><div role="group" aria-label="Strategy filter">{(['current', 'all', 'attention'] as const).map((item) => <button key={item} aria-pressed={filter === item} onClick={() => setFilter(item)}>{item === 'current' ? 'Current' : item === 'all' ? 'All history' : 'Needs attention'}</button>)}</div></div>
      </header>
      {visible.length ? <div className="dashboard-table-scroll"><table className="dashboard-table" aria-label="Strategy comparison"><thead><tr><th>Strategy / session</th><th>Operating state</th><th>Open</th><th>Realized P&amp;L</th><th>Unrealized P&amp;L</th><th aria-sort={sortByPnl ? 'descending' : 'none'}>Total P&amp;L</th><th>Closed</th><th>Recorded fees</th></tr></thead><tbody>
        {visible.map((row) => {
          const live = isTradingResult(row)
          const state = strategyOperatingState(row, service)
          const number = row.performance?.open_positions ?? positionsFor(row.id)
          return <tr key={row.id} data-strategy-id={row.id}>
            <td><button className="dashboard-row-link" onClick={() => selectFocus({ kind: 'strategy', id: row.id })}>{row.label} ↗</button><small>{row.workspace?.label || 'Independent strategy'}{row.archived ? ' · archived' : ''}</small></td>
            <td data-label="Operating state"><span className={`dashboard-state ${state === 'Trading' ? 'trading' : state === 'Needs attention' ? 'attention' : ''}`}>{state}</span><small>{live ? row.session?.brief.execution_style === 'autopilot' ? 'Automatic setup' : 'Wallet approval per order' : 'Observation / paper research'}</small>{row.session?.tracking.fault && <small className="dashboard-data-warning">{row.session.tracking.fault}</small>}</td>
            <td data-label="Open positions">{live ? number : '—'}</td>
            {(['realized_lamports', 'unrealized_lamports', 'total_lamports'] as const).map((field) => <td key={field} data-label={field === 'realized_lamports' ? 'Realized P&L' : field === 'unrealized_lamports' ? 'Unrealized P&L' : 'Total P&L'} className={pnlTone(row.performance?.[field])} title={row.performance?.detail}>{live ? signedSol(row.performance?.[field]) : 'Not applicable'}</td>)}
            <td data-label="Closed positions">{live ? row.performance?.closed_positions ?? 'Unavailable' : '—'}</td><td data-label="Recorded fees">{live ? signedSol(row.performance?.fees_lamports).replace(/^\+/, '') : 'Not applicable'}</td>
          </tr>
        })}
      </tbody></table></div> : <div className="dashboard-list-empty"><b>{rows.length ? 'No strategies match this filter' : 'No strategies in this scope'}</b><p>{rows.length ? 'Choose All history to include archived results, or Current for every current strategy.' : 'Create a session, then add a strategy from your library.'}</p><button onClick={rows.length ? () => setFilter(filter === 'attention' ? 'current' : 'all') : createSession}>{rows.length ? filter === 'attention' ? 'View current strategies' : 'View all history' : 'Create session'}</button></div>}
    </section>
      {focus && <section className="dashboard-section" aria-label="Open positions preview"><header><div><h3>Open positions <span>{open.length}</span></h3><p>Recorded wallet positions · retained estimates before exit costs</p></div><button onClick={viewPositions}>View all positions →</button></header>
        {open.length ? <div className="dashboard-table-scroll"><table className="dashboard-table" aria-label="Open positions preview"><thead><tr><th>Token / strategy</th><th>Remaining cost</th><th>Current value</th><th>Unrealized P&amp;L</th><th>Management</th></tr></thead><tbody>{open.slice(0, 6).map((position) => <tr key={position.id}>
          <td><button className="dashboard-row-link" onClick={() => viewPosition(position.id, position.agent_id)}>{marketByMint.get(position.mint)?.symbol || short(position.mint)} ↗</button><small>{position.strategy_label || rows.find((row) => row.id === position.agent_id)?.label || (position.agent_id ? 'Archived strategy' : 'Manual trade')}</small></td>
          <td data-label="Remaining cost">{position.valuation?.basis.state === 'known' && position.valuation.basis.currency === 'USD' ? dollars(finiteAmount(position.valuation.basis.amount)) : 'Unavailable'}</td>
          <td data-label="Current value">{dollars(markedValue(position.valuation))}<small>{tokenQuantity(position.quantity, position.decimals)}</small></td><td data-label="Unrealized P&L" className={tone(markedPnl(position.valuation))}>{dollars(markedPnl(position.valuation), true)}</td><td data-label="Management">{position.managed ? 'Strategy managed' : 'User managed'}</td>
        </tr>)}</tbody></table></div> : <p className="dashboard-empty-note">{trades && !error ? 'No open wallet positions.' : 'Trading records unavailable.'}</p>}
      </section>}
      <section className="dashboard-section" aria-label="Recent trades"><header><div><h3>Recent trades</h3><p>Runner orders in the selected scope</p></div><button onClick={viewActivity}>View activity →</button></header>
        {!orders.length ? <p className="dashboard-empty-note">No recorded trades yet.</p> : <div className="dashboard-recent-trades">{orders.slice(0, 5).map((order) => <button key={order.id} data-order-id={order.id} onClick={viewActivity}><b>{marketByMint.get(order.mint)?.symbol || short(order.mint)}</b><span>{order.side.toUpperCase()}</span><span>{receiptStatus(order.status)}</span><time>{stamp(order.created_at)}</time></button>)}</div>}
      </section>
  </div>
}

export function WalletOverview({ portfolio, history, error }: { portfolio: WalletPortfolioState | null; history: ReturnType<typeof usePortfolioHistory>; error: string }) {
  const [period, setPeriod] = useState(86_400)
  const now = Math.max(portfolio?.checked_at || 0, Date.now() / 1000)
  return <div className="portfolio-dashboard wallet-overview">
    {(error || history.error) && <p className="dashboard-data-warning" role="status">{error || history.error}</p>}
    <div className="dashboard-wallet-value"><div><small>{history.valuation.partial ? 'Known wallet value · partial' : 'Estimated wallet value'}</small><b>{dollars(history.valuation.known)}</b><p>{history.valuation.reason}</p></div><p>Confirmed SOL and token assets · deposits, withdrawals and outside trades included</p></div>
    <section className="dashboard-history" aria-label="Wallet valuation history"><header><h3>Wallet valuation history</h3><div role="group" aria-label="Chart time range"><button aria-pressed={period === 86_400} onClick={() => setPeriod(86_400)}>24h</button><button aria-pressed={period === 7 * 86_400} onClick={() => setPeriod(7 * 86_400)}>7d</button></div></header>
      <p className="dashboard-worth-movement" aria-label="24-hour wallet worth movement"><small>24H WALLET VALUE CHANGE</small><b className={tone(history.movement?.usd ?? null)}>{dollars(history.movement?.usd ?? null, true)}</b><span>{history.movement ? `${history.movement.percent === null ? 'Percentage unavailable' : `${history.movement.percent >= 0 ? '+' : '−'}${Math.abs(history.movement.percent).toFixed(2)}%`} · includes deposits and withdrawals` : history.movementReason}</span></p>
      <div className="dashboard-charts"><HistoryChart title="Wallet value" field="usd" rows={history.rows} now={now} period={period} current={history.worth} detail="Estimated USD value · deposits and withdrawals included" /><HistoryChart title="Open positions P&L" field="pnl" rows={history.rows} now={now} period={period} current={history.pnl} detail="Current open Runner positions · USD estimate before exit costs" emptyDetail={history.pnlReason} /></div>
      <p className="dashboard-history-note">Observed snapshots only, retained for seven days as the app refreshes. Gaps stay blank. Open-position P&amp;L excludes closed positions and changes when positions close; it is not a strategy return curve.</p>
    </section>
  </div>
}
