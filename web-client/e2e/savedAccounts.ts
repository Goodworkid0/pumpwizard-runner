import type { Page } from '@playwright/test'

// Browser contexts no longer own account data. Reset only this isolated E2E
// server's account store before each scenario; retained strategy fixtures stay.
export async function resetSavedAccounts(page: Page) {
  const root = await page.request.get('/')
  const headers = { Origin: new URL(root.url()).origin }
  const response = await page.request.get('/api/v1/app-state', { headers })
  if (!response.ok()) throw new Error(await response.text())
  const state = await response.json() as { wallets: { address: string }[]; preferences: Record<string, string> }
  for (const wallet of state.wallets) {
    const removed = await page.request.post('/api/v1/app-state/wallets/remove', { headers, data: { address: wallet.address } })
    if (!removed.ok()) throw new Error(await removed.text())
  }
  for (const key of Object.keys(state.preferences)) {
    const removed = await page.request.post('/api/v1/app-state/preferences', { headers, data: { key, value: null } })
    if (!removed.ok()) throw new Error(await removed.text())
  }
}
