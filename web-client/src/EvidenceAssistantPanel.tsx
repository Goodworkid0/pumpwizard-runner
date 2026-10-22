import { WalletWorkspaceScope } from './walletWorkspaceScope'
import { useContext, useEffect, useMemo, useState } from 'react'
import { api, errorText } from './browserWallet'
import './evidence-assistant.css'

export type EvidenceSelector = {
  schema: 'pumpwizard.evidence-selector/v1'
  kind: 'campaign' | 'campaign_report' | 'research_report' | 'decision' | 'holding' | 'receipt'
  id: string
  parent_id: string | null
}

type EvidenceQuestion = 'summary' | 'why_decision' | 'cost_impact' | 'benchmark_result' | 'valuation_reliability' | 'missing_evidence' | 'receipt_status'

type EvidenceFact = {
  id: string
  subject_id: string
  metric: string
  value: string | number | boolean | null
  unit: string | null
  state: 'known' | 'unknown'
  display: string
  citation_ids: string[]
}

type EvidenceContext = {
  id: string
  selector: EvidenceSelector
  evidence_class: string
  as_of: number | null
  facts: EvidenceFact[]
  blockers: { code: string; message: string; citation_ids: string[] }[]
  sources: { id: string; kind: string; record_id: string; href: string; as_of: number | null }[]
  authority: Record<string, boolean>
}

type EvidenceClaim = {
  id?: string
  status?: 'supported' | 'unknown' | 'withheld'
  text: string
  citation_ids: string[]
  fact_ids: string[]
}

type LocalExplanation = {
  id: string
  context_id: string
  question: EvidenceQuestion
  claims: EvidenceClaim[]
  source_links: { citation_id: string; href: string }[]
  provider_used: false
  limitations: string[]
}

type EvidenceView = {
  schema: 'pumpwizard.evidence-assistant-view/v1'
  context: EvidenceContext
  explanation: LocalExplanation
  ai: {
    available: boolean
    profile: { id: string; label: string; model: string } | null
    unavailable_reason: string | null
  }
}

type EvidencePreview = {
  schema: 'pumpwizard.evidence-ai-preview/v1'
  request_id: string
  conversation: string
  provider: { profile_id: string; endpoint: string; model: string }
  context_id: string
  request_sha256: string
  estimated_input: number
  output_cap: number
  estimated_cost_usd: number | null
  actual_cost_usd: null
  preview: unknown
  expires_in_seconds: number
}

type ReviewedResponse = {
  id: string
  context_id: string
  status: 'accepted' | 'partial' | 'withheld'
  summary: EvidenceClaim | null
  claims: EvidenceClaim[]
  flags: { code: string; location: string }[]
  validation_limits: string[]
}

const QUESTION_LABELS: Record<EvidenceQuestion, string> = {
  summary: 'Summarize this evidence',
  why_decision: 'Why was this decision made?',
  cost_impact: 'What did known costs change?',
  benchmark_result: 'What beat the declared benchmarks?',
  valuation_reliability: 'Can this holding be valued reliably?',
  missing_evidence: 'What evidence is missing?',
  receipt_status: 'What does this receipt establish?',
}

function shortIdentity(value: string) {
  return value.length > 24 ? `${value.slice(0, 11)}…${value.slice(-9)}` : value
}

