import { useEffect, useState } from 'react'
import { api, errorText } from './browserWallet'

type Connection = { network: string; source: 'saved' | 'environment' | 'public'; provider: string }

export function RpcConnection({ close, changed }: { close: () => void; changed: () => void }) {
  const [connection, setConnection] = useState<Connection | null>(null)
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  useEffect(() => {
    let active = true
    void api<{ connection: Connection }>('/api/v1/rpc').then((result) => { if (active) setConnection(result.connection) })
      .catch((failure) => { if (active) setError(errorText(failure)) })
    return () => { active = false }
  }, [])
  const run = async (action: 'save' | 'test' | 'reset') => {
    if (busy) return
    setBusy(true); setError(''); setNotice('')
    const entered = url.trim()
    setUrl('')
    try {
      const result = await api<{ connection: Connection; notice: string }>('/api/v1/rpc', action === 'save' ? { action, url: entered } : { action })
      setConnection(result.connection); setNotice(result.notice)
      changed()
    } catch (failure) { setError(errorText(failure)) }
    finally { setBusy(false) }
  }
  return <div className="modal-backdrop"><section className="strategy-modal rpc-connection" role="dialog" aria-modal="true" aria-label="RPC connection">
    <button className="close" aria-label="Close dialog" onClick={close} disabled={busy}>×</button>
    <small>SOLANA MAINNET</small><h2>RPC connection</h2>
    <p>Runner uses this connection for wallet balances, order reviews, submissions and receipt checks.</p>
    <p>Current connection: <strong>{connection?.provider || 'Loading…'}</strong>{connection && ` · ${connection.source === 'saved' ? 'saved on this computer' : connection.source === 'environment' ? 'startup configuration' : 'shared public endpoint'}`}</p>
    <p>For a free provider account, open the <a href="https://dashboard.helius.dev/" target="_blank" rel="noreferrer">Helius dashboard</a>, choose Free, then copy its Mainnet RPC URL here.</p>
    <div className="setup-fields"><label>Mainnet RPC URL<input type="password" value={url} onChange={(event) => setUrl(event.target.value)} disabled={busy} autoComplete="off" spellCheck={false} placeholder="https://mainnet.helius-rpc.com/?api-key=…" /></label></div>
    <p>The URL may contain your API key. It is saved in an owner-readable local settings file and is never returned in diagnostics or stored in browser storage.</p>
    {error && <p className="error" role="alert">{error}</p>}
    <p role="status">{busy ? 'Checking the connection…' : notice}</p>
    <div className="modal-actions">
      <button onClick={() => void run('test')} disabled={busy || !connection}>Test connection</button>
      {connection?.source === 'saved' && <button onClick={() => void run('reset')} disabled={busy}>Remove saved connection</button>}
      <button className="primary" onClick={() => void run('save')} disabled={busy || !url.trim()}>Test & save</button>
    </div>
    <p>Testing reads Mainnet identity and a current blockhash. It does not sign or send a transaction. Provider quotas still apply.</p>
  </section></div>
}
