import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import type { WalletPortfolioState } from '../src/useWalletPortfolio'
import { appendBalanceSample, balanceSamples, solBalance, validLamports } from '../src/walletBalanceHistory'
import { resetSavedAccounts } from './savedAccounts'

async function enterWallet(page: Page) {
  await resetSavedAccounts(page)
  await page.goto('/')
  const password = 'native wallet history fixture password'
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
  await expect.poll(() => page.evaluate(async () => (await (await fetch('/api/v1/app-state')).json()).preferences['pumpwizard.welcome-acknowledged-version/v1'])).toMatch(/^0\.2\.0\.dev\d+$/)
}

test('native balance history keeps exact lamports, unknown prices and bounded buckets', () => {
  expect(solBalance('2000000')).toBe('0.002 SOL')
  expect(solBalance('1')).toBe('0.000000001 SOL')
  expect(validLamports('18446744073709551615')).toBe(true)
  for (const value of ['-1', '01', '1.5', '18446744073709551616', '١', 1]) expect(validLamports(value)).toBe(false)
  let rows = appendBalanceSample([], { at: 900, lamports: '1', usd: null })
  rows = appendBalanceSample(rows, { at: 950, lamports: '2000000', usd: .24 })
  expect(rows).toEqual([{ at: 950, lamports: '2000000', usd: .24 }])
  rows = appendBalanceSample(rows, { at: 1800, lamports: '2000000', usd: null })
  expect(balanceSamples(JSON.stringify(rows))).toEqual(rows)
  expect(balanceSamples(JSON.stringify([...rows].reverse()))).toEqual([])
  expect(balanceSamples(JSON.stringify([{ ...rows[0], secret: 'reject' }]))).toEqual([])
  rows = appendBalanceSample(rows, { at: 7 * 86400 + 1801, lamports: '0', usd: 0 })
  expect(rows).toHaveLength(1)
})

