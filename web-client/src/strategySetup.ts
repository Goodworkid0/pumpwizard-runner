import type { PortfolioSession, SessionWorkspace } from './PortfolioWorkspace'
import type { LocalStrategy } from './StrategySelection'
import type { SessionPlan } from './SessionPlanDialog'
import type { ServiceConnectionState } from './serviceControl'
import { strategyOperatingState } from './dashboardData'

export function sessionStrategyAssociation(sessions: PortfolioSession[], workspaceId: string | undefined, digest: string) {
  if (!workspaceId) return null
  return sessions.find((row) => row.instance.workspace_id === workspaceId
    && (row.instance.strategy_digest === digest || row.brief.strategy_digest_at_creation === digest)) || null
}

export function groupSavedStrategyVersions(strategies: LocalStrategy[], sessions: PortfolioSession[], workspaceId?: string) {
  const groups = new Map<string, LocalStrategy[]>()
  for (const strategy of strategies) {
    const key = `${strategy.strategy_id}:${strategy.label}`
    groups.set(key, [...(groups.get(key) || []), strategy])
  }
  return [...groups.values()].map((versions) => versions.slice().sort((a, b) => {
    const current = (row: LocalStrategy) => Boolean(workspaceId) && sessions.some((session) => session.instance.workspace_id === workspaceId && session.instance.strategy_digest === row.digest)
    return Number(current(b)) - Number(current(a)) || String(b.imported_at || '').localeCompare(String(a.imported_at || '')) || a.digest.localeCompare(b.digest)
  }))
}

export function strategyTradingSetup({ strategy, session, workspace, plan, walletSigning, service }: {
  strategy: LocalStrategy | null
  session: PortfolioSession | null
  workspace: SessionWorkspace | null
  plan: SessionPlan | null | undefined
  walletSigning: boolean
  service?: ServiceConnectionState
}) {
  const rulesSupported = strategy?.compatibility === 'monitoring-ready'
  const trade = plan?.session_mode === 'trade'
  const now = Date.now() / 1000
  const funding = workspace?.funding
  const fundsFresh = funding?.checked_at != null && now - funding.checked_at >= 0 && now - funding.checked_at < 60
  const fundsReady = funding?.state === 'sufficient' && fundsFresh
  const active = Boolean(session && workspace && strategy && strategyOperatingState({ id: session.instance.id, label: session.instance.label, archived: false, session, workspace, performance: null }, service) === 'Trading')
  const heading = !plan ? 'Choose a session' : !trade ? 'Monitoring only'
    : !rulesSupported ? 'Strategy rules need review'
      : !session ? 'Add this strategy to the session'
        : funding?.state === 'insufficient' ? 'Session needs funds'
          : !fundsReady ? 'Check session funds'
            : active ? 'Trading active'
              : plan.execution_style === 'interactive' ? walletSigning ? 'Ready to review an order' : 'Unlock wallet signing'
                : 'Trading approval required'
  return { heading, trade, rulesSupported, fundsReady, active,
    approval: active ? 'Trading active' : plan?.execution_style === 'interactive' ? 'Approve each order in your wallet' : 'Approve limits and unlock the service signer',
    detail: !plan ? 'Add these saved rules to a monitoring or trading session.' : !trade ? 'This session collects observations. Choose a trading session to use wallet funds.'
      : active ? 'The matching service signer and trading authorization are active.'
        : 'Add and configure the strategy, confirm session funds, then approve trading in its controls.',
  }
}
