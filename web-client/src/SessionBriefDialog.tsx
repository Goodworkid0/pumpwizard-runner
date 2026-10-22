import { useMemo, useState } from 'react'
import type { LocalStrategy } from './StrategySelection'
import type { SessionPlan, SessionPreferences } from './SessionPlanDialog'

export type SessionBriefInput = {
  goal: 'observe-market' | 'validate-strategy' | 'collect-evidence'
  intent: string
  paper_budget_usd: number
  max_session_loss_usd: number
  review_after_seconds: number
  session_mode: SessionPlan['session_mode']
  execution_style: SessionPlan['execution_style']
  automation_scope: SessionPlan['automation_scope']
  custody_mode?: 'main-wallet-service'
  profit_management: SessionPlan['profit_management']
  preferences: SessionPreferences | null
}

const GOALS: { id: SessionBriefInput['goal']; label: string; description: string }[] = [
  { id: 'observe-market', label: 'Observe the market', description: 'Watch matching tokens and learn how this strategy reacts.' },
  { id: 'validate-strategy', label: 'Validate the strategy', description: 'Collect evidence to challenge the strategy before any funded use.' },
  { id: 'collect-evidence', label: 'Build a dataset', description: 'Retain a bounded decision history for later Lab comparisons.' },
]

const WINDOWS = [
  { value: 3_600, label: '1 hour' },
  { value: 14_400, label: '4 hours' },
  { value: 86_400, label: '1 day' },
  { value: 259_200, label: '3 days' },
  { value: 604_800, label: '7 days' },
  { value: 2_592_000, label: '30 days' },
]

function number(value: unknown, fallback: number) {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : fallback
}

function money(value: number) {
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
}

