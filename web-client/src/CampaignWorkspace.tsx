import { useEffect, useMemo, useState } from 'react'
import type {
  CampaignCapabilities,
  CampaignDraft,
  EvidenceAlert,
  ForwardCampaign,
  ForwardPerformanceReport,
  PerformanceComparison,
} from './campaignWorkspace'
import { campaignPaths, makeCampaignDraft, useCampaignWorkspace } from './campaignWorkspace'
import { EvidenceAssistantPanel } from './EvidenceAssistantPanel'

type CampaignSection = 'operation' | 'performance' | 'receipts' | 'alerts'
const CAMPAIGN_EVIDENCE_QUESTIONS = ['summary', 'missing_evidence'] as const
const REPORT_EVIDENCE_QUESTIONS = ['benchmark_result', 'cost_impact', 'missing_evidence'] as const
const RECEIPT_EVIDENCE_QUESTIONS = ['receipt_status', 'missing_evidence'] as const

function dateTime(value: number | null) {
  return value === null ? 'Unknown' : new Date(value * 1_000).toLocaleString()
}

function duration(seconds: number | null) {
  if (seconds === null || !Number.isFinite(seconds)) return 'Unknown'
  if (seconds < 60) return `${Math.round(seconds)} seconds`
  if (seconds < 3_600) return `${Math.round(seconds / 60)} minutes`
  if (seconds < 86_400) return `${(seconds / 3_600).toFixed(1)} hours`
  return `${(seconds / 86_400).toFixed(1)} days`
}

