import { useEffect, useMemo, useState } from 'react'
import { api, errorText, formatSol } from './browserWallet'
import type { WalletSession } from './browserWallet'
import type { MarketplaceStrategy } from './StrategySelection'
import type { StrategyGoalId } from './marketIntelligence'
import './session-plan.css'
import { AutomationWalletSetup } from './AutomationWalletSetup'

export type RiskTolerance = 'conservative' | 'balanced' | 'aggressive'
export type InvestmentStyle = 'small' | 'standard' | 'concentrated'
export type TimeHorizon = 'intraday' | 'swing' | 'long-term'
export type SessionMode = 'monitoring' | 'trade'
export type SessionExecutionStyle = 'interactive' | 'autopilot'
export type AutomationScope = 'entries-only' | 'exits-only' | 'entries-and-exits'
export type AutopilotCustody = 'main-wallet-browser' | 'main-wallet-service'
export type ProfitManagement = 'user-directed' | 'strategy-managed' | 'winner-handoff'

export type SessionPermissions = {
  prepare_orders: boolean
  automatic_entries: boolean
  automatic_protective_exits: boolean
  automatic_profit_exits: boolean
  winner_handoff: boolean
  user_signature_each_order: boolean
  automation_wallet_required: boolean
  generated_wallet_required: boolean
  activation_required: boolean
  custody: 'none' | 'connected-wallet' | AutopilotCustody
  service_vault_required?: boolean
  continues_without_browser?: boolean
}

export type SessionPreferences = {
  risk_tolerance: RiskTolerance
  reward_risk_ratio: number
  investment_style: InvestmentStyle
  time_horizon: TimeHorizon
}

export type SessionPlan = SessionPreferences & {
  workspace_id?: string
  strategy_setup?: 'catalog'
  monitor_activity?: 'observations' | 'paper'
  session_mode: SessionMode
  execution_style: SessionExecutionStyle | null
  automation_scope: AutomationScope | null
  custody_mode: AutopilotCustody | null
  profit_management: ProfitManagement | null
  permissions: SessionPermissions
  allocation_sol: string
  buys_enabled: boolean
  sells_enabled: true
  min_trade_sol: string
  max_trade_sol: string
  max_daily_buy_sol: string
  max_trades_per_day: number
  max_daily_loss_sol: string
  max_daily_loss_percent: string
  max_trade_loss_percent: string
  max_open_positions: number
  recommended_goal: StrategyGoalId
  recommended_families: string[]
  recommendation: string
}

type FundingOverview = {
  balance_lamports: string
  allocated_lamports: string
  unallocated_lamports: string
}

type WizardStep = 'mode' | 'monitoring-confirm' | 'execution' | 'funding'

function sol(lamports: string) {
  try { return Number(BigInt(lamports)) / 1_000_000_000 } catch { return 0 }
}

function exactSol(value: number) {
  return Math.max(.000001, value).toFixed(9).replace(/0+$/, '').replace(/\.$/, '')
}

function permissions(mode: SessionMode, execution: SessionExecutionStyle, scope: AutomationScope | null, custody: AutopilotCustody): SessionPermissions {
  if (mode === 'monitoring') return {
    prepare_orders: false,
    automatic_entries: false,
    automatic_protective_exits: false,
    automatic_profit_exits: false,
    winner_handoff: false,
    user_signature_each_order: false,
    automation_wallet_required: false,
    generated_wallet_required: false,
    activation_required: false,
    custody: 'none',
  }
  if (execution === 'interactive') return {
    prepare_orders: true,
    automatic_entries: false,
    automatic_protective_exits: false,
    automatic_profit_exits: false,
    winner_handoff: false,
    user_signature_each_order: true,
    automation_wallet_required: false,
    generated_wallet_required: false,
    activation_required: true,
    custody: 'connected-wallet',
  }
  return {
    prepare_orders: true,
    automatic_entries: scope === 'entries-only' || scope === 'entries-and-exits',
    automatic_protective_exits: scope !== 'entries-only',
    automatic_profit_exits: scope !== 'entries-only',
    winner_handoff: false,
    user_signature_each_order: false,
    automation_wallet_required: false,
    generated_wallet_required: true,
    activation_required: true,
    custody,
    ...(custody === 'main-wallet-service' ? { service_vault_required: true, continues_without_browser: true } : {}),
  }
}

