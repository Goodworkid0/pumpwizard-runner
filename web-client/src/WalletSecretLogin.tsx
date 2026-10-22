import { useEffect, useRef, useState } from 'react'
import { errorText, signInWithImportedWallet, storedWallets } from './browserWallet'
import type { WalletNetwork, WalletSession } from './browserWallet'
import { privateKeyWallet, recoveryDerivationPath, recoveryPhraseWallet, toBase58 } from './walletSecrets'
import type { LocalKeypair, RecoveryPath } from './walletSecrets'

export function WalletSecretLogin({ network, onReady, onBack }: { network: WalletNetwork; onReady: (session: WalletSession) => void; onBack: () => void }) {
  const [source, setSource] = useState<'key' | 'phrase'>('key')
  const [secret, setSecret] = useState('')
  const [revealed, setRevealed] = useState(false)
  const [passphrase, setPassphrase] = useState('')
  const [path, setPath] = useState<RecoveryPath>('bip44-change')
  const [account, setAccount] = useState('0')
  const [address, setAddress] = useState('')
  const [derivedPath, setDerivedPath] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const pair = useRef<LocalKeypair | null>(null)
  const revision = useRef(0)
  const operation = useRef(false)
  const alreadySaved = Boolean(address) && storedWallets().some((wallet) => wallet.address === address)

  useEffect(() => () => {
    revision.current++
    pair.current?.secretKey.fill(0)
    pair.current = null
  }, [])

  const clear = () => {
    revision.current++
    pair.current?.secretKey.fill(0)
    pair.current = null
    setSecret(''); setPassphrase(''); setRevealed(false)
    setPassword(''); setConfirmation(''); setAddress(''); setDerivedPath(''); setMessage('')
  }

  const review = async () => {
    if (operation.current) return
    operation.current = true
    setBusy(true); setMessage('Checking the wallet…')
    const current = revision.current
    try {
      if (source === 'phrase' && path !== 'legacy' && !/^\d+$/.test(account)) throw new Error('Use an account index from 0 to 99.')
      const accountIndex = path === 'legacy' ? 0 : Number(account)
      const prepared = source === 'key' ? privateKeyWallet(secret) : await recoveryPhraseWallet(secret, passphrase, path, accountIndex)
      if (current !== revision.current) { prepared.secretKey.fill(0); return }
      pair.current = prepared
      setAddress(toBase58(prepared.publicKey))
      setDerivedPath(source === 'phrase' ? recoveryDerivationPath(path, accountIndex) : '')
      setSecret(''); setPassphrase(''); setRevealed(false); setMessage('')
    } catch (error) {
      if (current === revision.current) setMessage(errorText(error))
    } finally {
      operation.current = false
      if (current === revision.current) setBusy(false)
    }
  }

  const signIn = async () => {
    if (operation.current || !pair.current) return
    if (!alreadySaved && password !== confirmation) { setMessage('The two passwords do not match.'); return }
    operation.current = true
    setBusy(true); setMessage(alreadySaved ? 'Proving wallet ownership…' : 'Saving the encrypted wallet and signing in…')
    const current = revision.current
    // The sign-in helper now owns this keypair, including wiping it on failure.
    const prepared = pair.current
    pair.current = null
    try {
      const session = await signInWithImportedWallet(prepared, password, network)
      if (current !== revision.current) { await session.disconnect?.(); return }
      setPassword(''); setConfirmation('')
      onReady(session)
    } catch (error) {
      if (current === revision.current) {
        setAddress(''); setDerivedPath(''); setPassword(''); setConfirmation('')
        setMessage(`${errorText(error)} Enter your private key or recovery phrase again to retry.`)
      }
    } finally {
      operation.current = false
      if (current === revision.current) setBusy(false)
    }
  }

  return <div className="wallet-form secret-login">
    {!address ? <>
      <div className="secret-source" role="group" aria-label="Sign-in method">
        <button type="button" aria-pressed={source === 'key'} onClick={() => { clear(); setSource('key') }} disabled={busy}>Private key</button>
        <button type="button" aria-pressed={source === 'phrase'} onClick={() => { clear(); setSource('phrase') }} disabled={busy}>Recovery phrase</button>
      </div>
      <label>{source === 'key' ? 'Solana private key' : 'Recovery phrase'}
        <input type={revealed ? 'text' : 'password'} autoComplete="off" autoCapitalize="off" spellCheck={false} maxLength={2048} value={secret} onChange={(event) => setSecret(event.target.value)} onPaste={(event) => { event.preventDefault(); setSecret(event.clipboardData.getData('text').replace(/\s+/g, ' ').trim()) }} aria-describedby="wallet-secret-hint" disabled={busy} />
      </label>
      <div className="secret-input-note"><small id="wallet-secret-hint">{source === 'key' ? 'Paste a base58 private key or a 64-byte Solana JSON keypair.' : 'Paste an English recovery phrase of 12, 15, 18, 21 or 24 words.'}</small><button type="button" onClick={() => setRevealed(!revealed)} disabled={busy} aria-label={revealed ? 'Hide secret' : 'Show secret'}>{revealed ? 'Hide' : 'Show'}</button></div>
      {source === 'phrase' && <details className="recovery-options"><summary>Advanced recovery — address doesn’t match?</summary><div>
        <small>Use the defaults first. Change these settings only if the address preview differs from your original wallet.</small>
        <label>Derivation path<select value={path} onChange={(event) => setPath(event.target.value as RecoveryPath)} disabled={busy}><option value="bip44-change">m/44′/501′/account′/0′ (default)</option><option value="bip44">m/44′/501′/account′</option><option value="legacy">m/44′/501′ (legacy)</option></select></label>
        {path !== 'legacy' && <label>Wallet account<select value={account} onChange={(event) => setAccount(event.target.value)} disabled={busy}>{Array.from({ length: 100 }, (_, index) => <option key={index} value={String(index)}>Account {index + 1}{index === 0 ? ' (default)' : ''}</option>)}</select><small>Choose the account you used in your original wallet. Start with Account 1.</small></label>}
        <label>Recovery passphrase (optional)<input type="password" autoComplete="off" maxLength={1024} value={passphrase} onChange={(event) => setPassphrase(event.target.value)} disabled={busy} /><small>Leave empty unless your original wallet used a BIP39 passphrase. Your PumpWizard password is entered later.</small></label>
      </div></details>}
      <p>Your secret is used temporarily to unlock this wallet. The local server saves only a copy encrypted with your password.</p>
      <div className="wallet-form-actions"><button onClick={() => { clear(); onBack() }} disabled={busy}>Back</button><button className="primary" onClick={review} disabled={busy || !secret.trim()}>Review wallet</button></div>
    </> : <>
      <div className="stored-address"><small>WALLET ADDRESS</small><code>{address}</code>{derivedPath && <small>{path === 'legacy' ? 'Legacy wallet' : `Account ${Number(account) + 1}`}</small>}</div>
      <p>Check that this is your wallet address. {derivedPath && 'If it differs, go back and open Advanced recovery to choose your original wallet settings.'}</p>
      {derivedPath && <details className="recovery-options"><summary>Advanced recovery details</summary><div><small>Derivation path: {derivedPath}</small></div></details>}
      {alreadySaved ? <p>This wallet is already saved. Its existing password and encrypted record will be kept.</p> : <>
        <label>New wallet password<input type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy} /></label>
        <label>Confirm wallet password<input type="password" autoComplete="new-password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} disabled={busy} /></label>
        <p>Use at least 10 characters. You’ll use this password to unlock the saved wallet next time.</p>
      </>}
      <div className="wallet-form-actions"><button onClick={clear} disabled={busy}>Back to secret entry</button><button className="primary" onClick={signIn} disabled={busy || (!alreadySaved && (password.length < 10 || !confirmation))}>{alreadySaved ? 'Sign in' : 'Save & sign in'}</button></div>
    </>}
    {message && <p className="wallet-status" role="status" aria-live="polite">{message}</p>}
  </div>
}
