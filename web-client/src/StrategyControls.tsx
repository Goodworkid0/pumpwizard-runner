import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, errorText, formatSol } from './browserWallet'
import type { WalletSession } from './browserWallet'
import './strategy-controls.css'

type Policy = {
  allocation_lamports: number
  buys_enabled: boolean
  sells_enabled: boolean
  min_trade_lamports: number
  max_trade_lamports: number
  max_daily_buy_lamports: number
  max_trades_per_day: number
  max_daily_loss_lamports: number
  max_daily_loss_bps: number
  max_trade_loss_bps: number
  max_open_positions: number
}
type Metrics = {
  utc_day: string
  daily_trades: number
  daily_buy_lamports: number
  daily_loss_lamports: number
  open_positions: number
  open_cost_lamports: number
  attributed_open_positions: number
  attributed_open_cost_lamports: number
  allocated_lamports: number
  hold_id: string | null
}
type Control = {
  instance: { id: string; label: string; status: string; entry_mode: string; exit_mode: string }
  policy: Policy | null
  metrics: Metrics
  entry_blocks: string[]
}
type Overview = {
  sender: string
  network: string
  balance_lamports: string
  allocated_lamports: string
  unallocated_lamports: string
  controls: Control[]
}
type Form = {
  allocation_sol: string
  buys_enabled: boolean
  sells_enabled: boolean
  min_trade_sol: string
  max_trade_sol: string
  max_daily_buy_sol: string
  max_trades_per_day: string
  max_daily_loss_sol: string
  max_daily_loss_percent: string
  max_trade_loss_percent: string
  max_open_positions: string
}

const EMPTY: Form = {
  allocation_sol: '', buys_enabled: false, sells_enabled: true,
  min_trade_sol: '', max_trade_sol: '', max_daily_buy_sol: '', max_trades_per_day: '5',
  max_daily_loss_sol: '', max_daily_loss_percent: '10', max_trade_loss_percent: '20',
  max_open_positions: '3',
}

function solValue(lamports: number | string) {
  const value = BigInt(lamports)
  const whole = value / 1_000_000_000n
  const fraction = (value % 1_000_000_000n).toString().padStart(9, '0').replace(/0+$/, '')
  return `${whole}${fraction ? `.${fraction}` : ''}`
}

function toForm(policy: Policy | null): Form {
  if (!policy) return { ...EMPTY }
  return {
    allocation_sol: solValue(policy.allocation_lamports),
    buys_enabled: policy.buys_enabled,
    sells_enabled: policy.sells_enabled,
    min_trade_sol: solValue(policy.min_trade_lamports),
    max_trade_sol: solValue(policy.max_trade_lamports),
    max_daily_buy_sol: solValue(policy.max_daily_buy_lamports),
    max_trades_per_day: String(policy.max_trades_per_day),
    max_daily_loss_sol: solValue(policy.max_daily_loss_lamports),
    max_daily_loss_percent: String(policy.max_daily_loss_bps / 100),
    max_trade_loss_percent: String(policy.max_trade_loss_bps / 100),
    max_open_positions: String(policy.max_open_positions),
  }
}

