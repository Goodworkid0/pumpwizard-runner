import { serverStorage } from './serverStorage'
import type { AllocationSizing, SizingRequest } from './SavedStrategyEditor'
import { WalletWorkspaceScope } from './walletWorkspaceScope'
import { useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ChangeEvent } from 'react'
import './desktop.css'
import './index.css'
import './unified-dashboard.css'
import './market-selection.css'
import { DepositDialog } from './DepositDialog'
import { StrategyControls } from './StrategyControls'
import { useDialogFocus } from './useDialogFocus'
import { RpcConnection } from './RpcConnection'
import { WithdrawDialog } from './WithdrawDialog'
import { StrategyOrders } from './StrategyOrders'
import type { StrategyOrder } from './WalletTrading'
import { WalletTrading } from './WalletTrading'
import { WelcomeFlow } from './WelcomeFlow'
import type { RunnerReleaseNote } from './WelcomeFlow'
import { PortfolioWorkspace } from './PortfolioWorkspace'
import type { PortfolioSellTarget, PortfolioSession, SessionWorkspace } from './PortfolioWorkspace'
import { SessionFundsDialog } from './SessionFundsDialog'
import { SessionMonitor } from './SessionMonitor'
import type { StrategySessionDetail } from './SessionMonitor'
import { SessionReview } from './SessionReview'
import type { SessionReviewEvidence, SessionReviewReport } from './SessionReview'
import { MainWalletAllowanceDialog } from './MainWalletAllowanceDialog'
import { MainWalletAutopilotBrowserRuntime } from './mainWalletAutopilot'
import type { MainWalletRuntimeState } from './mainWalletAutopilot'
import { SessionAgentTerminal } from './SessionAgentTerminal'
import { ServiceControlDialog } from './ServiceControlDialog'
import { useServiceControl } from './serviceControl'
import { SessionBriefDialog } from './SessionBriefDialog'
import type { SessionBriefInput } from './SessionBriefDialog'
import { SessionPlanDialog } from './SessionPlanDialog'
import type { SessionPlan } from './SessionPlanDialog'
import { StrategySettings } from './StrategySettings'
import type { EditableStrategySettings } from './StrategySettings'
import { useTradeRecords } from './useTradeRecords'
import { useWalletPortfolio } from './useWalletPortfolio'
import { WalletSummary } from './WalletSummary'
import { useWalletBalanceHistory } from './useWalletBalanceHistory'
import { PortfolioDashboard, WalletOverview } from './PortfolioDashboard'
import { usePortfolioHistory } from './usePortfolioHistory'
import { SessionSellAll } from './SessionSellAll'
import { TradingSidebar } from './TradingSidebar'
import { OpenPositionsTerminal } from './OpenPositionsTerminal'
import type { TradingDashboardTab, TradingFocus } from './OpenPositionsTerminal'
import { TokenDetail } from './TokenDetail'
import './token-detail.css'
import type { CoinDetailData, MarketRow } from './TokenDetail'
import { forgetWalletSession, rememberWalletSession, restoreWalletSession, revokeWallet } from './browserWallet'
import type { MainWalletAutopilotRuntime, TradingMarket, WalletSession } from './browserWallet'
import { WalletOnboarding } from './WalletOnboarding'
import { WalletSessionUnlock } from './WalletSessionUnlock'
import { WorkspaceHeader } from './WorkspaceHeader'
import type { WorkspaceDestination } from './WorkspaceHeader'
import { ResearchHome } from './ResearchHome'
import { LocalStrategyLibrary } from './StrategySelection'
import type { LocalStrategy, MarketplaceStrategy } from './StrategySelection'
import { analyzeMarket, STRATEGY_GOALS, strategyGoal } from './marketIntelligence'
import type { StrategyGoalId } from './marketIntelligence'

type ApiEnvelope<T> = { schema: string; version: string } & T
type Session = PortfolioSession
type SessionDetail = StrategySessionDetail
type Strategy = LocalStrategy
type StrategyDetail = {
  strategy: Strategy
  identity: { package_sha256: string; rules_sha256: string; package_schema: string; decision_engine: string; profile: string }
  capability: string
  support: string
}
type Market = MarketRow
type AssistantProfile = { id: string; label: string; endpoint: string; model: string; key_available: boolean; key_persisted: boolean }
type AssistantOverview = { configured: boolean; profile: AssistantProfile | null; conversations: { id: string; title: string; updated: number }[] }
type PendingSigningAction =
  | { kind: 'trade'; mint: string; positionId: string; quantity: string; strategy: StrategyOrder | null }
  | { kind: 'withdraw'; recipient: string }
const MARKET_LOCAL_REFRESH_MS = 5_000
const WELCOME_VERSION_STORAGE_KEY = 'pumpwizard.welcome-acknowledged-version/v1'
const RESEARCH_MODE_STORAGE_KEY = 'pumpwizard.research-mode/v1'

type FirstUseCapabilities = {
  schema: 'pumpwizard.first-use-capabilities/v1'
  authentication: { state: string; wallet_identity: boolean }
  research: { state: string; wallet_required: boolean; ai_provider_required: boolean }
  wallet_actions: { state: string }
  production_execution: { state: string; automatic_execution: boolean }
  read_effects: Record<string, boolean>
}

