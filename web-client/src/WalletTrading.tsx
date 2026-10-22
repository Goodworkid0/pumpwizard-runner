import { useEffect, useRef, useState } from 'react'
import { api, errorText, formatSol, fromBase64, toBase64, WalletTransactionError } from './browserWallet'
import type { SigningDiagnostic, WalletInspection, WalletSession } from './browserWallet'
import type { PortfolioTradeState } from './PortfolioWorkspace'
import { OrderReceipts } from './OrderReceipts'
import { receiptStatus, tokenQuantity } from './tradeFormatting'
import './trading.css'

type TradePosition = PortfolioTradeState['positions'][number]
export type StrategyOrder = { agentId: string; label: string; mint: string; positionId?: string }
type TradePreview = {
  agent_order?: { agent_id: string; reason: string; cash_hold: string; decision_at: number }
  strategy_limits?: { max_trade_lamports: number; max_daily_buy_lamports: number; max_trades_per_day: number; max_open_positions: number }
  id: string
  network: string
  sender: string
  mint: string
  side: 'buy' | 'sell'
  amount: string
  decimals: number
  slippage_bps: number
  position_id: string | null
  route: string
  reward_mode?: 'creator' | 'holders'
  expires_at: number
  fee: string
  priority_fee: string
  minimum_output: string
  expected_wallet_delta: string
  max_wallet_debit: string
  unsigned_transaction: string
}

