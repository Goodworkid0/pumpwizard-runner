import { useContext, useEffect, useState } from 'react'
import { api } from './browserWallet'
import { WalletWorkspaceScope } from './walletWorkspaceScope'
import type { SessionPlan } from './SessionPlanDialog'

// Scale values only for display; the saved package keeps its existing units.
const sections: [string, string, string[]][] = [
  ['Entry rules', 'Choose which tokens qualify and how much to buy.', ['entry_cap_min', 'entry_cap_max', 'min_curve_liquidity_usd', 'min_amm_liquidity_usd']],
  ['Signal confirmation', 'Require enough price and liquidity evidence before an entry.', ['observation_seconds', 'minimum_samples', 'minimum_price_changes', 'minimum_growth_fraction', 'minimum_reserve_growth_usd', 'maximum_peak_drawdown', 'breakout_fraction', 'pullback_min_fraction', 'reclaim_fraction']],
  ['Exits', 'Exit rules run automatically only when the session permits automatic sells.', ['stop_loss_fraction', 'trailing_drawdown_fraction', 'max_hold_seconds', 'quote_gap_exit_seconds']],
  ['Quote and liquidity checks', 'Limit stale data, execution delay and market impact.', ['max_sample_gap_seconds', 'quote_max_age_seconds', 'entry_expiry_seconds', 'fill_delay_seconds', 'ownership_ttl_seconds', 'max_entry_reserve_fraction', 'max_roundtrip_loss_fraction']],
  ['Simulation costs', 'Assumptions for research estimates; these do not set network fees.', ['fee_fraction', 'adverse_fraction', 'fixed_fee_usd', 'entry_rent_usd', 'failed_attempt_probability']],
]
const labels: Record<string, string> = {
  entry_cap_min: 'Minimum market cap (USD)', entry_cap_max: 'Maximum market cap (USD)',
  min_curve_liquidity_usd: 'Minimum curve liquidity (USD)', min_amm_liquidity_usd: 'Minimum pool liquidity (USD)',
  order_usd: 'Trade size (USD)', budget_usd: 'Research budget (USD)', max_positions: 'Strategy maximum positions',
  observation_seconds: 'Observation window (minutes)', minimum_samples: 'Minimum samples', minimum_price_changes: 'Minimum price changes',
  minimum_growth_fraction: 'Minimum price growth (%)', minimum_reserve_growth_usd: 'Minimum reserve growth (USD)',
  maximum_peak_drawdown: 'Maximum pullback from peak (%)', breakout_fraction: 'Breakout threshold (%)',
  pullback_min_fraction: 'Minimum pullback (%)', reclaim_fraction: 'Reclaim threshold (%)',
  stop_loss_fraction: 'Stop loss (%)', trailing_drawdown_fraction: 'Trailing exit (%)', max_hold_seconds: 'Maximum hold (hours)',
  quote_gap_exit_seconds: 'Exit after missing quotes (minutes)', max_sample_gap_seconds: 'Maximum sample gap (seconds)',
  quote_max_age_seconds: 'Maximum quote age (seconds)', entry_expiry_seconds: 'Entry expiry (seconds)',
  fill_delay_seconds: 'Simulated fill delay (seconds)', ownership_ttl_seconds: 'Ownership timeout (seconds)',
  max_entry_reserve_fraction: 'Maximum entry share of reserves (%)', max_roundtrip_loss_fraction: 'Maximum round-trip loss (%)',
  fee_fraction: 'Trading fee assumption (%)', adverse_fraction: 'Adverse price movement (%)', fixed_fee_usd: 'Fixed fee assumption (USD)',
  entry_rent_usd: 'Account rent assumption (USD)', failed_attempt_probability: 'Failed attempt probability (%)',
}
const percentages = new Set(Object.keys(labels).filter((key) => labels[key].endsWith('(%)')))
function factor(key: string) { return key === 'stop_loss_fraction' ? -100 : percentages.has(key) ? 100 : key === 'max_hold_seconds' ? 1 / 3600 : ['observation_seconds', 'quote_gap_exit_seconds'].includes(key) ? 1 / 60 : 1 }
function displayed(key: string, value: unknown) { return typeof value === 'number' ? String(Number((value * factor(key)).toPrecision(12))) : String(value ?? '') }
const styles = ['momentum', 'breakout', 'pullback_reclaim', 'slow_burn', 'persistent', 'higher-low', 'multi-window', 'squeeze', 'retest', 'post-grad', 'liquidity', 'relative', 'second-wave', 'recovery']