function usd(value: number | null, signed = false) {
  if (value === null || !Number.isFinite(value)) return 'Unknown'
  const sign = value < 0 ? '-' : signed && value > 0 ? '+' : ''
  return `${sign}$${Math.abs(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function percentage(value: number | null) {
  return value === null || !Number.isFinite(value) ? 'Unknown' : `${(value * 100).toFixed(1)}%`
}

function shortId(value: string | null) {
  if (!value) return 'Not available'
  return value.length <= 22 ? value : `${value.slice(0, 10)}…${value.slice(-8)}`
}

function boundedNumber(value: string, fallback: number) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function selectedProvider(capabilities: CampaignCapabilities, draft: CampaignDraft) {
  return capabilities.providers.find((provider) => provider.id === draft.source.provider) || null
}

function CampaignSetup({ capabilities, sourceSessionId, supersedes, busy, error, start, cancel }: {
  capabilities: CampaignCapabilities
  sourceSessionId: string
  supersedes: string | null
  busy: boolean
  error: string
  start: (draft: CampaignDraft) => Promise<unknown>
  cancel: (() => void) | null
}) {
  const [draft, setDraft] = useState(() => makeCampaignDraft(capabilities, sourceSessionId, supersedes))
  const [reviewing, setReviewing] = useState(false)
  const [attempted, setAttempted] = useState(false)
  const provider = selectedProvider(capabilities, draft)
  const limits = capabilities.limits
  const selectedSessions = new Set(draft.arms.map((arm) => arm.source_session_id))

  const change = (transform: (current: CampaignDraft) => CampaignDraft) => {
    setDraft((current) => {
      const next = transform(current)
      return attempted ? { ...next, campaign_key: crypto.randomUUID() } : next
    })
    if (attempted) setAttempted(false)
    setReviewing(false)
  }

  const toggleStrategy = (strategy: CampaignCapabilities['strategies'][number]) => change((current) => {
    const exists = current.arms.some((arm) => arm.source_session_id === strategy.source_session_id)
    let ordinal = 1
    while (current.arms.some((arm) => arm.id === `arm-${ordinal}`)) ordinal += 1
    return {
      ...current,
      arms: exists
        ? current.arms.filter((arm) => arm.source_session_id !== strategy.source_session_id)
        : [...current.arms, {
          id: `arm-${ordinal}`,
          source_session_id: strategy.source_session_id,
          strategy_sha256: strategy.strategy_package_sha256,
          simulated_capital_usd: capabilities.defaults.simulated_capital_usd,
        }],
    }
  })

  const valid = draft.arms.length > 0
    && draft.arms.length <= limits.maximum_arms
    && draft.arms.every((arm) => arm.simulated_capital_usd > 0)
    && draft.source.maximum_cap > draft.source.minimum_cap
    && draft.source.limit >= 1 && draft.source.limit <= 100
    && draft.duration_seconds >= 60
    && draft.duration_seconds <= limits.maximum_planned_duration_seconds
    && draft.source.cadence_seconds >= limits.minimum_cadence_seconds
    && draft.quotas.max_requests > 0 && draft.quotas.max_requests <= limits.maximum_requests
    && draft.quotas.max_active_runtime_seconds <= draft.duration_seconds
    && draft.quotas.max_active_runtime_seconds >= 1
    && draft.quotas.max_storage_bytes >= 1_024 && draft.quotas.max_storage_bytes <= limits.maximum_storage_bytes
    && draft.quotas.max_concurrency > 0 && draft.quotas.max_concurrency <= limits.maximum_concurrency
    && draft.coverage.minimum_independent_tokens >= 1
    && draft.coverage.minimum_observations >= 1
    && draft.coverage.maximum_gap_seconds >= 1
    && (draft.provider_cost.price_basis !== 'known'
      || (draft.provider_cost.marginal_cost_per_request !== null
        && draft.provider_cost.fixed_subscription_cost !== null))

  if (reviewing) return <div className="campaign-setup-review">
    <section className="campaign-safety"><span aria-hidden="true">◎</span><div><small>OBSERVATION ONLY</small><h3>Confirm one bounded forward campaign</h3><p>This creates paper evidence. Wallet allocation, signing, orders and submission all remain disabled.</p></div></section>
    <section className="campaign-review-manifest" aria-label="Exact campaign manifest review">
      <header><div><small>IMMUTABLE CAMPAIGN INTENT</small><h3>{draft.arms.length} strategy arm{draft.arms.length === 1 ? '' : 's'} · {provider?.label || draft.source.provider}</h3></div><code title={draft.campaign_key}>{shortId(draft.campaign_key)}</code></header>
      <dl>
        <div><dt>Universe</dt><dd>{usd(draft.source.minimum_cap)}–{usd(draft.source.maximum_cap)} market cap · {draft.source.limit} results · {duration(draft.source.cadence_seconds)} cadence</dd></div>
        <div><dt>Priority mints</dt><dd>{draft.source.priority_mints.length ? draft.source.priority_mints.join(', ') : 'None; bounded provider universe only'}</dd></div>
        <div><dt>Observation window</dt><dd>{duration(draft.duration_seconds)} planned · {duration(draft.quotas.max_active_runtime_seconds)} active-runtime ceiling</dd></div>
        <div><dt>Hard quotas</dt><dd>{draft.quotas.max_requests.toLocaleString()} requests · {draft.quotas.max_storage_bytes.toLocaleString()} bytes · {draft.quotas.max_concurrency} concurrent</dd></div>
        <div><dt>Modeled execution</dt><dd>{draft.assumptions.cost_model} · {draft.assumptions.execution_model}</dd></div>
        <div><dt>Evidence target</dt><dd>{draft.coverage.minimum_independent_tokens} independent tokens · {draft.coverage.minimum_observations} observations · maximum {duration(draft.coverage.maximum_gap_seconds)} gap</dd></div>
        <div><dt>Provider price</dt><dd>{draft.provider_cost.price_basis}{draft.provider_cost.marginal_cost_per_request === null ? ' · no verified marginal price' : ` · ${usd(draft.provider_cost.marginal_cost_per_request)} / request`}</dd></div>
        <div><dt>Version</dt><dd>{draft.supersedes_campaign_id ? `New version of ${shortId(draft.supersedes_campaign_id)}` : 'New campaign; no predecessor'}</dd></div>
      </dl>
      <div className="campaign-review-arms">{draft.arms.map((arm, index) => {
        const strategy = capabilities.strategies.find((row) => row.source_session_id === arm.source_session_id)
        return <article key={arm.id}><small>ARM {index + 1} · {arm.id}</small><b>{strategy?.label || shortId(arm.strategy_sha256)}</b><span>{usd(arm.simulated_capital_usd)} simulated · {shortId(arm.strategy_sha256)}</span></article>
      })}</div>
    </section>
    <p className="campaign-confirm-copy">Start is idempotent for this exact campaign key. If the response is lost, use <b>Retry exact Start</b> without editing. Any edit creates a deliberate new campaign identity.</p>
    {error && <p className="research-inline-error" role="alert">{error}</p>}
    <div className="campaign-setup-actions"><button disabled={busy} onClick={() => setReviewing(false)}>Back to settings</button>{cancel && <button disabled={busy} onClick={cancel}>Cancel</button>}<button className="primary" disabled={busy || !valid} onClick={() => {
      setAttempted(true)
      void start(draft)
    }}>{busy ? 'Starting…' : attempted ? 'Retry exact Start' : 'START FORWARD CAMPAIGN'}</button></div>
  </div>

  return <div className="campaign-setup">
    <section className="campaign-safety"><span aria-hidden="true">◎</span><div><small>BOUNDED FORWARD EVIDENCE</small><h3>Configure a fair observation campaign</h3><p>One shared source window feeds every selected arm plus cash and holding benchmarks. No wallet funds are reserved.</p></div></section>
    <section className="campaign-setup-section">
      <header><small>1 · SAVED STRATEGIES</small><h3>Select up to {limits.maximum_arms} supported immutable packages</h3><p>Each arm has separate simulated USD. Unsupported packages remain visible but cannot start.</p></header>
      <div className="campaign-strategy-grid">{capabilities.strategies.map((strategy) => {
        const selected = selectedSessions.has(strategy.source_session_id)
        const atLimit = draft.arms.length >= limits.maximum_arms && !selected
        return <label className={`${selected ? 'selected' : ''} ${!strategy.supported ? 'unsupported' : ''}`} key={`${strategy.source_session_id}:${strategy.strategy_package_sha256}`}>
          <input type="checkbox" checked={selected} disabled={!strategy.supported || atLimit} onChange={() => toggleStrategy(strategy)} />
          <span><b>{strategy.label}</b><small>{strategy.family} · {shortId(strategy.strategy_package_sha256)}</small>{strategy.reason && <em>{strategy.reason}</em>}</span>
        </label>
      })}</div>
      {draft.arms.map((arm) => <label className="campaign-capital" key={arm.id}>{capabilities.strategies.find((row) => row.source_session_id === arm.source_session_id)?.label || shortId(arm.strategy_sha256)} simulated capital (USD)<input type="number" min="1" value={arm.simulated_capital_usd} onChange={(event) => change((current) => ({ ...current, arms: current.arms.map((row) => row.id === arm.id ? { ...row, simulated_capital_usd: boundedNumber(event.target.value, 0) } : row) }))} /></label>)}
    </section>
    <section className="campaign-setup-section">
      <header><small>2 · SOURCE AND WINDOW</small><h3>Declare one provider universe</h3><p>Source, cadence and time limits are frozen into the manifest. Navigation never widens them.</p></header>
      <div className="campaign-form-grid">
        <label>Provider<select value={draft.source.provider} onChange={(event) => change((current) => { const choice = capabilities.providers.find((row) => row.id === event.target.value); return { ...current, source: { ...current.source, provider: event.target.value, minimum_cap: choice?.minimum_cap ?? current.source.minimum_cap, maximum_cap: choice?.maximum_cap ?? current.source.maximum_cap } } })}>{capabilities.providers.map((choice) => <option key={choice.id} value={choice.id}>{choice.label} · cost {choice.price_basis}</option>)}</select></label>
        <label>Minimum market cap (USD)<input type="number" min="0" value={draft.source.minimum_cap} onChange={(event) => change((current) => ({ ...current, source: { ...current.source, minimum_cap: boundedNumber(event.target.value, 0) } }))} /></label>
        <label>Maximum market cap (USD)<input type="number" min="1" value={draft.source.maximum_cap} onChange={(event) => change((current) => ({ ...current, source: { ...current.source, maximum_cap: boundedNumber(event.target.value, 0) } }))} /></label>
        <label>Result limit<input type="number" min="1" max="100" value={draft.source.limit} onChange={(event) => change((current) => ({ ...current, source: { ...current.source, limit: boundedNumber(event.target.value, 0) } }))} /></label>
        <label>Cadence (seconds)<input type="number" min={limits.minimum_cadence_seconds} value={draft.source.cadence_seconds} onChange={(event) => change((current) => ({ ...current, source: { ...current.source, cadence_seconds: boundedNumber(event.target.value, 0) } }))} /></label>
        <label>Planned duration (hours)<input type="number" min="0.1" step="0.1" max={limits.maximum_planned_duration_seconds / 3_600} value={draft.duration_seconds / 3_600} onChange={(event) => change((current) => ({ ...current, duration_seconds: boundedNumber(event.target.value, 0) * 3_600 }))} /></label>
        <label>Priority mints (comma separated)<input value={draft.source.priority_mints.join(', ')} placeholder={`Optional; maximum ${limits.maximum_priority_mints}`} onChange={(event) => change((current) => ({ ...current, source: { ...current.source, priority_mints: event.target.value.split(',').map((value) => value.trim()).filter(Boolean).slice(0, limits.maximum_priority_mints) } }))} /></label>
      </div>
    </section>
    <section className="campaign-setup-section">
      <header><small>3 · MODELED ASSUMPTIONS AND HARD CEILINGS</small><h3>Make costs and resource limits explicit</h3><p>Unknown provider price remains unknown. A reached ceiling caps the campaign; it is not automatically increased.</p></header>
      <div className="campaign-form-grid">
        <label>Cost model<input value={draft.assumptions.cost_model} onChange={(event) => change((current) => ({ ...current, assumptions: { ...current.assumptions, cost_model: event.target.value } }))} /></label>
        <label>Execution model<input value={draft.assumptions.execution_model} onChange={(event) => change((current) => ({ ...current, assumptions: { ...current.assumptions, execution_model: event.target.value } }))} /></label>
        <label>Provider price basis<select value={draft.provider_cost.price_basis} onChange={(event) => change((current) => ({ ...current, provider_cost: { ...current.provider_cost, price_basis: event.target.value as CampaignDraft['provider_cost']['price_basis'], marginal_cost_per_request: event.target.value === 'unknown' ? null : current.provider_cost.marginal_cost_per_request, fixed_subscription_cost: event.target.value === 'unknown' ? null : current.provider_cost.fixed_subscription_cost } }))}><option value="unknown">Unknown</option><option value="mixed">Mixed / partial</option><option value="known">Known</option></select></label>
        <label>Marginal provider cost (USD)<input type="number" min="0" step="0.000001" disabled={draft.provider_cost.price_basis === 'unknown'} value={draft.provider_cost.marginal_cost_per_request ?? ''} onChange={(event) => change((current) => ({ ...current, provider_cost: { ...current.provider_cost, marginal_cost_per_request: boundedNumber(event.target.value, 0) } }))} /></label>
        <label>Fixed provider subscription (USD)<input type="number" min="0" step="0.01" disabled={draft.provider_cost.price_basis === 'unknown'} value={draft.provider_cost.fixed_subscription_cost ?? ''} onChange={(event) => change((current) => ({ ...current, provider_cost: { ...current.provider_cost, fixed_subscription_cost: boundedNumber(event.target.value, 0) } }))} /></label>
        <label>Request ceiling<input type="number" min="1" max={limits.maximum_requests} value={draft.quotas.max_requests} onChange={(event) => change((current) => ({ ...current, quotas: { ...current.quotas, max_requests: boundedNumber(event.target.value, 0) } }))} /></label>
        <label>Active-runtime ceiling (hours)<input type="number" min="0.1" step="0.1" value={draft.quotas.max_active_runtime_seconds / 3_600} onChange={(event) => change((current) => ({ ...current, quotas: { ...current.quotas, max_active_runtime_seconds: boundedNumber(event.target.value, 0) * 3_600 } }))} /></label>
        <label>Storage ceiling (bytes)<input type="number" min="1024" max={limits.maximum_storage_bytes} value={draft.quotas.max_storage_bytes} onChange={(event) => change((current) => ({ ...current, quotas: { ...current.quotas, max_storage_bytes: boundedNumber(event.target.value, 0) } }))} /></label>
        <label>Campaign concurrency<input type="number" min="1" max={limits.maximum_concurrency} value={draft.quotas.max_concurrency} onChange={(event) => change((current) => ({ ...current, quotas: { ...current.quotas, max_concurrency: boundedNumber(event.target.value, 0) } }))} /></label>
        <label>Maximum source age (seconds)<input type="number" min="0" value={draft.coverage.max_source_age_seconds} onChange={(event) => change((current) => ({ ...current, coverage: { ...current.coverage, max_source_age_seconds: boundedNumber(event.target.value, 0) } }))} /></label>
        <label>Independent-token target<input type="number" min="1" value={draft.coverage.minimum_independent_tokens} onChange={(event) => change((current) => ({ ...current, coverage: { ...current.coverage, minimum_independent_tokens: boundedNumber(event.target.value, 0) } }))} /></label>
        <label>Minimum observations<input type="number" min="1" value={draft.coverage.minimum_observations} onChange={(event) => change((current) => ({ ...current, coverage: { ...current.coverage, minimum_observations: boundedNumber(event.target.value, 0) } }))} /></label>
        <label>Maximum evidence gap (seconds)<input type="number" min="1" value={draft.coverage.maximum_gap_seconds} onChange={(event) => change((current) => ({ ...current, coverage: { ...current.coverage, maximum_gap_seconds: boundedNumber(event.target.value, 0) } }))} /></label>
      </div>
    </section>
    {!valid && <p className="research-inline-error" role="alert">Select at least one supported arm and keep every duration, cadence and hard ceiling within the shown service limits.</p>}
    <div className="campaign-setup-actions">{cancel && <button disabled={busy} onClick={cancel}>Cancel</button>}<button className="primary" disabled={busy || !valid} onClick={() => setReviewing(true)}>Review exact campaign</button></div>
  </div>
}

function UsageMeter({ label, used, limit, detail }: { label: string; used: number; limit: number; detail: string }) {
  const ratio = limit > 0 ? Math.max(0, Math.min(1, used / limit)) : 0
  return <article className={ratio >= 1 ? 'exhausted' : ratio >= 0.8 ? 'near' : ''}><div><small>{label}</small><b>{used.toLocaleString()} / {limit.toLocaleString()}</b></div><div className="campaign-meter" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={limit} aria-valuenow={Math.min(used, limit)}><i style={{ width: `${ratio * 100}%` }} /></div><span>{detail}</span></article>
}

function CampaignOperation({ campaign, busy, command, newVersion }: {
  campaign: ForwardCampaign
  busy: boolean
  command: (command: 'pause' | 'resume' | 'stop') => void
  newVersion: () => void
}) {
  const [stopReview, setStopReview] = useState(false)
  const [stopConfirmation, setStopConfirmation] = useState('')
  const terminal = campaign.state === 'stopped' || campaign.state === 'completed'
  const authorityDisabled = Object.values(campaign.authority).every((allowed) => allowed === false)
  return <>
    <section className={`campaign-status ${campaign.state}`}>
      <div><small>CAMPAIGN {campaign.state.toUpperCase()}</small><h3>{campaign.state_reason}</h3><p>Created {dateTime(campaign.created_at)} · actual operation {duration(campaign.actual_operating_seconds)} of {duration(campaign.planned_duration_seconds)} planned</p></div>
      <dl><div><dt>Campaign</dt><dd title={campaign.id}>{shortId(campaign.id)}</dd></div><div><dt>Manifest</dt><dd title={campaign.manifest_digest}>{shortId(campaign.manifest_digest)}</dd></div><div><dt>Service</dt><dd className={`status-${campaign.service.status}`}>{campaign.service.status}</dd></div><div><dt>Authority</dt><dd>{authorityDisabled ? 'Observation only' : 'Unexpected authority'}</dd></div></dl>
    </section>
    {campaign.service.restart_resume_required && <section className="campaign-warning" role="alert"><div><small>EXPLICIT RESUME REQUIRED</small><h3>A service restart did not silently resume collection</h3><p>{campaign.service.repair || 'Inspect service health and resume this same campaign only when its source is ready.'}</p></div></section>}
    {!authorityDisabled && <p className="research-inline-error" role="alert">The campaign returned unexpected wallet or execution authority. Do not operate it.</p>}
    <section className="campaign-usage" aria-label="Campaign provider and resource usage">
      <UsageMeter label="PROVIDER REQUESTS" used={campaign.provider.request_count} limit={campaign.provider.request_ceiling} detail={`${campaign.provider.shared_request_count} shared calls · ${campaign.provider.attributed_request_units} attributed units`} />
      <UsageMeter label="ATTRIBUTABLE STORAGE" used={campaign.provider.response_bytes} limit={campaign.provider.storage_ceiling_bytes} detail="Retained bytes; reaching the ceiling caps collection" />
      <article className={campaign.provider.cost_status === 'unknown' ? 'unknown' : ''}><div><small>PROVIDER COST</small><b>{campaign.provider.cost_status}</b></div><span>{campaign.provider.cost_status === 'unknown' ? 'Not priced as free; fully-costed profitability is unavailable' : 'See the performance report for known and estimated cost attribution'}</span></article>
      <article className={campaign.provider.status === 'healthy' ? '' : 'unknown'}><div><small>SOURCE QUALITY</small><b>{campaign.provider.status}</b></div><span>{campaign.provider.source_age_seconds === null ? 'Unknown source age' : `${duration(campaign.provider.source_age_seconds)} source age`} · {campaign.provider.latency_ms === null ? 'unknown latency' : `${Math.round(campaign.provider.latency_ms)}ms latency`} · {campaign.provider.error_count} errors</span></article>
    </section>
    <div className="campaign-arm-table-wrap"><table className="campaign-arm-table"><caption>Every arm receives the same attributed source window; decisions retain their actual decision time</caption><thead><tr><th scope="col">Arm / immutable package</th><th scope="col">State</th><th scope="col">Paper capital</th><th scope="col">Evidence</th><th scope="col">Last decision / lag</th><th scope="col">Repair</th></tr></thead><tbody>{campaign.arms.map((arm) => <tr key={arm.id}><th scope="row"><b>{arm.label}</b><small>{shortId(arm.strategy_package_sha256)} · run {shortId(arm.paper_run_id)}</small></th><td><span className={`campaign-state-pill ${arm.state}`}>{arm.state}</span></td><td>{usd(arm.simulated_capital_usd)}</td><td>{arm.observations} observations<small>{arm.decisions} decisions · {arm.opportunities} opportunities</small></td><td>{dateTime(arm.last_decided_at)}<small>{arm.lag_seconds === null ? 'Lag unknown' : `${duration(arm.lag_seconds)} lag`}</small></td><td>{arm.repair || 'No repair currently required'}</td></tr>)}</tbody></table></div>
    {(campaign.blockers.length > 0 || campaign.gaps.length > 0) && <section className="campaign-evidence-limits"><div><small>DURABLE BLOCKERS AND GAPS</small><h3>Missed operating time is not replayed as live evidence</h3></div>{campaign.blockers.map((blocker) => <p key={`${blocker.code}:${blocker.source_id}`}><b>{blocker.code}</b> — {blocker.message} <span>{blocker.repair}</span></p>)}{campaign.gaps.map((gap) => <p key={gap.id}><b>{gap.reason}</b> · {dateTime(gap.started_at)} → {dateTime(gap.ended_at)} — {gap.detail}</p>)}</section>}
    <section className="campaign-controls">
      <div><small>EXPLICIT CAMPAIGN CONTROL</small><h3>Commands affect this campaign, not individual browser tabs</h3><p>Refresh, report views and export are read-only. Capped limits require a deliberate new version; they are never widened by Resume.</p></div>
      <div>{(campaign.state === 'running' || campaign.state === 'degraded') && <button disabled={busy} onClick={() => command('pause')}>Pause campaign</button>}{campaign.state === 'paused' && <button className="primary" disabled={busy || campaign.service.status === 'disconnected'} onClick={() => command('resume')}>Resume same campaign</button>}{campaign.state === 'capped' && <button className="primary" disabled={busy} onClick={newVersion}>Configure new bounded version</button>}{!terminal && !stopReview && <button className="danger" disabled={busy} onClick={() => setStopReview(true)}>Review stop</button>}{stopReview && <div className="campaign-stop-confirm"><label>Type campaign ID to stop<input value={stopConfirmation} onChange={(event) => setStopConfirmation(event.target.value)} placeholder={campaign.id} /></label><button disabled={busy} onClick={() => { setStopReview(false); setStopConfirmation('') }}>Cancel</button><button className="danger" disabled={busy || stopConfirmation !== campaign.id} onClick={() => command('stop')}>Stop {shortId(campaign.id)}</button></div>}</div>
    </section>
    <EvidenceAssistantPanel selector={{ schema: 'pumpwizard.evidence-selector/v1', kind: 'campaign', id: campaign.id, parent_id: null }} questions={CAMPAIGN_EVIDENCE_QUESTIONS} title="Explain this campaign state" />
  </>
}

function PerformanceCurve({ comparisons }: { comparisons: PerformanceComparison[] }) {
  const all = comparisons.flatMap((row) => row.curve.filter((point) => point.equity_usd !== null))
  if (!all.length) return <div className="research-empty"><b>No priced curve points</b><p>Unknown marks and gaps are not interpolated.</p></div>
  const times = all.map((point) => point.at)
  const values = all.map((point) => point.equity_usd as number)
  const minTime = Math.min(...times); const maxTime = Math.max(...times)
  const low = Math.min(...values); const high = Math.max(...values)
  const x = (at: number) => maxTime === minTime ? 400 : 28 + (at - minTime) / (maxTime - minTime) * 744
  const y = (value: number) => high === low ? 100 : 18 + (high - value) / (high - low) * 150
  const colors = ['#70e4bc', '#8a72ff', '#ffbd67', '#70b8ff', '#ee7ca5']
  return <div className="campaign-performance-chart"><div>{comparisons.map((row, index) => <span key={row.id}><i style={{ background: colors[index % colors.length] }} />{row.label}{!row.comparable && ' · unmatched'}</span>)}</div><svg viewBox="0 0 800 190" role="img" aria-label="Matched campaign equity curves split at declared gaps">{[18, 93, 168].map((line) => <line key={line} x1="28" x2="772" y1={line} y2={line} />)}{comparisons.map((row, rowIndex) => {
    const segments: { at: number; equity_usd: number }[][] = []; let segment: { at: number; equity_usd: number }[] = []; let segmentId: string | null = null
    row.curve.forEach((point) => {
      if (point.equity_usd === null || point.gap_reason || (segment.length > 0 && segmentId !== point.segment_id)) { if (segment.length) segments.push(segment); segment = [] }
      if (point.equity_usd !== null && !point.gap_reason) { segment.push({ at: point.at, equity_usd: point.equity_usd }); segmentId = point.segment_id }
    }); if (segment.length) segments.push(segment)
    return segments.map((part, index) => <polyline key={`${row.id}:${index}`} style={{ stroke: colors[rowIndex % colors.length] }} points={part.map((point) => `${x(point.at)},${y(point.equity_usd)}`).join(' ')} />)
  })}</svg><p>Backend-declared gaps and segment changes break the lines. Missing marks are not filled.</p></div>
}

function CampaignPerformance({ report, busy, createReport }: {
  report: ForwardPerformanceReport | null
  busy: boolean
  createReport: () => void
}) {
  if (!report) return <div className="research-empty"><b>No immutable campaign report yet</b><p>Viewing this page does not evaluate or create a report. Save an explicit snapshot of the evidence retained so far.</p><button className="primary" disabled={busy} onClick={createReport}>{busy ? 'Saving snapshot…' : 'Save report snapshot'}</button></div>
  return <>
    <section className={`campaign-readiness ${report.readiness.state}`}><div><small>EVIDENCE {report.readiness.state.replace('_', ' ').toUpperCase()}</small><h3>{report.ranking.label}</h3><p>{report.ranking.reason}</p></div><dl><div><dt>Actual period</dt><dd>{dateTime(report.actual_period.first_at)} → {dateTime(report.actual_period.last_at)}</dd></div><div><dt>Operating duration</dt><dd>{duration(report.actual_period.operating_seconds)}</dd></div><div><dt>Cost coverage</dt><dd>{report.ranking.fully_costed ? 'Fully costed from retained inputs' : 'Not fully costed'}</dd></div><div><dt>Report</dt><dd title={report.id}>{shortId(report.id)}</dd></div></dl></section>
    {report.readiness.reasons.length > 0 && <section className="research-blockers"><small>READINESS REASONS</small><ul>{report.readiness.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></section>}
    <div className="campaign-performance-table-wrap"><table className="campaign-performance-table"><caption>Matched backend results; incomparable coverage and unknown costs prevent ranking</caption><thead><tr><th scope="col">Benchmark / arm</th><th scope="col">Ending / net P&amp;L</th><th scope="col">Realized / unrealized</th><th scope="col">Exposure / drawdown</th><th scope="col">Coverage</th><th scope="col">Costs</th><th scope="col">Positions</th></tr></thead><tbody>{report.comparisons.map((row) => <tr key={row.id} className={row.comparable ? '' : 'unmatched'}><th scope="row"><b>{row.label}</b><small>{row.kind}{row.arm_id ? ` · ${shortId(row.arm_id)}` : ''}</small>{!row.comparable && <em>{row.comparability_reason || 'Coverage is not matched'}</em>}</th><td>{usd(row.ending_capital_usd)}<small>{usd(row.modeled_net_pnl_usd, true)} modeled net</small></td><td>{usd(row.modeled_realized_pnl_usd, true)}<small>{usd(row.modeled_unrealized_pnl_usd, true)}</small></td><td>{percentage(row.exposure_fraction)}<small>{percentage(row.maximum_drawdown_fraction)} drawdown · {usd(row.turnover_usd)} turnover</small></td><td>{percentage(row.coverage_fraction)}<small>{row.independent_tokens} tokens · {duration(row.operating_seconds)} · {row.opportunities} opportunities</small></td><td>{usd(row.known_costs_usd)} known<small>{row.cost_status === 'unknown' ? 'Unknown provider/other costs remain' : `${usd(row.estimated_costs_usd)} estimated · ${row.cost_status}`}</small></td><td>{row.unpriced_positions} unpriced<small>{row.censored_positions} censored</small></td></tr>)}</tbody></table></div>
    <PerformanceCurve comparisons={report.comparisons} />
    <section className="campaign-daily"><header><small>DAILY AND WHOLE-RUN COVERAGE</small><h3>Source-linked summary windows</h3></header>{report.daily.length ? <div>{report.daily.map((day) => <article key={day.id}><b>{day.label}</b><span>{dateTime(day.first_at)} → {dateTime(day.last_at)}</span><small>{day.comparison_ids.length} comparisons · {day.gaps} gaps</small></article>)}</div> : <p>No complete daily interval is available; the whole-run period above remains the only summary.</p>}</section>
    {report.gaps.length > 0 && <details className="campaign-drilldown"><summary>Inspect report gaps ({report.gaps.length})</summary>{report.gaps.map((gap) => <p key={gap.id}><b>{gap.reason}</b> · {dateTime(gap.started_at)} → {dateTime(gap.ended_at)} — {gap.detail}</p>)}</details>}
    <section className="campaign-export"><div><small>REPRODUCIBLE CAMPAIGN ARTIFACTS</small><h3>Export this campaign and its immutable report</h3><p>Report {shortId(report.id)} stays bound to manifest {shortId(report.manifest_digest)}, engine versions and retained source lineage.</p></div><button disabled={busy} onClick={createReport}>{busy ? 'Saving…' : 'Save corrected snapshot'}</button><a className="button" href={campaignPaths.export(report.id, 'json')}>Export JSON</a><a className="button" href={campaignPaths.export(report.id, 'csv')}>Export CSV</a></section>
    <EvidenceAssistantPanel selector={{ schema: 'pumpwizard.evidence-selector/v1', kind: 'campaign_report', id: report.id, parent_id: null }} questions={REPORT_EVIDENCE_QUESTIONS} title="Explain this immutable report" />
    <details className="campaign-operator-guide"><summary>Operator guide: interpreting and extending this observation</summary><div><p><b>Actually observed:</b> {duration(report.actual_period.operating_seconds)} between {dateTime(report.actual_period.first_at)} and {dateTime(report.actual_period.last_at)}, excluding the visible gaps.</p><p><b>Modeled:</b> {report.model_label}. Paper fills, retrospective cases and recorded receipts are separate evidence classes.</p><p><b>Still unknown:</b> {report.limitations.length ? report.limitations.join(' · ') : 'No additional limitations were supplied; readiness reasons and cost coverage still apply.'}</p><p><b>Longer local observation:</b> keep the local service healthy, verify provider access and pricing, choose immutable supported arms, preregister a duration and hard ceilings, then explicitly start a new campaign. A short fixture is not a completed multi-day study.</p><p><b>Engines:</b> {report.engine_versions.join(' · ') || 'Not reported'}</p></div></details>
  </>
}

function CampaignReceipts({ report }: { report: ForwardPerformanceReport | null }) {
  const [selectedReceipt, setSelectedReceipt] = useState('')
  if (!report) return <div className="research-empty"><b>No receipt reconciliation snapshot</b><p>Recorded execution evidence is read-only and is never inferred from paper fills.</p></div>
  const groups = [
    ['modeled_paper', 'Modeled paper'],
    ['retrospective', 'Retrospective'],
    ['recorded_execution', 'Recorded execution'],
  ] as const
  return <>
    <section className={`campaign-reconciliation ${report.reconciliation.status}`}><div><small>RECEIPT RECONCILIATION</small><h3>{report.reconciliation.status}</h3><p>{report.reconciliation.unresolved_count} unresolved discrepancies · external cash flow {report.reconciliation.external_cash_flow || 'unknown'}</p></div><p>Deposits, withdrawals and transferred inventory are external flows—not strategy P/L. Portfolio worth movement is not used as a substitute.</p></section>
    {report.reconciliation.reasons.length > 0 && <section className="research-blockers"><ul>{report.reconciliation.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></section>}
    <div className="campaign-receipt-groups">{groups.map(([kind, label]) => {
      const rows = report.receipts.filter((receipt) => receipt.evidence_class === kind)
      return <section key={kind}><header><small>{label.toUpperCase()}</small><h3>{rows.length} retained record{rows.length === 1 ? '' : 's'}</h3><p>{kind === 'recorded_execution' ? 'Network receipt evidence; never relabeled as paper.' : kind === 'modeled_paper' ? 'Observed paper model; no on-chain execution claim.' : 'Historical scenario output; not a live or paper fill.'}</p></header>{rows.length ? <div><table><thead><tr><th scope="col">Record / state</th><th scope="col">Ownership</th><th scope="col">Cash / token delta</th><th scope="col">Fee</th><th scope="col">Discrepancy / source</th></tr></thead><tbody>{rows.map((row) => <tr key={row.id}><th scope="row"><b>{row.record_kind}</b><small>{row.classification} · {dateTime(row.occurred_at)}</small></th><td>{row.ownership}</td><td>{row.cash_delta || 'Unknown'}<small>{row.token_delta || 'Unknown'}</small></td><td>{row.fee || 'Unknown'}</td><td>{row.discrepancy || 'No retained discrepancy'}<small><a href={row.source_href}>Inspect source</a></small><button aria-pressed={selectedReceipt === row.id} onClick={() => setSelectedReceipt((current) => current === row.id ? '' : row.id)}>{selectedReceipt === row.id ? 'Hide explanation' : 'Explain receipt'}</button></td></tr>)}</tbody></table></div> : <p>No {label.toLowerCase()} records are linked to this report.</p>}</section>
    })}</div>
    {selectedReceipt && <EvidenceAssistantPanel selector={{ schema: 'pumpwizard.evidence-selector/v1', kind: 'receipt', id: selectedReceipt, parent_id: report.id }} questions={RECEIPT_EVIDENCE_QUESTIONS} title="Explain this retained receipt" />}
  </>
}

function AlertInbox({ campaignId, alerts, busy, acknowledge, inspect }: { campaignId: string; alerts: EvidenceAlert[]; busy: boolean; acknowledge: (alert: EvidenceAlert) => void; inspect: (alert: EvidenceAlert) => void }) {
  const [filter, setFilter] = useState<'open' | 'all'>('open')
  const campaignAlerts = alerts.filter((alert) => alert.campaign_id === campaignId && (filter === 'all' || alert.state === 'open'))
  return <>
    <section className="campaign-alert-safety"><div><small>LOCAL EVIDENCE INBOX</small><h3>No external delivery or buy/sell signals</h3><p>Acknowledgment only records review. The evaluator resolves an alert after the underlying condition clears; recurrence opens a new generation.</p></div><label>Show<select value={filter} onChange={(event) => setFilter(event.target.value as 'open' | 'all')}><option value="open">Open conditions</option><option value="all">Open and resolved</option></select></label></section>
    <div className="campaign-alert-list">{campaignAlerts.length ? campaignAlerts.map((alert) => <article className={`${alert.severity} ${alert.state}`} key={alert.id}>
      <header><div><span>{alert.severity}</span><small>{alert.state} · occurrence generation {alert.occurrence_generation}</small><h3>{alert.title}</h3></div><a href={alert.source.href} onClick={(event) => { event.preventDefault(); inspect(alert) }}>Inspect {alert.source.kind}</a></header>
      <p>{alert.message}</p><p className="repair"><b>Repair:</b> {alert.repair}</p>
      <dl><div><dt>First</dt><dd>{dateTime(alert.first_occurred_at)}</dd></div><div><dt>Last</dt><dd>{dateTime(alert.last_occurred_at)}</dd></div><div><dt>Occurrences</dt><dd>{alert.occurrence_count} · {alert.recurrence_count} recurrence{alert.recurrence_count === 1 ? '' : 's'}</dd></div><div><dt>Acknowledged</dt><dd>{dateTime(alert.acknowledged_at)}</dd></div>{alert.resolved_at !== null && <div><dt>Resolved</dt><dd>{dateTime(alert.resolved_at)}</dd></div>}</dl>
      {alert.state === 'open' && alert.acknowledged_at === null && <button disabled={busy} onClick={() => acknowledge(alert)}>Acknowledge review</button>}
      {alert.state === 'open' && alert.acknowledged_at !== null && <p className="campaign-alert-ack">Acknowledged; condition remains active until the linked evidence recovers.</p>}
    </article>) : <div className="research-empty"><b>No {filter === 'open' ? 'open ' : ''}local evidence alerts</b><p>Unknown telemetry is still shown in campaign quality; reads do not evaluate new evidence.</p></div>}</div>
  </>
}

export function CampaignWorkspace({ sourceSessionId, active }: { sourceSessionId: string; active: boolean }) {
  const workspace = useCampaignWorkspace(sourceSessionId, active)
  const [selectedCampaignId, setSelectedCampaignId] = useState('')
  const [section, setSection] = useState<CampaignSection>('operation')
  const [settingUp, setSettingUp] = useState(false)
  const [supersedes, setSupersedes] = useState<string | null>(null)
  const snapshot = workspace.snapshot
  const campaign = useMemo(() => snapshot?.campaigns.find((row) => row.id === selectedCampaignId) || snapshot?.campaigns[0] || null, [selectedCampaignId, snapshot])
  const report = useMemo(() => {
    if (!campaign || !snapshot) return null
    return snapshot.reports.find((row) => row.id === campaign.latest_report_id)
      || snapshot.reports.find((row) => row.campaign_id === campaign.id)
      || null
  }, [campaign, snapshot])

  useEffect(() => { if (campaign && !selectedCampaignId) setSelectedCampaignId(campaign.id) }, [campaign, selectedCampaignId])
  useEffect(() => { setSelectedCampaignId(''); setSettingUp(false); setSupersedes(null); setSection('operation') }, [sourceSessionId])

  if (!active) return null
  if (workspace.loading && !snapshot) return <div className="research-empty campaign-loading"><span className="pulse" /><b>Loading forward campaigns…</b><p>This read cannot start, resume or evaluate a campaign.</p></div>
  if (workspace.available === false) return <div className="research-empty"><b>Forward campaigns are not available in this runner service</b><p>The existing research and paper views remain available. A campaign cannot be created until the local Batch 5 service route is present.</p><button onClick={() => void workspace.refresh()}>Check again</button></div>
  if (!snapshot) return <div className="research-empty"><b>Campaign records unavailable</b><p role="alert">{workspace.error || 'The runner did not return a campaign workspace.'}</p><button onClick={() => void workspace.refresh()}>Retry</button></div>
  if (settingUp || !campaign) return <CampaignSetup capabilities={snapshot.capabilities} sourceSessionId={sourceSessionId} supersedes={supersedes} busy={Boolean(workspace.busy)} error={workspace.error} cancel={campaign ? () => { setSettingUp(false); setSupersedes(null) } : null} start={async (draft) => {
    const created = await workspace.start(draft)
    if (created) { setSelectedCampaignId(created.id); setSettingUp(false); setSupersedes(null); setSection('operation') }
    return created
  }} />

  const campaignAlerts = snapshot.alerts.filter((alert) => alert.campaign_id === campaign.id && alert.state === 'open')
  return <div className="campaign-workspace">
    <section className="campaign-toolbar"><label>Campaign<select value={campaign.id} onChange={(event) => { setSelectedCampaignId(event.target.value); setSection('operation') }}>{snapshot.campaigns.map((row) => <option key={row.id} value={row.id}>{row.state} · {shortId(row.id)} · {new Date(row.created_at * 1_000).toLocaleDateString()}</option>)}</select></label><div><span>{snapshot.campaigns.length} campaign{snapshot.campaigns.length === 1 ? '' : 's'} · {campaignAlerts.length} active alert{campaignAlerts.length === 1 ? '' : 's'}</span><button onClick={() => { setSupersedes(null); setSettingUp(true) }}>New campaign</button></div></section>
    <nav className="campaign-sections" aria-label="Campaign sections">{(['operation', 'performance', 'receipts', 'alerts'] as CampaignSection[]).map((item) => <button key={item} aria-current={section === item ? 'page' : undefined} onClick={() => setSection(item)}>{item}{item === 'alerts' && campaignAlerts.length > 0 ? ` (${campaignAlerts.length})` : ''}</button>)}</nav>
    {section === 'operation' && <CampaignOperation campaign={campaign} busy={Boolean(workspace.busy)} command={(command) => { void workspace.command(campaign, command) }} newVersion={() => { setSupersedes(campaign.id); setSettingUp(true) }} />}
    {section === 'performance' && <CampaignPerformance report={report} busy={Boolean(workspace.busy)} createReport={() => { void workspace.report(campaign, report?.id || null) }} />}
    {section === 'receipts' && <CampaignReceipts report={report} />}
    {section === 'alerts' && <AlertInbox campaignId={campaign.id} alerts={snapshot.alerts} busy={Boolean(workspace.busy)} acknowledge={(alert) => { void workspace.acknowledge(alert) }} inspect={(alert) => setSection(alert.kind === 'accounting-mismatch' || alert.source.kind === 'accounting' ? 'receipts' : 'operation')} />}
    {workspace.error && <p className="research-inline-error" role="alert">{workspace.error}</p>}
    {workspace.operationWarning && <p className="session-review-warning" role="status">{workspace.operationWarning}</p>}
  </div>
}
