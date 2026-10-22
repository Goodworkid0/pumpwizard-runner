import { useEffect, useMemo, useState } from 'react'
import { api, errorText, formatSol } from './browserWallet'
import type { MainWalletAutopilotRuntime, WalletSession } from './browserWallet'
import type { MainWalletRuntimeState } from './mainWalletAutopilot'
import './main-wallet-autopilot.css'

type StrategyLimits = {
  allocation_lamports: number | string
  max_trade_lamports: number | string
  max_daily_buy_lamports: number | string
  max_trades_per_day: number
  max_open_positions: number
}

type AllowanceReview = {
  id: string
  session_id: string
  sender: string
  strategy_digest: string
  policy_revision: number
  cash_hold: string
  duration_seconds: number
  budget_lamports: number
  max_order_debit_lamports: number
  max_daily_debit_lamports: number
  max_orders: number
  max_trades_per_day: number
  max_open_positions: number
  slippage_bps: number
  exit_headroom_lamports: number
  expires_at: number
  ready: boolean
  can_activate: false
  preflight_blockers: string[]
  activation_blockers: string[]
  requested_permissions: Record<string, boolean | string>
  automation_scope: 'entries-only' | 'exits-only' | 'entries-and-exits'
}

type Allowance = {
  id: string
  session_id: string
  status: 'authorized' | 'active' | 'entry-paused' | 'locked' | 'revoked' | 'expired' | 'replaced'
  created_at: number
  expires_at: number
  detail: string
  used_lamports: number
  used_orders: number
  granted_permissions: { synthetic_signing: boolean; live_submission: boolean; automatic_entries: boolean; automatic_protective_exits: boolean }
  scope: { budget_lamports: number; max_orders: number; strategy_digest: string; automation_scope?: 'entries-only' | 'exits-only' | 'entries-and-exits' }
}

type ActivationCapability = {
  available: boolean
  mode: 'blocked' | 'synthetic'
  blockers: string[]
}

function integer(value: string, fallback: number) {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback
}

