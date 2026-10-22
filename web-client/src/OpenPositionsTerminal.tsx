import { useEffect, useState, type ReactNode } from 'react'
import { formatSol } from './browserWallet'
import type { PortfolioExitPlan, PortfolioSession, PortfolioTradeState, SessionWorkspace } from './PortfolioWorkspace'
import { tokenQuantity } from './tradeFormatting'
import type { MarketRow } from './TokenDetail'
import type { HoldingValuation } from './useWalletPortfolio'
import { SessionObservations } from './SessionObservations'
import { ActivityOrders } from './ActivityOrders'

export type TradingDashboardTab = 'overview' | 'holdings' | 'positions' | 'order' | 'history' | 'observations'
export type TradingFocus = { kind: 'session' | 'strategy'; id: string }

type TerminalPosition = {
  id: string
  symbol: string
  name: string
  mint: string
  source: string
  sourceDetail: string
  sourceKind: 'wallet' | 'session'
  strategyId: string | null
  managed: boolean
  quantity: string
  openedAt: number
  basis: string
  initialBasis: string
  market: MarketRow | null
  markValue: number | null
  valuationDetail: string
  pnlUsd: number | null
  pnlPercent: number | null
  peakPrice: number | null
  management: string
  nextAction: string
  riskDetail: string
  action: string
  open: () => void
}

function shortMint(value: string) {
  return value ? `${value.slice(0, 6)}…${value.slice(-4)}` : 'Token'
}

function finite(value: unknown): number | null {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null
  const amount = Number(value)
  return Number.isFinite(amount) ? amount : null
}

function usd(value: string | number | null | undefined) {
  const amount = finite(value)
  if (amount === null) return '—'
  const magnitude = Math.abs(amount)
  return `$${amount.toLocaleString(undefined, {
    minimumFractionDigits: magnitude >= 1 && magnitude < 1000 ? 2 : 0,
    maximumFractionDigits: magnitude < 1 ? 8 : magnitude < 1000 ? 2 : 0,
  })}`
}