test('combined native balance switches units and inspects saved history across reload and mobile', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1600, height: 1000 })
  await enterWallet(page)
  const summary = page.getByRole('region', { name: 'Wallet balance and valuation summary' })
  await expect(summary.locator('.wallet-balance-amounts b')).toHaveText('1.25 SOL')
  await expect(summary.locator('.wallet-balance-amounts em')).toHaveText('≈$125.00')
  const fixture = await page.evaluate(async () => {
    const state = await (await fetch('/api/v1/app-state')).json() as { wallets: { address: string }[] }
    const sender = state.wallets[0].address
    const key = `pumpwizard.wallet-balance-history/v1:mainnet-beta:${sender}`
    const at = Math.floor(Date.now() / 1000)
    const rows = [{ at: at - 90000, lamports: '1000000000', usd: 100 }, { at: at - 2700, lamports: '1500000000', usd: 120 }, { at: at - 1800, lamports: '1500000000', usd: null }, { at: at - 900, lamports: '1600000000', usd: 144 }]
    const response = await fetch('/api/v1/app-state/preferences', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key, value: JSON.stringify(rows) }) })
    if (!response.ok) throw new Error(await response.text())
    return { key, sender }
  })
  await page.reload()
  await expect(summary.locator('.wallet-balance-amounts em')).toHaveText('≈$125.00')
  await summary.getByRole('button', { name: 'Show wallet balance in USD' }).focus()
  await page.keyboard.press('Enter')
  await expect(summary.locator('.wallet-balance-amounts b')).toHaveText('≈$125.00')
  await expect(summary.locator('.wallet-balance-amounts em')).toHaveText('1.25 SOL')
  await expect.poll(() => page.evaluate(async (sender) => (await (await fetch('/api/v1/app-state')).json()).preferences[`pumpwizard.wallet-balance-unit/v1:mainnet-beta:${sender}`], fixture.sender)).toBe('USD')
  await page.reload()
  await expect(summary.locator('.wallet-balance-amounts b')).toHaveText('≈$125.00')
  await summary.getByRole('button', { name: 'Open wallet balance history' }).click()
  const dialog = page.getByRole('dialog', { name: 'Wallet balance history', exact: true })
  const chart = dialog.getByRole('region', { name: 'Wallet balance chart', exact: true })
  await expect(dialog).toContainText('earliest available confirmed transaction')
  await expect(chart.locator('svg path')).toHaveCount(1)
  expect((await chart.locator('svg path').getAttribute('d'))!.match(/M/g)!.length).toBeGreaterThan(1)
  await chart.getByRole('img').focus()
  await page.keyboard.press('ArrowLeft')
  await expect(chart.locator('footer')).toContainText('$144.00')
  await page.keyboard.press('ArrowLeft')
  await expect(chart.locator('footer')).toContainText('Unavailable')
  await dialog.getByRole('group', { name: 'History currency' }).getByRole('button', { name: 'SOL', exact: true }).click()
  await chart.getByRole('img').focus()
  await page.keyboard.press('ArrowLeft')
  await expect(chart.locator('footer')).toContainText('1.6 SOL')
  await page.keyboard.press('ArrowLeft')
  await expect(chart.locator('footer')).toContainText('1.5 SOL')
  await dialog.getByRole('group', { name: 'Wallet history range' }).getByRole('button', { name: '7d' }).click()
  await expect(chart.getByRole('img')).toHaveAttribute('aria-label', /5 observed snapshots/)
  await page.screenshot({ path: testInfo.outputPath('native-wallet-history-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await expect(dialog).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('native-wallet-history-mobile.png'), fullPage: true })
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
  await expect(summary.getByRole('button', { name: 'Open wallet balance history' })).toBeFocused()
  await expect(summary.locator('.wallet-balance-amounts b')).toHaveText('1.25 SOL')
  const sent = await page.evaluate(async (sender) => (await (await fetch('/api/v1/test-chain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender, changes: {} }) })).json()).sent, fixture.sender)
  expect(sent).toBe(0)
})

test('native wallet history starts with a real reading and retains SOL when USD is unknown', async ({ page }) => {
  await page.route('**/api/v1/wallet/balance?**', async (route) => {
    const response = await route.fetch({ headers: { ...await route.request().allHeaders(), Origin: new URL(route.request().url()).origin } })
    const balance = await response.json()
    await route.fulfill({ response, json: { ...balance, estimated_usd: null, sol_usd_price: null } })
  })
  await page.route('**/api/v1/wallet/portfolio?**', async (route) => {
    const response = await route.fetch({ headers: { ...await route.request().allHeaders(), Origin: new URL(route.request().url()).origin, 'Sec-Fetch-Site': 'same-origin' } })
    const portfolio = await response.json() as WalletPortfolioState
    await route.fulfill({ response, json: { ...portfolio, sol_usd_price: null, estimated_usd: null } })
  })
  await enterWallet(page)
  const summary = page.getByRole('region', { name: 'Wallet balance and valuation summary' })
  await expect(summary.locator('.wallet-balance-amounts b')).toHaveText('1.25 SOL')
  await expect(summary.locator('.wallet-balance-amounts em')).toHaveText('USD price offline')
  await summary.getByRole('button', { name: 'Show wallet balance in USD' }).click()
  await expect(summary.locator('.wallet-balance-amounts b')).toHaveText('1.25 SOL')
  await summary.getByRole('button', { name: 'Show wallet balance in SOL' }).click()
  await expect.poll(() => page.evaluate(async () => {
    const state = await (await fetch('/api/v1/app-state')).json() as { wallets: { address: string }[]; preferences: Record<string, string> }
    return JSON.parse(state.preferences[`pumpwizard.wallet-balance-history/v1:mainnet-beta:${state.wallets[0].address}`] || '[]')
  })).toEqual([{ at: expect.any(Number), lamports: '1250000000', usd: null }])
  await summary.getByRole('button', { name: 'Open wallet balance history' }).click()
  const dialog = page.getByRole('dialog', { name: 'Wallet balance history', exact: true })
  await expect(dialog.locator('.dashboard-chart footer')).toContainText('First snapshot')
  await expect(dialog.locator('svg circle')).toHaveCount(1)
  await dialog.getByRole('group', { name: 'History currency' }).getByRole('button', { name: 'USD', exact: true }).click()
  await expect(dialog).toContainText('A SOL/USD price is needed')
  await expect(dialog.locator('.dashboard-chart header b')).toHaveText('Unavailable')
})

test('wider wallet card shows an inline chart from first available activity through today', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1600, height: 1000 })
  const start = Math.floor(Date.now() / 1000) - 500 * 86400
  await page.route('**/api/v1/wallet/balance-history?**', async (route) => {
    const params = new URL(route.request().url()).searchParams
    await route.fulfill({ json: { sender: params.get('sender'), network: params.get('network'), checked_at: Date.now() / 1000,
      history_complete: true, transactions: 2, missing_transactions: 0, limited: false,
      rows: [{ at: start, lamports: '0', usd: params.get('currency') === 'USD' ? 0 : null, continuous: false }, { at: start, lamports: '1000000000', usd: params.get('currency') === 'USD' ? 80 : null, continuous: true },
        { at: start + 100 * 86400, lamports: '1000000000', usd: params.get('currency') === 'USD' ? 70 : null, continuous: true }, { at: start + 100 * 86400, lamports: '1250000000', usd: params.get('currency') === 'USD' ? 87.5 : null, continuous: true },
        { at: Date.now() / 1000, lamports: '1250000000', usd: params.get('currency') === 'USD' ? 125 : null, continuous: true }] } })
  })
  await enterWallet(page)
  const summary = page.getByRole('region', { name: 'Wallet balance and valuation summary' })
  const mini = summary.getByRole('region', { name: 'Wallet balance mini chart', exact: true })
  await expect(mini).toBeVisible()
  await expect(mini).toContainText('Today')
  await expect(mini).toContainText(new Date(start * 1000).getFullYear().toString())
  await expect(mini.locator('.wallet-sparkline-line')).toHaveAttribute('d', /H.*V/)
  await expect(mini.locator('.wallet-chart-axis')).toHaveCount(6)
  expect((await mini.getByRole('img').boundingBox())!.height).toBe(58)
  expect((await summary.boundingBox())!.height).toBeLessThanOrEqual(190)
  await expect(mini.getByRole('group', { name: 'Inline wallet history range' }).getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'true')
  expect((await summary.boundingBox())!.width).toBeGreaterThanOrEqual(430)
  await mini.getByRole('img').focus()
  await page.keyboard.press('ArrowLeft')
  await page.keyboard.press('ArrowLeft')
  await page.keyboard.press('ArrowLeft')
  await expect(mini.locator('.wallet-sparkline-caption')).toContainText('1 SOL')
  await summary.getByRole('button', { name: 'Show wallet balance in USD' }).click()
  await expect(summary.locator('.wallet-balance-amounts b')).toHaveText('≈$125.00')
  await expect(mini).toContainText('USD history')
  await expect(mini.getByRole('img')).toHaveAttribute('aria-label', /^Native USD balance/)
  await expect(mini.locator('.wallet-sparkline-line')).toHaveAttribute('d', /H.*V/)
  await expect(mini.locator('.wallet-chart-axis').first()).toContainText('$')
  await mini.getByRole('img').focus()
  for (let index = 0; index < 10; index++) await page.keyboard.press('ArrowLeft')
  await page.keyboard.press('ArrowRight')
  await expect(mini.locator('.wallet-sparkline-caption')).toContainText('$80.00')
  await page.screenshot({ path: testInfo.outputPath('inline-wallet-history-desktop.png'), fullPage: true })
  await summary.getByRole('button', { name: 'Open wallet balance history' }).click()
  const dialog = page.getByRole('dialog', { name: 'Wallet balance history', exact: true })
  await dialog.getByRole('group', { name: 'History currency' }).getByRole('button', { name: 'SOL', exact: true }).click()
  await dialog.getByRole('group', { name: 'Wallet history range' }).getByRole('button', { name: 'All', exact: true }).click()
  expect(Number((await dialog.locator('.dashboard-chart-plot').getAttribute('aria-label'))!.match(/(\d+) observed snapshots/)![1])).toBeGreaterThanOrEqual(5)
  await expect(dialog).toContainText('Wallet creation dates are not supplied')
  await page.keyboard.press('Escape')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(mini).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('inline-wallet-history-mobile.png'), fullPage: true })
})

