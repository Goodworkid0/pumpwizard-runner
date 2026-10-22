import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { api, errorText } from './browserWallet'
import type { WalletSession } from './browserWallet'
import { CandleChart } from './CandleChart'
import { HoldingPriceGraph, HoldingTokenIcon } from './HoldingVisuals'
import { holdingPriceSeries } from './holdingPriceHistory'
import { tokenQuantity } from './tradeFormatting'
import type { CoinDetailData, MarketRow } from './TokenDetail'
import type { HoldingValuation, WalletAsset, WalletPortfolioState } from './useWalletPortfolio'

export type PortfolioExitPlan = {
  approval: string
  stop_loss_percent: number | null
  take_profit_percent: number | null
  trailing_drawdown_percent: number | null
  max_hold_seconds: number | null
  targets: { market_cap_usd: number; sell_percent: number }[]
}

export type StrategyPosition = {
  id: string
  mint: string
  symbol: string
  status: string
  management_mode: string
  opened_at: number
  initial_quantity: string
  quantity: string
  total_cost_usd: string | null
  remaining_cost_usd: string | null
  realized_proceeds_usd: string
  sold_stages_json: string
  peak_price_usd: string
  last_usable_at: number
  valuation?: HoldingValuation
}

export type StrategyMonitorPlan = {
  entry_style: string
  entry_cap_min: number | null
  entry_cap_max: number | null
  observation_seconds: number | null
  minimum_samples: number | null
  minimum_price_changes: number | null
  order_usd: number | null
  max_positions: number | null
  min_curve_liquidity_usd: number | null
  min_amm_liquidity_usd: number | null
  scan_interval_seconds: number
}

export type StrategyRiskPolicy = {
  allocation_lamports: number | string
  available_cash_lamports?: number | string
  buys_enabled: boolean
  sells_enabled: boolean
  min_trade_lamports: number | string
  max_trade_lamports: number | string
  max_daily_buy_lamports: number | string
  max_trades_per_day: number
  max_daily_loss_lamports: number | string
  max_trade_loss_bps: number
  max_daily_loss_bps: number
  max_open_positions: number
}

export type SessionEntryObservation = {
  mint: string
  symbol?: string
  observed_at: number
  price_usd?: number | string
  market_cap_usd?: number | string
  liquidity_usd?: number | string
  created_at?: number
  metadata?: { name?: string; verified?: boolean; source?: string }
}

export type SessionEntryConsideration = {
  at: number
  kind: 'entry_recommended' | 'entry_blocked'
  mint: string
  reason: string
  payload: { symbol?: string; price_usd?: number | string; market_cap_usd?: number | string }
  observation: SessionEntryObservation | null
}

export type SessionBrief = {
  schema: 'pumpwizard.session-brief/v1' | 'pumpwizard.session-brief/v2' | 'pumpwizard.session-brief/v3' | 'pumpwizard.session-brief/v4'
  strategy_digest_at_creation: string
  goal: 'observe-market' | 'validate-strategy' | 'collect-evidence'
  intent: string
  mode: 'observe-only' | 'monitoring' | 'trade-interactive' | 'trade-autopilot'
  session_mode?: 'monitoring' | 'trade'
  execution_style?: null | 'interactive' | 'autopilot'
  automation_scope?: null | 'entries-only' | 'exits-only' | 'entries-and-exits'
  custody_mode?: 'main-wallet-service'
  profit_management?: null | 'user-directed' | 'strategy-managed' | 'winner-handoff'
  permissions?: {
    prepare_orders: boolean
    automatic_entries: boolean
    automatic_protective_exits: boolean
    automatic_profit_exits: boolean
    winner_handoff: boolean
    user_signature_each_order: boolean
    automation_wallet_required: boolean
    generated_wallet_required?: boolean
    activation_required: boolean
    custody: 'none' | 'connected-wallet' | 'automation-wallet' | 'main-wallet-browser' | 'main-wallet-service'
    service_vault_required?: boolean
    continues_without_browser?: boolean
  }
  paper_budget_usd: number
  paper_order_usd: number
  max_session_loss_usd: number
  max_positions: number
  review_after_seconds: number
  created_at: number
  review_at: number
  answers: { question_id: string; answer: string | number | null }[]
  preferences: null | {
    risk_tolerance: 'conservative' | 'balanced' | 'aggressive'
    reward_risk_ratio: number
    investment_style: 'small' | 'standard' | 'concentrated'
    time_horizon: 'intraday' | 'swing' | 'long-term'
  }
  limits: string
}

