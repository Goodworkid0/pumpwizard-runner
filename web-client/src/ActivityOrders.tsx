import { Fragment, useState } from 'react'
import { HoldingTokenIcon } from './HoldingVisuals'
import type { MarketRow } from './TokenDetail'
import { receiptStatus, tokenQuantity } from './tradeFormatting'
import { ActivityPager, ActivityRangeControl, CopyActivityValue } from './ActivityControls'
import { OrderActivityChart } from './ActivityCharts'
import { activityTime, awaitingReceipt, csvText, downloadCsv, exactSol, inActivityRange, recordedFees, validTime } from './activityData'
import type { ActivityOrder, ActivityRange } from './activityData'
import './activity-workspace.css'

type StatusFilter = 'all' | 'awaiting' | 'finalized' | 'failed' | 'closed'

export function ActivityOrders({ orders, market, owner, strategyLabel, openToken, prepareOrder, available, error }: {
  orders: ActivityOrder[]; market: Map<string, MarketRow>; owner: (order: ActivityOrder) => string | null
  strategyLabel: (id: string) => string; openToken: (mint: string) => void
  prepareOrder: (() => void) | null; available: boolean; error: string
}) {
  const [search, setSearch] = useState('')
  const [range, setRange] = useState<ActivityRange>('all')
  const [status, setStatus] = useState<StatusFilter>('all')
  const [side, setSide] = useState('all')
  const [strategy, setStrategy] = useState('all')
  const [sort, setSort] = useState('newest')
  const [requestedPage, setPage] = useState(0)
  const [selected, setSelected] = useState<string | null>(null)
  const [charts, setCharts] = useState(true)
  const now = Date.now() / 1000, term = search.trim().toLowerCase()
  const label = (order: ActivityOrder) => market.get(order.mint)?.metadata?.name || market.get(order.mint)?.symbol || `${order.mint.slice(0, 6)}…${order.mint.slice(-4)}`
  const source = (order: ActivityOrder) => owner(order) ? strategyLabel(owner(order)!) : 'You'
  const choices = [...new Map(orders.map((order) => [owner(order) || 'manual', source(order)])).entries()]
  const base = orders.filter((order) => inActivityRange(order.created_at, range, now)
    && (side === 'all' || order.side === side) && (strategy === 'all' || (owner(order) || 'manual') === strategy)
    && (!term || [label(order), market.get(order.mint)?.symbol, order.mint, source(order), order.id, order.signature, order.route, order.detail].join(' ').toLowerCase().includes(term)))
  const statuses: { id: StatusFilter; label: string; count: number }[] = [
    { id: 'all', label: 'All orders', count: base.length },
    { id: 'awaiting', label: 'Awaiting receipt', count: base.filter((order) => awaitingReceipt(order.status)).length },
    { id: 'finalized', label: 'Finalized', count: base.filter((order) => order.status === 'finalized').length },
    { id: 'failed', label: 'Failed', count: base.filter((order) => order.status === 'failed').length },
    { id: 'closed', label: 'Cancelled / expired', count: base.filter((order) => ['cancelled', 'expired'].includes(order.status)).length },
  ]
  const visible = base.filter((order) => status === 'all' || (status === 'awaiting' ? awaitingReceipt(order.status) : status === 'closed' ? ['cancelled', 'expired'].includes(order.status) : order.status === status))
    .sort((a, b) => (sort === 'oldest' ? 1 : -1) * ((validTime(a.created_at) ? a.created_at : 0) - (validTime(b.created_at) ? b.created_at : 0)))
  const fees = recordedFees(visible), size = 20, page = Math.min(requestedPage, Math.max(0, Math.ceil(visible.length / size) - 1))
  const filtered = Boolean(term || range !== 'all' || status !== 'all' || side !== 'all' || strategy !== 'all')
  const clear = () => { setSearch(''); setRange('all'); setStatus('all'); setSide('all'); setStrategy('all'); setPage(0) }
  const exportRows = () => downloadCsv('pumpwizard-orders.csv', csvText(
    ['Submitted', 'Token', 'Mint', 'Strategy', 'Side', 'Status', 'Requested atoms', 'Token decimals', 'Fee lamports', 'Wallet delta lamports', 'Token delta atoms', 'Route', 'Signature', 'Order ID'],
    visible.map((order) => [validTime(order.created_at) ? new Date(order.created_at * 1000).toISOString() : '', label(order), order.mint, source(order), order.side, order.status, order.amount, order.decimals, order.receipt?.fee_lamports, order.receipt?.wallet_delta, order.receipt?.token_delta, order.route, order.signature, order.id]),
  ))
  return <section className="activity-workspace orders-activity" aria-label="Orders workspace">
    <header className="activity-heading"><div><small>RUNNER ORDERS</small><h3>Trade History</h3><p>Reviewed submissions, settlement and receipts in this scope.</p></div><div className="activity-heading-actions"><button aria-pressed={charts} onClick={() => setCharts((value) => !value)}>{charts ? 'Hide charts' : 'Show charts'}</button><button onClick={exportRows} disabled={!available || !visible.length}>Export orders CSV</button></div></header>
    {error && <p className="activity-warning" role="status">Order records unavailable: {error}</p>}
    <div className="activity-summary">
      <article><small>Matching orders</small><b>{available ? visible.length : 'Unavailable'}</b><span>{available ? `${visible.filter((order) => order.side === 'buy').length} buys · ${visible.filter((order) => order.side === 'sell').length} sells` : 'Records could not be loaded'}</span></article>
      <article><small>Finalized</small><b className="activity-positive">{available ? visible.filter((order) => order.status === 'finalized').length : 'Unavailable'}</b><span>Final receipt recorded</span></article>
      <article><small>Awaiting receipt</small><b className="activity-amber">{available ? visible.filter((order) => awaitingReceipt(order.status)).length : 'Unavailable'}</b><span>Pending, confirmed or unknown</span></article>
      <article><small>{fees.known < fees.total ? 'Known recorded fees' : 'Recorded fees'}</small><b>{available && (fees.known || !fees.total) ? exactSol(fees.atoms) : 'Unavailable'}</b><span>{fees.known} of {fees.total} orders have recorded fees</span></article>
    </div>
    <div className="activity-toolbar"><label className="activity-search"><span>Search orders</span><input type="search" value={search} onChange={(event) => { setSearch(event.target.value); setPage(0) }} placeholder="Token, mint, strategy or signature" /></label><ActivityRangeControl label="Order time range" value={range} change={(value) => { setRange(value); setPage(0) }} /><label>Side<select value={side} onChange={(event) => { setSide(event.target.value); setPage(0) }}><option value="all">Buys &amp; sells</option><option value="buy">Buys</option><option value="sell">Sells</option></select></label>{choices.length > 1 && <label>Order strategy<select value={strategy} onChange={(event) => { setStrategy(event.target.value); setPage(0) }}><option value="all">All strategies &amp; manual</option>{choices.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>}</div>
    <div className="activity-filter-row"><div className="activity-segments" role="group" aria-label="Order status">{statuses.filter((item) => item.id !== 'closed' || item.count > 0 || status === 'closed').map((item) => <button key={item.id} aria-pressed={status === item.id} onClick={() => { setStatus(item.id); setPage(0) }}>{item.label}<b>{item.count}</b></button>)}</div><label>Sort orders<select value={sort} onChange={(event) => { setSort(event.target.value); setPage(0) }}><option value="newest">Newest first</option><option value="oldest">Oldest first</option></select></label>{filtered && <button className="activity-clear" onClick={clear}>Clear filters</button>}</div>
    {charts && orders.length > 0 && <div className="activity-chart-layout"><OrderActivityChart orders={visible} range={range} now={now} /><section className="activity-chart-card activity-breakdown" aria-label="Order status breakdown"><header><div><h4>Settlement status</h4><p>Matching search, side and time range</p></div></header>{statuses.filter((item) => item.id !== 'all').map((item) => <button key={item.id} aria-pressed={status === item.id} onClick={() => { setStatus(status === item.id ? 'all' : item.id); setPage(0) }}><span>{item.label}<b>{item.count}</b></span><i><em className={item.id} style={{ width: `${base.length ? item.count / base.length * 100 : 0}%` }} /></i></button>)}</section></div>}
    <div className="activity-list-heading"><h4>Order records</h4><span>{visible.length} matching · {orders.length} in this scope</span></div>
    {!visible.length ? <div className="activity-empty trade-history-empty"><b>{!available ? 'Order records unavailable' : orders.length ? 'No orders match these filters' : 'No Runner trade history'}</b><p>{orders.length ? 'Adjust the search, status or time range to see more records.' : 'Reviewed buys and sells appear here after submission.'}</p>{filtered ? <button onClick={clear}>Show all orders</button> : available && prepareOrder && <button className="primary" onClick={prepareOrder}>Prepare an order</button>}</div>
      : <div className="activity-table-scroll"><table className="activity-record-table" aria-label="Runner trade history"><thead><tr>{['Token', 'Order', 'Strategy', 'Requested', 'Settlement', 'Status / details', 'Submitted'].map((name) => <th scope="col" key={name}>{name}</th>)}</tr></thead><tbody>{visible.slice(page * size, (page + 1) * size).map((order) => <Fragment key={order.id}>
        <tr className={`trade-history-row ${selected === order.id ? 'activity-selected' : ''}`} data-order-id={order.id}>
          <td data-label="Token"><div className="activity-token"><HoldingTokenIcon asset={{ mint: order.mint, name: label(order), symbol: market.get(order.mint)?.symbol || '', image_url: null }} /><span><button className="activity-token-link" title="Open historical token chart" onClick={() => openToken(order.mint)}>{label(order)}</button><small title={order.mint}>{order.mint.slice(0, 6)}…{order.mint.slice(-4)}</small></span></div></td>
          <td data-label="Order"><span className={`activity-badge ${order.side}`}>{order.side.toUpperCase()}</span><small>{order.route}</small></td>
          <td data-label="Strategy"><b>{source(order)}</b><small>{owner(order) ? 'Attributed strategy trade' : 'Manual Runner order'}</small></td>
          <td data-label="Requested"><b>{order.side === 'buy' ? exactSol(order.amount) : `${tokenQuantity(order.amount, order.decimals)} tokens`}</b></td>
          <td data-label="Settlement"><b>{order.receipt ? order.side === 'buy' ? `${tokenQuantity(order.receipt.token_delta, order.decimals)} tokens received` : `${exactSol(order.receipt.wallet_delta)} wallet change` : 'Receipt unavailable'}</b><small>{order.receipt ? `${exactSol(order.receipt.fee_lamports)} fee` : order.detail}</small></td>
          <td data-label="Status"><button className="activity-record-toggle" aria-label={`Inspect order ${order.id}`} aria-expanded={selected === order.id} aria-controls={`activity-order-${encodeURIComponent(order.id)}`} onClick={() => setSelected(selected === order.id ? null : order.id)}><span className={`activity-badge ${order.status}`}>{receiptStatus(order.status)}</span><small>{selected === order.id ? 'Hide details ↑' : 'Details ↓'}</small></button></td>
          <td data-label="Submitted"><time dateTime={validTime(order.created_at) ? new Date(order.created_at * 1000).toISOString() : undefined} title={activityTime(order.created_at)}>{activityTime(order.created_at, true)}</time></td>
        </tr>
        {selected === order.id && <tr className="activity-expanded-row"><td colSpan={7}><section id={`activity-order-${encodeURIComponent(order.id)}`} className="activity-inspector" aria-label="Order receipt details"><header><div><small>{receiptStatus(order.status)} · {order.side.toUpperCase()}</small><h4>{label(order)}</h4><p>{order.detail || 'No additional status detail recorded.'}</p></div><button onClick={() => openToken(order.mint)}>Open token chart ↗</button></header><dl><div><dt>Submitted</dt><dd>{activityTime(order.created_at)}</dd></div><div><dt>Receipt slot</dt><dd>{order.receipt?.slot ?? 'Unavailable'}</dd></div><div><dt>Transaction fee</dt><dd>{exactSol(order.receipt?.fee_lamports)}</dd></div><div><dt>Wallet change</dt><dd>{exactSol(order.receipt?.wallet_delta)}</dd></div><div><dt>Token change</dt><dd>{order.receipt ? tokenQuantity(order.receipt.token_delta, order.decimals) : 'Unavailable'}</dd></div><div><dt>Receipt time</dt><dd>{order.receipt ? activityTime(order.receipt.block_time) : 'Unavailable'}</dd></div></dl><div className="activity-identifiers"><span><small>MINT</small><code>{order.mint}</code><CopyActivityValue value={order.mint} kind="mint" /></span><span><small>ORDER</small><code>{order.id}</code></span>{order.signature && <span><small>SIGNATURE</small><code>{order.signature}</code><CopyActivityValue value={order.signature} kind="signature" /><a href={`https://explorer.solana.com/tx/${encodeURIComponent(order.signature)}${order.network === 'devnet' ? '?cluster=devnet' : ''}`} target="_blank" rel="noreferrer">Explorer ↗</a></span>}</div>{awaitingReceipt(order.status) && <p className="activity-inspector-note">This submission has no final receipt yet. Its status does not confirm a fill.</p>}</section></td></tr>}
      </Fragment>)}</tbody></table></div>}
    <ActivityPager page={page} count={visible.length} size={size} change={setPage} />
    <footer>Runner-reviewed submissions in this scope. External wallet trades appear in Wallet. CSV includes all matching records across pages.</footer>
  </section>
}
