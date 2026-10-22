import { useEffect, useMemo, useState } from 'react'
import { ResearchWorkspace } from './ResearchWorkspace'
import './session-review.css'

type ReviewMetrics = {
  signals: number
  scored_samples: number
  unknown_samples: number
  coverage_fraction: number
  mean_modeled_return_fraction: number | null
  compounded_modeled_result_fraction: number | null
  maximum_trade_drawdown_fraction: number | null
  loss_tail_fraction: number | null
  positive_fraction: number | null
  exposure_fraction: number
}

type ReviewSplit = {
  outcomes: { status: 'scored' | 'unknown'; coverage: string }[]
  metrics: ReviewMetrics
}

export type SessionReviewResult = {
  candidate_id: string
  kind: 'baseline' | 'current' | 'candidate'
  label: string
  package_sha256: string | null
  changes: Record<string, number>
  cost_model: null | {
    variable_fee_fraction_each_side: number
    slippage_fraction_each_side: number
    fixed_cost_usd_each_side: number
    entry_rent_usd: number
    basis: string
  }
  splits: { train: ReviewSplit; validation: ReviewSplit; holdout: ReviewSplit }
  embargoed_signals: number
}

export type SessionReviewReport = {
  schema: 'pumpwizard.session-review-report/v1'
  id: string
  dataset_id: string
  source_session_id: string
  source_strategy_sha256: string
  engine_version: string
  search_sha256: string
  inputs: {
    horizon_seconds: number
    endpoint_grace_seconds: number
    split_boundaries: {
      first_at: number
      train_end_at: number
      validation_end_at: number
      last_at: number
    }
    ranking: {
      selection_split: 'validation'
      objective: string
      minimum_samples: number
      minimum_coverage_fraction: number
    }
  }
  results: SessionReviewResult[]
  recommendation: {
    action: 'collect_more_data' | 'keep_current' | 'create_inactive_draft'
    candidate_id: string | null
    reason: string
  }
  holdout_policy: string
  funded_performance: false
  submission_enabled: false
  limitations: string[]
}

export type SessionReviewEvidence = {
  observations: number
  decisions: number
  decisions_with_recorded_features: number
  decisions_with_incomplete_features: number
  scored_outcomes: number
  unknown_outcomes: number
  outcome_basis: string
}