export type PortfolioSession = {
  paper_research_allowed?: boolean
  instance: { id: string; workspace_id?: string | null; strategy_digest?: string; label: string; status: string; entry_mode: string; exit_mode: string }
  brief: SessionBrief
  tracking: { fault: string; enabled: number; next_at?: number }
  state: { label: string; tone: string; reason: string; action: string }
  health: { last_run: 'never' | { finished_at: number; status: string; observations?: number; decisions?: number; detail: string } }
  positions: StrategyPosition[]
  exit_plan: PortfolioExitPlan
  monitor_plan: StrategyMonitorPlan
  risk_policy: StrategyRiskPolicy | null
  entry_considerations: SessionEntryConsideration[]
}

export type SessionPerformance = {
  state: 'estimated' | 'unavailable' | 'not_applicable'
  total_lamports: string | null
  realized_lamports: string | null
  unrealized_lamports: string | null
  fees_lamports?: string | null
  open_positions: number
  closed_positions?: number
  detail: string
}

export type SessionWorkspace = {
  id: string
  created_at: number
  label: string
  mode: 'monitoring' | 'trade'
  allocation_lamports: string
  available_lamports: string
  committed_lamports: string
  attributed_open_cost_lamports: string
  allocation_status: 'none' | 'held' | 'released'
  wallet_sender: string | null
  wallet_network: string | null
  plan: Record<string, unknown>
  strategy_count: number
  position_count: number
  strategy_ids: string[]
  history_strategies?: { id: string; label: string; removed: boolean }[]
  running_strategies: number
  performance?: SessionPerformance
  strategy_performance?: { id: string; label: string; removed: boolean; performance: SessionPerformance }[]
  attention_strategies: number
  position_summary?: {
    open_positions: number
    managed_positions: number
    manual_positions: number
    other_wallet_positions: number
    entries_paused: boolean
    paused_entry_strategies: number
    automatic_exit_strategies: number
    pending: boolean
  } | null
  funding?: {
    state: 'checking' | 'sufficient' | 'insufficient' | 'pending' | 'unavailable'
    balance_lamports: string | null
    required_lamports: string
    shortfall_lamports: string
    checked_at: number | null
    paused_at: number | null
    detail: string
  } | null
}

export type PortfolioTradeState = {
  sender: string
  network: string
  positions: {
    id: string
    genesis: string
    sender: string
    mint: string
    opened_at: number
    quantity: string
    decimals: number | null
    initial_quantity: string
    remaining_cost: string
    original_cost: string
    realized_proceeds: string
    realized_cost: string
    transaction_fees: string | null
    realized_pnl: string | null
    status: string
    last_signature: string
    managed: boolean
    agent_id: string | null
    entry_fx: number | null
    sold_stages: number[]
    peak_price_usd: number | null
    last_usable_at: number | null
    strategy_label: string | null
    exit_plan: PortfolioExitPlan | null
    valuation?: HoldingValuation
  }[]
  orders: {
    id: string
    signature: string
    status: string
    detail: string
    created_at: number
    network: string
    side: 'buy' | 'sell'
    mint: string
    amount: string
    decimals: number | null
    position_id: string | null
    agent_id: string | null
    route: string
    receipt: null | { wallet_delta: string; token_delta: string; fee_lamports: string; slot: number; block_time: number; cost_basis: string }
  }[]
  withdrawals: { id: string; signature: string; status: string; recipient: string; lamports: string; fee: string; detail: string }[]
  pending: boolean
}

export type PortfolioMarket = {
  symbol: string
  mint: string
  price_usd: string
  observed_at: number
}

export type PortfolioSellTarget = {
  mint: string
  positionId: string
  quantity: string
  strategy: null | { agentId: string; label: string; mint: string; positionId: string }
}

type PurchaseSource = {
  key: string
  label: string
  detail: string
  target: PortfolioSellTarget
  disabled: boolean
}