export function MainWalletAllowanceDialog({
  wallet,
  session,
  limits,
  activeRuntime,
  activateRuntime,
  pauseRuntime,
  stopRuntime,
  close,
}: {
  wallet: WalletSession
  session: { id: string; label: string; strategy_digest: string; automation_scope: 'entries-only' | 'exits-only' | 'entries-and-exits' }
  limits: StrategyLimits
  activeRuntime: (MainWalletRuntimeState & { allowanceId: string }) | null
  activateRuntime: (runtime: MainWalletAutopilotRuntime) => Promise<void>
  pauseRuntime: (allowanceId: string, paused: boolean) => Promise<void>
  stopRuntime: (allowanceId: string) => Promise<void>
  close: () => void
}) {
  const allocation = Number(limits.allocation_lamports)
  const conservativeOverhead = 20_100_000
  const [duration, setDuration] = useState('15')
  const [slippage, setSlippage] = useState('100')
  const [review, setReview] = useState<AllowanceReview | null>(null)
  const [allowances, setAllowances] = useState<Allowance[]>([])
  const [activation, setActivation] = useState<ActivationCapability>({ available: false, mode: 'blocked', blockers: [] })
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const phrase = `ALLOW ${session.id.slice(0, 8).toUpperCase()}`
  const plan = useMemo(() => ({
    duration_seconds: integer(duration, 15) * 60,
    budget_lamports: allocation,
    max_order_debit_lamports: Number(limits.max_trade_lamports) + conservativeOverhead,
    max_daily_debit_lamports: Math.min(
      allocation,
      Number(limits.max_daily_buy_lamports) + limits.max_trades_per_day * conservativeOverhead,
    ),
    max_orders: limits.max_trades_per_day,
    max_trades_per_day: limits.max_trades_per_day,
    max_open_positions: limits.max_open_positions,
    slippage_bps: integer(slippage, 100),
  }), [allocation, duration, limits, slippage])

  useEffect(() => {
    const query = new URLSearchParams({
      sender: wallet.address,
      network: wallet.network,
      session_id: session.id,
    })
    api<{ allowances: Allowance[]; activation: ActivationCapability }>(`/api/v1/main-wallet-autopilot?${query}`)
      .then((result) => { setAllowances(result.allowances); setActivation(result.activation) })
      .catch((reason) => setError(errorText(reason)))
  }, [activeRuntime?.order_id, activeRuntime?.phase, session.id, wallet.address, wallet.network])

  const prepare = async () => {
    setBusy(true)
    setError('')
    setConfirmation('')
    try {
      const result = await api<{ review: AllowanceReview }>(
        '/api/v1/main-wallet-autopilot/review',
        {
          sender: wallet.address,
          network: wallet.network,
          session_id: session.id,
          wallet_kind: wallet.kind,
          plan,
        },
      )
      setReview(result.review)
    } catch (reason) {
      setError(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  const authorize = async () => {
    if (!review || confirmation !== phrase) return
    setBusy(true)
    setError('')
    try {
      const result = await api<{ allowance: Allowance }>(
        '/api/v1/main-wallet-autopilot/authorize',
        {
          sender: wallet.address,
          network: wallet.network,
          session_id: session.id,
          review_id: review.id,
          confirmation,
        },
      )
      setAllowances((current) => [result.allowance, ...current.filter((item) => item.id !== result.allowance.id)])
      setReview(null)
      setConfirmation('')
    } catch (reason) {
      setError(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (allowance: Allowance) => {
    setBusy(true)
    setError('')
    try {
      await api('/api/v1/main-wallet-autopilot/revoke', {
        sender: wallet.address,
        network: wallet.network,
        allowance_id: allowance.id,
      })
      setAllowances((current) => current.map((item) => item.id === allowance.id
        ? { ...item, status: 'revoked', detail: 'Allowance revoked; signing runtime removed' }
        : item))
    } catch (reason) {
      setError(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  const activate = async (allowance: Allowance) => {
    setBusy(true)
    setError('')
    let runtime: MainWalletAutopilotRuntime | null = null
    try {
      const result = await api<{ runtime: MainWalletAutopilotRuntime }>(
        '/api/v1/main-wallet-autopilot/activate',
        {
          sender: wallet.address,
          network: wallet.network,
          allowance_id: allowance.id,
          wallet_kind: wallet.kind,
        },
      )
      runtime = result.runtime
      await activateRuntime(runtime)
      setAllowances((current) => current.map((item) => item.id === allowance.id
        ? { ...item, status: 'active', detail: 'Synthetic browser runtime active' }
        : item))
    } catch (reason) {
      if (runtime) {
        await api('/api/v1/main-wallet-autopilot/lock', {
          sender: wallet.address,
          network: wallet.network,
          allowance_id: runtime.allowance_id,
          runtime_token: runtime.runtime_token,
        }).catch(() => undefined)
      }
      setError(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  const pause = async (allowance: Allowance, paused: boolean) => {
    setBusy(true)
    setError('')
    try {
      await pauseRuntime(allowance.id, paused)
      setAllowances((current) => current.map((item) => item.id === allowance.id
        ? { ...item, status: paused ? 'entry-paused' : 'active', detail: paused ? 'Entries paused; eligible exits remain available' : 'Synthetic browser runtime active' }
        : item))
    } catch (reason) {
      setError(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  const stop = async (allowance: Allowance) => {
    setBusy(true)
    setError('')
    try {
      await stopRuntime(allowance.id)
      setAllowances((current) => current.map((item) => item.id === allowance.id
        ? { ...item, status: 'locked', detail: 'Browser runtime stopped; create a fresh allowance to restart' }
        : item))
    } catch (reason) {
      setError(errorText(reason))
    } finally {
      setBusy(false)
    }
  }

  return <div className="modal-backdrop detail-layer" role="presentation" onMouseDown={busy ? undefined : close}>
    <section className="strategy-modal main-wallet-allowance" role="dialog" aria-modal="true" aria-label="Main-wallet Autopilot allowance" onMouseDown={(event) => event.stopPropagation()}>
      <button className="close" aria-label="Close allowance review" onClick={close} disabled={busy}>×</button>
      <small>MAIN-WALLET AUTOPILOT · {session.automation_scope === 'entries-only' ? 'AUTOMATIC ENTRIES · MANUAL EXITS' : session.automation_scope === 'exits-only' ? 'AUTOMATIC EXITS' : 'ENTRIES + EXITS'}</small>
      <h2>Bound this session before it can sign</h2>
      <p>Authorization is separate from session creation and activation. The generated wallet key stays in this browser tab; refresh, lock, disconnect, expiry, revocation, or a Runner restart stops new signing.</p>

      <div className="allowance-identity"><span><small>SESSION</small><b>{session.label}</b><code>{session.id}</code></span><span><small>MAIN WALLET</small><b>{wallet.name}</b><code>{wallet.address}</code></span></div>
      {wallet.kind !== 'local' && <p className="error" role="alert">Extension wallets cannot provide unattended signing. Keep this session interactive or use a backed-up PumpWizard Wallet.</p>}

      <div className="allowance-limits">
        <label>Duration (minutes)<input inputMode="numeric" value={duration} onChange={(event) => { setDuration(event.target.value); setReview(null) }} /></label>
        <label>Maximum slippage (bps)<input inputMode="numeric" value={slippage} onChange={(event) => { setSlippage(event.target.value); setReview(null) }} /></label>
        <span><small>TOTAL SESSION BUDGET</small><b>{formatSol(allocation)}</b></span>
        <span><small>MAXIMUM FEE-INCLUSIVE ORDER</small><b>{formatSol(plan.max_order_debit_lamports)}</b></span>
        <span><small>UTC-DAY FEE-INCLUSIVE BUYS</small><b>{formatSol(plan.max_daily_debit_lamports)}</b></span>
        <span><small>POSITIONS / DAILY TRADES</small><b>{plan.max_open_positions} / {plan.max_trades_per_day}</b></span>
      </div>

      {!review && <section className="allowance-callout"><b>Review only—no signing permission</b><p>The server will bind the exact strategy digest, wallet and session policy revision, current cash reservation, routes, budget, expiry and requested actions.</p><button className="primary" disabled={busy || wallet.kind !== 'local'} onClick={() => void prepare()}>{busy ? 'Reviewing…' : 'Review current allowance'}</button></section>}

      {review && <section className="allowance-review">
        <header><span><small>REVIEW {review.ready ? 'READY' : 'BLOCKED'}</small><h3>Exact durable scope</h3></span><em>Expires {new Date(review.expires_at * 1000).toLocaleTimeString()}</em></header>
        <dl><div><dt>Strategy digest</dt><dd><code>{review.strategy_digest}</code></dd></div><div><dt>Policy revision</dt><dd>{review.policy_revision}</dd></div><div><dt>Existing cash hold</dt><dd><code>{review.cash_hold}</code></dd></div><div><dt>Exit headroom</dt><dd>{formatSol(review.exit_headroom_lamports)}</dd></div><div><dt>Order count</dt><dd>{review.max_orders} total · {review.max_trades_per_day}/UTC day</dd></div><div><dt>Activation</dt><dd>Separate and currently blocked</dd></div></dl>
        {review.preflight_blockers.length > 0 && <ul className="allowance-blockers">{review.preflight_blockers.map((item) => <li key={item}>{item}</li>)}</ul>}
        <div className="allowance-live-block"><b>{activation.available ? 'Synthetic acceptance runtime available' : 'Live activation remains blocked'}</b>{(activation.available ? ['This test-only runtime uses the same exact-ticket browser signer; it does not remove the production funded-acceptance gate.'] : review.activation_blockers).map((item) => <span key={item}>{item}</span>)}</div>
        {review.ready && <label className="allowance-confirm">Type <code>{phrase}</code> to authorize this inactive allowance<input value={confirmation} autoComplete="off" onChange={(event) => setConfirmation(event.target.value.toUpperCase())} /></label>}
        <div className="modal-actions"><button disabled={busy} onClick={() => { setReview(null); setConfirmation('') }}>Change limits</button><button className="primary" disabled={busy || !review.ready || confirmation !== phrase} onClick={() => void authorize()}>{busy ? 'Authorizing…' : 'Authorize inactive allowance'}</button></div>
      </section>}

      {allowances.length > 0 && <section className="allowance-history"><small>RECENT ALLOWANCES</small>{allowances.slice(0, 5).map((item) => {
        const runtime = activeRuntime?.allowanceId === item.id ? activeRuntime : null
        return <article key={item.id}><span><b>{item.status.replace('-', ' ')}</b><small>{runtime?.detail || item.detail}</small></span><span><b>{formatSol(item.used_lamports)} used</b><small>{item.used_orders} / {item.scope.max_orders} orders · {(item.scope.automation_scope || 'entries-and-exits').replace(/-/g, ' ')}</small></span><div className="allowance-actions">{item.status === 'authorized' && activation.available && <button className="primary" disabled={busy || !wallet.signingAvailable} onClick={() => void activate(item)}>Start synthetic Autopilot</button>}{runtime && runtime.phase !== 'locked' && item.granted_permissions.automatic_entries && <button disabled={busy} onClick={() => void pause(item, item.status !== 'entry-paused')}>{item.status === 'entry-paused' ? 'Resume entries' : 'Pause entries'}</button>}{runtime && runtime.phase !== 'locked' && <button disabled={busy} onClick={() => void stop(item)}>Stop</button>}{['authorized', 'active', 'entry-paused'].includes(item.status) && <button disabled={busy} onClick={() => void revoke(item)}>Revoke</button>}</div></article>
      })}</section>}
      {error && <p className="error" role="alert">{error}</p>}
      <footer><p>Prices and retained valuations can block entries, but they are not executable loss guarantees. Signed or submitted transactions still reconcile after signing stops.</p><button onClick={close} disabled={busy}>Done</button></footer>
    </section>
  </div>
}