function monitoringSessionPlan(recommendedFamilies: string[], workspaceId?: string): SessionPlan {
  return {
    ...(workspaceId ? { workspace_id: workspaceId } : {}),
    session_mode: 'monitoring',
    execution_style: null,
    automation_scope: null,
    custody_mode: null,
    profit_management: null,
    permissions: permissions('monitoring', 'interactive', null, 'main-wallet-browser'),
    allocation_sol: '0',
    risk_tolerance: 'balanced',
    reward_risk_ratio: 2,
    investment_style: 'small',
    time_horizon: 'swing',
    buys_enabled: false,
    sells_enabled: true,
    min_trade_sol: '0',
    max_trade_sol: '0',
    max_daily_buy_sol: '0',
    max_trades_per_day: 0,
    max_daily_loss_sol: '0',
    max_daily_loss_percent: '0',
    max_trade_loss_percent: '0',
    max_open_positions: 0,
    recommended_goal: 'custom',
    recommended_families: recommendedFamilies,
    recommendation: 'Add one or more exact strategies to this monitoring session. Each strategy keeps its own immutable rules and evidence.',
  }
}

export function SessionPlanDialog({ wallet, simulations, close, confirmTrading, confirmMonitoring, selectAutomationWallet, sourceUnlocked, refreshWallet }: {
  wallet: WalletSession | null
  selectAutomationWallet: (wallet: WalletSession) => Promise<void>
  sourceUnlocked: (wallet: WalletSession) => void
  refreshWallet: () => Promise<void>
  simulations: MarketplaceStrategy[]
  close: () => void
  confirmTrading: (plan: SessionPlan) => Promise<void>
  confirmMonitoring: (plan: SessionPlan) => Promise<void>
}) {
  const [step, setStep] = useState(0)
  const [walletSetup, setWalletSetup] = useState(false)
  const [mode, setMode] = useState<SessionMode>('monitoring')
  const [monitorActivity, setMonitorActivity] = useState<'observations' | 'paper'>('observations')
  const [execution, setExecution] = useState<SessionExecutionStyle>('interactive')
  const [automationScope, setAutomationScope] = useState<AutomationScope | null>(null)
  const custody: AutopilotCustody = 'main-wallet-service'
  const [overview, setOverview] = useState<FundingOverview | null>(null)
  const [allocation, setAllocation] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(Boolean(wallet))
  const [creating, setCreating] = useState(false)

  const walletAddress = wallet?.address
  const walletNetwork = wallet?.network
  useEffect(() => {
    let current = true
    setOverview(null)
    setAllocation('')
    setError('')
    if (!walletAddress || !walletNetwork) {
      setLoading(false)
      return
    }
    setLoading(true)
    const query = new URLSearchParams({ sender: walletAddress, network: walletNetwork })
    api<FundingOverview>(`/api/v1/strategy-controls?${query}`).then((result) => {
      if (!current) return
      setOverview(result)
      const available = sol(result.unallocated_lamports)
      setAllocation(exactSol(Math.min(.2, available * .25)))
    }).catch((reason) => { if (current) setError(errorText(reason)) }).finally(() => { if (current) setLoading(false) })
    return () => { current = false }
  }, [walletAddress, walletNetwork])

  const steps = useMemo<WizardStep[]>(() => mode === 'monitoring'
    ? ['mode', 'monitoring-confirm']
    : ['mode', 'execution', 'funding'], [mode])
  const currentStep = steps[step] || steps[steps.length - 1]
  const available = overview ? sol(overview.unallocated_lamports) : 0
  const allocationNumber = Number(allocation)
  const validAllocation = Number.isFinite(allocationNumber) && allocationNumber >= .000001 && allocationNumber <= available
  const monitoringFamilies = useMemo(() => Array.from(new Set(
    simulations.map((item) => item.family).filter((family): family is string => Boolean(family)),
  )).sort(), [simulations])
  const permissionManifest = useMemo(() => permissions(mode, execution, automationScope, custody), [automationScope, custody, execution, mode])
  const plan = useMemo<SessionPlan>(() => {
    const funded = mode === 'trade'
    if (!funded) return monitoringSessionPlan(monitoringFamilies)
    const allocated = funded ? allocationNumber || .000001 : 0
    return {
      strategy_setup: 'catalog',
      session_mode: mode,
      execution_style: funded ? execution : null,
      automation_scope: funded && execution === 'autopilot' ? automationScope : null,
      custody_mode: funded && execution === 'autopilot' ? custody : null,
      profit_management: funded ? execution === 'interactive' || automationScope === 'entries-only' ? 'user-directed' : 'strategy-managed' : null,
      permissions: permissionManifest,
      allocation_sol: funded ? exactSol(allocated) : '0',
      // Retained compatibility fields are not presented as user preferences.
      risk_tolerance: 'balanced',
      reward_risk_ratio: 2,
      investment_style: 'small',
      time_horizon: 'swing',
      buys_enabled: funded && execution === 'autopilot' ? true : false,
      sells_enabled: true,
      min_trade_sol: funded ? exactSol(allocated * .025) : '0',
      max_trade_sol: funded ? exactSol(allocated * .10) : '0',
      max_daily_buy_sol: funded ? exactSol(allocated * .35) : '0',
      max_trades_per_day: 8,
      max_daily_loss_sol: funded ? exactSol(allocated * .10) : '0',
      max_daily_loss_percent: '10',
      max_trade_loss_percent: '15',
      max_open_positions: 5,
      recommended_goal: 'custom',
      recommended_families: monitoringFamilies,
      recommendation: 'Choose a saved strategy by its rules and evidence. Adding it does not start trading.',
    }
  }, [allocationNumber, automationScope, custody, execution, mode, monitoringFamilies, permissionManifest])

  const createMonitoringSession = async () => {
    if (creating) return
    setCreating(true)
    setError('')
    try {
      await confirmMonitoring({ ...plan, monitor_activity: monitorActivity })
    } catch (reason) {
      setError(errorText(reason))
      setCreating(false)
    }
  }

  const createTradingSession = async () => {
    if (creating || loading) return
    if (!validAllocation) {
      setError('Enter an allocation within your available balance.')
      return
    }
    setCreating(true)
    setError('')
    try {
      await confirmTrading(plan)
    } catch (reason) {
      setError(errorText(reason))
      setCreating(false)
    }
  }

  const chooseMode = (nextMode: SessionMode) => {
    if (nextMode === 'trade' && !wallet) {
      setError('Connect a wallet only when you are ready to configure wallet-backed trading.')
      return
    }
    setError('')
    setMode(nextMode)
    if (nextMode === 'monitoring') {
      setExecution('interactive')
      setAutomationScope(null)
    }
  }
  const chooseExecution = (nextExecution: SessionExecutionStyle, nextScope: AutomationScope | null = null) => {
    if (nextExecution === 'autopilot' && wallet?.kind !== 'local') {
      setError('Autopilot requires a PumpWizard Wallet. Extension wallets keep interactive signing only.')
      return
    }
    setError('')
    setExecution(nextExecution)
    setAutomationScope(nextExecution === 'autopilot' ? nextScope : null)
  }
  const next = () => {
    setError('')
    if (currentStep === 'funding' && !validAllocation) {
      setError(`Choose an allocation between 0.000001 SOL and the available ${available.toLocaleString(undefined, { maximumFractionDigits: 9 })} SOL.`)
      return
    }
    setStep((current) => Math.min(steps.length - 1, current + 1))
  }
  const title = currentStep === 'mode' ? 'Choose what this session may do'
    : currentStep === 'monitoring-confirm' ? 'Confirm monitoring session'
      : currentStep === 'execution' ? 'Choose how trading decisions are approved'
      : 'Allocate from your main balance'

  const chooseInitialMode = (nextMode: SessionMode) => {
    if (nextMode === 'trade' && !wallet) {
      setError('Connect a wallet only when you are ready to configure wallet-backed trading.')
      return
    }
    chooseMode(nextMode)
    setStep(1)
  }

  if (walletSetup && wallet) return <AutomationWalletSetup source={wallet} close={() => setWalletSetup(false)} sourceUnlocked={sourceUnlocked} refresh={refreshWallet} selectWallet={async (selected) => { await selectAutomationWallet(selected); setWalletSetup(false); setError('') }} />

  if (currentStep === 'mode') return <div className="modal-backdrop detail-layer" role="presentation" onMouseDown={close}>
    <section className="strategy-modal session-mode-picker session-mode-picker-expanded" role="dialog" aria-modal="true" aria-label="Create session" onMouseDown={(event) => event.stopPropagation()}>
      <button className="close" aria-label="Close dialog" onClick={close}>×</button>
      <div className="session-mode-step session-answer-options">
        <button onClick={() => chooseInitialMode('monitoring')}>
          <span className="session-mode-heading"><i aria-hidden="true">◎</i><span><small>LEARN FIRST</small><b>Monitor</b></span></span>
          <p>Choose observations or paper trading, then add your strategies.</p>
          <ul><li>Watches matching market candidates</li><li>Saves observations and every decision</li><li>Scores later 5m, 30m, and 4h outcomes</li><li>Builds evidence for the next session</li></ul>
          <span className="session-mode-facts"><span><small>WALLET</small><b>No allocation</b></span><span><small>ORDERS</small><b>None</b></span><span><small>START</small><b>You start monitor</b></span></span>
          <em>Research only</em>
        </button>
          <button onClick={() => chooseInitialMode('trade')} disabled={!wallet}>
          <span className="session-mode-heading"><i aria-hidden="true">↗</i><span><small>ACT WITH LIMITS</small><b>Trading session</b></span></span>
          <p>Use an exact strategy with a bounded SOL budget and explicit execution permissions.</p>
          <ul><li>Interactive: you sign every order</li><li>Autopilot: bounded use of this generated main wallet</li><li>Strict trade, loss, and position limits</li><li>Browser lock, refresh, or disconnect stops signing</li></ul>
          <span className="session-mode-facts"><span><small>WALLET</small><b>SOL reserved</b></span><span><small>ORDERS</small><b>Mode dependent</b></span><span><small>START</small><b>Disabled first</b></span></span>
          <em>{wallet ? 'Starts disabled' : 'Connect wallet to configure'}</em>
        </button>
      </div>
    </section>
  </div>

  if (currentStep === 'monitoring-confirm') return <div className="modal-backdrop detail-layer" role="presentation" onMouseDown={close}>
    <section className="strategy-modal session-mode-picker monitoring-confirm-picker" role="dialog" aria-modal="true" aria-label="Confirm monitoring session" onMouseDown={(event) => event.stopPropagation()}>
      <button className="close" aria-label="Close dialog" onClick={close}>×</button>
      <article className="monitoring-confirm-card">
        <header>
          <span className="session-mode-heading"><i aria-hidden="true">◎</i><span><small>RESEARCH ONLY</small><h2>Confirm monitoring session</h2></span></span>
          <p>Watch an exact strategy, retain what it sees and review its decisions before putting wallet funds at risk.</p>
        </header>
        <div className="monitor-activity-options" role="group" aria-label="Monitor activity">
          <button aria-pressed={monitorActivity === 'observations'} onClick={() => setMonitorActivity('observations')}><b>Observations</b><span>Watch matching tokens, save decisions and inspect later outcomes.</span></button>
          <button aria-pressed={monitorActivity === 'paper'} onClick={() => setMonitorActivity('paper')}><b>Paper trading</b><span>Test your strategy with simulated capital and track modeled results.</span></button>
        </div>
        <div className="monitoring-confirm-facts"><span><small>WALLET</small><b>No allocation</b><em>No reservation</em></span><span><small>ORDERS</small><b>Disabled</b><em>No signing path</em></span><span><small>START</small><b>Manual</b><em>You start monitoring</em></span><span><small>STRATEGIES</small><b>Add after creation</b><em>Exact rules per strategy</em></span></div>
        <aside><b>Nothing starts from this confirmation.</b><p>Confirmation creates the inactive session immediately in Active Sessions. From that session you can add exact strategies; Choose strategies next, then explicitly start monitoring or a paper run.</p></aside>
        {error && <p className="error" role="alert">{error}</p>}
        <footer><button disabled={creating} onClick={() => { setError(''); setStep(0) }}>Back</button><button className="primary" disabled={creating} onClick={() => void createMonitoringSession()}>{creating ? 'Creating…' : 'Confirm monitoring session'}</button></footer>
      </article>
    </section>
  </div>

  return <div className="modal-backdrop detail-layer" role="presentation" onMouseDown={close}>
    <section className="strategy-modal session-plan-wizard" role="dialog" aria-modal="true" aria-label="Create session" onMouseDown={(event) => event.stopPropagation()}>
      <button className="close" aria-label="Close dialog" onClick={close}>×</button>
      <small>NEW SESSION · STEP {step + 1} OF {steps.length}</small>
      <h2>{title}</h2>
      <p>{mode === 'monitoring' ? 'Monitoring retains observations and decisions without reserving wallet funds.' : 'Create the session with a balance, then add strategies from your library.'}</p>
      <div className="session-brief-progress" aria-hidden="true">{steps.map((item, index) => <i className={index <= step ? 'complete' : ''} key={item} />)}</div>

      {currentStep === 'execution' && <div className="session-execution-step">
        {wallet?.kind === 'extension' && <section className="automation-wallet-notice"><h3>Want automatic buys or sells?</h3><p>{wallet.name} requires your approval for each trade. Choose or create a PumpWizard wallet for automatic signing, and optionally transfer SOL with your current wallet’s approval.</p><button className="primary" onClick={() => setWalletSetup(true)}>Set up automation wallet</button><small>Setup does not enable live automation. Activation and spending limits remain separate.</small></section>}
        <div className="session-answer-options execution-options">
          <button className={execution === 'interactive' ? 'selected' : ''} aria-pressed={execution === 'interactive'} onClick={() => chooseExecution('interactive')}><b>Interactive</b><span>The background service monitors your strategy. You review and approve every buy and sell with your wallet.</span><em>You approve every order</em></button>
          <button className={execution === 'autopilot' && automationScope === 'entries-only' ? 'selected' : ''} aria-pressed={execution === 'autopilot' && automationScope === 'entries-only'} disabled={wallet?.kind !== 'local'} onClick={() => chooseExecution('autopilot', 'entries-only')}><b>Automatic entries only</b><span>{wallet?.kind === 'local' ? 'Your strategy buys within your approved limits. You decide when to sell and approve every exit.' : 'Phantom and other extension wallets require your approval for every order in this app.'}</span><em>{wallet?.kind === 'local' ? 'Strategy buys · you sell' : 'Generated wallet required'}</em></button>
          <button className={execution === 'autopilot' && automationScope === 'entries-and-exits' ? 'selected' : ''} aria-pressed={execution === 'autopilot' && automationScope === 'entries-and-exits'} disabled={wallet?.kind !== 'local'} onClick={() => chooseExecution('autopilot', 'entries-and-exits')}><b>Full Autopilot</b><span>{wallet?.kind === 'local' ? 'The background service buys and sells within your strategy and approved limits, even when the dashboard is closed.' : 'Phantom and other extension wallets require your approval for every order in this app.'}</span><em>{wallet?.kind === 'local' ? 'Entries and exits' : 'Generated wallet required'}</em></button>
        </div>
        <section className="profit-management-choice"><small>RUNS IN THE BACKGROUND SERVICE</small>
          <p>{execution === 'interactive' ? 'Monitoring continues when you close the dashboard. Open it to review and approve trades.' : automationScope === 'entries-only' ? 'Only buys can be automatic. You manage every exit: stop-loss, profit targets, trailing exits and maximum-hold sells will not run automatically.' : 'The service handles strategy entries and exits after you authorize and start it.'}</p>
          <p className="session-plan-note">{execution === 'interactive' ? 'Your wallet stays under your control. No trade is signed without your approval.' : 'Requires a PumpWizard wallet enrolled with the service, spending limits and a separate Start. Closing the dashboard does not stop an authorized session.'} The computer and service must stay running.</p>
        </section>
      </div>}

      {currentStep === 'funding' && <div className="session-funding-step">
        {overview && wallet && <div className="allocation-summary"><div><span>Main wallet</span><b>{formatSol(overview.balance_lamports)}</b><code>{wallet.address}</code></div><div><span>Already assigned</span><b>{formatSol(overview.allocated_lamports)}</b><small>durable session reservations</small></div><div><span>Available</span><b>{formatSol(overview.unallocated_lamports)}</b><small>maximum new allocation</small></div></div>}
        <label>Session allocation (SOL)<input aria-label="Session allocation (SOL)" inputMode="decimal" value={allocation} onChange={(event) => setAllocation(event.target.value)} disabled={loading || !overview} /></label>
        <div className="allocation-presets" aria-label="Allocation presets">{[.1, .25, .5].map((fraction) => <button key={fraction} onClick={() => setAllocation(exactSol(available * fraction))}>{fraction * 100}% available</button>)}</div>
        <p className="session-plan-note">This amount is assigned to the session and stays in your wallet. No trade starts. Add strategies after creating the session.</p>
      </div>}

      {error && <p className="error" role="alert">{error}</p>}
      <div className="modal-actions"><button disabled={creating} onClick={step === 0 ? close : () => { setError(''); setStep((current) => current - 1) }}>{step === 0 ? 'Cancel' : 'Back'}</button>{step < steps.length - 1 ? <button className="primary" disabled={creating || (currentStep === 'funding' && loading)} onClick={next}>Continue</button> : <button className="primary" disabled={creating || loading || !validAllocation} onClick={() => void createTradingSession()}>{creating ? 'Creating session…' : 'Create session'}</button>}</div>
    </section>
  </div>
}
