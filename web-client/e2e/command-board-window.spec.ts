import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import { resetSavedAccounts } from './savedAccounts'

async function enterDashboard(page: Page) {
  await resetSavedAccounts(page)
  await page.goto('/')
  const password = 'synthetic detachable command board password'
  await page.getByRole('button', { name: 'Create a new wallet' }).click()
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByLabel('Confirm password').fill(password)
  await page.getByRole('button', { name: 'Create encrypted wallet' }).click()
  await page.getByLabel(/I saved the encrypted backup/).check()
  await page.getByRole('button', { name: 'Choose a wallet' }).click()
  await page.getByRole('button', { name: /Unlock PumpWizard Wallet/ }).click()
  await page.getByLabel('Wallet password').fill(password)
  await page.getByRole('button', { name: 'Unlock & sign in' }).click()
  await page.locator('.welcome-dialog').getByRole('button', { name: 'Skip', exact: true }).click()
  await expect(page.getByRole('log', { name: 'Status history' })).toContainText('RPC sync test')
}

async function detach(page: Page) {
  const opened = page.waitForEvent('popup')
  await page.getByRole('button', { name: 'Open in new window' }).click()
  const popup = await opened
  await expect(popup.getByRole('heading', { name: 'System activity & AI chat' })).toBeVisible()
  return popup
}

test('detaches only the live section, preserves status history and returns from a collapsed board', async ({ page }, testInfo) => {
  await enterDashboard(page)
  const initial = await page.locator('.command-status-card').evaluateAll((rows) => rows.map((row) => row.getAttribute('data-sequence')))
  let repeatedStartup = 0
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (path === '/api/v1/diagnostics' || (path === '/api/v1/rpc' && request.method() === 'POST')) repeatedStartup++
  })
  await page.locator('.command-board-details > summary').click()
  const popup = await detach(page)
  await expect(page.getByRole('log', { name: 'Status history' })).toHaveCount(0)
  await expect(page.getByRole('main', { name: 'Wallet dashboard' })).toBeVisible()
  await expect(popup.getByRole('log', { name: 'Status history' })).toContainText('RPC sync test')
  expect(await popup.locator('.command-status-card').evaluateAll((rows) => rows.map((row) => row.getAttribute('data-sequence')))).toEqual(initial)
  await expect(popup.getByRole('region', { name: 'Wallet balance and valuation summary' })).toHaveCount(0)
  await expect(popup.getByRole('navigation', { name: 'Primary navigation' })).toHaveCount(0)
  await expect(popup.getByRole('main', { name: 'Wallet dashboard' })).toHaveCount(0)
  await expect.poll(() => popup.locator('.agent-terminal').evaluate((el) => getComputedStyle(el).display)).toBe('flex')
  await popup.screenshot({ path: testInfo.outputPath('detached-activity-desktop.png') })
  await page.evaluate(async () => {
    const state = await (await fetch('/api/v1/app-state')).json() as { wallets: { address: string }[] }
    const response = await fetch('/api/v1/test-strategy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender: state.wallets[0].address, workspace: true, label: 'Detached window strategy', workspace_label: 'Detached monitoring workspace' }) })
    if (!response.ok) throw new Error(await response.text())
  })
  await expect(popup.getByRole('log', { name: 'Status history' })).toContainText('Detached window strategy', { timeout: 15_000 })
  await popup.getByRole('button', { name: 'Open background service controls' }).click()
  await expect(page.getByRole('dialog', { name: 'Background service controls' })).toBeVisible()
  await page.getByRole('dialog', { name: 'Background service controls' }).getByRole('button', { name: 'Done' }).click()
  await page.getByRole('tab', { name: /Wallet/ }).click()
  await expect(popup.getByRole('log', { name: 'Status history' })).toBeVisible()
  await popup.getByRole('button', { name: 'Return to Dashboard' }).click()
  await expect.poll(() => popup.isClosed()).toBe(true)
  await expect(page.getByRole('log', { name: 'Status history' })).toContainText('Detached window strategy')
  expect(repeatedStartup).toBe(0)
})