function shortAddress(value: string) {
  return `${value.slice(0, 7)}…${value.slice(-5)}`
}

function CopyMint({ asset }: { asset: WalletAsset }) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<number | null>(null)
  const label = asset.symbol || asset.name || shortAddress(asset.mint)
  const copy = () => {
    navigator.clipboard.writeText(asset.mint).then(() => {
      setCopied(true)
      if (timer.current !== null) window.clearTimeout(timer.current)
      timer.current = window.setTimeout(() => setCopied(false), 1600)
    }).catch(() => undefined)
  }
  return <div className="portfolio-mint" data-label="Mint / CA">
    <code title={asset.mint}>{asset.mint}</code>
    <span><button onClick={copy} aria-label={`Copy CA for ${label}`} title="Copy full contract address">{copied ? 'Copied ✓' : 'Copy'}</button><a href={`https://solscan.io/token/${asset.mint}`} target="_blank" rel="noreferrer" aria-label="View on Solscan ↗">↗</a></span>
  </div>
}

function price(value: string | number | null | undefined) {
  const amount = Number(value)
  if (!Number.isFinite(amount)) return '—'
  return `$${amount.toLocaleString(undefined, { maximumSignificantDigits: 6 })}`
}

function dollars(value: string | number | null | undefined) {
  const amount = Number(value)
  if (value === null || value === undefined || !Number.isFinite(amount)) return 'Unavailable'
  if (amount > 0 && amount < .01) return '<$0.01'
  return `$${amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function quoteAge(observedAt: number | null) {
  if (observedAt === null) return 'No usable quote time'
  const seconds = Math.max(0, Math.round(Date.now() / 1_000 - observedAt))
  if (seconds < 60) return `${seconds}s old`
  if (seconds < 3_600) return `${Math.round(seconds / 60)}m old`
  if (seconds < 86_400) return `${Math.round(seconds / 3_600)}h old`
  return `${Math.round(seconds / 86_400)}d old`
}

function HoldingValue({ asset }: { asset: WalletAsset }) {
  const freshness = asset.valuation.mark.freshness
  const markState = ({
    fresh: '',
    stale: 'Stale mark',
    future: 'Future-dated mark',
    missing_time: 'Mark time unknown',
    invalid_time: 'Invalid mark time',
  } as const)[freshness]
  const qualityState = ({
    unpriced: 'Unpriced', stale: '', missing_time: '', illiquid: 'Illiquid',
    'likely-worthless': 'Likely worthless', flagged: 'Flagged', priced: '',
  } as const)[asset.valuation_state]
  const tags: string[] = [markState, qualityState]
  if (asset.organic_score_label === 'low' && asset.valuation_state !== 'likely-worthless') tags.push('Low market quality')
  const current = freshness === 'fresh' && asset.valuation.current_value.state === 'estimated'
  return <div className="portfolio-value" role="cell" data-label="Est. value">
    <b title={asset.valuation_detail}>{!current || asset.valuation.current_value.amount === null ? 'Unavailable' : `≈${dollars(asset.valuation.current_value.amount)}`}</b>
    <span title={asset.valuation_detail}>{current && asset.valuation.mark.price ? `${price(asset.valuation.mark.price)} / token` : 'No fresh usable market price'}</span>
    {tags.some(Boolean) && <em className={`portfolio-value-flags ${asset.valuation_state}`}>{tags.filter(Boolean).slice(0, 2).map((tag) => <i key={tag}>{tag}</i>)}</em>}
  </div>
}

function SnapshotPriceChart({ rows }: { rows: MarketRow[] }) {
  const points = rows.slice().reverse().map((row) => ({ at: row.observed_at, value: Number(row.price_usd) }))
    .filter((point) => Number.isFinite(point.value) && point.value > 0)
  if (!points.length) return <div className="portfolio-history-empty"><b>No saved price history yet</b><p>The token has no usable local snapshots or public pool candles.</p></div>
  const low = Math.min(...points.map((point) => point.value))
  const high = Math.max(...points.map((point) => point.value))
  const span = high - low || Math.max(high * .05, Number.EPSILON)
  const plotted = points.filter((_point, index) => index % Math.max(1, Math.ceil(points.length / 160)) === 0 || index === points.length - 1)
  const coordinates = plotted.map((point) => ({
    ...point,
    x: plotted.length === 1 ? 380 : 18 + (point.at - plotted[0].at) / (plotted[plotted.length - 1].at - plotted[0].at || 1) * 724,
    y: 18 + (high - point.value) / span * 142,
  }))
  const first = points[0].value
  const last = points[points.length - 1].value
  const change = first > 0 ? (last / first - 1) * 100 : null
  return <div className="portfolio-snapshot-chart">
    <div className="portfolio-chart-summary"><span><small>LATEST</small><b>{price(last)}</b></span><span><small>LOW</small><b>{price(low)}</b></span><span><small>HIGH</small><b>{price(high)}</b></span><span><small>PERIOD</small><b className={change !== null && change < 0 ? 'down' : 'up'}>{change === null ? '—' : `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`}</b></span></div>
    <svg viewBox="0 0 760 178" role="img" aria-label="Price history chart">
      {[18, 89, 160].map((y) => <line key={y} x1="18" x2="742" y1={y} y2={y} />)}
      <polyline points={coordinates.map((point) => `${point.x},${point.y}`).join(' ')} />
      {coordinates.map((point) => <circle key={point.at} cx={point.x} cy={point.y} r="3"><title>{new Date(point.at * 1000).toLocaleString()} · {price(point.value)}</title></circle>)}
    </svg>
    <div className="portfolio-chart-axis"><span>{new Date(points[0].at * 1000).toLocaleDateString()}</span><span>{points.length} saved snapshot{points.length === 1 ? '' : 's'}</span><span>{new Date(points[points.length - 1].at * 1000).toLocaleDateString()}</span></div>
  </div>
}

let inlineHistoryQueue = Promise.resolve()

function HoldingHistory({ asset, sources, coverageGaps, reviewSession, rowDetails, children }: {
  asset: WalletAsset
  sources: PurchaseSource[]
  coverageGaps: string[]
  reviewSession?: (id: string, label: string) => void
  rowDetails: ReactNode
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<CoinDetailData | null>(null)
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState('')
  const [pool, setPool] = useState('')
  const generation = useRef(0)
  const busy = useRef(false)
  const attempted = useRef(false)
  const inlineRef = useRef<HTMLDivElement>(null)
  const label = asset.symbol || asset.name || shortAddress(asset.mint)

  const load = useCallback(async (mode: 'auto' | 'open' | 'retry' = 'open') => {
    if (busy.current) return
    busy.current = true
    attempted.current = true
    const current = ++generation.current
    setLoading(true)
    setError('')
    try {
      let next = await api<CoinDetailData>(`/api/v1/coins/${encodeURIComponent(asset.mint)}?limit=100&candle_limit=500`)
      if (generation.current !== current) return
      setDetail(next)
      const needsHistory = holdingPriceSeries({ trend_points: [] }, next).length < 2
      const importable = next.history_backfill.status === 'not_started' || next.history_backfill.status === 'ready'
      if (mode === 'retry' || (importable && (mode !== 'auto' || needsHistory))) {
        setSyncing(true)
        try {
          await api(`/api/v1/coins/${encodeURIComponent(asset.mint)}/history/backfill`, {})
          next = await api<CoinDetailData>(`/api/v1/coins/${encodeURIComponent(asset.mint)}?limit=100&candle_limit=500`)
          if (generation.current === current) setDetail(next)
        } catch (failure) {
          if (generation.current === current) setError(errorText(failure))
        } finally {
          if (generation.current === current) setSyncing(false)
        }
      }
    } catch (failure) {
      if (generation.current === current) setError(errorText(failure))
    } finally {
      busy.current = false
      if (generation.current === current) setLoading(false)
    }
  }, [asset.mint])

  useEffect(() => {
    let active = true
    const enqueue = () => {
      observer?.disconnect()
      inlineHistoryQueue = inlineHistoryQueue.then(async () => {
        if (active && !attempted.current) await load('auto')
      }).catch(() => undefined)
    }
    const observer = typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) enqueue()
    }, { rootMargin: '80px' })
    if (observer && inlineRef.current) observer.observe(inlineRef.current)
    else enqueue()
    return () => { active = false; observer?.disconnect(); generation.current += 1 }
  }, [load])

  const showChart = () => {
    setOpen(true)
    if (!detail || error || ['not_started', 'ready', 'error'].includes(detail.history_backfill.status)) void load(error || detail?.history_backfill.status === 'error' ? 'retry' : 'open')
  }
  const toggle = () => {
    if (open) {
      setOpen(false)
      return
    }
    showChart()
  }
  const pools = detail?.pool_histories || []
  const selectedPool = pools.find((item) => item.address === pool) || pools[0]
  const candles = selectedPool?.candles || detail?.candles || []
  const venue = (dex: string) => dex === 'pump-fun' ? 'Pump.fun' : dex === 'pumpswap' ? 'PumpSwap' : dex
  const strategy = sources.find((source) => source.target.strategy)?.target.strategy || null
  const ownership = sources.map((source) => source.label).join(' · ')
  const valuation = asset.valuation
  const valuationLabel = valuation.mark.freshness === 'fresh'
    ? 'Fresh retained estimate'
    : valuation.mark.freshness === 'stale'
      ? 'Stale mark — current value unavailable'
      : `${valuation.mark.freshness.replace('_', ' ')} — current value unavailable`

  return <>
    <HoldingPriceGraph asset={asset} detail={detail} poolAddress={pool} loading={loading || syncing} error={error} open={showChart} elementRef={inlineRef} />
    {rowDetails}
    <div className="portfolio-row-actions">{children}<button className="portfolio-history-toggle" aria-expanded={open} aria-label={`${open ? 'Hide' : 'Show'} ${label} price history and holding details`} onClick={toggle}><span aria-hidden="true">⌁</span>{open ? 'Hide details' : 'Details'}</button></div>
    {open && <section className="portfolio-history-panel" aria-label={`${label} holding details and historical price`}>
      <header><div><small>HOLDING DETAILS</small><h4>{asset.name || label} source and market context</h4><p className="portfolio-details-intro">Current wallet ownership and historical prices. A sale requires a reviewed quote.</p></div>{pools.length > 1 && <label>Pool<select value={selectedPool?.address || ''} onChange={(event) => setPool(event.target.value)}>{pools.map((item) => <option value={item.address} key={item.address}>{venue(item.dex)} · {shortAddress(item.address)}</option>)}</select></label>}</header>
      <section className="portfolio-evidence-card" aria-label={`${label} details summary`}>
        <span><small>OWNERSHIP</small><b>{ownership}</b><em>{asset.runner_atoms !== '0' ? 'Linked PumpWizard records shown above' : 'No linked PumpWizard position'}</em></span>
        <span><small>QUOTE AGE</small><b>{quoteAge(asset.observed_at)}</b><em>{asset.observed_at === null ? 'Valuation time unavailable' : new Date(asset.observed_at * 1_000).toLocaleString()}</em></span>
        <span><small>LIQUIDITY / SOURCE</small><b>{asset.liquidity_usd === null ? 'Liquidity unavailable' : dollars(asset.liquidity_usd)}</b><em>{asset.price_source || 'No recorded price source or route'}</em></span>
        <span><small>VALUATION STATUS</small><b>{valuationLabel}</b><em>{asset.valuation_detail}</em></span>
        <span><small>COST BASIS</small><b>{valuation.basis.state === 'known' && valuation.basis.amount !== null ? dollars(valuation.basis.amount) : 'Cost basis unavailable'}</b><em>{valuation.basis.source.replace(/_/g, ' ')}</em></span>
        <span><small>EXECUTABLE EXIT</small><b>Executable exit unavailable</b><em>{valuation.executable_liquidation.reason}</em></span>
        {coverageGaps.length > 0 && <span className="portfolio-evidence-gaps"><small>KNOWN PORTFOLIO GAPS</small><b>{coverageGaps.length} declared</b><em>{coverageGaps.join(' · ')}</em></span>}
        {strategy && reviewSession && <button onClick={() => reviewSession(strategy.agentId, strategy.label)}>Review linked strategy</button>}
      </section>
      {loading && !detail ? <div className="portfolio-history-empty"><b>Loading price history…</b><p>Reading saved snapshots and public pool candles.</p></div>
        : candles.length ? <CandleChart candles={candles} />
          : detail ? <SnapshotPriceChart rows={detail.history} />
            : <div className="portfolio-history-empty"><b>Price history unavailable</b><p>{error || 'No historical record could be loaded.'}</p></div>}
      <footer><span>{syncing ? 'Loading earlier public candles…' : candles.length ? `${candles.length} saved candles${selectedPool ? ` · ${venue(selectedPool.dex)}` : ''}` : detail?.history.length ? `${detail.history.length} locally saved price snapshots` : 'No saved history'}<small>Historical USD data is informational and is not an executable quote.</small></span>{(error || detail?.history_backfill.status === 'error') && <button disabled={loading || syncing} onClick={() => void load('retry')}>{syncing ? 'Retrying…' : 'Retry history'}</button>}</footer>
      {error && detail && <p className="portfolio-history-error" role="alert">Earlier public history unavailable: {error}</p>}
    </section>}
  </>
}

function purchaseSources(asset: WalletAsset, trades: PortfolioTradeState | null, sellable: boolean) {
  const positions = new Map((trades?.positions || []).map((position) => [position.id, position]))
  const sources: PurchaseSource[] = []
  for (const id of asset.runner_position_ids) {
    const position = positions.get(id)
    if (!position || position.status !== 'open') continue
    const decimals = position.decimals ?? asset.decimals
    const strategy = position.agent_id ? {
      agentId: position.agent_id,
      label: position.strategy_label || 'Strategy',
      mint: asset.mint,
      positionId: position.id,
    } : null
    sources.push({
      key: position.id,
      label: strategy ? strategy.label : 'Manual trade in PumpWizard',
      detail: `${tokenQuantity(position.quantity, decimals)} tokens${strategy ? ' · strategy' : ''}`,
      target: {
        mint: asset.mint,
        positionId: position.id,
        quantity: tokenQuantity(position.quantity, decimals, false),
        strategy,
      },
      disabled: !sellable,
    })
  }
  if (BigInt(asset.untracked_atoms) > 0n) {
    sources.push({
      key: `external-${asset.mint}`,
      label: 'Outside PumpWizard',
      detail: `${tokenQuantity(asset.untracked_atoms, asset.decimals)} tokens`,
      target: {
        mint: asset.mint,
        positionId: '',
        quantity: tokenQuantity(asset.untracked_atoms, asset.decimals, false),
        strategy: null,
      },
      disabled: false,
    })
  }
  if (!sources.length) {
    const recorded = asset.runner_position_ids.length > 0
    sources.push({
      key: `unknown-${asset.mint}`,
      label: recorded ? 'PumpWizard purchase · loading source' : 'Outside PumpWizard',
      detail: `${tokenQuantity(asset.amount, asset.decimals)} tokens`,
      target: {
        mint: asset.mint,
        positionId: '',
        quantity: tokenQuantity(asset.amount, asset.decimals, false),
        strategy: null,
      },
      disabled: recorded,
    })
  }
  return sources
}

export function PortfolioWorkspace({
  wallet,
  trades,
  walletPortfolio,
  walletPortfolioError,
  walletPortfolioLoading,
  refresh,
  sell,
  reviewSession,
  openToken,
}: {
  wallet: WalletSession
  trades: PortfolioTradeState | null
  walletPortfolio: WalletPortfolioState | null
  walletPortfolioError: string
  walletPortfolioLoading: boolean
  refresh: () => Promise<void>
  sell: (target: PortfolioSellTarget) => void
  reviewSession?: (id: string, label: string) => void
  openToken: (mint: string) => void
}) {
  const reconciliations = new Map(walletPortfolio?.reconciliations.map((row) => [row.mint, row]) || [])
  return <section className="workspace-page portfolio-workspace" aria-label="Wallet holdings and tools">
    <div className="page-heading portfolio-heading">
      <div><small>DASHBOARD · WALLET ASSETS</small><h2>Wallet holdings</h2><p>Current token balances, purchase sources, price history and reviewed sales.</p></div>
      <div className="portfolio-heading-actions">
        <button className="primary" onClick={() => void refresh()} disabled={walletPortfolioLoading}>{walletPortfolioLoading ? 'Refreshing…' : 'Refresh holdings'}</button>
      </div>
    </div>

    <section className="portfolio-reference-bar" aria-label="Portfolio valuation context">
      <span><small>VALUATION CONTEXT</small><b>{walletPortfolio?.sol_usd_price == null ? 'SOL/USD unavailable' : `${dollars(walletPortfolio.sol_usd_price)} per SOL`}</b></span>
      <span><small>FRESHNESS</small><b>{walletPortfolio ? `Checked ${quoteAge(walletPortfolio.checked_at)}` : 'Loading wallet state'}</b></span>
      <span><small>COVERAGE</small><b>{walletPortfolio?.coverage || 'Coverage not loaded'}</b></span>
      <span><small>WALLET</small><b>{wallet.name} · {shortAddress(wallet.address)}</b></span>
    </section>

    {walletPortfolioError && <p className="wallet-portfolio-error" role="alert">Holdings unavailable: {walletPortfolioError}</p>}
    {!walletPortfolio && walletPortfolioLoading ? <div className="portfolio-empty"><b>Reading current holdings</b><p>Checking the connected wallet onchain.</p></div>
      : !walletPortfolio?.assets.length ? <div className="portfolio-empty"><b>No current token holdings</b><p>This wallet has no non-zero SPL token balances.</p></div>
        : <section className="portfolio-holdings" aria-label="Current holdings">
          <div className="portfolio-holdings-head" role="row"><span>Token</span><span>Holding</span><span>Est. value</span><span>Price history</span><span>Mint / CA</span><span>Purchased by</span><span>Actions</span></div>
          {walletPortfolio.assets.map((asset) => {
            const reconciliation = reconciliations.get(asset.mint)
            const sellable = reconciliation?.sellable ?? true
            const sources = purchaseSources(asset, trades, sellable)
            return <article className="portfolio-holding-row" role="row" key={`${asset.token_program}:${asset.mint}`}>
              <div className="portfolio-token" role="cell"><HoldingTokenIcon asset={asset} /><div className="portfolio-token-identity"><h3 aria-label={asset.name || asset.symbol || shortAddress(asset.mint)}><button className="portfolio-token-link" aria-label={`Open ${asset.symbol || asset.name || shortAddress(asset.mint)} token details`} onClick={() => openToken(asset.mint)}>{asset.name || asset.symbol || shortAddress(asset.mint)}</button></h3><span>{asset.symbol ? `$${asset.symbol}` : 'Unknown token'}{asset.holder_count !== null && ` · ${asset.holder_count.toLocaleString()} holders`}{asset.website && <a href={asset.website} target="_blank" rel="noreferrer">Site ↗</a>}</span></div></div>
              <div className="portfolio-balance" role="cell" data-label="Holding"><b>{tokenQuantity(asset.amount, asset.decimals)}</b><span>{asset.symbol || 'tokens'}{asset.price_usd ? ` · ${price(asset.price_usd)}` : ''}</span></div>
              <HoldingValue asset={asset} />
              <HoldingHistory asset={asset} sources={sources} coverageGaps={walletPortfolio.coverage_gaps} reviewSession={reviewSession} rowDetails={<>
              <CopyMint asset={asset} />
              <div className="portfolio-purchase-source" role="cell" data-label="Purchased by">{sources.map((source) => <span key={source.key}><b>{source.label}</b><small>{source.detail}</small></span>)}{reconciliation && !reconciliation.sellable && <em title="Wallet balance changed outside PumpWizard">Balance mismatch</em>}</div>
              </>}>{sources.map((source) => <button className="portfolio-sell-button" key={source.key} disabled={source.disabled || trades?.pending} title={source.disabled ? 'The onchain balance no longer covers this recorded position' : `Review sale of ${source.label} holding`} aria-label={`Sell ${asset.symbol || asset.name || shortAddress(asset.mint)} · ${source.label}`} onClick={() => sell(source.target)}>Sell</button>)}</HoldingHistory>
            </article>
          })}
        </section>}
  </section>
}
