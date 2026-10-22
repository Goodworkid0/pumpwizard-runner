import { serverStorage } from './serverStorage'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, errorText, fromBase64, toBase64 } from './browserWallet'
import type { ServiceVaultEnrollment, WalletSession } from './browserWallet'

export type BuildIdentity = {
  schema: 'pumpwizard.build-identity/v1'
  build_id: string
  version: string
  source_checkpoint: string
  packaged_at: string
  package_kind: string
}

export const FRONTEND_BUILD_IDENTITY: BuildIdentity = __PUMPWIZARD_FRONTEND_BUILD__

export type BackgroundServiceStatus = {
  schema: 'pumpwizard.background-service-status/v1'
  instance_id: string | null
  instance_revision: number
  data_directory_id: string
  version: string
  state: 'starting' | 'running' | 'stopping' | 'stopped' | 'degraded'
  started_at: number | null
  heartbeat_at: number | null
  current_activity: string
  last_success_at: number | null
  health: 'healthy' | 'degraded' | 'stale' | 'stopped' | 'unknown'
  stop_reason: string
  production_trading: 'blocked'
  build_identity?: BuildIdentity
}

export type BackgroundServiceJob = {
  schema: 'pumpwizard.service-job/v1'
  id: string
  kind: string
  enabled: boolean
  interval_seconds: number
  revision: number
  next_at: number
  consecutive_failures: number
  current_activity: string
  last_started_at: number | null
  last_success_at: number | null
  last_error_at: number | null
  last_error: string
}

export type CollectorCoverage = {
  schema?: 'pumpwizard.collector-coverage/v1'
  id?: string
  scan_key?: string
  source?: string
  status?: string
  quote_count?: number
  gap_count?: number
  gap_reason?: string
  minimum_cap?: number
  maximum_cap?: number
  routes?: string[]
  pools?: string[]
  requested_at?: number | null
  started_at?: number | null
  received_at?: number | null
  earliest_event_at?: number | null
  latest_event_at?: number | null
  earliest_source_at?: number | null
  latest_source_at?: number | null
  consecutive_failures?: number
  next_retry_at?: number | null
  provider_diagnostics?: ProviderDiagnostics
  diagnostics?: ProviderDiagnostics
}

export type ProviderDiagnostics = {
  schema: 'pumpwizard.provider-diagnostics/v1'
  provider: string
  started_at: number
  finished_at: number
  status: 'healthy' | 'empty' | 'partial' | 'stale' | 'failing' | 'quota_exhausted'
  bounds: {
    maximum_http_requests: number
    maximum_elapsed_seconds: number
    maximum_response_bytes: number
    maximum_concurrency: number
  }
  stages: {
    discovery_requests: number
    listing_rows: number
    unique_listings: number
    eligible_candidates: number
    selected_records: number
    selected_detail_requests: number
    returned_details: number
    normalized_quotes: number
    http_requests: number
    response_bytes?: number
  }
  rejections: Record<string, number>
  provenance: {
    provider_hosts?: string[]
    endpoint_classes?: string[]
    normalized_routes?: string[]
    source_timestamp_field?: string | null
    receipt_timestamp_field?: string | null
    response_byte_basis?: string | null
  }
  readiness: {
    ready: boolean
    routes: string[]
    reasons: string[]
    first_received_at?: number | null
    earliest_source_at?: number | null
    latest_source_at?: number | null
    maximum_source_age_seconds?: number | null
  }
}

export type ServiceTradingState = {
  schema: 'pumpwizard.service-trading-state/v1'
  custody_mode: 'not-enrolled' | 'main-wallet-browser' | 'main-wallet-service'
  signer_state: 'locked' | 'unlocked'
  authorization_status: 'none' | 'blocked' | 'authorized' | 'active' | 'paused' | 'expired' | 'revoked'
  authorization_id?: string | null
  authorization_expires_at?: number | null
  signer_expires_at?: number | null
  sender?: string | null
  network?: string | null
  session_id?: string | null
  entries_paused: boolean
  recovery_required: boolean
  production_activation: 'blocked' | 'wallet-approval-required' | 'wallet-approved' | 'active'
  acceptance_activation?: 'blocked' | 'available' | 'active'
  activation_capability?: {
    available: boolean
    mode: 'blocked' | 'synthetic' | 'acceptance' | 'production'
    blockers: string[]
    approval?: {
      approval_id: string
      allowance_id: string
      session_id: string
      sender: string
      network: string
      routes: string[]
      expires_at: number
      build_id: string
      message_sha256: string
    }
  }
  blockers: string[]
}

