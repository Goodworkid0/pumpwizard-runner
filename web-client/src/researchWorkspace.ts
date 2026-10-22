import { serverStorage } from './serverStorage'
import { useCallback, useEffect, useRef, useState } from 'react'

export type ResearchDataset = {
  schema: 'pumpwizard.research-dataset/v1'
  id: string
  content_sha256: string
  source_session_id: string
  source_strategy_sha256: string
  created_at: number
  event_range: { first_at: number | null; last_at: number | null }
  first_known_range: { first_at: number | null; last_at: number | null; basis: string }
  source_age_seconds: number | null
  counts: {
    observations: number
    decisions: number
    outcomes: number
    independent_observations: number
    unknown_first_known: number
  }
  quality: Record<string, number>
  gaps: { id: string; started_at: number | null; ended_at: number | null; reason: string; detail: string }[]
  supported_family: boolean
  family: string
  eligible: boolean
  blockers: string[]
  evidence_policy: string
}

export type ResearchCostModel = {
  label: string
  capital_unit: string
  variable_fee_fraction_each_side: number
  slippage_fraction_each_side: number
  fixed_cost_each_side: number
  rent_each_entry: number
}

export type ResearchExperiment = {
  schema: 'pumpwizard.research-experiment/v1'
  id: string
  digest: string
  dataset_id: string
  parent_experiment_id: string | null
  created_at: number
  purpose: 'exploratory' | 'eligible'
  starting_capital: number
  capital_unit: string
  candidate_count: number
  candidate_package_sha256: string[]
  cost_model: ResearchCostModel
  split: { train_end_at: number; validation_end_at: number; holdout_end_at: number; embargo_seconds: number }
  evidence_policy: string
  code_version: string
  replay_engine_version: string
  holdout_state: 'unseen' | 'reserved' | 'exposed' | 'not_evaluated' | 'ineligible'
  changed_from_parent: string[]
}

export type ResearchAttempt = {
  schema: 'pumpwizard.research-attempt/v1'
  id: string
  experiment_id: string
  number: number
  state: 'queued' | 'running' | 'failed' | 'cancelled' | 'completed'
  started_at: number | null
  finished_at: number | null
  error: string | null
  restart_count: number
}

export type ResearchJob = {
  schema: 'pumpwizard.research-job/v1'
  id: string
  experiment_id: string
  attempt_id: string
  state: 'queued' | 'running' | 'cancelling' | 'interrupted' | 'failed' | 'cancelled' | 'completed'
  progress: number
  cancellation_requested: boolean
  restart_count: number
  report_id: string | null
  error: string | null
  created_at: number
  updated_at: number
}

export type ResearchEquityPoint = {
  at: number
  equity: number | null
  gap_reason: string | null
}

export type ResearchComparison = {
  id: string
  kind: 'cash' | 'current' | 'candidate' | 'holding'
  label: string
  package_sha256: string | null
  starting_capital: number
  ending_capital: number | null
  capital_unit: string
  modeled_net_pnl: number | null
  modeled_realized_pnl: number | null
  modeled_unrealized_pnl: number | null
  maximum_drawdown_fraction: number | null
  coverage_fraction: number | null
  independent_observations: number | null
  modeled_costs: number | null
  uncertainty: string | null
  equity: ResearchEquityPoint[]
}

export type ResearchLedgerRow = {
  id: string
  comparison_id: string
  opened_at: number | null
  closed_at: number | null
  symbol: string
  side: string
  quantity: number | null
  price: number | null
  value: number | null
  cost: number | null
  pnl: number | null
  state: 'modeled' | 'unpriced' | 'unknown-exit' | 'excluded'
  explanation: string
}

export type ResearchStressResult = {
  id: string
  label: string
  comparison_id: string
  modeled_net_pnl: number | null
  costs: number | null
  changed_recommendation: boolean
}

