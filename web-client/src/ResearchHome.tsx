import type { PortfolioSession, SessionWorkspace } from './PortfolioWorkspace'
import type { MarketRow } from './TokenDetail'
import type { ServiceConnectionState } from './serviceControl'

function timeAgo(value: number | null | undefined) {
  if (!value) return 'not recorded'
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - value))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86_400) return `${Math.floor(seconds / 3_600)}h ago`
  return `${Math.floor(seconds / 86_400)}d ago`
}

export function ResearchHome({ sessions, workspaces, market, service, createSession, openSession, inspectResearch }: {
  sessions: PortfolioSession[]
  workspaces: SessionWorkspace[]
  market: MarketRow[]
  service: ServiceConnectionState
  createSession: () => void
  openSession: (id: string) => void
  inspectResearch: (id: string, label: string) => void
}) {
  const attention = sessions.find((session) => Boolean(session.tracking.fault) || session.state.tone === 'warning')
  const collecting = sessions.find((session) => session.tracking.enabled && !session.tracking.fault)
  const configured = sessions[0]
  const emptyWorkspace = workspaces.find((workspace) => workspace.strategy_count === 0)
  const collector = service.jobs.find((job) => job.id === 'strategy-monitor')
  const latestMarket = market.reduce<number | null>((latest, row) => latest === null || row.observed_at > latest ? row.observed_at : latest, null)
  const next = attention
    ? { eyebrow: 'RESOLVE A BLOCKER', title: `Review ${attention.instance.label}`, detail: attention.tracking.fault || attention.state.reason, action: 'Review session', run: () => openSession(attention.instance.id) }
    : collecting
      ? { eyebrow: 'COLLECTING EVIDENCE', title: `Inspect ${collecting.instance.label}`, detail: 'Review retained evidence, coverage and the next honest result.', action: 'Inspect evidence', run: () => inspectResearch(collecting.instance.id, collecting.instance.label) }
      : configured
        ? { eyebrow: 'START EXPLICITLY', title: `Start collecting for ${configured.instance.label}`, detail: 'Open the session, confirm its exact rules, then start monitoring. Navigation alone creates no evidence.', action: 'Open session', run: () => openSession(configured.instance.id) }
        : { eyebrow: 'FIRST USE', title: emptyWorkspace ? `Add rules to ${emptyWorkspace.label}` : 'Create a monitoring session', detail: emptyWorkspace ? 'The session exists but has no immutable strategy rules yet.' : 'Monitoring needs no wallet, deposit or AI provider and starts only after explicit confirmation.', action: emptyWorkspace ? 'Choose exact strategy' : 'Create monitoring session', run: createSession }

  return <main className="research-home workspace-page">
    <section className="research-hero" aria-labelledby="research-title">
      <div><small>READ-ONLY RESEARCH</small><h1 id="research-title">Understand one strategy before risking funds.</h1><p>Configure a session, collect bounded evidence, inspect data health, then export a report or keep an honest “not enough evidence” result.</p></div>
      <aside className="research-next-action"><small>{next.eyebrow}</small><h2>{next.title}</h2><p>{next.detail}</p><button className="primary" onClick={next.run}>{next.action}</button></aside>
    </section>

    <section className="research-health" aria-label="Research data health">
      <article><small>SERVICE PROCESS</small><b>{service.phase !== 'connected' ? 'Unknown' : service.service?.health === 'healthy' ? 'Available' : service.service?.health || 'Unknown'}</b><span>{service.phase === 'connected' ? `Heartbeat ${timeAgo(service.service?.heartbeat_at)}` : service.error || 'No authenticated service response'}</span></article>
      <article><small>COLLECTION</small><b>{collector?.enabled ? 'Collecting' : collector ? 'Paused' : 'Unknown'}</b><span>{collector?.enabled ? `Last success ${timeAgo(collector.last_success_at)}` : 'Opening Research does not start collection'}</span></article>
      <article><small>RETAINED MARKET</small><b>{market.length ? `${market.length} records` : 'No records'}</b><span>{latestMarket ? `Newest ${timeAgo(latestMarket)}` : 'Wait for real source-linked evidence'}</span></article>
      <article><small>RESEARCH SESSIONS</small><b>{workspaces.length || sessions.length ? `${workspaces.length || sessions.length} configured` : 'None configured'}</b><span>{sessions.filter((session) => session.tracking.enabled).length} collecting · {sessions.filter((session) => session.tracking.fault).length} degraded</span></article>
    </section>

    <section className="research-route" aria-label="Research journey">
      <header><small>ONE USEFUL ROUTE</small><h2>From observation to a reviewable result</h2><p>Each step preserves the selected session. Wallet and paid AI setup stay outside this route.</p></header>
      <ol><li><span>1</span><b>Configure</b><small>Choose immutable strategy rules</small></li><li><span>2</span><b>Collect</b><small>Start bounded observation explicitly</small></li><li><span>3</span><b>Inspect</b><small>Read coverage, decisions and gaps</small></li><li><span>4</span><b>Conclude</b><small>Export, continue, pause or stop</small></li></ol>
      <div className="research-route-actions"><button onClick={createSession}>New monitoring session</button>{configured && <button onClick={() => inspectResearch(configured.instance.id, configured.instance.label)}>Open latest research</button>}</div>
    </section>

    <details className="research-glossary"><summary>Terms used in this workspace</summary><dl><div><dt>Strategy</dt><dd>Immutable rules being observed.</dd></div><div><dt>Session</dt><dd>A configured workspace and its activity.</dd></div><div><dt>Campaign</dt><dd>A bounded evidence comparison.</dd></div><div><dt>Holding</dt><dd>Tokens the connected wallet owns.</dd></div><div><dt>Paper result</dt><dd>Modeled evidence, never a funded return.</dd></div><div><dt>Service</dt><dd>The local process that may continue after the browser closes.</dd></div></dl></details>
  </main>
}
