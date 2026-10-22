import type { PortfolioSession, PortfolioTradeState, SessionPerformance, SessionWorkspace } from './PortfolioWorkspace'
import type { TradingFocus } from './OpenPositionsTerminal'
import type { ServiceConnectionState } from './serviceControl'

export type StrategyResult = {
  id: string
  label: string
  workspace: SessionWorkspace | null
  session: PortfolioSession | null
  archived: boolean
  performance: SessionPerformance | null
}

export function isTradingResult(row: StrategyResult) {
  return row.workspace?.mode === 'trade' || row.session?.brief.session_mode === 'trade'
    || row.session?.brief.mode === 'trade-interactive' || row.session?.brief.mode === 'trade-autopilot'
}

export function strategyResults(sessions: PortfolioSession[], workspaces: SessionWorkspace[], focus: TradingFocus | null) {
  const byId = new Map(sessions.map((session) => [session.instance.id, session]))
  const rows: StrategyResult[] = workspaces.flatMap((workspace) => {
    const history = workspace.strategy_performance || workspace.history_strategies || workspace.strategy_ids.map((id) => ({ id, label: byId.get(id)?.instance.label || 'Strategy', removed: false }))
    return history.map((item) => ({
      id: item.id, label: byId.get(item.id)?.instance.label || item.label,
      workspace, session: byId.get(item.id) || null, archived: item.removed,
      performance: 'performance' in item ? item.performance as SessionPerformance : null,
    }))
  })
  const assigned = new Set(rows.map((row) => row.id))
  rows.push(...sessions.filter((session) => !assigned.has(session.instance.id)).map((session) => ({ id: session.instance.id, label: session.instance.label, workspace: null, session, archived: false, performance: null })))
  return rows.filter((row) => !focus || (focus.kind === 'strategy' ? row.id === focus.id : row.workspace?.id === focus.id))
}

export function scopeStrategyIds(focus: TradingFocus | null, workspaces: SessionWorkspace[]) {
  if (!focus) return null
  if (focus.kind === 'strategy') return new Set([focus.id])
  const workspace = workspaces.find((row) => row.id === focus.id)
  return new Set([...(workspace?.strategy_ids || []), ...(workspace?.history_strategies || []).map((row) => row.id)])
}

export function orderOwner(order: PortfolioTradeState['orders'][number], trades: PortfolioTradeState | null) {
  return order.agent_id || trades?.positions.find((position) => position.id === order.position_id)?.agent_id || null
}

export function strategyOperatingState(row: StrategyResult, service?: ServiceConnectionState) {
  if (row.archived) return 'Archived'
  const session = row.session
  if (!session) return 'Status unavailable'
  if (session.tracking.fault) return 'Needs attention'
  const trading = service?.trading
  const now = Date.now() / 1000
  const active = service?.phase === 'connected' && !service.identityMismatch && service.service?.health === 'healthy'
    && service.checked_at !== null && now - service.checked_at >= 0 && now - service.checked_at < 15
    && trading?.session_id === row.id && trading.sender === row.workspace?.wallet_sender
    && trading.network === row.workspace?.wallet_network && trading.signer_state === 'unlocked'
    && trading.authorization_status === 'active' && !trading.recovery_required
    && (trading.authorization_expires_at ?? 0) > now && (trading.signer_expires_at ?? 0) > now
  if (active) return trading.entries_paused || session.instance.entry_mode === 'disabled' ? 'Entries paused' : 'Trading'
  if (!session.tracking.enabled) return 'Stopped'
  if (session.instance.entry_mode === 'disabled' && row.workspace?.mode === 'trade') return 'Entries paused'
  if (session.brief.execution_style === 'autopilot' || session.brief.mode === 'trade-autopilot') return 'Awaiting approval'
  return 'Monitoring'
}

export function aggregatePerformance(rows: StrategyResult[], trades: PortfolioTradeState | null, ids: Set<string> | null) {
  const applicable = rows.filter(isTradingResult)
  // Manual/unassigned Runner positions need their own accounting. Never silently
  // omit them from a wallet-wide total or treat a missing trade read as zero.
  const unattributed = !ids && trades?.positions.some((position) => !position.agent_id || !rows.some((row) => row.id === position.agent_id))
  const ready = Boolean(trades) && !unattributed
  const sum = (field: 'realized_lamports' | 'unrealized_lamports' | 'total_lamports') => ready && applicable.every((row) => row.performance?.[field] != null) ? applicable.reduce((total, row) => total + BigInt(row.performance![field]!), 0n).toString() : null
  return { realized: sum('realized_lamports'), unrealized: sum('unrealized_lamports'), total: sum('total_lamports'), detail: unattributed ? 'Manual or independent trades are outside the session comparison.' : sum('total_lamports') !== null ? 'Since start · recorded fees included · before future exit costs' : 'Needs complete trading records, receipt costs and fresh marks.' }
}

export function signedSol(value: string | null | undefined) {
  if (value == null) return 'Unavailable'
  const amount = BigInt(value)
  const magnitude = amount < 0n ? -amount : amount
  const whole = magnitude / 1_000_000_000n
  const fraction = (magnitude % 1_000_000_000n).toString().padStart(9, '0').replace(/0+$/, '')
  return `${amount > 0n ? '+' : amount < 0n ? '−' : ''}${whole}${fraction ? `.${fraction}` : ''} SOL`
}