export function StrategyControls({ session, close }: { session: WalletSession; close: () => void }) {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [selected, setSelected] = useState<Control | null>(null)
  const [form, setForm] = useState<Form>({ ...EMPTY })
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('Loading main-wallet allocations…')

  const refresh = useCallback(async () => {
    const query = new URLSearchParams({ sender: session.address, network: session.network })
    const result = await api<Overview>(`/api/v1/strategy-controls?${query}`)
    setOverview(result)
    setSelected((current) => current ? result.controls.find((item) => item.instance.id === current.instance.id) || null : null)
    setMessage('These controls apply only to reviewed strategy orders and are enforced again before agent order signing.')
  }, [session.address, session.network])

  useEffect(() => { refresh().catch((error) => setMessage(errorText(error))) }, [refresh])

  const choose = (control: Control) => {
    setSelected(control)
    setForm(toForm(control.policy))
    setMessage(control.policy ? 'Edit the existing limits. Saving invalidates stale balance reviews.' : 'Set every limit to create this strategy allocation.')
  }
  const update = (field: keyof Form, value: string | boolean) => setForm((current) => ({ ...current, [field]: value }))
  const complete = useMemo(() => Object.entries(form).every(([, value]) => typeof value === 'boolean' || value.trim()), [form])
  const save = async () => {
    if (!selected || !complete) return
    setBusy(true)
    setMessage('Checking the live main-wallet balance and reserving this strategy allocation…')
    try {
      await api(`/api/v1/strategy-controls/${encodeURIComponent(selected.instance.id)}`, {
        sender: session.address,
        network: session.network,
        ...form,
        max_trades_per_day: Number(form.max_trades_per_day),
        max_open_positions: Number(form.max_open_positions),
      })
      await refresh()
      setSelected(null)
      setMessage('Strategy allocation and limits saved. Monitoring and automatic submission remain separate controls.')
    } catch (error) { setMessage(errorText(error)) } finally { setBusy(false) }
  }

  return <div className="modal-backdrop" role="presentation" onMouseDown={busy ? undefined : close}><section className="strategy-modal control-modal" role="dialog" aria-modal="true" aria-label="Reviewed order safety" onMouseDown={(event) => event.stopPropagation()}><button className="close" aria-label="Close dialog" onClick={close} disabled={busy}>×</button><small>ADVANCED · REVIEWED STRATEGY ORDERS</small><h2>Reviewed order safety</h2>
    {overview && <div className="allocation-summary"><div><span>Main wallet</span><b>{formatSol(overview.balance_lamports)}</b><code>{session.address}</code></div><div><span>Assigned</span><b>{formatSol(overview.allocated_lamports)}</b><small>reserved across strategies</small></div><div><span>Unassigned</span><b>{formatSol(overview.unallocated_lamports)}</b><small>available to allocate</small></div></div>}
    <p className="control-explainer">Your connected wallet supplies the SOL. These optional reservations matter only when a strategy may prepare reviewed orders: they prevent concurrent orders from committing the same balance. They do not create addresses or transfer funds.</p>
    {!selected ? <div className="control-list">{overview?.controls.length === 0 ? <p className="empty">Create a strategy session before assigning wallet funds.</p> : overview?.controls.map((control) => <article key={control.instance.id}><div><h3>{control.instance.label}</h3><span>{control.instance.status} · {control.policy ? 'limits configured' : 'not allocated'}</span></div><dl><div><dt>Remaining allocation</dt><dd>{formatSol(control.metrics.allocated_lamports)}</dd></div><div><dt>Today</dt><dd>{control.metrics.daily_trades} trades · {formatSol(control.metrics.daily_buy_lamports)} buys</dd></div><div><dt>Daily loss</dt><dd>{formatSol(control.metrics.daily_loss_lamports)}</dd></div><div><dt>Managed positions</dt><dd>{control.metrics.open_positions}{control.policy ? ` / ${control.policy.max_open_positions}` : ''}{control.metrics.attributed_open_positions > control.metrics.open_positions ? ` · ${control.metrics.attributed_open_positions - control.metrics.open_positions} user-managed, still attributed` : ''}</dd></div></dl>{control.entry_blocks.length > 0 && <p className="control-block">{control.entry_blocks.join(' · ')}</p>}<button className={control.policy ? '' : 'primary'} onClick={() => choose(control)}>{control.policy ? 'Edit limits' : 'Assign funds'}</button></article>)}</div> : <div className="control-editor"><button className="back-control" onClick={() => setSelected(null)} disabled={busy}>← All strategies</button><h3>{selected.instance.label}</h3><div className="toggle-grid"><label><input type="checkbox" checked={form.buys_enabled} onChange={(event) => update('buys_enabled', event.target.checked)} /> Allow reviewed buys</label><label><input type="checkbox" checked={form.sells_enabled} onChange={(event) => update('sells_enabled', event.target.checked)} /> Allow managed strategy sells</label></div><div className="control-fields"><label>Assigned SOL<input inputMode="decimal" value={form.allocation_sol} onChange={(event) => update('allocation_sol', event.target.value)} placeholder="0.20" /></label><label>Minimum buy (SOL)<input inputMode="decimal" value={form.min_trade_sol} onChange={(event) => update('min_trade_sol', event.target.value)} placeholder="0.005" /></label><label>Maximum buy (SOL)<input inputMode="decimal" value={form.max_trade_sol} onChange={(event) => update('max_trade_sol', event.target.value)} placeholder="0.02" /></label><label>Maximum buys per day (SOL)<input inputMode="decimal" value={form.max_daily_buy_sol} onChange={(event) => update('max_daily_buy_sol', event.target.value)} placeholder="0.05" /></label><label>Maximum trades per day<input inputMode="numeric" value={form.max_trades_per_day} onChange={(event) => update('max_trades_per_day', event.target.value)} /></label><label>Maximum open positions<input inputMode="numeric" value={form.max_open_positions} onChange={(event) => update('max_open_positions', event.target.value)} /></label><label>Daily loss cap (SOL)<input inputMode="decimal" value={form.max_daily_loss_sol} onChange={(event) => update('max_daily_loss_sol', event.target.value)} placeholder="0.02" /></label><label>Daily loss cap (%)<input inputMode="decimal" value={form.max_daily_loss_percent} onChange={(event) => update('max_daily_loss_percent', event.target.value)} /></label><label>Maximum loss per trade (%)<input inputMode="decimal" value={form.max_trade_loss_percent} onChange={(event) => update('max_trade_loss_percent', event.target.value)} /></label></div><p className="control-note">The effective daily stop is whichever is reached first: the SOL cap or the percentage of assigned funds. Maximum loss per trade supplies a stricter executable-net stop for strategy positions. The existing reviewed handoff can release a managed position for manual exit; it remains purchase-attributed but stops consuming this strategy’s managed position and exposure limits.</p><div className="modal-actions"><button onClick={() => setSelected(null)} disabled={busy}>Cancel</button><button className="primary" onClick={save} disabled={busy || !complete}>Save allocation & limits</button></div></div>}
    <p className="wallet-status" aria-live="polite">{busy ? 'Working… ' : ''}{message}</p>
  </section></div>
}
