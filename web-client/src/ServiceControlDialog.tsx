import { useMemo, useRef, useState } from 'react'
import { formatSol, prepareServiceVaultEnrollment } from './browserWallet'
import type { ServiceVaultEnrollment, WalletSession } from './browserWallet'
import type { PortfolioSession } from './PortfolioWorkspace'
import { FRONTEND_BUILD_IDENTITY } from './serviceControl'
import type { CollectorCoverage, ProviderDiagnostics, ServiceAcceptanceEvidence, ServiceAllowanceReview, ServiceConnectionState } from './serviceControl'
import './service-dashboard.css'

type ServiceControlProps = {
  wallet: WalletSession | null
  sessions: PortfolioSession[]
  initialSessionId?: string
  state: ServiceConnectionState
  refresh: () => Promise<void>
  acknowledgeRestart: () => void
  configureMonitoring: (enabled: boolean, intervalSeconds: number) => Promise<unknown>
  enrollWallet: (envelope: ServiceVaultEnrollment, serviceUnlockSecret: string) => Promise<unknown>
  reviewAllowance: (sessionId: string, plan: object) => Promise<{ review: ServiceAllowanceReview }>
  authorizeAllowance: (sessionId: string, reviewId: string, confirmation: string) => Promise<unknown>
  approveAcceptance: (allowanceId: string, routes: string[], durationSeconds: number) => Promise<unknown>
  acceptanceEvidence: (approvalId: string) => Promise<ServiceAcceptanceEvidence>
  startSigning: (allowanceId: string, serviceUnlockSecret: string, confirmation: string) => Promise<unknown>
  unlockSigning: () => void
  pauseEntries: () => Promise<unknown>
  resumeEntries: (allowanceId: string, confirmation: string) => Promise<unknown>
  revokeTrading: () => Promise<unknown>
  shutdown: () => Promise<unknown>
  close: () => void
}

function moment(value: number | null | undefined) {
  if (!value) return 'Never'
  return new Date(value * 1000).toLocaleString()
}

function shortIdentity(value: string | null | undefined) {
  return value ? `${value.slice(0, 8)}…${value.slice(-4)}` : 'None'
}

function titleCase(value: string | null | undefined) {
  return value ? value.replace(/-/g, ' ').replace(/\b\w/g, (letter: string) => letter.toUpperCase()) : 'Unknown'
}

function commandError(reason: unknown) {
  return reason instanceof Error ? reason.message : 'The background-service command failed.'
}

function providerDiagnostics(row: CollectorCoverage): ProviderDiagnostics | null {
  return row.provider_diagnostics || row.diagnostics || null
}

function providerHeadline(status: ProviderDiagnostics['status'] | string | undefined, diagnostics?: ProviderDiagnostics | null) {
  if ((diagnostics?.rejections.unavailable_route || 0) > 0 && !diagnostics?.readiness.ready) return 'Canonical route unavailable'
  if (status === 'healthy') return 'Usable source-linked quotes'
  if (status === 'empty') return 'No matching provider universe'
  if (status === 'partial') return 'Partial provider coverage'
  if (status === 'stale') return 'Source evidence is stale'
  if (status === 'failing') return 'Provider attempt failed'
  if (status === 'quota_exhausted') return 'Provider quota exhausted'
  return 'Coverage state unknown'
}

