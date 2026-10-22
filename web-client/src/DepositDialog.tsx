import { useEffect, useState } from 'react'
import QRCode from 'qrcode'
import { errorText, exportBrowserWalletSecret, formatSol, inspectWallet } from './browserWallet'
import type { WalletSession } from './browserWallet'

type ExportedSecret = { address: string; base58: string; json: string }

function downloadKeypair(secret: ExportedSecret) {
  const url = URL.createObjectURL(new Blob([secret.json], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `pumpwizard-keypair-${secret.address.slice(0, 8)}.json`
  link.click()
  URL.revokeObjectURL(url)
}

function KeyExportDialog({ address, close }: { address: string; close: () => void }) {
  const [password, setPassword] = useState('')
  const [secret, setSecret] = useState<ExportedSecret | null>(null)
  const [message, setMessage] = useState('Enter the wallet password to decrypt the key in this browser.')
  const [busy, setBusy] = useState(false)

  const reveal = async () => {
    setBusy(true)
    try {
      setSecret(await exportBrowserWalletSecret(address, password))
      setPassword('')
      setMessage('Private key revealed. Close this window as soon as the export is complete.')
    } catch (error) {
      setMessage(errorText(error))
    } finally {
      setBusy(false)
    }
  }

  const copyKey = async () => {
    if (!secret) return
    await navigator.clipboard.writeText(secret.base58)
    setMessage('Base58 private key copied to the clipboard.')
  }

  return <div className="modal-backdrop key-export-layer" role="presentation" onMouseDown={busy ? undefined : close}>
    <section className="strategy-modal key-export-modal" role="dialog" aria-modal="true" aria-label="Export private key" onMouseDown={(event) => event.stopPropagation()}>
      <button className="close" aria-label="Close private key export" onClick={close} disabled={busy}>×</button>
      <small>WALLET RECOVERY</small>
      <h2>Export private key</h2>
      <p className="key-export-warning"><b>Anyone with this key can control the wallet.</b><span>Export it only for migration or recovery, then store it offline.</span></p>
      {!secret ? <div className="key-password-form">
        <label>Wallet password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && password && !busy) reveal() }} disabled={busy} autoFocus /></label>
        <div className="modal-actions"><button onClick={close} disabled={busy}>Cancel</button><button className="primary" onClick={reveal} disabled={busy || !password}>{busy ? 'Decrypting…' : 'Reveal private key'}</button></div>
      </div> : <div className="revealed-key">
        <label>Base58 private key<textarea readOnly value={secret.base58} rows={3} /></label>
        <p>The JSON download contains the same 64-byte keypair in Solana CLI format.</p>
        <div className="key-export-actions"><button onClick={copyKey}>Copy base58 key</button><button onClick={() => downloadKeypair(secret)}>Download JSON keypair</button><button className="primary" onClick={close}>Done</button></div>
      </div>}
      <p className="wallet-status" aria-live="polite">{message}</p>
    </section>
  </div>
}

export function DepositDialog({ session, close, onBalance }: { session: WalletSession; close: () => void; onBalance: (balance: string) => void }) {
  const [balance, setBalance] = useState(session.balance)
  const [qr, setQr] = useState('')
  const [message, setMessage] = useState(`Ready to receive deposits on ${session.network === 'mainnet-beta' ? 'Solana Mainnet' : 'Solana Devnet'}.`)
  const [busy, setBusy] = useState(false)
  const [exportOpen, setExportOpen] = useState(false)

  useEffect(() => { QRCode.toDataURL(`solana:${session.address}`, { width: 280, margin: 1, color: { dark: '#0b1020', light: '#ffffff' } }).then(setQr).catch(() => setQr('')) }, [session.address])

  const copy = async () => {
    await navigator.clipboard.writeText(session.address)
    setMessage('Deposit address copied.')
  }

  const refresh = async () => {
    setBusy(true)
    try {
      const result = await inspectWallet(session.address, session.network)
      setBalance(result.balance)
      onBalance(result.balance)
      setMessage('Balance refreshed.')
    } catch (error) { setMessage(errorText(error)) } finally { setBusy(false) }
  }

  const network = session.network === 'mainnet-beta' ? 'Solana Mainnet' : 'Solana Devnet'

  return <>
    <div className="modal-backdrop" role="presentation" onMouseDown={close}>
      <section className="strategy-modal deposit-modal" role="dialog" aria-modal="true" aria-label="Deposit SOL" onMouseDown={(event) => event.stopPropagation()}>
        <button className="close" aria-label="Close dialog" onClick={close}>×</button>
        <small>ADD FUNDS</small>
        <h2>Deposit SOL</h2>
        <p className="deposit-intro">Scan the QR code or copy your wallet address.</p>
        <div className="deposit-network"><span className="deposit-network-mark">≋</span><span><b>{network}</b><small>Selected deposit network</small></span></div>
        <div className="deposit-layout">
          {qr ? <img src={qr} width="220" height="220" alt={`QR code for ${session.address}`} /> : <div className="qr-placeholder">QR unavailable</div>}
          <div>
            <span className="deposit-label">WALLET ADDRESS</span>
            <code className="deposit-address">{session.address}</code>
            <div className="deposit-actions"><button className="primary" onClick={copy}>Copy address</button><button onClick={refresh} disabled={busy}>{busy ? 'Refreshing…' : 'Refresh balance'}</button></div>
            <dl><div><dt>Balance</dt><dd>{formatSol(balance)}</dd></div><div><dt>Wallet</dt><dd className="deposit-wallet-name"><span>{session.kind === 'local' ? 'PumpWizard Wallet' : session.name}</span>{session.kind === 'local' && <button onClick={() => setExportOpen(true)}>Export key</button>}</dd></div></dl>
          </div>
        </div>
        <p className="deposit-warning">Only send SOL and Solana tokens through {network}. Transfers sent through another network may not arrive in this wallet.</p>
        <p className="wallet-status deposit-status" aria-live="polite">{message}</p>
      </section>
    </div>
    {exportOpen && <KeyExportDialog address={session.address} close={() => setExportOpen(false)} />}
  </>
}
