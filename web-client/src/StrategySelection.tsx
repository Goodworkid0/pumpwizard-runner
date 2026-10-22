import type { AllocationSizing, SizingRequest } from './SavedStrategyEditor'
import { SavedStrategyEditor, InitialOrderLimitsEditor } from './SavedStrategyEditor'
import { WalletWorkspaceScope } from './walletWorkspaceScope'
import { useContext, useEffect, useMemo, useState } from 'react'
import type { PortfolioSession, SessionWorkspace } from './PortfolioWorkspace'
import type { ServiceConnectionState } from './serviceControl'
import { formatSol } from './browserWallet'
import { groupSavedStrategyVersions, sessionStrategyAssociation, strategyTradingSetup } from './strategySetup'
import type { SessionPlan } from './SessionPlanDialog'

export type MarketplaceReport = {
  initial_usd?: number | null
  equity_usd?: number | null
  pnl_usd?: number | null
  realized_net_pnl_usd?: number | null
  unrealized_pnl_usd?: number | null
  closed_count?: number | null
  closed_wins?: number | null
  closed_win_rate?: number | null
  total_entries?: number | null
  open_position_count?: number | null
  started_at?: number | null
  last_at?: number | null
  pnl_1h_usd?: number | null
  pnl_8h_usd?: number | null
  pnl_24h_usd?: number | null
  max_drawdown?: number | null
  fees_usd?: number | null
  failed_fees_usd?: number | null
}

export type MarketplaceStrategy = {
  id: string
  digest: string
  label: string
  family: string
  description: string
  report: MarketplaceReport | null
  source_as_of: number | null
  source_stale: boolean
  compatibility: string
  reason: string | null
  package: { configuration?: Record<string, unknown>; trial?: object }
}

export type LocalStrategy = {
  digest: string
  imported_at?: string | number | null
  schema_name?: string | null
  label: string
  strategy_id: string
  strategy_version: string | null
  engine?: string | null
  compatibility: string
  reason: string | null
  allocation_sizing?: AllocationSizing[]
  configuration?: Record<string, unknown>
}

export type LocalStrategySession = PortfolioSession

type Period = 'All time' | '1 hour' | '8 hours' | '24 hours'
type ResultFilter = 'All results' | 'Profitable' | 'Loss or flat' | 'Untested'
type Sort = 'Best P&L' | 'Best ROI' | 'Most entries' | 'Highest win rate' | 'Name'

const PERIODS: { label: Period; field: keyof MarketplaceReport }[] = [
  { label: 'All time', field: 'pnl_usd' },
  { label: '1 hour', field: 'pnl_1h_usd' },
  { label: '8 hours', field: 'pnl_8h_usd' },
  { label: '24 hours', field: 'pnl_24h_usd' },
]
const PAGE_SIZE = 40

