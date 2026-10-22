import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import type {
  PaperRun,
  PaperHistory,
  ResearchAttempt,
  ResearchComparison,
  ResearchDataset,
  ResearchEquityPoint,
  ResearchExperiment,
  ResearchJob,
  ResearchReport,
  ResearchSettings,
} from './researchWorkspace'
import { loadPaperHistory, researchPaths, useResearchWorkspace } from './researchWorkspace'
import { CampaignWorkspace } from './CampaignWorkspace'
import { EvidenceAssistantPanel } from './EvidenceAssistantPanel'

type ResearchTab = 'evidence' | 'results' | 'history' | 'paper' | 'campaigns'
const RESEARCH_REPORT_QUESTIONS = ['benchmark_result', 'cost_impact', 'missing_evidence'] as const

function dateTime(value: number | null) {
  return value === null ? 'Unknown' : new Date(value * 1_000).toLocaleString()
}

function period(first: number | null, last: number | null) {
  if (first === null || last === null) return 'No complete time range'
  return `${dateTime(first)} — ${dateTime(last)}`
}

function age(seconds: number | null) {
  if (seconds === null) return 'Unknown source age'
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3_600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86_400) return `${Math.round(seconds / 3_600)}h`
  return `${Math.round(seconds / 86_400)}d`
}

function shortId(value: string | null) {
  if (!value) return 'Not available'
  return value.length < 22 ? value : `${value.slice(0, 10)}…${value.slice(-8)}`
}

