import { useState } from 'react'
import type { FormEvent } from 'react'
import type { PortfolioSession, PortfolioTradeState, SessionWorkspace } from './PortfolioWorkspace'
import type { MarketRow } from './TokenDetail'
import { tokenQuantity } from './tradeFormatting'
import type { TradingFocus } from './OpenPositionsTerminal'

type StrategyManagement = {
  removal_blocks: string[]
  allocation_lamports: string
}

function compactUsd(value: number | null) {
  return value === null || !Number.isFinite(value) ? '—' : `$${value.toLocaleString(undefined, { notation: value >= 1000 ? 'compact' : 'standard', maximumFractionDigits: 1 })}`
}

function lastScan(session: PortfolioSession) {
  const run = session.health.last_run
  if (run === 'never') return 'Not scanned'
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - run.finished_at))
  return seconds < 60 ? `${seconds}s ago` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ago` : `${Math.floor(seconds / 3600)}h ago`
}

function actionError(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback
}

function sessionStateLabel(label: string) {
  return label.trim().toLowerCase() === 'ready' ? 'Configured' : label
}

export function TradingSidebar({
  sessions,
  workspaces,
  trades,
  market,
  openPosition,
  focus,
  selectWorkspace,
  selectStrategy,
  addStrategy,
  addStrategyToWorkspace,
  openSession,
  loadManagement,
  renameStrategy,
  removeStrategy,
  setTracking,
  scanSession,
  manageLimits,
  reviewOrders,
  reviewSession,
  paperSession,
  pauseWorkspace,
  sellWorkspace,
  editFunds,
  viewPositions,
  viewWalletHoldings,
  deposit,
}: {
  sessions: PortfolioSession[]
  workspaces: SessionWorkspace[]
  trades: PortfolioTradeState | null
  market: MarketRow[]
  openPosition: (id: string, strategyId: string) => void
  focus: TradingFocus | null
  selectWorkspace: (workspace: SessionWorkspace) => void
  selectStrategy: (id: string) => void
  addStrategy: () => void
  addStrategyToWorkspace: (workspace: SessionWorkspace) => void
  openSession: (id: string) => void
  loadManagement: (id: string) => Promise<StrategyManagement>
  renameStrategy: (id: string, label: string) => Promise<void>
  removeStrategy: (id: string, releaseAllocation: boolean) => Promise<void>
  setTracking: (session: PortfolioSession) => Promise<void>
  scanSession: (session: PortfolioSession) => Promise<void>
  manageLimits?: () => void
  reviewOrders?: () => void
  reviewSession: (id: string, label: string) => void
  paperSession: (id: string, label: string) => void
  pauseWorkspace: (workspace: SessionWorkspace) => Promise<void>
  sellWorkspace: (workspace: SessionWorkspace) => void
  editFunds: (workspace: SessionWorkspace) => void
  viewPositions: (workspace: SessionWorkspace) => void
  viewWalletHoldings: () => void
  deposit: () => void
}) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const [busyWorkspace, setBusyWorkspace] = useState('')
  const [workspaceError, setWorkspaceError] = useState<{ id: string; message: string } | null>(null)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [mode, setMode] = useState<'actions' | 'edit' | 'remove'>('actions')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [management, setManagement] = useState<(StrategyManagement & { id: string }) | null>(null)
  const legacySessions = sessions.filter((session) => !session.instance.workspace_id)
  const sessionCount = workspaces.length + legacySessions.length
  const marketByMint = new Map(market.map((row) => [row.mint, row]))
  const livePositionIds = new Set(trades?.positions.map((position) => position.id) || [])

  const toggleActions = (session: PortfolioSession) => {
    if (activeId === session.instance.id) {
      setActiveId(null)
      return
    }
    setActiveId(session.instance.id)
    setMode('actions')
    setName(session.instance.label)
    setError('')
    setManagement(null)
  }

  const saveName = async (event: FormEvent, session: PortfolioSession) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await renameStrategy(session.instance.id, name)
      setMode('actions')
    } catch (reason) {
      setError(actionError(reason, 'Strategy name could not be updated.'))
    } finally {
      setBusy(false)
    }
  }

  const beginRemove = async (session: PortfolioSession) => {
    setMode('remove')
    setBusy(true)
    setError('')
    setManagement(null)
    try {
      const value = await loadManagement(session.instance.id)
      setManagement({ id: session.instance.id, ...value })
    } catch (reason) {
      setError(actionError(reason, 'Removal checks could not be loaded.'))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (session: PortfolioSession, current: StrategyManagement) => {
    setBusy(true)
    setError('')
    try {
      await removeStrategy(session.instance.id, Number(current.allocation_lamports) > 0)
      setActiveId(null)
    } catch (reason) {
      setError(actionError(reason, 'Strategy could not be removed.'))
      try {
        const value = await loadManagement(session.instance.id)
        setManagement({ id: session.instance.id, ...value })
      } catch {
        // Preserve the command error; the next open will perform fresh checks.
      }
    } finally {
      setBusy(false)
    }
  }

  const run = async (action: () => Promise<void>, fallback: string) => {
    setBusy(true)
    setError('')
    try {
      await action()
    } catch (reason) {
      setError(actionError(reason, fallback))
    } finally {
      setBusy(false)
    }
  }

  const pauseAll = async (workspace: SessionWorkspace) => {
    setBusyWorkspace(workspace.id)
    setWorkspaceError(null)
    try { await pauseWorkspace(workspace) } catch (reason) { setWorkspaceError({ id: workspace.id, message: actionError(reason, 'Could not pause this session.') }) } finally { setBusyWorkspace('') }
  }

  const renderStrategy = (session: PortfolioSession) => {
    const open = activeId === session.instance.id
    const paperAllowed = session.paper_research_allowed === true
    const currentManagement = management?.id === session.instance.id ? management : null
    const allocation = Number(currentManagement?.allocation_lamports || 0)
    const status = `${sessionStateLabel(session.state.label)}: ${session.state.reason}`
    const positions = [
      ...(trades?.positions.filter((position) => position.status === 'open' && position.agent_id === session.instance.id).map((position) => ({
        id: `wallet-${position.id}`,
        mint: position.mint,
        symbol: marketByMint.get(position.mint)?.symbol || `${position.mint.slice(0, 6)}…${position.mint.slice(-4)}`,
        quantity: tokenQuantity(position.quantity, position.decimals),
        kind: 'Open',
      })) || []),
      ...session.positions.filter((position) => position.status === 'open' && !livePositionIds.has(position.id)).map((position) => ({
        id: `session-${position.id}`,
        mint: position.mint,
        symbol: position.symbol || marketByMint.get(position.mint)?.symbol || `${position.mint.slice(0, 6)}…${position.mint.slice(-4)}`,
        quantity: Number.isFinite(Number(position.quantity)) ? Number(position.quantity).toLocaleString(undefined, { maximumFractionDigits: 6 }) : '—',
        kind: 'Tracked',
      })),
    ]
    const selected = focus?.kind === 'strategy' && focus.id === session.instance.id
    return <div className={`rail-strategy-item${open ? ' expanded' : ''}${selected ? ' selected' : ''}`} data-session-id={session.instance.id} key={session.instance.id}>
      <div className="rail-strategy-line">
        <button className="rail-row" aria-pressed={selected} title={`${session.instance.label} · ${status}`} onClick={() => selectStrategy(session.instance.id)}>
          <svg className="rail-file-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" aria-hidden="true"><path d="M4 2h5l3 3v9H4zM9 2v4h3M6 9h4M6 11h3" /></svg>
          <b>{session.instance.label}</b>
          {positions.length > 0 && <span className="rail-count" title={`${positions.length} open positions`}>{positions.length}</span>}
          <span className={`rail-status-dot ${session.state.tone === 'warning' ? 'warning' : session.tracking.enabled ? 'running' : ''}`} role="img" aria-label={status} title={status} />
        </button>
        <button className="rail-menu-button" aria-label={`Manage ${session.instance.label}`} aria-expanded={open} onClick={() => toggleActions(session)}>•••</button>
      </div>
      {positions.length > 0 && <ul className="rail-strategy-positions" aria-label={`Open positions for ${session.instance.label}`}>
        {positions.map((position) => <li key={position.id}>
          <button className="rail-position-row" data-position-id={position.id} title={`${position.symbol} · ${position.quantity} tokens · ${position.mint}`} onClick={() => openPosition(position.id, session.instance.id)}>
            <span className="rail-position-icon" aria-hidden="true">●</span>
            <b>{position.symbol}</b><span className="rail-position-quantity">{position.quantity}</span><small>{position.kind}</small>
          </button>
        </li>)}
      </ul>}
      {open && <div className="rail-strategy-actions" role="group" aria-label={`${session.instance.label} session actions`}>
        <button className="rail-actions-close" aria-label="Close session actions" disabled={busy} onClick={() => setActiveId(null)}>×</button>
        {error && <p className="rail-action-error" role="alert">{error}</p>}
        {mode === 'edit' ? <form onSubmit={(event) => void saveName(event, session)}>
          <label><span>DISPLAY NAME</span><input aria-label="Session display name" value={name} maxLength={120} autoFocus onChange={(event) => setName(event.target.value)} /></label>
          <p>Only the session label changes. Imported rules stay unchanged.</p>
          <div className="rail-confirm-actions"><button type="button" disabled={busy} onClick={() => { setMode('actions'); setName(session.instance.label); setError('') }}>Cancel</button><button className="primary" type="submit" disabled={busy || !name.trim()}>{busy ? 'Saving…' : 'Save'}</button></div>
        </form> : mode === 'remove' ? <div className="rail-remove-confirm">
          <b>Remove this session?</b>
          <p>Monitoring stops and the row disappears. Stored evidence remains.</p>
          {busy && !currentManagement && <p>Checking positions and authorizations…</p>}
          {allocation > 0 && <p className="rail-allocation-note">{(allocation / 1_000_000_000).toLocaleString(undefined, { maximumFractionDigits: 9 })} SOL will return to main-wallet availability.</p>}
          {currentManagement && currentManagement.removal_blocks.length > 0 && <ul>{currentManagement.removal_blocks.map((block) => <li key={block}>{block}</li>)}</ul>}
          <div className="rail-confirm-actions"><button disabled={busy} onClick={() => { setMode('actions'); setError('') }}>Cancel</button><button className="danger" disabled={busy || !currentManagement || currentManagement.removal_blocks.length > 0} onClick={() => currentManagement && void remove(session, currentManagement)}>{busy ? 'Checking…' : allocation > 0 ? 'Release & remove' : 'Remove'}</button></div>
        </div> : <div className="rail-action-grid">
          <div className="rail-monitor-summary">
            <span><small>MONITOR</small><b className={session.tracking.enabled ? 'running' : ''}>{session.tracking.enabled ? 'Running' : 'Stopped'}</b><em>{lastScan(session)}</em></span>
            <span><small>OBSERVING</small><b>{compactUsd(session.monitor_plan.entry_cap_min)}–{compactUsd(session.monitor_plan.entry_cap_max)}</b><em>{session.monitor_plan.entry_style}</em></span>
            <span><small>MAX LOSS / POSITION</small><b>{session.risk_policy ? `${session.risk_policy.max_trade_loss_bps / 100}%` : session.exit_plan.stop_loss_percent === null ? 'Not set' : `${session.exit_plan.stop_loss_percent}%`}</b><em>{session.risk_policy ? 'Wallet limit' : 'Strategy exit'}</em></span>
            <span><small>POSITIONS</small><b>{positions.length}</b><em>Currently open</em></span>
          </div>
          <button className="rail-open-monitor" onClick={() => { setActiveId(null); openSession(session.instance.id) }}>Open monitor</button>
          {paperAllowed && <button onClick={() => { setActiveId(null); paperSession(session.instance.id, session.instance.label) }}>Paper trading</button>}
          <button onClick={() => { setActiveId(null); reviewSession(session.instance.id, session.instance.label) }}>Inspect research</button>
          <button onClick={() => { setMode('edit'); setName(session.instance.label); setError('') }}>Rename</button>
          {manageLimits && <button onClick={() => { setActiveId(null); manageLimits() }}>Order safety</button>}
          <button className={session.tracking.enabled ? 'rail-stop-monitor' : 'rail-run-monitor'} disabled={busy} onClick={() => void run(() => setTracking(session), 'Monitoring could not be updated.')}>{session.state.label === 'Waiting for monitor' ? 'Start scanning' : session.tracking.fault ? 'Retry monitor' : session.tracking.enabled ? 'Stop monitor' : 'Run monitor'}</button>
          <button disabled={busy || !session.tracking.enabled} onClick={() => void run(() => scanSession(session), 'Scan could not be requested.')}>Scan now</button>
          {reviewOrders && <button onClick={() => { setActiveId(null); reviewOrders() }}>Orders</button>}
          <button className="danger" onClick={() => void beginRemove(session)}>Remove</button>
        </div>}
      </div>}
    </div>
  }

  return <aside className="trade-rail">
    <section>
      <div className="rail-heading"><b>SESSIONS <span className="rail-count">{sessionCount}</span></b><button title="Add session" aria-label="Add session" onClick={addStrategy}>+</button></div>
      <div className="rail-table" aria-label="Sessions and strategies">{sessionCount === 0 ? <p className="empty">No sessions yet</p> : <>
        {workspaces.map((workspace) => {
          const assigned = sessions.filter((session) => session.instance.workspace_id === workspace.id)
          const expanded = !collapsed[workspace.id]
          const allocation = Number(workspace.allocation_lamports) / 1_000_000_000
          const context = workspace.mode === 'monitoring' ? workspace.plan.monitor_activity === 'paper' ? 'Paper trading' : 'Observations' : `${allocation.toLocaleString(undefined, { maximumFractionDigits: 6 })} SOL allocated`
          const pnl = workspace.performance?.total_lamports == null ? null : Number(workspace.performance.total_lamports)
          const pnlLabel = pnl === null ? 'Unavailable' : `${pnl >= 0 ? '+' : '−'}${(Math.abs(pnl) / 1e9).toLocaleString(undefined, { maximumFractionDigits: 6 })} SOL`
          const insufficient = workspace.funding?.state === 'insufficient'
          const positions = workspace.position_summary
          const entriesPaused = assigned.length > 0 && assigned.every((session) => session.instance.entry_mode === 'disabled')
          const openCount = positions?.open_positions ?? workspace.performance?.open_positions ?? 0
          const exitLabel = positions?.automatic_exit_strategies ? 'Exits as configured' : 'Manual exits'
          const status = insufficient ? 'Low SOL · entries paused · exits unchanged' : entriesPaused && workspace.mode === 'trade' ? 'Entries paused · exits unchanged' : workspace.attention_strategies ? `${workspace.attention_strategies} need attention` : workspace.running_strategies ? `${workspace.running_strategies} running` : 'Not running'
          const groupId = `session-strategies-${workspace.id}`
          return <article className="rail-workspace-item" aria-label={`${workspace.label} session`} key={workspace.id}>
            <div className={`rail-workspace-header${focus?.kind === 'session' && focus.id === workspace.id ? ' selected' : ''}`}>
              <div className="rail-workspace-name">
                <button className="rail-workspace-toggle" aria-label={`${expanded ? 'Collapse' : 'Expand'} ${workspace.label}`} aria-expanded={expanded} aria-controls={groupId} title={`${workspace.label} · ${context} · ${status}`} onClick={() => setCollapsed((current) => ({ ...current, [workspace.id]: expanded }))} onKeyDown={(event) => {
                  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
                    event.preventDefault()
                    setCollapsed((current) => ({ ...current, [workspace.id]: event.key === 'ArrowLeft' }))
                  }
                }}>
                  <svg className={`rail-chevron${expanded ? ' open' : ''}`} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
                </button>
                <button className="rail-workspace-select" aria-label={`Select ${workspace.label}`} aria-pressed={focus?.kind === 'session' && focus.id === workspace.id} title={`${workspace.label} · ${context} · ${status}`} onClick={() => selectWorkspace(workspace)}>
                  <svg className="rail-folder-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" aria-hidden="true"><path d="M2 4V3h5l2 2h5v8H2z" /></svg>
                  <b>{workspace.label}</b><span className="rail-count" aria-label={`${assigned.length} strategies`}>{assigned.length}</span>
                  <span className={`rail-status-dot ${insufficient || workspace.attention_strategies ? 'warning' : workspace.running_strategies ? 'running' : ''}`} role="img" aria-label={status} />
                </button>
              </div>
              <div className="rail-workspace-metrics">
                {workspace.mode === 'trade' ? <button className="rail-edit-funds" aria-label={`Edit funds for ${workspace.label}`} title={`${context} · ${Number(workspace.available_lamports) / 1e9} SOL available for trades`} onClick={() => editFunds(workspace)}>{context} <span aria-hidden="true">✎</span></button> : <small>{context}</small>}
                {workspace.mode === 'trade' && <small className={pnl === null ? '' : pnl < 0 ? 'negative' : 'positive'} aria-label={`All-time P&L ${pnlLabel}`} title={`All-time P&L · ${workspace.performance?.detail || 'Session performance is loading'}`}>P&amp;L <b>{pnlLabel}</b></small>}
              </div>
              <div className="rail-workspace-controls" role="group" aria-label={`${workspace.label} controls`}>
                <button disabled={Boolean(busyWorkspace) || !assigned.length || (workspace.mode === 'trade' && entriesPaused)} onClick={() => void pauseAll(workspace)} title={workspace.mode === 'trade' ? 'Pause new entries across this session; keep monitoring and existing exit settings' : 'Stop monitoring every strategy in this session'}>{busyWorkspace === workspace.id ? 'Pausing…' : workspace.mode === 'trade' ? entriesPaused ? 'Entries paused' : 'Ⅱ Pause entries' : 'Ⅱ Pause all'}</button>
                <button className="rail-add-strategy" aria-label={`Add strategy to ${workspace.label}`} title="Add strategy" onClick={() => addStrategyToWorkspace(workspace)}>+</button>
              </div>
            </div>
            {workspace.mode === 'trade' && <div className="rail-workspace-positions" aria-label={`${workspace.label} positions`}>
              <span title="Open positions recorded for this session; a sell review checks live holdings."><b>{openCount} open</b> · {exitLabel}</span>
              <div className="rail-position-actions">
                <button onClick={() => viewPositions(workspace)} title={`${positions?.managed_positions ?? 0} strategy-managed · ${positions?.manual_positions ?? 0} manual`}>View</button>
                <button disabled={Boolean(busyWorkspace) || openCount === 0 || positions?.pending} onClick={() => sellWorkspace(workspace)} title="Review each session position’s sale; every sale requires approval">Review sells</button>
                {Boolean(positions?.other_wallet_positions) && <button onClick={viewWalletHoldings} title="View all wallet holdings">Wallet ({positions!.other_wallet_positions})</button>}
              </div>
            </div>}
            {insufficient && <details className="rail-funding-warning">
              <summary><b role="status">Low SOL · entries paused</b><span>Short {(Number(workspace.funding!.shortfall_lamports) / 1e9).toLocaleString(undefined, { maximumFractionDigits: 6 })} SOL</span><small>Details</small></summary>
              <div className="rail-funding-details">
                <span>Wallet: {(Number(workspace.funding!.balance_lamports) / 1e9).toLocaleString(undefined, { maximumFractionDigits: 9 })} SOL · Reserved across sessions: {(Number(workspace.funding!.required_lamports) / 1e9).toLocaleString(undefined, { maximumFractionDigits: 9 })} SOL</span>
                <span>Existing positions stay held. Exit settings are unchanged; sales need SOL for fees and sufficient wallet funds. Restore funds or reduce allocations, then resume entries manually.</span>
                <div><button onClick={deposit}>Add SOL</button><button onClick={() => editFunds(workspace)}>Edit funds</button></div>
              </div>
            </details>}
            {workspace.funding?.state === 'unavailable' && <p className="rail-funding-warning" role="status">Funds check unavailable · verify before resuming entries.</p>}
            {workspaceError?.id === workspace.id && <p className="rail-action-error" role="alert">{workspaceError.message}</p>}
            <div id={groupId} className="rail-workspace-strategies" aria-label={`${workspace.label} strategies`} hidden={!expanded}>
              {assigned.map(renderStrategy)}
              {assigned.length === 0 && <span className="rail-empty-strategies">No strategies · use + to add</span>}
            </div>
          </article>
        })}
        {legacySessions.map(renderStrategy)}</>}</div>
    </section>
  </aside>
}
