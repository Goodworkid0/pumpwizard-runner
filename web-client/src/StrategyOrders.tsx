import { useCallback, useEffect, useState } from 'react'
import { api, errorText, formatSol } from './browserWallet'
import type { WalletSession } from './browserWallet'
import type { PortfolioTradeState } from './PortfolioWorkspace'
import type { StrategyOrder } from './WalletTrading'

type Session = { id: string; label: string; eligible: boolean; entry_mode: string; entry_blocks: string[]; metrics: { allocated_lamports: number }; decisions: { mint: string; reason: string; at: number }[]; reviews: { id: string; mint: string; side: string }[] }

export function StrategyOrders({ wallet, trades, close, review, refresh }: { wallet: WalletSession; trades: PortfolioTradeState | null; close: () => void; review: (order: StrategyOrder) => void; refresh: () => Promise<void> }) {
  const [sessions, setSessions] = useState<Session[]>([])
  const [message, setMessage] = useState('Loading decisions and reservations…')
  const [busy, setBusy] = useState(false)
  const [release, setRelease] = useState<string | null>(null)
  const identity = { sender: wallet.address, network: wallet.network }
  const load = useCallback(async () => {
    const data = await api<{ sessions: Session[] }>(`/api/v1/agent-orders?${new URLSearchParams({ sender: wallet.address, network: wallet.network })}`)
    setSessions(data.sessions)
  }, [wallet.address, wallet.network])
  useEffect(() => { void load().then(() => setMessage('Only the pinned Core Frequency strategy can prepare reviewed orders. Every signature still needs your approval.')).catch((error) => setMessage(errorText(error))) }, [load])
  const act = async (action: string, values: Record<string, unknown>) => {
    if (busy) return
    setBusy(true)
    try { await api(`/api/v1/agent-orders/${action}`, { ...identity, ...values }); await load(); await refresh(); setRelease(null); setMessage('Saved. Review a fresh order before signing.') }
    catch (error) { setMessage(errorText(error)) }
    finally { setBusy(false) }
  }
  return <div className="modal-backdrop"><section className="strategy-modal control-modal" role="dialog" aria-modal="true" aria-label="Strategy orders"><button className="close" aria-label="Close dialog" onClick={close} disabled={busy}>×</button><h2>Strategy orders</h2><p>Entries can be paused independently. Existing positions keep their exit checks. A managed exit review only opens when the current executable quote meets an exit rule.</p><div className="control-list">{sessions.map((session) => <article data-session-id={session.id} key={session.id}><h3>{session.label}</h3><p>{formatSol(session.metrics.allocated_lamports)} assigned · {session.eligible ? 'Reviewed trading supported' : 'Monitoring only'}</p><button disabled={busy || !session.eligible} onClick={() => void act('entries', { agent_id: session.id, enabled: session.entry_mode === 'disabled' })}>{session.entry_mode === 'disabled' ? 'Enable reviewed entries' : 'Pause entries'}</button>{session.entry_blocks.length > 0 && <p>{session.entry_blocks.join(' · ')}</p>}{session.reviews.map((row) => <p key={row.id}>Unsubmitted {row.side} review · {row.mint.slice(0, 10)} <button disabled={busy} onClick={() => void act('cancel', { preview_id: row.id })}>Cancel retained review</button></p>)}{!session.decisions.length && <p>No recent entry recommendations.</p>}{session.decisions.map((decision) => <p key={decision.mint}><code>{decision.mint}</code> · {decision.reason.replace(/_/g, ' ')} <button disabled={busy || !session.eligible || session.entry_mode === 'disabled'} onClick={() => review({ agentId: session.id, label: session.label, mint: decision.mint })}>Review entry</button></p>)}{trades?.positions.filter((p) => p.agent_id === session.id && p.status === 'open').map((position) => <div key={position.id}><code>{position.mint}</code>{position.managed ? <><button disabled={busy} onClick={() => review({ agentId: session.id, label: session.label, mint: position.mint, positionId: position.id })}>Check managed exit</button><button disabled={busy} onClick={() => setRelease(position.id)}>Take over exit manually</button>{release === position.id && <p>Release this position’s token reservation and stop its managed exits? Ownership and past receipts remain attributed to the strategy. You must then review and sign a separate manual sell. <button disabled={busy} onClick={() => void act('release-position', { position_id: position.id, acknowledge: 'release-managed-exit' })}>Release position for manual exit</button><button onClick={() => setRelease(null)}>Keep managed</button></p>}</> : <p>Released for manual exit in Portfolio.</p>}</div>)}</article>)}</div><p role="status">{message}</p></section></div>
}