test('inline wallet history leaves missing transaction gaps and recovers from earlier-history failure', async ({ page }) => {
  let offline = true
  await page.route('**/api/v1/wallet/balance-history?**', async (route) => {
    if (offline) { await route.fulfill({ status: 502, json: { error: 'History RPC unavailable' } }); return }
    const params = new URL(route.request().url()).searchParams
    const at = Date.now() / 1000
    await route.fulfill({ json: { sender: params.get('sender'), network: params.get('network'), checked_at: at,
      history_complete: false, transactions: 3, missing_transactions: 1, limited: false,
      rows: [{ at: at - 3000, lamports: '1000000000', usd: null, continuous: false },
        { at: at - 2000, lamports: null, usd: null, continuous: false }, { at, lamports: '1250000000', usd: null, continuous: false }] } })
  })
  await enterWallet(page)
  const summary = page.getByRole('region', { name: 'Wallet balance and valuation summary' })
  const mini = summary.getByRole('region', { name: 'Wallet balance mini chart', exact: true })
  await expect(summary.locator('.wallet-balance-amounts b')).toHaveText('1.25 SOL')
  await expect(mini).toContainText('Earlier history unavailable')
  await summary.getByRole('button', { name: 'Open wallet balance history' }).click()
  const dialog = page.getByRole('dialog', { name: 'Wallet balance history', exact: true })
  offline = false
  await dialog.getByRole('button', { name: 'Retry history', exact: true }).click()
  await expect(dialog.getByRole('button', { name: 'Retry history', exact: true })).toHaveCount(0)
  await page.keyboard.press('Escape')
  await expect(mini).toContainText('Partial history')
  expect((await mini.locator('.wallet-sparkline-line').getAttribute('d'))!.match(/M/g)).toHaveLength(2)
})

