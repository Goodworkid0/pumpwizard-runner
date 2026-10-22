import { useCallback, useEffect, useState } from 'react'
import { api, errorText } from './browserWallet'
import type { WalletInspection, WalletSession } from './browserWallet'
import type { PortfolioTradeState, SessionWorkspace } from './PortfolioWorkspace'
import { WalletTrading } from './WalletTrading'
import { tokenQuantity } from './tradeFormatting'

type Position = PortfolioTradeState['positions'][number]
type SellList = { positions: Position[]; pending: boolean }

export function SessionSellAll({ workspace, wallet, scopeQuery, trades, funds, refreshing, recordsError, refresh, pause, unlock, close }: {
  workspace: SessionWorkspace
  wallet: WalletSession
  scopeQuery: string
  trades: PortfolioTradeState | null
  funds: WalletInspection | null
  refreshing: boolean
  recordsError: string
  refresh: () => Promise<void>
  pause: () => Promise<void>
  unlock: () => void
  close: () => void
}) {
  const [list, setList] = useState<SellList | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [started, setStarted] = useState(false)
  const [selected, setSelected] = useState<Position | null>(null)
  const [orderBusy, setOrderBusy] = useState(false)
  const load = useCallback(async () => {
    const result = await api<SellList>(`/api/v1/session-workspaces/${encodeURIComponent(workspace.id)}/sell-all${scopeQuery}`)
    setList(result)
    return result
  }, [workspace.id, scopeQuery])
  useEffect(() => { let active = true; load().catch((reason) => { if (active) setError(errorText(reason)) }); return () => { active = false } }, [load])
  const inspect = async (position: Position) => {
    if (!wallet.signingAvailable) { unlock(); return }
    setBusy(true)
    setError('')
    try {
      await pause()
      setStarted(true)
      const fresh = await load()
      if (fresh.pending) throw new Error('An original wallet transaction is still pending. Wait for its outcome before selling.')
      const current = fresh.positions.find((item) => item.id === position.id)
      if (!current) { await refresh(); return }
      if (current.decimals === null) throw new Error('This position has no finalized receipt quantity. Refresh its original receipt before selling.')
      // Explicit user handoff for a manual liquidation keeps purchase attribution.
      if (current.managed) await api('/api/v1/agent-orders/release-position', { sender: wallet.address, network: wallet.network, position_id: current.id, acknowledge: 'release-managed-exit' })
      await refresh()
      setSelected(current)
    } catch (reason) { setError(errorText(reason)) } finally { setBusy(false) }
  }
  const begin = async () => {
    const first = list?.positions.find((position) => position.decimals !== null)
    if (first) await inspect(first)
  }
  const back = async () => {
    setSelected(null)
    setBusy(true)
    try { await refresh(); await load() } catch (reason) { setError(errorText(reason)) } finally { setBusy(false) }
  }
  const current = selected && trades?.positions.find((position) => position.id === selected.id)
  const pending = Boolean(list?.pending || trades?.pending)
  return <div className="modal-backdrop" role="presentation"><section className="strategy-modal session-sell-all" role="dialog" aria-modal="true" aria-label="Sell all session positions">
    <button className="close" aria-label="Close sell all" disabled={busy || orderBusy} onClick={close}>×</button>
    <small>SESSION CONTROLS · {workspace.label}</small><h2>Sell all positions</h2>
    <p>{selected ? 'Review the full position, approve its sale, then continue with the remaining positions.' : 'Sell the live positions bought by this session’s strategies. Each sale has a fresh quote and a separate wallet approval.'}</p>
    {!selected && <p>Starting stops this session’s strategies while you review sales. Managed positions transfer to manual exit as you review them. Closing this window leaves strategies stopped.</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {selected ? <>
      <div className="session-sell-progress"><span>{current?.status === 'closed' ? 'Position sold · finalized' : pending ? 'Waiting for the original receipt…' : 'Full position selected'}</span><button disabled={busy || orderBusy || pending} onClick={() => void back()}>{current?.status === 'closed' ? 'Continue to remaining positions' : 'Back to remaining positions'}</button></div>
      <WalletTrading key={selected.id} embedded sellOnly onBusyChange={setOrderBusy} session={wallet} initialMint={selected.mint} initialPositionId={selected.id} initialSellQuantity={tokenQuantity(selected.quantity, selected.decimals, false)} trades={trades} funds={funds} refreshRecords={refresh} refreshingRecords={refreshing} recordsError={recordsError} close={() => void back()} />
    </> : <>
      {!list ? <p role="status">Loading session positions…</p> : !list.positions.length ? <p role="status">No open live positions in this session.</p> : <div className="session-sell-list">{list.positions.map((position) => <article key={position.id}><span><b>{position.mint.slice(0, 6)}…{position.mint.slice(-6)}</b><code>{position.mint}</code><small>{position.decimals === null ? 'Receipt quantity unavailable' : `${tokenQuantity(position.quantity, position.decimals)} tokens`} · {position.managed ? 'Strategy-managed' : 'Manual exit'}</small></span>{started && <button disabled={busy || pending || position.decimals === null} onClick={() => void inspect(position)}>Review sell</button>}</article>)}</div>}
      {pending && <p role="status">Another wallet transaction is unresolved. Runner checks its original receipt before allowing a sale.</p>}
      <footer className="modal-actions"><button disabled={busy} onClick={() => void refresh().then(load).catch((reason) => setError(errorText(reason)))}>Refresh positions</button><button disabled={busy} onClick={close}>Done</button>{Boolean(list?.positions.length) && !started && <button className="primary" disabled={busy || pending || !list?.positions.some((position) => position.decimals !== null)} onClick={() => void begin()}>{busy ? 'Preparing…' : wallet.signingAvailable ? 'Stop strategies & review sells' : 'Unlock wallet to continue'}</button>}</footer>
    </>}
  </section></div>
}