function percent(value: number | null, digits = 1) {
  if (value === null || !Number.isFinite(value)) return 'Not available'
  return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(digits)}%`
}

function coverage(metrics: ReviewMetrics) {
  return `${metrics.scored_samples}/${metrics.signals} · ${(metrics.coverage_fraction * 100).toFixed(0)}%`
}

function shortHash(value: string | null) {
  return value ? `${value.slice(0, 10)}…${value.slice(-8)}` : 'No package'
}

function duration(seconds: number) {
  if (seconds % 3600 === 0) return `${seconds / 3600}h`
  if (seconds % 60 === 0) return `${seconds / 60}m`
  return `${seconds}s`
}

function changeLabel(changes: Record<string, number>) {
  const entries = Object.entries(changes)
  if (!entries.length) return 'Exact source package'
  return entries.map(([key, value]) => `${key.replace(/_/g, ' ')} ${value}`).join(' · ')
}

function recommendationLabel(action: SessionReviewReport['recommendation']['action']) {
  if (action === 'create_inactive_draft') return 'Bounded candidate to review'
  if (action === 'keep_current') return 'Keep current'
  return 'Collect more evidence'
}

function LegacySessionReview({
  sessionLabel,
  report,
  evidence,
  busy,
  error,
  close,
  run,
  keepCurrent,
  createDraft,
}: {
  sessionLabel: string
  report: SessionReviewReport | null
  evidence: SessionReviewEvidence | null
  busy: boolean
  error: string
  close: () => void
  run: () => void
  keepCurrent: () => void
  createDraft: (candidateId: string) => void
}) {
  const [confirmCandidate, setConfirmCandidate] = useState<string | null>(null)
  useEffect(() => setConfirmCandidate(null), [report?.id])
  const recommended = useMemo(() => report?.results.find(
    (row) => row.candidate_id === report.recommendation.candidate_id,
  ) || null, [report])

  return <div className="modal-backdrop session-review-backdrop" role="presentation" onMouseDown={busy ? undefined : close}>
    <section className="strategy-modal session-review-modal" role="dialog" aria-modal="true" aria-label="Session review" onMouseDown={(event) => event.stopPropagation()}>
      <header className="session-review-header">
        <div><small>OFFLINE LAB</small><h2>Review {sessionLabel}</h2><p>Frozen session evidence · modeled comparison only</p></div>
        <span className="session-review-safety">No signing or execution</span>
        <button className="close" aria-label="Close session review" disabled={busy} onClick={close}>×</button>
      </header>

      {!report ? <div className="session-review-start">
        <span aria-hidden="true">◎</span>
        <div><small>REPRODUCIBLE RESEARCH</small><h3>Compare this session without changing it</h3>
          <p>The Lab freezes this session’s retained quotes, eligible decisions, rejections and outcome coverage. It tests the exact current package, a no-entry baseline and no more than five parser-validated variants.</p>
          <ul><li>Chronological training, validation and untouched holdout</li><li>Missing quotes and route changes stay unknown</li><li>A result can only prepare an inactive, unallocated successor draft</li></ul>
        </div>
        <button className="primary" disabled={busy} onClick={run}>{busy ? 'Freezing evidence…' : 'Freeze & compare'}</button>
      </div> : <>
        <div className="session-review-summary">
          <section className={`session-review-verdict ${report.recommendation.action}`}>
            <small>VALIDATION-ONLY RESULT</small>
            <h3>{recommendationLabel(report.recommendation.action)}</h3>
            <p>{report.recommendation.reason}</p>
          </section>
          <dl>
            <div><dt>Dataset</dt><dd title={report.dataset_id}><code>{shortHash(report.dataset_id)}</code></dd></div>
            <div><dt>Strategy</dt><dd title={report.source_strategy_sha256}><code>{shortHash(report.source_strategy_sha256)}</code></dd></div>
            <div><dt>Outcome horizon</dt><dd>{duration(report.inputs.horizon_seconds)}</dd></div>
            <div><dt>Selection rule</dt><dd>Validation only</dd></div>
          </dl>
        </div>

        {evidence && <section className="session-review-evidence" aria-label="Frozen evidence coverage">
          <span><small>OBSERVATIONS</small><b>{evidence.observations}</b></span>
          <span><small>DECISIONS / REJECTIONS</small><b>{evidence.decisions}</b></span>
          <span><small>RECORDED FEATURES</small><b>{evidence.decisions_with_recorded_features}/{evidence.decisions}</b></span>
          <span><small>KNOWN OUTCOMES</small><b>{evidence.scored_outcomes}</b><em>{evidence.unknown_outcomes} unknown</em></span>
        </section>}

        <div className="session-review-table-wrap">
          <table className="session-review-table">
            <thead><tr><th scope="col">Strategy</th><th scope="col">Training</th><th scope="col">Validation</th><th scope="col">Holdout</th><th scope="col">Exposure</th><th scope="col">Worst drawdown</th></tr></thead>
            <tbody>{report.results.map((result) => {
              const selected = result.candidate_id === report.recommendation.candidate_id
              const validation = result.splits.validation.metrics
              return <tr key={result.candidate_id} className={selected ? 'selected' : ''}>
                <th scope="row"><span>{result.label}{selected && <em>Selected on validation</em>}</span><small>{changeLabel(result.changes)}</small><code title={result.package_sha256 || ''}>{shortHash(result.package_sha256)}</code></th>
                <td><b>{percent(result.splits.train.metrics.mean_modeled_return_fraction)}</b><small>{coverage(result.splits.train.metrics)} covered</small></td>
                <td><b>{percent(validation.mean_modeled_return_fraction)}</b><small>{coverage(validation)} covered</small></td>
                <td><b>{percent(result.splits.holdout.metrics.mean_modeled_return_fraction)}</b><small>{coverage(result.splits.holdout.metrics)} covered</small></td>
                <td><b>{percent(validation.exposure_fraction)}</b><small>validation time</small></td>
                <td><b>{percent(validation.maximum_trade_drawdown_fraction)}</b><small>{result.embargoed_signals} embargoed</small></td>
              </tr>
            })}</tbody>
          </table>
        </div>

        <div className="session-review-details">
          <section><small>MODELED COSTS</small><h3>Explicit round-trip assumptions</h3>
            {recommended?.cost_model ? <dl>
              <div><dt>Fee each side</dt><dd>{percent(recommended.cost_model.variable_fee_fraction_each_side)}</dd></div>
              <div><dt>Slippage each side</dt><dd>{percent(recommended.cost_model.slippage_fraction_each_side)}</dd></div>
              <div><dt>Fixed cost each side</dt><dd>${recommended.cost_model.fixed_cost_usd_each_side.toFixed(2)}</dd></div>
              <div><dt>Entry rent</dt><dd>${recommended.cost_model.entry_rent_usd.toFixed(2)}</dd></div>
            </dl> : <p>The no-entry baseline has no modeled costs or exposure.</p>}
            <p>These assumptions are not current executable quotes or funded receipts.</p>
          </section>
          <section><small>EVIDENCE LIMITS</small><h3>What this result cannot claim</h3><ul>{report.limitations.map((item) => <li key={item}>{item}</li>)}</ul><p>{report.holdout_policy}</p></section>
        </div>

        {confirmCandidate && recommended && <section className="session-review-confirm" role="alertdialog" aria-label="Confirm inactive successor draft">
          <div><small>EXPLICIT ACCEPTANCE</small><h3>Create an inactive successor?</h3><p>This imports <b>{recommended.label}</b> as a new immutable package and creates a paused session with monitoring off, entries disabled, manual exits and zero wallet allocation. It does not copy execution authorization.</p></div>
          <button disabled={busy} onClick={() => setConfirmCandidate(null)}>Cancel</button>
          <button className="primary" disabled={busy} onClick={() => createDraft(confirmCandidate)}>{busy ? 'Creating draft…' : 'Create inactive draft'}</button>
        </section>}
      </>}

      {error && <p className="session-review-error" role="alert">{error}</p>}
      <footer className="session-review-footer">
        <p>Quote outcomes, modeled replay and funded receipts remain separate evidence classes.</p>
        <div>{report && <button disabled={busy} onClick={run}>Rerun comparison</button>}
          {report && <button disabled={busy} onClick={keepCurrent}>Keep current</button>}
          {report?.recommendation.action === 'create_inactive_draft' && report.recommendation.candidate_id && <button className="primary" disabled={busy || Boolean(confirmCandidate)} onClick={() => setConfirmCandidate(report.recommendation.candidate_id)}>Review draft action</button>}
          <button disabled={busy} onClick={close}>Done</button>
        </div>
      </footer>
    </section>
  </div>
}

export function SessionReview({
  sourceSessionId,
  sessionLabel,
  report,
  evidence,
  busy,
  error,
  close,
  run,
  keepCurrent,
  createDraft,
  researchDraftCreated,
  initialTab,
}: {
  sourceSessionId: string
  sessionLabel: string
  report: SessionReviewReport | null
  evidence: SessionReviewEvidence | null
  busy: boolean
  error: string
  close: () => void
  run: () => void
  keepCurrent: () => void
  createDraft: (candidateId: string) => void
  researchDraftCreated: () => void
  initialTab?: 'evidence' | 'paper'
}) {
  return <ResearchWorkspace
    initialTab={initialTab}
    sourceSessionId={sourceSessionId}
    sessionLabel={sessionLabel}
    close={close}
    onDraftCreated={researchDraftCreated}
    preferFallback={initialTab !== 'paper' && Boolean(report || evidence)}
    fallback={<LegacySessionReview sessionLabel={sessionLabel} report={report} evidence={evidence} busy={busy} error={error} close={close} run={run} keepCurrent={keepCurrent} createDraft={createDraft} />}
  />
}
