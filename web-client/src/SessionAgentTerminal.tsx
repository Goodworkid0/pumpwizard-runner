import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import type { PortfolioSession, SessionWorkspace } from './PortfolioWorkspace'
import { api, errorText, inspectWallet } from './browserWallet'
import type { WalletSession } from './browserWallet'
import type { ServiceConnectionState } from './serviceControl'
import { appendBoardEvents, appendStatusChanges } from './commandBoardLog'
import type { BoardEvent, BoardLog } from './commandBoardLog'
import { CommandBoardPanel } from './CommandBoardPanel'
import { WalletIdentity } from './WalletIdentity'

type AgentProfile = {
  id: string
  label: string
  model: string
  key_available: boolean
}

type ConversationSummary = {
  id: string
  title: string
  updated: number
}

type ChatMessage = {
  id: string
  role: 'user' | 'assistant' | 'notice'
  content: string
  at: number
}

type PreparedRequest = {
  request_id: string
  conversation?: string
  estimated_input: number
  output_cap?: number
  preview: string
  expires_in_seconds: number
}

type BoardCheck = {
  id: string
  label: string
  status: 'checking' | 'ready' | 'info' | 'attention'
  headline: string
  detail: string
}

type BoardCheckResult = Pick<BoardCheck, 'status' | 'headline' | 'detail'>

type AgentActivity = {
  session: PortfolioSession
  state: string
  detail: string
  at: number | null
  tone: 'active' | 'attention' | 'idle'
}

const CHECKS: (Pick<BoardCheck, 'id' | 'label'> & { loading: string })[] = [
  { id: 'runner', label: 'Runner startup', loading: 'Loading the local workspace and build identity…' },
  { id: 'diagnostics', label: 'Local storage', loading: 'Checking the database, scheduler and execution journal…' },
  { id: 'rpc', label: 'RPC sync test', loading: 'Verifying Mainnet identity and reading a current blockhash…' },
  { id: 'wallet', label: 'Wallets', loading: 'Loading connected and locally recorded wallet state…' },
  { id: 'market', label: 'Market cache', loading: 'Loading the latest retained market evidence…' },
  { id: 'sessions', label: 'Sessions', loading: 'Restoring sessions, monitors and last scan state…' },
  { id: 'assistant', label: 'AI provider', loading: 'Checking optional provider configuration without contacting it…' },
]

function ago(value: number) {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - value))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