function workspaceMonitoringPlan(recommendedFamilies: string[], workspaceId: string): SessionPlan {
  return {
    workspace_id: workspaceId,
    session_mode: 'monitoring',
    execution_style: null,
    automation_scope: null,
    custody_mode: null,
    profit_management: null,
    permissions: {
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
    },
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

async function readApi<T>(path: string): Promise<ApiEnvelope<T>> {
  const response = await fetch(path)
  if (!response.ok) throw new Error(`Local API returned ${response.status}`)
  return response.json() as Promise<ApiEnvelope<T>>
}

async function sendApi<T>(path: string, body: object): Promise<ApiEnvelope<T>> {
  const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { error?: unknown } | null
    throw new Error(typeof payload?.error === 'string' ? payload.error : `Local API returned ${response.status}`)
  }
  return response.json() as Promise<ApiEnvelope<T>>
}

const assistantProviders = {
  openai: { label: 'OpenAI API', endpoint: 'https://api.openai.com/v1/chat/completions', model: 'gpt-5.6-terra', models: ['gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-luna'], note: 'OpenAI Chat Completions' },
  anthropic: { label: 'Anthropic Claude', endpoint: 'https://api.anthropic.com/v1/messages', model: 'claude-sonnet-5', models: ['claude-sonnet-5', 'claude-opus-5-5', 'claude-haiku-4-5-20251001'], note: 'Native Anthropic Messages API' },
  deepseek: { label: 'DeepSeek API', endpoint: 'https://api.deepseek.com/chat/completions', model: 'deepseek-flash', models: ['deepseek-flash', 'deepseek-v4-pro'], note: 'Official DeepSeek Chat Completions API' },
  ollama: { label: 'Local Ollama', endpoint: 'http://127.0.0.1:11434/v1/chat/completions', model: 'gpt-oss-20b', models: ['gpt-oss-20b', 'gpt-oss-120b'], note: 'Local OpenAI-compatible server' },
  compatible: { label: 'Compatible Chat API', endpoint: null, model: '', models: [], note: 'Custom OpenAI-compatible endpoint' },
} as const

type AssistantProvider = keyof typeof assistantProviders

function AssistantSetupDialog({ profile, close, saved }: { profile: AssistantProfile | null; close: () => void; saved: () => void }) {
  const initialProvider = (Object.entries(assistantProviders).find(([, item]) => item.endpoint === profile?.endpoint)?.[0] || 'compatible') as AssistantProvider
  const [provider, setProvider] = useState<AssistantProvider>(profile ? initialProvider : 'openai')
  const [model, setModel] = useState(profile?.model || assistantProviders.openai.model)
  const [apiKey, setApiKey] = useState('')
  const [endpoint, setEndpoint] = useState(initialProvider === 'compatible' ? profile?.endpoint || '' : '')
  const [error, setError] = useState('')
  const submit = () => sendApi('/api/v1/assistant/setup', { provider, model, api_key: apiKey, endpoint })
    .then(() => { saved(); close() })
    .catch(() => setError('The local AI connection was rejected. Check the provider, endpoint, model, and API key.'))
  const preset = assistantProviders[provider]
  const chooseProvider = (next: AssistantProvider) => {
    setProvider(next)
    setModel(assistantProviders[next].model)
    setError('')
  }
  const reusesKey = Boolean(profile && (preset.endpoint || endpoint.trim()) === profile.endpoint)
  return <div className="modal-backdrop" role="presentation"><section className="strategy-modal" role="dialog" aria-modal="true" aria-label="Connect AI provider"><button className="close" aria-label="Close dialog" onClick={close}>×</button><small>LOCAL AI CONNECTION</small><h2>Connect optional AI explanations</h2><p>Your key goes only to the local Runner process. It is session-only in this browser app and is never sent back to the browser, wallet, or strategy library.</p><div className="setup-fields"><label>Provider<select value={provider} onChange={(event) => chooseProvider(event.target.value as AssistantProvider)}>{Object.entries(assistantProviders).map(([value, item]) => <option value={value} key={value}>{item.label}</option>)}</select></label><label>Model<input list="assistant-models" value={model} onChange={(event) => setModel(event.target.value)} /><datalist id="assistant-models">{preset.models.map((item) => <option value={item} key={item} />)}</datalist><small>{preset.note}. You can enter another model ID supported by your account.</small></label>{provider === 'compatible' && <label>HTTPS endpoint<input value={endpoint} onChange={(event) => setEndpoint(event.target.value)} placeholder="https://…/v1/chat/completions" /></label>}<label>API key{provider === 'ollama' ? ' (optional)' : reusesKey ? ' (leave blank to keep current)' : ''}<input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="off" /></label></div>{error && <p className="error">{error}</p>}<div className="modal-actions"><button onClick={close}>Cancel</button><button className="primary" onClick={submit}>Save local connection</button></div></section></div>
}

function DraftDialog({ market, marketplace, initialGoal, close, saved }: { market: Market[]; marketplace: MarketplaceStrategy[]; initialGoal?: StrategyGoalId | null; close: () => void; saved: () => void }) {
  const strategyScope = useContext(WalletWorkspaceScope)
  const pulse = useMemo(() => analyzeMarket(market, marketplace), [market, marketplace])
  const initialProfile = strategyGoal(initialGoal || pulse.recommendedGoal)
  const [goal, setGoal] = useState<StrategyGoalId>(initialProfile.id)
  const [label, setLabel] = useState(initialProfile.draftLabel)
  const [identifier, setIdentifier] = useState(initialProfile.identifier)
  const [entryStyle, setEntryStyle] = useState(initialProfile.entryStyle)
  const [minimum, setMinimum] = useState(String(initialProfile.minimumCap))
  const [maximum, setMaximum] = useState(String(initialProfile.maximumCap))
  const [order, setOrder] = useState('10')
  const [error, setError] = useState('')
  const selectedGoal = strategyGoal(goal)
  const fit = pulse.fits.find((item) => item.id === goal)
  const recommended = strategyGoal(pulse.recommendedGoal)
  const chooseGoal = (next: StrategyGoalId) => {
    const profile = strategyGoal(next)
    setGoal(next)
    setLabel(profile.draftLabel)
    setIdentifier(profile.identifier)
    setEntryStyle(profile.entryStyle)
    setMinimum(String(profile.minimumCap))
    setMaximum(String(profile.maximumCap))
    setError('')
  }
  const submit = () => sendApi(`/api/v1/strategies/draft${strategyScope}`, {
    label, identifier, strategy_goal: goal, entry_style: entryStyle, entry_cap_min: Number(minimum), entry_cap_max: Number(maximum), order_usd: Number(order), budget_usd: Math.max(500, Number(order) * 20),
  }).then(() => { saved(); close() }).catch(() => setError('Check the identifier and values. This draft needs a supported entry style, market-cap range, and positive trade size.'))
  return <div className="modal-backdrop detail-layer" role="presentation"><section className="strategy-modal strategy-wizard" role="dialog" aria-modal="true" aria-label="Create research draft"><button className="close" aria-label="Close dialog" onClick={close}>×</button><small>GUIDED · LOCAL RESEARCH</small><h2>What kind of strategy do you want?</h2><p>Runner compares these goals with its current retained watchlist and published simulation summaries. The suggestion is a research starting point—not a return prediction or trading authorization.</p>
    <section className="wizard-recommendation"><span><small>CURRENT DATA SUGGESTION</small><b>{recommended.label}</b><p>{pulse.fits[0]?.reason}</p></span><em>Ordinal template fit · heuristic only</em></section>
    <div className="strategy-goal-grid" role="group" aria-label="Strategy goal">
      {STRATEGY_GOALS.map((item) => {
        const itemFit = pulse.fits.find((candidate) => candidate.id === item.id)
        return <button type="button" className={goal === item.id ? 'selected' : ''} aria-pressed={goal === item.id} onClick={() => chooseGoal(item.id)} key={item.id}><span><b>{item.label}</b>{item.id === pulse.recommendedGoal && <em>Highest ordinal fit</em>}</span><p>{item.description}</p><small>{itemFit ? 'Ordinal template fit · heuristic only' : 'Set every field yourself'}</small></button>
      })}
    </div>
    <section className="wizard-evidence"><div><small>WHY THIS FIT</small><b>{fit?.reason || 'Custom strategy selected.'}</b><p>{selectedGoal.boundary}</p></div><dl><div><dt>Fresh rows</dt><dd>{pulse.fresh}/{pulse.tracked}</dd></div><div><dt>History coverage</dt><dd>{pulse.covered}/{pulse.tracked}</dd></div><div><dt>Published results</dt><dd>{fit?.reportedSimulations ? `${fit.positiveSimulations}/${fit.reportedSimulations} positive` : 'Unavailable'}</dd></div></dl></section>
    <details className="wizard-settings" open><summary>Review and adjust the template</summary><div className="setup-fields draft-fields"><label>Strategy name<input value={label} onChange={(event) => setLabel(event.target.value)} /></label><label>Identifier<input value={identifier} onChange={(event) => setIdentifier(event.target.value)} /></label><label>Entry style<select value={entryStyle} onChange={(event) => setEntryStyle(event.target.value)}>{['momentum', 'breakout', 'pullback_reclaim', 'slow_burn', 'persistent', 'higher-low', 'multi-window', 'squeeze', 'retest', 'post-grad', 'liquidity', 'relative', 'second-wave', 'recovery'].map((style) => <option value={style} key={style}>{style}</option>)}</select></label><label>Minimum market cap (USD)<input inputMode="decimal" value={minimum} onChange={(event) => setMinimum(event.target.value)} /></label><label>Maximum market cap (USD)<input inputMode="decimal" value={maximum} onChange={(event) => setMaximum(event.target.value)} /></label><label>Trade size (USD)<input inputMode="decimal" value={order} onChange={(event) => setOrder(event.target.value)} /></label></div></details>
    {error && <p className="error">{error}</p>}<p className="wizard-safety">Creation stores an immutable paper-only strategy package. Monitoring, wallet allocation, order review, signing, and funded execution remain separate.</p><div className="modal-actions"><button onClick={close}>Cancel</button><button className="primary" onClick={submit}>Create local draft</button></div></section></div>
}

export default function App() {
  useDialogFocus()
  const [runnerVersion, setRunnerVersion] = useState('')
  const [sessionRows, setSessions] = useState<Session[]>([])
  const [workspaceRows, setSessionWorkspaces] = useState<SessionWorkspace[]>([])
  const [loadedScope, setLoadedScope] = useState('')
  const [market, setMarket] = useState<Market[]>([])
  const [strategies, setStrategies] = useState<Strategy[]>([])
  const [status, setStatus] = useState('Connecting to the local Runner API…')
  const [strategyOpen, setStrategyOpen] = useState(false)
  const [strategyDetail, setStrategyDetail] = useState<StrategyDetail | null>(null)
  const [fundsWorkspace, setFundsWorkspace] = useState<SessionWorkspace | null>(null)
  const [sessionPlanOpen, setSessionPlanOpen] = useState(false)
  const [sessionPlan, setSessionPlan] = useState<SessionPlan | null>(null)
  const [sessionDraftStrategy, setSessionDraftStrategy] = useState<Strategy | null>(null)
  const [draftOpen, setDraftOpen] = useState(false)
  const [draftInitialGoal, setDraftInitialGoal] = useState<StrategyGoalId | null>(null)
  const [selectedCoin, setSelectedCoin] = useState<CoinDetailData | null>(null)
  const [historyImporting, setHistoryImporting] = useState(false)
  const [sessionDetail, setSessionDetail] = useState<SessionDetail | null>(null)
  const [sessionDetailBusy, setSessionDetailBusy] = useState(false)
  const [sessionDetailError, setSessionDetailError] = useState('')
  const [sessionReview, setSessionReview] = useState<{
    sourceId: string
    initialTab: 'evidence' | 'paper'
    sessionLabel: string
    report: SessionReviewReport | null
    evidence: SessionReviewEvidence | null
  } | null>(null)
  const [sessionReviewBusy, setSessionReviewBusy] = useState(false)
  const [sessionReviewError, setSessionReviewError] = useState('')
  const [mainWalletAllowanceOpen, setMainWalletAllowanceOpen] = useState(false)
  const [serviceOpen, setServiceOpen] = useState(false)
  const [serviceTargetSessionId, setServiceTargetSessionId] = useState('')
  const [mainWalletRuntimeState, setMainWalletRuntimeState] = useState<(MainWalletRuntimeState & { allowanceId: string }) | null>(null)
  const [strategySettingsOpen, setStrategySettingsOpen] = useState(false)
  const [assistant, setAssistant] = useState<AssistantOverview | null>(null)
  const [assistantSetup, setAssistantSetup] = useState(false)
  const [walletOpen, setWalletOpen] = useState(false)
  const [withdrawRecipient, setWithdrawRecipient] = useState<string | null>(null)
  const [rpcOpen, setRpcOpen] = useState(false)
  const [rpcRevision, setRpcRevision] = useState(0)
  const [ordersOpen, setOrdersOpen] = useState(false)
  const [strategyOrder, setStrategyOrder] = useState<StrategyOrder | null>(null)
  const [dashboardTab, setDashboardTab] = useState<TradingDashboardTab>('overview')
  const [positionFocus, setPositionFocus] = useState<{ id: string; requestedAt: number } | null>(null)
  const [tradingFocus, setTradingFocus] = useState<{ scope: string; target: TradingFocus } | null>(null)
  const [tradeMint, setTradeMint] = useState('')
  const [tradePositionId, setTradePositionId] = useState('')
  const [tradeQuantity, setTradeQuantity] = useState('')
  const [depositOpen, setDepositOpen] = useState(false)
  const [browserWallet, setBrowserWallet] = useState<WalletSession | null>(null)
  const [researchOnly, setResearchOnly] = useState(false)
  const [sessionRestorePending, setSessionRestorePending] = useState(true)
  const [walletSessionUnlockOpen, setWalletSessionUnlockOpen] = useState(false)
  const [pendingSigningAction, setPendingSigningAction] = useState<PendingSigningAction | null>(null)
  const [tradingMarket, setTradingMarket] = useState<TradingMarket | null>(null)
  const [welcomeMode, setWelcomeMode] = useState<'introduction' | 'update' | null>(null)
  const [welcomeAfterSignIn, setWelcomeAfterSignIn] = useState(false)
  const [releaseNote, setReleaseNote] = useState<RunnerReleaseNote | null>(null)
  const [releaseNoteUrl, setReleaseNoteUrl] = useState('https://pumpwizard.fun/community')
  const [workspaceView, setWorkspaceView] = useState<WorkspaceDestination>('dashboard')
  const [capabilities, setCapabilities] = useState<FirstUseCapabilities | null>(null)
  const [capabilitiesError, setCapabilitiesError] = useState('')
  const [marketplace, setMarketplace] = useState<MarketplaceStrategy[]>([])
  const importInput = useRef<HTMLInputElement>(null)
  const historyImportActive = useRef(false)
  const sessionQuery = browserWallet ? `?${new URLSearchParams({ sender: browserWallet.address, network: browserWallet.network })}` : '?scope=research'
  const scopeRef = useRef(sessionQuery)
  scopeRef.current = sessionQuery
  const sessions = loadedScope === sessionQuery ? sessionRows : []
  const sessionWorkspaces = loadedScope === sessionQuery ? workspaceRows : []
  const scopedAssistant = loadedScope === sessionQuery ? assistant : null
  const scopedTradingFocus = tradingFocus?.scope === sessionQuery ? tradingFocus.target : null
  const sessionCollectionPath = `/api/v1/sessions${sessionQuery}`
  const sessionItemPath = (id: string) => `/api/v1/sessions/${encodeURIComponent(id)}${sessionQuery}`
  const [sellAllWorkspace, setSellAllWorkspace] = useState<SessionWorkspace | null>(null)
  const mainWalletRuntime = useRef<MainWalletAutopilotBrowserRuntime | null>(null)

  useEffect(() => {
    const failed = (event: Event) => setStatus(`Could not save app data: ${(event as CustomEvent<string>).detail}`)
    window.addEventListener('pumpwizard-storage-error', failed)
    return () => window.removeEventListener('pumpwizard-storage-error', failed)
  }, [])

  useEffect(() => {
    let active = true
    restoreWalletSession().then((restored) => {
      if (!active) return
      if (restored) {
        setBrowserWallet(restored.session)
        setWorkspaceView('dashboard')
        setTradingMarket(restored.market || 'pumpfun')
        if (!restored.market) rememberWalletSession(restored.session, 'pumpfun')
        setStatus('Restored this tab session. Wallet signing remains locked until you unlock it again.')
        return
      }
      try {
        if (serverStorage.getItem(RESEARCH_MODE_STORAGE_KEY) === 'enabled') {
          setResearchOnly(true)
          setWorkspaceView('research')
          setTradingMarket('pumpfun')
          setStatus('Reopened the authenticated read-only research workspace. No wallet action was started.')
        }
      } catch { /* A hardened browser can still choose Research on this visit. */ }
    }).finally(() => { if (active) setSessionRestorePending(false) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    let active = true
    readApi<{ capabilities: FirstUseCapabilities }>('/api/v1/capabilities').then((data) => {
      if (!active) return
      setCapabilities(data.capabilities)
      setCapabilitiesError('')
    }).catch((reason) => {
      if (!active) return
      setCapabilitiesError(reason instanceof Error ? reason.message : 'Authenticated capabilities are unavailable.')
    })
    return () => { active = false }
  }, [])

  useEffect(() => {
    let active = true
    readApi<{ note: RunnerReleaseNote; url: string }>('/api/v1/release-note').then((data) => {
      if (!active) return
      setRunnerVersion(data.version)
      setReleaseNote(data.note)
      setReleaseNoteUrl(data.url)
    }).catch(() => undefined)
    return () => { active = false }
  }, [])

  const loadMarket = useCallback(async () => {
    const data = await readApi<{ rows: Market[] }>('/api/v1/market?limit=100')
    setRunnerVersion(data.version)
    setMarket(data.rows)
    return data
  }, [])

  const refresh = useCallback(() => {
    setStatus('Refreshing local records…')
    Promise.all([readApi<{ rows: Session[] }>(sessionCollectionPath), readApi<{ rows: SessionWorkspace[] }>(`/api/v1/session-workspaces${sessionQuery}`), readApi<{ rows: Market[] }>('/api/v1/market?limit=100'), readApi<{ rows: Strategy[] }>(`/api/v1/strategies${sessionQuery}`), readApi<AssistantOverview>(`/api/v1/assistant${sessionQuery}`)])
      .then(([sessionData, workspaceData, marketData, strategyData, assistantData]) => {
        if (scopeRef.current !== sessionQuery) return
        setLoadedScope(sessionQuery)
        setRunnerVersion(marketData.version)
        setSessions(sessionData.rows)
        setSessionWorkspaces(workspaceData.rows)
        setMarket(marketData.rows)
        setStrategies(strategyData.rows)
        setAssistant(assistantData)
        setStatus('Market, strategy, and wallet records are ready.')
      })
      .catch(() => setStatus('Local API unavailable. Start Runner web-api with its browser token.'))
  }, [sessionCollectionPath, sessionQuery])

  useEffect(() => { refresh() }, [refresh])
  useEffect(() => {
    setSessionDetail(null)
    setSellAllWorkspace(null)
    setFundsWorkspace(null)
    setSessionReview(null)
    setSessionDraftStrategy(null)
    setStrategyOpen(false)
    setStrategySettingsOpen(false)
    setOrdersOpen(false)
    setWalletOpen(false)
    setStrategyOrder(null)
    setServiceTargetSessionId('')
    setPositionFocus(null)
    setTradingFocus(null)
    setDashboardTab('overview')
  }, [sessionQuery])

  const updateBrowserBalance = useCallback((balance: string) => {
    setBrowserWallet((current) => current ? { ...current, balance } : current)
  }, [])
  const { funds, trades: tradeState, refresh: refreshTradeState, error: receiptError, refreshing: refreshingReceipts } = useTradeRecords(browserWallet, updateBrowserBalance)
  const walletPortfolio = useWalletPortfolio(browserWallet, Boolean(tradingMarket), 30_000)
  const portfolioHistory = usePortfolioHistory(browserWallet, walletPortfolio.portfolio, tradeState, Boolean(walletPortfolio.error), Boolean(receiptError))
  const walletBalanceHistory = useWalletBalanceHistory(browserWallet, walletPortfolio.portfolio, Boolean(walletPortfolio.error))
  const serviceControl = useServiceControl(browserWallet && tradingMarket ? browserWallet : null)


  const enterResearch = () => {
    if (!capabilities || capabilities.research.state !== 'available' || capabilities.research.wallet_required) {
      setStatus(capabilitiesError || 'Authenticated wallet-optional research is unavailable in this Runner build.')
      return
    }
    setWorkspaceView('research')
    setTradingMarket('pumpfun')
    setResearchOnly(true)
    try { serverStorage.setItem(RESEARCH_MODE_STORAGE_KEY, 'enabled') } catch { /* The current read-only session still works. */ }
    setStatus('Opened read-only Research. No wallet scan, provider request, collection, signing or submission was started.')
  }

  const connectWallet = () => {
    setResearchOnly(false)
    try { serverStorage.removeItem(RESEARCH_MODE_STORAGE_KEY) } catch { /* Wallet setup can continue without persistence. */ }
  }

  const activateBrowserWallet = (session: WalletSession, market: TradingMarket = 'pumpfun') => {
    setWorkspaceView('dashboard')
    setDashboardTab('overview')
    setTradingMarket(market)
    setWelcomeAfterSignIn(true)
    setBrowserWallet(session)
    setResearchOnly(false)
    try { serverStorage.removeItem(RESEARCH_MODE_STORAGE_KEY) } catch { /* Wallet session remains active for this tab. */ }
    rememberWalletSession(session, market)
  }

  const selectAutomationWallet = async (session: WalletSession) => {
    if (session.kind !== 'local' || !browserWallet || browserWallet.kind !== 'extension' || session.network !== browserWallet.network || session.address === browserWallet.address) throw new Error('Choose a different generated wallet on the current network.')
    await revokeWallet(browserWallet)
    setBrowserWallet(session)
    rememberWalletSession(session, tradingMarket)
    setWelcomeAfterSignIn(false)
    setWelcomeMode(null)
    setSessionPlan(null)
    setStatus('Generated wallet selected. Continue session setup; automatic trading remains inactive.')
  }

  const connectedWalletKey = browserWallet
    ? `${browserWallet.kind}:${browserWallet.network}:${browserWallet.address}`
    : ''
  useEffect(() => {
    if (!connectedWalletKey) {
      setWelcomeMode(null)
      setWelcomeAfterSignIn(false)
      return
    }
    if (!runnerVersion) return
    let acknowledged: string | null = null
    try { acknowledged = serverStorage.getItem(WELCOME_VERSION_STORAGE_KEY) } catch { /* Storage can be unavailable in hardened browsers. */ }
    setWelcomeMode(acknowledged && acknowledged !== runnerVersion ? 'update' : welcomeAfterSignIn || !acknowledged ? 'introduction' : null)
  }, [connectedWalletKey, runnerVersion, welcomeAfterSignIn])

  const dismissWelcome = () => {
    try { serverStorage.setItem(WELCOME_VERSION_STORAGE_KEY, runnerVersion) } catch { /* Continue without persistence when storage is unavailable. */ }
    setWelcomeAfterSignIn(false)
    setWelcomeMode(null)
  }

  const lockBrowserWallet = useCallback(async () => {
    const current = browserWallet
    const runtime = mainWalletRuntime.current
    mainWalletRuntime.current = null
    if (runtime) await runtime.stop('Wallet locked or disconnected')
    setMainWalletRuntimeState(null)
    setDepositOpen(false)
    setTradeMint('')
    setTradePositionId('')
    setWorkspaceView('research')
    setDashboardTab('overview')
    setTradingMarket('pumpfun')
    setBrowserWallet(null)
    setResearchOnly(true)
    try { serverStorage.setItem(RESEARCH_MODE_STORAGE_KEY, 'enabled') } catch { /* Research remains available for this visit. */ }
    setWalletSessionUnlockOpen(false)
    setPendingSigningAction(null)
    setMainWalletAllowanceOpen(false)
    setServiceOpen(false)
    setServiceTargetSessionId('')
    setSessionReview(null)
    forgetWalletSession()
    if (current) await revokeWallet(current)
  }, [browserWallet])

  useEffect(() => browserWallet?.onInvalidated?.(() => { void lockBrowserWallet() }), [browserWallet, lockBrowserWallet])

  const startMainWalletAutopilot = async (runtime: MainWalletAutopilotRuntime) => {
    if (!browserWallet) throw new Error('Unlock the generated main wallet before starting Autopilot.')
    const previous = mainWalletRuntime.current
    if (previous) await previous.stop('Replaced by another Autopilot allowance')
    const active = new MainWalletAutopilotBrowserRuntime(browserWallet, runtime, (state) => {
      setMainWalletRuntimeState({ ...state, allowanceId: runtime.allowance_id })
      setStatus(state.detail)
      if (state.order_id) void refreshTradeState()
    })
    mainWalletRuntime.current = active
    setMainWalletRuntimeState({
      allowanceId: runtime.allowance_id,
      phase: 'checking',
      detail: 'Starting bounded browser signer',
      last_check: Date.now() / 1000,
    })
    active.start()
  }

  const pauseMainWalletAutopilot = async (allowanceId: string, paused: boolean) => {
    const active = mainWalletRuntime.current
    if (!active || active.allowanceId !== allowanceId) throw new Error('This Autopilot runtime is not active in the current tab.')
    await active.pauseEntries(paused)
  }

  const stopMainWalletAutopilot = async (allowanceId: string) => {
    const active = mainWalletRuntime.current
    if (!active || active.allowanceId !== allowanceId) return
    mainWalletRuntime.current = null
    await active.stop('Autopilot stopped by the user')
  }

  const backfillCoinHistory = useCallback(async (mint: string) => {
    if (historyImportActive.current) return
    historyImportActive.current = true
    setHistoryImporting(true)
    setStatus('Loading earlier public price history…')
    try {
      const data = await sendApi<{ job: CoinDetailData['history_backfill'] }>(`/api/v1/coins/${encodeURIComponent(mint)}/history/backfill`, {})
      const refreshed = await readApi<CoinDetailData>(`/api/v1/coins/${encodeURIComponent(mint)}`)
      setSelectedCoin((current) => current?.mint === mint ? refreshed : current)
      setStatus(data.job.row_count
        ? data.job.status === 'complete'
          ? `${data.job.row_count.toLocaleString()} historical candles loaded.`
          : `${data.job.row_count.toLocaleString()} historical candles loaded · older prices remain available.`
        : 'No earlier public price history is available for this token.')
    } catch {
      readApi<CoinDetailData>(`/api/v1/coins/${encodeURIComponent(mint)}`).then((data) => {
        setSelectedCoin((current) => current?.mint === mint ? data : current)
      }).catch(() => undefined)
      setStatus('Earlier public price history is temporarily unavailable. You can retry from the token page.')
    } finally {
      historyImportActive.current = false
      setHistoryImporting(false)
    }
  }, [])

  const openCoin = (row: Pick<Market, 'mint'>) => {
    readApi<CoinDetailData>(`/api/v1/coins/${encodeURIComponent(row.mint)}`).then((data) => {
      setSelectedCoin(data)
    }).catch(() => setStatus('Token details are unavailable for that local record.'))
  }

  useEffect(() => {
    if (!selectedCoin || historyImporting) return
    if (selectedCoin.history_backfill.status === 'not_started' || selectedCoin.history_backfill.status === 'ready') {
      void backfillCoinHistory(selectedCoin.mint)
    }
  }, [backfillCoinHistory, historyImporting, selectedCoin])
  const loadEarlierCoinHistory = useCallback(async () => {
    const current = selectedCoin
    if (!current || (current.next_before == null && current.next_candle_before == null)) return
    try {
      const params = new URLSearchParams({ limit: '500', candle_limit: '1000' })
      const localCursor = current.next_before ?? current.history[current.history.length - 1]?.observed_at
      const candleCursor = current.next_candle_before ?? current.candles[current.candles.length - 1]?.opened_at
      if (localCursor) params.set('before', String(localCursor))
      if (candleCursor) params.set('candle_before', String(candleCursor))
      const data = await readApi<CoinDetailData>(`/api/v1/coins/${encodeURIComponent(current.mint)}?${params}`)
      setSelectedCoin((latest) => {
        if (!latest || latest.mint !== data.mint) return latest
        const timestamps = new Set(latest.history.map((row) => row.observed_at))
        const candleKeys = new Set(latest.candles.map((row) => `${row.source}:${row.opened_at}`))
        return {
          ...latest,
          history: [...latest.history, ...data.history.filter((row) => !timestamps.has(row.observed_at))],
          candles: [...latest.candles, ...data.candles.filter((row) => !candleKeys.has(`${row.source}:${row.opened_at}`))],
          next_before: data.next_before,
          next_candle_before: data.next_candle_before,
          history_backfill: data.history_backfill,
        }
      })
    } catch {
      setStatus('Earlier retained history could not be loaded.')
    }
  }, [selectedCoin])
  const showTrading = (mint: string, positionId: string, quantity: string, strategy: StrategyOrder | null) => {
    setTradingFocus(null)
    setStrategyOrder(strategy)
    setTradeMint(mint)
    setTradePositionId(positionId)
    setTradeQuantity(quantity)
    setSelectedCoin(null)
    setWorkspaceView('dashboard')
    setDashboardTab('order')
  }
  const selectDashboardTab = (tab: TradingDashboardTab) => {
    if (tab === 'holdings' || tab === 'order') setTradingFocus(null)
    if (tab !== 'order') {
      setStrategyOrder(null)
      setTradeMint('')
      setTradePositionId('')
      setTradeQuantity('')
    }
    setDashboardTab(tab)
    if (tab === 'holdings' && browserWallet) {
      void refreshTradeState().then(() => walletPortfolio.refresh())
    }
  }
  const closeTrading = () => {
    const strategyId = strategyOrder?.agentId || tradeState?.positions.find((position) => position.id === tradePositionId)?.agent_id
    if (strategyId) selectTradingFocus({ kind: 'strategy', id: strategyId }, 'positions')
    else clearTradingFocus('holdings')
    refresh()
    void refreshTradeState()
    void walletPortfolio.refresh()
  }
  const selectTradingFocus = (target: TradingFocus, tab?: TradingDashboardTab) => {
    setWorkspaceView('dashboard')
    setTradingFocus({ scope: sessionQuery, target })
    setPositionFocus(null)
    selectDashboardTab(tab || (dashboardTab === 'holdings' || dashboardTab === 'order' ? 'overview' : dashboardTab))
  }
  const clearTradingFocus = (tab: TradingDashboardTab = 'overview') => {
    setTradingFocus(null)
    setPositionFocus(null)
    selectDashboardTab(tab === 'positions' ? 'overview' : tab)
  }
  const inspectPosition = (id: string, strategyId: string) => {
    selectTradingFocus({ kind: 'strategy', id: strategyId }, 'positions')
    setPositionFocus({ id, requestedAt: Date.now() })
  }
  const requestSigning = (action: PendingSigningAction) => {
    if (browserWallet?.signingAvailable) return false
    setPendingSigningAction(action)
    setWalletSessionUnlockOpen(true)
    return true
  }
  const openTrading = (mint = '', positionId = '', quantity = '') => {
    if (!browserWallet) {
      connectWallet()
      setStatus('Connect a wallet to review a trade. Research navigation created no order.')
      return
    }
    const action: PendingSigningAction = { kind: 'trade', mint, positionId, quantity, strategy: null }
    if (!requestSigning(action)) showTrading(mint, positionId, quantity, null)
  }
  const pauseWorkspace = async (workspace: SessionWorkspace, entriesOnly = false) => {
    const action = entriesOnly && workspace.mode === 'trade' ? 'pause-entries' : 'pause'
    const result = await sendApi<{ notice: string }>(`/api/v1/session-workspaces/${encodeURIComponent(workspace.id)}/${action}${sessionQuery}`, {})
    if (scopeRef.current !== sessionQuery) return
    setStatus(result.notice)
    const [sessionData, workspaceData] = await Promise.all([readApi<{ rows: Session[] }>(sessionCollectionPath), readApi<{ rows: SessionWorkspace[] }>(`/api/v1/session-workspaces${sessionQuery}`)])
    if (scopeRef.current !== sessionQuery) return
    setSessions(sessionData.rows)
    setSessionWorkspaces(workspaceData.rows)
    await serviceControl.refresh()
  }
  const sellPortfolioHolding = (target: PortfolioSellTarget) => {
    const action: PendingSigningAction = { kind: 'trade', mint: target.mint, positionId: target.positionId, quantity: target.quantity, strategy: target.strategy }
    if (!requestSigning(action)) showTrading(target.mint, target.positionId, target.quantity, target.strategy)
  }
  const reviewStrategyOrder = (order: StrategyOrder) => {
    setOrdersOpen(false)
    const action: PendingSigningAction = { kind: 'trade', mint: order.mint, positionId: order.positionId || '', quantity: '', strategy: order }
    if (!requestSigning(action)) showTrading(order.mint, order.positionId || '', '', order)
  }
  const requestWithdrawal = (recipient = '') => {
    if (!browserWallet) {
      connectWallet()
      setStatus('Connect the wallet that will authorize the withdrawal.')
      return
    }
    const action: PendingSigningAction = { kind: 'withdraw', recipient }
    if (!requestSigning(action)) setWithdrawRecipient(recipient)
  }
  const resumeWalletSigning = (session: WalletSession) => {
    const action = pendingSigningAction
    setBrowserWallet(session)
    rememberWalletSession(session, tradingMarket)
    setWalletSessionUnlockOpen(false)
    setPendingSigningAction(null)
    setStatus('Wallet signing restored for this tab session.')
    if (action?.kind === 'trade') showTrading(action.mint, action.positionId, action.quantity, action.strategy)
    if (action?.kind === 'withdraw') setWithdrawRecipient(action.recipient)
  }
  useEffect(() => {
    if ((!browserWallet && !researchOnly) || tradingMarket !== 'pumpfun') return
    const visible = () => document.visibilityState === 'visible'
    const reload = () => { if (visible()) void loadMarket().catch(() => undefined) }
    const localTimer = window.setInterval(reload, MARKET_LOCAL_REFRESH_MS)
    const onVisibility = () => { if (visible()) reload() }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      window.clearInterval(localTimer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [browserWallet, loadMarket, researchOnly, tradingMarket])
  useEffect(() => {
    if ((!browserWallet && !researchOnly) || !tradingMarket) return
    let cancelled = false
    const update = async () => {
      if (document.visibilityState !== 'visible') return
      try {
        const [data, workspaceData] = await Promise.all([
          readApi<{ rows: Session[] }>(sessionCollectionPath),
          readApi<{ rows: SessionWorkspace[] }>(`/api/v1/session-workspaces${sessionQuery}`),
        ])
        if (!cancelled && scopeRef.current === sessionQuery) {
          setSessions(data.rows)
          setSessionWorkspaces(workspaceData.rows)
        }
      } catch { if (!cancelled) setStatus('Monitoring health could not be refreshed.') }
    }
    const timer = window.setInterval(() => { void update() }, 5000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [browserWallet, researchOnly, tradingMarket, sessionCollectionPath, sessionQuery])

  const openStrategies = () => {
    setSessionPlan(null)
    readApi<{ rows: MarketplaceStrategy[] }>('/api/v1/marketplace').then((data) => {
      setMarketplace(data.rows)
      setSessionPlanOpen(true)
    }).catch(() => setStatus('Local strategy marketplace could not be loaded.'))
  }
  const confirmTradingSession = async (plan: SessionPlan) => {
    if (plan.session_mode === 'trade') {
      if (!browserWallet) throw new Error('Connect the wallet that will own this trading session.')
      const created = await sendApi<{ session: SessionWorkspace; notice: string }>('/api/v1/session-workspaces', {
        mode: 'trade',
        funding: {
          sender: browserWallet.address,
          network: browserWallet.network,
          allocation_sol: plan.allocation_sol,
        },
        plan,
      })
      if (scopeRef.current !== sessionQuery) return
      setSessionWorkspaces((current) => [created.session, ...current])
      selectTradingFocus({ kind: 'session', id: created.session.id })
      setStatus(created.notice)
    }
    setSessionPlan(null)
    setSessionPlanOpen(false)
    setStrategyOpen(false)
  }

  const confirmMonitoringSession = async (plan: SessionPlan) => {
    const created = await sendApi<{ session: SessionWorkspace; notice: string }>('/api/v1/session-workspaces', { mode: 'monitoring', monitor_activity: plan.monitor_activity || 'observations', ...(browserWallet ? { owner: { sender: browserWallet.address, network: browserWallet.network } } : {}) })
    if (scopeRef.current !== sessionQuery) return
    setSessionWorkspaces((current) => [created.session, ...current])
    setSessionPlanOpen(false)
    setSessionPlan(null)
    setStatus(created.notice)
  }
  const addStrategyToWorkspace = (workspace: SessionWorkspace) => {
    const open = (catalog: MarketplaceStrategy[]) => {
      if (workspace.mode === 'trade') {
        if (!browserWallet || browserWallet.address !== workspace.wallet_sender || browserWallet.network !== workspace.wallet_network) {
          setStatus('Connect the exact wallet and network that own this trading session before adding a strategy.')
          return
        }
        if (workspace.plan.session_mode !== 'trade' || workspace.plan.allocation_sol === undefined) {
          setStatus('This older trading session has no reusable session plan. Create a new trading session.')
          return
        }
        setSessionPlan({ ...workspace.plan, workspace_id: workspace.id } as SessionPlan)
      } else {
        const families = Array.from(new Set(catalog.map((item) => item.family).filter(Boolean))).sort()
        setSessionPlan({ ...workspaceMonitoringPlan(families, workspace.id), monitor_activity: workspace.plan.monitor_activity === 'paper' ? 'paper' : 'observations' })
      }
        setStrategyOpen(true)
    }
    if (marketplace.length) {
      open(marketplace)
      return
    }
    readApi<{ rows: MarketplaceStrategy[] }>('/api/v1/marketplace').then((data) => {
      setMarketplace(data.rows)
      open(data.rows)
    }).catch(() => setStatus('Strategy evidence could not be loaded for this session.'))
  }
  const showStrategyDraft = (goal: StrategyGoalId | null) => {
    setDraftInitialGoal(goal)
    if (marketplace.length) {
      setDraftOpen(true)
      return
    }
    readApi<{ rows: MarketplaceStrategy[] }>('/api/v1/marketplace').then((data) => {
      setMarketplace(data.rows)
      setDraftOpen(true)
    }).catch(() => setStatus('Strategy evidence could not be loaded for the creation wizard.'))
  }
  const openStrategyDraft = () => showStrategyDraft(null)
  const importStrategy = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) return
    file.text().then(JSON.parse).then((strategy: object) => sendApi(`/api/v1/strategies/import${sessionQuery}`, strategy))
      .then(() => { setStatus(`Imported ${file.name} through local Python validation.`); refresh() })
      .catch(() => setStatus('Strategy import failed. Use a valid PumpWizard handoff JSON.'))
      .finally(() => { event.target.value = '' })
  }
  const importMarketplaceStrategy = async (strategy: MarketplaceStrategy) => {
    try {
      await sendApi(`/api/v1/marketplace/import${sessionQuery}`, { id: strategy.id })
      const saved = await readApi<{ rows: Strategy[] }>(`/api/v1/strategies${sessionQuery}`)
      if (scopeRef.current !== sessionQuery) throw new Error('Wallet changed. Return to the original wallet to continue.')
      setStrategies(saved.rows)
      const local = saved.rows.find((row) => row.digest === strategy.digest)
      if (!local) throw new Error('Imported strategy could not be loaded.')
      const instanceId = await beginSession(local, true)
      return { digest: local.digest, instanceId }
    } catch (error) {
      throw new Error(error instanceof Error ? error.message : 'Could not add this strategy.')
    }
  }
  const saveStrategyRevision = async (digest: string, label: string, configuration: Record<string, unknown>, sizing?: SizingRequest, instanceId?: string) => {
    const saved = await sendApi<{ strategy: { digest: string }; sizing: AllocationSizing | null }>(`/api/v1/strategies/${encodeURIComponent(digest)}/revisions${sessionQuery}`, { label, configuration, ...(sizing ? { sizing } : {}), ...(instanceId ? { instance_id: instanceId } : {}) })
    const list = await readApi<{ rows: Strategy[] }>(`/api/v1/strategies${sessionQuery}`)
    if (scopeRef.current !== sessionQuery) throw new Error('Wallet changed. Return to the original wallet to view the saved revision.')
    setStrategies(list.rows)
    if (saved.sizing) {
      const sized = saved.sizing
      setSessionPlan((current) => current?.workspace_id === sized.workspace_id ? { ...current, max_trade_sol: sized.trade_budget_sol } : current)
    }
    if (instanceId) {
      const detail = await readApi<SessionDetail>(sessionItemPath(instanceId))
      if (scopeRef.current === sessionQuery) setSessions((current) => current.map((item) => item.instance.id === instanceId ? detail : item))
    }
    return saved.strategy.digest
  }
  const saveInitialLimits = async (plan: SessionPlan, instanceId?: string) => {
    if (instanceId) {
      if (!browserWallet) throw new Error('Connect the session wallet to save limits.')
      const { allocation_sol, buys_enabled, sells_enabled, min_trade_sol, max_trade_sol, max_daily_buy_sol, max_trades_per_day, max_daily_loss_sol, max_daily_loss_percent, max_trade_loss_percent, max_open_positions } = plan
      await sendApi(`/api/v1/strategy-controls/${encodeURIComponent(instanceId)}`, { sender: browserWallet.address, network: browserWallet.network, allocation_sol, buys_enabled, sells_enabled, min_trade_sol, max_trade_sol, max_daily_buy_sol, max_trades_per_day, max_daily_loss_sol, max_daily_loss_percent, max_trade_loss_percent, max_open_positions })
      const detail = await readApi<SessionDetail>(sessionItemPath(instanceId))
      if (scopeRef.current !== sessionQuery) throw new Error('Wallet changed. Return to the original wallet to view these limits.')
      setSessions((current) => current.map((item) => item.instance.id === instanceId ? detail : item))
    }
    setSessionPlan(plan)
  }
  const beginSession = async (strategy: Strategy, editAfter = false) => {
    if (!sessionPlan) {
      setStatus('Start from Add session so mode and allocation are reviewed first.')
      return
    }
    if (sessionPlan.strategy_setup === 'catalog' || editAfter) {
      const sizing = strategy.allocation_sizing?.find((item) => item.workspace_id === sessionPlan.workspace_id)
      const assignedPlan = sizing ? { ...sessionPlan, max_trade_sol: sizing.trade_budget_sol } : sessionPlan
      const config = strategy.configuration || {}
      const orderUsd = Math.max(.000000001, Number(config.order_usd) || 10)
      const budget = Math.max(orderUsd, Number(config.budget_usd) || 500)
      return await createSession(strategy, strategy.label, {
        goal: 'validate-strategy', intent: `Use the saved rules for ${strategy.label}.`,
        paper_budget_usd: budget, max_session_loss_usd: Math.min(budget, Math.max(orderUsd, budget * .2)),
        review_after_seconds: 86400,
        session_mode: sessionPlan.session_mode, execution_style: sessionPlan.execution_style,
        automation_scope: sessionPlan.automation_scope, profit_management: sessionPlan.profit_management,
        ...(sessionPlan.custody_mode === 'main-wallet-service' ? { custody_mode: sessionPlan.custody_mode } : {}),
        preferences: null,
      }, assignedPlan, editAfter)
    }
    setStrategyOpen(false)
    setSessionDraftStrategy(strategy)
  }
  const createSession = async (strategy: Strategy, label: string, brief: SessionBriefInput, assignedPlan = sessionPlan, keepEditorOpen = false) => {
    if (!assignedPlan) throw new Error('Review session mode and allocation before creating the session.')
    if (assignedPlan.session_mode === 'trade' && !browserWallet) throw new Error('Connect the wallet that will own this trading session.')
    try {
      const created = await sendApi<{ instance: { id: string } }>(`/api/v1/sessions${sessionQuery}`, {
        digest: strategy.digest,
        label,
        brief,
        ...(assignedPlan.workspace_id ? { workspace_id: assignedPlan.workspace_id } : {}),
        ...(assignedPlan.session_mode === 'trade' ? { funding: {
          sender: browserWallet!.address,
          network: browserWallet!.network,
          allocation_sol: assignedPlan.allocation_sol,
          buys_enabled: assignedPlan.buys_enabled,
          sells_enabled: assignedPlan.sells_enabled,
          min_trade_sol: assignedPlan.min_trade_sol,
          max_trade_sol: assignedPlan.max_trade_sol,
          max_daily_buy_sol: assignedPlan.max_daily_buy_sol,
          max_trades_per_day: assignedPlan.max_trades_per_day,
          max_daily_loss_sol: assignedPlan.max_daily_loss_sol,
          max_daily_loss_percent: assignedPlan.max_daily_loss_percent,
          max_trade_loss_percent: assignedPlan.max_trade_loss_percent,
          max_open_positions: assignedPlan.max_open_positions,
        } } : {}),
      })
      const detail = await readApi<SessionDetail>(sessionItemPath(created.instance.id))
      if (scopeRef.current !== sessionQuery) return
      setStatus(assignedPlan.session_mode === 'monitoring'
        ? 'Monitoring session created without a wallet allocation. Monitoring remains off until you start it.'
        : assignedPlan.execution_style === 'interactive'
          ? `Strategy added to the session's shared ${assignedPlan.allocation_sol} SOL allocation. Every order still requires your signature.`
          : `${assignedPlan.automation_scope === 'entries-only' ? 'Automatic-entries' : assignedPlan.automation_scope === 'exits-only' ? 'Automatic-exits' : 'Full Autopilot'} strategy added to the session's shared ${assignedPlan.allocation_sol} SOL allocation. ${assignedPlan.custody_mode === 'main-wallet-service' ? 'Service custody still requires encrypted enrollment, an exact allowance, fresh unlock and Start.' : 'Browser automation remains disabled pending wallet authorization and activation.'}`)
      if (!keepEditorOpen) {
        setStrategyOpen(false)
        setSessionPlan(null)
      }
      setSessionDraftStrategy(null)
      setSessions((current) => [detail, ...current.filter((session) => session.instance.id !== detail.instance.id)])
      if (browserWallet) selectTradingFocus({ kind: 'strategy', id: detail.instance.id })
      if (keepEditorOpen || assignedPlan.strategy_setup === 'catalog') {
        setSessionDetail(null)
      } else if (detail.brief.permissions?.custody === 'main-wallet-service') {
        setSessionDetail(null)
        setServiceTargetSessionId(detail.instance.id)
        setServiceOpen(true)
      } else if (assignedPlan.monitor_activity === 'paper') {
        void openSessionReview(detail.instance.id, detail.instance.label, 'paper')
      } else {
        setSessionDetail(detail)
      }
      refresh()
      return created.instance.id
    } catch (reason) {
      setStatus(reason instanceof Error ? reason.message : 'Session creation was rejected by the local safety checks.')
      throw reason
    }
  }
  const openStrategyDetail = (digest: string) => {
    readApi<StrategyDetail>(`/api/v1/strategies/${encodeURIComponent(digest)}${sessionQuery}`)
      .then(setStrategyDetail)
      .catch(() => setStatus('Configuration identity and evidence could not be loaded.'))
  }
  const exportStrategy = (digest: string) => {
    readApi<{ package: object; filename: string }>(`/api/v1/strategies/${encodeURIComponent(digest)}/export${sessionQuery}`).then((data) => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(data.package, null, 2)], { type: 'application/json' }))
      const link = document.createElement('a')
      link.href = url
      link.download = data.filename
      link.click()
      URL.revokeObjectURL(url)
      setStatus('Exported the exact existing PumpWizard strategy handoff package.')
    }).catch(() => setStatus('Strategy export could not be prepared from the local library.'))
  }
  const setTracking = async (session: Session) => {
    const enable = session.state.label === 'Waiting for monitor' || Boolean(session.tracking.fault) || !session.tracking.enabled
    try {
      await sendApi(`/api/v1/sessions/${session.instance.id}/tracking`, { enabled: enable })
      setStatus(`${session.tracking.fault ? 'Retry requested' : `Monitoring ${enable ? 'enabled' : 'paused'}`} locally. No orders were created.`)
      refresh()
    } catch (error) {
      setStatus('The local monitoring update was rejected.')
      throw error
    }
  }
  const scanSession = async (session: Session) => {
    setStatus('Requesting one bounded local monitoring pass…')
    try {
      const data = await sendApi<{ completed: number; notice: string }>(`/api/v1/sessions/${session.instance.id}/scan`, {})
      setStatus(data.completed ? `${data.completed} local monitors completed. No order was created.` : 'The monitoring service was notified. Check activity for the next completed scan.')
      refresh()
    } catch (error) {
      setStatus('One-time monitoring scan was rejected. Start tracking first, then retry.')
      throw error
    }
  }
  const openSession = (id: string) => {
    setStrategySettingsOpen(false)
    setSessionDetailError('')
    readApi<SessionDetail>(sessionItemPath(id)).then((detail) => { if (scopeRef.current === sessionQuery) setSessionDetail(detail) }).catch(() => setStatus('Session detail could not be loaded.'))
  }
  const openSessionReview = async (id: string, label: string, initialTab: 'evidence' | 'paper' = 'evidence') => {
    setStrategySettingsOpen(false)
    setSessionReviewError('')
    setSessionReview({ sourceId: id, sessionLabel: label, initialTab, report: null, evidence: null })
    try {
      const data = await readApi<{
        reports: SessionReviewReport[]
        evidence: SessionReviewEvidence | null
      }>(`/api/v1/sessions/${encodeURIComponent(id)}/reviews`)
      setSessionReview((current) => current?.sourceId === id ? {
        ...current,
        report: data.reports[0] || null,
        evidence: data.evidence,
      } : current)
    } catch (reason) {
      setSessionReviewError(reason instanceof Error ? reason.message : 'Saved session reviews could not be loaded.')
    }
  }
  const runSessionReview = async () => {
    const current = sessionReview
    if (!current || sessionReviewBusy) return
    setSessionReviewBusy(true)
    setSessionReviewError('')
    try {
      const data = await sendApi<{
        dataset: { id: string; evidence: SessionReviewEvidence }
        report: SessionReviewReport
      }>(`/api/v1/sessions/${encodeURIComponent(current.sourceId)}/reviews`, {
        horizon_seconds: 1_800,
      })
      setSessionReview((latest) => latest?.sourceId === current.sourceId ? {
        ...latest,
        report: data.report,
        evidence: data.dataset.evidence,
      } : latest)
    } catch (reason) {
      setSessionReviewError(reason instanceof Error ? reason.message : 'The offline comparison could not be completed.')
    } finally {
      setSessionReviewBusy(false)
    }
  }
  const createSessionReviewDraft = async (candidateId: string) => {
    const current = sessionReview
    if (!current?.report || sessionReviewBusy) return
    setSessionReviewBusy(true)
    setSessionReviewError('')
    try {
      const data = await sendApi<{ session: { id: string }; notice: string }>(
        `/api/v1/session-reviews/${encodeURIComponent(current.report.id)}/draft`,
        { candidate_id: candidateId, accepted: true },
      )
      const detail = await readApi<SessionDetail>(sessionItemPath(data.session.id))
      setSessionReview(null)
      setSessionDetail(detail)
      setStatus(data.notice)
      refresh()
    } catch (reason) {
      setSessionReviewError(reason instanceof Error ? reason.message : 'The inactive successor draft could not be created.')
    } finally {
      setSessionReviewBusy(false)
    }
  }
  const saveSessionSettings = async (settings: EditableStrategySettings) => {
    if (!sessionDetail || !browserWallet) throw new Error('Connect the wallet used by this strategy session.')
    const id = sessionDetail.instance.id
    const result = await sendApi<{ notice: string }>(`/api/v1/sessions/${encodeURIComponent(id)}/configuration`, {
      ...settings,
      sender: browserWallet.address,
      network: browserWallet.network,
    })
    const updated = await readApi<SessionDetail>(sessionItemPath(id))
    setSessionDetail(updated)
    setStatus(result.notice)
    refresh()
  }
  const loadSessionManagement = async (id: string) => {
    const detail = await readApi<SessionDetail>(sessionItemPath(id))
    return detail.management
  }
  const toggleDetailMonitoring = async () => {
    if (!sessionDetail || sessionDetailBusy) return
    const enable = sessionDetail.state.label === 'Waiting for monitor' || Boolean(sessionDetail.tracking.fault) || !sessionDetail.tracking.enabled
    setSessionDetailBusy(true)
    setSessionDetailError('')
    try {
      await sendApi(`/api/v1/sessions/${sessionDetail.instance.id}/tracking`, { enabled: enable })
      const updated = await readApi<SessionDetail>(sessionItemPath(sessionDetail.instance.id))
      setSessionDetail(updated)
      setStatus(`Monitoring ${enable ? 'started' : 'stopped'} for ${updated.instance.label}. No order was created.`)
      refresh()
    } catch (error) {
      setSessionDetailError(error instanceof Error ? error.message : 'Monitoring could not be updated.')
    } finally {
      setSessionDetailBusy(false)
    }
  }
  const scanSessionDetail = async () => {
    if (!sessionDetail || sessionDetailBusy) return
    setSessionDetailBusy(true)
    setSessionDetailError('')
    try {
      await sendApi(`/api/v1/sessions/${sessionDetail.instance.id}/scan`, {})
      const updated = await readApi<SessionDetail>(sessionItemPath(sessionDetail.instance.id))
      setSessionDetail(updated)
      setStatus(`Updated ${updated.instance.label} monitoring activity. No order was created.`)
      refresh()
    } catch (error) {
      setSessionDetailError(error instanceof Error ? error.message : 'The scan could not be completed.')
    } finally {
      setSessionDetailBusy(false)
    }
  }
  const renameStrategy = async (id: string, label: string) => {
    const data = await sendApi<{ notice: string }>(`/api/v1/sessions/${id}/edit`, { label })
    setStatus(data.notice)
    refresh()
  }
  const removeStrategy = async (id: string, releaseAllocation: boolean) => {
    const data = await sendApi<{ notice: string }>(`/api/v1/sessions/${id}/remove`, {
      confirm: id,
      release_allocation: releaseAllocation,
    })
    setStatus(data.notice)
    refresh()
  }
  const mainWalletAllowanceDetail = sessionDetail
    && ['pumpwizard.session-brief/v2', 'pumpwizard.session-brief/v3'].includes(sessionDetail.brief.schema)
    && sessionDetail.brief.mode === 'trade-autopilot'
    && sessionDetail.brief.permissions?.custody === 'main-wallet-browser'
    && sessionDetail.risk_policy
    ? { session: sessionDetail.instance, limits: sessionDetail.risk_policy }
    : null
  const serviceCustodyDetail = sessionDetail
    && sessionDetail.brief.schema === 'pumpwizard.session-brief/v4'
    && sessionDetail.brief.mode === 'trade-autopilot'
    && sessionDetail.brief.permissions?.custody === 'main-wallet-service'
  const dashboardContent = <PortfolioDashboard key={`${sessionQuery}:${scopedTradingFocus?.kind || 'wallet'}:${scopedTradingFocus?.id || ''}`} sessions={sessions} workspaces={sessionWorkspaces} trades={tradeState} market={market} error={receiptError} focus={scopedTradingFocus} service={serviceControl.state} selectFocus={selectTradingFocus} viewPosition={(id, strategyId) => {
    if (strategyId) inspectPosition(`wallet-${id}`, strategyId)
    else clearTradingFocus('holdings')
  }} viewPositions={() => selectDashboardTab('positions')} viewWallet={() => clearTradingFocus('holdings')} viewActivity={() => selectDashboardTab('history')} reviewFunds={setFundsWorkspace} createSession={openStrategies} />
  const rpcDialog = rpcOpen && <RpcConnection close={() => setRpcOpen(false)} changed={() => { setRpcRevision((value) => value + 1); if (browserWallet) void refreshTradeState() }} />
  const rpcSetup = <button className="rpc-setup-button" onClick={() => setRpcOpen(true)}>RPC connection</button>
  if (sessionRestorePending) return <main className="market-onboarding"><div className="market-shell"><section className="market-wizard session-restore"><span className="pulse" /><small>SECURE TAB SESSION</small><h1>Restoring your terminal…</h1><p>Checking the existing wallet authorization without loading signing material from storage.</p></section></div></main>
  if (!browserWallet && !researchOnly) return <><WalletOnboarding version={runnerVersion} onReady={(session) => activateBrowserWallet(session)} onExplore={enterResearch} />{capabilitiesError && <p className="entry-capability-error" role="alert">Research entrance unavailable: {capabilitiesError}</p>}{rpcSetup}{rpcDialog}</>
  if (!tradingMarket) return <main className="market-onboarding"><div className="market-shell"><section className="market-wizard session-restore"><span className="pulse" /><small>PUMP.FUN CONTEXT</small><h1>Opening your terminal…</h1><p>Preparing the supported Pump.fun routes without changing wallet permissions.</p></section></div></main>

  return <WalletWorkspaceScope.Provider value={sessionQuery}><div className="desktop-shell">
    <p className="sr-only" aria-live="polite" aria-atomic="true">{status}</p>
    <header className="desktop-header unified-header">
      <SessionAgentTerminal key={sessionQuery} version={runnerVersion} rpcRevision={rpcRevision} profile={scopedAssistant?.profile || null} conversations={scopedAssistant?.conversations || []} sessions={sessions} workspaces={sessionWorkspaces} wallet={browserWallet} service={serviceControl.state} setup={() => { setAssistantSetup(true) }} connectWallet={connectWallet} unlockSigning={() => { setPendingSigningAction(null); setWalletSessionUnlockOpen(true) }} configureRpc={() => { setRpcOpen(true) }} openService={() => { setServiceTargetSessionId(''); setServiceOpen(true) }} openGuide={() => setWelcomeMode('introduction')} signOut={() => { void lockBrowserWallet() }} openSession={(id) => { openSession(id) }} workspaceHeader={
      <WorkspaceHeader destination={workspaceView} select={(destination) => {
        setWorkspaceView(destination)
        if (destination === 'dashboard') clearTradingFocus()
      }} />
      } walletSummary={browserWallet && <WalletSummary key={`${browserWallet.network}:${browserWallet.address}`} wallet={browserWallet} history={walletBalanceHistory} deposit={() => setDepositOpen(true)} withdraw={() => requestWithdrawal()} />} assistantSettings={assistantSetup && <AssistantSetupDialog profile={assistant?.profile || null} close={() => { setAssistantSetup(false) }} saved={refresh} />} />
    </header>

    {workspaceView === 'research' && <div className="research-layout"><TradingSidebar sessions={sessions} workspaces={sessionWorkspaces} trades={tradeState} market={market} openPosition={inspectPosition} focus={scopedTradingFocus} selectWorkspace={(workspace) => selectTradingFocus({ kind: 'session', id: workspace.id })} selectStrategy={(id) => { if (browserWallet) selectTradingFocus({ kind: 'strategy', id }); else { setTradingFocus({ scope: sessionQuery, target: { kind: 'strategy', id } }); openSession(id) } }} pauseWorkspace={(workspace) => pauseWorkspace(workspace, true)} viewPositions={(workspace) => selectTradingFocus({ kind: 'session', id: workspace.id }, 'positions')} viewWalletHoldings={() => { setWorkspaceView('dashboard'); clearTradingFocus('holdings') }} deposit={() => setDepositOpen(true)} sellWorkspace={setSellAllWorkspace} editFunds={setFundsWorkspace} addStrategy={openStrategies} addStrategyToWorkspace={addStrategyToWorkspace} openSession={openSession} loadManagement={loadSessionManagement} renameStrategy={renameStrategy} removeStrategy={removeStrategy} setTracking={setTracking} scanSession={scanSession} manageLimits={browserWallet ? () => setWalletOpen(true) : undefined} reviewOrders={browserWallet ? () => setOrdersOpen(true) : undefined} reviewSession={(id, label) => void openSessionReview(id, label)} paperSession={(id, label) => void openSessionReview(id, label, 'paper')} /><ResearchHome sessions={sessions} workspaces={sessionWorkspaces} market={market} service={serviceControl.state} createSession={openStrategies} openSession={openSession} inspectResearch={(id, label) => void openSessionReview(id, label)} /></div>}

    {workspaceView === 'dashboard' && <main className="trade-layout restored-dashboard unified-dashboard" aria-label="Wallet dashboard">
      <TradingSidebar sessions={sessions} workspaces={sessionWorkspaces} trades={tradeState} market={market} openPosition={inspectPosition} focus={scopedTradingFocus} selectWorkspace={(workspace) => selectTradingFocus({ kind: 'session', id: workspace.id })} selectStrategy={(id) => { if (browserWallet) selectTradingFocus({ kind: 'strategy', id }); else { setTradingFocus({ scope: sessionQuery, target: { kind: 'strategy', id } }); openSession(id) } }} pauseWorkspace={(workspace) => pauseWorkspace(workspace, true)} viewPositions={(workspace) => selectTradingFocus({ kind: 'session', id: workspace.id }, 'positions')} viewWalletHoldings={() => { setWorkspaceView('dashboard'); clearTradingFocus('holdings') }} deposit={() => setDepositOpen(true)} sellWorkspace={setSellAllWorkspace} editFunds={setFundsWorkspace} addStrategy={openStrategies} addStrategyToWorkspace={addStrategyToWorkspace} openSession={openSession} loadManagement={loadSessionManagement} renameStrategy={renameStrategy} removeStrategy={removeStrategy} setTracking={setTracking} scanSession={scanSession} manageLimits={browserWallet ? () => setWalletOpen(true) : undefined} reviewOrders={browserWallet ? () => setOrdersOpen(true) : undefined} reviewSession={(id, label) => void openSessionReview(id, label)} paperSession={(id, label) => void openSessionReview(id, label, 'paper')} />
      <section className="market-panel workspace-page trade-workspace">
        <OpenPositionsTerminal holdingsCount={walletPortfolio.portfolio?.assets.length ?? null} holdings={browserWallet ? <><WalletOverview portfolio={walletPortfolio.portfolio} history={portfolioHistory} error={walletPortfolio.error || receiptError} /><PortfolioWorkspace wallet={browserWallet} trades={tradeState} walletPortfolio={walletPortfolio.portfolio} walletPortfolioError={walletPortfolio.error} walletPortfolioLoading={walletPortfolio.loading} refresh={async () => { await refreshTradeState(); await walletPortfolio.refresh() }} sell={sellPortfolioHolding} openToken={(mint) => openCoin({ mint })} reviewSession={(id, label) => void openSessionReview(id, label)} /></> : <div className="activity-wallet-required"><h2>Connect a wallet to view holdings</h2><p>Connect to read token balances, valuations and holding details.</p><button className="primary" onClick={connectWallet}>Connect wallet</button></div>} overview={dashboardContent} editFunds={setFundsWorkspace} focus={scopedTradingFocus} workspaces={sessionWorkspaces} selectFocus={selectTradingFocus} clearFocus={clearTradingFocus} positionFocus={positionFocus} recordsError={receiptError} refreshActivity={() => { refresh(); void refreshTradeState() }} sessions={sessions} trades={tradeState} market={market} openSession={openSession} openToken={openCoin} trade={openTrading} activeTab={dashboardTab} selectTab={selectDashboardTab} order={dashboardTab === 'order' ? browserWallet ? <WalletTrading key={`${browserWallet.address}:${browserWallet.network}:${tradeMint}:${tradePositionId}:${tradeQuantity}:${strategyOrder?.agentId || ''}`} embedded session={browserWallet} close={closeTrading} strategy={strategyOrder} initialMint={tradeMint} initialPositionId={tradePositionId} initialSellQuantity={tradeQuantity} trades={tradeState} funds={funds} refreshRecords={refreshTradeState} refreshingRecords={refreshingReceipts} recordsError={receiptError} /> : <div className="activity-wallet-required"><small>WALLET ACTION</small><h2>Connect when you are ready to review an order</h2><p>Research, observations and reports remain available without a wallet. Connecting does not submit or sign anything.</p><button className="primary" onClick={connectWallet}>Connect wallet</button></div> : null} />
      </section>
    </main>}

    {browserWallet && fundsWorkspace && <SessionFundsDialog key={`${sessionQuery}:${fundsWorkspace.id}`} workspace={fundsWorkspace} wallet={browserWallet} scopeQuery={sessionQuery} pause={() => pauseWorkspace(fundsWorkspace)} changed={() => { refresh(); void refreshTradeState() }} close={() => setFundsWorkspace(null)} />}
    {browserWallet && sellAllWorkspace && <SessionSellAll key={`${sessionQuery}:${sellAllWorkspace.id}`} workspace={sellAllWorkspace} wallet={browserWallet} scopeQuery={sessionQuery} trades={tradeState} funds={funds} refreshing={refreshingReceipts} recordsError={receiptError} refresh={refreshTradeState} pause={() => pauseWorkspace(sellAllWorkspace)} unlock={() => { setPendingSigningAction(null); setWalletSessionUnlockOpen(true) }} close={() => { setSellAllWorkspace(null); refresh(); void refreshTradeState() }} />}
    {sessionDetail && <SessionMonitor scopeQuery={sessionQuery} detail={sessionDetail} busy={sessionDetailBusy} error={sessionDetailError} close={() => { if (!sessionDetailBusy) { setStrategySettingsOpen(false); setMainWalletAllowanceOpen(false); setSessionDetail(null) } }} toggle={() => void toggleDetailMonitoring()} scan={() => void scanSessionDetail()} settings={() => setStrategySettingsOpen(true)} orders={() => { setMainWalletAllowanceOpen(false); setSessionDetail(null); setOrdersOpen(true) }} review={() => void openSessionReview(sessionDetail.instance.id, sessionDetail.instance.label)} paper={() => void openSessionReview(sessionDetail.instance.id, sessionDetail.instance.label, 'paper')} allowance={mainWalletAllowanceDetail ? () => setMainWalletAllowanceOpen(true) : serviceCustodyDetail ? () => { setServiceTargetSessionId(sessionDetail.instance.id); setSessionDetail(null); setServiceOpen(true) } : undefined} />}
    {sessionDetail && strategySettingsOpen && <StrategySettings detail={sessionDetail} close={() => setStrategySettingsOpen(false)} save={saveSessionSettings} />}
    {sessionReview && <SessionReview key={`${sessionReview.sourceId}:${sessionReview.initialTab}`} initialTab={sessionReview.initialTab} sourceSessionId={sessionReview.sourceId} sessionLabel={sessionReview.sessionLabel} report={sessionReview.report} evidence={sessionReview.evidence} busy={sessionReviewBusy} error={sessionReviewError} close={() => { if (!sessionReviewBusy) setSessionReview(null) }} run={() => void runSessionReview()} keepCurrent={() => { setStatus(`Kept the current strategy for ${sessionReview.sessionLabel}. No session or wallet setting changed.`); setSessionReview(null) }} createDraft={(candidateId) => void createSessionReviewDraft(candidateId)} researchDraftCreated={() => { setStatus('Created the exact inactive research successor. It remains paused, unallocated and unable to enter.'); setSessionReview(null); refresh() }} />}
    {browserWallet && mainWalletAllowanceOpen && mainWalletAllowanceDetail && <MainWalletAllowanceDialog wallet={browserWallet} session={{ id: mainWalletAllowanceDetail.session.id, label: mainWalletAllowanceDetail.session.label, strategy_digest: mainWalletAllowanceDetail.session.strategy_digest || sessionDetail?.brief.strategy_digest_at_creation || '', automation_scope: sessionDetail?.brief.automation_scope || 'entries-and-exits' }} limits={mainWalletAllowanceDetail.limits} activeRuntime={mainWalletRuntimeState} activateRuntime={startMainWalletAutopilot} pauseRuntime={pauseMainWalletAutopilot} stopRuntime={stopMainWalletAutopilot} close={() => setMainWalletAllowanceOpen(false)} />}
    {serviceOpen && <ServiceControlDialog wallet={browserWallet} sessions={sessions} initialSessionId={serviceTargetSessionId} state={serviceControl.state} refresh={serviceControl.refresh} acknowledgeRestart={serviceControl.acknowledgeRestart} configureMonitoring={serviceControl.configureMonitoring} enrollWallet={serviceControl.enrollWallet} reviewAllowance={serviceControl.reviewAllowance} authorizeAllowance={serviceControl.authorizeAllowance} approveAcceptance={serviceControl.approveAcceptance} acceptanceEvidence={serviceControl.acceptanceEvidence} startSigning={serviceControl.startSigning} unlockSigning={() => { setPendingSigningAction(null); setWalletSessionUnlockOpen(true) }} pauseEntries={serviceControl.pauseEntries} resumeEntries={serviceControl.resumeEntries} revokeTrading={serviceControl.revokeTrading} shutdown={serviceControl.shutdown} close={() => { setServiceOpen(false); setServiceTargetSessionId('') }} />}
    {sessionPlanOpen && <SessionPlanDialog wallet={browserWallet} simulations={marketplace} close={() => setSessionPlanOpen(false)} confirmTrading={confirmTradingSession} confirmMonitoring={confirmMonitoringSession} selectAutomationWallet={selectAutomationWallet} sourceUnlocked={resumeWalletSigning} refreshWallet={refreshTradeState} />}
    <input ref={importInput} hidden type="file" accept="application/json,.json" aria-label="Import strategy configuration" onChange={importStrategy} />
    {strategyOpen && <LocalStrategyLibrary strategies={loadedScope === sessionQuery ? strategies : []} sessions={sessions} sessionPlan={sessionPlan} workspace={sessionWorkspaces.find((row) => row.id === sessionPlan?.workspace_id) || null} walletSigning={Boolean(browserWallet?.signingAvailable)} service={serviceControl.state} openTradingSetup={(id) => { setStrategyOpen(false); setSessionPlan(null); openSession(id) }} reviewFunds={(workspace) => { setStrategyOpen(false); setSessionPlan(null); setFundsWorkspace(workspace) }} catalog={marketplace} close={() => { setStrategyOpen(false); setSessionPlan(null) }} create={openStrategyDraft} importPreset={importMarketplaceStrategy} importFile={() => importInput.current?.click()} inspect={openStrategyDetail} exportStrategy={exportStrategy} start={(strategy) => beginSession(strategy, true)} saveRevision={saveStrategyRevision} updatePlan={saveInitialLimits} />}
    {sessionDraftStrategy && sessionPlan && <SessionBriefDialog strategy={sessionDraftStrategy} plan={sessionPlan} close={() => { setSessionDraftStrategy(null); setStrategyOpen(true) }} submit={async (label, brief) => { await createSession(sessionDraftStrategy, label, brief) }} />}
    {strategyDetail && <div className="modal-backdrop detail-layer" role="presentation" onMouseDown={() => setStrategyDetail(null)}>
      <section className="strategy-modal strategy-detail" role="dialog" aria-modal="true" aria-label="Strategy identity and evidence" onMouseDown={(event) => event.stopPropagation()}>
        <button className="close" aria-label="Close dialog" onClick={() => setStrategyDetail(null)}>×</button>
        <small>CONFIGURATION IDENTITY</small>
        <h2>{strategyDetail.strategy.label}</h2>
        <p><b>{strategyDetail.capability}</b> · {strategyDetail.strategy.compatibility}</p>
        <div className="identity-grid"><span>Package SHA-256<code>{strategyDetail.identity.package_sha256}</code></span><span>Rules SHA-256<code>{strategyDetail.identity.rules_sha256}</code></span><span>Schema<code>{strategyDetail.identity.package_schema}</code></span><span>Engine / profile<code>{strategyDetail.identity.decision_engine} · {strategyDetail.identity.profile}</code></span></div>
        <pre className="support-text">{strategyDetail.support}</pre>
        <div className="modal-actions"><button onClick={() => exportStrategy(strategyDetail.strategy.digest)}>Export exact config</button><button className="primary" onClick={() => setStrategyDetail(null)}>Done</button></div>
      </section>
    </div>}
    {selectedCoin && <TokenDetail coin={selectedCoin} close={() => setSelectedCoin(null)} trade={(mint) => openTrading(mint)} loadEarlier={loadEarlierCoinHistory} retryHistory={() => void backfillCoinHistory(selectedCoin.mint)} historyImporting={historyImporting} />}
    {draftOpen && <DraftDialog market={market} marketplace={marketplace} initialGoal={draftInitialGoal} close={() => { setDraftOpen(false); setDraftInitialGoal(null) }} saved={() => { setDraftInitialGoal(null); setStrategyOpen(true); refresh() }} />}
    {browserWallet && depositOpen && <DepositDialog session={browserWallet} close={() => setDepositOpen(false)} onBalance={updateBrowserBalance} />}
    {browserWallet && withdrawRecipient !== null && <WithdrawDialog wallet={browserWallet} recipient={withdrawRecipient} close={() => setWithdrawRecipient(null)} refresh={refreshTradeState} />}
    {rpcDialog}
    {browserWallet && ordersOpen && <StrategyOrders wallet={browserWallet} trades={tradeState} close={() => setOrdersOpen(false)} refresh={refreshTradeState} review={reviewStrategyOrder} />}
    {browserWallet && walletOpen && <StrategyControls session={browserWallet} close={() => { setWalletOpen(false); refresh() }} />}
    {browserWallet && walletSessionUnlockOpen && <WalletSessionUnlock session={browserWallet} close={() => { setWalletSessionUnlockOpen(false); setPendingSigningAction(null) }} unlocked={resumeWalletSigning} />}
    {welcomeMode && runnerVersion && <WelcomeFlow key={`${welcomeMode}:${runnerVersion}:${browserWallet ? 'wallet' : 'research'}`} mode={welcomeMode} version={runnerVersion} note={releaseNote} noteUrl={releaseNoteUrl} wallet={browserWallet} dismiss={dismissWelcome} changeWallet={browserWallet ? () => { void lockBrowserWallet() } : connectWallet} />}
  </div></WalletWorkspaceScope.Provider>
}
