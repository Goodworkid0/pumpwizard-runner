import { useState } from 'react'
import { SessionObservations } from './SessionObservations'
import { EvidenceAssistantPanel } from './EvidenceAssistantPanel'
import type { PortfolioSession } from './PortfolioWorkspace'

const DECISION_EVIDENCE_QUESTIONS = ['why_decision', 'missing_evidence'] as const
const HOLDING_EVIDENCE_QUESTIONS = ['valuation_reliability', 'missing_evidence'] as const

type Observation = {
  mint: string
  symbol: string
  observed_at: number
  route: string
  market_cap_usd: number
  price_usd: number
  liquidity_usd: number
}

export type StrategySessionDetail = PortfolioSession & {
  observations: Observation[]
  decisions: { decision_key: string; at: number; kind: string; mint: string; reason: string }[]
  outcomes: {
    decision_at: number
    decision_kind: string
    decision_reason: string
    mint: string
    horizon_seconds: number
    status: 'scored' | 'unknown'
    coverage: string
    decision_return_fraction?: number
    maximum_favorable_fraction?: number
    maximum_adverse_fraction?: number
    sample_count?: number
  }[]
  runs: { started_at: number; finished_at: number; status: string; observations: number; decisions: number; detail: string }[]
  monitor_lease: { owner: string; expires_at: number } | null
  management: { removal_blocks: string[]; allocation_lamports: string; workspace_allocation_lamports?: string }
}

function shortMint(value: string) {
  return value ? `${value.slice(0, 6)}…${value.slice(-4)}` : 'Unknown token'
}

function money(value: number | string | null, digits = 2) {
  const amount = Number(value)
  return Number.isFinite(amount) ? `$${amount.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: Math.max(digits, amount < 1 ? 8 : digits) })}` : '—'
}

function compactMoney(value: number | null) {
  if (value === null || !Number.isFinite(value)) return '—'
  return `$${value.toLocaleString(undefined, { notation: value >= 1000 ? 'compact' : 'standard', maximumFractionDigits: 1 })}`
}

function duration(value: number | null) {
  if (value === null || !Number.isFinite(value)) return '—'
  if (value >= 86400 && value % 86400 === 0) return `${value / 86400}d`
  if (value >= 3600 && value % 3600 === 0) return `${value / 3600}h`
  if (value >= 60) return `${Math.round(value / 60)}m`
  return `${value}s`
}

function ago(value: number) {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - value))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

function decisionLabel(value: string) {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (character) => character.toUpperCase())
}

function goalLabel(value: StrategySessionDetail['brief']['goal']) {
  return value === 'observe-market' ? 'Observe the market' : value === 'validate-strategy' ? 'Validate the strategy' : 'Build a dataset'
}

