import { useEffect, useRef, useState } from 'react'
import { api, createBrowserWallet, downloadVault, errorText, storedWallets, unlockBrowserWallet, verifyBrowserWalletPassword, revokeWallet } from './browserWallet'
import type { EncryptedWallet, WalletSession } from './browserWallet'
import { WithdrawDialog } from './WithdrawDialog'
import type { WithdrawalReview } from './WithdrawDialog'
import { AutomationWalletBalance } from './AutomationWalletBalance'
import { useWalletInventories } from './useWalletInventories'
import { WalletSessionUnlock } from './WalletSessionUnlock'

export function AutomationWalletSetup({ source, close, selectWallet, sourceUnlocked, refresh }: {
  source: WalletSession
  close: () => void
  selectWallet: (wallet: WalletSession) => Promise<void>
  sourceUnlocked: (wallet: WalletSession) => void
  refresh: () => Promise<void>
}) {
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const [transferAttempted, setTransferAttempted] = useState(false)
  const [vaults, setVaults] = useState(() => storedWallets().filter((vault) => vault.address !== source.address))
  const [vault, setVault] = useState<EncryptedWallet | null>(null)
  const [step, setStep] = useState<'choose' | 'create' | 'backup' | 'access' | 'ready'>('choose')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [backedUp, setBackedUp] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [blockedTransfer, setBlockedTransfer] = useState<{ id: string; signature: string } | null>(null)
  const [recoveryMessage, setRecoveryMessage] = useState('')
  const [unlockAction, setUnlockAction] = useState<'fund' | 'recover'>('fund')
  const [fundingReview, setFundingReview] = useState<WithdrawalReview | null>(null)
  const [amount, setAmount] = useState('')
  const previewing = useRef(false)
  const inventory = useWalletInventories([source.address, ...vaults.map((saved) => saved.address)], source.network)
  const [unlockSource, setUnlockSource] = useState(false)
  const run = async (action: () => Promise<void>) => {
    if (busy) return
    setBusy(true); setError('')
    try { await action() } catch (reason) { setError(errorText(reason)) } finally { setBusy(false) }
  }
  const choose = (selected: EncryptedWallet) => { setVault(selected); setPassword(''); setError(''); setStep('access') }
  const create = () => run(async () => {
    if (password !== confirmation) throw new Error('The two passwords do not match.')
    const created = await createBrowserWallet(password)
    setVault(created); setVaults(storedWallets().filter((saved) => saved.address !== source.address)); setPassword(''); setConfirmation(''); setBackedUp(false); setStep('backup')
  })
  const verify = () => run(async () => {
    if (!vault) return
    await verifyBrowserWalletPassword(vault, password)
    setStep('ready')
  })
  const activate = () => run(async () => {
    if (!vault) return
    const target = await unlockBrowserWallet(vault, password, source.network)
    try {
      if (!mounted.current) throw new Error('Wallet setup closed. Open it again to continue.')
      if (source.signingAvailable) source.assertActive()
      await selectWallet(target); setPassword('')
    }
    catch (reason) { await revokeWallet(target); throw reason }
  })
  const prepareFunding = async (wallet = source) => {
    if (!vault || previewing.current || transferAttempted) return
    previewing.current = true; setBusy(true); setError('')
    try {
      wallet.assertActive()
      if (wallet.address !== source.address || wallet.network !== source.network) throw new Error('The funding wallet changed. Open setup again.')
      const result = await api<{ preview: WithdrawalReview }>('/api/v1/withdrawals/preview', { sender: wallet.address, network: wallet.network, recipient: vault.address, amount: amount.trim(), funding_budget: true })
      wallet.assertActive()
      if (result.preview.sender !== wallet.address || result.preview.network !== wallet.network || result.preview.recipient !== vault.address) throw new Error('Transfer destination changed. Prepare a new review.')
      if (mounted.current) setFundingReview(result.preview)
    } catch (reason) {
      if (mounted.current) setError(errorText(reason))
      try {
        const state = await api<{ withdrawals: { id: string; signature: string; status: string }[] }>(`/api/v1/trades?${new URLSearchParams({ sender: wallet.address, network: wallet.network })}`)
        if (mounted.current) setBlockedTransfer(state.withdrawals.find((item) => ['pending', 'unknown'].includes(item.status)) || null)
      } catch { /* Preserve the original preview error when history is unavailable. */ }
    }
    finally { previewing.current = false; if (mounted.current) setBusy(false) }
  }
  const recoverFunding = async (wallet = source) => {
    if (!blockedTransfer || busy) return
    setBusy(true); setError(''); setRecoveryMessage('')
    try {
      wallet.assertActive()
      await api('/api/v1/withdrawals/resolve-expired', { sender: wallet.address, network: wallet.network, transfer_id: blockedTransfer.id, signature: blockedTransfer.signature })
      if (mounted.current) { setBlockedTransfer(null); setTransferAttempted(false); setRecoveryMessage('Previous transfer expired. Its lock is released. Use Fund wallet to review a new transfer. Nothing was resent.') }
      await refresh(); inventory.refresh()
    } catch (reason) { if (mounted.current) setError(errorText(reason)) }
    finally { if (mounted.current) setBusy(false) }
  }
  const fraction = (percent: number) => {
    const balance = inventory.results[source.address]?.data?.balance
    if (!balance) return
    const atoms = BigInt(balance) * BigInt(percent) / 100n
    setAmount(`${atoms / 1000000000n}.${(atoms % 1000000000n).toString().padStart(9, '0')}`.replace(/\.?0+$/, ''))
  }
  const selectedCard = vault ? <article className="automation-wallet-card automation-wallet-selected"><header><b>Selected PumpWizard wallet</b><button disabled={busy} onClick={inventory.refresh}>Refresh balances</button></header><div className="stored-address"><small>DESTINATION · {source.network}</small><code>{vault.address}</code></div><AutomationWalletBalance result={inventory.results[vault.address]} /></article> : null
  const stage = step === 'choose' || step === 'create' ? 0 : step === 'ready' ? 2 : 1
  if (unlockSource) return <WalletSessionUnlock session={source} close={() => setUnlockSource(false)} unlocked={(wallet) => { if (wallet.address !== source.address || wallet.network !== source.network) throw new Error('Reconnect the original funding wallet to continue.'); sourceUnlocked(wallet); setUnlockSource(false); if (unlockAction === 'recover') void recoverFunding(wallet); else void prepareFunding(wallet) }} />
  if (fundingReview && vault) return <WithdrawDialog wallet={source} recipient={vault.address} funding onResolved={() => setTransferAttempted(false)} initialReview={fundingReview} initialAmount={amount} onSubmissionAttempt={() => setTransferAttempted(true)} close={() => { setFundingReview(null); inventory.refresh() }} refresh={async () => { await refresh(); inventory.refresh() }} />
  return <div className="modal-backdrop detail-layer"><section className="strategy-modal automation-wallet-setup" role="dialog" aria-modal="true" aria-label="Set up automation wallet">
    <button className="close" aria-label="Close wallet setup" disabled={busy} onClick={close}>×</button>
    <small>SESSION SETUP · AUTOMATION WALLET</small><h2>{step === 'create' ? 'Create a PumpWizard wallet' : step === 'backup' ? 'Save your wallet backup' : step === 'access' ? 'Confirm access to this wallet' : step === 'ready' ? 'Fund and use this wallet' : 'Choose a PumpWizard wallet'}</h2>
    <p>Use a PumpWizard wallet for automatic signing. Choose an existing wallet or create one, then add SOL if needed.</p>
    <ol className="automation-wallet-progress" aria-label="Wallet setup progress">{['Choose wallet', 'Confirm access', 'Fund & continue'].map((label, index) => <li key={label} className={index <= stage ? 'reached' : ''} aria-current={index === stage ? 'step' : undefined}><span>{index + 1}</span>{label}</li>)}</ol>
    {step === 'choose' && <>
      <div className="automation-wallet-section-heading"><h3>Saved in the local app</h3><button onClick={inventory.refresh}>Refresh balances</button></div>
      <div className="automation-wallet-choices">{vaults.map((saved) => <article className="automation-wallet-card" key={saved.address}><header><b>PumpWizard wallet</b><small>{source.network}</small></header><code>{saved.address}</code><AutomationWalletBalance result={inventory.results[saved.address]} /><button onClick={() => choose(saved)}>Use existing PumpWizard wallet</button></article>)}
      <article className="automation-wallet-card automation-wallet-new"><span className="automation-wallet-plus" aria-hidden="true">+</span><h3>A separate wallet for automation</h3><p>Create an encrypted wallet and save its backup before adding funds.</p><button className="primary" onClick={() => { setStep('create'); setPassword(''); setConfirmation('') }}>Create new PumpWizard wallet</button></article></div>
    </>}
    {step === 'create' && <div className="wallet-form"><p>Use at least 10 characters. PumpWizard cannot recover this password.</p><label>Password<input type="password" autoComplete="new-password" disabled={busy} value={password} onChange={(event) => setPassword(event.target.value)} /></label><label>Confirm password<input type="password" autoComplete="new-password" disabled={busy} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label><button className="primary" disabled={busy || password.length < 10 || !confirmation} onClick={() => void create()}>Create encrypted wallet</button></div>}
    {vault && step !== 'create' && step !== 'choose' && step !== 'ready' && selectedCard}
    {step === 'backup' && vault && <div className="wallet-form"><p>Save the encrypted file and keep your password. Creating this wallet has not moved any funds.</p><button onClick={() => downloadVault(vault)}>Download encrypted backup</button><label className="backup-confirm"><input type="checkbox" checked={backedUp} onChange={(event) => setBackedUp(event.target.checked)} />I saved the encrypted backup and understand that PumpWizard cannot recover my password.</label><button className="primary" disabled={!backedUp} onClick={() => setStep('access')}>Continue to wallet access</button></div>}
    {step === 'access' && <div className="wallet-form"><p>Check that you can decrypt the destination before sending funds. This check does not enable signing.</p><label>Wallet password<input type="password" autoComplete="current-password" disabled={busy} value={password} onChange={(event) => setPassword(event.target.value)} /></label><button className="primary" disabled={busy || !password} onClick={() => void verify()}>Confirm wallet access</button></div>}
    {step === 'ready' && <div className="automation-wallet-funding"><div className="automation-wallet-destination">{selectedCard}
      <section className="automation-wallet-continue"><div><h3>Ready to continue?</h3><p>Funding is optional. Selecting this wallet returns to your session settings.</p></div><button className="primary" disabled={busy} onClick={() => void activate()}>Use this wallet for session</button></section>
</div>
      <section className="automation-funding-panel"><h3>Add SOL from {source.name} <small>OPTIONAL</small></h3><p>{source.name} signs this transfer. Token holdings stay in the original wallet.</p>
        <div className="automation-funding-source"><small>FROM · {source.name}</small><code>{source.address}</code><AutomationWalletBalance result={inventory.results[source.address]} compact /></div>
        <label className="automation-amount">Amount (SOL)<input value={amount} disabled={busy || transferAttempted} inputMode="decimal" autoComplete="off" placeholder="0.00" onChange={(event) => setAmount(event.target.value)} /></label>
        <div className="automation-amount-shortcuts"><span>Of your SOL balance</span>{[25, 50, 100].map((percent) => <button key={percent} disabled={busy || transferAttempted || !inventory.results[source.address]?.data} onClick={() => fraction(percent)}>{percent === 100 ? 'Max' : `${percent}%`}</button>)}</div>
        <p className="automation-funding-hint">This is your total spending budget, including the fee. We deduct the fee and keep at least 0.002 SOL in the source wallet, plus any reserved funds. Review the exact amounts next.</p>
        <button disabled={busy || transferAttempted || !amount.trim()} onClick={() => source.signingAvailable ? void prepareFunding() : (setUnlockAction('fund'), setUnlockSource(true))}>{busy ? 'Preparing review…' : 'Fund wallet'}</button>
        {busy && <p role="status">Preparing the transfer review…</p>}
        {error && <p className="error" role="alert" ref={(element) => { element?.scrollIntoView({ block: 'nearest' }) }}>{error}</p>}
        {blockedTransfer && <div className="transfer-receipt-state"><b>Previous transfer needs a status check</b><p>Check the original signature with two RPCs. Its lock is released only if it has expired and neither RPC finds a transaction. This does not send SOL.</p><button disabled={busy} onClick={() => source.signingAvailable ? void recoverFunding() : (setUnlockAction('recover'), setUnlockSource(true))}>Check & resolve previous transfer</button></div>}
        {recoveryMessage && <p role="status">{recoveryMessage}</p>}
        {transferAttempted && <p role="status">A transfer was submitted or its outcome is unknown. Check the original transfer in Portfolio before sending again.</p>}
      </section>

    </div>}
    <p className="session-plan-note">Your wallet stays encrypted locally. Selecting it does not start trading. Automatic trading uses the background service and requires separate wallet enrollment, spending limits and Start.</p>
    {error && step !== 'ready' && <p className="error" role="alert">{error}</p>}
    <div className="modal-actions">{step !== 'choose' && step !== 'backup' && <button disabled={busy} onClick={() => { setStep('choose'); setVault(null); setPassword(''); setAmount(''); setError('') }}>Choose another wallet</button>}<button disabled={busy} onClick={close}>Back to session</button></div>
  </section></div>
}
