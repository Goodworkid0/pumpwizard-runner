import { serverStorage } from './serverStorage'
import { WalletWorkspaceScope } from './walletWorkspaceScope'
import { useCallback, useContext, useEffect, useRef, useState } from 'react'

export type CampaignState = 'running' | 'paused' | 'degraded' | 'capped' | 'completed' | 'stopped'

export type CampaignStrategyChoice = {
  source_session_id: string
  label: string
  strategy_package_sha256: string
  family: string
  supported: boolean
  reason: string | null
}

export type CampaignProviderChoice = {
  id: string
  label: string
  source_class: string
  minimum_cap: number
  maximum_cap: number
  price_basis: 'known' | 'mixed' | 'unknown'
  marginal_request_cost_usd: number | null
  fixed_subscription_cost_usd: number | null
}

export type CampaignCapabilities = {
  strategies: CampaignStrategyChoice[]
  providers: CampaignProviderChoice[]
  limits: {
    maximum_arms: number
    maximum_priority_mints: number
    maximum_planned_duration_seconds: number
    maximum_requests: number
    maximum_storage_bytes: number
    maximum_concurrency: number
    minimum_cadence_seconds: number
  }
  defaults: {
    provider_id: string
    minimum_cap: number
    maximum_cap: number
    result_limit: number
    cadence_seconds: number
    duration_seconds: number
    request_ceiling: number
    runtime_ceiling_seconds: number
    storage_ceiling_bytes: number
    concurrency_ceiling: number
    simulated_capital_usd: number
    cost_model: string
    execution_model: string
    maximum_source_age_seconds: number
    minimum_independent_tokens: number
    minimum_observations: number
    maximum_gap_seconds: number
  }
}

export type CampaignDraft = {
  campaign_key: string
  supersedes_campaign_id: string | null
  source: {
    provider: string
    minimum_cap: number
    maximum_cap: number
    priority_mints: string[]
    limit: number
    cadence_seconds: number
  }
  arms: {
    id: string
    source_session_id: string
    strategy_sha256: string
    simulated_capital_usd: number
  }[]
  assumptions: {
    cost_model: string
    execution_model: string
  }
  coverage: {
    max_source_age_seconds: number
    minimum_independent_tokens: number
    minimum_observations: number
    maximum_gap_seconds: number
  }
  duration_seconds: number
  quotas: {
    max_requests: number
    max_active_runtime_seconds: number
    max_storage_bytes: number
    max_concurrency: number
  }
  provider_cost: {
    currency: 'USD'
    marginal_cost_per_request: number | null
    fixed_subscription_cost: number | null
    price_basis: 'known' | 'mixed' | 'unknown'
  }
}

export type CampaignGap = {
  id: string
  started_at: number
  ended_at: number | null
  reason: string
  detail: string
}

export type CampaignArm = {
  id: string
  source_session_id: string
  strategy_package_sha256: string
  paper_run_id: string
  label: string
  state: 'running' | 'paused' | 'stopped' | 'completed' | 'lagging' | 'stalled' | 'faulted'
  simulated_capital_usd: number
  last_decided_at: number | null
  lag_seconds: number | null
  observations: number
  decisions: number
  opportunities: number
  repair: string | null
}

export type ForwardCampaign = {
  schema: 'pumpwizard.forward-campaign/v1'
  id: string
  campaign_key: string
  manifest_digest: string
  supersedes_campaign_id: string | null
  state: CampaignState
  state_reason: string
  created_at: number
  start_at: number
  updated_at: number
  stopped_at: number | null
  planned_duration_seconds: number
  actual_operating_seconds: number
  service: {
    status: 'healthy' | 'degraded' | 'disconnected' | 'restarting' | 'unknown'
    last_seen_at: number | null
    restart_resume_required: boolean
    repair: string | null
  }
  authority: {
    wallet_allocation: boolean
    signer: boolean
    orders: boolean
    submission: boolean
  }
  provider: {
    id: string
    label: string
    status: 'healthy' | 'degraded' | 'outage' | 'unknown'
    cost_status: 'known' | 'estimated' | 'unknown'
    source_at: number | null
    first_received_at: number | null
    source_age_seconds: number | null
    request_count: number
    request_ceiling: number
    response_bytes: number
    storage_ceiling_bytes: number
    latency_ms: number | null
    error_count: number
    retry_count: number
    shared_request_count: number
    attributed_request_units: number
    repair: string | null
  }
  source: CampaignDraft['source']
  assumptions: CampaignDraft['assumptions']
  coverage: CampaignDraft['coverage']
  quotas: CampaignDraft['quotas']
  operating_intervals: { id: string; started_at: number; ended_at: number | null; reason: string }[]
  gaps: CampaignGap[]
  blockers: { code: string; message: string; retryable: boolean; repair: string; source_id: string }[]
  arms: CampaignArm[]
  latest_report_id: string | null
}

