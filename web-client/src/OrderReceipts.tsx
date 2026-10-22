import { formatSol } from './browserWallet'
import type { PortfolioTradeState } from './PortfolioWorkspace'
import { receiptStatus, tokenQuantity } from './tradeFormatting'

export function OrderReceipts({ orders }: { orders: PortfolioTradeState['orders'] }) {
  return <div className="receipt-list">{orders.map((order) => <article className="receipt-card" key={order.id}>
    <div className="receipt-heading"><b className={`receipt-state ${order.status}`}>{order.side.toUpperCase()} · {receiptStatus(order.status)}</b><time>{new Date(order.created_at * 1000).toLocaleString()}</time></div>
    <code className="receipt-mint" title={order.mint}>{order.mint}</code>
    <p>{order.side === 'buy' ? formatSol(order.amount) : `${tokenQuantity(order.amount, order.decimals)} tokens`}</p>
    {order.signature && <a className="receipt-explorer" title={order.signature} aria-label={`View signature ${order.signature} on Solana Explorer`} href={`https://explorer.solana.com/tx/${encodeURIComponent(order.signature)}${order.network === 'devnet' ? '?cluster=devnet' : ''}`} target="_blank" rel="noreferrer">View transaction <span aria-hidden="true">↗</span></a>}
    <p>{order.detail}</p>
    {order.receipt && <dl><div><dt>Wallet change</dt><dd>{formatSol(order.receipt.wallet_delta)}</dd></div><div><dt>Token change</dt><dd>{tokenQuantity(order.receipt.token_delta, order.decimals)}</dd></div><div><dt>Transaction fee</dt><dd>{formatSol(order.receipt.fee_lamports)}</dd></div><div><dt>Finalized slot</dt><dd>{order.receipt.slot}</dd></div></dl>}
  </article>)}</div>
}
