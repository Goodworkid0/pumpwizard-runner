import { useMemo, useState } from 'react'
import type { WalletSession } from './browserWallet'
import './welcome-flow.css'

export type RunnerReleaseNote = {
  id: string
  date: string
  kind: 'patch-notes'
  status: 'draft' | 'published'
  title: string
  summary: string
  paragraphs: string[]
  bullets: string[]
  links: { label: string; href: string }[]
}

type WelcomePage = 'changes' | 'tips' | 'community'

const PROJECT_LINKS = [
  { label: 'PumpWizard project', detail: 'Product overview', href: 'https://it.pumpwizard.fun' },
  { label: 'Community', detail: 'Updates and patch notes', href: 'https://pumpwizard.fun/community' },
  { label: 'X · @PumpWizardLab', detail: 'Project updates', href: 'https://x.com/PumpWizardLab' },
  { label: 'Source', detail: 'Runner repository', href: 'https://github.com/Goodworkid0/pumpwizard-runner' },
  { label: 'Documentation', detail: 'Runner guides', href: 'https://github.com/Goodworkid0/pumpwizard-runner/tree/main/docs' },
  { label: 'Issues', detail: 'Report a problem', href: 'https://github.com/Goodworkid0/pumpwizard-runner/issues' },
] as const

const TIPS = [
  { title: 'Configure exact rules', body: 'A strategy is immutable rules. A session is the workspace that observes those rules.' },
  { title: 'Start explicitly', body: 'Opening Research never starts collection. Review the session and start monitoring yourself.' },
  { title: 'Check coverage', body: 'Read freshness, gaps and unknown costs before interpreting any modeled result.' },
  { title: 'Connect only when needed', body: 'Holdings and wallet actions request a connection at their boundary. Research does not.' },
] as const

type WelcomeHighlight = {
  title?: string
  body: string
}

const BUILD_HIGHLIGHTS: Record<string, readonly WelcomeHighlight[]> = {
  '0.2.0.dev62': [
    {
      title: 'Open Pump.fun faster',
      body: 'Continue to the Dashboard as soon as your wallet is ready.',
    },
    {
      title: 'Review local evidence',
      body: 'Compare retained session results before you create a new draft.',
    },
    {
      title: 'Keep Autopilot guarded',
      body: 'Review a generated-wallet allowance while live activation stays blocked.',
    },
  ],
}