export type AllocationSizing = { workspace_id: string; allocation_lamports: string; budget_sol: string; trade_budget_sol: string; trade_percent: string; sol_usd_price: number; budget_usd: number; order_usd: number }
export type SizingRequest = Pick<AllocationSizing, 'workspace_id' | 'allocation_lamports' | 'trade_percent'>
const editorTabs = [['budget', 'Budget & trades'], ['entry', 'Entry rules'], ['signal', 'Confirmation'], ['exits', 'Exits & targets'], ['advanced', 'Advanced']] as const

export function SavedStrategyEditor({ label, configuration, plan, sizing, attachedToSession = false, save, cancel }: {
  attachedToSession?: boolean
  label: string; configuration: Record<string, unknown>; plan?: SessionPlan | null; sizing?: AllocationSizing
  save: (label: string, configuration: Record<string, unknown>, sizing?: SizingRequest) => Promise<void>; cancel: () => void
}) {
  const scope = useContext(WalletWorkspaceScope)
  const funded = plan?.session_mode === 'trade'
  const [panel, setPanel] = useState('budget')
  const [quote, setQuote] = useState<AllocationSizing | null>(null)
  const [quoteError, setQuoteError] = useState('')
  const [refreshQuote, setRefreshQuote] = useState(0)
  const [tradePercent, setTradePercent] = useState(sizing?.trade_percent || (funded ? String(Number((Number(plan.max_trade_sol) / Number(plan.allocation_sol) * 100).toFixed(4))) : String(Number((Number(configuration.order_usd) / Number(configuration.budget_usd) * 100).toFixed(4)))))
  useEffect(() => {
    if (!funded || !plan?.workspace_id) return
    let current = true
    setQuote(null); setQuoteError('')
    api<{ sizing: AllocationSizing }>(`/api/v1/strategy-sizing${scope}&workspace_id=${encodeURIComponent(plan.workspace_id)}`)
      .then((result) => { if (current) setQuote(result.sizing) })
      .catch((reason) => { if (current) setQuoteError(reason instanceof Error ? reason.message : 'Sizing unavailable') })
    return () => { current = false }
  }, [funded, plan?.workspace_id, scope, refreshQuote])
  const [name, setName] = useState(label)
  const [fields, setFields] = useState(() => Object.fromEntries(Object.entries(configuration).filter(([key]) => key !== 'targets').map(([key, value]) => [key, displayed(key, value)])))
  const [targets, setTargets] = useState<{ cap: string; percent: string; original?: { market_cap_usd: number; initial_fraction: number } }[]>(() => (configuration.targets as { market_cap_usd: number; initial_fraction: number }[] || []).map((t) => ({ cap: String(t.market_cap_usd), percent: String(Number((t.initial_fraction * 100).toPrecision(12))), original: t })))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const percent = Number(tradePercent)
  const percentValid = /^\d+(?:\.\d{1,4})?$/.test(tradePercent) && percent > 0 && percent <= 100
  const tradeSol = quote && percentValid ? Number(BigInt(quote.allocation_lamports) * BigInt(Math.round(percent * 10000)) / 1000000n) / 1e9 : null
  const sizingProblem = !percentValid ? 'Choose a trade size greater than 0% and at most 100%.' : funded && !quote ? quoteError || 'Loading the allocated budget and SOL/USD…' : funded && plan && tradeSol !== null && (tradeSol < Number(plan.min_trade_sol) || tradeSol > Number(plan.max_daily_buy_sol)) ? `Trade budget must be between the initial minimum buy (${plan.min_trade_sol} SOL) and daily buy limit (${plan.max_daily_buy_sol} SOL).` : ''
  const money = (value: number) => value.toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 4 })
  const update = (key: string, value: string) => setFields((old) => ({ ...old, [key]: value }))
  const input = (key: string) => <label key={key}>{labels[key] || key.replace(/_/g, ' ')}<input type="number" step={['max_positions', 'minimum_samples', 'minimum_price_changes'].includes(key) ? '1' : 'any'} min="0" required value={fields[key]} onChange={(event) => update(key, event.target.value)} /></label>
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setError('')
    const config = { ...configuration }
    for (const [key, value] of Object.entries(fields)) {
      // Preserve untouched numbers exactly, including time conversions.
      config[key] = value === displayed(key, configuration[key]) ? configuration[key] : typeof configuration[key] === 'number' ? Number(value) / factor(key) : value
    }
    if (sizingProblem) { setError(sizingProblem); setPanel('budget'); return }
    if (!funded) config.order_usd = Number(config.budget_usd) * percent / 100
    config.targets = targets.map((t) => ({ ...t.original, market_cap_usd: t.original && t.cap === String(t.original.market_cap_usd) ? t.original.market_cap_usd : Number(t.cap), initial_fraction: t.original && t.percent === String(Number((t.original.initial_fraction * 100).toPrecision(12))) ? t.original.initial_fraction : Number(t.percent) / 100 }))
    if (!targets.length || targets.some((t) => !t.cap || !t.percent)) { setError('Add at least one complete exit target.'); return }
    setBusy(true)
    try { await save(name.trim(), config, funded && quote ? { workspace_id: quote.workspace_id, allocation_lamports: quote.allocation_lamports, trade_percent: tradePercent } : undefined) } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not save these rules.') } finally { setBusy(false) }
  }
  return <form className="saved-strategy-editor" aria-label="Edit saved strategy" onSubmit={(event) => void submit(event)}>
    <header className="editor-intro"><div><small>EDIT STRATEGY</small><h3>{name || 'Your strategy'}</h3><p>{attachedToSession ? 'Added to your session. Save applies these settings here; monitoring and trading remain off.' : 'Choose a section below. Save creates a revision for this wallet.'}</p></div></header>
    <nav className="strategy-editor-tabs" aria-label="Strategy editor sections">{editorTabs.map(([id, title]) => <button type="button" key={id} aria-pressed={panel === id} className={panel === id ? 'selected' : ''} onClick={() => setPanel(id)}>{title}</button>)}</nav>
    <div className="strategy-editor-panel">
    {panel === 'budget' && <>
      <div className="strategy-budget-summary"><span><small>{funded ? 'STRATEGY BUDGET · FROM SESSION' : 'SIMULATED RESEARCH BUDGET'}</small><b>{funded ? `${quote?.budget_sol || plan?.allocation_sol} SOL` : money(Number(fields.budget_usd))}</b><em>{funded ? quote ? `≈ ${money(quote.budget_usd)}` : 'Loading USD estimate…' : 'Simulated funds'}</em></span><span><small>PER TRADE · {percentValid ? `${percent}%` : '—'}</small><b>{funded ? tradeSol === null ? '—' : `${tradeSol.toLocaleString(undefined, { maximumFractionDigits: 9 })} SOL` : percentValid ? money(Number(fields.budget_usd) * percent / 100) : '—'}</b><em>{funded && quote && tradeSol !== null ? `≈ ${money(tradeSol * quote.sol_usd_price)}` : ''}</em></span></div>
      <p>{funded ? 'Budget comes from this session’s allocated SOL. Strategies share that pool; adding a strategy does not allocate the balance again.' : 'This monitoring session has no allocated wallet funds. Set a simulated research budget.'}</p>
      <fieldset disabled={busy}><legend>Budget & trade size</legend><div className="strategy-edit-grid"><label>Strategy name<input required maxLength={120} value={name} onChange={(e) => setName(e.target.value)} /></label><label>Trade size (% of strategy budget)<input type="number" min="0.0001" max="100" step="0.0001" required value={tradePercent} onChange={(e) => setTradePercent(e.target.value)} /></label>{!funded && input('budget_usd')}{input('max_positions')}</div><div className="sizing-shortcuts">{[5, 10, 20, 25].map((value) => <button type="button" key={value} onClick={() => setTradePercent(String(value))}>{value}% per trade</button>)}</div><p>{funded ? 'This amount becomes the initial maximum buy budget, including transaction costs. Orders must still fit the remaining allocation, fees and order limits.' : 'Each simulated trade uses this percentage of the research budget.'}</p></fieldset>
      {funded && <div className="sizing-reference"><span>{quote ? `USD conversion at setup: ${money(quote.sol_usd_price)} / SOL. It does not track wallet balance changes.` : quoteError || 'Fetching current SOL/USD…'}</span><button type="button" onClick={() => setRefreshQuote((value) => value + 1)}>Refresh conversion</button></div>}
      {sizingProblem && <p role="status" className="sizing-warning">{sizingProblem}</p>}
    </>}
    {panel === 'entry' && <fieldset disabled={busy}><legend>Entry signal</legend><label>Entry signal<select value={fields.entry_style} onChange={(e) => update('entry_style', e.target.value)}>{styles.map((style) => <option key={style} value={style}>{style.replace(/_/g, ' ').replace(/-/g, ' ')}</option>)}</select></label></fieldset>}
    {sections.filter((_, index) => (panel === 'entry' && index === 0) || (panel === 'signal' && index === 1) || (panel === 'exits' && index === 2) || (panel === 'advanced' && index >= 3)).map(([title, help, keys]) => <fieldset disabled={busy} key={title}><legend>{title}</legend><p>{help}</p><div className="strategy-edit-grid">{keys.filter((key) => key in configuration).map(input)}</div></fieldset>)}
    {panel === 'exits' &&     <fieldset disabled={busy}><legend>Profit targets</legend><p>Increasing market-cap targets. Sell percentages refer to the original position and may total up to 100%.</p>{targets.map((target, index) => <div className="strategy-target-row" key={index}><label>Target {index + 1} market cap (USD)<input type="number" min="0" step="any" required value={target.cap} onChange={(e) => setTargets((rows) => rows.map((row, i) => i === index ? { ...row, cap: e.target.value } : row))} /></label><label>Target {index + 1} sell (%)<input type="number" min="0.000001" max="100" step="any" required value={target.percent} onChange={(e) => setTargets((rows) => rows.map((row, i) => i === index ? { ...row, percent: e.target.value } : row))} /></label><button type="button" aria-label={`Remove target ${index + 1}`} disabled={targets.length === 1} onClick={() => setTargets((rows) => rows.filter((_, i) => i !== index))}>Remove</button></div>)}<button type="button" onClick={() => setTargets((rows) => [...rows, { cap: '', percent: '' }])}>+ Add target</button></fieldset>}
    {panel === 'advanced' && <fieldset disabled={busy}><legend>Advanced policy</legend><label>Entry policy<select value={fields.entry_policy} onChange={(e) => update('entry_policy', e.target.value)}><option value="simulation_unverified">Simulation assumptions</option><option value="strict">Strict checks</option></select></label><p>Format: {fields.version}. Package mode: {fields.mode}. These identify the supported strategy format; trading permissions are set by the session.</p></fieldset>}
    </div>
    {error && <p className="error" role="alert">{error}</p>}<footer><button type="button" disabled={busy} onClick={cancel}>{attachedToSession ? 'Back to strategies' : 'Cancel edits'}</button><button className="primary" disabled={busy || Boolean(sizingProblem)} type="submit">{busy ? 'Saving…' : attachedToSession ? 'Save session strategy' : 'Save strategy revision'}</button></footer>
  </form>
}

