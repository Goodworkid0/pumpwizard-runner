export function tokenQuantity(atoms: string | number, decimals: number | null, grouped = true) {
  if (decimals === null || !Number.isInteger(decimals) || decimals < 0 || decimals > 18) return '—'
  try {
    const value = BigInt(atoms)
    const absolute = value < 0n ? -value : value
    const unit = 10n ** BigInt(decimals)
    const whole = absolute / unit
    const fraction = decimals ? (absolute % unit).toString().padStart(decimals, '0').replace(/0+$/, '') : ''
    return `${value < 0n ? '−' : ''}${grouped ? whole.toLocaleString() : whole}${fraction ? `.${fraction}` : ''}`
  } catch { return '—' }
}

export function receiptStatus(status: string) {
  return ({ pending: 'Pending', confirmed: 'Pending final receipt', unknown: 'Unknown', finalized: 'Finalized', failed: 'Failed', cancelled: 'Cancelled', expired: 'Expired' } as Record<string, string>)[status] || 'Unknown'
}
