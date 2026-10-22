import { useEffect, useRef, useState } from 'react'
import { api, errorText, formatSol, fromBase64, toBase64 } from './browserWallet'
import type { WalletSession } from './browserWallet'

export type WithdrawalReview = { id: string; sender: string; recipient: string; network: string; lamports: string; fee: string; balance: string; expires_at: number; unsigned_transaction: string; funding_budget?: string | null; source_reserve?: string; reserved_lamports?: string }
export function WithdrawDialog({ wallet, close, refresh, recipient: initialRecipient = '', funding = false, onSubmissionAttempt, onResolved, initialReview = null, initialAmount = '' }: { wallet: WalletSession; close: () => void; refresh: () => Promise<void>; recipient?: string; funding?: boolean; onSubmissionAttempt?: () => void; onResolved?: () => void; initialReview?: WithdrawalReview | null; initialAmount?: string }) {
  const [submitted, setSubmitted] = useState(false)
  const [receipt, setReceipt] = useState<{ review: WithdrawalReview; signature?: string; state: string; detail: string } | null>(null)
  const [checking, setChecking] = useState(false)
  const [confirmationPaused, setConfirmationPaused] = useState(false)
  const confirmationStarted = useRef(0)
  const callbacks = useRef({ refresh, onResolved })
  useEffect(() => { callbacks.current = { refresh, onResolved } }, [refresh, onResolved])
  const [recipient, setRecipient] = useState(initialRecipient)
  const [amount, setAmount] = useState(initialAmount)
  const [review, setReview] = useState<WithdrawalReview | null>(initialReview)
  const [busy, setBusy] = useState(false)
  const [now, setNow] = useState(Date.now())
  const [message, setMessage] = useState(initialReview ? 'Check the complete destination address and debit. Approval signs one SOL transfer.' : 'Enter the destination wallet and exact SOL amount.')
  const mounted = useRef(true)
  const operation = useRef(false)
  useEffect(() => { mounted.current = true; const timer = setInterval(() => setNow(Date.now()), 500); return () => { mounted.current = false; clearInterval(timer) } }, [])
  const identity = { sender: wallet.address, network: wallet.network }
  const prepare = async () => {
    if (operation.current) return
    operation.current = true; setBusy(true)
    try { wallet.assertActive(); const result = await api<{ preview: WithdrawalReview }>('/api/v1/withdrawals/preview', { ...identity, recipient: recipient.trim(), amount: amount.trim(), ...(funding ? { funding_budget: true } : {}) }); wallet.assertActive(); if (mounted.current) { setReview(result.preview); setMessage('Check the complete destination address and debit. Approval signs one SOL transfer.') } }
    catch (error) { setMessage(errorText(error)) }
    finally { operation.current = false; setBusy(false) }
  }
  const submit = async () => {
    if (!review || operation.current) return
    operation.current = true; setBusy(true)
    const current = () => { wallet.assertActive(); if (!mounted.current || Date.now()/1000 >= review.expires_at || wallet.address !== review.sender || wallet.network !== review.network) throw new Error('Wallet or withdrawal review changed. Prepare a new review.') }
    let attempted = false
    try {
      current()
      const signed = await wallet.signTransaction(fromBase64(review.unsigned_transaction))
      current(); attempted = true
      if (funding) { confirmationStarted.current = Date.now(); setConfirmationPaused(false); setSubmitted(true); setReceipt({ review, state: 'submitting', detail: 'Waiting for the submission response.' }) }
      onSubmissionAttempt?.()
      const result = await api<{ signature: string; state: string; detail: string }>('/api/v1/withdrawals/submit', { ...identity, preview_id: review.id, signed_transaction: toBase64(signed) })
      setReceipt({ review, signature: result.signature, state: result.state, detail: result.detail })
      setMessage(result.state === 'unknown' ? 'Submission unconfirmed. Check the original transfer; do not send it again.' : `Submitted signature: ${result.signature}. Waiting for network confirmation.`)
    } catch (error) { if (mounted.current) { if (attempted) setReceipt({ review, state: 'unknown', detail: 'No reliable submission response. Check the original transfer before any retry.' }); setMessage(`${errorText(error)}${attempted ? ' Check the original transfer status before retrying.' : ' Nothing was submitted by Runner.'}`) } }
    finally { if (mounted.current) { setReview(null); await refresh().catch(() => undefined); if (mounted.current) setBusy(false) } operation.current = false }
  }
  const receiptId = receipt?.review.id
  const receiptState = receipt?.state
  const receiptSender = receipt?.review.sender
  const receiptNetwork = receipt?.review.network
  const { address, network, assertActive } = wallet
  useEffect(() => {
    if (!submitted || !receiptId || busy || checking || confirmationPaused || !['pending', 'unknown', 'confirmed'].includes(receiptState || '')) return
    let active = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      if (!active) return
      if (Date.now() - confirmationStarted.current >= 120000) {
        setConfirmationPaused(true)
        setMessage(receiptState === 'confirmed' ? 'The transfer is confirmed; finality is taking longer than expected. You can follow the original signature in the explorer.' : 'Confirmation is taking longer than expected. The original transfer remains recorded. Use the explorer or recovery option before sending again.')
        return
      }
      try {
        assertActive()
        if (receiptSender !== address || receiptNetwork !== network) throw new Error('Wallet identity changed. Return to the original wallet to view its receipt.')
        const result = await api<{ sender: string; network: string; withdrawals: { id: string; signature: string; status: string; detail: string }[] }>('/api/v1/trades/reconcile', { sender: address, network })
        if (!active) return
        if (result.sender !== address || result.network !== network) throw new Error('Receipt wallet identity changed.')
        const row = result.withdrawals.find((item) => item.id === receiptId)
        if (row) {
          setReceipt((current) => current?.review.id === receiptId ? { ...current, signature: row.signature, state: row.status, detail: row.detail } : current)
          setMessage(row.detail)
          if (row.status === 'expired') callbacks.current.onResolved?.()
          if (['finalized', 'failed', 'expired'].includes(row.status)) {
            void callbacks.current.refresh().catch(() => undefined)
            return
          }
        }
      } catch (error) {
        if (active) setMessage(`Confirmation check delayed: ${errorText(error)}. Checking again automatically.`)
      }
      if (active) timer = setTimeout(() => void poll(), 3000)
    }
    timer = setTimeout(() => void poll(), 1000)
    return () => { active = false; clearTimeout(timer) }
  }, [submitted, receiptId, receiptState, receiptSender, receiptNetwork, address, network, assertActive, busy, checking, confirmationPaused])
  const resolveReceipt = async () => {
    if (!receipt?.signature || checking) return
    setChecking(true)
    try {
      wallet.assertActive()
      const result = await api<{ transfer: { status: string; detail: string } }>('/api/v1/withdrawals/resolve-expired', { ...identity, transfer_id: receipt.review.id, signature: receipt.signature })
      if (mounted.current) { setReceipt({ ...receipt, state: result.transfer.status, detail: result.transfer.detail }); setMessage(result.transfer.detail); if (result.transfer.status === 'expired') onResolved?.() }
      await refresh()
    } catch (error) { if (mounted.current) setMessage(errorText(error)) }
    finally { if (mounted.current) setChecking(false) }
  }
  const shown = submitted ? receipt?.review : review
  const transferDetails = shown && <dl>
    <dt>Network</dt><dd>{shown.network}</dd><dt>From</dt><dd><code>{shown.sender}</code></dd><dt>To</dt><dd><code>{shown.recipient}</code></dd>
    {shown.funding_budget && <><dt>Spending budget</dt><dd>{formatSol(shown.funding_budget)} · includes fee</dd></>}
    <dt>Recipient receives</dt><dd>{formatSol(shown.lamports)}</dd><dt>Network fee</dt><dd>{formatSol(shown.fee)}{shown.funding_budget ? ' · deducted from budget' : ''}</dd><dt>Total debit</dt><dd>{formatSol((BigInt(shown.lamports)+BigInt(shown.fee)).toString())}</dd>
    <dt>Source balance after transfer</dt><dd>{formatSol((BigInt(shown.balance)-BigInt(shown.lamports)-BigInt(shown.fee)).toString())}{submitted ? ' · projected if successful' : ' · projected'}</dd>
    {BigInt(shown.source_reserve || '0') > 0n && <><dt>Minimum kept in source</dt><dd>{formatSol(shown.source_reserve!)}{BigInt(shown.reserved_lamports || '0') > 0n ? ` + ${formatSol(shown.reserved_lamports!)} already reserved` : ''}</dd></>}
  </dl>
  const waiting = submitted && !!receipt && ['submitting', 'pending', 'unknown', 'confirmed'].includes(receipt.state) && !confirmationPaused
  const stateLabel = confirmationPaused && receipt?.state !== 'confirmed' ? 'Transfer not confirmed yet' : receipt ? ({ submitting: 'Submitting transfer…', pending: 'Awaiting confirmation', unknown: 'Transfer not confirmed', confirmed: 'Confirmed on Solana', finalized: 'Transfer finalized', failed: 'Transfer failed', expired: 'Expired transfer resolved' } as Record<string, string>)[receipt.state] || 'Status unavailable' : ''
  return <div className="modal-backdrop"><section className={`strategy-modal${funding ? ' automation-wallet-transfer' : ''}`} role="dialog" aria-modal="true" aria-label={funding ? 'Fund automation wallet' : 'Withdraw SOL'}>
    <button className="close" aria-label="Close dialog" disabled={busy || checking} onClick={close}>×</button>
    <small>{funding ? 'SESSION SETUP · TRANSFER REVIEW' : 'WALLET TRANSFER'}</small><h2>{submitted ? stateLabel : funding ? 'Review transfer' : 'Withdraw SOL'}</h2>
    {funding && !submitted && <p>The fee is included in your spending budget. The source wallet reserve is protected before you sign. This transfer does not enable automated trades.</p>}
    <p className="transfer-result-message" role="status">{message}</p>
    {submitted && receipt && <div className={`transfer-receipt-state transfer-receipt-${receipt.state}`} aria-busy={waiting}><b>{waiting && <span className="transfer-confirmation-spinner" aria-hidden="true" />}{stateLabel}</b>{waiting && <p>{receipt.state === 'confirmed' ? 'Confirmed. Waiting for finality…' : 'Checking Solana automatically. You can leave this screen; the original transfer stays in your history.'}</p>}<p>{receipt.detail}</p>{!['confirmed', 'finalized', 'failed', 'expired'].includes(receipt.state) && <p>A signature alone does not prove funds arrived. Check the original transfer before sending again.</p>}</div>}
    {transferDetails}
    {shown?.funding_budget && BigInt(shown.funding_budget) > BigInt(shown.lamports)+BigInt(shown.fee) && <p>The reviewed debit was reduced to preserve your source reserve and existing allocations.</p>}
    {submitted ? <>
      {receipt?.signature && <div className="transfer-signature"><small>TRANSACTION SIGNATURE</small><code>{receipt.signature}</code><a href={`https://explorer.solana.com/tx/${encodeURIComponent(receipt.signature)}${wallet.network === 'devnet' ? '?cluster=devnet' : ''}`} target="_blank" rel="noreferrer">View on Solana Explorer ↗</a></div>}
      {receipt?.signature && ['pending', 'unknown'].includes(receipt.state) && <button disabled={busy || checking} onClick={() => void resolveReceipt()}>Check & resolve expired transfer</button>}
      <div className="modal-actions"><button className="primary" disabled={busy || checking} onClick={close}>Back to wallet setup</button></div>
    </> : review ? <><p>{now/1000 >= review.expires_at ? 'Review expired.' : `Expires in ${Math.ceil(review.expires_at-now/1000)} seconds.`}</p><div className="modal-actions"><button disabled={busy} onClick={() => funding ? close() : setReview(null)}>Cancel review</button><button className="primary" disabled={busy || now/1000 >= review.expires_at} onClick={() => void submit()}>Sign & send SOL</button></div></> : <div className="control-fields"><label>Destination wallet<input value={recipient} disabled={busy || funding} onChange={(e) => setRecipient(e.target.value)} autoComplete="off" /></label><label>Amount (SOL)<input value={amount} disabled={busy} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" /></label><button className="primary" disabled={busy || !recipient.trim() || !amount.trim()} onClick={() => void prepare()}>{funding ? 'Review transfer' : 'Review withdrawal'}</button></div>}
  </section></div>
}