const limitFields = [
  ['min_trade_sol', 'Minimum buy (SOL)'], ['max_trade_sol', 'Maximum buy (SOL)'], ['max_daily_buy_sol', 'Daily buy limit (SOL)'],
  ['max_trades_per_day', 'Maximum buys per day'], ['max_open_positions', 'Maximum open positions'],
  ['max_daily_loss_sol', 'Daily loss limit (SOL)'], ['max_daily_loss_percent', 'Daily loss limit (%)'], ['max_trade_loss_percent', 'Per-position loss limit (%)'],
] as const
export function InitialOrderLimitsEditor({ plan, sizing, save, cancel, attachedToSession = false }: { plan: SessionPlan; sizing?: AllocationSizing; attachedToSession?: boolean; save: (plan: SessionPlan) => Promise<void>; cancel: () => void }) {
  const [values, setValues] = useState(() => Object.fromEntries(limitFields.map(([key]) => [key, String(plan[key])])))
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  return <form className="saved-strategy-editor initial-limits-editor" aria-label="Edit initial order limits" onSubmit={async (event) => {
    event.preventDefault()
    if (Number(values.min_trade_sol) > Number(values.max_trade_sol) || Number(values.max_trade_sol) > Number(plan.allocation_sol) || Number(values.max_daily_buy_sol) < Number(values.max_trade_sol)) { setError('Minimum buy must not exceed maximum buy. Maximum buy must fit the allocation and daily buy limit.'); return }
    setBusy(true)
    try { await save({ ...plan, ...values, max_trades_per_day: Number(values.max_trades_per_day), max_open_positions: Number(values.max_open_positions) }) } catch (error) { setError(error instanceof Error ? error.message : 'Could not save limits.') } finally { setBusy(false) }
  }}><h3>Initial order limits</h3><p>{attachedToSession ? 'For this session strategy' : 'For the next strategy added'} · {plan.allocation_sol} SOL session allocation. Saved strategy rules also apply.</p>{sizing && <p>Maximum buy is set by the {sizing.trade_percent}% trade budget. Change it in Edit strategy → Budget &amp; trades.</p>}<div className="strategy-edit-grid">{limitFields.map(([key, label]) => <label key={key}>{label}<input required readOnly={key === 'max_trade_sol' && Boolean(sizing)} type="number" min={key.endsWith('_percent') ? '0.01' : key === 'max_trades_per_day' || key === 'max_open_positions' ? '1' : '0.000000001'} max={key.endsWith('_percent') || key === 'max_trades_per_day' ? '100' : key === 'max_open_positions' ? '25' : undefined} step={key === 'max_trades_per_day' || key === 'max_open_positions' ? '1' : 'any'} value={values[key]} onChange={(e) => setValues((old) => ({ ...old, [key]: e.target.value }))} /></label>)}</div>{error && <p className="error" role="alert">{error}</p>}<footer><button type="button" disabled={busy} onClick={cancel}>Cancel limits</button><button className="primary" type="submit" disabled={busy}>{busy ? 'Saving…' : 'Apply initial limits'}</button></footer></form>
}