function amount(value: number | null, unit: string, signed = false) {
  if (value === null || !Number.isFinite(value)) return 'Not available'
  const prefix = value < 0 ? '-' : signed && value > 0 ? '+' : ''
  const magnitude = Math.abs(value)
  if (unit === 'USD') return `${prefix}$${magnitude.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  return `${prefix}${magnitude.toLocaleString(undefined, { maximumFractionDigits: 6 })} ${unit}`
}

function percentage(value: number | null) {
  if (value === null || !Number.isFinite(value)) return 'Not available'
  return `${(value * 100).toFixed(1)}%`
}

function DatasetSummary({ dataset }: { dataset: ResearchDataset }) {
  const quality = Object.entries(dataset.quality).filter(([, count]) => count > 0)
  return <>
    <section className="research-evidence-grid" aria-label="Frozen evidence identity and coverage">
      <article><small>DATASET PERIOD</small><b>{period(dataset.event_range.first_at, dataset.event_range.last_at)}</b><span>Event/source time retained as recorded</span></article>
      <article><small>FIRST-KNOWN PERIOD</small><b>{period(dataset.first_known_range.first_at, dataset.first_known_range.last_at)}</b><span>{dataset.first_known_range.basis}</span></article>
      <article><small>SOURCE AGE</small><b>{age(dataset.source_age_seconds)}</b><span>Age reported by the evidence store</span></article>
      <article><small>COMPATIBILITY</small><b>{dataset.supported_family ? dataset.family : 'Unsupported strategy family'}</b><span>{dataset.eligible ? 'Eligible for declared comparison policy' : 'Viewable evidence; not comparison-eligible'}</span></article>
    </section>
    <section className="research-quality-panel">
      <div><small>FIRST-KNOWN COVERAGE</small><h3>{dataset.counts.independent_observations.toLocaleString()} independent observations</h3><p>{dataset.counts.unknown_first_known.toLocaleString()} records have unknown first-known time. Unknown time is not inferred from event time.</p></div>
      <dl>
        <div><dt>Observations</dt><dd>{dataset.counts.observations.toLocaleString()}</dd></div>
        <div><dt>Decisions</dt><dd>{dataset.counts.decisions.toLocaleString()}</dd></div>
        <div><dt>Outcomes</dt><dd>{dataset.counts.outcomes.toLocaleString()}</dd></div>
        <div><dt>Collection gaps</dt><dd>{dataset.gaps.length.toLocaleString()}</dd></div>
      </dl>
    </section>
    {(quality.length > 0 || dataset.gaps.length > 0) && <section className="research-issues" aria-label="Evidence gaps and exclusions">
      <header><small>VISIBLE QUALITY LIMITS</small><h3>Sparse and backfilled records stay inspectable</h3><p>They are not silently repaired, repriced or treated as independent evidence.</p></header>
      <div>{quality.map(([label, count]) => <span key={label}><b>{count.toLocaleString()}</b>{label.replace(/_/g, ' ')}</span>)}</div>
      {dataset.gaps.map((gap) => <p key={gap.id}><b>{gap.reason}</b> · {period(gap.started_at, gap.ended_at)} — {gap.detail}</p>)}
    </section>}
    {dataset.blockers.length > 0 && <section className="research-blockers" role="note"><small>COMPARISON BLOCKERS</small><ul>{dataset.blockers.map((item) => <li key={item}>{item}</li>)}</ul></section>}
  </>
}

function JobStatus({ job, disabled, cancel, retry }: {
  job: ResearchJob
  disabled: boolean
  cancel: () => void
  retry: () => void
}) {
  const canCancel = job.state === 'queued' || job.state === 'running'
  const canRetry = job.state === 'failed' || job.state === 'cancelled' || job.state === 'interrupted'
  return <section className={`research-job ${job.state}`} aria-label={`Comparison job ${job.state}`}>
    <header><div><small>BACKGROUND COMPARISON</small><h3>{job.state.replace('-', ' ')}</h3></div><code title={job.id}>{shortId(job.id)}</code></header>
    <div className="research-progress" role="progressbar" aria-label="Comparison progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(job.progress * 100)}><i style={{ width: `${Math.max(0, Math.min(100, job.progress * 100))}%` }} /></div>
    <p>{Math.round(job.progress * 100)}% complete · attempt {shortId(job.attempt_id)} · {job.restart_count} service restart{job.restart_count === 1 ? '' : 's'}</p>
    {job.error && <p className="research-inline-error" role="alert">{job.error}</p>}
    <footer>{canCancel && <button disabled={disabled} onClick={cancel}>{job.state === 'running' ? 'Cancel comparison' : 'Cancel queued job'}</button>}{canRetry && <button className="primary" disabled={disabled} onClick={retry}>Retry as another attempt</button>}</footer>
  </section>
}

function EquityChart({ rows }: { rows: ResearchComparison[] }) {
  const points = rows.flatMap((row) => row.equity.filter((point) => point.equity !== null) as (ResearchEquityPoint & { equity: number })[])
  if (!points.length) return <div className="research-empty"><b>No priced equity points</b><p>Missing or unpriced intervals remain visible in the table below.</p></div>
  const times = points.map((point) => point.at)
  const values = points.map((point) => point.equity)
  const minTime = Math.min(...times)
  const maxTime = Math.max(...times)
  const low = Math.min(...values)
  const high = Math.max(...values)
  const x = (at: number) => maxTime === minTime ? 400 : 28 + (at - minTime) / (maxTime - minTime) * 744
  const y = (value: number) => high === low ? 100 : 18 + (high - value) / (high - low) * 150
  const colors = ['#70e4bc', '#8a72ff', '#ffbd67', '#70b8ff', '#ee7ca5']
  return <div className="research-chart">
    <div className="research-chart-legend">{rows.map((row, index) => <span key={row.id}><i style={{ background: colors[index % colors.length] }} />{row.label}</span>)}</div>
    <svg viewBox="0 0 800 190" role="img" aria-label="Modeled equity chart with missing intervals left disconnected">
      {[18, 93, 168].map((line) => <line className="grid" key={line} x1="28" x2="772" y1={line} y2={line} />)}
      {rows.map((row, rowIndex) => {
        const segments: ResearchEquityPoint[][] = []
        let segment: ResearchEquityPoint[] = []
        row.equity.forEach((point) => {
          if (point.equity === null || point.gap_reason) {
            if (segment.length) segments.push(segment)
            segment = []
          } else segment.push(point)
        })
        if (segment.length) segments.push(segment)
        return segments.map((part, partIndex) => <polyline key={`${row.id}:${partIndex}`} style={{ stroke: colors[rowIndex % colors.length] }} points={part.map((point) => `${x(point.at)},${y(point.equity as number)}`).join(' ')} />)
      })}
    </svg>
    <p>Lines stop at every backend-declared gap or unpriced point; disconnected intervals are not interpolated.</p>
  </div>
}

function Results({ report, confirmDraft, setConfirmDraft, createDraft, disabled }: {
  report: ResearchReport
  confirmDraft: string | null
  setConfirmDraft: (candidate: string | null) => void
  createDraft: (candidate: string) => void
  disabled: boolean
}) {
  const recommendation = ({
    insufficient_evidence: 'Insufficient evidence',
    prefer_cash: 'Prefer cash',
    keep_current: 'Keep current',
    create_inactive_draft: 'Eligible inactive draft',
  } as const)[report.recommendation.action]
  return <>
    <section className={`research-recommendation ${report.recommendation.action}`}>
      <div><small>BACKEND RECOMMENDATION</small><h3>{recommendation}</h3><p>{report.recommendation.reason}</p></div>
      <dl><div><dt>Report</dt><dd title={report.id}>{shortId(report.id)}</dd></div><div><dt>Dataset</dt><dd title={report.dataset_id}>{shortId(report.dataset_id)}</dd></div><div><dt>Period</dt><dd>{period(report.period.first_at, report.period.last_at)}</dd></div><div><dt>Holdout</dt><dd>{report.holdout_state}</dd></div></dl>
    </section>
    {report.recommendation.blockers.length > 0 && <section className="research-blockers"><small>RECOMMENDATION BLOCKERS</small><ul>{report.recommendation.blockers.map((item) => <li key={item}>{item}</li>)}</ul></section>}
    <div className="research-result-table-wrap"><table className="research-result-table"><caption>Backend-modeled comparison using consistent capital, period and cost units</caption><thead><tr><th scope="col">Benchmark / package</th><th scope="col">Starting</th><th scope="col">Ending</th><th scope="col">Net P/L</th><th scope="col">Realized / unrealized</th><th scope="col">Drawdown</th><th scope="col">Coverage</th><th scope="col">Costs</th></tr></thead>
      <tbody>{report.comparisons.map((row) => <tr key={row.id}><th scope="row"><b>{row.label}</b><small>{row.kind} · {shortId(row.package_sha256)}</small>{row.uncertainty && <em>{row.uncertainty}</em>}</th><td>{amount(row.starting_capital, row.capital_unit)}</td><td>{amount(row.ending_capital, row.capital_unit)}</td><td>{amount(row.modeled_net_pnl, row.capital_unit, true)}</td><td>{amount(row.modeled_realized_pnl, row.capital_unit, true)}<small>{amount(row.modeled_unrealized_pnl, row.capital_unit, true)}</small></td><td>{percentage(row.maximum_drawdown_fraction)}</td><td>{percentage(row.coverage_fraction)}<small>{row.independent_observations ?? 'Unknown'} independent</small></td><td>{amount(row.modeled_costs, row.capital_unit)}</td></tr>)}</tbody>
    </table></div>
    <EquityChart rows={report.comparisons} />
    <details className="research-ledger"><summary>Inspect modeled order and position ledger ({report.ledger.length})</summary><div><table><thead><tr><th>State</th><th>Asset / side</th><th>Opened / closed</th><th>Quantity</th><th>Price / value</th><th>Cost / modeled P&amp;L</th><th>Evidence note</th></tr></thead><tbody>{report.ledger.map((row) => <tr key={row.id}><td>{row.state}</td><td>{row.symbol}<small>{row.side}</small></td><td>{dateTime(row.opened_at)}<small>{dateTime(row.closed_at)}</small></td><td>{row.quantity ?? 'Unknown'}</td><td>{row.price ?? 'Unknown'}<small>{row.value ?? 'Unknown'}</small></td><td>{row.cost ?? 'Unknown'}<small>{row.pnl ?? 'Unknown'}</small></td><td>{row.explanation}</td></tr>)}</tbody></table></div></details>
    <section className="research-result-lower"><article><small>COST STRESS</small><h3>Does the result survive changed assumptions?</h3>{report.cost_stress.length ? report.cost_stress.map((row) => <p key={row.id}><b>{row.label}</b> · {amount(row.modeled_net_pnl, report.capital_unit, true)} net · {amount(row.costs, report.capital_unit)} costs {row.changed_recommendation && <em>Reverses recommendation</em>}</p>) : <p>No backend cost-stress cases were provided.</p>}</article><article><small>EXCLUSIONS AND UNKNOWN VALUES</small><h3>Not silently filled</h3>{report.exclusions.map((row) => <p key={row.reason}><b>{row.count}</b> {row.reason} — {row.action}</p>)}{report.limitations.map((item) => <p key={item}>{item}</p>)}</article></section>
    <section className="research-export"><div><small>IMMUTABLE ARTIFACTS</small><h3>Export this exact report</h3><p>Artifact IDs, units and filenames are generated by the backend and remain tied to report <code>{report.id}</code>.</p></div><a className="button" href={researchPaths.export(report.id, 'json')}>Export JSON</a><a className="button" href={researchPaths.export(report.id, 'csv')}>Export CSV</a></section>
    {report.recommendation.action === 'create_inactive_draft' && report.recommendation.candidate_id && <section className="research-draft">
      {confirmDraft === report.recommendation.candidate_id ? <><div><small>EXPLICIT ACCEPTANCE</small><h3>Confirm exact inactive successor</h3><p>Report <code>{report.id}</code> · candidate <code>{confirmDraft}</code>. The successor starts paused with zero wallet allocation, entries disabled and no copied authorization.</p></div><button disabled={disabled} onClick={() => setConfirmDraft(null)}>Cancel</button><button className="primary" disabled={disabled} onClick={() => createDraft(confirmDraft)}>Create exact inactive draft</button></> : <><div><small>OPTIONAL SUCCESSOR</small><h3>Review a separate inactive draft</h3><p>This never edits the current session or grants trading authority.</p></div><button className="primary" disabled={disabled} onClick={() => setConfirmDraft(report.recommendation.candidate_id)}>Review draft action</button></>}
    </section>}
  </>
}

function ExperimentHistory({ experiments, attempts, reports }: {
  experiments: ResearchExperiment[]
  attempts: ResearchAttempt[]
  reports: ResearchReport[]
}) {
  return <div className="research-history-list">{experiments.length ? experiments.map((experiment) => {
    const rows = attempts.filter((attempt) => attempt.experiment_id === experiment.id)
    const report = reports.find((item) => item.experiment_id === experiment.id)
    return <article key={experiment.id}><header><div><small>{experiment.purpose.toUpperCase()} EXPERIMENT</small><h3>{shortId(experiment.id)}</h3></div><span className={`holdout ${experiment.holdout_state}`}>Holdout {experiment.holdout_state}</span></header><dl><div><dt>Dataset</dt><dd>{shortId(experiment.dataset_id)}</dd></div><div><dt>Candidates</dt><dd>{experiment.candidate_count}</dd></div><div><dt>Capital</dt><dd>{amount(experiment.starting_capital, experiment.capital_unit)}</dd></div><div><dt>Costs</dt><dd>{experiment.cost_model.label}</dd></div><div><dt>Replay engine</dt><dd>{experiment.replay_engine_version}</dd></div><div><dt>Code</dt><dd>{experiment.code_version}</dd></div></dl><p>{experiment.evidence_policy}</p>{experiment.changed_from_parent.length > 0 && <p><b>Changed from parent:</b> {experiment.changed_from_parent.join(' · ')}</p>}<section><small>ATTEMPT HISTORY</small>{rows.map((attempt) => <p key={attempt.id}><b>#{attempt.number} {attempt.state}</b> · {dateTime(attempt.started_at)} → {dateTime(attempt.finished_at)} · {attempt.restart_count} restart{attempt.restart_count === 1 ? '' : 's'}{attempt.error && ` — ${attempt.error}`}</p>)}</section>{experiment.holdout_state === 'exposed' && <p className="research-holdout-warning">This holdout was exposed and cannot be reused as unseen evidence in a related run.</p>}{report && <p>Immutable report: <code>{report.id}</code></p>}</article>
  }) : <div className="research-empty"><b>No experiments yet</b><p>Freeze eligible evidence and explicitly start a comparison to create immutable history.</p></div>}</div>
}

function PaperPanel({ runs, strategySha256, busy, start, command }: {
  runs: PaperRun[]
  strategySha256: string
  busy: boolean
  start: (capital: number) => void
  command: (runId: string, action: 'pause' | 'resume' | 'stop') => void
}) {
  const [capital, setCapital] = useState(1_000)
  const [confirming, setConfirming] = useState(false)
  const [history, setHistory] = useState<Record<string, PaperHistory>>({})
  const [historyBusy, setHistoryBusy] = useState('')
  const [historyError, setHistoryError] = useState('')
  const active = runs.find((run) => run.state === 'running' || run.state === 'paused' || run.state === 'interrupted')
  const activeId = active?.id
  const activeState = active?.state
  useEffect(() => {
    if (activeId) setConfirming(false)
  }, [activeId, activeState])
  const inspectHistory = async (runId: string) => {
    setHistoryBusy(runId)
    setHistoryError('')
    try {
      const value = await loadPaperHistory(runId)
      setHistory((current) => ({ ...current, [runId]: value }))
    } catch (failure) {
      setHistoryError(failure instanceof Error ? failure.message : 'Paper evidence history could not be loaded.')
    } finally {
      setHistoryBusy('')
    }
  }
  return <>
    <section className="paper-safety"><span aria-hidden="true">◎</span><div><small>PROSPECTIVE PAPER EVIDENCE</small><h3>No wallet, signer, allowance or order path</h3><p>Paper monitoring observes only evidence first known after its immutable start. New strategy settings or capital create a new run instead of rewriting history.</p></div></section>
    {!active && <section className="paper-start"><div><small>EXPLICIT START</small><h3>Start a new forward paper run</h3><p>Exact strategy <code>{shortId(strategySha256)}</code>. Capital is simulated USD, never reserved wallet funds.</p></div><label>Simulated starting capital (USD)<input type="number" min="1" step="1" value={capital} onChange={(event) => setCapital(Number(event.target.value))} /></label>{confirming ? <><button disabled={busy} onClick={() => setConfirming(false)}>Cancel</button><button className="primary" disabled={busy || capital <= 0} onClick={() => start(capital)}>START PAPER RESEARCH</button></> : <button className="primary" disabled={busy || !strategySha256} onClick={() => setConfirming(true)}>Review paper start</button>}</section>}
    {runs.map((run) => {
      const evidence = history[run.id]
      const latest = evidence?.observations[evidence.observations.length - 1]
      const excluded = evidence?.observations.filter((row) => row.quality?.status === 'excluded') || []
      return <article className={`paper-run ${run.state}`} key={run.id}><header><div><small>{run.state.toUpperCase()} PAPER RUN</small><h3>{shortId(run.id)}</h3></div><span>{dateTime(run.started_at)}</span></header><dl><div><dt>Immutable strategy</dt><dd title={run.strategy_sha256}>{shortId(run.strategy_sha256)}</dd></div><div><dt>Starting capital</dt><dd>{amount(run.starting_capital_usd, 'USD')}</dd></div><div><dt>Current modeled capital</dt><dd>{amount(run.simulated_capital_usd, 'USD')}</dd></div><div><dt>First known</dt><dd>{dateTime(run.first_known_at)}</dd></div><div><dt>Decisions / outcomes</dt><dd>{run.decisions} / {run.scored_outcomes}</dd></div><div><dt>Evidence quality</dt><dd>{run.independent_observations} independent · {run.excluded_observations ?? 0} excluded · {run.unknown_outcomes} unknown</dd></div><div><dt>Worker health</dt><dd>{run.processing_state ?? 'healthy'} · retry {run.retry?.count ?? 0}/{run.retry?.maximum ?? 0}</dd></div></dl>{run.fault && <p className="research-inline-error" role="alert"><b>{run.fault.code}</b> — {run.fault.message} {run.fault.repair}</p>}{run.assumptions.map((item) => <p key={item}>{item}</p>)}{run.gaps.map((gap) => <p className="paper-gap" key={gap.id}><b>{gap.reason}</b> · {period(gap.started_at, gap.ended_at)}</p>)}
        {!evidence ? <button className="paper-history-load" disabled={historyBusy === run.id} onClick={() => void inspectHistory(run.id)}>{historyBusy === run.id ? 'Loading evidence…' : 'Inspect durable decisions and outcomes'}</button> : <details className="paper-history" open><summary>Durable decision and outcome history ({evidence.decisions.length})</summary>
          {latest && <p><b>Latest retained input:</b> {shortId(latest.evidence_id)} · {latest.route} / {shortId(latest.pool)} · {age(latest.source_age_seconds)} source age · first known {dateTime(latest.first_received_at)}</p>}
          {excluded.length > 0 && <section className="paper-quality"><h4>Excluded from decisions ({excluded.length})</h4>{excluded.map((row) => <p key={row.evidence_id}><code>{shortId(row.evidence_id)}</code> · {(row.quality?.reasons || ['quality-exclusion']).join(', ')} — {row.quality?.repair || 'Collect corrected fresh evidence.'}</p>)}</section>}
          {(evidence.operating_intervals?.length || 0) > 0 && <section className="paper-intervals"><h4>Decision-service operating intervals</h4>{evidence.operating_intervals?.map((row) => <p key={row.interval_id}>{period(row.started_at, row.ended_at)} · {row.reason}</p>)}</section>}
          {evidence.decisions.length ? <div><table><caption>Decisions were persisted before any later outcome</caption><thead><tr><th scope="col">Decision / evidence</th><th scope="col">Asset / kind</th><th scope="col">Decided</th><th scope="col">Later outcome</th></tr></thead><tbody>{evidence.decisions.map((decision) => {
            const outcome = evidence.outcomes.find((item) => item.decision_key === decision.decision_key)
            return <tr key={decision.decision_key}><th scope="row"><code title={decision.decision_key}>{shortId(decision.decision_key)}</code><small>{shortId(decision.evidence_id)}</small></th><td>{decision.decision.mint ? shortId(decision.decision.mint) : 'Unknown asset'}<small>{decision.decision.kind || 'Recorded decision'}{decision.decision.reason ? ` · ${decision.decision.reason}` : ''}</small></td><td>{dateTime(decision.decided_at)}</td><td>{outcome ? <>{outcome.status}<small>{outcome.modeled_return_fraction === null ? 'Modeled result unavailable' : `${percentage(outcome.modeled_return_fraction)} modeled · ${outcome.horizon_seconds}s horizon`}</small></> : 'Pending later evidence'}</td></tr>
          })}</tbody></table></div> : <p>No durable decisions have been recorded for this run yet.</p>}
          {evidence.gaps.map((gap) => <p className="paper-gap" key={gap.gap_id}><b>{gap.reason}</b> · {period(gap.started_at, gap.ended_at)}</p>)}
          {evidence.faults?.map((fault) => <p className="paper-fault-history" key={fault.fault_id}><b>{fault.code}</b> · {dateTime(fault.occurred_at)} — {fault.message}. {fault.repair}</p>)}
        </details>}
        {historyError && <p className="research-inline-error" role="alert">{historyError}</p>}
        <footer>{run.state === 'running' && <button disabled={busy} onClick={() => command(run.id, 'pause')}>Pause</button>}{(run.state === 'paused' || run.state === 'interrupted') && <button className="primary" disabled={busy} onClick={() => command(run.id, 'resume')}>Resume same run</button>}{run.state !== 'stopped' && <button className="danger" disabled={busy} onClick={() => command(run.id, 'stop')}>Stop</button>}</footer></article>
    })}
  </>
}

export function ResearchWorkspace({ sourceSessionId, sessionLabel, close, onDraftCreated, fallback, preferFallback, initialTab = 'evidence' }: {
  sourceSessionId: string
  sessionLabel: string
  close: () => void
  onDraftCreated: () => void
  fallback: ReactNode
  preferFallback: boolean
  initialTab?: 'evidence' | 'paper'
}) {
  const research = useResearchWorkspace(sourceSessionId)
  const [tab, setTab] = useState<ResearchTab>(initialTab)
  const [selectedDataset, setSelectedDataset] = useState('')
  const [selectedReport, setSelectedReport] = useState('')
  const [confirmDraft, setConfirmDraft] = useState<string | null>(null)
  const [settings, setSettings] = useState<ResearchSettings>({ starting_capital: 1_000, capital_unit: 'USD', purpose: 'eligible', variable_fee_bps_each_side: 100, slippage_bps_each_side: 100, fixed_cost_each_side: 0, rent_each_entry: 0, embargo_seconds: 1_800 })
  const snapshot = research.snapshot
  const paperAllowed = snapshot?.paper_research_allowed === true
  const visibleTab = !paperAllowed && (tab === 'paper' || tab === 'campaigns') ? 'evidence' : tab
  const dataset = useMemo(() => snapshot?.datasets.find((item) => item.id === selectedDataset) || snapshot?.datasets[0] || snapshot?.dataset_preview || null, [selectedDataset, snapshot])
  const report = useMemo(() => snapshot?.reports.find((item) => item.id === selectedReport) || snapshot?.reports[0] || null, [selectedReport, snapshot])
  const job = snapshot?.jobs[0] || null
  useEffect(() => { if (dataset && !selectedDataset) setSelectedDataset(dataset.id) }, [dataset, selectedDataset])
  useEffect(() => { if (report && !selectedReport) setSelectedReport(report.id) }, [report, selectedReport])
  if (research.available === false) return fallback
  if (research.loading && !snapshot) return <div className="modal-backdrop session-review-backdrop"><section className="strategy-modal session-review-modal research-loading" role="dialog" aria-modal="true" aria-label="Research workspace"><span className="pulse" /><h2>Opening research records…</h2><p>Read-only navigation does not create a dataset, experiment or paper run.</p></section></div>
  if (!snapshot) return <div className="modal-backdrop session-review-backdrop"><section className="strategy-modal session-review-modal research-loading" role="dialog" aria-modal="true" aria-label="Research workspace unavailable"><h2>Research records unavailable</h2><p role="alert">{research.error || 'The research service did not return a workspace.'}</p><button onClick={() => void research.refresh()}>Retry</button><button onClick={close}>Close</button></section></div>
  if (preferFallback && !snapshot.dataset_preview && !snapshot.datasets.length && !snapshot.experiments.length && !snapshot.jobs.length && !snapshot.reports.length && !snapshot.paper_runs.length) return fallback
  const strategySha256 = snapshot.source_strategy_sha256 || dataset?.source_strategy_sha256 || ''
  return <div className="modal-backdrop session-review-backdrop" role="presentation" onMouseDown={research.busy ? undefined : close}>
    <section className="strategy-modal session-review-modal research-workspace" role="dialog" aria-modal="true" aria-label={`Research workspace for ${sessionLabel}`} onMouseDown={(event) => event.stopPropagation()}>
      <header className="session-review-header"><div><small>RESEARCH WORKSPACE</small><h2>{sessionLabel}</h2><p>Observed evidence · retrospective models{paperAllowed ? ' · prospective paper tracking' : ''}</p></div><span className="session-review-safety">No signing or execution</span><button className="close" aria-label="Close research workspace" disabled={Boolean(research.busy)} onClick={close}>×</button></header>
      <nav className="research-tabs" aria-label="Research workspace sections">{(['evidence', 'results', 'history', ...(paperAllowed ? ['paper', 'campaigns'] : [])] as ResearchTab[]).map((item) => <button key={item} aria-current={visibleTab === item ? 'page' : undefined} onClick={() => setTab(item)}>{item === 'paper' ? 'Forward paper' : item === 'campaigns' ? 'Campaigns' : item}</button>)}</nav>
      <main className="research-content">
        {visibleTab === 'evidence' && <>{dataset ? <DatasetSummary dataset={dataset} /> : <div className="research-empty"><b>No frozen evidence yet</b><p>Inspecting this page does not freeze data. Use the explicit action below when the source session is ready.</p></div>}
          <section className="research-freeze"><div><small>IMMUTABLE DATASET</small><h3>{dataset?.id ? `Frozen ${shortId(dataset.id)}` : 'Freeze retained source evidence'}</h3><p>A freeze keeps recorded gaps, source/receipt times, routes, pools and units. Limits reject an oversized source instead of truncating it.</p></div><button className="primary" disabled={Boolean(research.busy)} onClick={() => void research.freeze()}>{research.busy === 'freeze' ? 'Freezing…' : 'Freeze current evidence'}</button></section>
          {dataset && <section className="research-settings"><header><small>BOUNDED COMPARISON</small><h3>Review settings before starting</h3><p>Registration preserves the exact dataset, candidates, capital, cost model, split and code lineage.</p></header><div><label>Starting capital (USD)<input type="number" min="1" value={settings.starting_capital} onChange={(event) => setSettings({ ...settings, starting_capital: Number(event.target.value) })} /></label><label>Purpose<select value={settings.purpose} onChange={(event) => setSettings({ ...settings, purpose: event.target.value as ResearchSettings['purpose'] })}><option value="eligible">Eligible comparison</option><option value="exploratory">Exploratory only</option></select></label><label>Fee each side (bps)<input type="number" min="0" value={settings.variable_fee_bps_each_side} onChange={(event) => setSettings({ ...settings, variable_fee_bps_each_side: Number(event.target.value) })} /></label><label>Slippage each side (bps)<input type="number" min="0" value={settings.slippage_bps_each_side} onChange={(event) => setSettings({ ...settings, slippage_bps_each_side: Number(event.target.value) })} /></label><label>Fixed cost each side<input type="number" min="0" value={settings.fixed_cost_each_side} onChange={(event) => setSettings({ ...settings, fixed_cost_each_side: Number(event.target.value) })} /></label><label>Embargo (seconds)<input type="number" min="0" value={settings.embargo_seconds} onChange={(event) => setSettings({ ...settings, embargo_seconds: Number(event.target.value) })} /></label></div><button className="primary" disabled={Boolean(research.busy) || !dataset.eligible || settings.starting_capital <= 0} onClick={() => void research.compare(dataset.id, settings)}>{research.busy === 'compare' ? 'Registering…' : 'Start background comparison'}</button></section>}
          {job && <JobStatus job={job} disabled={Boolean(research.busy)} cancel={() => void research.cancel(job.id)} retry={() => void research.retry(job.id)} />}</>}
        {visibleTab === 'results' && <>{snapshot.reports.length > 1 && <label className="research-select">Immutable report<select value={report?.id || ''} onChange={(event) => setSelectedReport(event.target.value)}>{snapshot.reports.map((item) => <option key={item.id} value={item.id}>{shortId(item.id)} · {dateTime(item.created_at)}</option>)}</select></label>}{report ? <><Results report={report} confirmDraft={confirmDraft} setConfirmDraft={setConfirmDraft} disabled={Boolean(research.busy)} createDraft={(candidate) => { void research.draft(report.id, candidate).then((created) => { if (created) onDraftCreated() }) }} /><EvidenceAssistantPanel selector={{ schema: 'pumpwizard.evidence-selector/v1', kind: 'research_report', id: report.id, parent_id: null }} questions={RESEARCH_REPORT_QUESTIONS} title="Explain this research report" /></> : <div className="research-empty"><b>No completed report</b><p>Completed background jobs publish validated reports. Partial and failed attempts do not.</p></div>}</>}
        {visibleTab === 'history' && <ExperimentHistory experiments={snapshot.experiments} attempts={snapshot.attempts} reports={snapshot.reports} />}
        {visibleTab === 'paper' && <PaperPanel runs={snapshot.paper_runs} strategySha256={strategySha256} busy={Boolean(research.busy)} start={(capital) => { void research.paperStart(strategySha256, capital) }} command={(runId, command) => { void research.paperCommand(runId, command) }} />}
        {visibleTab === 'campaigns' && <CampaignWorkspace sourceSessionId={sourceSessionId} active={visibleTab === 'campaigns'} />}
      </main>
      {research.error && <p className="session-review-error" role="alert">{research.error}</p>}
      {research.operationWarning && <p className="session-review-warning" role="status">{research.operationWarning}</p>}
      <footer className="session-review-footer"><p>{paperAllowed ? 'Observed records, retrospective replay, forward paper evidence and actual wallet receipts remain separate evidence classes.' : 'Observed records, retrospective replay and actual wallet receipts remain separate evidence classes.'}</p><div><button disabled={Boolean(research.busy)} onClick={() => void research.refresh()}>{research.loading ? 'Refreshing…' : 'Refresh'}</button><button disabled={Boolean(research.busy)} onClick={close}>Done</button></div></footer>
    </section>
  </div>
}