test('keeps AI settings, drafts and pending approvals usable in the detached window', async ({ page }, testInfo) => {
  await enterDashboard(page)
  let approvals = 0
  page.on('request', (request) => { if (new URL(request.url()).pathname === '/api/v1/assistant/approve') approvals++ })
  const popup = await detach(page)
  await popup.getByRole('button', { name: /^(Add API key|AI settings)$/ }).click()
  const settings = popup.getByRole('dialog', { name: 'Connect AI provider' })
  await expect(settings).toBeVisible()
  await expect(settings.getByRole('button', { name: 'Close dialog' })).toBeFocused()
  await popup.keyboard.press('Shift+Tab')
  await expect(settings.getByRole('button', { name: 'Save local connection' })).toBeFocused()
  await popup.keyboard.press('Escape')
  await expect(settings).toHaveCount(0)
  await popup.getByRole('button', { name: /^(Add API key|AI settings)$/ }).click()
  await settings.getByLabel('Provider').selectOption('deepseek')
  await settings.getByLabel('API key').fill('synthetic-only-detached-provider-key')
  await settings.getByRole('button', { name: 'Save local connection' }).click()
  await expect(settings).toHaveCount(0)
  const message = popup.getByLabel('Assistant message')
  await expect(message).toBeEnabled()
  await message.fill('Keep this unsent draft while returning to the dashboard.')
  await popup.getByRole('button', { name: 'AI settings', exact: true }).click()
  await expect(settings).toBeVisible()
  await popup.keyboard.press('Escape')
  await expect(message).toHaveValue('Keep this unsent draft while returning to the dashboard.')
  await popup.setViewportSize({ width: 390, height: 650 })
  await expect.poll(() => popup.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await popup.screenshot({ path: testInfo.outputPath('detached-chat-compact-narrow.png') })
  await popup.getByRole('button', { name: 'Return to Dashboard' }).click()
  await expect(page.getByLabel('Assistant message')).toHaveValue('Keep this unsent draft while returning to the dashboard.')
  await page.getByRole('button', { name: 'Review send' }).click()
  await expect(page.getByRole('region', { name: 'Approve AI request' })).toBeVisible()
  const pendingWindow = await detach(page)
  await expect(pendingWindow.getByRole('region', { name: 'Approve AI request' })).toContainText('PROVIDER APPROVAL REQUIRED')
  await expect(pendingWindow.getByLabel('Assistant message')).toBeDisabled()
  await pendingWindow.screenshot({ path: testInfo.outputPath('detached-chat-approval.png') })
  await pendingWindow.getByRole('region', { name: 'Approve AI request' }).getByRole('button', { name: 'Cancel' }).click()
  await expect(pendingWindow).toHaveTitle('PumpWizard — System activity & AI chat')
  await expect(pendingWindow.locator('.command-board-stream')).toContainText('No provider request was sent')
  await expect(pendingWindow.getByLabel('Assistant message')).toBeEnabled()
  expect(approvals).toBe(0)
  await pendingWindow.close()
  await expect(page.getByLabel('Assistant message')).toBeEnabled()
  await expect(page.locator('.command-board-stream')).toContainText('No provider request was sent')
})

test('recovers blocked popups and closes the detached window with its dashboard', async ({ page }) => {
  await enterDashboard(page)
  await page.evaluate(() => { window.open = () => null })
  await page.getByRole('button', { name: 'Open in new window' }).click()
  await expect(page.getByRole('alert')).toContainText('Allow pop-ups for this app')
  await expect(page.getByRole('log', { name: 'Status history' })).toBeVisible()
  await page.reload()
  const popup = await detach(page)
  await page.reload()
  await expect.poll(() => popup.isClosed()).toBe(true)
  await expect(page.getByRole('button', { name: 'Open in new window' })).toBeVisible()
})

test('reattaches on native window close and fits a narrow resizable window', async ({ page }, testInfo) => {
  await enterDashboard(page)
  const popup = await detach(page)
  await popup.setViewportSize({ width: 390, height: 650 })
  await expect.poll(() => popup.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await expect(popup.getByRole('button', { name: /^(Add API key|AI settings)$/ })).toBeVisible()
  await popup.screenshot({ path: testInfo.outputPath('detached-activity-narrow.png') })
  await popup.close()
  await expect(page.getByRole('log', { name: 'Status history' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open in new window' })).toBeFocused()
  const secondWindow = await detach(page)
  await page.getByRole('button', { name: 'Focus window' }).click()
  await expect(secondWindow.getByRole('log', { name: 'Status history' })).toBeVisible()
  await page.getByRole('button', { name: 'Return here' }).click()
  await expect.poll(() => secondWindow.isClosed()).toBe(true)
  await expect(page.getByRole('log', { name: 'Status history' })).toBeVisible()
})

test('closes the detached view on wallet sign-out and on dashboard closure', async ({ page }) => {
  await enterDashboard(page)
  const popup = await detach(page)
  await page.getByRole('button', { name: 'Sign out of wallet' }).click()
  await expect.poll(() => popup.isClosed()).toBe(true)
  await expect(page.getByRole('region', { name: 'Wallet connection' })).toContainText('No wallet connected')
  const researchWindow = await detach(page)
  await expect(researchWindow.locator('.command-board-popout-bar')).toContainText('Research workspace')
  await page.close()
  await expect.poll(() => researchWindow.isClosed()).toBe(true)
})

test('keeps the detached window through startup unload events and restores after real navigation', async ({ page }) => {
  await enterDashboard(page)
  const popup = await detach(page)
  await popup.evaluate(() => {
    window.dispatchEvent(new Event('beforeunload'))
    window.dispatchEvent(new PageTransitionEvent('pagehide'))
  })
  // The next service read gives both the lifecycle check and normal app refresh
  // a chance to run; the live document must remain in its companion window.
  await page.waitForResponse((response) => new URL(response.url()).pathname === '/api/v1/service/status')
  expect(popup.isClosed()).toBe(false)
  await expect(popup.getByRole('log', { name: 'Status history' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Focus window' })).toBeVisible()
  await popup.goto('about:blank')
  await expect(page.getByRole('log', { name: 'Status history' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open in new window' })).toBeVisible()
  if (!popup.isClosed()) await popup.close()
})