function percent(value: number | null) {
  return value === null ? 'Not comparable' : `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

function timeAgo(value: number) {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - value))
  return seconds < 60 ? `${seconds}s ago` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : seconds < 86400 ? `${Math.floor(seconds / 3600)}h ago` : `${Math.floor(seconds / 86400)}d ago`
}

function marketTrend(row: MarketRow | null) {
  const samples = (row?.market_cap_history || []).filter((value) => Number.isFinite(value) && value > 0)
  if (samples.length < 2) return null
  return (samples[samples.length - 1] / samples[0] - 1) * 100
}

function holdingValuationDetail(valuation: HoldingValuation | undefined) {
  if (!valuation) return 'Typed position valuation unavailable'
  if (valuation.mark.freshness === 'stale') return 'Stale mark — current value unavailable'
  if (valuation.mark.freshness === 'missing_time') return 'Mark time unknown — current value unavailable'
  if (valuation.mark.freshness === 'future') return 'Future-dated mark — current value unavailable'
  if (valuation.mark.freshness === 'invalid_time') return 'Invalid mark time — current value unavailable'
  if (valuation.mark.state === 'unavailable') return 'No retained mark — current value unavailable'
  if (valuation.basis.state !== 'known') return 'Fresh retained mark · cost basis unknown'
  return 'Fresh retained mark · estimate before exit costs'
}

function positionBasis(valuation: HoldingValuation | undefined) {
  if (!valuation) return 'Unavailable'
  if (valuation.basis.state === 'known' && valuation.basis.amount !== null) {
    return valuation.basis.currency === 'USD'
      ? usd(valuation.basis.amount)
      : `${valuation.basis.amount} ${valuation.basis.currency}`
  }
  if (valuation.basis.native_state === 'known' && typeof valuation.basis.native_amount === 'string'
    && valuation.basis.native_currency === 'lamports') return formatSol(valuation.basis.native_amount)
  return 'Unavailable'
}

function duration(seconds: number | null) {
  if (seconds === null || !Number.isFinite(seconds)) return null
  if (seconds >= 86_400) return `${Math.round(seconds / 86_400)}d`
  if (seconds >= 3_600) return `${Math.round(seconds / 3_600)}h`
  return `${Math.round(seconds / 60)}m`
}

function soldStages(value: number[] | string) {
  if (Array.isArray(value)) return value
  try {
    const parsed = JSON.parse(value)
    return Array.isArray(parsed) ? parsed.filter((stage): stage is number => Number.isInteger(stage)) : []
  } catch {
    return []
  }
}

function positionPlan(plan: PortfolioExitPlan | null, stages: number[], managed: boolean) {
  if (!managed || !plan) return {
    management: 'User managed',
    nextAction: 'Manual review',
    riskDetail: 'No active session exit plan',
  }
  const sold = new Set(stages)
  const nextTarget = plan.targets.find((_, index) => !sold.has(index))
  const protections = [
    plan.stop_loss_percent === null ? null : `${plan.stop_loss_percent}% stop`,
    plan.trailing_drawdown_percent === null ? null : `${plan.trailing_drawdown_percent}% trail`,
    duration(plan.max_hold_seconds),
  ].filter(Boolean)
  return {
    management: 'Session managed',
    nextAction: nextTarget ? `${usd(nextTarget.market_cap_usd)} cap · sell ${nextTarget.sell_percent}%` : 'Protect remaining position',
    riskDetail: protections.join(' · ') || 'Strategy exit rules active',
  }
}

export function OpenPositionsTerminal({
  sessions,
  trades,
  market,
  openSession,
  openToken,
  trade,
  activeTab: requestedTab,
  selectTab,
  order,
  positionFocus,
  workspaces,
  focus,
  selectFocus,
  clearFocus,
  editFunds,
  overview,
  holdings,
  holdingsCount,
  recordsError = '',
  refreshActivity,
}: {
  sessions: PortfolioSession[]
  trades: PortfolioTradeState | null
  market: MarketRow[]
  openSession: (id: string) => void
  openToken: (row: Pick<MarketRow, 'mint'>) => void
  trade: (mint: string, positionId: string) => void
  activeTab: TradingDashboardTab
  selectTab: (tab: TradingDashboardTab) => void
  overview: ReactNode
  holdings: ReactNode
  holdingsCount: number | null
  recordsError?: string
  refreshActivity?: () => void
  order: ReactNode
  positionFocus: { id: string; requestedAt: number } | null
  workspaces: SessionWorkspace[]
  focus: TradingFocus | null
  selectFocus: (focus: TradingFocus, tab?: TradingDashboardTab) => void
  clearFocus: () => void
  editFunds: (workspace: SessionWorkspace) => void
}) {
  const activeTab = requestedTab === 'positions' && !focus ? 'overview' : requestedTab
  const [activityView, setActivityView] = useState<'orders' | 'decisions'>('orders')
  const showingDecisions = activeTab === 'observations' || activityView === 'decisions'
  useEffect(() => {
    if (focus) document.querySelector('.trading-focus-header')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [focus])
  useEffect(() => {
    if (activeTab !== 'positions' || !positionFocus) return
    const target = document.getElementById(`position-${positionFocus.id}`)
    target?.focus({ preventScroll: true })
    target?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [activeTab, positionFocus])
  const walletPositionIds = new Set(trades?.positions.map((position) => position.id) || [])
  const marketByMint = new Map(market.map((row) => [row.mint, row]))
  const strategyById = new Map(sessions.map((session) => [session.instance.id, session]))
  const historyById = new Map(workspaces.flatMap((row) => (row.history_strategies || []).map((item) => [item.id, { ...item, workspace: row }] as const)))
  const workspace = focus?.kind === 'session' ? workspaces.find((row) => row.id === focus.id) : null
  const strategy = focus?.kind === 'strategy' ? strategyById.get(focus.id) : null
  const parentWorkspace = strategy ? workspaces.find((row) => row.id === strategy.instance.workspace_id) : focus?.kind === 'strategy' ? historyById.get(focus.id)?.workspace : null
  const focusIds = focus ? new Set(focus.kind === 'strategy' ? [focus.id] : [...(workspace?.strategy_ids || []), ...(workspace?.history_strategies?.map((row) => row.id) || [])]) : null
  const allPositions: TerminalPosition[] = [
    ...(trades?.positions.filter((position) => position.status === 'open').map((position) => {
      const retained = marketByMint.get(position.mint) || null
      const sessionPurchased = Boolean(position.agent_id)
      const managed = Boolean(position.agent_id && position.managed)
      const valuation = position.valuation
      const valuationDetail = holdingValuationDetail(valuation)
      const plan = positionPlan(position.exit_plan, position.sold_stages, managed)
      return {
        id: `wallet-${position.id}`,
        symbol: retained?.symbol || shortMint(position.mint),
        name: retained?.metadata?.name || retained?.symbol || shortMint(position.mint),
        mint: position.mint,
        source: sessionPurchased ? position.strategy_label || 'Strategy session' : 'You',
        sourceDetail: sessionPurchased ? managed ? 'Purchased and managed by session' : 'Purchased by session · now user-managed' : 'Purchased manually',
        sourceKind: sessionPurchased ? 'session' as const : 'wallet' as const,
        strategyId: position.agent_id,
        managed,
        quantity: tokenQuantity(position.quantity, position.decimals),
        openedAt: position.opened_at,
        basis: positionBasis(valuation),
        initialBasis: formatSol(position.original_cost),
        market: retained,
        markValue: valuation?.mark.freshness === 'fresh' && valuation.current_value.state === 'estimated' && valuation.current_value.currency === 'USD' ? finite(valuation.current_value.amount) : null,
        valuationDetail,
        pnlUsd: valuation?.mark.freshness === 'fresh' && valuation.unrealized_result.state === 'estimated_before_exit_costs' && valuation.unrealized_result.currency === 'USD' ? finite(valuation.unrealized_result.amount) : null,
        pnlPercent: valuation?.mark.freshness === 'fresh' && valuation.unrealized_result.state === 'estimated_before_exit_costs' ? finite(valuation.unrealized_result.percent) : null,
        peakPrice: finite(position.peak_price_usd),
        ...plan,
        action: managed ? 'Open session' : 'Review sell',
        open: () => managed && position.agent_id ? openSession(position.agent_id) : trade(position.mint, position.id),
      }
    }) || []),
    ...sessions.flatMap((session) => session.positions
      .filter((position) => position.status === 'open' && !walletPositionIds.has(position.id))
      .map((position) => {
        const retained = marketByMint.get(position.mint) || null
        const quantity = finite(position.quantity)
        const valuation = position.valuation
        const valuationDetail = holdingValuationDetail(valuation)
        const managed = position.management_mode === 'automatic'
        const plan = positionPlan(session.exit_plan, soldStages(position.sold_stages_json), managed)
        return {
          id: `session-${position.id}`,
          symbol: position.symbol || retained?.symbol || shortMint(position.mint),
          name: retained?.metadata?.name || position.symbol || retained?.symbol || shortMint(position.mint),
          mint: position.mint,
          source: session.instance.label,
          sourceDetail: 'Local monitor record · not a wallet receipt',
          sourceKind: 'session' as const,
          strategyId: session.instance.id,
          managed,
          quantity: quantity === null ? 'Unavailable' : quantity.toLocaleString(undefined, { maximumFractionDigits: 6 }),
          openedAt: position.opened_at,
          basis: positionBasis(valuation),
          initialBasis: position.total_cost_usd === null ? 'Unavailable' : usd(position.total_cost_usd),
          market: retained,
          markValue: valuation?.mark.freshness === 'fresh' && valuation.current_value.state === 'estimated' && valuation.current_value.currency === 'USD' ? finite(valuation.current_value.amount) : null,
          valuationDetail,
          pnlUsd: valuation?.mark.freshness === 'fresh' && valuation.unrealized_result.state === 'estimated_before_exit_costs' && valuation.unrealized_result.currency === 'USD' ? finite(valuation.unrealized_result.amount) : null,
          pnlPercent: valuation?.mark.freshness === 'fresh' && valuation.unrealized_result.state === 'estimated_before_exit_costs' ? finite(valuation.unrealized_result.percent) : null,
          peakPrice: finite(position.peak_price_usd),
          ...plan,
          action: 'Open session',
          open: () => openSession(session.instance.id),
        }
      })),
  ]
  const positions = focusIds ? allPositions.filter((position) => position.strategyId && focusIds.has(position.strategyId)) : allPositions
  const recordedPositions = new Map(trades?.positions.map((position) => [position.id, position]) || [])
  const orderOwner = (order: PortfolioTradeState['orders'][number]) => order.agent_id || recordedPositions.get(order.position_id || order.id)?.agent_id || null
  const strategyLabel = (id: string) => strategyById.get(id)?.instance.label || historyById.get(id)?.label || trades?.positions.find((position) => position.agent_id === id)?.strategy_label || 'Strategy'
  const managed = positions.filter((position) => position.managed).length
  const orders = (trades?.orders || []).filter((order) => !focusIds || Boolean(orderOwner(order) && focusIds.has(orderOwner(order)!))).sort((a, b) => b.created_at - a.created_at)
  const focusLabel = focus?.kind === 'session' ? workspace?.label || 'Session' : focus?.kind === 'strategy' ? strategy?.instance.label || strategyLabel(focus.id) : 'Wallet activity'
  const renderPosition = (position: TerminalPosition) => {
    const trend = marketTrend(position.market)
    const pnlTone = position.pnlUsd === null ? '' : position.pnlUsd >= 0 ? 'positive' : 'negative'
    return <article className="terminal-position-row" id={`position-${position.id}`} tabIndex={-1} key={position.id}>
      <div className="terminal-position-compact">
        <div className="terminal-position-token"><i>{position.symbol.slice(0, 2).toUpperCase()}</i><span><b>{position.name}</b><small>{position.source} · {position.sourceDetail}</small></span></div>
        <div data-label="Remaining cost"><b>{position.basis}</b></div>
        <div data-label="Current value"><b>{usd(position.markValue)}</b><small>{position.quantity} tokens</small></div>
        <div data-label="Unrealized P&L" className={pnlTone}><b>{position.pnlUsd === null ? 'Unavailable' : `${position.pnlUsd >= 0 ? '+' : '−'}${usd(Math.abs(position.pnlUsd))}`}</b><small>{position.pnlUsd === null ? position.valuationDetail : percent(position.pnlPercent)}</small></div>
        <div data-label="Management"><b>{position.management}</b><small>{position.nextAction}</small></div>
        <button className="terminal-position-action" onClick={position.open}>{position.action} →</button>
      </div>
      <details className="terminal-position-details"><summary>Position details</summary>
        <div className="terminal-position-buttons">{position.market && <button onClick={() => openToken(position.market!)}>Token details</button>}<code title={position.mint}>{position.mint}</code></div>
        <dl className="terminal-position-metrics">
          <div><dt>Opened</dt><dd>{timeAgo(position.openedAt)}</dd><small>{position.sourceDetail}</small></div>
          <div><dt>Initial cost</dt><dd>{position.initialBasis}</dd><small>Remaining {position.basis}</small></div>
          <div><dt>Market cap</dt><dd>{position.market ? usd(position.market.market_cap_usd) : '—'}</dd><small>{trend === null ? 'Trend unavailable' : `${percent(trend)} retained trend`}</small></div>
          <div><dt>Liquidity</dt><dd>{position.market ? usd(position.market.liquidity_usd) : '—'}</dd><small>Public market record</small></div>
          <div><dt>Quote freshness</dt><dd>{position.market ? timeAgo(position.market.observed_at) : 'Unavailable'}</dd><small>{position.valuationDetail}</small></div>
          <div><dt>Exit rules</dt><dd>{position.nextAction}</dd><small>{position.riskDetail}{position.peakPrice === null ? '' : ` · peak ${usd(position.peakPrice)}`}</small></div>
        </dl>
      </details>
    </article>
  }

  return <section className="positions-terminal trading-dashboard" aria-label="Trading dashboard">
    {focus && <header className="trading-focus-header" aria-label="Focus section">
      <div>
        <nav aria-label="Trading selection"><button onClick={() => clearFocus()}>All wallet activity</button>{parentWorkspace && <><span aria-hidden="true">›</span><button onClick={() => selectFocus({ kind: 'session', id: parentWorkspace.id })}>{parentWorkspace.label}</button></>}</nav>
        <small>{focus.kind === 'session' ? 'SESSION' : 'STRATEGY'}</small><h2>{focusLabel}</h2>
      </div>
      {focus.kind === 'strategy' && (strategy ? <button className="focus-strategy-controls" onClick={() => openSession(focus.id)}>Strategy controls</button> : <span className="focus-archived">Archived strategy</span>)}
      {workspace?.mode === 'trade' && <button className="focus-strategy-controls" onClick={() => editFunds(workspace)}>Edit session funds</button>}
    </header>}
    {!focus && <div className="dashboard-toolbar"><span>All sessions &amp; strategies</span><button className="primary dashboard-new-order" onClick={() => selectTab('order')}>New order</button></div>}
    <nav className="trading-tabs" role="tablist" aria-label="Trading dashboard views">
      <button id="trading-tab-overview" role="tab" aria-controls="trading-panel-overview" aria-selected={activeTab === 'overview'} className={activeTab === 'overview' ? 'selected' : ''} onClick={() => selectTab('overview')}><span>Overview</span><small>{focus ? 'Strategy performance and positions' : 'Strategy performance'}</small></button>
      {focus && <button id="trading-tab-positions" role="tab" aria-controls="trading-panel-positions" aria-selected={activeTab === 'positions'} className={activeTab === 'positions' ? 'selected' : ''} onClick={() => selectTab('positions')}><span>Positions</span><b>{positions.length}</b><small>Ownership and exit rules</small></button>}
      <button id="trading-tab-history" role="tab" aria-controls="trading-panel-history" aria-selected={activeTab === 'history' || activeTab === 'observations'} className={activeTab === 'history' || activeTab === 'observations' ? 'selected' : ''} onClick={() => { setActivityView('orders'); selectTab('history') }}><span>Activity</span><b>{orders.length}</b><small>Orders and decisions</small></button>
      <button id="trading-tab-holdings" role="tab" aria-controls="trading-panel-holdings" aria-selected={activeTab === 'holdings'} className={activeTab === 'holdings' ? 'selected' : ''} onClick={() => selectTab('holdings')}><span>Wallet</span>{holdingsCount !== null && <b>{holdingsCount}</b>}<small>All assets and wallet history</small></button>
    </nav>
    {activeTab === 'overview' && <div id="trading-panel-overview" className="trading-tab-panel" role="tabpanel" aria-labelledby="trading-tab-overview">{overview}</div>}
    {!focus && activeTab === 'holdings' && <div id="trading-panel-holdings" className="trading-tab-panel" role="tabpanel" aria-labelledby="trading-tab-holdings">{holdings}</div>}
    {focus && activeTab === 'positions' && <div id="trading-panel-positions" className="trading-tab-panel" role="tabpanel" aria-labelledby="trading-tab-positions" tabIndex={0}>
      <div className="trading-panel-heading"><div><small>PURCHASE ATTRIBUTION + CURRENT MANAGEMENT</small><h3 aria-label="Open Positions">Position activity</h3></div><span><b>{managed}</b> currently session-managed</span></div>
      {positions.length === 0 ? <div className="positions-terminal-empty">
        <b>No open positions for this {focus.kind}</b>
        <p>Holdings appear after a finalized buy or a recorded position.</p>
      </div> : <div className="positions-terminal-list">
        <div className="terminal-position-list-head" aria-hidden="true"><span>Token / strategy</span><span>Remaining cost</span><span>Current value</span><span>Unrealized P&amp;L</span><span>Management</span><span>Action</span></div>
        {positions.map(renderPosition)}
      </div>}
      <footer>Live retained data is market context, not an execution quote. Every sell still requests a fresh executable quote and wallet review.</footer>
    </div>}

    {activeTab === 'order' && !focus && <div id="trading-panel-order" className="trading-tab-panel trading-order-panel" role="tabpanel" aria-label="New order" tabIndex={0}><div className="dashboard-order-heading"><button onClick={() => selectTab('overview')}>← Overview</button><b>New order</b></div>{order}</div>}

    {(activeTab === 'history' || activeTab === 'observations') && <div id="trading-panel-history" className="trading-tab-panel trade-history-panel" role="tabpanel" aria-labelledby="trading-tab-history" tabIndex={0}>
      <div className="dashboard-activity-switch" role="group" aria-label="Activity type"><button aria-pressed={!showingDecisions} onClick={() => { setActivityView('orders'); selectTab('history') }}>Orders</button><button aria-pressed={showingDecisions} onClick={() => { setActivityView('decisions'); selectTab('history') }}>Decisions</button>{refreshActivity && <button className="activity-refresh" onClick={refreshActivity}>Refresh activity ↻</button>}</div>
      {showingDecisions ? <SessionObservations key={`${focus?.kind || 'wallet'}:${focus?.id || ''}`} sessions={sessions.filter((row) => !focusIds || focusIds.has(row.instance.id))} openToken={(mint) => openToken({ mint })} />
        : <ActivityOrders key={`${focus?.kind || 'wallet'}:${focus?.id || ''}`} orders={orders} market={marketByMint} owner={orderOwner} strategyLabel={strategyLabel} openToken={(mint) => openToken({ mint })} prepareOrder={focus ? null : () => selectTab('order')} available={Boolean(trades && !recordsError)} error={recordsError} />}
    </div>}
  </section>
}
