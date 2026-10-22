import { useState } from 'react'
import type { PortfolioSession } from './PortfolioWorkspace'
import { HoldingTokenIcon } from './HoldingVisuals'
import { ActivityPager, ActivityRangeControl, CopyActivityValue } from './ActivityControls'
import { DecisionMarketChart } from './ActivityCharts'
import { activityTime, activityUsd, csvText, decisionAmount, decisionLabel, decisionReason, downloadCsv, inActivityRange, latestDecisions, validTime } from './activityData'
import type { ActivityRange } from './activityData'
import './activity-workspace.css'

type Props = ({ session: PortfolioSession; sessions?: never } | { sessions: PortfolioSession[]; session?: never }) & { openToken?: (mint: string) => void }

export function SessionObservations({ session, sessions: multiple, openToken }: Props) {
  const sessions = multiple || (session ? [session] : [])
  const [filter, setFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [reason, setReason] = useState('all')
  const [strategy, setStrategy] = useState('all')
  const [range, setRange] = useState<ActivityRange>('all')
  const [sort, setSort] = useState('newest')
  const [requestedPage, setPage] = useState(0)
  const [selected, setSelected] = useState<string | null>(null)
  const [charts, setCharts] = useState(true)
  const decisions = latestDecisions(sessions), now = Date.now() / 1000, term = search.trim().toLowerCase()
  const base = decisions.filter((row) => inActivityRange(row.at, range, now) && (strategy === 'all' || row.strategyId === strategy)
    && (!term || [decisionLabel(row), row.observation?.metadata?.name, row.mint, row.reason.replace(/_/g, ' '), row.strategyLabel].join(' ').toLowerCase().includes(term)))
  const reasons = [...new Set(base.map(decisionReason))].map((label) => ({ label, count: base.filter((row) => decisionReason(row) === label).length })).sort((a, b) => b.count - a.count)
  const visible = base.filter((row) => (filter === 'all' || row.kind === filter) && (reason === 'all' || decisionReason(row) === reason)).sort((a, b) => {
    if (sort === 'cap' || sort === 'liquidity') {
      const field = sort === 'cap' ? 'market_cap_usd' : 'liquidity_usd', av = decisionAmount(a, field), bv = decisionAmount(b, field)
      if (av === null || bv === null) return av === bv ? b.at - a.at : av === null ? 1 : -1
      return bv - av || b.at - a.at
    }
    return (sort === 'oldest' ? 1 : -1) * ((validTime(a.at) ? a.at : 0) - (validTime(b.at) ? b.at : 0))
  })
  const size = 20, page = Math.min(requestedPage, Math.max(0, Math.ceil(visible.length / size) - 1))
  const selectedRow = visible.find((row) => row.id === selected)
  const plans = sessions.filter((row) => strategy === 'all' || row.instance.id === strategy).map((row) => row.monitor_plan)
  const plan = plans.length && plans.every((row) => row.entry_cap_min === plans[0].entry_cap_min && row.entry_cap_max === plans[0].entry_cap_max) ? plans[0] : null
  const newest = visible.reduce((at, row) => validTime(row.at) ? Math.max(at, row.at) : at, 0)
  const filtered = Boolean(term || filter !== 'all' || reason !== 'all' || strategy !== 'all' || range !== 'all')
  const clear = () => { setFilter('all'); setSearch(''); setReason('all'); setStrategy('all'); setRange('all'); setPage(0) }
  const choose = (id: string) => { setSelected(id); const index = visible.findIndex((row) => row.id === id); if (index >= 0) setPage(Math.floor(index / size)) }
  const exportRows = () => downloadCsv('pumpwizard-decisions.csv', csvText(
    ['Saved', 'Token', 'Mint', 'Strategy', 'Decision', 'Reason', 'Saved price USD', 'Saved market cap USD', 'Saved liquidity USD', 'Observation time'],
    visible.map((row) => [validTime(row.at) ? new Date(row.at * 1000).toISOString() : '', decisionLabel(row), row.mint, row.strategyLabel, row.kind === 'entry_recommended' ? 'Enter signal' : 'Do not enter', row.reason, row.observation?.price_usd ?? row.payload.price_usd, row.observation?.market_cap_usd ?? row.payload.market_cap_usd, row.observation?.liquidity_usd, row.observation && validTime(row.observation.observed_at) ? new Date(row.observation.observed_at * 1000).toISOString() : '']),
  ))
  return <section className="activity-workspace session-observations session-entry-observations" aria-label="Decisions workspace">
    <header className="activity-heading"><div><small>SAVED STRATEGY DECISIONS</small><h3>Entry decisions</h3><p>{sessions.length === 1 ? sessions[0].instance.label : `${sessions.length} strategies in this scope`} · latest saved stance per token</p></div><div className="activity-heading-actions"><button aria-pressed={charts} onClick={() => setCharts((value) => !value)}>{charts ? 'Hide charts' : 'Show charts'}</button><button onClick={exportRows} disabled={!visible.length}>Export decisions CSV</button></div></header>
    <div className="activity-summary"><article><small>Matching candidates</small><b>{visible.length}</b><span>Open positions excluded</span></article><article><small>Enter signals</small><b className="activity-positive">{visible.filter((row) => row.kind === 'entry_recommended').length}</b><span>Saved signal · no order implied</span></article><article><small>Do not enter</small><b className="activity-amber">{visible.filter((row) => row.kind === 'entry_blocked').length}</b><span>Reason recorded below</span></article><article><small>Latest saved</small><b className="activity-saved-time">{newest ? activityTime(newest, true) : 'Unavailable'}</b><span>Recorded decision time</span></article></div>
    <div className="activity-toolbar"><label className="activity-search"><span>Search decisions</span><input type="search" value={search} onChange={(event) => { setSearch(event.target.value); setPage(0) }} placeholder="Token, mint, reason or strategy" /></label><ActivityRangeControl label="Decision time range" value={range} change={(value) => { setRange(value); setPage(0) }} />{sessions.length > 1 && <label>Decision strategy<select value={strategy} onChange={(event) => { setStrategy(event.target.value); setPage(0) }}><option value="all">All strategies</option>{sessions.map((row) => <option key={row.instance.id} value={row.instance.id}>{row.instance.label}</option>)}</select></label>}<label>Decision reason<select value={reason} onChange={(event) => { setReason(event.target.value); setPage(0) }}><option value="all">All reasons</option>{[...new Set([...reasons.map((item) => item.label), ...(reason !== 'all' ? [reason] : [])])].map((item) => <option key={item} value={item}>{item}</option>)}</select></label></div>
    <div className="activity-filter-row"><div className="activity-segments observation-toolbar" role="group" aria-label="Observation filters">{([['all', 'All decisions'], ['entry_recommended', 'Enter'], ['entry_blocked', 'Do not enter']] as const).map(([id, label]) => <button key={id} aria-pressed={filter === id} onClick={() => { setFilter(id); setPage(0) }}>{label}<b>{id === 'all' ? base.length : base.filter((row) => row.kind === id).length}</b></button>)}</div><label>Sort decisions<select value={sort} onChange={(event) => { setSort(event.target.value); setPage(0) }}><option value="newest">Newest first</option><option value="oldest">Oldest first</option><option value="cap">Highest market cap</option><option value="liquidity">Highest liquidity</option></select></label>{filtered && <button className="activity-clear" onClick={clear}>Clear filters</button>}</div>
    {charts && decisions.length > 0 && <div className="activity-chart-layout"><DecisionMarketChart rows={visible} plan={plan} selected={selectedRow?.id || null} choose={choose} /><section className="activity-chart-card activity-breakdown" aria-label="Decision reason breakdown"><header><div><h4>Decision breakdown</h4><p>Why candidates entered or stayed out</p></div></header>{reasons.length ? reasons.map((item) => <button key={item.label} aria-pressed={reason === item.label} onClick={() => { setReason(reason === item.label ? 'all' : item.label); setPage(0) }}><span>{item.label}<b>{item.count}</b></span><i><em className={item.label === 'Enter signal' ? 'finalized' : 'awaiting'} style={{ width: `${item.count / base.length * 100}%` }} /></i></button>) : <p className="activity-chart-empty">No saved decisions match this scope.</p>}</section></div>}
    {selectedRow && <section className="activity-inspector decision-inspector" aria-label="Saved decision details"><header><div><small>{selectedRow.kind === 'entry_recommended' ? 'ENTER SIGNAL' : 'DO NOT ENTER'} · {selectedRow.strategyLabel}</small><h4>{decisionLabel(selectedRow)}</h4><p>{selectedRow.reason.replace(/_/g, ' ')}</p></div><div>{openToken && <button onClick={() => openToken(selectedRow.mint)}>Open token chart ↗</button>}<button onClick={() => setSelected(null)} aria-label="Close decision details">×</button></div></header><dl><div><dt>Saved price</dt><dd>{activityUsd(decisionAmount(selectedRow, 'price_usd'))}</dd></div><div><dt>Saved market cap</dt><dd>{activityUsd(decisionAmount(selectedRow, 'market_cap_usd'))}</dd></div><div><dt>Saved liquidity</dt><dd>{activityUsd(decisionAmount(selectedRow, 'liquidity_usd'))}</dd></div><div><dt>Decision time</dt><dd>{activityTime(selectedRow.at)}</dd></div><div><dt>Observation time</dt><dd>{selectedRow.observation ? activityTime(selectedRow.observation.observed_at) : 'Unavailable'}</dd></div></dl><div className="activity-identifiers"><span><small>MINT</small><code>{selectedRow.mint}</code><CopyActivityValue value={selectedRow.mint} kind="mint" /></span></div><p className="activity-inspector-note">These inputs were saved at or before the decision. The token chart may include later market data.</p></section>}
    <div className="activity-list-heading"><h4>Candidate decisions</h4><span>{visible.length} matching · {decisions.length} loaded</span></div>
    {visible.length ? <div className="activity-table-scroll session-entry-table"><table className="activity-record-table" aria-label="Session entry observations"><thead><tr>{['Token', ...(sessions.length > 1 ? ['Strategy'] : []), 'Decision', 'Reason', 'Saved price / cap', 'Saved liquidity', 'Saved'].map((name) => <th scope="col" key={name}>{name}</th>)}</tr></thead><tbody>{visible.slice(page * size, (page + 1) * size).map((row) => <tr key={row.id} className={`activity-decision-row ${selected === row.id ? 'activity-selected' : ''}`} data-strategy-id={row.strategyId} data-mint={row.mint}>
      <td data-label="Token"><div className="activity-token"><HoldingTokenIcon asset={{ mint: row.mint, name: row.observation?.metadata?.name || decisionLabel(row), symbol: decisionLabel(row), image_url: null }} /><span><button className="activity-token-link" aria-label={`Inspect ${decisionLabel(row)} decision`} onClick={() => choose(selected === row.id ? '' : row.id)}>{decisionLabel(row)}</button><small title={row.mint}>{row.mint.slice(0, 6)}…{row.mint.slice(-4)}</small></span></div></td>
      {sessions.length > 1 && <td data-label="Strategy"><b>{row.strategyLabel}</b></td>}
      <td data-label="Decision"><span className={`activity-badge ${row.kind === 'entry_recommended' ? 'finalized' : 'blocked'}`}>{row.kind === 'entry_recommended' ? 'Enter signal' : 'Do not enter'}</span></td>
      <td data-label="Reason"><span className="activity-reason-label">{decisionReason(row)}</span><small className="activity-full-reason">{row.reason.replace(/_/g, ' ')}</small></td>
      <td data-label="Saved price / cap"><b>{activityUsd(decisionAmount(row, 'price_usd'))}</b><small>{activityUsd(decisionAmount(row, 'market_cap_usd'))} cap</small></td>
      <td data-label="Saved liquidity"><b>{activityUsd(decisionAmount(row, 'liquidity_usd'))}</b></td>
      <td data-label="Saved"><time title={activityTime(row.at)}>{activityTime(row.at, true)}</time></td>
    </tr>)}</tbody></table></div> : <div className="activity-empty session-empty compact"><b>{decisions.length ? 'No decisions match these filters' : 'No session entry decisions yet'}</b><p>{decisions.length ? 'Adjust the search, reason or time range to see more candidates.' : 'Run the strategy monitor to collect observations and evaluate candidates.'}</p>{filtered && <button onClick={clear}>Show all decisions</button>}</div>}
    <ActivityPager page={page} count={visible.length} size={size} change={setPage} />
    <footer>Up to 12 latest candidates per strategy · held tokens excluded. Saved signals are separate from orders. CSV includes all matching rows across pages.</footer>
  </section>
}
