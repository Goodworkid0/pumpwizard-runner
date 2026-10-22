import { useState } from 'react'
import {
  connectStandardWallet,
  detectedWallets,
  errorText,
  storedWallets,
  unlockBrowserWallet,
} from './browserWallet'
import type { WalletSession } from './browserWallet'

export function WalletSessionUnlock({
  session,
  close,
  unlocked,
}: {
  session: WalletSession
  close: () => void
  unlocked: (session: WalletSession) => void
}) {
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(session.kind === 'local'
    ? 'Enter the generated wallet password. It is used only in memory and is not added to the restored view session.'
    : `Reconnect ${session.name} to restore transaction signing.`)

  const resume = async () => {
    setBusy(true)
    try {
      if (session.kind === 'local') {
        const vault = storedWallets().find((item) => item.address === session.address)
        if (!vault) throw new Error('The encrypted wallet is no longer saved in the local app.')
        unlocked(await unlockBrowserWallet(vault, password, session.network))
        return
      }
      const wallet = detectedWallets().find((item) => item.name === session.name
        && item.accounts.some((account) => account.address === session.address))
        || detectedWallets().find((item) => item.name === session.name)
      if (!wallet) throw new Error(`${session.name} is not available. Unlock the extension and try again.`)
      unlocked(await connectStandardWallet(wallet, session.network))
    } catch (error) {
      setMessage(errorText(error))
    } finally {
      setBusy(false)
    }
  }

  return <div className="modal-backdrop wallet-session-layer" role="presentation" onMouseDown={busy ? undefined : close}>
    <section className="strategy-modal wallet-session-unlock" role="dialog" aria-modal="true" aria-label="Unlock wallet signing" onMouseDown={(event) => event.stopPropagation()}>
      <button className="close" aria-label="Close signing unlock" onClick={close} disabled={busy}>×</button>
      <small>RESTORED VIEW SESSION</small>
      <h2>Unlock wallet signing</h2>
      <p>Your terminal session survived the refresh. Signing was intentionally locked because PumpWizard does not store a plaintext private key or wallet password.</p>
      {session.kind === 'local' && <p className="session-plan-note">Unlocking restores interactive signing only. Any Autopilot runtime ended on refresh and needs a fresh allowance review and explicit activation.</p>}
      <div className="session-wallet-identity"><span><small>WALLET</small><b>{session.name}</b></span><code>{session.address}</code></div>
      {session.kind === 'local' && <label className="session-wallet-password">Wallet password<input type="password" autoComplete="current-password" value={password} autoFocus disabled={busy} onChange={(event) => setPassword(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && password && !busy) void resume() }} /></label>}
      <p className="wallet-status" role="status">{busy ? 'Restoring signing… ' : ''}{message}</p>
      <div className="modal-actions"><button onClick={close} disabled={busy}>Keep view-only</button><button className="primary" onClick={() => void resume()} disabled={busy || (session.kind === 'local' && !password)}>{session.kind === 'local' ? 'Unlock signing' : `Reconnect ${session.name}`}</button></div>
    </section>
  </div>
}