export function EvidenceAssistantPanel({ selector, questions, title = 'Explain selected evidence', scopeQuery: explicitScope }: {
  scopeQuery?: string
  selector: EvidenceSelector
  questions: readonly EvidenceQuestion[]
  title?: string
}) {
  const workspaceScope = useContext(WalletWorkspaceScope)
  const scopeQuery = explicitScope || workspaceScope
  const selectorIdentity = `${selector.kind}:${selector.parent_id || ''}:${selector.id}`
  const [question, setQuestion] = useState<EvidenceQuestion>(questions[0] || 'summary')
  const [view, setView] = useState<EvidenceView | null>(null)
  const [preview, setPreview] = useState<EvidencePreview | null>(null)
  const [reviewed, setReviewed] = useState<ReviewedResponse | null>(null)
  const [message, setMessage] = useState('Explain the selected evidence and its limitations without adding unsupported claims.')
  const [busy, setBusy] = useState<'local' | 'preview' | 'approve' | 'decline' | ''>('')
  const [error, setError] = useState('')
  const sourceLinks = useMemo(() => new Map(
    view?.explanation.source_links.map((row) => [row.citation_id, row.href]) || [],
  ), [view])

  useEffect(() => {
    setQuestion(questions[0] || 'summary')
    setView(null)
    setPreview(null)
    setReviewed(null)
    setError('')
  }, [questions, selectorIdentity])

  const inspect = async (selectedQuestion = question) => {
    setBusy('local')
    setError('')
    setPreview(null)
    setReviewed(null)
    try {
      const result = await api<EvidenceView>('/api/v1/assistant/evidence', {
        selector,
        question: selectedQuestion,
      })
      setView(result)
    } catch (reason) {
      setView(null)
      setError(errorText(reason))
    } finally {
      setBusy('')
    }
  }

  const chooseQuestion = (next: EvidenceQuestion) => {
    setQuestion(next)
    setPreview(null)
    setReviewed(null)
    if (view) void inspect(next)
  }

  const prepare = async () => {
    if (!view || !message.trim()) return
    setBusy('preview')
    setError('')
    setReviewed(null)
    try {
      const result = await api<EvidencePreview>(`/api/v1/assistant/evidence/prepare${scopeQuery}`, {
        conversation: null,
        message: message.trim(),
        max_output: 800,
        selector,
        question,
        context_id: view.context.id,
      })
      setPreview(result)
    } catch (reason) {
      setPreview(null)
      setError(errorText(reason))
    } finally {
      setBusy('')
    }
  }

  const approve = async () => {
    if (!preview) return
    setBusy('approve')
    setError('')
    try {
      const result = await api<{ reviewed_response: ReviewedResponse | null }>(
        `/api/v1/assistant/approve${scopeQuery}`, { request_id: preview.request_id },
      )
      setReviewed(result.reviewed_response)
      setPreview(null)
    } catch (reason) {
      setPreview(null)
      setError(errorText(reason))
    } finally {
      setBusy('')
    }
  }

  const decline = async () => {
    if (!preview) return
    setBusy('decline')
    setError('')
    try {
      await api(`/api/v1/assistant/decline${scopeQuery}`, { request_id: preview.request_id })
      setPreview(null)
    } catch (reason) {
      setError(errorText(reason))
    } finally {
      setBusy('')
    }
  }

  return <section className="evidence-assistant" aria-label={`${title}: ${selector.kind}`}>
    <header><div><small>LOCAL EVIDENCE ASSISTANT</small><h3>{title}</h3><p>Reads only this retained {selector.kind.replace(/_/g, ' ')}. Opening or changing the question never contacts an AI provider.</p></div><code title={selector.id}>{shortIdentity(selector.id)}</code></header>
    <div className="evidence-assistant-actions"><label>Question<select value={question} onChange={(event) => chooseQuestion(event.target.value as EvidenceQuestion)}>{questions.map((item) => <option value={item} key={item}>{QUESTION_LABELS[item]}</option>)}</select></label><button disabled={Boolean(busy)} onClick={() => void inspect()}>{busy === 'local' ? 'Reading retained evidence…' : view ? 'Refresh local explanation' : 'Explain from local evidence'}</button></div>
    {view && <>
      <div className="evidence-assistant-claims">{view.explanation.claims.map((claim, index) => <article className={claim.status || 'supported'} key={claim.id || `${claim.text}:${index}`}><span>{(claim.status || 'supported').toUpperCase()}</span><p>{claim.text}</p><div>{claim.citation_ids.map((citation) => sourceLinks.has(citation) ? <a href={sourceLinks.get(citation)} key={citation}>Source {shortIdentity(citation)}</a> : <code key={citation}>{shortIdentity(citation)}</code>)}</div></article>)}</div>
      {view.context.blockers.length > 0 && <details className="evidence-assistant-blockers"><summary>Evidence limitations ({view.context.blockers.length})</summary><ul>{view.context.blockers.map((row) => <li key={`${row.code}:${row.message}`}><b>{row.code.replace(/_/g, ' ')}</b> — {row.message}</li>)}</ul></details>}
      <details className="evidence-assistant-facts"><summary>Typed facts and exact source identity</summary><p>Context <code>{shortIdentity(view.context.id)}</code> · class {view.context.evidence_class} · {view.context.facts.filter((row) => row.state === 'known').length}/{view.context.facts.length} facts known</p><table><thead><tr><th>Subject</th><th>Metric</th><th>Retained value</th></tr></thead><tbody>{view.context.facts.slice(0, 24).map((fact) => <tr key={fact.id}><td>{fact.subject_id}</td><td>{fact.metric.replace(/_/g, ' ')}</td><td>{fact.display}</td></tr>)}</tbody></table></details>
      <section className="evidence-assistant-ai"><div><small>OPTIONAL AI EXPLANATION</small><b>{view.ai.available ? `${view.ai.profile?.label} · ${view.ai.profile?.model}` : 'Provider unavailable'}</b><p>{view.ai.available ? 'Preparing freezes the exact context and shows what would be sent. A second explicit approval is still required.' : view.ai.unavailable_reason}</p></div>{view.ai.available && <><label>Question for the configured provider<textarea rows={3} maxLength={2000} value={message} onChange={(event) => { setMessage(event.target.value); setPreview(null) }} /></label><button disabled={Boolean(busy) || !message.trim()} onClick={() => void prepare()}>{busy === 'preview' ? 'Preparing…' : 'Preview optional AI request'}</button></>}</section>
    </>}
    {preview && <section className="evidence-assistant-preview" role="alertdialog" aria-label="Review optional AI evidence request"><header><div><small>EXPLICIT REQUEST REVIEW</small><h4>One frozen provider request</h4></div><code>{shortIdentity(preview.request_sha256)}</code></header><dl><div><dt>Provider / model</dt><dd>{preview.provider.model}</dd></div><div><dt>Estimated input</dt><dd>{preview.estimated_input.toLocaleString()}</dd></div><div><dt>Output cap</dt><dd>{preview.output_cap.toLocaleString()}</dd></div><div><dt>Estimated cost</dt><dd>{preview.estimated_cost_usd === null ? 'Unknown' : `$${preview.estimated_cost_usd}`}</dd></div></dl><details><summary>Exact bounded preview</summary><pre>{JSON.stringify(preview.preview, null, 2)}</pre></details><div><button disabled={Boolean(busy)} onClick={() => void decline()}>{busy === 'decline' ? 'Declining…' : 'Decline'}</button><button className="primary" disabled={Boolean(busy)} onClick={() => void approve()}>{busy === 'approve' ? 'Sending approved request…' : 'Approve one request'}</button></div></section>}
    {reviewed && <section className={`evidence-assistant-reviewed ${reviewed.status}`}><small>VALIDATED AI RESPONSE · {reviewed.status.toUpperCase()}</small>{reviewed.summary && <p>{reviewed.summary.text}</p>}{reviewed.claims.map((claim, index) => <p key={`${claim.text}:${index}`}>{claim.text}</p>)}{reviewed.flags.length > 0 && <ul>{reviewed.flags.map((flag) => <li key={`${flag.code}:${flag.location}`}>{flag.code.replace(/_/g, ' ')} at {flag.location}</li>)}</ul>}<em>{reviewed.validation_limits.join(' ')}</em></section>}
    {error && <p className="evidence-assistant-error" role="alert">{error}</p>}
  </section>
}