export type ServiceEnrollment = {
  schema: 'pumpwizard.service-wallet-vault/v1'
  vault_id: string
  custody: 'main-wallet-service'
  address: string
  network: string
  created_at: string
}

export type ServiceAllowance = {
  id: string
  session_id: string
  status: string
  expires_at: number
  entries_paused: boolean
  scope?: {
    duration_seconds?: number
    budget_lamports?: number | string
    max_order_debit_lamports?: number | string
    max_daily_debit_lamports?: number | string
    max_orders?: number
    max_trades_per_day?: number
    max_open_positions?: number
    slippage_bps?: number
    network?: string
    routes?: string[]
  }
}

export type ServiceAcceptanceRequest = {
  schema: 'pumpwizard.service-acceptance-request/v1'
  payload: {
    schema: 'pumpwizard.service-acceptance-approval/v1'
    approval_id: string
    allowance_id: string
    sender: string
    network: string
    routes: string[]
    expires_at: number
    [key: string]: unknown
  }
  message: string
  expires_at: number
}

export type ServiceAcceptanceEvidence = {
  schema: 'pumpwizard.service-acceptance-evidence/v1'
  status: 'recovery-required' | 'reconciled' | 'empty' | 'incomplete'
  recovery_required: boolean
  approval: { activated_at: number; message_sha256: string; signature_verified: boolean }
  allowance: {
    id: string
    used_orders: number
    used_lamports: number
    status: string
    binding_matches_approval: boolean
  }
  orders: { order_id: string; side: string; route: string | null; status: string }[]
}

export type ServiceOrderDisposition = {
  order_id: string
  session_id: string
  side: string
  status: 'cancelled' | 'submitted'
  created_at: number
  cancelled_at?: number
  detail?: string
  submission_state?: string
  signature?: string
}

export type ServiceOrderControls = {
  schema: 'pumpwizard.service-order-controls/v1'
  cancelled_reviews: ServiceOrderDisposition[]
  unresolved_submissions: ServiceOrderDisposition[]
  authority: { orders: false; signer: false; submission: false }
}

export type ServiceAllowanceReview = {
  id: string
  session_id: string
  strategy_digest: string
  policy_revision: number
  cash_hold: string
  duration_seconds: number
  budget_lamports: number
  max_order_debit_lamports: number
  max_daily_debit_lamports: number
  max_orders: number
  max_trades_per_day: number
  max_open_positions: number
  slippage_bps: number
  exit_headroom_lamports: number
  expires_at: number
  ready: boolean
  preflight_blockers: string[]
  activation_blockers: string[]
}

type ServicePayload = {
  service: BackgroundServiceStatus
  build_identity?: BuildIdentity
  jobs?: BackgroundServiceJob[]
  coverage?: CollectorCoverage[]
  trading?: ServiceTradingState
}

type ServiceSigningPayload = {
  trading: ServiceTradingState
  enrollment: ServiceEnrollment | null
  allowances: ServiceAllowance[]
  order_controls?: ServiceOrderControls
}

export type ServiceConnectionState = {
  phase: 'checking' | 'connected' | 'disconnected'
  service: BackgroundServiceStatus | null
  jobs: BackgroundServiceJob[]
  coverage: CollectorCoverage[]
  trading: ServiceTradingState | null
  enrollment: ServiceEnrollment | null
  allowances: ServiceAllowance[]
  orderControls: ServiceOrderControls | null
  signingError: string
  restarted: boolean
  runtimeBuild: BuildIdentity | null
  identityMismatch: string
  error: string
  checked_at: number | null
}

const INSTANCE_STORAGE_KEY = 'pumpwizard.background-service-instance/v1'
const STALE_APPROVAL_STORAGE_KEY = 'pumpwizard.background-service-approval/v1'

const EMPTY_STATE: ServiceConnectionState = {
  phase: 'checking',
  service: null,
  jobs: [],
  coverage: [],
  trading: null,
  enrollment: null,
  allowances: [],
  orderControls: null,
  signingError: '',
  restarted: false,
  runtimeBuild: null,
  identityMismatch: '',
  error: '',
  checked_at: null,
}

function storedInstance() {
  try {
    const value = serverStorage.getItem(INSTANCE_STORAGE_KEY)
    if (!value) return null
    const parsed = JSON.parse(value) as { id?: unknown; revision?: unknown }
    return typeof parsed.id === 'string' && typeof parsed.revision === 'number'
      ? { id: parsed.id, revision: parsed.revision }
      : null
  } catch {
    return null
  }
}

