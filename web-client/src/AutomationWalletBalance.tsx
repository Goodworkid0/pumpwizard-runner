import { formatSol } from './browserWallet'
import { tokenQuantity } from './tradeFormatting'
import type { InventoryResult } from './useWalletInventories'

function solDollars(lamports: string, price: number | null | undefined) {
  if (!price || !Number.isFinite(price)) return 'USD estimate unavailable'
  return `≈ ${(Number(lamports) / 1e9 * price).toLocaleString(undefined, { style: 'currency', currency: 'USD' })} in SOL`
}

export function AutomationWalletBalance({ result, compact = false }: { result?: InventoryResult; compact?: boolean }) {
  if (!result) return <p className="wallet-balance-pending" role="status">Loading balances…</p>
  if (!result.data) return <p className="wallet-balance-pending" role="status">Balance unavailable · {result.error}</p>
  const data = result.data
  return <div className="automation-wallet-balance">
    <div className="automation-balance-total"><strong>{formatSol(data.balance)}</strong><span>{solDollars(data.balance, data.sol_usd_price)}</span></div>
    {!compact && <details className="automation-token-list"><summary>{data.assets.length ? `${data.assets.length} token holding${data.assets.length === 1 ? '' : 's'}` : data.coverage_gaps.length ? 'Token coverage incomplete' : 'No token holdings'}</summary>
      {data.assets.map((asset) => <div key={asset.mint}><span><b>{asset.symbol || asset.name || `${asset.mint.slice(0, 6)}…${asset.mint.slice(-4)}`}</b><code title={asset.mint}>{asset.mint}</code></span><span>{tokenQuantity(asset.amount, asset.decimals)}<small>{asset.program}</small></span></div>)}
      <p>Token values are not included in the SOL estimate.</p>
    </details>}
    {data.coverage_gaps.length > 0 && <p className="wallet-balance-pending">Some token accounts could not be read. Holdings may be incomplete.</p>}
    {!compact && <small>Checked {new Date(data.checked_at * 1000).toLocaleTimeString()}</small>}
  </div>
}