test('public balance and chart remain visible after ownership proof expires while signing stays locked', async ({ page }) => {
  await enterWallet(page)
  const summary = page.getByRole('region', { name: 'Wallet balance and valuation summary' })
  await expect(summary.locator('.wallet-balance-amounts em')).toHaveText('≈$125.00')
  const response = await page.evaluate(async () => {
    const state = await (await fetch('/api/v1/app-state')).json()
    const view = JSON.parse(state.preferences['pumpwizard.wallet-session/v1'])
    await fetch('/api/v1/wallet/disconnect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender: view.address, network: view.network }) })
    return (await fetch(`/api/v1/wallet/portfolio?sender=${view.address}&network=${view.network}`)).status
  })
  expect(response).toBe(400)
  let restoreAttempt = true
  await page.route('**/api/v1/wallet/balance?**', async (route) => {
    if (restoreAttempt) { restoreAttempt = false; await route.fulfill({ status: 502, json: { error: 'RPC temporarily offline' } }) }
    else await route.continue()
  })
  await page.reload()
  await expect(page.getByRole('main', { name: 'Wallet dashboard' })).toBeVisible()
  await expect(summary.locator('.wallet-balance-amounts em')).toHaveText('≈$125.00')
  await expect(page.getByRole('region', { name: 'Wallet connection' })).toContainText('signing locked')
  await expect(page.getByRole('button', { name: 'Unlock signing', exact: true })).toBeVisible()
})

test('inline history automatically continues backfill until all receipts are confirmed', async ({ page }) => {
  let reads = 0
  await page.route('**/api/v1/wallet/balance-history?**', async (route) => {
    const query = new URL(route.request().url()).searchParams
    const at = Date.now() / 1000, complete = ++reads > 1
    await route.fulfill({ json: { sender: query.get('sender'), network: query.get('network'), checked_at: at,
      transactions: 2, loaded_transactions: complete ? 2 : 1, missing_transactions: complete ? 0 : 1,
      limited: false, sync_pending: !complete, retry_after: 3, history_complete: complete,
      rows: [{ at: at - 200000, lamports: '0', usd: null, continuous: false }, { at: at - 200000, lamports: '1000000000', usd: null, continuous: true },
        { at: at - 100000, lamports: complete ? '1250000000' : null, usd: null, continuous: complete }, { at, lamports: '1250000000', usd: null, continuous: complete }] } })
  })
  await enterWallet(page)
  const chart = page.getByRole('region', { name: 'Wallet balance mini chart', exact: true })
  await expect(chart).toContainText('Syncing 1/2 transactions')
  await expect(chart).toContainText('2 transactions · confirmed', { timeout: 12000 })
  await expect(chart.locator('.wallet-sparkline-line')).toHaveAttribute('d', /H.*V/)
  await expect(page.locator('.wallet-balance-amounts em')).toHaveText('≈$125.00')
})