export type PerformanceComparison = {
  id: string
  kind: 'cash' | 'current' | 'arm' | 'holding'
  label: string
  arm_id: string | null
  comparable: boolean
  comparability_reason: string | null
  starting_capital_usd: number
  ending_capital_usd: number | null
  modeled_net_pnl_usd: number | null
  modeled_realized_pnl_usd: number | null
  modeled_unrealized_pnl_usd: number | null
  maximum_drawdown_fraction: number | null
  exposure_fraction: number | null
  turnover_usd: number | null
  opportunities: number
  independent_tokens: number
  coverage_fraction: number | null
  operating_seconds: number
  unpriced_positions: number
  censored_positions: number
  known_costs_usd: number
  estimated_costs_usd: number | null
  cost_status: 'known' | 'estimated' | 'unknown'
  curve: { at: number; equity_usd: number | null; segment_id: string | null; gap_reason: string | null }[]
}

export type ReconciliationReceipt = {
  id: string
  evidence_class: 'modeled_paper' | 'retrospective' | 'recorded_execution'
  classification: 'pending' | 'finalized_success' | 'fee_only_failure' | 'unresolved' | string
  record_kind: string
  source_label: string
  occurred_at: number | null
  ownership: string
  cash_delta: string | null
  token_delta: string | null
  fee: string | null
  discrepancy: string | null
  source_href: string
}

export type ForwardPerformanceReport = {
  schema: 'pumpwizard.forward-performance-report/v1'
  id: string
  campaign_id: string
  manifest_digest: string
  previous_report_id: string | null
  created_at: number
  as_of: number
  actual_period: { first_at: number | null; last_at: number | null; operating_seconds: number }
  model_label: string
  engine_versions: string[]
  readiness: { state: 'not_ready' | 'exploratory' | 'ready'; reasons: string[] }
  ranking: { comparison_id: string | null; label: string; reason: string; fully_costed: boolean }
  comparisons: PerformanceComparison[]
  daily: { id: string; label: string; first_at: number; last_at: number; comparison_ids: string[]; gaps: number }[]
  gaps: CampaignGap[]
  limitations: string[]
  receipts: ReconciliationReceipt[]
  reconciliation: {
    status: 'reconciled' | 'mismatch' | 'unknown'
    external_cash_flow: string | null
    unresolved_count: number
    reasons: string[]
  }
}

export type EvidenceAlert = {
  schema: 'pumpwizard.evidence-alert/v1'
  id: string
  campaign_id: string
  kind: string
  severity: 'info' | 'warning' | 'critical'
  state: 'open' | 'resolved'
  source: { kind: string; type: string; id: string; href: string }
  title: string
  message: string
  repair: string
  first_occurred_at: number
  last_occurred_at: number
  occurrence_count: number
  recurrence_count: number
  occurrence_generation: number
  acknowledged_at: number | null
  resolved_at: number | null
  acknowledgment_changes_condition: false
  local_only: true
  external_delivery: false
}

export type CampaignWorkspaceSnapshot = {
  schema: 'pumpwizard.forward-campaign-workspace/v1'
  source_session_id: string
  capabilities: CampaignCapabilities
  campaigns: ForwardCampaign[]
  reports: ForwardPerformanceReport[]
  alerts: EvidenceAlert[]
  alerts_local_only: true
  evaluated_at: number
}

type CampaignCommand = 'start' | 'pause' | 'resume' | 'stop' | 'report' | 'acknowledge'