export function ServiceControlDialog({ wallet, sessions, initialSessionId = '', state, refresh, acknowledgeRestart, configureMonitoring, enrollWallet, reviewAllowance, authorizeAllowance, approveAcceptance, acceptanceEvidence, startSigning, unlockSigning, pauseEntries, resumeEntries, revokeTrading, shutdown, close }: ServiceControlProps) {
  const monitor = state.jobs.find((job) => job.id === 'strategy-monitor')
  const trading = state.trading
  const displayedCustody = state.enrollment ? 'main-wallet-service' : trading?.custody_mode
  const [interval, setIntervalValue] = useState(() => String(monitor?.interval_seconds || 15))
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [confirmStop, setConfirmStop] = useState(false)
  const [confirmShutdown, setConfirmShutdown] = useState(false)
  const [resumeConfirmation, setResumeConfirmation] = useState('')
  const [allowanceSessionId, setAllowanceSessionId] = useState(initialSessionId)
  const [allowanceDuration, setAllowanceDuration] = useState('15')
  const [allowanceSlippage, setAllowanceSlippage] = useState('100')
  const [allowanceReview, setAllowanceReview] = useState<ServiceAllowanceReview | null>(null)
  const [allowanceConfirmation, setAllowanceConfirmation] = useState('')
  const [acceptanceRoute, setAcceptanceRoute] = useState('')
  const [acceptanceDuration, setAcceptanceDuration] = useState('15')
  const [evidence, setEvidence] = useState<ServiceAcceptanceEvidence | null>(null)
  const [showStart, setShowStart] = useState(false)
  const [startConsent, setStartConsent] = useState(false)
  const [startConfirmation, setStartConfirmation] = useState('')
  const [startPasswordReady, setStartPasswordReady] = useState(false)
  const [showEnrollment, setShowEnrollment] = useState(false)
  const [enrollmentConsent, setEnrollmentConsent] = useState(false)
  const walletPassword = useRef<HTMLInputElement>(null)
  const servicePassword = useRef<HTMLInputElement>(null)
  const servicePasswordConfirmation = useRef<HTMLInputElement>(null)
  const startPassword = useRef<HTMLInputElement>(null)
  const connected = state.phase === 'connected' && Boolean(state.service)
  const serviceTone = !connected ? 'unknown' : state.service?.health === 'healthy' ? 'healthy' : 'warning'
  const activeAuthorization = trading?.authorization_status === 'active'
  const serviceTradingEnabled = trading?.signer_state === 'unlocked'
    && ['active', 'paused', 'authorized'].includes(trading.authorization_status)
  const canResume = Boolean(trading
    && trading.entries_paused
    && ['active', 'paused', 'authorized'].includes(trading.authorization_status)
    && trading.signer_state === 'unlocked'
    && (!trading.authorization_expires_at || trading.authorization_expires_at > Date.now() / 1000))
  const expectedResume = trading?.session_id ? `RESUME ${trading.session_id.slice(0, 8).toUpperCase()}` : ''
  const serviceSessions = useMemo(() => sessions.filter((session) => session.brief.schema === 'pumpwizard.session-brief/v4'
    && session.brief.permissions?.custody === 'main-wallet-service'), [sessions])
  const allowanceSession = serviceSessions.find((session) => session.instance.id === allowanceSessionId) || serviceSessions[0]
  const allowancePhrase = allowanceSession ? `ALLOW SERVICE ${allowanceSession.instance.id.slice(0, 8).toUpperCase()}` : ''
  const startAllowance = state.allowances.find((allowance) => allowance.status === 'authorized'
    && allowance.session_id === allowanceSession?.instance.id)

  const startPhrase = startAllowance ? `START ${startAllowance.session_id.slice(0, 8).toUpperCase()}` : ''
  const activationCapability = trading?.activation_capability
  const acceptanceRoutes = startAllowance?.scope?.routes || []
  const selectedAcceptanceRoute = acceptanceRoutes.includes(acceptanceRoute) ? acceptanceRoute : 'all'
  const selectedApprovalRoutes = selectedAcceptanceRoute === 'all' ? acceptanceRoutes : [selectedAcceptanceRoute]
  const packagedCandidate = Boolean(state.runtimeBuild
    && state.runtimeBuild.package_kind !== 'source'
    && FRONTEND_BUILD_IDENTITY.package_kind !== 'source'
    && !state.identityMismatch)
  const canApproveAcceptance = Boolean(connected && wallet?.signingAvailable
    && state.enrollment && startAllowance && selectedApprovalRoutes.length > 0 && packagedCandidate
    && trading?.signer_state !== 'unlocked' && activationCapability?.mode !== 'synthetic' && !activationCapability?.available)
  const canStart = Boolean(connected && state.enrollment && startAllowance
    && activationCapability?.available && trading?.signer_state !== 'unlocked')
  const allowanceSlippageNumber = Number(allowanceSlippage)
  const allowanceSlippageValid = Number.isInteger(allowanceSlippageNumber)
    && allowanceSlippageNumber >= 1 && allowanceSlippageNumber <= 500
  const allowancePlan = useMemo(() => {
    const limits = allowanceSession?.risk_policy
    if (!limits || !allowanceSlippageValid) return null
    const allocation = Math.min(
      Number(limits.allocation_lamports),
      Number(limits.available_cash_lamports ?? limits.allocation_lamports),
    )
    const overhead = 20_100_000
    return {
      duration_seconds: Number(allowanceDuration) * 60,
      budget_lamports: allocation,
      max_order_debit_lamports: Math.min(allocation, Number(limits.max_trade_lamports) + overhead),
      max_daily_debit_lamports: Math.min(allocation, Number(limits.max_daily_buy_lamports) + limits.max_trades_per_day * overhead),
      max_orders: limits.max_trades_per_day,
      max_trades_per_day: limits.max_trades_per_day,
      max_open_positions: Math.min(limits.max_open_positions, allowanceSession?.brief.max_positions ?? limits.max_open_positions),
      slippage_bps: allowanceSlippageNumber,
    }
  }, [allowanceDuration, allowanceSession, allowanceSlippageNumber, allowanceSlippageValid])
  const latestCoverage = useMemo(() => [...state.coverage].sort((left, right) =>
    (right.received_at || right.started_at || 0) - (left.received_at || left.started_at || 0)), [state.coverage])

  const run = async (label: string, task: () => Promise<unknown>) => {
    setBusy(label)
    setError('')
    try {
      await task()
    } catch (reason) {
      setError(commandError(reason))
    } finally {
      setBusy('')
    }
  }

  const clearEnrollmentSecrets = () => {
    if (walletPassword.current) walletPassword.current.value = ''
    if (servicePassword.current) servicePassword.current.value = ''
    if (servicePasswordConfirmation.current) servicePasswordConfirmation.current.value = ''
  }

  const enroll = async () => {
    if (!wallet) {
      setError('Connect an imported or app-local wallet to set up auto trading.')
      return
    }
    const browserSecret = walletPassword.current?.value || ''
    const serviceSecret = servicePassword.current?.value || ''
    if (!enrollmentConsent) {
      setError('Confirm that you understand the service custody boundary before enrolling.')
      return
    }
    if (serviceSecret !== (servicePasswordConfirmation.current?.value || '')) {
      setError('The service wallet passwords do not match.')
      return
    }
    setBusy('enroll')
    setError('')
    try {
      const envelope = await prepareServiceVaultEnrollment(wallet.address, browserSecret, serviceSecret, wallet.network)
      await enrollWallet(envelope, serviceSecret)
      setShowEnrollment(false)
      setEnrollmentConsent(false)
    } catch (reason) {
      setError(commandError(reason))
    } finally {
      clearEnrollmentSecrets()
      setBusy('')
    }
  }

  const prepareAllowance = async () => {
    if (!allowanceSession || !allowancePlan) return
    setBusy('review-allowance')
    setError('')
    setAllowanceConfirmation('')
    try {
      const result = await reviewAllowance(allowanceSession.instance.id, allowancePlan)
      setAllowanceReview(result.review)
    } catch (reason) {
      setError(commandError(reason))
    } finally {
      setBusy('')
    }
  }

  const authorize = async () => {
    if (!allowanceSession || !allowanceReview || allowanceConfirmation !== allowancePhrase) return
    setBusy('authorize-allowance')
    setError('')
    try {
      await authorizeAllowance(allowanceSession.instance.id, allowanceReview.id, allowanceConfirmation)
      setAllowanceReview(null)
      setAllowanceConfirmation('')
    } catch (reason) {
      setError(commandError(reason))
    } finally {
      setBusy('')
    }
  }

  const approveServiceAcceptance = async () => {
    if (!startAllowance || !selectedAcceptanceRoute || !canApproveAcceptance) return
    setBusy('approve-acceptance')
    setError('')
    try {
      await approveAcceptance(
        startAllowance.id,
        selectedApprovalRoutes,
        Number(acceptanceDuration) * 60,
      )
    } catch (reason) {
      setError(commandError(reason))
    } finally {
      setBusy('')
    }
  }

  const loadAcceptanceEvidence = async () => {
    const approvalId = activationCapability?.approval?.approval_id
    if (!approvalId) return
    setBusy('acceptance-evidence')
    setError('')
    try {
      setEvidence(await acceptanceEvidence(approvalId))
    } catch (reason) {
      setError(commandError(reason))
    } finally {
      setBusy('')
    }
  }

  const clearStartSecrets = () => {
    if (startPassword.current) startPassword.current.value = ''
    setStartPasswordReady(false)
    setStartConfirmation('')
    setStartConsent(false)
  }

  const startServiceSigning = async () => {
    if (!startAllowance || !canStart || !startConsent || startConfirmation !== startPhrase) return
    const secret = startPassword.current?.value || ''
    setBusy('start-signing')
    setError('')
    try {
      await startSigning(startAllowance.id, secret, startConfirmation)
      setShowStart(false)
    } catch (reason) {
      setError(commandError(reason))
    } finally {
      clearStartSecrets()
      setBusy('')
    }
  }

  return <div className="modal-backdrop service-control-layer" role="presentation">
    <section className="strategy-modal service-control" role="dialog" aria-modal="true" aria-label="Background service controls">
      <button className="close" aria-label="Close dialog" onClick={close}>×</button>
      <header className="service-control-heading">
        <span><small>PUMPWIZARD SERVICE</small><h2>Background service</h2><p>Keeps monitoring while your browser is closed. Your computer must stay awake.</p></span>
        <span className={`service-health ${serviceTone}`}><i aria-hidden="true" />{connected ? state.service?.health === 'healthy' ? 'Running' : 'Needs attention' : state.phase === 'checking' ? 'Checking' : 'Disconnected · state unknown'}</span>
      </header>

      {state.restarted && <section className="service-restart" role="alert"><div><b>Background service restarted</b><p>The instance identity changed. Any client-side approval was cleared; service signing must be freshly unlocked and authorized.</p></div><button onClick={acknowledgeRestart}>Acknowledge</button></section>}
      {state.identityMismatch && <section className="service-restart service-build-mismatch" role="alert"><div><b>Dashboard and service build mismatch</b><p>{state.identityMismatch} Stop the verified service with the executable that started it, then reopen Dashboard from one candidate and start that same candidate explicitly.</p></div></section>}
      {!connected && <section className="service-disconnected" role="status"><b>Service state is unknown</b><p>{state.error || 'The dashboard has not received an authenticated service status response.'} Read-only collection and signing status cannot be inferred from this page.</p><button onClick={() => void run('refresh', refresh)} disabled={Boolean(busy)}>{busy === 'refresh' ? 'Checking…' : 'Retry connection'}</button></section>}

      <section className="service-section" aria-labelledby="collector-heading">
        <header><h3 id="collector-heading">Strategy monitoring</h3><b className="service-simple-state">{!connected || !monitor ? 'Unknown' : monitor.enabled ? 'On' : 'Off'}</b></header><p className="service-simple-description">Checks your configured strategies and records observations. It does not buy or sell.</p>
        <div className="collector-controls"><label>Check every<select aria-label="Service monitoring interval" value={interval} onChange={(event) => setIntervalValue(event.target.value)}><option value="15">15 seconds</option><option value="30">30 seconds</option><option value="60">60 seconds</option><option value="300">5 minutes</option></select></label><button onClick={() => void run('collector', () => configureMonitoring(!monitor?.enabled, Number(interval)))} disabled={!connected || !monitor || Boolean(busy)}>{busy === 'collector' ? 'Saving…' : monitor?.enabled ? 'Stop monitoring' : 'Start monitoring'}</button></div>
        <p className="service-simple-note">{monitor?.last_success_at ? `Last check: ${moment(monitor.last_success_at)}` : 'No checks recorded yet.'}{!sessions.length && !monitor?.enabled ? ' Add a session on Dashboard to choose what to monitor.' : ''}</p>
        {connected && monitor?.last_error && <p className="service-error" role="alert">Latest monitoring error: {monitor.last_error}</p>}
      </section>

      <section className="service-section service-trading-overview" aria-labelledby="background-trading-heading">
        <header><h3 id="background-trading-heading">Background trading</h3><b className="service-simple-state">{!connected || !trading ? 'Unknown' : serviceTradingEnabled ? trading.entries_paused ? 'Entries paused' : 'Running' : 'Off'}</b></header>
        <p>{!connected || !trading ? 'Reconnect to check whether trading is running.' : serviceTradingEnabled ? 'Closing the browser will not stop this authorization. Your computer must stay awake.' : wallet?.kind === 'extension' ? `${wallet.name} needs your approval in the browser. It cannot sign trades after you close it.` : !wallet ? 'Connect a supported wallet to set up trading while the browser is closed.' : 'Background trading needs a supported wallet, a trading session and approved spending limits.'}</p>
        {connected && trading?.production_activation === 'blocked' && !activationCapability?.available && !serviceTradingEnabled && <p className="service-simple-note">Enable auto trading below by enrolling your wallet and approving the selected session’s limits.</p>}
        {trading && !['none', 'revoked'].includes(trading.authorization_status) && <div className="service-active-controls" aria-label="Active trading controls">
        {canResume && expectedResume && <label className="service-resume-confirmation">Confirm resume<input aria-label="Resume entries confirmation" value={resumeConfirmation} onChange={(event) => setResumeConfirmation(event.target.value)} placeholder={expectedResume} autoComplete="off" /><small>Type {expectedResume}. This cannot renew or widen the current allowance.</small></label>}
        <div className="service-button-row"><button onClick={() => void run('pause', pauseEntries)} disabled={!connected || !activeAuthorization || Boolean(busy)}>{busy === 'pause' ? 'Pausing…' : 'Pause entries'}</button><button onClick={() => void run('resume', () => resumeEntries(trading?.authorization_id || '', resumeConfirmation))} disabled={!connected || !canResume || !trading?.authorization_id || !expectedResume || resumeConfirmation !== expectedResume || Boolean(busy)}>{busy === 'resume' ? 'Resuming…' : 'Resume entries'}</button><button className="danger" onClick={() => setConfirmStop(true)} disabled={!connected || !trading || trading.authorization_status === 'none' || trading.authorization_status === 'revoked' || Boolean(busy)}>Stop & lock trading</button></div>
        {confirmStop && <div className="service-confirm" role="alertdialog" aria-label="Confirm stop and lock trading"><div><b>Stop future service trading and lock signing?</b><p>This revokes the bounded authorization and prevents new signatures. Transactions already submitted to Solana cannot be cancelled and remain visible for reconciliation.</p></div><button onClick={() => setConfirmStop(false)}>Keep current state</button><button className="danger" onClick={() => void run('revoke', async () => { await revokeTrading(); setConfirmStop(false) })} disabled={Boolean(busy)}>{busy === 'revoke' ? 'Stopping…' : 'Confirm stop & lock'}</button></div>}
        </div>}
      </section>
        {(state.orderControls?.cancelled_reviews.length || state.orderControls?.unresolved_submissions.length) ? <div className="service-order-dispositions" role="region" aria-label="Service order control outcomes">
          <header><b>Order control outcomes</b><small>Revoked reviews were never submitted. Submitted signatures remain chain-reconciliation work.</small></header>
          {state.orderControls.cancelled_reviews.map((order) => <article className="cancelled" key={`cancelled-${order.order_id}`}><span><b>Unsigned {order.side.toUpperCase()} review revoked</b><small>Order {shortIdentity(order.order_id)} · session {shortIdentity(order.session_id)}</small></span><span><b>Cancelled before submission</b><small>{order.detail || `Cancelled ${moment(order.cancelled_at)}`}</small></span></article>)}
          {state.orderControls.unresolved_submissions.map((order) => <article className="submitted" key={`submitted-${order.order_id}`}><span><b>Submitted {order.side.toUpperCase()} requires reconciliation</b><small>Order {shortIdentity(order.order_id)} · session {shortIdentity(order.session_id)}</small></span><span><b>{titleCase(order.submission_state)}</b><small>Signature {shortIdentity(order.signature)}</small></span></article>)}
        </div> : null}
      <details className="service-disclosure" open={Boolean(initialSessionId)}>
        <summary>Trading setup and limits</summary>
      <section className="service-section" aria-labelledby="continuation-heading">
        <header><span><small>CUSTODY AND CONTINUATION</small><h3 id="continuation-heading">1. Enroll your trading wallet</h3></span></header>
        <div className="custody-options">
          <article className={trading?.custody_mode === 'main-wallet-browser' ? 'selected' : ''}><span><b>Browser-only signing</b><em>{wallet ? 'Interactive' : 'Wallet not connected'}</em></span><p>The current browser signer stops when this tab closes, refreshes, disconnects or locks. Extension wallets remain on this interactive path.</p><small>{!wallet ? 'Research and collection controls remain available without a wallet.' : wallet.kind === 'extension' ? 'Current wallet is an extension wallet.' : 'Available for this local wallet.'}</small></article>
          <article className={state.enrollment || trading?.custody_mode === 'main-wallet-service' ? 'selected' : ''}><span><b>Background-service custody</b><em>{state.enrollment ? 'Encrypted enrollment stored' : !wallet ? 'Connect when needed' : wallet.kind !== 'local' ? 'Not eligible' : wallet.signingAvailable ? 'Imported or app-local wallet' : 'Browser unlock required'}</em></span><p>An eligible local wallet is copied into separate encrypted local service storage, unlocked only in memory, and constrained by explicit duration and spending limits. This gives local software control of the whole wallet while unlocked.</p><ul><li>Service restart, crash, shutdown or detected sleep/resume locks signing.</li><li>The computer and user service must remain awake and running.</li><li>Closing or signing out of Dashboard does not revoke a separately accepted service authorization.</li></ul>
            {state.enrollment ? <dl className="service-enrollment-identity"><div><dt>Wallet</dt><dd>{shortIdentity(state.enrollment.address)}</dd></div><div><dt>Vault</dt><dd>{shortIdentity(state.enrollment.vault_id)}</dd></div><div><dt>Network</dt><dd>{state.enrollment.network}</dd></div><div><dt>Enrolled</dt><dd>{new Date(state.enrollment.created_at).toLocaleString()}</dd></div></dl> : !wallet ? <small>Connect a local wallet only if you choose to configure service custody.</small> : wallet.kind === 'extension' ? <small>Extension wallets stay interactive; PumpWizard never extracts their private keys.</small> : state.signingError ? <small>Service signing setup unavailable: {state.signingError}</small> : !wallet.signingAvailable ? <button onClick={unlockSigning}>Unlock browser signing first</button> : <button onClick={() => setShowEnrollment(true)} disabled={!connected || Boolean(busy)}>Set up trading wallet</button>}
            {showEnrollment && !state.enrollment && <div className="service-enrollment-form" role="group" aria-label="Enroll local wallet for auto trading"><p>The Solana address and browser backup stay unchanged. Enter the browser-wallet password once to create a second encrypted envelope, then choose a separate service password. Neither password is stored by Dashboard.</p><label>Current wallet password<input ref={walletPassword} type="password" autoComplete="current-password" /></label><label>New service wallet password<input ref={servicePassword} type="password" autoComplete="new-password" /></label><label>Confirm service wallet password<input ref={servicePasswordConfirmation} type="password" autoComplete="new-password" /></label><label className="service-enrollment-consent"><input type="checkbox" checked={enrollmentConsent} onChange={(event) => setEnrollmentConsent(event.target.checked)} /><span>I understand that local service software can control this whole wallet while explicitly unlocked, subject to application limits that are not on-chain caps.</span></label><div><button onClick={() => { clearEnrollmentSecrets(); setShowEnrollment(false); setEnrollmentConsent(false); setError('') }} disabled={Boolean(busy)}>Cancel enrollment</button><button className="primary" onClick={() => void enroll()} disabled={Boolean(busy)}>{busy === 'enroll' ? 'Encrypting & enrolling…' : 'Encrypt & enroll wallet'}</button></div></div>}
          </article>
        </div>
      </section>

      <section className="service-section" aria-labelledby="trading-heading">
        <header><span><small>BOUNDED AUTHORIZATION</small><h3 id="trading-heading">2. Approve session limits</h3></span><p>Dashboard access, wallet sign-in and browser unlock do not grant service trading authority.</p></header>
        {trading?.blockers?.length ? <div className="service-blockers"><b>Before starting</b><ul>{trading.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul></div> : null}
        <div className="service-authorization-setup">
          <div><b>Review auto-trading limits</b><p>Choose how long this session may trade. The allowance uses its existing SOL allocation and limits on order size, daily spending, order count, positions and slippage.</p></div>
          {serviceSessions.length ? <><label>Service-custody session<select aria-label="Service-custody session" value={allowanceSession?.instance.id || ''} onChange={(event) => { setAllowanceSessionId(event.target.value); setAllowanceReview(null); setAllowanceConfirmation('') }}>{serviceSessions.map((session) => <option value={session.instance.id} key={session.instance.id}>{session.instance.label}</option>)}</select></label><label>Duration<select aria-label="Service allowance duration" value={allowanceDuration} onChange={(event) => { setAllowanceDuration(event.target.value); setAllowanceReview(null) }}><option value="5">5 minutes</option><option value="15">15 minutes</option><option value="30">30 minutes</option><option value="60">60 minutes</option></select></label><label>Maximum slippage (bps)<input aria-label="Service allowance slippage" type="number" inputMode="numeric" min="1" max="500" step="1" aria-invalid={!allowanceSlippageValid} value={allowanceSlippage} onChange={(event) => { setAllowanceSlippage(event.target.value); setAllowanceReview(null) }} /><small>{allowanceSlippageValid ? `${(allowanceSlippageNumber / 100).toFixed(2)}% maximum` : 'Enter a whole number from 1 to 500 bps.'}</small></label>{allowancePlan && <dl><div><dt>Total budget</dt><dd>{formatSol(allowancePlan.budget_lamports)}</dd></div><div><dt>Fee-inclusive order</dt><dd>{formatSol(allowancePlan.max_order_debit_lamports)}</dd></div><div><dt>Positions / daily trades</dt><dd>{allowancePlan.max_open_positions} / {allowancePlan.max_trades_per_day}</dd></div></dl>}<button onClick={() => void prepareAllowance()} disabled={!state.enrollment || !allowancePlan || Boolean(busy)} title={!allowanceSlippageValid ? 'Maximum slippage must be a whole number from 1 to 500 bps' : !state.enrollment ? 'Set up encrypted service custody before reviewing an allowance' : undefined}>{busy === 'review-allowance' ? 'Reviewing…' : 'Review exact allowance'}</button></> : <p className="service-no-allowance">Create a new Autopilot session with explicit background-service custody before reviewing an allowance. Existing browser-custody briefs are not rewritten.</p>}
          {allowanceReview && <div className="service-allowance-review"><span><b>{allowanceReview.ready ? 'Review ready' : 'Review blocked'}</b><small>Expires {moment(allowanceReview.expires_at)} · policy revision {allowanceReview.policy_revision}</small></span>{allowanceReview.preflight_blockers.length ? <><p>Allocated {formatSol(allowanceReview.budget_lamports)} · required exit reserve {formatSol(allowanceReview.exit_headroom_lamports)}, plus entry funds.</p><ul>{allowanceReview.preflight_blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul></> : <label>Type <code>{allowancePhrase}</code> to create an inactive allowance<input aria-label="Service allowance confirmation" value={allowanceConfirmation} onChange={(event) => setAllowanceConfirmation(event.target.value.toUpperCase())} autoComplete="off" /></label>}<p>Next, approve these limits with your selected wallet, then unlock the service and Start.</p><div><button onClick={() => { setAllowanceReview(null); setAllowanceConfirmation('') }} disabled={Boolean(busy)}>Change limits</button><button className="primary" onClick={() => void authorize()} disabled={!allowanceReview.ready || allowanceConfirmation !== allowancePhrase || Boolean(busy)}>{busy === 'authorize-allowance' ? 'Authorizing…' : 'Authorize inactive allowance'}</button></div></div>}
        </div>
        {state.allowances.length ? <div className="service-allowance-list" aria-label="Service trading allowances">{state.allowances.map((allowance) => <article key={allowance.id}><span><b>Session {shortIdentity(allowance.session_id)}</b><small>{shortIdentity(allowance.id)}</small></span><span><b>{titleCase(allowance.status)}</b><small>Expires {moment(allowance.expires_at)} · entries {allowance.entries_paused ? 'paused' : 'eligible'}</small></span></article>)}</div> : <p className="service-no-allowance">No service trading allowance exists for this wallet. Enrollment alone cannot trade.</p>}
        {startAllowance && activationCapability?.mode !== 'synthetic' && !activationCapability?.available && <div className="service-enrollment-form service-start-form" role="group" aria-label="Approve auto trading"><p>Approve the selected routes and duration for this session. Your wallet signs these exact spending limits for the installed app. Start then allows real buys and strategy-managed sells within those limits.</p><label>Trading routes<select aria-label="Auto-trading routes" value={selectedAcceptanceRoute} onChange={(event) => setAcceptanceRoute(event.target.value)}><option value="all">All approved routes</option>{acceptanceRoutes.map((route) => <option value={route} key={route}>{route}</option>)}</select></label><label>Auto-trading duration<select aria-label="Auto-trading duration" value={acceptanceDuration} onChange={(event) => setAcceptanceDuration(event.target.value)}><option value="1">1 minute</option><option value="5">5 minutes</option><option value="15">15 minutes</option><option value="30">30 minutes</option><option value="60">60 minutes</option></select></label>{!packagedCandidate && <small>Live auto trading requires the installed app. The source preview keeps transaction signing disabled.</small>}{wallet && !wallet.signingAvailable && <button onClick={unlockSigning}>Unlock wallet to approve</button>}<button className="primary" onClick={() => void approveServiceAcceptance()} disabled={!canApproveAcceptance || Boolean(busy)}>{busy === 'approve-acceptance' ? 'Requesting wallet signature…' : 'Approve auto trading'}</button></div>}
        {activationCapability?.mode === 'acceptance' && activationCapability.approval && <div className="service-allowance-review"><span><b>{activationCapability.available ? 'Wallet-approved auto trading ready' : 'Previous trading approval used'}</b><small>Expires {moment(activationCapability.approval.expires_at)} · build {shortIdentity(activationCapability.approval.build_id)}</small></span><p>Routes: {activationCapability.approval.routes.join(', ')}</p>{!activationCapability.available && <button onClick={() => void loadAcceptanceEvidence()} disabled={Boolean(busy)}>{busy === 'acceptance-evidence' ? 'Verifying evidence…' : 'Inspect acceptance evidence'}</button>}{evidence && <dl><div><dt>Evidence</dt><dd>{titleCase(evidence.status)}</dd></div><div><dt>Orders</dt><dd>{evidence.allowance.used_orders}</dd></div><div><dt>Used allowance</dt><dd>{formatSol(evidence.allowance.used_lamports)}</dd></div><div><dt>Signature</dt><dd>{evidence.approval.signature_verified ? 'Verified' : 'Invalid'}</dd></div></dl>}</div>}
        {showStart && startAllowance && <div className="service-enrollment-form service-start-form" role="group" aria-label="Start auto trading"><b>3. Start auto trading</b><p>Unlock the separately encrypted service vault for this exact allowance. The password is sent only to the authenticated loopback service and is not stored by Dashboard.</p><label>Service wallet password<input ref={startPassword} type="password" autoComplete="current-password" onChange={(event) => setStartPasswordReady(Boolean(event.target.value))} /></label><label>Type <code>{startPhrase}</code> to start<input aria-label="Service start confirmation" value={startConfirmation} onChange={(event) => setStartConfirmation(event.target.value.toUpperCase())} autoComplete="off" /></label><label className="service-enrollment-consent"><input type="checkbox" checked={startConsent} onChange={(event) => setStartConsent(event.target.checked)} /><span>I understand that closing Dashboard will not stop this bounded authorization. Pause prevents new entries; Stop &amp; lock removes signing access.</span></label>{activationCapability?.mode === 'synthetic' && <small>This is an isolated synthetic acceptance runtime. Production submission remains blocked.</small>}<div><button onClick={() => { clearStartSecrets(); setShowStart(false); setError('') }} disabled={Boolean(busy)}>Cancel start</button><button className="primary" onClick={() => void startServiceSigning()} disabled={!startConsent || !startPasswordReady || startConfirmation !== startPhrase || Boolean(busy)}>{busy === 'start-signing' ? 'Unlocking service…' : 'Confirm auto-trading start'}</button></div></div>}
        <div className="service-button-row"><button onClick={() => setShowStart(true)} disabled={!canStart || Boolean(busy)} title={canStart ? 'Unlock this exact bounded allowance' : activationCapability?.blockers?.join(' · ') || 'Approve this wallet and session before starting'}>Start auto trading</button></div>
      </section>

      </details>
      <details className="service-disclosure">
        <summary>Technical details</summary>
      <div className="service-summary-grid">
        <article><small>SERVICE INSTANCE</small><b>{connected ? `Runner v${state.service?.version}` : 'Unavailable'}</b><dl><div><dt>Instance</dt><dd>{shortIdentity(state.service?.instance_id)}</dd></div><div><dt>Revision</dt><dd>{state.service?.instance_revision ?? 'Unknown'}</dd></div><div><dt>Data store</dt><dd>{shortIdentity(state.service?.data_directory_id)}</dd></div><div><dt>Heartbeat</dt><dd>{moment(state.service?.heartbeat_at)}</dd></div></dl></article>
        <article><small>BUILD IDENTITY</small><b>{state.identityMismatch ? 'Mismatch — repair required' : connected ? 'Dashboard and service checked' : 'Runtime unknown'}</b><dl><div><dt>Dashboard</dt><dd>{shortIdentity(FRONTEND_BUILD_IDENTITY.build_id)}</dd></div><div><dt>Service</dt><dd>{shortIdentity(state.runtimeBuild?.build_id)}</dd></div><div><dt>Source</dt><dd>{shortIdentity(state.runtimeBuild?.source_checkpoint || FRONTEND_BUILD_IDENTITY.source_checkpoint)}</dd></div><div><dt>Package</dt><dd>{state.runtimeBuild?.package_kind || FRONTEND_BUILD_IDENTITY.package_kind}</dd></div></dl></article>
        <article><small>READ-ONLY COLLECTOR</small><b>{monitor ? monitor.enabled ? 'Monitoring enabled' : 'Monitoring disabled' : 'Job state unknown'}</b><dl><div><dt>Interval</dt><dd>{monitor ? `${monitor.interval_seconds}s` : 'Unknown'}</dd></div><div><dt>Last success</dt><dd>{moment(monitor?.last_success_at)}</dd></div><div><dt>Failures</dt><dd>{monitor?.consecutive_failures ?? 'Unknown'}</dd></div><div><dt>Next scan</dt><dd>{monitor?.enabled ? moment(monitor.next_at) : 'Not scheduled'}</dd></div></dl></article>
        <article><small>SERVICE TRADING</small><b>{trading ? `${titleCase(displayedCustody)} · ${titleCase(trading.signer_state)}` : 'State unknown'}</b><dl><div><dt>Authorization</dt><dd>{titleCase(trading?.authorization_status)}</dd></div><div><dt>Entries</dt><dd>{trading ? trading.entries_paused ? 'Paused' : 'Allowed by service state' : 'Unknown'}</dd></div><div><dt>Expires</dt><dd>{moment(trading?.authorization_expires_at)}</dd></div><div><dt>Auto trading</dt><dd>{titleCase(trading?.production_activation)}</dd></div></dl></article>
      </div>

        <div className="coverage-list" aria-label="Collector coverage">
          {latestCoverage.length ? latestCoverage.slice(0, 10).map((row, index) => {
            const diagnostics = providerDiagnostics(row)
            const status = diagnostics?.status || row.status
            const rejectionRows = diagnostics ? Object.entries(diagnostics.rejections).filter(([, count]) => count > 0) : []
            const canonicalPumpSwap = diagnostics?.provider === 'CanonicalPumpSwapMarketSource'
            return <article className={`coverage-${status || 'unknown'}`} key={row.id || `${row.scan_key}-${index}`}><span><b>{diagnostics?.provider || row.source || 'Unknown source'}</b><small>{row.scan_key || 'Unlabeled scan'} · {providerHeadline(status, diagnostics)}</small></span><span><b>{diagnostics?.stages.normalized_quotes ?? row.quote_count ?? 0} usable quotes · {row.gap_count ?? 0} gaps</b><small>Latest source {moment(row.latest_source_at)} · received {moment(row.received_at)}</small></span>{canonicalPumpSwap && <p>Read-only observation, not an executable quote. Pilot eligibility is limited to canonical native-SOL, standard-SPL, creator-reward pools; signing must re-read exact current state.</p>}{diagnostics?.readiness.reasons.map((reason) => <p key={reason}>{titleCase(reason.replace(/_/g, '-'))}</p>)}{!diagnostics && row.gap_reason && <p>{row.gap_reason}</p>}{diagnostics && <details><summary>Inspect bounded provider stages</summary><dl><div><dt>Listings / unique</dt><dd>{diagnostics.stages.listing_rows} / {diagnostics.stages.unique_listings}</dd></div><div><dt>Eligible / selected</dt><dd>{diagnostics.stages.eligible_candidates} / {diagnostics.stages.selected_records}</dd></div><div><dt>Details returned</dt><dd>{diagnostics.stages.returned_details} / {diagnostics.stages.selected_detail_requests}</dd></div><div><dt>HTTP requests</dt><dd>{diagnostics.stages.http_requests} / {diagnostics.bounds.maximum_http_requests}</dd></div></dl>{rejectionRows.length ? <ul>{rejectionRows.map(([reason, count]) => <li key={reason}>{titleCase(reason.replace(/_/g, '-'))}: {count}</li>)}</ul> : <p>No retained rejection category was reported.</p>}<small>Routes: {diagnostics.readiness.routes.join(', ') || 'none ready'} · source age limit {diagnostics.readiness.maximum_source_age_seconds ?? 'unknown'}s</small></details>}</article>
          }) : <p>No collector coverage has been recorded for this service instance.</p>}
        </div>
      </details>
      <details className="service-disclosure">
        <summary>Stop the background service</summary>
      <section className="service-section service-lifecycle" aria-labelledby="lifecycle-heading">
        <header><span><small>SERVICE LIFECYCLE</small><h3 id="lifecycle-heading">Dashboard sign-out is separate</h3></span></header>
        <p>Signing out removes this tab’s dashboard and browser-wallet session. It does not stop service-owned read-only jobs or revoke a separately authorized service signer. Use Stop &amp; lock trading for authorization, or shut down the entire service below.</p>
        <button className="danger" onClick={() => setConfirmShutdown(true)} disabled={!connected || Boolean(busy)}>Shut down background service</button>
        {confirmShutdown && <div className="service-confirm" role="alertdialog" aria-label="Confirm background service shutdown"><div><b>Shut down the local background service?</b><p>Signing locks first, then read-only jobs stop and state is flushed. Already submitted transactions are not cancelled. Restarting later requires a fresh trading unlock and authorization.</p></div><button onClick={() => setConfirmShutdown(false)}>Cancel</button><button className="danger" onClick={() => void run('shutdown', async () => { await shutdown(); setConfirmShutdown(false) })} disabled={Boolean(busy)}>{busy === 'shutdown' ? 'Shutting down…' : 'Confirm service shutdown'}</button></div>}
      </section>

      </details>

      {error && <p className="service-error" role="alert">{error}</p>}
      <footer className="service-control-footer"><span>Updated {moment(state.checked_at)}</span><div><button onClick={() => void run('refresh', refresh)} disabled={Boolean(busy)}>{busy === 'refresh' ? 'Refreshing…' : 'Refresh status'}</button><button className="primary" onClick={close}>Done</button></div></footer>
    </section>
  </div>
}