function signedPercent(value: number | undefined) {
  if (value === undefined || !Number.isFinite(value)) return 'Unknown'
  return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(1)}%`
}

export function SessionMonitor({ detail, busy, error, close, toggle, scan, settings, orders, review, paper, allowance, scopeQuery }: {
  scopeQuery?: string
  detail: StrategySessionDetail
  busy: boolean
  error: string
  close: () => void
  toggle: () => void
  scan: () => void
  settings: () => void
  orders: () => void
  review?: () => void
  paper?: () => void
  allowance?: () => void
}) {
  const [selectedDecision, setSelectedDecision] = useState('')
  const [selectedHolding, setSelectedHolding] = useState('')
  const running = Boolean(detail.tracking.enabled && detail.instance.status === 'monitoring' && !detail.tracking.fault)
  const plan = detail.monitor_plan
  const policyLoss = detail.risk_policy ? detail.risk_policy.max_trade_loss_bps / 100 : null
  const strategyLoss = detail.exit_plan.stop_loss_percent
  const displayedLoss = policyLoss ?? strategyLoss
  const lastRun = detail.health.last_run === 'never' ? null : detail.health.last_run
  const leaseActive = Boolean(detail.monitor_lease && detail.monitor_lease.expires_at > Date.now() / 1000)
  const waiting = running && detail.state.label === 'Waiting for monitor'
  const paperAllowed = detail.paper_research_allowed === true

  return <div className="modal-backdrop session-monitor-backdrop" role="presentation" onMouseDown={busy ? undefined : close}>
    <section className="strategy-modal session-monitor-modal" role="dialog" aria-modal="true" aria-label="Session monitor" onMouseDown={(event) => event.stopPropagation()}>
      <header className="session-monitor-header">
        <div><small>SESSION MONITOR</small><h2>{detail.instance.label}</h2><p>{plan.entry_style} · {compactMoney(plan.entry_cap_min)}–{compactMoney(plan.entry_cap_max)} market cap</p></div>
        <span className={`session-run-state ${running ? 'running' : detail.tracking.fault ? 'fault' : 'stopped'}`}><i />{waiting ? 'Waiting for monitor' : running ? 'Running' : detail.tracking.fault ? 'Needs attention' : 'Stopped'}</span>
        <div className="session-header-actions"><button className={running ? 'danger' : 'primary'} disabled={busy} onClick={toggle}>{busy ? 'Working…' : waiting ? 'Start scanning' : running ? 'Stop monitor' : detail.tracking.fault ? 'Retry monitor' : 'Run monitor'}</button><button disabled={busy || !running} onClick={scan}>Scan now</button></div>
        <button className="close" aria-label="Close dialog" disabled={busy} onClick={close}>×</button>
      </header>

      <div className="session-monitor-stats">
        <span><small>MONITOR</small><b>{waiting ? 'Waiting' : running ? 'Active' : 'Inactive'}</b><em>{waiting ? 'Scanner needs to start' : running ? `Every ${plan.scan_interval_seconds}s` : 'No scheduled scans'}</em></span>
        <span><small>OBSERVING</small><b>{compactMoney(plan.entry_cap_min)}–{compactMoney(plan.entry_cap_max)}</b><em>{plan.entry_style} entries</em></span>
        <span><small>MAX LOSS / POSITION</small><b>{displayedLoss === null ? 'Not set' : `${displayedLoss}%`}</b><em>{policyLoss !== null ? `Wallet limit · strategy exit ${strategyLoss ?? '—'}%` : 'Strategy exit · wallet limit not set'}</em></span>
        <span><small>CURRENT POSITIONS</small><b>{detail.positions.length}</b><em>{detail.positions.length ? 'Open strategy holdings' : 'No open positions'}</em></span>
      </div>

      <div className={`session-state-callout ${detail.state.label.toLocaleLowerCase().replace(/\s+/g, '-')}`}><i aria-hidden="true">{running ? '●' : '○'}</i><span><b>{detail.state.label}</b><p>{detail.state.reason} Recommended action: {detail.state.action}.</p></span><em>{leaseActive ? 'Local scheduler connected' : 'No active scheduler lease'}</em></div>
      {error && <p className="session-monitor-error" role="alert">{error}</p>}

      <div className="session-monitor-body">
        <div className="session-monitor-primary">
          <SessionObservations session={detail} />
          <section className="session-observations"><header><span><small>LIVE INPUT</small><h3>What this strategy is observing</h3></span><em>{detail.observations.length} latest tokens</em></header>
            {detail.observations.length === 0 ? <div className="session-empty"><b>No retained token observations yet</b><p>Run this monitor, then use Scan now to collect the first supported market batch.</p></div> : <div className="session-observation-list">{detail.observations.slice(0, 8).map((observation) => <article key={observation.mint}><span><b>{observation.symbol || shortMint(observation.mint)}</b><code title={observation.mint}>{shortMint(observation.mint)}</code></span><span><small>MARKET CAP</small><b>{money(observation.market_cap_usd, 0)}</b></span><span><small>PRICE</small><b>{money(observation.price_usd)}</b></span><span><small>LIQUIDITY</small><b>{money(observation.liquidity_usd, 0)}</b></span><time>{ago(observation.observed_at)}</time></article>)}</div>}
          </section>

          <section className="session-runs"><header><span><small>SCAN HISTORY</small><h3>Recent monitoring runs</h3></span><em>{lastRun ? `Last ${ago(lastRun.finished_at)}` : 'Never scanned'}</em></header>
            {detail.runs.length === 0 ? <div className="session-empty compact"><b>No completed run retained</b><p>The first completed or failed scan will appear here.</p></div> : <div className="session-run-list">{detail.runs.slice(0, 6).map((run) => <article key={`${run.finished_at}-${run.status}`}><i className={run.status} /><time>{ago(run.finished_at)}</time><b>{run.status}</b><span>{run.observations} observations · {run.decisions} decisions</span><em>{run.detail || 'Completed without a monitor warning'}</em></article>)}</div>}
          </section>
        </div>

        <aside className="session-monitor-secondary">
          <section className="session-brief-summary"><small>SESSION BRIEF</small><h3>{goalLabel(detail.brief.goal)}</h3><p>{detail.brief.intent}</p><dl>
            <div><dt>Mode</dt><dd>{detail.brief.mode === 'trade-autopilot' ? detail.brief.automation_scope === 'entries-only' ? 'Trade · Automatic entries' : detail.brief.automation_scope === 'exits-only' ? 'Trade · Automatic exits' : 'Trade · Full Autopilot' : detail.brief.mode === 'trade-interactive' ? 'Trade · Interactive' : 'Monitoring'}</dd></div>{paperAllowed && <div><dt>Paper budget</dt><dd>{money(detail.brief.paper_budget_usd, 0)}</dd></div>}
            <div><dt>Loss review</dt><dd>{money(detail.brief.max_session_loss_usd, 0)}</dd></div><div><dt>Review target</dt><dd>{new Date(detail.brief.review_at * 1000).toLocaleString()}</dd></div>
            <div><dt>Order at creation</dt><dd>{money(detail.brief.paper_order_usd, 0)}</dd></div><div><dt>Maximum positions</dt><dd>{detail.brief.max_positions}</dd></div>
            <div><dt>Wallet allocation</dt><dd>{detail.brief.session_mode === 'monitoring' ? 'None' : `${(Number(detail.management.workspace_allocation_lamports ?? detail.management.allocation_lamports) / 1_000_000_000).toLocaleString(undefined, { maximumFractionDigits: 9 })} SOL shared by session`}</dd></div><div><dt>Preference</dt><dd>{detail.brief.preferences ? `${detail.brief.preferences.risk_tolerance} · ${detail.brief.preferences.reward_risk_ratio}:1 · ${detail.brief.preferences.time_horizon}` : 'Legacy session'}</dd></div>
            {detail.brief.execution_style && <div><dt>Profit handling</dt><dd>{detail.brief.profit_management?.replace(/-/g, ' ')}</dd></div>}{detail.brief.mode === 'trade-autopilot' && <div><dt>Signing custody</dt><dd>{detail.brief.permissions?.custody === 'main-wallet-service' ? 'Background service · survives Dashboard closure' : 'Browser tab · stops on closure'}</dd></div>}<div><dt>Order authority</dt><dd>{detail.brief.permissions?.automatic_entries ? detail.brief.permissions.automatic_protective_exits ? 'Automatic entries + exits · activation required' : 'Automatic entries · manual exits · activation required' : detail.brief.permissions?.automatic_protective_exits ? 'Automatic exits only · activation required' : detail.brief.permissions?.user_signature_each_order ? 'User signs every order' : 'None'}</dd></div>
          </dl><em>{detail.brief.limits}</em></section>
          <section className="session-plan"><small>CURRENT STRATEGY</small><h3>Monitoring plan</h3><dl>
            <div><dt>Entry signal</dt><dd>{plan.entry_style}</dd></div><div><dt>Market-cap range</dt><dd>{compactMoney(plan.entry_cap_min)}–{compactMoney(plan.entry_cap_max)}</dd></div>
            <div><dt>Observation window</dt><dd>{duration(plan.observation_seconds)}</dd></div><div><dt>Required samples</dt><dd>{plan.minimum_samples ?? '—'}</dd></div>
            <div><dt>Price changes</dt><dd>{plan.minimum_price_changes ?? '—'}</dd></div><div><dt>Trade size</dt><dd>{money(plan.order_usd, 0)} USD</dd></div>
            <div><dt>Curve liquidity</dt><dd>{money(plan.min_curve_liquidity_usd, 0)} min</dd></div><div><dt>Migrated liquidity</dt><dd>{money(plan.min_amm_liquidity_usd, 0)} min</dd></div>
            <div><dt>Stop loss</dt><dd>{detail.exit_plan.stop_loss_percent === null ? '—' : `${detail.exit_plan.stop_loss_percent}%`}</dd></div><div><dt>Trailing exit</dt><dd>{detail.exit_plan.trailing_drawdown_percent === null ? '—' : `${detail.exit_plan.trailing_drawdown_percent}%`}</dd></div>
            <div><dt>Maximum hold</dt><dd>{duration(detail.exit_plan.max_hold_seconds)}</dd></div><div><dt>Scan cadence</dt><dd>{plan.scan_interval_seconds}s</dd></div>
          </dl></section>

          <section className="session-positions"><header><span><small>LIVE HOLDINGS</small><h3>Current positions</h3></span><em>{detail.positions.length}</em></header>{detail.positions.length === 0 ? <p>No positions are currently assigned to this strategy.</p> : detail.positions.slice(0, 5).map((position) => <article key={position.id}><span><b>{position.symbol || shortMint(position.mint)}</b><code title={position.mint}>{shortMint(position.mint)}</code></span><span><small>QUANTITY</small><b>{Number(position.quantity).toLocaleString(undefined, { maximumFractionDigits: 6 })}</b></span><span><small>REMAINING BASIS</small><b>{position.remaining_cost_usd === null ? '—' : money(position.remaining_cost_usd)}</b></span><button aria-pressed={selectedHolding === position.id} onClick={() => setSelectedHolding((current) => current === position.id ? '' : position.id)}>{selectedHolding === position.id ? 'Hide explanation' : 'Explain valuation'}</button></article>)}</section>
          {selectedHolding && <EvidenceAssistantPanel scopeQuery={scopeQuery} selector={{ schema: 'pumpwizard.evidence-selector/v1', kind: 'holding', id: selectedHolding, parent_id: detail.instance.id }} questions={HOLDING_EVIDENCE_QUESTIONS} title="Explain this retained holding" />}

          <section className="session-decisions"><header><span><small>STRATEGY OUTPUT</small><h3>Recent decisions</h3></span><em>{detail.decisions.length}</em></header>{detail.decisions.length === 0 ? <p>No decisions retained. The observation window may still be building.</p> : detail.decisions.slice(0, 5).map((decision) => <article key={decision.decision_key}><span><b>{decisionLabel(decision.kind)}</b><time>{ago(decision.at)}</time></span><code title={decision.mint}>{shortMint(decision.mint)}</code><p>{decision.reason}</p><button aria-pressed={selectedDecision === decision.decision_key} onClick={() => setSelectedDecision((current) => current === decision.decision_key ? '' : decision.decision_key)}>{selectedDecision === decision.decision_key ? 'Hide explanation' : 'Explain evidence'}</button></article>)}</section>
          {selectedDecision && <EvidenceAssistantPanel scopeQuery={scopeQuery} selector={{ schema: 'pumpwizard.evidence-selector/v1', kind: 'decision', id: selectedDecision, parent_id: detail.instance.id }} questions={DECISION_EVIDENCE_QUESTIONS} title="Explain this retained decision" />}

          <section className="session-outcomes"><header><span><small>LEARNING EVIDENCE</small><h3>Decision outcomes</h3></span><em>{detail.outcomes.length}</em></header>{detail.outcomes.length === 0 ? <p>Outcomes appear after a fixed horizon plus a two-minute coverage window.</p> : detail.outcomes.slice(0, 8).map((outcome) => <article key={`${outcome.decision_at}-${outcome.horizon_seconds}-${outcome.mint}`}><span><b>{decisionLabel(outcome.decision_kind)}</b><time>{duration(outcome.horizon_seconds)} later</time></span><span><code title={outcome.mint}>{shortMint(outcome.mint)}</code><strong className={outcome.status === 'scored' && (outcome.decision_return_fraction || 0) >= 0 ? 'positive' : outcome.status === 'scored' ? 'negative' : ''}>{signedPercent(outcome.decision_return_fraction)}</strong></span><p>{outcome.status === 'scored' ? `${outcome.sample_count} continuous samples · favorable ${signedPercent(outcome.maximum_favorable_fraction)} · adverse ${signedPercent(outcome.maximum_adverse_fraction)}` : `Not scored · ${outcome.coverage.replace(/_/g, ' ')}`}</p></article>)}</section>
        </aside>
      </div>

      <footer className="session-monitor-footer"><p>{detail.positions.length ? 'Stop monitor blocks new entries; current positions remain observed for exit safety.' : detail.brief.mode === 'trade-autopilot' ? detail.brief.permissions?.custody === 'main-wallet-service' ? 'Auto trading uses your encrypted local wallet, approved session limits and a fresh service unlock. Once started, the service scans and trades while this computer stays awake.' : 'Browser Autopilot intent is recorded, but execution remains disabled until an exact main-wallet allowance is authorized and live activation passes a separate release gate.' : detail.brief.mode === 'trade-interactive' ? 'The session may prepare reviewed orders; your connected-wallet signature is required for every submission.' : 'This monitoring session retains evidence only and has no wallet allocation or order authority.'}</p><div>{paper && paperAllowed && <button onClick={paper}>Paper trading</button>}{review && <button onClick={review}>Review session evidence</button>}{allowance && <button onClick={allowance}>{detail.brief.permissions?.custody === 'main-wallet-service' ? 'Set up auto trading' : 'Review Autopilot allowance'}</button>}<button onClick={settings}>Review &amp; edit strategy</button><button onClick={orders}>Review strategy orders</button><button className="primary" onClick={close}>Done</button></div></footer>
    </section>
  </div>
}