function formatSolBalance(lamports: string) {
  const value = Number(lamports) / 1_000_000_000
  if (!Number.isFinite(value)) return 'Balance unavailable'
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 4 })} SOL`
}

function statusLabel(status: BoardCheck['status']) {
  if (status === 'ready') return 'Available'
  if (status === 'info') return 'Info'
  if (status === 'attention') return 'Action needed'
  return 'Checking'
}

function activityFor(session: PortfolioSession): AgentActivity {
  const lastRun = session.health.last_run
  if (session.tracking.fault) {
    return {
      session,
      state: 'Needs attention',
      detail: session.tracking.fault,
      at: lastRun === 'never' ? null : lastRun.finished_at,
      tone: 'attention',
    }
  }
  if (!session.tracking.enabled || session.instance.status !== 'monitoring') {
    return {
      session,
      state: session.state.label,
      detail: session.state.reason,
      at: lastRun === 'never' ? null : lastRun.finished_at,
      tone: session.state.tone === 'warning' ? 'attention' : 'idle',
    }
  }
  if (lastRun === 'never') {
    return {
      session,
      state: 'Starting',
      detail: 'Monitor is enabled and waiting for its first completed scan.',
      at: null,
      tone: 'active',
    }
  }
  const observations = lastRun.observations ?? 0
  const decisions = lastRun.decisions ?? 0
  const progress = `${observations} observations · ${decisions} decisions`
  const warning = session.state.tone === 'warning'
  const detail = [progress, lastRun.detail, warning ? session.state.reason : ''].filter(Boolean).join(' · ')
  return {
    session,
    state: lastRun.status === 'failed' ? 'Scan failed' : warning ? session.state.label : 'Scan complete',
    detail,
    at: lastRun.finished_at,
    tone: lastRun.status === 'failed' || warning ? 'attention' : 'active',
  }
}

export function SessionAgentTerminal({ version, rpcRevision, profile, conversations, sessions, workspaces, wallet, service, setup, connectWallet, unlockSigning, configureRpc, openService, openGuide, signOut, openSession, workspaceHeader, walletSummary, assistantSettings }: {
  version: string
  rpcRevision: number
  profile: AgentProfile | null
  conversations: ConversationSummary[]
  sessions: PortfolioSession[]
  workspaces: SessionWorkspace[]
  wallet: WalletSession | null
  service: ServiceConnectionState
  setup: () => void
  connectWallet: () => void
  unlockSigning: () => void
  configureRpc: () => void
  openService: () => void
  openGuide: () => void
  signOut: () => void
  openSession: (id: string) => void
  workspaceHeader: ReactNode
  walletSummary: ReactNode
  assistantSettings: ReactNode
}) {
  const connected = Boolean(profile?.key_available)
  const walletAddress = wallet?.address
  const walletNetwork = wallet?.network
  const walletSigningAvailable = wallet?.signingAvailable
  const scopeQuery = wallet ? `?${new URLSearchParams({ sender: wallet.address, network: wallet.network })}` : '?scope=research' 
  const [statusLog, setStatusLog] = useState<BoardLog>({ entries: [], latest: {}, sequence: 0 })
  const [activitySince] = useState(() => Date.now())
  const followUpdates = useRef(true)
  const [checks, setChecks] = useState<BoardCheck[]>(() => CHECKS.map((item) => ({ id: item.id, label: item.label, status: 'checking', headline: 'Checking', detail: item.loading })))
  const [bootRevision, setBootRevision] = useState(0)
  const [conversation, setConversation] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState<PreparedRequest | null>(null)
  const [busy, setBusy] = useState(false)
  const [chatError, setChatError] = useState('')
  const [historyResolved, setHistoryResolved] = useState(false)
  const [historySelection, setHistorySelection] = useState('')
  const stream = useRef<HTMLDivElement>(null)
  const streamScroll = useRef(0)
  const retainStream = useCallback((node: HTMLDivElement | null) => {
    if (stream.current) streamScroll.current = stream.current.scrollTop
    stream.current = node
    if (node) node.scrollTop = followUpdates.current ? node.scrollHeight : streamScroll.current
  }, [])
  const activity = useMemo(() => sessions.map(activityFor).sort((left, right) => {
    const priority = { attention: 2, active: 1, idle: 0 }
    return priority[right.tone] - priority[left.tone] || (right.at || 0) - (left.at || 0)
  }), [sessions])
  const booting = checks.some((item) => item.status === 'checking') || service.phase === 'checking'
  const coreAttention = checks.filter((item) => item.id !== 'assistant' && item.status === 'attention')
  const serviceAttention = service.phase === 'disconnected' || service.phase === 'connected' && Boolean(service.restarted || service.identityMismatch || service.service?.health !== 'healthy' || service.service?.state !== 'running')
  const attentionCount = coreAttention.length + (serviceAttention ? 1 : 0)
  const rpcAttention = coreAttention.find((item) => item.id === 'rpc')
  const retryAttention = coreAttention.filter((item) => item.id !== 'rpc')
  const firstSessionAttention = activity.find((item) => item.tone === 'attention')
  const historyPending = connected && !historyResolved && !conversation && conversations.length > 0
  const runningSessions = sessions.filter((session) => session.tracking.enabled && session.instance.status === 'monitoring').length
  const sessionCount = workspaces.length + sessions.filter((session) => !session.instance.workspace_id).length
  const serviceJob = service.jobs.find((job) => job.id === 'strategy-monitor')
  const tradingActive = service.trading?.authorization_status === 'active' && service.trading.signer_state === 'unlocked'
  const latestCoverage = [...service.coverage].sort((left, right) => (right.received_at || right.started_at || 0) - (left.received_at || left.started_at || 0))[0]
  const serviceCardStatus: BoardCheck['status'] = service.phase === 'checking'
    ? 'checking'
    : service.phase === 'disconnected'
      ? 'info'
      : serviceAttention
        ? 'attention'
        : service.service?.health === 'healthy'
          ? 'ready'
          : 'info'
  const serviceCardHeadline = service.phase === 'checking'
    ? 'Checking service status'
    : service.phase === 'disconnected'
      ? 'Disconnected · state unknown'
      : service.restarted
        ? 'Restart detected · approvals cleared'
        : service.identityMismatch
          ? 'Dashboard and service do not match'
          : `Runner v${service.service?.version || 'unknown'} · ${service.service?.health || 'unknown'}`
  const serviceCardDetail = service.phase !== 'connected'
    ? service.error || 'No authenticated background-service response is available.'
    : `${service.identityMismatch ? `${service.identityMismatch} · ` : ''}${serviceJob?.last_error ? `${serviceJob.last_error} · ` : ''}${service.service?.instance_id ? `instance ${service.service.instance_id.slice(0, 8)}` : 'no active instance'} · collector ${serviceJob?.enabled ? 'enabled' : serviceJob ? 'disabled' : 'unknown'} · last success ${serviceJob?.last_success_at ? new Date(serviceJob.last_success_at * 1000).toLocaleString() : 'never'}${latestCoverage ? ` · ${latestCoverage.source || 'unknown source'}: ${latestCoverage.quote_count || 0} quotes, ${latestCoverage.gap_count || 0} gaps · ${latestCoverage.provider_diagnostics?.status || latestCoverage.diagnostics?.status || latestCoverage.status || 'unknown'}, latest source ${latestCoverage.latest_source_at ? new Date(latestCoverage.latest_source_at * 1000).toLocaleString() : 'unknown'}` : ' · no retained coverage'}`

  const updateCheck = useCallback((id: string, result: BoardCheckResult) => {
    setChecks((current) => {
      const previous = current.find((item) => item.id === id)
      if (previous?.status === result.status && previous.headline === result.headline && previous.detail === result.detail) return current
      return current.map((item) => item.id === id ? { ...item, ...result } : item)
    })
  }, [])

  useEffect(() => {
    const starting = sessions.filter((session) => session.tracking.enabled && session.health.last_run === 'never').length
    const attention = sessions.filter((session) => Boolean(session.tracking.fault) || session.state.tone === 'warning').length
    const shortfalls = workspaces.filter((workspace) => workspace.funding?.state === 'insufficient').length
    if (shortfalls) {
      updateCheck('sessions', { status: 'attention', headline: `${shortfalls} session${shortfalls === 1 ? '' : 's'} · entries paused`, detail: 'Low wallet SOL. Existing positions remain held; exit settings are unchanged. Review positions or add funds.' })
    } else if (attention) {
      updateCheck('sessions', { status: 'attention', headline: `${attention} need review`, detail: `${sessionCount} sessions · ${sessions.length} strategies · ${runningSessions} monitoring · ${starting} starting.` })
    } else if (!sessionCount) {
      updateCheck('sessions', { status: 'info', headline: 'No sessions yet', detail: 'Create a monitoring or trading session when you are ready.' })
    } else {
      updateCheck('sessions', { status: runningSessions ? 'ready' : 'info', headline: runningSessions ? `${runningSessions} strategies monitoring` : 'Sessions configured', detail: `${sessionCount} sessions · ${sessions.length} strategies · ${starting} starting · none need attention.` })
    }
  }, [runningSessions, sessionCount, sessions, workspaces, updateCheck])

  useEffect(() => {
    let current = true
    const run = async (id: string, task: () => Promise<BoardCheckResult>) => {
      try {
        const result = await task()
        if (current) updateCheck(id, result)
      } catch (error) {
        if (current) updateCheck(id, { status: 'attention', headline: 'Unavailable', detail: errorText(error) })
      }
    }
    void Promise.all([
      run('runner', async () => {
        const result = await api<{ version?: string; strategies: number; market_rows: number }>('/api/v1/workspace')
        return { status: 'ready', headline: result.version ? `Runner v${result.version} online` : 'Runner online', detail: `${result.strategies} strategies and ${result.market_rows} retained market rows loaded.` }
      }),
      run('diagnostics', async () => {
        const result = await api<{ report: { monitor?: { lease_active?: boolean }; journals?: Record<string, Record<string, number> | string> } }>('/api/v1/diagnostics')
        const integrity = result.report.journals?.integrity
        if (integrity === 'needs attention') throw new Error('The execution journal integrity check needs attention.')
        const authorizations = result.report.journals?.automation_authorizations
        const runningAutomation = typeof authorizations === 'object' && authorizations ? authorizations.running || 0 : 0
        const readyAutomation = typeof authorizations === 'object' && authorizations ? authorizations.authorized || 0 : 0
        const pausedAutomation = typeof authorizations === 'object' && authorizations ? authorizations.paused || 0 : 0
        const activeAutomation = runningAutomation + readyAutomation + pausedAutomation
        const schedulerRunning = Boolean(result.report.monitor?.lease_active)
        return { status: schedulerRunning ? 'ready' : 'info', headline: schedulerRunning ? 'Storage healthy' : 'Scheduler idle', detail: `Database and journal ready · ${activeAutomation} automation authorization${activeAutomation === 1 ? '' : 's'} · ${runningAutomation} running · ${pausedAutomation} paused.` }
      }),
      run('rpc', async () => {
        const result = await api<{ connection: { provider: string }; notice: string }>('/api/v1/rpc', { action: 'test' })
        return { status: 'ready', headline: 'Mainnet connected', detail: `${result.connection.provider} · ${result.notice}` }
      }),
      run('wallet', async () => {
        if (!walletAddress || !walletNetwork) return { status: 'info', headline: 'Wallet not connected', detail: 'Research, reports and read-only collection remain available. Connect only for holdings or wallet actions.' }
        const [result, state] = await Promise.all([
          inspectWallet(walletAddress, walletNetwork),
          api<{ wallets: { sender: string }[]; reservations: { status: string }[]; signing_sessions: { status: string }[] }>('/api/v1/wallet-state'),
        ])
        const known = new Set([walletAddress, ...state.wallets.map((item) => item.sender)]).size
        const allocations = state.reservations.filter((item) => item.status === 'held').length
        const signing = state.signing_sessions.filter((item) => item.status === 'active').length
        return { status: walletSigningAvailable ? 'ready' : 'info', headline: walletSigningAvailable ? 'Signing ready' : 'Signing locked', detail: `${formatSolBalance(result.balance)} · ${known} wallet${known === 1 ? '' : 's'} known · ${allocations} fund allocation${allocations === 1 ? '' : 's'} · ${signing} active signing session${signing === 1 ? '' : 's'}.` }
      }),
      run('market', async () => {
        const result = await api<{ rows: unknown[] }>('/api/v1/market?limit=1')
        return result.rows.length
          ? { status: 'ready', headline: 'Market data ready', detail: 'Retained market records are available for the dashboard and session scans.' }
          : { status: 'info', headline: 'Waiting for data', detail: 'The market API is online. No retained market records have arrived yet.' }
      }),
      run('sessions', async () => {
        const [result, workspaceResult] = await Promise.all([
          api<{ rows: PortfolioSession[] }>(`/api/v1/sessions${scopeQuery}`),
          api<{ rows: SessionWorkspace[] }>(`/api/v1/session-workspaces${scopeQuery}`),
        ])
        const running = result.rows.filter((session) => session.tracking.enabled && session.instance.status === 'monitoring').length
        const starting = result.rows.filter((session) => session.tracking.enabled && session.health.last_run === 'never').length
        const attention = result.rows.filter((session) => Boolean(session.tracking.fault) || session.state.tone === 'warning').length
        const saved = workspaceResult.rows.length + result.rows.filter((session) => !session.instance.workspace_id).length
        const shortfalls = workspaceResult.rows.filter((workspace) => workspace.funding?.state === 'insufficient').length
        if (shortfalls) return { status: 'attention', headline: `${shortfalls} session${shortfalls === 1 ? '' : 's'} · entries paused`, detail: 'Low wallet SOL. Existing positions remain held; exit settings are unchanged. Review positions or add funds.' }
        if (attention) return { status: 'attention', headline: `${attention} need review`, detail: `${saved} sessions · ${result.rows.length} strategies · ${running} monitoring · ${starting} starting.` }
        if (!saved) return { status: 'info', headline: 'No sessions yet', detail: 'Create a monitoring or trading session when you are ready.' }
        return { status: running ? 'ready' : 'info', headline: running ? `${running} strategies monitoring` : 'Sessions configured', detail: `${saved} sessions · ${result.rows.length} strategies · ${starting} starting · none need attention.` }
      }),
      run('assistant', async () => {
        const result = await api<{ profile: AgentProfile | null }>(`/api/v1/assistant${scopeQuery}`)
        if (!result.profile?.key_available) return { status: 'info', headline: 'Optional', detail: 'Connect a provider only if you want command-board chat. Trading and monitoring work without it.' }
        return { status: 'ready', headline: 'Chat ready', detail: `${result.profile.label} · ${result.profile.model} · every provider request still needs approval.` }
      }),
    ])
    return () => { current = false }
  }, [bootRevision, rpcRevision, scopeQuery, profile?.id, profile?.key_available, profile?.label, profile?.model, updateCheck, walletAddress, walletNetwork, walletSigningAvailable])

  const serviceFingerprint = JSON.stringify([
    service.phase, serviceCardStatus, serviceCardHeadline, service.error, service.identityMismatch,
    service.service?.instance_id, serviceJob?.enabled, serviceJob?.last_error,
    latestCoverage?.provider_diagnostics?.status || latestCoverage?.diagnostics?.status || latestCoverage?.status,
    latestCoverage?.gap_reason,
    service.trading?.signer_state, service.trading?.authorization_status, service.trading?.entries_paused,
  ])
  const snapshot = JSON.stringify([
    { id: 'service', label: 'Background service', status: serviceCardStatus, headline: serviceCardHeadline,
      detail: `${serviceCardDetail}${service.trading ? ` · trading ${service.trading.signer_state}, ${service.trading.authorization_status}${service.trading.entries_paused ? ', entries paused' : ''}` : ''}`,
      fingerprint: serviceFingerprint },
    ...checks.filter((item) => item.id !== 'runner' || item.status === 'attention' || statusLog.latest.runner)
      .map((item) => item.id === 'wallet' && wallet && ['ready', 'info'].includes(item.status)
        ? { ...item, detail: item.detail.replace(/^[^·]+ SOL/, formatSolBalance(wallet.balance)) }
        : item),
  ])
  const initialReady = !booting && service.phase !== 'checking'
  useEffect(() => {
    if (!initialReady) return
    setStatusLog((current) => appendStatusChanges(current, JSON.parse(snapshot), Date.now()))
  }, [initialReady, snapshot])

  const activitySnapshot = JSON.stringify([
    ...workspaces.filter((workspace) => workspace.created_at * 1000 >= activitySince).map((workspace): BoardEvent => ({
      id: `workspace-created:${workspace.id}`, at: workspace.created_at * 1000,
      label: 'Session', status: 'info', headline: 'Session created',
      detail: `${workspace.label} · ${workspace.mode === 'monitoring' ? 'Monitor' : 'Trading'}`,
    })),
    ...sessions.filter((session) => session.brief.created_at * 1000 >= activitySince).map((session): BoardEvent => ({
      id: `strategy-added:${session.instance.id}`, at: session.brief.created_at * 1000,
      sessionId: session.instance.id, label: 'Strategy', status: 'info', headline: 'Strategy added',
      detail: `${session.instance.label}${workspaces.find((workspace) => workspace.id === session.instance.workspace_id)?.label ? ` · ${workspaces.find((workspace) => workspace.id === session.instance.workspace_id)?.label}` : ''}`,
    })),
  ].sort((left, right) => left.at - right.at))
  useEffect(() => {
    if (initialReady) setStatusLog((current) => appendBoardEvents(current, JSON.parse(activitySnapshot)))
  }, [activitySnapshot, initialReady])

  useEffect(() => {
    if (!initialReady) return
    let cancelled = false
    let cursor: number | undefined
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      let delay = 5000
      try {
        const result = await api<{ rows: { id: string; session_id: string; strategy: string; workspace: string | null; at: number; kind: string; mint: string; symbol: string; reason: string }[]; cursor: number; has_more: boolean }>(`/api/v1/session-activity${scopeQuery}${cursor === undefined ? '' : `&after=${cursor}`}`)
        if (cancelled) return
        const events: BoardEvent[] = result.rows.filter((item) => cursor !== undefined || item.at * 1000 >= activitySince).map((item) => ({
          id: item.id, at: item.at * 1000, sessionId: item.session_id,
          label: item.strategy, status: 'info',
          headline: `${item.kind === 'entry_recommended' ? 'Entry signal' : 'Entry skipped'} · ${item.symbol || `${item.mint.slice(0, 6)}…${item.mint.slice(-4)}`}`,
          detail: `${item.workspace ? `${item.workspace} · ` : ''}${item.reason.replace(/_/g, ' ')} · Strategy decision.`,
        }))
        setStatusLog((current) => appendBoardEvents(current, events))
        cursor = result.cursor
        if (result.has_more) delay = 100
      } catch {
        // Keep the cursor and retry; a failed read must not discard events.
      }
      if (!cancelled) timer = setTimeout(() => { void poll() }, delay)
    }
    void poll()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [activitySince, initialReady, scopeQuery, service.service?.instance_id])

  useEffect(() => {
    if (stream.current && followUpdates.current) stream.current.scrollTop = stream.current.scrollHeight
  }, [statusLog.sequence])

  const loadMessages = useCallback(async (id: string) => {
    const result = await api<{ conversation: string; messages: ChatMessage[] }>(`/api/v1/assistant/conversations/${encodeURIComponent(id)}${scopeQuery}`)
    setConversation(result.conversation)
    setMessages(result.messages)
  }, [scopeQuery])

  useEffect(() => {
    setConversation(null)
    setMessages([])
    setPending(null)
    setHistoryResolved(false)
    setHistorySelection('')
    setChatError('')
  }, [profile?.id])

  useEffect(() => {
    if (!stream.current || (!messages.length && !pending)) return
    stream.current.scrollTop = stream.current.scrollHeight
  }, [messages, pending])

  const prepare = async (event: FormEvent) => {
    event.preventDefault()
    if (!draft.trim() || busy || pending) return
    setBusy(true)
    setChatError('')
    try {
      const result = await api<PreparedRequest>(`/api/v1/assistant/prepare${scopeQuery}`, {
        conversation,
        message: draft.trim(),
        max_output: 1200,
      })
      setConversation(result.conversation || null)
      setPending(result)
      setDraft('')
      if (result.conversation) await loadMessages(result.conversation)
    } catch (error) {
      setChatError(errorText(error))
    } finally {
      setBusy(false)
    }
  }

  const approve = async () => {
    if (!pending || busy) return
    const request = pending
    setBusy(true)
    setChatError('')
    try {
      const result = await api<{ conversation: string; context_request: PreparedRequest | null }>(`/api/v1/assistant/approve${scopeQuery}`, { request_id: request.request_id })
      await loadMessages(result.conversation)
      setPending(result.context_request ? { ...result.context_request, conversation: result.conversation } : null)
    } catch (error) {
      setPending(null)
      setChatError(errorText(error))
      if (request.conversation) await loadMessages(request.conversation).catch(() => undefined)
    } finally {
      setBusy(false)
    }
  }

  const decline = async () => {
    if (!pending || busy) return
    const request = pending
    setBusy(true)
    setChatError('')
    try {
      const result = await api<{ conversation: string }>(`/api/v1/assistant/decline${scopeQuery}`, { request_id: request.request_id })
      setPending(null)
      await loadMessages(result.conversation)
    } catch (error) {
      setChatError(errorText(error))
    } finally {
      setBusy(false)
    }
  }

  const newChat = () => {
    if (busy || pending) return
    setConversation(null)
    setMessages([])
    setDraft('')
    setChatError('')
    setHistoryResolved(true)
  }

  const readHistory = async () => {
    const selected = historySelection || conversations[0]?.id
    if (!selected || busy) return
    setBusy(true)
    setChatError('')
    try {
      await loadMessages(selected)
      setHistoryResolved(true)
    } catch (error) {
      setChatError(errorText(error))
    } finally {
      setBusy(false)
    }
  }

  const boardTone = booting ? 'checking' : attentionCount ? 'attention' : 'ready'
  const runningVersion = service.service?.version || version
  const runnerLabel = runningVersion ? `Runner v${runningVersion}` : 'Runner'
  const runnerReady = checks.find((item) => item.id === 'runner')?.status === 'ready'
  const rpcReady = checks.find((item) => item.id === 'rpc')?.status === 'ready'
  const serviceHealth = service.phase === 'disconnected' ? 'offline' : service.service?.state !== 'running' ? service.service?.state || 'unknown' : service.service?.health || 'unknown'
  const boardHeadline = booting ? `${runnerLabel} · Checking`
    : !runnerReady ? `${runnerLabel} · Unavailable`
      : service.identityMismatch ? `${runnerLabel} · Version mismatch`
        : service.phase === 'disconnected' ? `${runnerLabel} · Service offline`
          : service.service?.state !== 'running' ? `${runnerLabel} · Service ${service.service?.state || 'unknown'}`
            : `${runnerLabel} · Running`
  const boardDetail = booting
    ? 'Checking local storage, RPC and workspace status'
    : [
      `Service ${serviceHealth}`,
      rpcReady ? 'RPC verified' : 'RPC needs attention',
      tradingActive ? 'Trading active' : '',
      service.restarted ? 'Restart needs review' : '',
      service.identityMismatch ? 'Dashboard and service do not match' : '',
      coreAttention.length ? `${coreAttention.length} check${coreAttention.length === 1 ? ' needs' : 's need'} attention: ${coreAttention.map((item) => item.label).join(', ')}` : '',
    ].filter(Boolean).join(' · ')
  return <section className="assistant-header command-board" aria-label="System and optional AI">
    <div className={`agent-terminal ${connected ? 'connected' : 'disconnected'}`}>
      <header className="agent-terminal-bar">
        <div className="command-board-identity">
          <div className="command-board-mascot" aria-hidden="true">
            <img src="/pumpwizard-relaxed-v1.png" alt="" width={1254} height={1254} draggable={false} />
          </div>
          <div className="command-board-title">
            <small>LOCAL COMMAND BOARD</small>
            <div className="command-board-brand-heading"><h1>Pump<span>Wizard</span></h1>{workspaceHeader}</div>
            <p>Your wallet, portfolio and sessions in one dashboard.</p>
          </div>
        </div>
        <button type="button" className="command-board-overview" data-state={boardTone} aria-label="System status and service controls" aria-description={`${boardHeadline}. ${boardDetail}`} title="View service health, monitoring and trading controls" onClick={openService}><i aria-hidden="true" /><span><small>SYSTEM STATUS</small><b>{boardHeadline}</b><em>{boardDetail}</em></span></button>
        <WalletIdentity wallet={wallet} connectWallet={connectWallet} unlockSigning={unlockSigning} />
        <div className={`command-board-wallet-tools${wallet ? '' : ' no-wallet'}`} role="group" aria-label="Wallet tools and guides">
        {walletSummary}
        <button type="button" className="command-board-guides" aria-label="Guides" onClick={openGuide}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 5.5C9 3.5 5.5 3.5 2.5 5v14c3-1.5 6.5-1.5 9.5.5 3-2 6.5-2 9.5-.5V5c-3-1.5-6.5-1.5-9.5.5Z" /><path d="M12 5.5v14M6 8h2.5M6 11h2.5M15.5 8H18M15.5 11H18" /></svg>
          <span><b>Guides</b><small>Explore PumpWizard</small></span>
        </button>
        {wallet && <button type="button" className="command-board-signout" aria-label="Sign out of wallet" title="Sign out and lock wallet signing in this browser" onClick={signOut}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M10 4H4v16h6M14 8l4 4-4 4M8 12h12" /></svg><span><b>Sign out</b><small>Lock wallet</small></span>
        </button>}
        </div>
      </header>
      <CommandBoardPanel settings={assistantSettings} scope={wallet ? `${wallet.name} · ${wallet.address.slice(0, 6)}…${wallet.address.slice(-4)}` : 'Research workspace'}>
        <div className="command-board-activity-actions" role="region" aria-label="System activity actions">
          <div className="command-board-signals" aria-label="Actionable system signals">
            {tradingActive && <button className="command-signal attention" onClick={openService}>Pause or stop service</button>}
            {booting && <span className="command-signal checking"><i aria-hidden="true" />Checking connections</span>}
            {!booting && rpcAttention && <button className="command-signal attention" title={rpcAttention.detail} onClick={configureRpc}><i aria-hidden="true" />Fix RPC connection</button>}
            {!booting && retryAttention.length > 0 && <button className="command-signal attention" title={retryAttention.map((item) => `${item.label}: ${item.detail}`).join('\n')} onClick={() => setBootRevision((value) => value + 1)}><i aria-hidden="true" />Retry startup</button>}
            {!booting && firstSessionAttention && <button className="command-signal attention" onClick={() => openSession(firstSessionAttention.session.instance.id)}><i aria-hidden="true" />Review session</button>}
            {!booting && attentionCount === 0 && (!wallet || wallet.signingAvailable) && !firstSessionAttention && connected && <span className="command-signal ready"><i aria-hidden="true" />Checks available</span>}
          </div>
        </div>

      <div className="command-board-stream" ref={retainStream} onScroll={() => { const node = stream.current; if (node) followUpdates.current = node.scrollHeight - node.scrollTop - node.clientHeight < 40 }}>
        <div className="command-board-status-grid" role="log" aria-label="Status history" aria-live="polite" aria-relevant="additions">
          {!statusLog.entries.length && <p className="command-status-loading">Loading initial status…</p>}
          {statusLog.entries.map((item) => {
            const current = statusLog.latest[item.id]?.sequence === item.sequence
            const content = <><span className="command-status-badge"><i aria-hidden="true" />{statusLabel(item.status)}<time dateTime={new Date(item.at).toISOString()} title={new Date(item.at).toLocaleString()}>{new Date(item.at).toLocaleTimeString()}</time></span><h2>{item.label}</h2><p><strong>{item.headline}</strong><span>{item.detail}</span></p></>
            return item.id === 'service'
              ? <button className="command-status-card command-status-action" data-status={item.status} data-check-id={item.id} data-sequence={item.sequence} data-current={current} key={item.sequence} onClick={() => { window.focus(); openService() }} aria-label={current ? 'Open background service controls' : 'Open service controls from earlier update'}>{content}</button>
              : item.sessionId
                ? <button className="command-status-card command-status-action" data-status={item.status} data-check-id={item.id} data-sequence={item.sequence} data-current={current} key={item.sequence} title={item.detail} onClick={() => { window.focus(); openSession(item.sessionId!) }} aria-label={`Open ${item.label}: ${item.headline}`}>{content}</button>
              : <article className="command-status-card" data-status={item.status} data-check-id={item.id} data-sequence={item.sequence} data-current={current} key={item.sequence}>{content}</article>
          })}
        </div>
        {historyPending && <section className="command-history" role="region" aria-label="Previous chat history">
          <span aria-hidden="true">history</span><div><b>I found {conversations.length} previous local chat{conversations.length === 1 ? '' : 's'}.</b><p>Would you like me to read one here, or start with a clean conversation? Opening history stays local until you approve a provider request.</p></div>
          <select aria-label="Previous conversation" value={historySelection || conversations[0].id} onChange={(event) => setHistorySelection(event.target.value)}>{conversations.map((item) => <option value={item.id} key={item.id}>{item.title} · {new Date(item.updated * 1000).toLocaleString()}</option>)}</select>
          <button onClick={readHistory} disabled={busy}>{busy ? 'Reading…' : 'Read selected chat'}</button><button onClick={newChat} disabled={busy}>Start fresh</button>
        </section>}
        {connected && messages.map((message) => <article className={`command-message ${message.role}`} key={message.id}>
          <time dateTime={new Date(message.at * 1000).toISOString()}>{ago(message.at)}</time><i /><b>{message.role === 'assistant' ? profile?.label : message.role === 'user' ? 'You' : 'Runner'}</b><p>{message.content}</p>
        </article>)}
        {!connected && <div className="command-connect"><span aria-hidden="true">›_</span><p>Connect an AI provider to chat.</p><button onClick={setup}>Add API key</button></div>}
      </div>
      {pending && <div className="command-approval" role="region" aria-label="Approve AI request">
        <div><small>PROVIDER APPROVAL REQUIRED</small><b>{pending.preview}</b><span>Estimated input {pending.estimated_input.toLocaleString()} tokens · output cap {pending.output_cap?.toLocaleString() || 'provider default'} · expires in {Math.round(pending.expires_in_seconds / 60)} min</span></div>
        <button onClick={decline} disabled={busy}>Cancel</button><button className="primary" onClick={approve} disabled={busy}>{busy ? 'Sending…' : 'Approve & send once'}</button>
      </div>}
      {connected && <form className="command-chat" onSubmit={prepare}>
        <span aria-hidden="true">›_</span><input aria-label="Assistant message" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder={historyPending ? 'Choose a previous chat or start fresh…' : `Ask ${profile?.label} about markets, sessions, or previous decisions…`} disabled={busy || Boolean(pending) || historyPending} /><button type="submit" disabled={busy || Boolean(pending) || historyPending || !draft.trim()}>{busy && !pending ? 'Working…' : 'Review send'}</button>
        <div className="command-chat-actions" role="group" aria-label="Chat controls">
          <button type="button" className="command-chat-tool" aria-label="New chat" title="New chat" onClick={newChat} disabled={busy || Boolean(pending)}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11v7a2 2 0 0 1-2 2H6l-4 2V4a2 2 0 0 1 2-2h9M18 2v8m-4-4h8" /></svg></button>
          <button type="button" className="command-chat-tool" aria-label="AI settings" title={`AI settings · ${profile?.label} · ${profile?.model}`} onClick={setup}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h4m4 0h10M3 12h10m4 0h4M3 18h4m4 0h10" /><circle cx="9" cy="6" r="2" /><circle cx="15" cy="12" r="2" /><circle cx="9" cy="18" r="2" /></svg></button>
        </div>
      </form>}
      {chatError && <div className="command-chat-error" role="alert">{chatError}</div>}
      </CommandBoardPanel>
    </div>
  </section>
}