function numeric(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function money(value: unknown, signed = false) {
  const amount = numeric(value)
  if (amount === null) return '—'
  const sign = amount < 0 ? '−' : signed && amount > 0 ? '+' : ''
  return `${sign}$${Math.abs(amount).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function percent(value: unknown, fraction = false) {
  const amount = numeric(value)
  if (amount === null) return '—'
  const normalized = fraction ? amount * 100 : amount
  return `${normalized.toLocaleString(undefined, { maximumFractionDigits: 1 })}%`
}

function count(value: unknown) {
  const amount = numeric(value)
  return amount === null ? '—' : amount.toLocaleString()
}

function duration(value: unknown) {
  const seconds = numeric(value)
  if (seconds === null) return '—'
  if (seconds >= 86400 && seconds % 86400 === 0) return `${seconds / 86400} days`
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600} hours`
  if (seconds >= 60) return `${Math.round(seconds / 60)} minutes`
  return `${seconds} seconds`
}

function cap(value: unknown) {
  const amount = numeric(value)
  if (amount === null) return '—'
  return `$${amount.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
}

function periodPnl(strategy: MarketplaceStrategy, period: Period) {
  const field = PERIODS.find((item) => item.label === period)?.field || 'pnl_usd'
  return numeric(strategy.report?.[field])
}

function periodRoi(strategy: MarketplaceStrategy, period: Period) {
  const pnl = periodPnl(strategy, period)
  const report = strategy.report
  if (pnl === null || !report) return null
  const baseline = period === 'All time' ? numeric(report.initial_usd) : (() => {
    const equity = numeric(report.equity_usd)
    return equity === null ? null : equity - pnl
  })()
  return baseline !== null && baseline > 0 ? pnl / baseline * 100 : null
}

function sourceTime(value: number | null) {
  return value ? new Date(value * 1000).toLocaleString() : 'No published simulation timestamp'
}

function importedTime(value: LocalStrategy['imported_at']) {
  if (!value) return 'Import time unavailable'
  const parsed = typeof value === 'number' ? new Date(value * 1000) : new Date(value)
  return Number.isNaN(parsed.getTime()) ? 'Import time unavailable' : `Imported ${parsed.toLocaleDateString()}`
}

function strategyVersion(strategy: LocalStrategy) {
  return strategy.strategy_version || String(strategy.configuration?.version || 'legacy')
}

function strategyInitials(label: string) {
  const words = label.trim().split(/\s+/).filter(Boolean)
  return `${words[0]?.[0] || 'S'}${words[1]?.[0] || ''}`.toUpperCase()
}

export function LocalStrategyLibrary({ strategies, sessions, sessionPlan, workspace = null, walletSigning = false, service, openTradingSetup, reviewFunds, catalog = [], close, create, importPreset, importFile, inspect, exportStrategy, start, saveRevision, updatePlan }: {
  strategies: LocalStrategy[]
  sessions: LocalStrategySession[]
  sessionPlan?: SessionPlan | null
  workspace?: SessionWorkspace | null
  walletSigning?: boolean
  service?: ServiceConnectionState
  openTradingSetup?: (id: string) => void
  reviewFunds?: (workspace: SessionWorkspace) => void
  catalog?: MarketplaceStrategy[]
  close: () => void
  create: () => void
  importPreset: (strategy: MarketplaceStrategy) => Promise<{ digest: string; instanceId?: string }>
  importFile: () => void
  inspect: (digest: string) => void
  exportStrategy: (digest: string) => void
  saveRevision: (digest: string, label: string, configuration: Record<string, unknown>, sizing?: SizingRequest, instanceId?: string) => Promise<string>
  updatePlan: (plan: SessionPlan, instanceId?: string) => Promise<void>
  start: (strategy: LocalStrategy) => Promise<string | undefined>
}) {
  const walletScoped = useContext(WalletWorkspaceScope) !== '?scope=research'
  const [source, setSource] = useState<'saved' | 'predefined'>('saved')
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<'all' | 'ready' | 'review'>('all')
  const [selectedDigest, setSelectedDigest] = useState(() => strategies.find((strategy) => sessionPlan?.workspace_id && sessions.some((row) => row.instance.workspace_id === sessionPlan.workspace_id && row.instance.strategy_digest === strategy.digest))?.digest || strategies[0]?.digest || '')
  const [attachedSessionId, setAttachedSessionId] = useState<string | undefined>()
  const [editing, setEditing] = useState(false)
  const [editingLimits, setEditingLimits] = useState(false)
  const [revisionBusy, setRevisionBusy] = useState(false)
  const [savedNotice, setSavedNotice] = useState('')
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState('')
  const catalogSetup = sessionPlan?.strategy_setup === 'catalog'
  const assignedDigests = useMemo(() => sessionPlan?.workspace_id ? sessions.filter((item) => item.instance.workspace_id === sessionPlan.workspace_id).flatMap((item) => [item.instance.strategy_digest || '', item.brief.strategy_digest_at_creation]) : [], [sessions, sessionPlan?.workspace_id])
  const addSelected = async () => {
    if (!selected || adding || alreadyAdded || editing || editingLimits) return
    setAdding(true)
    setAddError('')
    try { const id = await start(selected); if (id) { setAttachedSessionId(id); setEditing(true); setSavedNotice('Added to this session. Review the settings before starting.') } } catch (error) { setAddError(error instanceof Error ? error.message : 'Strategy could not be added.') } finally { setAdding(false) }
  }
  const sessionCounts = useMemo(() => {
    const counts = new Map<string, number>()
    sessions.forEach(({ instance }) => {
      if (instance.strategy_digest) counts.set(instance.strategy_digest, (counts.get(instance.strategy_digest) || 0) + 1)
    })
    return counts
  }, [sessions])
  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase()
    const rows = strategies.filter((strategy) => {
      const ready = strategy.compatibility === 'monitoring-ready'
      if (filter === 'ready' && (!ready || assignedDigests.includes(strategy.digest))) return false
      if (filter === 'review' && ready) return false
      return !needle || `${strategy.label} ${strategy.strategy_id} ${strategy.strategy_version || ''} ${strategy.engine || ''}`.toLocaleLowerCase().includes(needle)
    })
    if (!sessionPlan) return rows
    return rows.sort((left, right) => {
      const leftFit = sessionPlan.recommended_families.includes(String(left.configuration?.entry_style || '')) ? 1 : 0
      const rightFit = sessionPlan.recommended_families.includes(String(right.configuration?.entry_style || '')) ? 1 : 0
      return Number(assignedDigests.includes(right.digest)) - Number(assignedDigests.includes(left.digest)) || rightFit - leftFit || left.label.localeCompare(right.label)
    })
  }, [strategies, query, filter, sessionPlan, assignedDigests])
  const selected = visible.find((strategy) => strategy.digest === selectedDigest) || visible[0] || null
  const readyCount = strategies.filter((strategy) => strategy.compatibility === 'monitoring-ready').length
  const availableCount = strategies.filter((strategy) => strategy.compatibility === 'monitoring-ready' && !assignedDigests.includes(strategy.digest)).length
  const groups = groupSavedStrategyVersions(visible, sessions, sessionPlan?.workspace_id)
  const totalGroups = groupSavedStrategyVersions(strategies, sessions, sessionPlan?.workspace_id).length

  useEffect(() => {
    if (selected && selected.digest !== selectedDigest) setSelectedDigest(selected.digest)
  }, [selected, selectedDigest])

  const config = selected?.configuration || {}
  const targets = Array.isArray(config.targets) ? config.targets : []
  const selectedSessions = selected ? sessionCounts.get(selected.digest) || 0 : 0
  const ready = selected?.compatibility === 'monitoring-ready'
  const selectedEvidence = selected ? catalog?.find((item) => item.digest === selected.digest) || null : null
  const selectedSizing = selected?.allocation_sizing?.find((item) => item.workspace_id === sessionPlan?.workspace_id)

  const associatedSession = selected ? sessionStrategyAssociation(sessions, sessionPlan?.workspace_id, selected.digest) : null
  const alreadyAdded = Boolean(associatedSession)
  const setup = strategyTradingSetup({ strategy: selected, session: associatedSession, workspace, plan: sessionPlan, walletSigning, service })
  const editSelected = () => {
    if (associatedSession) {
      setAttachedSessionId(associatedSession.instance.id)
      if (associatedSession.instance.strategy_digest) setSelectedDigest(associatedSession.instance.strategy_digest)
    }
    setEditing(true)
  }
  const savePreset = async (preset: MarketplaceStrategy) => {
    setAdding(true)
    let added
    try { added = await importPreset(preset) } finally { setAdding(false) }
    setAttachedSessionId(added.instanceId)
    setEditing(Boolean(added.instanceId))
    setSelectedDigest(added.digest)
    setQuery('')
    setFilter('all')
    setSource('saved')
  }

  return <div className="modal-backdrop local-library-backdrop" role="presentation" onMouseDown={adding || editing || editingLimits ? undefined : close}>
    <section className={`strategy-modal local-library${editing ? ' editing-strategy' : ''}`} role="dialog" aria-modal="true" aria-label="Local strategy library" onMouseDown={(event) => event.stopPropagation()}>
      <header className="strategy-workspace-header">
        <div className="strategy-workspace-title"><small>{editing ? 'SESSION STRATEGY' : 'STRATEGY WORKSPACE'}</small><h2>{editing ? 'Configure strategy' : sessionPlan ? 'Choose a strategy' : 'Saved strategies'}</h2><p>{sessionPlan?.session_mode === 'trade' ? `${sessionPlan.allocation_sol} SOL allocated · ${sessionPlan.execution_style === 'interactive' ? 'Interactive' : sessionPlan.automation_scope?.replace(/-/g, ' ')}` : sessionPlan ? 'Monitoring session · no wallet funds' : walletScoped ? 'Strategies for this wallet' : 'Research strategies'}</p></div>
        <div className="strategy-workspace-navigation" inert={editing || editingLimits || undefined}><nav aria-label="Strategy sources"><button className={source === 'saved' ? 'selected' : ''} aria-pressed={source === 'saved'} onClick={() => setSource('saved')}>My Strategies <span>{totalGroups}</span></button><button className={source === 'predefined' ? 'selected' : ''} aria-pressed={source === 'predefined'} onClick={() => setSource('predefined')}>Public Lab Data</button></nav><div className="strategy-workspace-tools"><button onClick={create}>+ Create strategy</button><button onClick={importFile}>↑ Import JSON</button></div></div>
        <button className="close" aria-label="Close dialog" disabled={adding || revisionBusy} onClick={close}>×</button>
      </header>
      {source === 'predefined' ? <PredefinedStrategies strategies={catalog} importStrategy={savePreset} sessionAvailable={Boolean(sessionPlan?.workspace_id)} assignedDigests={assignedDigests} /> : <>
      <div className="local-library-toolbar" inert={editing || editingLimits || undefined}>
        <label className="simulation-search"><span aria-hidden="true">⌕</span><input aria-label="Search local strategies" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search saved strategies" /></label>
        <div className="local-library-filters" aria-label="Filter local strategies">
          <button className={filter === 'all' ? 'selected' : ''} onClick={() => setFilter('all')}>All <span>{strategies.length}</span></button>
          <button className={filter === 'ready' ? 'selected' : ''} onClick={() => setFilter('ready')}>Can add <span>{availableCount}</span></button>
          <button className={filter === 'review' ? 'selected' : ''} onClick={() => setFilter('review')}>Needs fixes <span>{strategies.length - readyCount}</span></button>
        </div>
      </div>
      <div className="local-library-body">
        <section className="local-strategy-results" aria-label="Saved strategies" inert={editing || editingLimits || undefined}>
          <div className="local-results-heading"><span><b>{groups.length}</b> {groups.length === 1 ? 'strategy choice' : 'strategy choices'}</span><small>{visible.length} saved {visible.length === 1 ? 'version' : 'versions'}</small></div>
          {visible.length === 0 ? <div className="simulation-empty"><b>{strategies.length ? 'No matching strategies' : 'No saved strategies in this wallet'}</b><p>{strategies.length ? 'Try another name or filter.' : 'Add a strategy from Public Lab Data, create your own, or import a JSON file.'}</p><button onClick={() => { if (!strategies.length) setSource('predefined'); else { setQuery(''); setFilter('all') } }}>{strategies.length ? 'Clear filters' : 'Browse Public Lab Data'}</button></div> : <div className="local-strategy-list">{groups.map((versions) => <div className="local-strategy-version-group" key={`${versions[0].strategy_id}:${versions[0].label}`}>
            {versions.map((strategy, index) => {
              const itemReady = strategy.compatibility === 'monitoring-ready'
              const countForStrategy = sessionCounts.get(strategy.digest) || 0
              const association = sessionStrategyAssociation(sessions, sessionPlan?.workspace_id, strategy.digest)
              const card = <article className={selected?.digest === strategy.digest ? 'selected' : ''} key={strategy.digest}>
                <button className="local-strategy-row" onClick={() => { setSelectedDigest(strategy.digest); setSavedNotice(''); setAttachedSessionId(undefined) }}>
                  <i aria-hidden="true">{strategyInitials(strategy.label)}</i>
                  <span><b>{strategy.label}</b><small>{index === 0 && versions.length > 1 ? `${versions.length} saved versions · ` : ''}Version {strategy.digest.slice(0, 7)} · {importedTime(strategy.imported_at)}</small><em>{countForStrategy ? `Used by ${countForStrategy} ${countForStrategy === 1 ? 'strategy' : 'strategies'}` : 'Saved in your library'}</em></span>
                  <strong className={association ? 'added' : itemReady ? 'available' : 'review'}>{association ? association.instance.strategy_digest === strategy.digest ? 'Added to this session' : 'Session uses edited copy' : itemReady ? sessionPlan ? 'Can add' : 'Compatible rules' : 'Needs fixes'}</strong>
                </button>
              </article>
              return index === 0 ? card : null
            })}
            {versions.length > 1 && <details className="local-other-versions" open={versions.slice(1).some((row) => row.digest === selected?.digest) || undefined}><summary>Other saved versions ({versions.length - 1})</summary>{versions.slice(1).map((strategy) => <article className={selected?.digest === strategy.digest ? 'selected' : ''} key={strategy.digest}><button className="local-strategy-row" onClick={() => { setSelectedDigest(strategy.digest); setSavedNotice(''); setAttachedSessionId(undefined) }}><span><b>Version {strategy.digest.slice(0, 7)}</b><small>{importedTime(strategy.imported_at)}</small></span><strong className={assignedDigests.includes(strategy.digest) ? 'added' : 'available'}>{assignedDigests.includes(strategy.digest) ? 'Session uses edited copy' : strategy.compatibility === 'monitoring-ready' ? 'Can add' : 'Needs fixes'}</strong></button></article>)}</details>}
          </div>)}</div>}
        </section>
        <section className="local-strategy-detail" key={`${selected?.digest}:${editing}`} aria-live="polite">
          {!selected ? <div className="simulation-empty"><b>Select a strategy</b><p>Its stored rules and session options will appear here.</p></div> : editing ? <SavedStrategyEditor attachedToSession={Boolean(attachedSessionId)} key={selected.digest} label={selected.label} configuration={selected.configuration || {}} plan={sessionPlan} sizing={selected.allocation_sizing?.find((item) => item.workspace_id === sessionPlan?.workspace_id)} cancel={() => setEditing(false)} save={async (label, configuration, sizing) => {
            setRevisionBusy(true)
            try { const digest = await saveRevision(selected.digest, label, configuration, sizing, attachedSessionId); setSelectedDigest(digest); setQuery(''); setFilter('all'); setEditing(false); setSavedNotice(attachedSessionId ? 'Session strategy saved. Monitoring and trading remain off.' : 'Revision saved. Existing sessions retain their original rules.') } finally { setRevisionBusy(false) }
          }} /> : <>
            {savedNotice && <p className="strategy-saved-notice" role="status">{savedNotice}</p>}
            <header><div><small>{String(config.entry_style || 'LOCAL').toUpperCase()} STRATEGY</small><h3>{selected.label}</h3><p>{selected.strategy_id} · {strategyVersion(selected)} · {selected.engine || 'Engine unavailable'}</p></div><span className={ready ? 'ready' : 'review'}>{alreadyAdded ? 'Added to this session' : ready ? 'Rules supported' : 'Needs fixes'}</span></header>
            <div className="local-strategy-meta"><span>{importedTime(selected.imported_at)}</span><code title={selected.digest}>{selected.digest.slice(0, 12)}…</code></div>
            <details className={`strategy-evidence-status ${selectedEvidence?.report && selectedEvidence.source_as_of ? selectedEvidence.source_stale ? 'stale' : 'ready' : 'untested'}`}><summary>Simulation history (optional)</summary>
              <span><small>EXACT PACKAGE EVIDENCE</small><b>{selectedEvidence?.report && selectedEvidence.source_as_of ? selectedEvidence.source_stale ? 'Attributed report · retained older snapshot' : 'Attributed current simulation report' : 'No attributed simulation report'}</b><p>{selectedEvidence?.report && selectedEvidence.source_as_of ? `${money(selectedEvidence.report.pnl_usd, true)} all-time P&L · ${sourceTime(selectedEvidence.source_as_of)}. Historical evidence is not a return forecast.` : 'This exact configuration is an untested candidate. No result from another strategy is applied to it.'}</p></span>
            </details>
            <section className={`local-trading-setup ${setup.active ? 'active' : 'pending'}`} aria-label="Trading setup">
              <header><div><small>{setup.trade ? 'REAL WALLET TRADING' : 'MONITORING SESSION'}</small><h4>{setup.heading}</h4><p>{setup.detail}</p></div></header>
              {setup.trade && <ol>
                <li className={alreadyAdded ? 'done' : ''}><b>Strategy</b><span>{alreadyAdded ? 'Added to this session' : 'Add this saved strategy'}</span></li>
                <li className={setup.rulesSupported ? 'done' : ''}><b>Rules</b><span>{setup.rulesSupported ? 'Supported · review trade size and exits' : 'Needs fixes before use'}</span></li>
                <li className={setup.fundsReady ? 'done' : ''}><b>Session funds</b><span>{workspace?.funding?.state === 'insufficient' ? `Short ${formatSol(workspace.funding.shortfall_lamports)}` : setup.fundsReady ? 'Confirmed and sufficient' : 'Confirmation needed'}</span>{workspace && reviewFunds && <button onClick={() => reviewFunds(workspace)}>Review funds</button>}</li>
                <li className={setup.active ? 'done' : ''}><b>Trading approval</b><span>{setup.approval}</span>{associatedSession && openTradingSetup && <button onClick={() => openTradingSetup(associatedSession.instance.id)}>Open trading controls</button>}</li>
              </ol>}
            </section>
            <section className="local-config"><div className="simulation-section-title"><span><small>SAVED RULES</small><b>Configuration at a glance</b></span><button className="strategy-edit-button" onClick={editSelected} disabled={editingLimits || !ready || config.version !== 'early-ladder-v1'} title={config.version !== 'early-ladder-v1' ? 'This editor supports early-ladder strategies. Export other package formats for editing.' : undefined}>Edit strategy</button></div>
              <dl>
                <div><dt>Entry market cap</dt><dd>{cap(config.entry_cap_min)} – {cap(config.entry_cap_max)}</dd></div>
                <div><dt>Entry signal</dt><dd>{String(config.entry_style || '—')}</dd></div>
                <div><dt>Observation</dt><dd>{duration(config.observation_seconds)}</dd></div>
                <div><dt>{selectedSizing ? 'Trade budget' : 'Template trade size'}</dt><dd>{selectedSizing ? `${selectedSizing.trade_percent}% · ${selectedSizing.trade_budget_sol} SOL` : money(config.order_usd)}</dd></div>
                <div><dt>Curve liquidity</dt><dd>{money(config.min_curve_liquidity_usd)} min</dd></div>
                <div><dt>Maximum positions</dt><dd>{count(config.max_positions)}</dd></div>
                <div><dt>Maximum hold</dt><dd>{duration(config.max_hold_seconds)}</dd></div>
                <div><dt>Exit stages</dt><dd>{targets.length || '—'}</dd></div>
              </dl>
            </section>
            {catalogSetup && sessionPlan && editingLimits ? <InitialOrderLimitsEditor plan={selectedSizing && sessionPlan ? { ...sessionPlan, max_trade_sol: selectedSizing.trade_budget_sol } : sessionPlan} sizing={selectedSizing} cancel={() => setEditingLimits(false)} attachedToSession={Boolean(attachedSessionId)} save={async (plan) => { await updatePlan(plan, attachedSessionId); setEditingLimits(false); setSavedNotice(attachedSessionId ? 'Order limits saved for this session strategy.' : 'Initial limits applied for the next strategy added to this session.') }} /> : catalogSetup && sessionPlan ? <details className="session-plan-note"><summary>Initial order limits</summary><p>Maximum buy {selectedSizing?.trade_budget_sol || sessionPlan.max_trade_sol} SOL · minimum {sessionPlan.min_trade_sol} SOL · daily buys {sessionPlan.max_daily_buy_sol} SOL · {sessionPlan.max_trades_per_day} trades/day · {sessionPlan.max_open_positions} positions.</p><p>Daily loss cap {sessionPlan.max_daily_loss_sol} SOL ({sessionPlan.max_daily_loss_percent}%) · per-position loss limit {sessionPlan.max_trade_loss_percent}%. These wallet limits apply alongside the saved strategy rules. {attachedSessionId ? 'Changes apply to this session strategy.' : 'These initial limits are applied when you add this strategy.'}</p><button className="strategy-edit-button" onClick={() => setEditingLimits(true)}>Edit order limits</button></details> : <section className="local-session-flow"><div><i>1</i><span><b>Saved package</b><small>Rules remain unchanged</small></span></div><em>›</em><div><i>2</i><span><b>Create session</b><small>Entries start disabled</small></span></div><em>›</em><div><i>3</i><span><b>Review setup</b><small>Size, liquidity, and exits</small></span></div></section>}
            <footer><div className="local-footer-actions"><button onClick={() => inspect(selected.digest)}>Package details</button><button onClick={() => exportStrategy(selected.digest)}>Export exact JSON</button></div><p>{sessionPlan ? associatedSession ? associatedSession.tracking.enabled ? 'Monitoring is on. Trading requires the approval shown above.' : 'Use strategy controls to start monitoring and review trading.' : 'Adding prepares the strategy. Start from its controls.' : <><b>{selectedSessions} existing {selectedSessions === 1 ? 'strategy' : 'strategies'}.</b> Choose a session for wallet trading.</>}</p>{addError && <p className="error" role="alert">{addError}</p>}<button className="primary" disabled={(!ready && !alreadyAdded) || adding || editingLimits} onClick={alreadyAdded ? editSelected : () => void addSelected()}>{alreadyAdded ? 'Edit session strategy' : adding ? 'Adding…' : ready ? 'Add to session & edit' : 'Rules need fixes'}</button></footer>
          </>}
        </section>
      </div></>}
    </section>
  </div>
}

function ReportWindows({ strategy }: { strategy: MarketplaceStrategy }) {
  const rows = PERIODS.map((period) => ({ ...period, value: numeric(strategy.report?.[period.field]) }))
  const scale = Math.max(1, ...rows.map((row) => Math.abs(row.value || 0)))
  return <div className="simulation-windows" aria-label="Reported performance windows">
    {rows.map((row) => <div key={row.label} className={row.value === null ? 'missing' : row.value >= 0 ? 'positive' : 'negative'}>
      <span><small>{row.label}</small><b>{money(row.value, true)}</b></span>
      <i><em style={{ width: row.value === null ? '0' : `${Math.max(3, Math.abs(row.value) / scale * 100)}%` }} /></i>
    </div>)}
  </div>
}

function ConfigurationSummary({ strategy }: { strategy: MarketplaceStrategy }) {
  const config = strategy.package.configuration || {}
  const targets = Array.isArray(config.targets) ? config.targets as { market_cap_usd?: unknown; initial_fraction?: unknown }[] : []
  return <section className="simulation-config">
    <div className="simulation-section-title"><span><small>EXACT IMPORTED RULES</small><b>Configuration summary</b></span><em>{String(config.version || 'Version unavailable')}</em></div>
    <dl>
      <div><dt>Entry market cap</dt><dd>{cap(config.entry_cap_min)} – {cap(config.entry_cap_max)}</dd></div>
      <div><dt>Entry style</dt><dd>{String(config.entry_style || strategy.family)}</dd></div>
      <div><dt>Observation</dt><dd>{duration(config.observation_seconds)}</dd></div>
      <div><dt>Order / budget</dt><dd>{money(config.order_usd)} / {money(config.budget_usd)}</dd></div>
      <div><dt>Maximum positions</dt><dd>{count(config.max_positions)}</dd></div>
      <div><dt>Maximum hold</dt><dd>{duration(config.max_hold_seconds)}</dd></div>
      <div><dt>Stop loss</dt><dd>{percent(numeric(config.stop_loss_fraction) === null ? null : Math.abs(Number(config.stop_loss_fraction)), true)}</dd></div>
      <div><dt>Trailing drawdown</dt><dd>{percent(config.trailing_drawdown_fraction, true)}</dd></div>
    </dl>
    {targets.length > 0 && <div className="simulation-targets"><small>STAGED EXITS</small>{targets.map((target, index) => <span key={`${String(target.market_cap_usd)}-${index}`}><b>{cap(target.market_cap_usd)}</b><em>sell {percent(target.initial_fraction, true)}</em></span>)}</div>}
  </section>
}

function PredefinedStrategies({ strategies, importStrategy, assignedDigests, sessionAvailable }: {
  strategies: MarketplaceStrategy[]
  importStrategy: (strategy: MarketplaceStrategy) => Promise<void>
  assignedDigests: string[]
  sessionAvailable: boolean
}) {
  const [query, setQuery] = useState('')
  const [family, setFamily] = useState('All families')
  const [result, setResult] = useState<ResultFilter>('All results')
  const [compatibility, setCompatibility] = useState('All compatibility')
  const [period, setPeriod] = useState<Period>('All time')
  const [sort, setSort] = useState<Sort>('Best P&L')
  const [selectedId, setSelectedId] = useState('')
  const [page, setPage] = useState(0)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const families = useMemo(() => [...new Set(strategies.map((strategy) => strategy.family))].sort(), [strategies])
  const compatibilityOptions = useMemo(() => [...new Set(strategies.map((strategy) => strategy.compatibility))].sort(), [strategies])
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase()
    const rows = strategies.filter((strategy) => {
      const pnl = periodPnl(strategy, period)
      if (needle && !`${strategy.label} ${strategy.id} ${strategy.family} ${strategy.description}`.toLocaleLowerCase().includes(needle)) return false
      if (family !== 'All families' && strategy.family !== family) return false
      if (compatibility !== 'All compatibility' && strategy.compatibility !== compatibility) return false
      if (result === 'Untested' && strategy.report) return false
      if (result === 'Profitable' && (pnl === null || pnl <= 0)) return false
      if (result === 'Loss or flat' && (pnl === null || pnl > 0)) return false
      return true
    })
    return rows.sort((left, right) => {
      if (sort === 'Name') return left.label.localeCompare(right.label)
      const leftValue = sort === 'Best ROI' ? periodRoi(left, period)
        : sort === 'Most entries' ? numeric(left.report?.total_entries)
          : sort === 'Highest win rate' ? numeric(left.report?.closed_win_rate)
            : periodPnl(left, period)
      const rightValue = sort === 'Best ROI' ? periodRoi(right, period)
        : sort === 'Most entries' ? numeric(right.report?.total_entries)
          : sort === 'Highest win rate' ? numeric(right.report?.closed_win_rate)
            : periodPnl(right, period)
      if (leftValue === null) return rightValue === null ? left.label.localeCompare(right.label) : 1
      if (rightValue === null) return -1
      return rightValue - leftValue || left.label.localeCompare(right.label)
    })
  }, [strategies, query, family, result, compatibility, period, sort])
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const visible = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)
  const selected = visible.find((strategy) => strategy.id === selectedId) || visible[0] || null

  useEffect(() => { setPage(0) }, [query, family, result, compatibility, period, sort])
  useEffect(() => { if (selected && selected.id !== selectedId) setSelectedId(selected.id) }, [selected, selectedId])

  const reset = () => {
    setQuery(''); setFamily('All families'); setResult('All results'); setCompatibility('All compatibility')
    setPeriod('All time'); setSort('Best P&L'); setPage(0); setError('')
  }
  const runImport = async () => {
    if (!selected || busy) return
    setBusy(selected.id); setError('')
    try { await importStrategy(selected) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'This strategy could not be imported.') }
    finally { setBusy('') }
  }

  return <div className="embedded-simulation">
    <section className="strategy-modal simulation-library" role="region" aria-label="Public Lab Data">
      <div className="simulation-filters">
        <label className="simulation-search"><span aria-hidden="true">⌕</span><input aria-label="Search simulations" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search name, family, or strategy ID" /></label>
        <label><span>FAMILY</span><select aria-label="Simulation family" value={family} onChange={(event) => setFamily(event.target.value)}><option>All families</option>{families.map((item) => <option key={item}>{item}</option>)}</select></label>
        <label><span>RESULT</span><select aria-label="Simulation result" value={result} onChange={(event) => setResult(event.target.value as ResultFilter)}><option>All results</option><option>Profitable</option><option>Loss or flat</option><option>Untested</option></select></label>
        <label><span>COMPATIBILITY</span><select aria-label="Simulation compatibility" value={compatibility} onChange={(event) => setCompatibility(event.target.value)}><option>All compatibility</option>{compatibilityOptions.map((item) => <option key={item}>{item}</option>)}</select></label>
        <label><span>SORT</span><select aria-label="Simulation sort" value={sort} onChange={(event) => setSort(event.target.value as Sort)}><option>Best P&amp;L</option><option>Best ROI</option><option>Most entries</option><option>Highest win rate</option><option>Name</option></select></label>
        <button onClick={reset}>Reset</button>
      </div>
      <nav className="simulation-periods" aria-label="Simulation performance period">{PERIODS.map((item) => <button className={period === item.label ? 'selected' : ''} key={item.label} onClick={() => setPeriod(item.label)}>{item.label}</button>)}</nav>
      <div className="simulation-body">
        <section className="simulation-results" aria-label="Simulation results">
          <div className="simulation-results-heading"><span><b>{filtered.length.toLocaleString()}</b> matching strategies</span><small>{period} performance</small></div>
          {visible.length === 0 ? <div className="simulation-empty"><b>No matching simulations</b><p>Try a wider family, result, or compatibility filter.</p><button onClick={reset}>Clear filters</button></div> : <div className="simulation-result-list">{visible.map((strategy) => {
            const pnl = periodPnl(strategy, period)
            const roi = periodRoi(strategy, period)
            return <button className={selected?.id === strategy.id ? 'selected' : ''} key={strategy.id} onClick={() => { setSelectedId(strategy.id); setError('') }}>
              <span className="simulation-result-title"><b>{strategy.label}</b><em className={strategy.compatibility === 'monitoring-ready' ? 'ready' : ''}>{strategy.compatibility}</em></span>
              <small>{strategy.family} · {strategy.id}</small>
              <span className="simulation-result-metrics"><strong className={pnl === null ? '' : pnl >= 0 ? 'gain' : 'loss'}>{pnl === null ? 'Untested' : money(pnl, true)}</strong><em>{roi === null ? 'ROI —' : `${roi >= 0 ? '+' : ''}${percent(roi)} ROI`}</em><em>{count(strategy.report?.total_entries)} entries</em></span>
            </button>
          })}</div>}
          {visible.length > 0 && <div className="simulation-pagination"><button disabled={page === 0} onClick={() => setPage((value) => value - 1)}>← Previous</button><span>Page {page + 1} of {pages}</span><button disabled={page + 1 >= pages} onClick={() => setPage((value) => value + 1)}>Next →</button></div>}
        </section>
        <section className="simulation-detail" aria-live="polite">
          {!selected ? <div className="simulation-empty"><b>Select a simulation</b><p>Its published evidence and exact rules will appear here.</p></div> : <>
            <header><div><small>{selected.family.toUpperCase()} STRATEGY</small><h3>{selected.label}</h3><p>{selected.description}</p></div><span className={selected.compatibility === 'monitoring-ready' ? 'ready' : ''}>{selected.compatibility}</span></header>
            <div className="simulation-source"><span><i className={selected.source_stale ? 'stale' : ''} />{selected.source_stale ? 'Retained older result' : 'Attributed snapshot'}</span><time>{sourceTime(selected.source_as_of)}</time></div>
            {!selected.report ? <div className="simulation-untested"><b>No published simulation result</b><p>This is an exact research definition, but the catalog does not contain attributed performance evidence for it.</p></div> : <>
              <div className="simulation-metric-grid">
                <span><small>{period.toUpperCase()} P&amp;L</small><b className={(periodPnl(selected, period) || 0) >= 0 ? 'gain' : 'loss'}>{money(periodPnl(selected, period), true)}</b></span>
                <span><small>{period.toUpperCase()} ROI</small><b>{periodRoi(selected, period) === null ? '—' : percent(periodRoi(selected, period))}</b></span>
                <span><small>STARTING CAPITAL</small><b>{money(selected.report.initial_usd)}</b></span>
                <span><small>ENDING EQUITY</small><b>{money(selected.report.equity_usd)}</b></span>
                <span><small>ALL-TIME WIN RATE</small><b>{percent(selected.report.closed_win_rate, true)}</b></span>
                <span><small>CLOSED / ENTRIES</small><b>{count(selected.report.closed_count)} / {count(selected.report.total_entries)}</b></span>
                <span><small>RECORDED FEES</small><b>{money(selected.report.fees_usd)}</b></span>
                <span><small>MAX DRAWDOWN</small><b>{percent(selected.report.max_drawdown, true)}</b></span>
              </div>
              <ReportWindows strategy={selected} />
            </>}
            <ConfigurationSummary strategy={selected} />
            <div className="simulation-compatibility"><b>{selected.compatibility === 'monitoring-ready' ? 'Available for local monitoring' : 'Research inspection only'}</b><p>{selected.reason || 'The exact package passes the current local monitoring contract. Imported sessions still start with entries disabled and manual exits.'}</p></div>
            {error && <p className="simulation-error" role="alert">{error}</p>}
            <footer><p>Simulation performance is historical research evidence, not a return forecast or execution guarantee.</p><button className="primary" disabled={Boolean(busy) || !sessionAvailable || selected?.compatibility !== 'monitoring-ready' || Boolean(selected && assignedDigests.includes(selected.digest))} onClick={() => void runImport()}>{selected && assignedDigests.includes(selected.digest) ? 'Already in this session' : busy ? 'Adding…' : !sessionAvailable ? 'Choose a session first' : 'Add to session & edit'}</button></footer>
          </>}
        </section>
      </div>
    </section>
  </div>
}
