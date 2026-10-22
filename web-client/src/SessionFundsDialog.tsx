import { useCallback, useEffect, useRef, useState } from 'react'
import { api, errorText, formatSol } from './browserWallet'
import type { WalletSession } from './browserWallet'
import type { SessionWorkspace } from './PortfolioWorkspace'

type Funds = {
  balance_lamports: string
  allocation_lamports: string
  available_lamports: string
  committed_lamports: string
  unallocated_lamports: string
  maximum_allocation_lamports: string
  wallet_revision: number
}
const exact = (lamports: bigint) => `${lamports / 1000000000n}.${(lamports % 1000000000n).toString().padStart(9, '0')}`.replace(/\.?0+$/, '')
function parse(value: string): bigint | null {
  if (!/^\d+(\.\d{1,9})?$/.test(value)) return null
  const [whole, fraction = ''] = value.split('.')
  return BigInt(whole) * 1000000000n + BigInt(fraction.padEnd(9, '0'))
}

export function SessionFundsDialog({ workspace, wallet, scopeQuery, pause, changed, close }: {
  workspace: SessionWorkspace
  wallet: WalletSession
  scopeQuery: string
  pause: () => Promise<void>
  changed: () => void
  close: () => void
}) {
  const [funds, setFunds] = useState<Funds | null>(null)
  const [amount, setAmount] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [checkedAt, setCheckedAt] = useState<Date | null>(null)
  const walletRef = useRef(wallet)
  walletRef.current = wallet
  const path = `/api/v1/session-workspaces/${encodeURIComponent(workspace.id)}/funds${scopeQuery}`
  const load = useCallback(async () => {
    walletRef.current.assertActive()
    const result = await api<{ funds: Funds }>(path)
    walletRef.current.assertActive()
    setFunds(result.funds)
    setCheckedAt(new Date())
    setAmount(exact(BigInt(result.funds.allocation_lamports)))
  }, [path])
  useEffect(() => { let active = true; load().catch((reason) => { if (active) setError(errorText(reason)) }); return () => { active = false } }, [load])
  const requested = parse(amount)
  const delta = requested !== null && funds ? requested - BigInt(funds.allocation_lamports) : null
  const cashAfter = funds && delta !== null ? BigInt(funds.available_lamports) + delta : null
  const walletAfter = funds && delta !== null ? BigInt(funds.unallocated_lamports) - delta : null
  const valid = Boolean(funds && requested !== null && requested > 0n && requested <= BigInt(funds.maximum_allocation_lamports) && cashAfter !== null && cashAfter >= 0n && delta !== 0n)
  const run = async (action: () => Promise<void>) => {
    setBusy(true); setError(''); setNotice('')
    try { await action() } catch (reason) { setError(errorText(reason)) } finally { setBusy(false) }
  }
  const save = () => run(async () => {
    if (!funds || !valid) return
    wallet.assertActive()
    const result = await api<{ notice: string }>(path, { allocation_sol: amount, expected_allocation_lamports: funds.allocation_lamports, wallet_revision: funds.wallet_revision })
    wallet.assertActive()
    changed()
    await load()
    setNotice(result.notice)
  })
  return <div className="modal-backdrop" role="presentation"><section className="strategy-modal session-funds-dialog" role="dialog" aria-modal="true" aria-label="Edit session funds">
    <button className="close" aria-label="Close session funds" disabled={busy} onClick={close}>×</button>
    <header className="session-funds-heading"><small>SESSION FUNDS · {workspace.label}</small><h2>Adjust trading funds</h2>
      <p>Allocate more SOL from this wallet, or release available cash back to it.</p></header>
    <div className="session-funds-body">
    {funds ? <>
      <section className="session-funds-wallet" aria-label="Wallet funds">
        <div><small>Confirmed wallet balance</small><b>{formatSol(funds.balance_lamports)}</b></div>
        <div className="session-funds-wallet-available"><small>Available to allocate</small><b>{formatSol(funds.unallocated_lamports)}</b></div>
        <p>Available SOL excludes funds already reserved for sessions and orders.{checkedAt && <time dateTime={checkedAt.toISOString()}>Checked {checkedAt.toLocaleTimeString()}</time>}</p>
      </section>
      <div className="session-funds-summary"><div><small>Allocated</small><b>{formatSol(funds.allocation_lamports)}</b></div><div><small>Available for trades</small><b>{formatSol(funds.available_lamports)}</b></div><div><small>In managed positions</small><b>{formatSol(funds.committed_lamports)}</b></div></div>
      <div className="session-funds-editor"><label>Session allocation (SOL)<input aria-label="Session allocation (SOL)" inputMode="decimal" value={amount} disabled={busy} onChange={(e) => { setAmount(e.target.value); setNotice('') }} /></label>
        <div className="allocation-presets" aria-label="Top-up presets">{[25, 50, 100].map((percent) => <button key={percent} disabled={busy || BigInt(funds.unallocated_lamports) === 0n} onClick={() => setAmount(exact(BigInt(funds.allocation_lamports) + BigInt(funds.unallocated_lamports) * BigInt(percent) / 100n))}>{percent === 100 ? 'Add available SOL' : `Add ${percent}% available`}</button>)}</div>
        <p>Maximum session allocation: {formatSol(funds.maximum_allocation_lamports)}</p>
        {delta !== null && cashAfter !== null && cashAfter >= 0n && walletAfter !== null && walletAfter >= 0n && <div className="session-funds-preview"><span>{delta > 0n ? 'Add to session' : delta < 0n ? 'Release to wallet' : 'No change'}<b>{formatSol((delta < 0n ? -delta : delta).toString())}</b></span><span>Available for trades after change<b>{formatSol(cashAfter.toString())}</b></span><span>Unallocated in wallet after change<b>{formatSol(walletAfter.toString())}</b></span></div>}
        {walletAfter !== null && walletAfter < 0n && <p className="error">This exceeds the wallet’s available SOL. Refresh funds to check the latest balance.</p>}
        {cashAfter !== null && cashAfter < 0n && <p className="error">You can only release available cash. Funds already spent or in positions stay committed.</p>}
      </div>
      <p className="control-note">Pause strategies before saving. Per-trade and daily limits stay unchanged unless they must be reduced to fit. Trading stays paused after this change.</p>
    </> : !error && <p role="status">Loading current wallet and session funds…</p>}
    </div>
    {(error || notice) && <div className="session-funds-feedback">{error && <p className="error" role="alert">{error}</p>}{notice && <p className="session-funds-success" role="status">{notice}</p>}</div>}
    <footer className="modal-actions"><button disabled={busy} onClick={() => void run(async () => { await pause(); await load(); setNotice('Strategies stopped. You can now adjust funds.') })}>Stop strategies to edit funds</button><button disabled={busy} onClick={() => void run(load)}>Refresh funds</button><button disabled={busy} onClick={close}>Done</button><button className="primary" disabled={busy || !valid} onClick={() => void save()}>{busy ? 'Updating…' : 'Update allocation'}</button></footer>
  </section></div>
}
