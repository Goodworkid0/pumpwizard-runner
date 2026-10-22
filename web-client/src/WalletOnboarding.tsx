import { useEffect, useRef, useState } from 'react'
import type { ChangeEvent } from 'react'
import {
  chainFor,
  connectStandardWallet,
  createBrowserWallet,
  detectedWallets,
  downloadVault,
  errorText,
  importBrowserWallet,
  storedWallets,
  unlockBrowserWallet,
  watchWallets,
} from './browserWallet'
import type { EncryptedWallet, StandardWallet, WalletNetwork, WalletSession } from './browserWallet'
import { WalletSecretLogin } from './WalletSecretLogin'
import './wallet.css'

type Mode = 'choose' | 'create' | 'unlock' | 'backup' | 'secret'

export function WalletOnboarding({ version, onReady, onExplore }: { version: string; onReady: (session: WalletSession) => void; onExplore: () => void }) {
  const network: WalletNetwork = 'mainnet-beta'
  const [wallets, setWallets] = useState<readonly StandardWallet[]>([])
  const [walletScanDone, setWalletScanDone] = useState(false)
  const [vaults, setVaults] = useState<EncryptedWallet[]>(() => storedWallets())
  const [vault, setVault] = useState<EncryptedWallet | null>(null)
  const [mode, setMode] = useState<Mode>('choose')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [backupSaved, setBackupSaved] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const restoreInput = useRef<HTMLInputElement>(null)

  useEffect(() => watchWallets((available) => {
    setWallets(available)
    setWalletScanDone(true)
  }), [])

  const run = async (action: () => Promise<void>) => {
    setBusy(true)
    try { await action() } catch (error) { setMessage(errorText(error)) } finally { setBusy(false) }
  }

  const connect = (wallet: StandardWallet) => run(async () => {
    setMessage(`Waiting for ${wallet.name} connection and sign-in approval…`)
    onReady(await connectStandardWallet(wallet, network))
  })

  const scanWallets = () => {
    const available = detectedWallets()
    setWallets(available)
    setWalletScanDone(true)
    setMessage(available.length
      ? `${available.length} compatible wallet${available.length === 1 ? '' : 's'} detected automatically.`
      : 'No compatible Wallet Standard extension is available. Unlock an installed wallet and scan again.')
  }

  const create = () => run(async () => {
    if (password !== confirmation) throw new Error('The two passwords do not match.')
    setMessage('Creating and saving the encrypted wallet in the local app…')
    const created = await createBrowserWallet(password)
    setVault(created)
    setVaults(storedWallets())
    setPassword('')
    setConfirmation('')
    setBackupSaved(false)
    setMode('backup')
    setMessage('Wallet created. Save the encrypted backup, then choose which wallet to use.')
  })

  const unlock = () => run(async () => {
    if (!vault) throw new Error('No browser wallet is stored here.')
    setMessage('Decrypting locally and proving wallet ownership…')
    onReady(await unlockBrowserWallet(vault, password, network))
  })

  const restore = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) return
    file.text().then(JSON.parse).then(importBrowserWallet).then((restored) => {
      setVaults(storedWallets())
      setVault(null)
      setPassword('')
      setMode('choose')
      setMessage(`Encrypted backup restored for ${restored.address}. Choose it below to unlock, or use an extension wallet.`)
    }).catch((error) => setMessage(errorText(error))).finally(() => { event.target.value = '' })
  }

  const chooseStoredWallet = (selected: EncryptedWallet) => {
    setVault(selected)
    setPassword('')
    setMode('unlock')
    setMessage('Enter this wallet’s password to sign in.')
  }

  const finishBackup = () => {
    setVault(null)
    setBackupSaved(false)
    setMode('choose')
    setMessage('Generated wallet saved. Choose it to unlock, or connect an installed wallet.')
  }

  const shortAddress = (address: string) => `${address.slice(0, 7)}…${address.slice(-5)}`
  const availableCount = vaults.length + wallets.length
  const connectionPending = busy && (mode === 'choose' || mode === 'unlock')
  const connectionLabel = connectionPending ? 'CONNECTING' : 'NOT CONNECTED'
  const connectionDetail = connectionPending ? message : 'Choose a wallet to connect'
  const showStatus = Boolean(message) && !connectionPending
  const modeDescription = mode === 'secret'
    ? 'Sign in to an existing Solana wallet and save it encrypted in the local app.'
    : mode === 'create'
    ? 'Choose a password to encrypt this wallet on this device.'
    : mode === 'unlock'
      ? 'Enter the password for this wallet. Decryption stays in your browser.'
      : mode === 'backup'
        ? 'Save the encrypted file before you continue into the workspace.'
        : 'Use a wallet extension or unlock one saved on this device.'

  return <main className="wallet-onboarding">
    <div className="onboarding-dashboard-preview" aria-hidden="true">
      <header><div><span>✦</span><b>PumpWizard workspace</b><small>Local research workspace</small></div><div><small>WALLET BALANCE</small><b>Connect to view</b></div></header>
      <div className="onboarding-preview-grid">
        <aside><small>STRATEGY SESSIONS</small><span><i />Market watch</span><span><i />Risk limits</span><span><i />Trade review</span></aside>
        <section><nav><b>Open positions</b><span>Market activity</span><span>Trade history</span></nav><div className="onboarding-preview-metrics"><span><small>POSITIONS</small><b>—</b></span><span><small>AVAILABLE SOL</small><b>—</b></span><span><small>SESSION STATUS</small><b>Wallet required</b></span></div><div className="onboarding-preview-table"><span /><span /><span /><span /></div></section>
      </div>
    </div>
    <div className="onboarding-shade" aria-hidden="true" />
    <div className="onboarding-shell">
    <header className="onboarding-topbar"><span className="onboarding-local-status"><i />Local app running</span><a className="onboarding-version" href={version ? `https://pumpwizard.fun/community#pumpwizard-runner-${version.replace(/\./g, '-')}` : 'https://pumpwizard.fun/community'} target="_blank" rel="noreferrer">{version ? `v${version}` : 'Release notes'} ↗</a></header>
    <div className="onboarding-grid"><section className="onboarding-brand">
      <img className="onboarding-hero-wizard" src="/pumpwizard-relaxed-v1.png" alt="PumpWizard" />
      <div className="onboarding-brand-copy">
        <small className="onboarding-community-label">OPEN SOURCE · TRADER COMMUNITY</small>
        <p>An open source project and community for traders exploring Pump.fun together. Follow updates, share ideas, and help shape what comes next.</p>
        <nav className="onboarding-brand-links" aria-label="Official PumpWizard links"><a href="https://it.pumpwizard.fun" target="_blank" rel="noreferrer">Website <span>↗</span></a><a href="https://x.com/PumpWizardLab" target="_blank" rel="noreferrer">X · @PumpWizardLab <span>↗</span></a><a href="https://t.me/PumpWizardFun" target="_blank" rel="noreferrer">Telegram <span>↗</span></a></nav>
      </div>
    </section>
    <section className="onboarding-panel" aria-label="Wallet sign in">
      <div className="onboarding-heading"><small>ENTER PUMPWIZARD</small><h2>{mode === 'secret' ? 'Sign in to your wallet' : mode === 'create' ? 'Create a wallet' : mode === 'unlock' ? 'Unlock your wallet' : mode === 'backup' ? 'Save your backup' : 'Choose how to begin'}</h2><p>{mode === 'choose' ? 'Explore local research without a wallet, or connect only when you need holdings and wallet actions.' : modeDescription}</p></div>
      <div className={`mainnet-network${connectionPending ? ' pending' : ''}`}><span className="pulse" /><span><strong>Solana Mainnet</strong><small>{connectionDetail}</small></span><b>{connectionLabel}</b></div>

      {mode === 'choose' && <div className="wallet-entry-list">
        <button className="research-entry-choice" onClick={onExplore} disabled={busy}><span className="entry-icon">◎</span><span><strong>Explore research</strong><small>No wallet, deposit or AI provider required. Opening Research does not start collection.</small></span><b>Open</b></button>
        <div className="wallet-divider"><span>WALLET OPTIONAL</span></div>
        <div className="wallet-detection"><div><h3>Your wallets</h3><p>{!walletScanDone ? 'Loading saved wallets and compatible extensions…' : availableCount ? `${availableCount} wallet${availableCount === 1 ? '' : 's'} available · ${wallets.length} extension${wallets.length === 1 ? '' : 's'} detected automatically` : 'No saved or compatible wallet detected'}</p></div><button onClick={scanWallets} disabled={busy}>Scan again</button></div>
        <div className="detected-wallets" aria-label="Available wallets">
          {vaults.map((saved) => <button className="detected-wallet" key={saved.address} onClick={() => chooseStoredWallet(saved)} disabled={busy} aria-label={`Unlock PumpWizard Wallet ${saved.address}`}><span className="wallet-logo generated-wallet-logo">PW</span><span><strong>PumpWizard Wallet</strong><small>Saved in the local app · {shortAddress(saved.address)}</small></span><b>Unlock</b></button>)}
          {wallets.map((wallet, index) => {
            const connected = wallet.accounts.find((account) => account.chains.includes(chainFor(network)))
            return <button className="detected-wallet" key={`${wallet.name}-${index}`} onClick={() => connect(wallet)} disabled={busy} aria-label={`Connect ${wallet.name}`}><span className="wallet-logo">{wallet.icon ? <img src={wallet.icon} alt="" /> : wallet.name.slice(0, 1).toUpperCase()}</span><span><strong>{wallet.name}</strong><small>{connected ? `Connected in extension · ${shortAddress(connected.address)}` : 'Wallet Standard · ready to connect'}</small></span><b>{connected ? 'Sign in' : 'Connect'}</b></button>
          })}
          {walletScanDone && availableCount === 0 && <div className="wallet-empty"><span className="entry-icon">↗</span><div><strong>No wallet found</strong><p>Create a generated wallet, restore a backup, or unlock Phantom, Solflare, Backpack, Glow, or another Wallet Standard extension and scan again.</p></div></div>}
        </div>
        <div className="wallet-divider"><span>OR</span></div>
        <button className="create-wallet-choice" aria-label="Create a new wallet" onClick={() => { setMode('create'); setMessage('Choose a strong password. It cannot be recovered for you.') }} disabled={busy}><span className="entry-icon">＋</span><span><strong>Create a new wallet</strong><small>Save a Solana wallet encrypted with your password in the local app.</small></span><b>Create</b></button>
      </div>}

      {mode === 'secret' && <WalletSecretLogin network={network} onReady={onReady} onBack={() => { setVaults(storedWallets()); setMode('choose'); setMessage('') }} />}

      {mode === 'create' && <div className="wallet-form"><p>The wallet secret is encrypted with AES-GCM before it is saved. PumpWizard cannot reset this password. {vaults.length ? 'This adds another wallet; your existing generated wallets remain available.' : ''}</p><label>Password<input type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} disabled={busy} /></label><label>Confirm password<input type="password" autoComplete="new-password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} disabled={busy} /></label><div className="wallet-form-actions"><button onClick={() => setMode('choose')} disabled={busy}>Back</button><button className="primary" onClick={create} disabled={busy || password.length < 10 || !confirmation}>Create encrypted wallet</button></div></div>}

      {mode === 'unlock' && vault && <div className="wallet-form"><div className="stored-address"><small>PUMPWIZARD WALLET</small><code>{vault.address}</code></div><label>Wallet password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && password) unlock() }} disabled={busy} /></label><div className="wallet-form-actions"><button onClick={() => { setMode('choose'); setPassword(''); setVault(null) }} disabled={busy}>Back to wallet selection</button><button className="primary" onClick={unlock} disabled={busy || !password}>Unlock & sign in</button></div></div>}

      {mode === 'backup' && vault && <div className="wallet-form backup-step"><div className="stored-address"><small>NEW WALLET ADDRESS</small><code>{vault.address}</code></div><p>This file contains the encrypted secret. Keep it with your password so you can restore the wallet on another browser.</p><button className="backup-download" onClick={() => downloadVault(vault)}>Download encrypted backup</button><label className="backup-confirm"><input type="checkbox" checked={backupSaved} onChange={(event) => setBackupSaved(event.target.checked)} /> I saved the encrypted backup and understand that PumpWizard cannot recover my password.</label><button className="primary continue-wallet" onClick={finishBackup} disabled={!backupSaved}>Choose a wallet</button></div>}

      {showStatus && <p className="wallet-status" role="status" aria-live="polite">{message}</p>}
      {mode !== 'backup' && mode !== 'secret' && <div className="restore-row"><button onClick={() => restoreInput.current?.click()} disabled={busy}>Restore encrypted backup</button>{mode === 'choose' && <button aria-label="Sign in with private key or recovery phrase" onClick={() => { setMode('secret'); setMessage('') }} disabled={busy}>Private key or recovery phrase</button>}<input ref={restoreInput} hidden type="file" accept="application/json,.json" onChange={restore} /></div>}
    </section>
    </div></div>
  </main>
}