export type ResearchReport = {
  schema: 'pumpwizard.research-replay-report/v1'
  id: string
  experiment_id: string
  dataset_id: string
  created_at: number
  period: { first_at: number | null; last_at: number | null }
  capital_unit: string
  comparisons: ResearchComparison[]
  ledger: ResearchLedgerRow[]
  cost_stress: ResearchStressResult[]
  exclusions: { reason: string; count: number; action: string }[]
  recommendation: {
    action: 'insufficient_evidence' | 'prefer_cash' | 'keep_current' | 'create_inactive_draft'
    candidate_id: string | null
    reason: string
    blockers: string[]
  }
  holdout_state: 'unseen' | 'reserved' | 'exposed' | 'not_evaluated' | 'ineligible'
  limitations: string[]
}

export type PaperRun = {
  schema: 'pumpwizard.paper-run/v1'
  id: string
  source_session_id: string
  strategy_sha256: string
  state: 'running' | 'paused' | 'stopped' | 'interrupted'
  starting_capital_usd: number
  simulated_capital_usd: number | null
  started_at: number
  first_known_at: number | null
  updated_at: number
  decisions: number
  scored_outcomes: number
  unknown_outcomes: number
  independent_observations: number
  excluded_observations?: number
  processing_state?: 'healthy' | 'degraded' | 'stalled'
  gaps: { id: string; started_at: number; ended_at: number | null; reason: string }[]
  assumptions: string[]
  fault?: {
    code: string
    message: string
    evidence_id: string | null
    retryable: boolean
    retry_count: number
    max_retries: number
    repair: string
  } | null
  retry?: { count: number; maximum: number; next_at: number | null }
  exclusions?: { reason: string; count: number; repair: string }[]
}

export type PaperHistory = {
  observations: {
    evidence_id: string
    mint: string
    route: string
    pool: string
    event_at: number
    first_received_at: number
    source_age_seconds: number
    event_age_seconds?: number
    quality?: {
      status: 'eligible' | 'excluded'
      reasons: string[]
      decision_eligible: boolean
      repair: string | null
    }
  }[]
  decisions: {
    decision_key: string
    evidence_id: string
    decided_at: number
    decision: { kind?: string; mint?: string; reason?: string }
  }[]
  outcomes: {
    decision_key: string
    horizon_seconds: number
    scored_at: number
    status: string
    modeled_return_fraction: number | null
    entry_evidence_id: string
    outcome_evidence_id: string
  }[]
  gaps: { gap_id: string; started_at: number; ended_at: number | null; reason: string }[]
  operating_intervals?: { interval_id: string; started_at: number; ended_at: number | null; reason: string }[]
  faults?: {
    fault_id: string
    occurred_at: number
    evidence_id: string | null
    code: string
    message: string
    retryable: boolean
    repair: string
  }[]
}

export type ResearchWorkspaceSnapshot = {
  paper_research_allowed?: boolean
  schema: 'pumpwizard.research-workspace/v1'
  source_session_id: string
  source_label: string
  source_strategy_sha256?: string
  dataset_preview: ResearchDataset | null
  datasets: ResearchDataset[]
  experiments: ResearchExperiment[]
  attempts: ResearchAttempt[]
  jobs: ResearchJob[]
  reports: ResearchReport[]
  paper_runs: PaperRun[]
}

export type ResearchSettings = {
  starting_capital: number
  capital_unit: 'USD'
  purpose: 'exploratory' | 'eligible'
  variable_fee_bps_each_side: number
  slippage_bps_each_side: number
  fixed_cost_each_side: number
  rent_each_entry: number
  embargo_seconds: number
}

type Command = 'freeze' | 'compare' | 'cancel' | 'retry' | 'draft' | 'paper-start' | 'paper-pause' | 'paper-resume' | 'paper-stop'

export class ResearchApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ResearchApiError'
    this.status = status
  }
}

