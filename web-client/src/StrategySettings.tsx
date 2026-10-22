import { useState } from 'react'
import type { StrategySessionDetail } from './SessionMonitor'

export type EditableStrategySettings = {
  entry_cap_min: number
  entry_cap_max: number
  order_usd: number
  min_curve_liquidity_usd: number
  min_amm_liquidity_usd: number
  stop_loss_percent: number
  trailing_exit_percent: number
  max_hold_hours: number
}

function initial(detail: StrategySessionDetail) {
  const plan = detail.monitor_plan
  return {
    entry_cap_min: String(plan.entry_cap_min ?? ''),
    entry_cap_max: String(plan.entry_cap_max ?? ''),
    order_usd: String(plan.order_usd ?? ''),
    min_curve_liquidity_usd: String(plan.min_curve_liquidity_usd ?? ''),
    min_amm_liquidity_usd: String(plan.min_amm_liquidity_usd ?? ''),
    stop_loss_percent: String(detail.risk_policy ? detail.risk_policy.max_trade_loss_bps / 100 : detail.exit_plan.stop_loss_percent ?? ''),
    trailing_exit_percent: String(detail.exit_plan.trailing_drawdown_percent ?? ''),
    max_hold_hours: String(detail.exit_plan.max_hold_seconds === null ? '' : detail.exit_plan.max_hold_seconds / 3600),
  }
}

export function StrategySettings({ detail, close, save }: {
  detail: StrategySessionDetail
  close: () => void
  save: (settings: EditableStrategySettings) => Promise<void>
}) {
  const [form, setForm] = useState(() => initial(detail))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const locked = Boolean(detail.tracking.enabled || detail.runs.length || detail.decisions.length || detail.positions.length)
  const update = (field: keyof typeof form, value: string) => setForm((current) => ({ ...current, [field]: value }))
  const submit = async () => {
    const settings = Object.fromEntries(Object.entries(form).map(([key, value]) => [key, Number(value)])) as EditableStrategySettings
    if (Object.values(settings).some((value) => !Number.isFinite(value) || value <= 0)) {
      setError('Every strategy setting must be a positive number.')
      return
    }
    setBusy(true)
    setError('')
    try { await save(settings); close() }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Strategy settings could not be saved.') }
    finally { setBusy(false) }
  }

  return <div className="modal-backdrop detail-layer" role="presentation" onMouseDown={busy ? undefined : close}>
    <section className="strategy-modal strategy-settings-modal" role="dialog" aria-modal="true" aria-label="Review strategy settings" onMouseDown={(event) => event.stopPropagation()}>
      <button className="close" aria-label="Close dialog" disabled={busy} onClick={close}>×</button>
      <header><small>LIVE STRATEGY SETUP</small><h2>Review and edit before monitoring</h2><p>{detail.instance.label}</p></header>
      <div className="strategy-settings-notice"><i aria-hidden="true">◎</i><span><b>Your connected wallet supplies the SOL.</b><p>Trade size stays in USD so the strategy remains comparable across SOL prices. A fresh SOL/USD rate converts it to SOL during each reviewed order.</p></span></div>
      {locked ? <div className="strategy-settings-locked"><b>These rules are locked because this session has activity</b><p>Stop monitoring and create a new session to change its decision rules without rewriting retained observations or decisions.</p></div> : <>
        <div className="strategy-settings-grid">
          <fieldset><legend>Entry market</legend><p>Only observe tokens inside this capitalization and liquidity range.</p><div>
            <label>Minimum market cap (USD)<input aria-label="Minimum entry market cap" inputMode="decimal" value={form.entry_cap_min} onChange={(event) => update('entry_cap_min', event.target.value)} /></label>
            <label>Maximum market cap (USD)<input aria-label="Maximum entry market cap" inputMode="decimal" value={form.entry_cap_max} onChange={(event) => update('entry_cap_max', event.target.value)} /></label>
            <label>Minimum curve liquidity (USD)<input aria-label="Minimum curve liquidity" inputMode="decimal" value={form.min_curve_liquidity_usd} onChange={(event) => update('min_curve_liquidity_usd', event.target.value)} /></label>
            <label>Minimum migrated-pool liquidity (USD)<input aria-label="Minimum migrated liquidity" inputMode="decimal" value={form.min_amm_liquidity_usd} onChange={(event) => update('min_amm_liquidity_usd', event.target.value)} /></label>
          </div></fieldset>
          <fieldset><legend>Trade and exit</legend><p>Set one trade size and the loss, trailing, and time exits for each position.</p><div>
            <label>Trade size (USD)<input aria-label="Trade size in USD" inputMode="decimal" value={form.order_usd} onChange={(event) => update('order_usd', event.target.value)} /></label>
            <label>Maximum loss per trade (%)<input aria-label="Maximum loss per trade" inputMode="decimal" value={form.stop_loss_percent} onChange={(event) => update('stop_loss_percent', event.target.value)} /></label>
            <label>Trailing exit (%)<input aria-label="Trailing exit" inputMode="decimal" value={form.trailing_exit_percent} onChange={(event) => update('trailing_exit_percent', event.target.value)} /></label>
            <label>Maximum hold (hours)<input aria-label="Maximum hold hours" inputMode="decimal" value={form.max_hold_hours} onChange={(event) => update('max_hold_hours', event.target.value)} /></label>
          </div></fieldset>
        </div>
        <div className="strategy-settings-summary"><span><small>SIGNAL</small><b>{detail.monitor_plan.entry_style}</b></span><span><small>OBSERVATION</small><b>{detail.monitor_plan.observation_seconds}s</b></span><span><small>EXIT STAGES</small><b>{detail.exit_plan.targets.length} retained</b></span><span><small>ORDER APPROVAL</small><b>Reviewed manually</b></span></div>
      </>}
      {error && <p className="strategy-settings-error" role="alert">{error}</p>}
      <footer><p>{locked ? 'Existing evidence remains attached to its exact strategy revision.' : 'Saving creates a new immutable local revision. Monitoring and reviewed entries remain off.'}</p><div><button disabled={busy} onClick={close}>{locked ? 'Done' : 'Cancel'}</button>{!locked && <button className="primary" disabled={busy} onClick={() => void submit()}>{busy ? 'Saving…' : 'Save strategy revision'}</button>}</div></footer>
    </section>
  </div>
}