function rememberInstance(service: BackgroundServiceStatus) {
  if (!service.instance_id) return
  try {
    serverStorage.setItem(INSTANCE_STORAGE_KEY, JSON.stringify({
      id: service.instance_id,
      revision: service.instance_revision,
    }))
  } catch {
    // Status remains usable when hardened storage is unavailable.
  }
}

function clearStaleApproval() {
  try { window.sessionStorage.removeItem(STALE_APPROVAL_STORAGE_KEY) } catch { /* no persisted approval to clear */ }
}

function identityMismatch(service: BackgroundServiceStatus, runtimeBuild: BuildIdentity | null) {
  if (service.version !== FRONTEND_BUILD_IDENTITY.version) {
    return `Dashboard assets are Runner ${FRONTEND_BUILD_IDENTITY.version}, but the service is ${service.version}.`
  }
  if (runtimeBuild && runtimeBuild.version !== service.version) {
    return `The service build identity reports ${runtimeBuild.version}, but its runtime reports ${service.version}.`
  }
  if (FRONTEND_BUILD_IDENTITY.build_id !== 'development') {
    if (!runtimeBuild) return 'This packaged Dashboard could not verify the service build identity.'
    if (runtimeBuild.build_id !== FRONTEND_BUILD_IDENTITY.build_id) {
      return 'Dashboard assets and the running service come from different source checkpoints.'
    }
  }
  return ''
}