async function request<T>(path: string, body?: object): Promise<T> {
  const response = await fetch(path, body === undefined ? undefined : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const value = await response.json().catch(() => null) as (T & { error?: string }) | null
  if (!value) throw new ResearchApiError(response.ok ? 501 : response.status, `Runner API returned ${response.status} without a JSON research response`)
  if (!response.ok) throw new ResearchApiError(response.status, value.error || `Runner API returned ${response.status}`)
  return value
}

export const researchPaths = {
  workspace: (sourceSessionId: string) => `/api/v1/research/sessions/${encodeURIComponent(sourceSessionId)}`,
  freeze: '/api/v1/research/datasets/freeze',
  compare: '/api/v1/research/jobs',
  cancel: (jobId: string) => `/api/v1/research/jobs/${encodeURIComponent(jobId)}/cancel`,
  retry: (jobId: string) => `/api/v1/research/jobs/${encodeURIComponent(jobId)}/retry`,
  draft: (reportId: string) => `/api/v1/research/reports/${encodeURIComponent(reportId)}/draft`,
  export: (reportId: string, format: 'json' | 'csv') => `/api/v1/research/reports/${encodeURIComponent(reportId)}/export?format=${format}`,
  paperStart: '/api/v1/research/paper-runs',
  paperCommand: (runId: string, command: 'pause' | 'resume' | 'stop') => `/api/v1/research/paper-runs/${encodeURIComponent(runId)}/${command}`,
  paperHistory: (runId: string) => `/api/v1/research/paper-runs/${encodeURIComponent(runId)}/history?limit=1000`,
}

export async function loadPaperHistory(runId: string) {
  const value = await request<PaperHistory>(researchPaths.paperHistory(runId))
  if (!Array.isArray(value.observations) || !Array.isArray(value.decisions) || !Array.isArray(value.outcomes) || !Array.isArray(value.gaps)) {
    throw new ResearchApiError(500, 'The paper history response is incomplete.')
  }
  return value
}

function commandKey(command: Command, identity: string) {
  return `research:${command}:${identity}`
}

const fallbackRequestIds = new Map<string, string>()
const MAX_FALLBACK_REQUEST_IDS = 32

async function operationRequestId(command: Command, identity: string) {
  const key = commandKey(command, identity)
  try {
    const stored = serverStorage.getItem(key)
    if (stored) return { key, value: stored, fallback: false }
  } catch {
    const stored = fallbackRequestIds.get(key)
    if (stored) return { key, value: stored, fallback: true }
  }
  const value = crypto.randomUUID()
  try {
    await serverStorage.setItemAsync(key, value)
    return { key, value, fallback: false }
  } catch {
    fallbackRequestIds.set(key, value)
    while (fallbackRequestIds.size > MAX_FALLBACK_REQUEST_IDS) {
      const oldest = fallbackRequestIds.keys().next().value as string | undefined
      if (!oldest) break
      fallbackRequestIds.delete(oldest)
    }
    return { key, value, fallback: true }
  }
}

function consumeOperationKey(key: string) {
  fallbackRequestIds.delete(key)
  try {
    serverStorage.removeItem(key)
  } catch {
    // The bounded in-memory fallback was already cleared above.
  }
}

export function useResearchWorkspace(sourceSessionId: string) {
  const [snapshot, setSnapshot] = useState<ResearchWorkspaceSnapshot | null>(null)
  const [available, setAvailable] = useState<boolean | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<Command | null>(null)
  const [error, setError] = useState('')
  const [operationWarning, setOperationWarning] = useState('')
  const mounted = useRef(true)
  const source = useRef(sourceSessionId)
  const requestSequence = useRef(0)
  const mutationSequence = useRef(0)
  if (source.current !== sourceSessionId) {
    source.current = sourceSessionId
    requestSequence.current += 1
    mutationSequence.current += 1
  }

  const refresh = useCallback(async (background = false) => {
    const requestedSource = sourceSessionId
    const requestId = ++requestSequence.current
    if (!background) setLoading(true)
    try {
      const value = await request<ResearchWorkspaceSnapshot>(researchPaths.workspace(requestedSource))
      if (!mounted.current || source.current !== requestedSource || requestSequence.current !== requestId) return
      if (!Array.isArray(value.datasets) || !Array.isArray(value.jobs) || !Array.isArray(value.reports) || !Array.isArray(value.paper_runs)) {
        throw new ResearchApiError(501, 'The Batch 3 research workspace route is not available.')
      }
      setSnapshot(value)
      setAvailable(true)
      setError('')
    } catch (failure) {
      if (!mounted.current || source.current !== requestedSource || requestSequence.current !== requestId) return
      if (failure instanceof ResearchApiError && (failure.status === 404 || failure.status === 501)) {
        setAvailable(false)
        setError('')
      } else {
        setAvailable(true)
        setError(failure instanceof Error ? failure.message : 'Research records could not be loaded.')
      }
    } finally {
      if (mounted.current && source.current === requestedSource && requestSequence.current === requestId && !background) setLoading(false)
    }
  }, [sourceSessionId])

  useEffect(() => {
    source.current = sourceSessionId
    requestSequence.current += 1
    mutationSequence.current += 1
    setSnapshot(null)
    setAvailable(null)
    setLoading(true)
    setBusy(null)
    setError('')
    setOperationWarning('')
    void refresh()
  }, [refresh, sourceSessionId])

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const active = snapshot?.jobs.some((job) => ['queued', 'running', 'cancelling', 'interrupted'].includes(job.state))
    || snapshot?.paper_runs.some((run) => run.state === 'running' || run.state === 'interrupted')
  useEffect(() => {
    if (!available || !active) return
    const timer = window.setInterval(() => void refresh(true), 2_000)
    return () => window.clearInterval(timer)
  }, [active, available, refresh])

  const mutate = useCallback(async (command: Command, path: string, identity: string, body: object) => {
    if (busy) return null
    const requestedSource = sourceSessionId
    const mutationId = ++mutationSequence.current
    setBusy(command)
    const operation = await operationRequestId(command, `${requestedSource}:${identity}`)
    setError('')
    setOperationWarning(operation.fallback
      ? 'Local app storage is unavailable. Keep this page open to retry this exact command safely.'
      : '')
    try {
      const value = await request<{ notice?: string }>(path, {
        ...body,
        idempotency_key: operation.value,
      })
      if (!mounted.current || source.current !== requestedSource || mutationSequence.current !== mutationId) return null
      if (command === 'paper-start') consumeOperationKey(operation.key)
      await refresh(true)
      return value
    } catch (failure) {
      if (mounted.current && source.current === requestedSource && mutationSequence.current === mutationId) {
        setError(failure instanceof Error ? failure.message : 'The research command was rejected.')
      }
      return null
    } finally {
      if (mounted.current && source.current === requestedSource && mutationSequence.current === mutationId) setBusy(null)
    }
  }, [busy, refresh, sourceSessionId])

  return {
    snapshot,
    available,
    loading,
    busy,
    error,
    operationWarning,
    refresh,
    freeze: () => mutate('freeze', researchPaths.freeze, sourceSessionId, {
      schema: 'pumpwizard.research-freeze-request/v1', source_session_id: sourceSessionId,
    }),
    compare: (datasetId: string, settings: ResearchSettings) => mutate('compare', researchPaths.compare, `${datasetId}:${JSON.stringify(settings)}`, {
      schema: 'pumpwizard.research-comparison-request/v1', dataset_id: datasetId, settings,
    }),
    cancel: (jobId: string) => mutate('cancel', researchPaths.cancel(jobId), jobId, {
      schema: 'pumpwizard.research-cancel-request/v1', job_id: jobId,
    }),
    retry: (jobId: string) => mutate('retry', researchPaths.retry(jobId), jobId, {
      schema: 'pumpwizard.research-retry-request/v1', job_id: jobId,
    }),
    draft: (reportId: string, candidateId: string) => mutate('draft', researchPaths.draft(reportId), `${reportId}:${candidateId}`, {
      schema: 'pumpwizard.research-draft-request/v1', report_id: reportId, candidate_id: candidateId, accepted: true,
    }),
    paperStart: (strategySha256: string, startingCapitalUsd: number) => mutate('paper-start', researchPaths.paperStart, `${sourceSessionId}:${strategySha256}:${startingCapitalUsd}`, {
      schema: 'pumpwizard.paper-start-request/v1', source_session_id: sourceSessionId, strategy_sha256: strategySha256,
      starting_capital_usd: startingCapitalUsd, confirmation: 'START PAPER RESEARCH',
    }),
    paperCommand: (runId: string, command: 'pause' | 'resume' | 'stop') => mutate(`paper-${command}` as Command, researchPaths.paperCommand(runId, command), `${runId}:${command}`, {
      schema: 'pumpwizard.paper-command/v1', run_id: runId, command,
    }),
  }
}