export function WelcomeFlow({
  mode,
  version,
  note,
  noteUrl,
  wallet,
  dismiss,
  changeWallet,
}: {
  mode: 'introduction' | 'update'
  version: string
  note: RunnerReleaseNote | null
  noteUrl: string
  wallet: WalletSession | null
  dismiss: () => void
  changeWallet: () => void
}) {
  const pages = useMemo<WelcomePage[]>(() => mode === 'update' ? ['changes'] : ['changes', 'tips', 'community'], [mode])
  const [pageIndex, setPageIndex] = useState(0)
  const page = pages[pageIndex]
  const highlights: readonly WelcomeHighlight[] = BUILD_HIGHLIGHTS[version]
    || note?.bullets.slice(0, 3).map((body) => ({ body }))
    || []
  const lastPage = pageIndex === pages.length - 1
  const releaseStatus = note?.status === 'published'
    ? 'Published Community note'
    : 'Local development · Draft Community note'

  return <div className="welcome-backdrop">
    <section className="welcome-dialog" role="dialog" aria-modal="true" aria-labelledby="welcome-title">
      <header className="welcome-header">
        <div className="welcome-header-actions">
          <span>{wallet?.name || 'Read-only research mode'}</span>
          <button type="button" onClick={changeWallet}>{wallet ? 'Change wallet' : 'Connect wallet'}</button>
        </div>
      </header>

      <div className="welcome-body">
        <aside className="welcome-mascot" aria-label="PumpWizard mascot">
          <img src="/pumpwizard-relaxed-v1.png" alt="" />
          <div className="welcome-mascot-copy">
            <strong>When Pump meets a Wizard, magic happens.</strong>
            <span>Local evidence first. Wallet optional until needed.</span>
            <a href="https://x.com/PumpWizardLab" target="_blank" rel="noreferrer">@PumpWizardLab on X <i>↗</i></a>
          </div>
        </aside>

        <div className="welcome-content">
          <nav className="welcome-progress" aria-label="Introduction pages">
            {pages.map((item, index) => <button
              type="button"
              key={item}
              className={index === pageIndex ? 'active' : index < pageIndex ? 'complete' : ''}
              aria-current={index === pageIndex ? 'step' : undefined}
              onClick={() => setPageIndex(index)}
            ><i>{index < pageIndex ? '✓' : index + 1}</i><span>{item === 'changes' ? "What's new" : item === 'tips' ? 'Usage tips' : 'Community'}</span></button>)}
          </nav>

          {page === 'changes' && <section className="welcome-page welcome-changes">
            <div className="welcome-kicker"><span>{releaseStatus}</span><div><b>v{version}</b><time>{note?.date || ''}</time></div></div>
            <h1 id="welcome-title">{mode === 'update' ? `Runner updated to v${version}` : 'Welcome to PumpWizard'}</h1>
            <h2>What&apos;s new in Runner v{version}</h2>
            <p className="welcome-lead">{note?.summary || 'The local patch note for this development build is temporarily unavailable.'}</p>
            {highlights.length > 0 && <ul className="welcome-highlights">{highlights.map((item, index) => <li key={`${item.title || 'update'}-${index}`}>
              {item.title && <strong>{item.title}</strong>}
              <span>{item.body}</span>
            </li>)}</ul>}
            <div className="welcome-release-link">
              <a href={noteUrl} target="_blank" rel="noreferrer">Open the matching Community page ↗</a>
              {note?.status !== 'published' && <small>This bundled note is a draft. The matching Community article has not been verified as published.</small>}
            </div>
          </section>}

          {page === 'tips' && <section className="welcome-page welcome-tips">
            <small className="welcome-section-label">FIRST RESEARCH TASK</small>
            <h1 id="welcome-title">Four checks before you interpret a result</h1>
            <p className="welcome-lead">A useful first task ends with evidence and its limits—not a deposit or a promise of profit.</p>
            <div className="welcome-tip-grid">{TIPS.map((tip, index) => <article key={tip.title}><span>{index + 1}</span><div><h2>{tip.title}</h2><p>{tip.body}</p></div></article>)}</div>
            <p className="welcome-route-note"><b>Suggested route:</b> Research → create or choose a monitoring session → start collection → inspect coverage and decisions → export, continue, pause or stop.</p>
          </section>}

          {page === 'community' && <section className="welcome-page welcome-community">
            <small className="welcome-section-label">PROJECT LINKS</small>
            <h1 id="welcome-title">Stay close to the source</h1>
            <p className="welcome-lead">Use the existing project pages for documentation, release context and issue reports.</p>
            <div className="welcome-link-grid">{PROJECT_LINKS.map((link) => <a href={link.href} target="_blank" rel="noreferrer" key={link.href}><span><b>{link.label}</b><small>{link.detail}</small></span><i>↗</i></a>)}</div>
            <p className="welcome-route-note">Pump.fun is selected for this workspace. Opening Research does not start monitoring, allocate funds, authorize a strategy or grant signing access.</p>
          </section>}

          <footer className="welcome-footer">
            <button type="button" className="welcome-skip" onClick={dismiss}>Skip</button>
            <span>{pageIndex + 1} / {pages.length}</span>
            <div>
              {pageIndex > 0 && <button type="button" onClick={() => setPageIndex((current) => current - 1)}>Back</button>}
              {lastPage
                ? <button type="button" className="primary" onClick={dismiss}>{wallet ? 'Open Dashboard' : 'Open Research'}</button>
                : <button type="button" className="primary" onClick={() => setPageIndex((current) => current + 1)}>Next</button>}
            </div>
          </footer>
        </div>
      </div>
    </section>
  </div>
}