export function SessionBriefDialog({ strategy, plan, close, submit }: {
  strategy: LocalStrategy
  plan: SessionPlan
  close: () => void
  submit: (label: string, brief: SessionBriefInput) => Promise<void>
}) {
  const config = strategy.configuration || {}
  const orderUsd = number(config.order_usd, 10)
  const configuredBudget = number(config.budget_usd, Math.max(500, orderUsd * 20))
  const maxPositions = Math.max(1, Math.round(number(config.max_positions, 1)))
  const [step, setStep] = useState(0)
  const [goal, setGoal] = useState<SessionBriefInput['goal']>('validate-strategy')
  const [label, setLabel] = useState(strategy.label)
  const [intent, setIntent] = useState(`Observe ${strategy.label}, retain every decision, and review the evidence before changing its rules.`)
  const [budget, setBudget] = useState(String(configuredBudget))
  const [loss, setLoss] = useState(String(Math.min(configuredBudget, Math.max(orderUsd, configuredBudget * .2))))
  const [reviewAfter, setReviewAfter] = useState(86_400)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const selectedGoal = GOALS.find((item) => item.id === goal) || GOALS[0]
  const budgetNumber = Number(budget)
  const lossNumber = Number(loss)
  const windowLabel = WINDOWS.find((item) => item.value === reviewAfter)?.label || `${reviewAfter} seconds`
  const validIdentity = label.trim().length > 0 && label.trim().length <= 120 && intent.trim().length > 0 && intent.trim().length <= 500
  const validLimits = Number.isFinite(budgetNumber) && budgetNumber >= orderUsd && budgetNumber <= 1_000_000_000
    && Number.isFinite(lossNumber) && lossNumber > 0 && lossNumber <= budgetNumber
  const answers = useMemo(() => [
    ['Session mode', plan.session_mode === 'monitoring' ? 'Monitoring' : 'Trading'],
    ...(plan.execution_style ? [['Execution', plan.execution_style === 'interactive' ? 'Interactive · sign every order' : plan.automation_scope === 'entries-only' ? 'Automatic entries · you manage every exit' : plan.automation_scope === 'exits-only' ? 'Automatic strategy exits · main-wallet allowance required' : 'Full Autopilot · main-wallet allowance required']] : []),
    ...(plan.custody_mode ? [['Signing custody', plan.custody_mode === 'main-wallet-service' ? 'Background service · continues after Dashboard closes' : 'Browser tab · stops when closed']] : []),
    ...(plan.profit_management ? [['Profit management', plan.profit_management.replace(/-/g, ' ')]] : []),
    ['Goal', selectedGoal.label],
    ['Session name', label.trim()],
    ['Trading intent', intent.trim()],
    ['Paper research budget', money(Number.isFinite(budgetNumber) ? budgetNumber : 0)],
    ['Loss review threshold', money(Number.isFinite(lossNumber) ? lossNumber : 0)],
    ['Review after', windowLabel],
    ['Wallet allocation', plan.session_mode === 'trade' ? `${plan.allocation_sol} SOL` : 'None'],
    ...(plan.session_mode === 'trade' ? [
      ['Investment size', plan.investment_style],
      ['Time horizon', plan.time_horizon],
      ['Risk / minimum reward', `${plan.risk_tolerance} / ${plan.reward_risk_ratio}:1`],
    ] : []),
  ], [selectedGoal.label, label, intent, budgetNumber, lossNumber, windowLabel, plan])

  const next = () => {
    setError('')
    if (step === 1 && !validIdentity) {
      setError('Enter a session name and a 1–500 character explanation of what you want this session to learn.')
      return
    }
    if (step === 2 && !validLimits) {
      setError(`The paper budget must cover at least one ${money(orderUsd)} strategy order, and the loss threshold must fit inside that budget.`)
      return
    }
    setStep((current) => Math.min(3, current + 1))
  }

  const create = async () => {
    if (!validIdentity || !validLimits || busy) return
    setBusy(true)
    setError('')
    try {
      await submit(label.trim(), {
        goal,
        intent: intent.trim(),
        paper_budget_usd: budgetNumber,
        max_session_loss_usd: lossNumber,
        review_after_seconds: reviewAfter,
        session_mode: plan.session_mode,
        execution_style: plan.execution_style,
        automation_scope: plan.automation_scope,
        ...(plan.custody_mode === 'main-wallet-service' ? { custody_mode: plan.custody_mode } : {}),
        profit_management: plan.profit_management,
        preferences: plan.session_mode === 'trade' ? {
          risk_tolerance: plan.risk_tolerance,
          reward_risk_ratio: plan.reward_risk_ratio,
          investment_style: plan.investment_style,
          time_horizon: plan.time_horizon,
        } : null,
      })
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'The local Runner rejected this session brief.')
      setBusy(false)
    }
  }

  return <div className="modal-backdrop detail-layer" role="presentation">
    <section className="strategy-modal session-brief-wizard" role="dialog" aria-modal="true" aria-label="Create session brief">
      <button className="close" aria-label="Close dialog" disabled={busy} onClick={close}>×</button>
      <small>GUIDED SESSION SETUP · QUESTION {step + 1} OF 4</small>
      <h2>Describe this session before it starts</h2>
      <p>The answers become a durable research brief and permission manifest. {plan.session_mode === 'trade' ? 'Creating the session commits the reviewed wallet allocation; activation and signing remain separate.' : 'Monitoring reserves no wallet funds and cannot prepare or submit orders.'}</p>
      <div className="session-brief-progress" aria-hidden="true">{[0, 1, 2, 3].map((item) => <i className={item <= step ? 'complete' : ''} key={item} />)}</div>

      <div className="session-interview" aria-label="Session questions and answers">
        <aside><small>STRATEGY PACKAGE</small><b>{strategy.label}</b><p>{String(config.entry_style || 'Local')} entries · {money(orderUsd)} configured order · {maxPositions} maximum positions</p><p>{plan.session_mode === 'trade' ? `${plan.allocation_sol} SOL · ${plan.execution_style} · ${plan.investment_style} sizing · ${plan.time_horizon}` : 'Monitoring · no wallet allocation · strategy rules set timing'}</p><em>Rules remain immutable</em></aside>
        <section>
          {step === 0 && <><div className="session-question"><i>✦</i><span><b>What should this session accomplish?</b><p>Choose the purpose that should guide its later review.</p></span></div><div className="session-answer-options">{GOALS.map((item) => <button className={goal === item.id ? 'selected' : ''} aria-pressed={goal === item.id} onClick={() => setGoal(item.id)} key={item.id}><b>{item.label}</b><span>{item.description}</span></button>)}</div></>}
          {step === 1 && <><div className="session-question"><i>✦</i><span><b>How would you like this session to behave?</b><p>Name the run and explain what evidence you want it to collect or challenge.</p></span></div><div className="session-answer-fields"><label>Session name<input value={label} maxLength={120} onChange={(event) => setLabel(event.target.value)} /></label><label>Trading and learning intent<textarea value={intent} maxLength={500} rows={5} onChange={(event) => setIntent(event.target.value)} /></label><small>{intent.length}/500 characters</small></div></>}
          {step === 2 && <><div className="session-question"><i>✦</i><span><b>What research envelope should it use?</b><p>These values score and review evidence only. They do not reserve SOL or authorize orders.</p></span></div><div className="session-answer-fields limits"><label>Paper research budget (USD)<input type="number" min={orderUsd} step="0.01" value={budget} onChange={(event) => setBudget(event.target.value)} /><small>At least one configured {money(orderUsd)} order.</small></label><label>Loss review threshold (USD)<input type="number" min="0.01" step="0.01" value={loss} onChange={(event) => setLoss(event.target.value)} /><small>Flag the session for review at this modeled loss.</small></label><label>Review window<select value={reviewAfter} onChange={(event) => setReviewAfter(Number(event.target.value))}>{WINDOWS.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}</select><small>This is a review target, not automatic trading permission.</small></label></div></>}
          {step === 3 && <><div className="session-question"><i>✓</i><span><b>Review the immutable session brief</b><p>The brief stays fixed. Strategy-rule review remains separate and stops once the session has activity.</p></span></div><dl className="session-brief-review">{answers.map(([term, value]) => <div key={term}><dt>{term}</dt><dd>{value}</dd></div>)}</dl><p className="session-brief-safety">{plan.session_mode === 'monitoring' ? 'The session will retain monitoring evidence without reserving SOL or preparing orders.' : plan.execution_style === 'interactive' ? `The Runner will recheck the live balance and reserve ${plan.allocation_sol} SOL. Every order still requires your connected-wallet signature.` : plan.custody_mode === 'main-wallet-service' ? `The Runner will recheck the live balance and reserve ${plan.allocation_sol} SOL in this generated main wallet. Closing Dashboard may leave a separately authorized service running. Encrypted enrollment, exact allowance, fresh unlock and Start remain separate; production Start is blocked.` : `The Runner will recheck the live balance and reserve ${plan.allocation_sol} SOL in this generated main wallet. Browser closure stops signing. Creating the session grants no signing authority; a strategy-bound allowance and separate activation are still required, and live activation remains blocked in this build.`}</p></>}
        </section>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="modal-actions"><button disabled={busy} onClick={step === 0 ? close : () => { setError(''); setStep((current) => current - 1) }}>{step === 0 ? 'Cancel' : 'Back'}</button>{step < 3 ? <button className="primary" onClick={next}>Continue</button> : <button className="primary" disabled={busy || !validIdentity || !validLimits} onClick={() => void create()}>{busy ? 'Creating…' : plan.session_mode === 'monitoring' ? 'Create monitoring session' : plan.execution_style === 'interactive' ? 'Create interactive session' : plan.automation_scope === 'entries-only' ? 'Create automatic-entries session' : plan.automation_scope === 'exits-only' ? 'Create automatic-exits session' : 'Create full Autopilot session'}</button>}</div>
    </section>
  </div>
}