export class CampaignApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'CampaignApiError'
    this.status = status
  }
}

async function request<T>(path: string, body?: object): Promise<T> {
  const response = await fetch(path, body === undefined ? undefined : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const value = await response.json().catch(() => null) as (T & { error?: string; message?: string }) | null
  if (!value) throw new CampaignApiError(response.status, `Runner API returned ${response.status} without campaign JSON`)
  if (!response.ok) throw new CampaignApiError(response.status, value.error || value.message || `Runner API returned ${response.status}`)
  return value
}

export const campaignPaths = {
  campaigns: '/api/v1/research/campaigns?limit=100',
  sessions: '/api/v1/sessions',
  alerts: '/api/v1/research/alerts?limit=500',
  service: '/api/v1/service/status',
  start: '/api/v1/research/campaigns',
  command: (campaignId: string, command: 'pause' | 'resume' | 'stop') => `/api/v1/research/campaigns/${encodeURIComponent(campaignId)}/${command}`,
  reports: (campaignId: string) => `/api/v1/research/campaigns/${encodeURIComponent(campaignId)}/reports?limit=100`,
  report: (campaignId: string) => `/api/v1/research/campaigns/${encodeURIComponent(campaignId)}/reports`,
  acknowledge: (alertId: string) => `/api/v1/research/alerts/${encodeURIComponent(alertId)}/acknowledge`,
  export: (reportId: string, format: 'json' | 'csv') => `/api/v1/research/campaign-reports/${encodeURIComponent(reportId)}/export?format=${format}`,
}

function operationKey(command: CampaignCommand, identity: string) {
  return `forward-campaign:${command}:${identity}`
}

const fallbackRequestIds = new Map<string, string>()
const MAX_FALLBACK_REQUEST_IDS = 32

async function operationRequestId(command: CampaignCommand, identity: string) {
  const key = operationKey(command, identity)
  try {
    const stored = serverStorage.getItem(key)
    if (stored) return { value: stored, fallback: false }
    const value = crypto.randomUUID()
    await serverStorage.setItemAsync(key, value)
    return { value, fallback: false }
  } catch {
    const stored = fallbackRequestIds.get(key)
    if (stored) return { value: stored, fallback: true }
    const value = crypto.randomUUID()
    fallbackRequestIds.set(key, value)
    while (fallbackRequestIds.size > MAX_FALLBACK_REQUEST_IDS) {
      const oldest = fallbackRequestIds.keys().next().value as string | undefined
      if (!oldest) break
      fallbackRequestIds.delete(oldest)
    }
    return { value, fallback: true }
  }
}

type JsonObject = Record<string, unknown>

function object(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {}
}

function rows(value: unknown): JsonObject[] {
  return Array.isArray(value) ? value.map(object) : []
}

function number(value: unknown, fallback = 0) {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function nullableNumber(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function text(value: unknown, fallback = '') {
  return typeof value === 'string' ? value : fallback
}

function capabilitiesFromSessions(value: JsonObject): CampaignCapabilities {
  const strategies = rows(value.rows).map((row) => {
    const instance = object(row.instance)
    const strategyPackage = object(instance.package)
    const strategy = object(strategyPackage.strategy)
    const digest = text(instance.strategy_digest)
    const sessionId = text(instance.id)
    const compatibility = text(instance.compatibility)
    // The list endpoint intentionally returns the retained strategy digest rather
    // than duplicating every immutable package. Campaign start resolves that
    // digest through the backend library and rejects an identity mismatch.
    const supported = Boolean(row.paper_research_allowed === true && sessionId && digest && compatibility === 'monitoring-ready')
    return {
      source_session_id: sessionId,
      label: text(instance.label, text(strategy.label, sessionId || 'Saved strategy')),
      strategy_package_sha256: digest,
      family: text(strategy.family, text(strategy.id, text(instance.strategy_id, 'saved strategy'))),
      supported,
      reason: supported ? null : row.paper_research_allowed !== true ? 'Paper campaigns require a monitoring session.' : compatibility
        ? `This saved session is ${compatibility.replace(/-/g, ' ')}.`
        : 'This saved session has no retained immutable strategy identity.',
    }
  })
  return {
    strategies,
    providers: [{
      id: 'CompositePumpMarketSource',
      label: 'Configured public market composite',
      source_class: 'CompositePumpMarketSource',
      minimum_cap: 1_000,
      maximum_cap: 50_000,
      price_basis: 'unknown',
      marginal_request_cost_usd: null,
      fixed_subscription_cost_usd: null,
    }],
    limits: {
      maximum_arms: 3,
      maximum_priority_mints: 30,
      maximum_planned_duration_seconds: 366 * 24 * 60 * 60,
      maximum_requests: 100_000,
      maximum_storage_bytes: 2 * 1024 * 1024 * 1024,
      maximum_concurrency: 4,
      minimum_cadence_seconds: 1,
    },
    defaults: {
      provider_id: 'CompositePumpMarketSource',
      minimum_cap: 1_000,
      maximum_cap: 50_000,
      result_limit: 24,
      cadence_seconds: 15,
      duration_seconds: 24 * 60 * 60,
      request_ceiling: 5_760,
      runtime_ceiling_seconds: 24 * 60 * 60,
      storage_ceiling_bytes: 100_000_000,
      concurrency_ceiling: 1,
      simulated_capital_usd: 1_000,
      cost_model: 'strategy package costs plus declared provider cost status',
      execution_model: 'prospective paper replay v3; no funded fills',
      maximum_source_age_seconds: 120,
      minimum_independent_tokens: 20,
      minimum_observations: 100,
      maximum_gap_seconds: 15 * 60,
    },
  }
}

type ServiceView = {
  status: ForwardCampaign['service']['status']
  lastSeenAt: number | null
  repair: string | null
}

function serviceView(value: JsonObject | null): ServiceView {
  const service = object(value?.service)
  const health = text(service.health, 'unknown')
  const status: ForwardCampaign['service']['status'] = health === 'healthy'
    ? 'healthy'
    : health === 'degraded' || health === 'stale'
      ? 'degraded'
      : health === 'stopped' ? 'disconnected' : 'unknown'
  return {
    status,
    lastSeenAt: nullableNumber(service.heartbeat_at),
    repair: status === 'healthy' ? null : text(service.stop_reason, 'Inspect the local service status before resuming collection.'),
  }
}

function normalizeCampaign(raw: JsonObject, service: ServiceView, labels: Map<string, string>, latestReportId: string | null): ForwardCampaign {
  const progress = object(raw.progress)
  const usage = object(raw.usage)
  const quotas = object(raw.quotas)
  const providerCost = object(raw.provider_cost)
  const coverage = object(raw.coverage)
  const blocker = Object.keys(object(raw.blocker)).length ? object(raw.blocker) : null
  const source = object(raw.source)
  const authority = object(raw.authority)
  const maximumAge = nullableNumber(usage.maximum_source_age_seconds)
  const allowedAge = nullableNumber(coverage.max_source_age_seconds)
  const errors = number(usage.errors)
  const sourceStatus: ForwardCampaign['provider']['status'] = (
    blocker && ['provider_outage', 'provider_error', 'provider_timeout'].includes(text(blocker.code))
  ) ? 'outage' : maximumAge === null ? 'unknown' : errors > 0 || (allowedAge !== null && maximumAge > allowedAge) ? 'degraded' : 'healthy'
  const campaignId = text(raw.id)
  const rawArms = rows(raw.arms)
  const arms: CampaignArm[] = rawArms.map((arm, index) => {
    const counts = object(arm.counts)
    const fault = Object.keys(object(arm.fault)).length ? object(arm.fault) : null
    const rawState = text(arm.state, 'paused')
    const state: CampaignArm['state'] = rawState === 'interrupted'
      ? fault ? 'faulted' : 'stalled'
      : ['running', 'paused', 'stopped', 'completed'].includes(rawState)
        ? rawState as CampaignArm['state'] : 'lagging'
    const cursor = Array.isArray(arm.cursor) ? arm.cursor : []
    const lastEvidenceAt = nullableNumber(cursor[0])
    return {
      id: text(arm.id, `arm-${index + 1}`),
      source_session_id: text(arm.source_session_id),
      strategy_package_sha256: text(arm.strategy_sha256),
      paper_run_id: text(arm.paper_run_id),
      label: labels.get(text(arm.source_session_id)) || `Arm ${index + 1}`,
      state,
      simulated_capital_usd: number(arm.simulated_capital_usd),
      last_decided_at: null,
      lag_seconds: lastEvidenceAt === null ? null : Math.max(0, Date.now() / 1_000 - lastEvidenceAt),
      observations: number(counts.observations),
      decisions: number(counts.decisions),
      opportunities: number(counts.decisions),
      repair: fault ? text(fault.repair, text(fault.message, 'Inspect the durable paper fault.')) : null,
    }
  })
  const providerPrice = text(providerCost.price_basis, 'unknown')
  const providerCostStatus: ForwardCampaign['provider']['cost_status'] = providerPrice === 'known'
    ? 'known' : providerPrice === 'mixed' ? 'estimated' : 'unknown'
  const rawIntervals = rows(raw.operating_intervals).sort((left, right) => number(left.started_at) - number(right.started_at))
  const explicitGaps = rows(raw.gaps).map((gap, index) => ({
    id: text(gap.id, `campaign-gap-${index + 1}`),
    started_at: number(gap.started_at),
    ended_at: nullableNumber(gap.ended_at),
    reason: text(gap.reason, 'campaign operating gap'),
    detail: text(gap.detail, 'No collection or decisions are attributed to this interval.'),
  }))
  const derivedGaps: CampaignGap[] = []
  rawIntervals.forEach((interval, index) => {
    if (index === 0) return
    const previousEnd = nullableNumber(rawIntervals[index - 1].ended_at)
    const nextStart = number(interval.started_at)
    if (previousEnd !== null && nextStart > previousEnd) derivedGaps.push({
      id: `campaign-gap-${campaignId}-${index}`,
      started_at: previousEnd,
      ended_at: nextStart,
      reason: 'Campaign collection paused',
      detail: 'This interval is excluded from operating duration and matched evidence.',
    })
  })
  const latestInterval = rawIntervals.length ? rawIntervals[rawIntervals.length - 1] : undefined
  const latestEnd = latestInterval ? nullableNumber(latestInterval.ended_at) : null
  if (latestEnd !== null && ['paused', 'capped'].includes(text(raw.state))) derivedGaps.push({
    id: `campaign-gap-${campaignId}-current`,
    started_at: latestEnd,
    ended_at: null,
    reason: blocker ? text(blocker.message, 'Campaign collection paused') : 'Campaign collection paused',
    detail: blocker ? text(blocker.repair, 'Explicit resume is required.') : 'Explicit resume is required before collection continues.',
  })
  return {
    schema: 'pumpwizard.forward-campaign/v1',
    id: campaignId,
    campaign_key: text(raw.campaign_key),
    manifest_digest: text(raw.request_sha256),
    supersedes_campaign_id: typeof raw.supersedes_campaign_id === 'string' ? raw.supersedes_campaign_id : null,
    state: text(raw.state, 'paused') as CampaignState,
    state_reason: blocker ? text(blocker.message, text(raw.state)) : `Campaign is ${text(raw.state, 'paused')}.`,
    created_at: number(raw.created_at, number(raw.start_at)),
    start_at: number(raw.start_at),
    updated_at: number(raw.updated_at),
    stopped_at: nullableNumber(raw.stopped_at),
    planned_duration_seconds: number(raw.duration_seconds, number(progress.planned_duration_seconds)),
    actual_operating_seconds: number(progress.active_seconds),
    service: {
      status: service.status,
      last_seen_at: service.lastSeenAt,
      restart_resume_required: text(blocker?.code) === 'service_restart',
      repair: blocker ? text(blocker.repair, service.repair || '') : service.repair,
    },
    authority: {
      wallet_allocation: authority.wallet_allocation === true,
      signer: authority.signer === true,
      orders: authority.orders === true,
      submission: authority.submission === true,
    },
    provider: {
      id: text(source.provider, 'CompositePumpMarketSource'),
      label: text(source.provider, 'Configured market provider'),
      status: sourceStatus,
      cost_status: providerCostStatus,
      source_at: null,
      first_received_at: nullableNumber(usage.last_request_at),
      source_age_seconds: maximumAge,
      request_count: number(usage.requests, number(raw.request_count)),
      request_ceiling: number(quotas.max_requests),
      response_bytes: number(usage.response_bytes),
      storage_ceiling_bytes: number(quotas.max_storage_bytes),
      latency_ms: null,
      error_count: errors,
      retry_count: number(usage.timeout_events) + number(usage.rate_limit_events),
      shared_request_count: number(usage.requests),
      attributed_request_units: number(usage.requests),
      repair: blocker ? text(blocker.repair, 'Inspect campaign provider history.') : null,
    },
    source: raw.source as CampaignDraft['source'],
    assumptions: raw.assumptions as CampaignDraft['assumptions'],
    coverage: raw.coverage as CampaignDraft['coverage'],
    quotas: raw.quotas as CampaignDraft['quotas'],
    operating_intervals: rawIntervals.map((interval, index) => ({
      id: text(interval.id, `interval-${index + 1}`),
      started_at: number(interval.started_at),
      ended_at: nullableNumber(interval.ended_at),
      reason: text(interval.reason, 'campaign operating'),
    })),
    gaps: explicitGaps.length ? explicitGaps : derivedGaps,
    blockers: blocker ? [{
      code: text(blocker.code, 'campaign_blocked'),
      message: text(blocker.message, 'Campaign operation is blocked.'),
      retryable: blocker.retryable === true,
      repair: text(blocker.repair, 'Inspect the campaign state.'),
      source_id: text(blocker.source_id, campaignId),
    }] : [],
    arms,
    latest_report_id: latestReportId,
  }
}

function normalizeAlert(raw: JsonObject): EvidenceAlert {
  const source = object(raw.source)
  return {
    schema: 'pumpwizard.evidence-alert/v1',
    id: text(raw.id),
    campaign_id: text(raw.campaign_id),
    kind: text(raw.kind, text(raw.condition_key)),
    severity: text(raw.severity, 'warning') as EvidenceAlert['severity'],
    state: text(raw.state, 'open') as EvidenceAlert['state'],
    source: {
      kind: text(source.kind, text(source.type, 'campaign')),
      type: text(source.type, text(source.kind, 'campaign')),
      id: text(source.id),
      href: text(source.href, `/research/campaigns/${encodeURIComponent(text(raw.campaign_id))}`),
    },
    title: text(raw.title, 'Evidence condition'),
    message: text(raw.message, text(raw.detail, 'Inspect the linked evidence.')),
    repair: text(raw.repair, text(raw.detail, 'Inspect the linked evidence.')),
    first_occurred_at: number(raw.first_occurred_at, number(raw.first_at)),
    last_occurred_at: number(raw.last_occurred_at, number(raw.last_at)),
    occurrence_count: number(raw.occurrence_count, 1),
    recurrence_count: number(raw.recurrence_count, Math.max(0, number(raw.generation, 1) - 1)),
    occurrence_generation: number(raw.occurrence_generation, number(raw.generation, 1)),
    acknowledged_at: nullableNumber(raw.acknowledged_at),
    resolved_at: nullableNumber(raw.resolved_at),
    acknowledgment_changes_condition: false,
    local_only: true,
    external_delivery: false,
  }
}

async function optionalRequest(path: string) {
  try {
    return await request<JsonObject>(path)
  } catch {
    return null
  }
}

async function loadWorkspace(sourceSessionId: string, scopeQuery: string): Promise<CampaignWorkspaceSnapshot> {
  const [campaignEnvelope, sessionsEnvelope, alertEnvelope, serviceEnvelope] = await Promise.all([
    request<JsonObject>(campaignPaths.campaigns),
    request<JsonObject>(`${campaignPaths.sessions}${scopeQuery}`),
    request<JsonObject>(campaignPaths.alerts),
    optionalRequest(campaignPaths.service),
  ])
  if (!Array.isArray(campaignEnvelope.campaigns) || !Array.isArray(sessionsEnvelope.rows) || !Array.isArray(alertEnvelope.alerts)) {
    throw new CampaignApiError(501, 'The forward-campaign API response is incomplete.')
  }
  const rawCampaigns = rows(campaignEnvelope.campaigns).filter((campaign) => (
    rows(campaign.arms).some((arm) => arm.source_session_id === sourceSessionId)
  ))
  const reportEnvelopes = await Promise.all(rawCampaigns.map((campaign) => (
    request<JsonObject>(campaignPaths.reports(text(campaign.id)))
  )))
  const reports = reportEnvelopes.flatMap((value) => rows(value.campaign_reports)) as unknown as ForwardPerformanceReport[]
  const latestByCampaign = new Map<string, string>()
  reports.forEach((report) => {
    if (!latestByCampaign.has(report.campaign_id)) latestByCampaign.set(report.campaign_id, report.id)
  })
  const capabilities = capabilitiesFromSessions(sessionsEnvelope)
  const labels = new Map(capabilities.strategies.map((strategy) => [strategy.source_session_id, strategy.label]))
  const service = serviceView(serviceEnvelope)
  return {
    schema: 'pumpwizard.forward-campaign-workspace/v1',
    source_session_id: sourceSessionId,
    capabilities,
    campaigns: rawCampaigns.map((campaign) => normalizeCampaign(
      campaign, service, labels, latestByCampaign.get(text(campaign.id)) || null,
    )),
    reports,
    alerts: rows(alertEnvelope.alerts).map(normalizeAlert),
    alerts_local_only: true,
    evaluated_at: Date.now() / 1_000,
  }
}

export function makeCampaignDraft(capabilities: CampaignCapabilities, sourceSessionId: string, supersedesCampaignId: string | null = null): CampaignDraft {
  const supported = capabilities.strategies.filter((strategy) => strategy.supported)
  const chosen = supported.find((strategy) => strategy.source_session_id === sourceSessionId) || supported[0]
  const defaults = capabilities.defaults
  return {
    campaign_key: crypto.randomUUID(),
    supersedes_campaign_id: supersedesCampaignId,
    source: {
      provider: defaults.provider_id,
      minimum_cap: defaults.minimum_cap,
      maximum_cap: defaults.maximum_cap,
      priority_mints: [],
      limit: defaults.result_limit,
      cadence_seconds: defaults.cadence_seconds,
    },
    arms: chosen ? [{
      id: 'arm-1',
      source_session_id: chosen.source_session_id,
      strategy_sha256: chosen.strategy_package_sha256,
      simulated_capital_usd: defaults.simulated_capital_usd,
    }] : [],
    assumptions: {
      cost_model: defaults.cost_model,
      execution_model: defaults.execution_model,
    },
    coverage: {
      max_source_age_seconds: defaults.maximum_source_age_seconds,
      minimum_independent_tokens: defaults.minimum_independent_tokens,
      minimum_observations: defaults.minimum_observations,
      maximum_gap_seconds: defaults.maximum_gap_seconds,
    },
    duration_seconds: defaults.duration_seconds,
    quotas: {
      max_requests: defaults.request_ceiling,
      max_active_runtime_seconds: defaults.runtime_ceiling_seconds,
      max_storage_bytes: defaults.storage_ceiling_bytes,
      max_concurrency: defaults.concurrency_ceiling,
    },
    provider_cost: {
      currency: 'USD',
      marginal_cost_per_request: null,
      fixed_subscription_cost: null,
      price_basis: 'unknown',
    },
  }
}

export function useCampaignWorkspace(sourceSessionId: string, enabled: boolean) {
  const scopeQuery = useContext(WalletWorkspaceScope)
  const [snapshot, setSnapshot] = useState<CampaignWorkspaceSnapshot | null>(null)
  const [available, setAvailable] = useState<boolean | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState<CampaignCommand | null>(null)
  const [error, setError] = useState('')
  const [operationWarning, setOperationWarning] = useState('')
  const mounted = useRef(true)
  const source = useRef(sourceSessionId)
  const requestSequence = useRef(0)
  const mutationSequence = useRef(0)

  const refresh = useCallback(async (background = false) => {
    if (!enabled) return
    const requestedSource = sourceSessionId
    const requestId = ++requestSequence.current
    if (!background) setLoading(true)
    try {
      const value = await loadWorkspace(requestedSource, scopeQuery)
      if (!mounted.current || source.current !== requestedSource || requestSequence.current !== requestId) return
      setSnapshot(value)
      setAvailable(true)
      setError('')
    } catch (failure) {
      if (!mounted.current || source.current !== requestedSource || requestSequence.current !== requestId) return
      if (failure instanceof CampaignApiError && (failure.status === 404 || failure.status === 501)) {
        setAvailable(false)
        setError('')
      } else {
        setAvailable(true)
        setError(failure instanceof Error ? failure.message : 'Campaign records could not be loaded.')
      }
    } finally {
      if (mounted.current && source.current === requestedSource && requestSequence.current === requestId && !background) setLoading(false)
    }
  }, [enabled, sourceSessionId, scopeQuery])

  useEffect(() => {
    source.current = sourceSessionId
    requestSequence.current += 1
    mutationSequence.current += 1
    setSnapshot(null)
    setAvailable(null)
    setBusy(null)
    setError('')
    setOperationWarning('')
  }, [sourceSessionId, scopeQuery])

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  useEffect(() => {
    if (enabled && available !== false && snapshot === null && !loading) void refresh()
  }, [available, enabled, loading, refresh, snapshot])

  const active = snapshot?.campaigns.some((campaign) => ['running', 'degraded'].includes(campaign.state))
  useEffect(() => {
    if (!enabled || !available || !active) return
    const timer = window.setInterval(() => void refresh(true), 2_000)
    return () => window.clearInterval(timer)
  }, [active, available, enabled, refresh])

  const mutate = useCallback(async <T,>(command: CampaignCommand, path: string, identity: string, body: object, includeIdempotency = true) => {
    if (busy) return null
    const requestedSource = sourceSessionId
    const mutationId = ++mutationSequence.current
    setBusy(command)
    const operation = includeIdempotency
      ? await operationRequestId(command, `${requestedSource}:${identity}`)
      : null
    setError('')
    setOperationWarning(operation?.fallback
      ? 'Local app storage is unavailable. Keep this page open to retry this exact command safely.'
      : '')
    try {
      const value = await request<T>(path, {
        ...body,
        ...(operation ? { idempotency_key: operation.value } : {}),
      })
      if (!mounted.current || source.current !== requestedSource || mutationSequence.current !== mutationId) return null
      await refresh(true)
      return value
    } catch (failure) {
      if (mounted.current && source.current === requestedSource && mutationSequence.current === mutationId) {
        setError(failure instanceof Error ? failure.message : 'The campaign command was rejected.')
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
    start: async (draft: CampaignDraft) => {
      const value = await mutate<{ campaign: { id: string } }>('start', campaignPaths.start, draft.campaign_key, {
        schema: 'pumpwizard.forward-campaign-start/v1',
        campaign: {
          schema: 'pumpwizard.forward-campaign-request/v1',
          ...draft,
        },
        confirmation: 'START FORWARD CAMPAIGN',
      })
      return value?.campaign || null
    },
    command: (campaign: ForwardCampaign, command: 'pause' | 'resume' | 'stop') => mutate<{ campaign: JsonObject }>(command, campaignPaths.command(campaign.id, command), `${campaign.id}:${campaign.updated_at}:${command}`, {
      schema: 'pumpwizard.forward-campaign-command/v1',
      campaign_id: campaign.id,
      command,
      ...(command === 'stop' ? { confirmation: campaign.id } : {}),
    }),
    report: (campaign: ForwardCampaign, previousReportId: string | null) => mutate<{ campaign_report: ForwardPerformanceReport }>('report', campaignPaths.report(campaign.id), `${campaign.id}:${campaign.updated_at}:${previousReportId || 'first'}`, {
      schema: 'pumpwizard.forward-report-request/v1',
      campaign_id: campaign.id,
      previous_report_id: previousReportId,
      created_at: Date.now() / 1_000,
    }, false),
    acknowledge: (alert: EvidenceAlert) => mutate<{ alert: EvidenceAlert }>('acknowledge', campaignPaths.acknowledge(alert.id), `${alert.id}:${alert.occurrence_generation}`, {
      schema: 'pumpwizard.evidence-alert-acknowledgment/v1',
      alert_id: alert.id,
    }, false),
  }
}