export function WalletTrading({ session, close, initialMint = '', initialPositionId = '', initialSellQuantity = '', strategy, trades, funds, refreshRecords, refreshingRecords, recordsError, embedded = false, sellOnly = false, onBusyChange }: {
  session: WalletSession
  close: () => void
  strategy?: StrategyOrder | null
  initialMint?: string
  initialPositionId?: string
  initialSellQuantity?: string
  trades: PortfolioTradeState | null
  funds: WalletInspection | null
  refreshRecords: () => Promise<void>
  refreshingRecords: boolean
  recordsError: string
  embedded?: boolean
  sellOnly?: boolean
  onBusyChange?: (busy: boolean) => void
}) {
  const [side, setSide] = useState<'buy' | 'sell'>(initialPositionId || initialSellQuantity ? 'sell' : 'buy')
  const [mint, setMint] = useState(initialMint)
  const [amount, setAmount] = useState(initialSellQuantity)
  const [slippage, setSlippage] = useState('100')
  const [positionId, setPositionId] = useState<string | null>(initialPositionId || null)
  const [reviewed, setReviewed] = useState<{ preview: TradePreview; context: string } | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => { onBusyChange?.(busy) }, [busy, onBusyChange])
  const [now, setNow] = useState(Date.now())
  const [message, setMessage] = useState('Enter an exact amount, then request an unsigned review.')
  const [failure, setFailure] = useState<{ title: string; detail: string; help?: string; kind?: 'graduated' | 'submission' } | null>(null)
  const [submittedReviewId, setSubmittedReviewId] = useState<string | null>(null)
  const [graduatedMint, setGraduatedMint] = useState<string | null>(null)
  const failureRef = useRef<HTMLDivElement>(null)
  const mounted = useRef(true)
  const operation = useRef(false)
  const initialized = useRef(false)
  const reviewedRef = useRef<{ preview: TradePreview; context: string } | null>(null)
  const selected = trades?.positions.find((position) => position.id === positionId)
  const externalSell = side === 'sell' && !positionId && Boolean(initialSellQuantity)
  const context = JSON.stringify([session.address, session.network, session.kind, side, mint.trim(), amount.trim(), slippage, positionId, selected?.quantity, selected?.remaining_cost, selected?.last_signature, session.balance])
  const contextRef = useRef(context)
  useEffect(() => { contextRef.current = context }, [context])
  useEffect(() => { reviewedRef.current = reviewed }, [reviewed])
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      const retained = reviewedRef.current
      if (strategy && retained) void api('/api/v1/agent-orders/cancel', { sender: session.address, network: session.network, preview_id: retained.preview.id }).catch(() => undefined)
    }
  }, [session.address, session.network, strategy])
  useEffect(() => {
    if (!failure) return
    failureRef.current?.focus({ preventScroll: true })
    failureRef.current?.scrollIntoView({ block: 'nearest' })
  }, [failure])
  useEffect(() => {
    if (!trades || initialized.current) return
    initialized.current = true
    const position = trades.positions.find((item) => item.id === initialPositionId && item.status === 'open')
    if (position) {
      setAmount(tokenQuantity(position.quantity, position.decimals, false))
      setMessage('Full recorded quantity selected. Enter a smaller token quantity for a partial sell.')
    } else if (initialSellQuantity) {
      setMessage('The wallet balance held outside PumpWizard is selected. Enter a smaller quantity for a partial sell.')
    }
  }, [trades, initialPositionId, initialSellQuantity])
  useEffect(() => {
    if (!reviewed) return
    const timer = window.setInterval(() => setNow(Date.now()), 500)
    return () => clearInterval(timer)
  }, [reviewed])

  const cancelReview = async () => {
    if (strategy && reviewed) await api('/api/v1/agent-orders/cancel', { sender: session.address, network: session.network, preview_id: reviewed.preview.id })
    setReviewed(null)
    reviewedRef.current = null
  }
  const dismiss = () => { void cancelReview().then(close).catch((error) => setFailure({ title: 'Could not close this order', detail: errorText(error) })) }
  const preview = reviewed?.preview || null
  const stale = Boolean(reviewed && (reviewed.context !== context || now / 1000 >= reviewed.preview.expires_at))
  const blocked = !trades || trades.pending || refreshingRecords || Boolean(recordsError)
  const unsupportedBuy = side === 'buy' && graduatedMint === mint.trim()
  const submittedOrder = trades?.orders.find((order) => order.id === submittedReviewId)
  const settled = submittedOrder?.status === 'finalized' || submittedOrder?.status === 'failed'
  const visibleFailure = failure?.kind === 'submission' && settled ? null : failure
  const newBuy = () => {
    setSide('buy'); setPositionId(null); setAmount(''); setReviewed(null)
    setFailure(null)
    setMessage('Enter an exact SOL amount and mint, then request an unsigned review.')
  }
  const selectSell = (position: TradePosition) => {
    setSide('sell'); setPositionId(position.id); setMint(position.mint)
    setAmount(tokenQuantity(position.quantity, position.decimals, false)); setReviewed(null)
    setFailure(null)
    setMessage('Full recorded quantity selected. Enter a smaller token quantity for a partial sell.')
  }
  const review = async () => {
    if (operation.current || blocked || unsupportedBuy) return
    const slippageBps = Number(slippage)
    if (!Number.isInteger(slippageBps)) { setFailure({ title: 'Could not prepare order review', detail: 'Slippage must be an integer number of basis points.' }); return }
    operation.current = true
    setBusy(true); setReviewed(null); setFailure(null); setSubmittedReviewId(null)
    setMessage('Reading on-chain route accounts and simulating an unsigned trade…')
    const requestedContext = context
    try {
      session.assertActive()
      const data = await api<{ preview: TradePreview }>(strategy ? '/api/v1/agent-orders/preview' : '/api/v1/trades/preview', strategy ? { sender: session.address, network: session.network, agent_id: strategy.agentId, mint: mint.trim(), position_id: positionId, slippage_bps: slippageBps } : {
        sender: session.address, network: session.network, mint: mint.trim(), side,
        amount: amount.trim(), slippage_bps: slippageBps, position_id: positionId,
      })
      if (!mounted.current) return
      session.assertActive()
      if (contextRef.current !== requestedContext) throw new Error('Order or wallet changed. Request a fresh review.')
      if (data.preview.sender !== session.address || data.preview.network !== session.network
        || data.preview.mint !== mint.trim() || data.preview.side !== side || data.preview.position_id !== positionId) {
        throw new Error('The returned review does not match this order. Request a fresh review.')
      }
      setNow(Date.now()); setReviewed({ preview: data.preview, context: requestedContext })
      setMessage(session.kind === 'local' ? 'Review every value. Sign & submit authorizes this one transaction using your unlocked generated wallet.' : `Review every value. ${session.name} will request approval before signing.`)
    } catch (error) {
      if (mounted.current) {
        const graduated = side === 'buy' && errorText(error) === 'Curve graduated; new buys require an active Pump curve'
        if (graduated) setGraduatedMint(mint.trim())
        setFailure(graduated ? {
          kind: 'graduated', title: 'Buying unavailable for this token',
          detail: 'This token has graduated from its Pump.fun bonding curve. Buying after graduation is not supported in Runner yet.',
          help: 'Choose a token with an active bonding curve. Nothing was signed or submitted.',
        } : {
          title: 'Could not prepare order review', detail: errorText(error),
          help: 'Nothing was signed or submitted. Your order details have been kept.',
        })
      }
    }
    finally { operation.current = false; if (mounted.current) setBusy(false) }
  }
  const saveSigningDiagnostic = async (current: TradePreview, diagnostic: SigningDiagnostic) => {
    const result = await api<{ diagnostic: { status: string; summary: string } }>('/api/v1/trades/signing-diagnostic', {
      sender: session.address, network: session.network, preview_id: current.id, diagnostic,
    })
    return result.diagnostic.summary
  }
  const checkWallet = async () => {
    if (!reviewed || !session.checkTransaction || operation.current || blocked || stale) return
    const current = reviewed
    operation.current = true; setBusy(true); setFailure(null)
    setMessage(`Waiting for ${session.name}. This check signs once but never submits the transaction…`)
    try {
      session.assertActive()
      const diagnostic = await session.checkTransaction(fromBase64(current.preview.unsigned_transaction))
      const summary = await saveSigningDiagnostic(current.preview, diagnostic)
      if (mounted.current) setFailure({ title: 'Wallet compatibility check', detail: summary,
        help: 'Diagnostic saved locally without signatures. No transaction was submitted and no SOL was spent by this check.' })
    } catch (error) {
      if (mounted.current) setFailure({ title: 'Wallet compatibility check could not finish', detail: errorText(error),
        help: 'This check never submits a transaction.' })
    } finally {
      if (strategy) await cancelReview().catch(() => {})
      operation.current = false
      if (mounted.current) { setReviewed(null); setBusy(false) }
    }
  }
  const signAndSubmit = async () => {
    if (!reviewed || operation.current || blocked) return
    const current = reviewed
    const requireCurrent = () => {
      session.assertActive()
      if (!mounted.current || current.context !== contextRef.current || Date.now() / 1000 >= current.preview.expires_at) {
        throw new Error('Quote expired or wallet, order or position changed. Request a fresh review before signing again.')
      }
    }
    operation.current = true; setBusy(true); setFailure(null)
    let submissionAttempted = false
    try {
      requireCurrent()
      setMessage(`Waiting for ${session.name} to sign this one transaction…`)
      const signedTransaction = await session.signTransaction(fromBase64(current.preview.unsigned_transaction))
      requireCurrent()
      submissionAttempted = true
      setSubmittedReviewId(current.preview.id)
      setMessage('Submitting once. The original signature will be retained for recovery…')
      const result = await api<{ signature: string; state: string }>(strategy ? '/api/v1/agent-orders/submit' : '/api/v1/trades/submit', {
        sender: session.address, network: session.network, preview_id: current.preview.id,
        signed_transaction: toBase64(signedTransaction),
      })
      if (mounted.current) setMessage(`${receiptStatus(result.state)}. The original transaction is saved in Recent orders.`)
    } catch (error) {
      let diagnosticSummary = ''
      if (error instanceof WalletTransactionError && !submissionAttempted) {
        try { diagnosticSummary = await saveSigningDiagnostic(current.preview, error.diagnostic) }
        catch { diagnosticSummary = 'The wallet response could not be recorded. Use Check wallet compatibility for diagnosis.' }
      }
      if (mounted.current) setFailure({
        kind: submissionAttempted ? 'submission' : undefined,
        title: submissionAttempted ? 'Check submission status' : 'Order was not submitted',
        detail: `${errorText(error)}${diagnosticSummary ? ` ${diagnosticSummary}` : ''}`,
        help: submissionAttempted ? 'Submission outcome must be checked against the saved original signature before another order.' : diagnosticSummary ? 'No transaction was sent by Runner. Resolve the wallet compatibility result before retrying.' : 'No transaction was sent by Runner. Request a fresh review to try again.',
      })
    } finally {
      if (strategy && !submissionAttempted) {
        await cancelReview().catch(() => { /* The retained review can be cancelled from strategy orders. */ })
      }
      if (mounted.current) {
        setReviewed(null)
        if (submissionAttempted) await refreshRecords()
        setBusy(false)
      }
      operation.current = false
    }
  }
  const reconcile = async () => {
    if (operation.current) return
    try {
      await cancelReview()
      await refreshRecords()
      setFailure(null)
      setMessage('Receipt check completed. Review the latest status below.')
    } catch (error) {
      setFailure({ title: 'Could not refresh order status', detail: errorText(error) })
    }
  }
  const minimum = preview && (preview.side === 'buy' ? `${tokenQuantity(preview.minimum_output, preview.decimals)} tokens` : formatSol(preview.minimum_output))
  const openPositions = trades?.positions.filter((position) => position.status === 'open' && !position.managed && (!sellOnly || position.id === initialPositionId)) || []

  const content = <section className={`strategy-modal trade-modal${embedded ? ' trade-inline' : ''}`} role={embedded ? 'region' : 'dialog'} aria-modal={embedded ? undefined : true} aria-label={embedded ? 'New order' : 'Manual trading'} onMouseDown={(event) => event.stopPropagation()}>
      {!embedded && <button className="close" aria-label="Close dialog" onClick={dismiss} disabled={busy}>×</button>}
      <small>PUMP.FUN · WALLET-REVIEWED ORDER</small>
      <h2>{strategy ? `Review ${strategy.label}` : 'Place an order'}</h2>
      <p className="trade-intro">Prepare the order here, review the exact transaction, then approve one signature.</p>
      <div className="connected-wallet"><span className="wallet-avatar">≋</span><span className="wallet-identity"><small>CONNECTED WALLET</small><b>{session.name}</b><code title={session.address}>{session.address}</code></span><span className="wallet-balance"><small>TOTAL BALANCE</small><strong>{formatSol(session.balance)}</strong></span></div>
      <div className="trade-capacity"><span>Estimated available to buy <strong>{funds ? formatSol(funds.buy_capacity_lamports) : 'Refreshing…'}</strong></span><details><summary>Balance &amp; limits</summary>
        {funds && <p>Allocated: {formatSol(funds.reserved_lamports)} · Unallocated: {formatSol(funds.unallocated_lamports)}. Buy capacity includes a conservative {formatSol(funds.buy_buffer_lamports)} fee/account buffer. The fresh review applies all limits.</p>}
        <p>{funds?.wallet_risk?.policy ? `Wallet limits: ${formatSol(funds.wallet_risk.policy.max_buy_lamports)} per buy · ${formatSol(funds.wallet_risk.policy.max_exposure_lamports)} open cost · ${formatSol(funds.wallet_risk.policy.reserve_lamports)} reserve · ${formatSol(funds.wallet_risk.policy.daily_loss_lamports)} daily realized loss.` : 'No additional wallet-wide risk policy is configured. Route and reservation checks still apply.'}</p>
      </details></div>
      {Boolean(funds?.entry_blocks.filter((reason) => !trades?.pending || reason !== 'An original wallet signature is unresolved').length) && <p className="trade-recovery">Buy restrictions: {funds?.entry_blocks.filter((reason) => !trades?.pending || reason !== 'An original wallet signature is unresolved').join(' · ')}</p>}
      {visibleFailure ? <div className={`trade-error${visibleFailure.kind === 'graduated' ? ' trade-unavailable' : ''}`} role="alert" tabIndex={-1} ref={failureRef} aria-labelledby="trade-error-title">
        <strong id="trade-error-title">{visibleFailure.title}</strong>
        <p>{visibleFailure.detail}</p>
        {visibleFailure.help && <p className="trade-error-help">{visibleFailure.help}</p>}
        {visibleFailure.kind === 'graduated' && <div className="trade-error-actions"><button onClick={dismiss} disabled={busy}>{strategy ? 'Back to positions' : 'Back to Wallet'}</button><button onClick={() => { setMint(''); setFailure(null); setMessage('Paste another token mint to prepare a new review.'); document.getElementById('trade-token-mint')?.focus() }} disabled={busy || Boolean(strategy)}>Change token</button></div>}
      </div> : !busy && !preview && (submittedOrder || trades?.pending) ? <div className={`trade-order-status ${trades?.pending ? 'pending' : submittedOrder?.status || 'pending'}`} role="status" aria-label="Order status">
        <span aria-hidden="true">{!trades?.pending && submittedOrder?.status === 'finalized' ? '✓' : '◷'}</span><div><strong>{trades?.pending ? 'Waiting for the original receipt' : submittedOrder?.status === 'finalized' ? `${submittedOrder.side === 'buy' ? 'Buy' : 'Sell'} finalized` : submittedOrder?.status === 'failed' ? 'Transaction failed' : 'Checking transaction status'}</strong><p>{trades?.pending ? 'Runner checks the saved transaction automatically. New orders stay paused until its outcome is resolved.' : submittedOrder?.status === 'finalized' ? 'Wallet balance and positions are updated. The finalized receipt is below.' : 'See the original receipt below for the outcome and any fee charged.'}</p></div>
      </div> : <p className="trade-message" aria-live="polite"><span className="pulse" />{message}</p>}
      {visibleFailure && trades?.pending && <p className="trade-recovery" role="status">New orders are paused while Runner checks the original receipt.</p>}
      {recordsError && <p className="trade-recovery" role="alert">{recordsError} <button onClick={reconcile} disabled={busy || refreshingRecords}>Retry receipt check</button></p>}

      {preview ? <section className="trade-review">
        <small>ORDER REVIEW · EXPIRES {new Date(preview.expires_at * 1000).toLocaleTimeString()}</small>
        <h3>{preview.side.toUpperCase()} {preview.side === 'buy' ? formatSol(preview.amount) : `${tokenQuantity(preview.amount, preview.decimals)} tokens`}</h3>
        {preview.agent_order && <p>Strategy: {strategy?.label} · {preview.agent_order.reason.replace(/_/g, ' ')}<br />Decision: {new Date(preview.agent_order.decision_at * 1000).toLocaleString()}<br />Allocation: <code>{preview.agent_order.cash_hold}</code></p>}
        {preview.strategy_limits && <p>Limits: {formatSol(preview.strategy_limits.max_trade_lamports)} per buy · {formatSol(preview.strategy_limits.max_daily_buy_lamports)} daily buys · {preview.strategy_limits.max_trades_per_day} daily trades · {preview.strategy_limits.max_open_positions} positions.</p>}
        {preview.reward_mode && <p className="trade-reward-mode">Fee destination: {preview.reward_mode === 'holders' ? 'Token holders · distributed by Pump.fun' : 'Creator / configured fee recipients'}. Trading fees are included in the simulated quote.</p>}
        <dl><div><dt>Signer</dt><dd>{session.kind === 'local' ? 'PumpWizard Wallet · signs here when you approve' : `${session.name} · installed wallet approval`}<code>{preview.sender}</code></dd></div><div><dt>Network</dt><dd>{preview.network === 'mainnet-beta' ? 'Solana Mainnet' : 'Solana Devnet'}</dd></div><div><dt>Mint</dt><dd><code>{preview.mint}</code></dd></div><div><dt>Route</dt><dd>{preview.route.startsWith('pumpswap') ? 'Canonical PumpSwap exit' : 'Pump bonding curve'}</dd></div><div><dt>Minimum route output</dt><dd>{minimum}</dd></div><div><dt>Estimated network fee</dt><dd>{formatSol(preview.fee)}</dd></div><div><dt>Priority fee</dt><dd>{formatSol(preview.priority_fee)}</dd></div><div><dt>Estimated wallet change</dt><dd>{formatSol(preview.expected_wallet_delta)}</dd></div><div><dt>Slippage ceiling</dt><dd>{preview.slippage_bps / 100}%</dd></div>{preview.side === 'buy' && <div><dt>Maximum wallet debit</dt><dd>{formatSol(preview.max_wallet_debit)}</dd></div>}</dl>
        <p role="status">{stale ? 'Quote expired or the reviewed context changed. Cancel and request a fresh review.' : `Quote expires in ${Math.max(0, Math.ceil(preview.expires_at - now / 1000))} seconds.`}</p><p>Nothing is signed yet. Runner will recheck the route, balance, fee, blockhash, risk limits and exact transaction before broadcast.</p>
        {session.checkTransaction && <p>Check wallet compatibility asks for one wallet signature, then discards it without submitting or spending SOL.</p>}
        <div className="modal-actions"><button onClick={() => { void cancelReview().then(() => { setFailure(null); setMessage('Review cancelled. Nothing was signed or submitted.') }).catch((error) => setFailure({ title: 'Could not cancel order review', detail: errorText(error) })) }} disabled={busy}>Cancel</button>{session.checkTransaction && <button onClick={() => void checkWallet()} disabled={busy || stale || blocked}>Check wallet compatibility</button>}<button className="primary" onClick={signAndSubmit} disabled={busy || stale || blocked}>Sign & submit</button></div>
      </section> : <>
        <section className="manual-trade-form">
          <div className="trade-ticket-heading"><div><small>ORDER TICKET</small><h3>{side === 'buy' ? 'Buy a token' : externalSell ? 'Sell wallet holding' : 'Sell selected position'}</h3></div><div className="trade-mode"><button className={side === 'buy' ? 'selected' : ''} onClick={newBuy} disabled={busy || Boolean(strategy) || sellOnly}>Buy</button><button className={side === 'sell' ? 'selected' : ''} disabled={busy || (!positionId && !externalSell)} title={positionId || externalSell ? 'Selected holding' : 'Choose an open position below'}>Sell</button></div></div>
          <div className="trade-fields"><label>Token mint<input id="trade-token-mint" value={mint} onChange={(event) => { setMint(event.target.value); if (failure?.kind === 'graduated') setFailure(null) }} disabled={busy || side === 'sell' || Boolean(strategy)} placeholder="Paste the Solana token mint" /></label><label>{strategy ? 'Strategy quantity (calculated at review)' : side === 'buy' ? 'Spend amount' : 'Quantity to sell'}<span className="field-with-unit"><input readOnly={sellOnly} value={amount} onChange={(event) => setAmount(event.target.value)} disabled={busy || Boolean(strategy)} inputMode="decimal" placeholder={side === 'buy' ? '0.01' : '1000'} /><b>{side === 'buy' ? 'SOL' : 'TOKENS'}</b></span></label><label>Max slippage<span className="field-with-unit"><input value={slippage} onChange={(event) => setSlippage(event.target.value)} disabled={busy} inputMode="numeric" /><b>BPS</b></span></label></div>
          <div className="trade-ticket-footer"><p>{side === 'buy' ? 'Enter the exact amount you want to spend.' : externalSell ? 'Only the balance held outside Runner positions is available in this order.' : sellOnly ? 'Sell all selects the full recorded position. Each transaction requires your approval.' : 'The full recorded position is selected; reduce the quantity for a partial sell.'}</p><button className="primary" onClick={review} disabled={busy || blocked || unsupportedBuy || !mint.trim() || (!strategy && !amount.trim()) || (side === 'sell' && !externalSell && (!selected || selected.status !== 'open' || (selected.managed && !strategy)))}>{busy ? 'Preparing review…' : unsupportedBuy ? 'Buy unavailable' : 'Review order'}</button></div>
        </section>
        <div className="trade-columns">
          <section><div className="trade-list-heading"><div><small>HOLDINGS</small><h3>Open positions</h3></div><span>{openPositions.length}</span></div>{!openPositions.length ? <div className="trade-empty"><b>No open positions</b><p>Completed buys will appear here and can be selected for sale.</p></div> : openPositions.map((position) => <button className="trade-row" key={position.id} onClick={() => selectSell(position)} disabled={busy || Boolean(strategy) || sellOnly}><span><b>{position.mint.slice(0, 8)}…{position.mint.slice(-6)}</b><small>{tokenQuantity(position.quantity, position.decimals)} tokens</small></span><span>{formatSol(position.remaining_cost)} cost</span><strong>Select</strong></button>)}</section>
          <section><div className="trade-list-heading"><div><small>TRANSACTIONS</small><h3>Recent orders</h3></div><button onClick={reconcile} disabled={busy || refreshingRecords}>Refresh status</button></div>{!trades?.orders.length ? <div className="trade-empty"><b>No submitted orders</b><p>Your signed buys and sells will appear here.</p></div> : <OrderReceipts orders={trades.orders} />}</section>
        </div>
        <section className="trade-signing-note"><span>✓</span><div><b>You approve every order</b><p>Runner simulates and validates each review and rechecks it before broadcast. Positions change only after finalized receipts.</p></div></section>
      </>}
    </section>
  return embedded ? content : <div className="modal-backdrop" role="presentation" onMouseDown={busy ? undefined : dismiss}>{content}</div>
}