export function useServiceControl(wallet: WalletSession | null) {
  const [state, setState] = useState<ServiceConnectionState>(EMPTY_STATE)
  const requestRevision = useRef(0)
  const latestServiceRevision = useRef(0)

  const refresh = useCallback(async () => {
    const request = ++requestRevision.current
    setState((current) => current.service ? current : { ...current, phase: 'checking', error: '' })
    try {
      const result = await api<ServicePayload>('/api/v1/service/status')
      if (request !== requestRevision.current) return
      const service = result.service
      if (!service || typeof service.instance_revision !== 'number' || !('health' in service)) {
        throw new Error('Background service returned an unsupported status payload.')
      }
      if (service.instance_revision < latestServiceRevision.current) return
      const runtimeBuild = result.build_identity || service.build_identity || null
      const mismatch = identityMismatch(service, runtimeBuild)
      const prior = storedInstance()
      if (prior && service.instance_revision < prior.revision) return
      latestServiceRevision.current = service.instance_revision
      const restarted = Boolean(service.instance_id && prior && (
        prior.id !== service.instance_id || prior.revision < service.instance_revision
      ))
      if (restarted) clearStaleApproval()
      rememberInstance(service)
      let trading = result.trading || null
      let enrollment: ServiceEnrollment | null = null
      let allowances: ServiceAllowance[] = []
      let orderControls: ServiceOrderControls | null = null
      let signingError = ''
      if (wallet) {
        try {
          const query = new URLSearchParams({ sender: wallet.address, network: wallet.network })
          const signing = await api<ServiceSigningPayload>(`/api/v1/service/signing?${query}`)
          if (request !== requestRevision.current) return
          trading = signing.trading || trading
          enrollment = signing.enrollment || null
          allowances = Array.isArray(signing.allowances) ? signing.allowances : []
          orderControls = signing.order_controls || null
        } catch (reason) {
          signingError = errorText(reason)
        }
      }
      setState((current) => ({
        phase: 'connected',
        service,
        jobs: Array.isArray(result.jobs) ? result.jobs : [],
        coverage: Array.isArray(result.coverage) ? result.coverage : [],
        trading,
        enrollment,
        allowances,
        orderControls,
        signingError,
        restarted: current.restarted || restarted,
        runtimeBuild,
        identityMismatch: mismatch,
        error: '',
        checked_at: Date.now() / 1000,
      }))
    } catch (reason) {
      if (request !== requestRevision.current) return
      setState((current) => ({
        ...current,
        phase: 'disconnected',
        service: null,
        jobs: [],
        coverage: [],
        trading: null,
        enrollment: null,
        allowances: [],
        orderControls: null,
        signingError: '',
        runtimeBuild: null,
        identityMismatch: '',
        error: errorText(reason),
        checked_at: Date.now() / 1000,
      }))
    }
  }, [wallet])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => { void refresh() }, 5_000)
    return () => {
      requestRevision.current += 1
      window.clearInterval(timer)
    }
  }, [refresh, wallet])

  const command = useCallback(async <T,>(path: string, body: object) => {
    const result = await api<T>(path, body)
    await refresh()
    return result
  }, [refresh])

  const acknowledgeRestart = useCallback(() => {
    setState((current) => ({ ...current, restarted: false }))
  }, [])

  return {
    state,
    refresh,
    acknowledgeRestart,
    configureMonitoring: (enabledValue: boolean, intervalSeconds: number) => command<{ job: BackgroundServiceJob }>(
      '/api/v1/service/jobs/strategy-monitor',
      { enabled: enabledValue, interval_seconds: intervalSeconds },
    ),
    enrollWallet: (envelope: ServiceVaultEnrollment, serviceUnlockSecret: string) => command<{ enrollment: ServiceEnrollment }>(
      '/api/v1/service/signing/enroll',
      { sender: wallet?.address, network: wallet?.network, envelope, service_unlock_secret: serviceUnlockSecret },
    ),
    reviewAllowance: (sessionId: string, plan: object) => command<{ review: ServiceAllowanceReview }>(
      '/api/v1/service/signing/review',
      { sender: wallet?.address, network: wallet?.network, session_id: sessionId, plan },
    ),
    authorizeAllowance: (sessionId: string, reviewId: string, confirmation: string) => command<{ allowance: ServiceAllowance }>(
      '/api/v1/service/signing/authorize',
      { sender: wallet?.address, network: wallet?.network, session_id: sessionId, review_id: reviewId, confirmation },
    ),
    approveAcceptance: async (allowanceId: string, routes: string[], durationSeconds: number) => {
      if (!wallet || !wallet.signingAvailable) {
        throw new Error('Unlock the selected wallet before approving auto trading.')
      }
      wallet.assertActive()
      const selectedRoutes = [...new Set(routes)].sort()
      const response = await api<{ acceptance: ServiceAcceptanceRequest }>(
        '/api/v1/service/signing/acceptance/request',
        {
          sender: wallet.address,
          network: wallet.network,
          allowance_id: allowanceId,
          routes: selectedRoutes,
          duration_seconds: durationSeconds,
        },
      )
      const acceptance = response.acceptance
      if (!acceptance || acceptance.schema !== 'pumpwizard.service-acceptance-request/v1'
        || acceptance.payload.allowance_id !== allowanceId
        || acceptance.payload.sender !== wallet.address
        || acceptance.payload.network !== wallet.network
        || JSON.stringify(acceptance.payload.routes) !== JSON.stringify(selectedRoutes)) {
        throw new Error('The service returned different auto-trading limits than the ones reviewed.')
      }
      wallet.assertActive()
      const signature = await wallet.signMessage(fromBase64(acceptance.message))
      wallet.assertActive()
      const result = await api<{ trading: ServiceTradingState }>(
        '/api/v1/service/signing/acceptance/install',
        {
          sender: wallet.address,
          network: wallet.network,
          payload: acceptance.payload,
          signature: toBase64(signature),
        },
      )
      await refresh()
      return result
    },
    acceptanceEvidence: async (approvalId: string) => {
      if (!wallet) throw new Error('Connect the approved wallet to inspect acceptance evidence.')
      wallet.assertActive()
      const query = new URLSearchParams({
        sender: wallet.address,
        network: wallet.network,
        approval_id: approvalId,
      })
      const result = await api<{ evidence: ServiceAcceptanceEvidence }>(
        `/api/v1/service/signing/acceptance/evidence?${query}`,
      )
      return result.evidence
    },
    startSigning: (allowanceId: string, serviceUnlockSecret: string, confirmation: string) => command<{ trading: ServiceTradingState }>(
      '/api/v1/service/signing/start',
      { sender: wallet?.address, network: wallet?.network, allowance_id: allowanceId, service_unlock_secret: serviceUnlockSecret, confirmation },
    ),
    pauseEntries: () => command<{ trading: ServiceTradingState }>('/api/v1/service/pause-entries', {}),
    resumeEntries: (allowanceId: string, confirmation: string) => command<{ trading: ServiceTradingState }>(
      '/api/v1/service/resume-entries',
      { allowance_id: allowanceId, confirmation },
    ),
    revokeTrading: () => command<{ trading: ServiceTradingState }>('/api/v1/service/revoke-trading', {}),
    shutdown: () => command<{ requested: boolean; notice: string }>('/api/v1/service/shutdown', {}),
  }
}
