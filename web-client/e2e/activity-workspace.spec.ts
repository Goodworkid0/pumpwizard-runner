import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import { readFile } from 'node:fs/promises'
import type { PortfolioSession, PortfolioTradeState } from '../src/PortfolioWorkspace'
import { activityUsd, awaitingReceipt, csvText, exactSol, inActivityRange, latestDecisions, numberValue, orderTimeline, recordedFees } from '../src/activityData'
import type { ActivityOrder } from '../src/activityData'
import { resetSavedAccounts } from './savedAccounts'

const mint = 'cGfHiC6Kgg3FpFZvgwGcswsCRtp4aBP2fzuXRQPizuN'

test('activity data preserves missing inputs, exact fees, snapshot ownership and submission counts', () => {
  expect(numberValue(null)).toBeNull()
  expect(numberValue(' ')).toBeNull()
  expect(numberValue(false)).toBeNull()
  expect(numberValue('0')).toBe(0)
  expect(activityUsd('0.000000000024')).not.toBe('$0')
  expect(awaitingReceipt('confirmed')).toBe(true)
  expect(awaitingReceipt('unknown')).toBe(true)
  expect(awaitingReceipt('cancelled')).toBe(false)
  expect(awaitingReceipt('expired')).toBe(false)
  const orders = [{ created_at: 950, side: 'buy', receipt: { fee_lamports: '9007199254740993' } }, { created_at: 1000, side: 'sell', receipt: { fee_lamports: '7' } }, { created_at: NaN, side: 'buy', receipt: null }] as ActivityOrder[]
  const fees = recordedFees(orders)
  expect(fees).toEqual({ atoms: 9007199254741000n, known: 2, total: 3 })
  expect(exactSol(fees.atoms)).toBe('9,007,199.254741 SOL')
  expect(exactSol(null)).toBe('Unavailable')
  const timeline = orderTimeline(orders, 'all', 1000)
  expect(timeline.undated).toBe(1)
  expect(timeline.buckets.reduce((n, row) => n + row.buy + row.sell, 0)).toBe(2)
  expect(orderTimeline([], 'all', 1000).buckets.every((row) => Number.isFinite(row.at))).toBe(true)
  expect(inActivityRange(1001, '24h', 1000)).toBe(false)
  const candidate = { mint: 'held', at: 950, kind: 'entry_blocked', reason: 'warming up', payload: {}, observation: null }
  const sessions = [{ instance: { id: 'a', label: 'A' }, positions: [{ mint: 'held', status: 'closed' }], entry_considerations: [candidate] }, { instance: { id: 'b', label: 'B' }, positions: [{ mint: 'held', status: 'open' }], entry_considerations: [candidate] }] as PortfolioSession[]
  expect(latestDecisions(sessions).map((row) => row.strategyId)).toEqual(['a'])
  expect(csvText(['Token', 'Atoms'], [['=unsafe()', '9007199254740993'], ['A,"B', null]])).toContain('"\'=unsafe()","9007199254740993"')
  expect(csvText(['Token'], [['A,"B']])).toContain('"A,""B"')
})

async function enter(page: Page) {
  await resetSavedAccounts(page)
  await page.goto('/')
  await page.getByRole('button', { name: 'Create a new wallet' }).click()
  await page.getByLabel('Password', { exact: true }).fill('synthetic activity dashboard password')
  await page.getByLabel('Confirm password').fill('synthetic activity dashboard password')
  await page.getByRole('button', { name: 'Create encrypted wallet' }).click()
  await page.getByLabel(/I saved the encrypted backup/).check()
  await page.getByRole('button', { name: 'Choose a wallet' }).click()
  await page.getByRole('button', { name: /Unlock PumpWizard Wallet/ }).click()
  await page.getByLabel('Wallet password').fill('synthetic activity dashboard password')
  await page.getByRole('button', { name: 'Unlock & sign in' }).click()
  await page.locator('.welcome-dialog').getByRole('button', { name: 'Skip', exact: true }).click()
}

async function fixture(page: Page) {
  const info = await page.evaluate(async () => {
    const sender = (await (await fetch('/api/v1/app-state')).json()).wallets[0].address
    const create = async (body: Record<string, unknown>) => {
      const response = await fetch('/api/v1/test-strategy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender, ...body }) })
      if (!response.ok) throw new Error(await response.text())
      return response.json() as Promise<{ id: string; workspace_id: string }>
    }
    const first = await create({ workspace: true, label: 'Activity momentum', workspace_label: 'Activity session' })
    const second = await create({ workspace_id: first.workspace_id, label: 'Activity breakout' })
    const outside = await create({ workspace: true, label: 'Outside strategy', workspace_label: 'Outside session' })
    return { sender, first, second, outside }
  })
  const now = Date.now() / 1000
  let shift = 0
  const ids = [info.first.id, info.second.id, info.outside.id]
  await page.route('**/api/v1/sessions?**', async (route) => {
    const response = await route.fetch({ headers: { ...await route.request().allHeaders(), Origin: new URL(route.request().url()).origin, 'Sec-Fetch-Site': 'same-origin' } })
    const data = await response.json()
    data.rows = data.rows.map((row: PortfolioSession) => {
      if (!ids.includes(row.instance.id)) return row
      row.monitor_plan = { ...row.monitor_plan, entry_cap_min: row.instance.id === info.outside.id ? 5000 : 8000, entry_cap_max: 25000 }
      row.positions = []
      row.entry_considerations = Array.from({ length: 12 }, (_, i) => ({
        at: (i < 9 ? now - i * 200 : now - 259200 - i) + shift, kind: i % 3 === 0 ? 'entry_recommended' as const : 'entry_blocked' as const,
        mint: i === 0 ? mint : `Candidate${i}1111111111111111111111111111111`,
        reason: i % 3 === 0 ? 'strategy_entry_signal' : i % 3 === 1 ? 'Warming up: insufficient continuous observations' : 'Outside entry market-cap band', payload: {},
        observation: i === 10 ? null : { mint: i === 0 ? mint : `Candidate${i}1111111111111111111111111111111`, symbol: i === 0 ? 'ALPHA' : `CAND${i}`, observed_at: now - i * 200 - 10 + shift, price_usd: .000005, market_cap_usd: i % 3 === 0 ? 12000 + i * 500 : 5000 + i * 4000, liquidity_usd: i === 11 ? 0 : 1200 + i * 400, metadata: { name: i === 0 ? 'Alpha Coin' : `Candidate ${i}` } },
      }))
      return row
    })
    await route.fulfill({ response, json: data })
  })
  const statuses = ['finalized', 'pending', 'confirmed', 'unknown', 'failed', 'cancelled', 'expired']
  const owners = [info.first.id, info.second.id, info.outside.id, null]
  const orders: PortfolioTradeState['orders'] = Array.from({ length: 27 }, (_, i) => ({
    id: `activity-order-${i}`, mint, agent_id: owners[i % 4], position_id: null, signature: `SyntheticSignature${i}`, status: statuses[i % 7],
    side: i % 2 ? 'sell' : 'buy', network: 'mainnet-beta', amount: i % 2 ? '1000000' : '50000000', decimals: 6,
    created_at: now - i * 3600, detail: i % 7 === 3 ? 'Submission outcome unknown; retain original signature.' : 'Synthetic recorded order', route: 'Synthetic PumpSwap',
    receipt: i % 7 === 0 || i % 7 === 4 ? { wallet_delta: '-15000000', token_delta: '2000000', fee_lamports: '5000', slot: 91, block_time: now - i * 3600 + 10, cost_basis: 'Synthetic receipt' } : null,
  }))
  await page.route('**/api/v1/trades?**', (route) => route.fulfill({ json: { sender: info.sender, network: 'mainnet-beta', positions: [], withdrawals: [], pending: true, orders } }))
  await page.evaluate(() => { Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async (text: string) => { (window as unknown as { copied: string }).copied = text } } }) })
  await page.getByRole('tab', { name: /Activity/ }).click()
  await page.getByRole('button', { name: 'Refresh activity ↻' }).click()
  return { ...info, advance: () => { shift = 30 } }
}

for (const width of [1540, 390]) test(`activity charts filters receipts exports and selection stay usable at ${width}px`, async ({ page }, testInfo) => {
  test.setTimeout(75_000)
  await page.setViewportSize({ width, height: 1000 })
  await enter(page)
  const info = await fixture(page)
  let orderWrites = 0
  page.on('request', (request) => { if (request.method() === 'POST' && /\/trades\/(preview|submit)/.test(request.url())) orderWrites++ })
  const orders = page.getByRole('region', { name: 'Orders workspace' })
  await expect(orders.locator('.trade-history-row')).toHaveCount(20)
  await expect(orders.getByRole('img', { name: 'Order submissions over time' })).toBeVisible()
  await orders.getByRole('button', { name: 'Next activity page' }).click()
  await expect(orders.locator('.trade-history-row')).toHaveCount(7)
  const exported = page.waitForEvent('download')
  await orders.getByRole('button', { name: 'Export orders CSV' }).click()
  const download = await exported
  const csv = await readFile((await download.path())!, 'utf8')
  expect(csv.trim().split('\r\n')).toHaveLength(28)
  expect(csv).toContain('"activity-order-26"')
  await orders.getByLabel('Search orders').fill('activity-order-3')
  await expect(orders.locator('.trade-history-row')).toHaveCount(1)
  await orders.getByRole('button', { name: 'Inspect order activity-order-3' }).click()
  const receipt = orders.getByRole('region', { name: 'Order receipt details' })
  await expect(receipt).toContainText('does not confirm a fill')
  await expect(receipt).toContainText('Unavailable')
  await receipt.getByRole('button', { name: 'Copy signature' }).click()
  expect(await page.evaluate(() => (window as unknown as { copied: string }).copied)).toBe('SyntheticSignature3')
  await orders.getByLabel('Search orders').fill('activity-order-0')
  await orders.getByRole('button', { name: 'Inspect order activity-order-0' }).click()
  await expect(receipt).toContainText('0.000005 SOL')
  await expect(receipt).toContainText('−0.015 SOL')
  await orders.getByRole('button', { name: 'Clear filters', exact: true }).click()
  await orders.getByRole('group', { name: 'Order status', exact: true }).getByRole('button', { name: /^Cancelled/ }).click()
  await expect(orders.locator('.trade-history-row')).toHaveCount(7)
  await expect(orders).not.toContainText('Pending final receipt')
  await orders.getByRole('button', { name: 'Clear filters', exact: true }).click()
  await orders.getByRole('group', { name: 'Order time range' }).getByRole('button', { name: '24h', exact: true }).click()
  await expect(orders.locator('.activity-list-heading')).toContainText('24 matching')
  await orders.getByRole('button', { name: 'Clear filters', exact: true }).click()
  await orders.screenshot({ path: testInfo.outputPath(`orders-${width}.png`) })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.getByRole('button', { name: 'Decisions', exact: true }).click()
  const decisions = page.getByRole('region', { name: 'Decisions workspace' })
  await expect(decisions.locator('.activity-decision-row')).toHaveCount(20)
  await expect(decisions.getByRole('img', { name: 'Saved market cap and liquidity by candidate' })).toBeVisible()
  await expect(decisions).toContainText('3 missing usable cap/liquidity')
  await decisions.getByRole('button', { name: 'Next activity page' }).click()
  await expect(decisions.locator('.activity-decision-row')).toHaveCount(16)
  const allDecisions = page.waitForEvent('download')
  await decisions.getByRole('button', { name: 'Export decisions CSV' }).click()
  expect((await readFile((await (await allDecisions).path())!, 'utf8')).trim().split('\r\n')).toHaveLength(37)
  await decisions.getByRole('group', { name: 'Observation filters' }).getByRole('button', { name: /^Enter/ }).click()
  await expect(decisions.locator('.activity-decision-row')).toHaveCount(12)
  await decisions.getByLabel('Decision strategy').selectOption(info.first.id)
  await expect(decisions.locator('.activity-decision-row')).toHaveCount(4)
  await decisions.getByRole('combobox', { name: /Decision reason/ }).selectOption('Market cap')
  await expect(decisions).toContainText('No decisions match these filters')
  await decisions.getByRole('button', { name: 'Clear filters', exact: true }).click()
  const folder = page.getByRole('article', { name: 'Activity session session', exact: true })
  await folder.getByRole('button', { name: 'Select Activity session', exact: true }).click()
  await expect(decisions.getByLabel('Decision strategy').locator('option')).toHaveCount(3)
  await expect(decisions).not.toContainText('Outside strategy')
  await expect(decisions).toContainText('Shaded entry range: $8,000–$25,000')
  await decisions.getByLabel('Decision strategy').selectOption(info.first.id)
  await expect(decisions.locator('.activity-decision-row')).toHaveCount(12)
  const point = decisions.locator('.decision-market-chart svg g[role="button"]').first()
  await point.focus(); await page.keyboard.press('Enter')
  const detail = decisions.getByRole('region', { name: 'Saved decision details' })
  await expect(detail).toContainText('ALPHA')
  await expect(detail).toContainText('$12,000')
  const inspectedTime = detail.locator('dl > div').filter({ has: page.getByText('Decision time', { exact: true }) }).locator('dd')
  const savedBefore = await inspectedTime.innerText()
  info.advance()
  await page.getByRole('button', { name: 'Refresh activity ↻' }).click()
  await expect(inspectedTime).not.toHaveText(savedBefore)
  await expect(detail).toBeVisible()
  await expect(detail).toContainText('$12,000')
  await detail.getByRole('button', { name: 'Copy mint' }).click()
  expect(await page.evaluate(() => (window as unknown as { copied: string }).copied)).toBe(mint)
  await detail.getByRole('button', { name: 'Open token chart ↗' }).click()
  const chart = page.getByRole('dialog', { name: 'Token details' })
  await expect(chart).toBeVisible()
  await chart.getByRole('button', { name: 'Close dialog' }).click()
  await decisions.screenshot({ path: testInfo.outputPath(`decisions-${width}.png`) })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.getByRole('button', { name: 'Orders', exact: true }).click()
  await expect(orders.locator('.trade-history-row')).toHaveCount(14)
  await expect(orders).not.toContainText('Outside strategy')
  await page.getByRole('button', { name: 'All wallet activity', exact: true }).click()
  await page.getByRole('tab', { name: /Activity/ }).click()
  await expect(orders.locator('.trade-history-row')).toHaveCount(20)
  expect(orderWrites).toBe(0)
  await page.unrouteAll({ behavior: 'wait' })
})

test('activity unavailable records and missing decision inputs stay explicit', async ({ page }) => {
  await enter(page)
  await fixture(page)
  await page.route('**/api/v1/trades?**', (route) => route.fulfill({ status: 503, json: { error: 'Synthetic order records unavailable' } }))
  await page.getByRole('button', { name: 'Refresh activity ↻' }).click()
  const orders = page.getByRole('region', { name: 'Orders workspace' })
  await expect(orders.getByRole('status')).toContainText('Order records unavailable')
  await expect(orders.locator('.activity-summary')).toContainText('Unavailable')
  await expect(orders.getByRole('button', { name: 'Export orders CSV' })).toBeDisabled()
  await page.getByRole('button', { name: 'Decisions', exact: true }).click()
  const decisions = page.getByRole('region', { name: 'Decisions workspace' })
  await decisions.getByLabel('Search decisions').fill('Candidate10')
  await expect(decisions.locator('.activity-decision-row')).toHaveCount(3)
  await expect(decisions).toContainText('Saved cap and liquidity are unavailable')
  await expect(decisions.locator('.activity-record-table')).toContainText('Unavailable')
  expect(await decisions.innerHTML()).not.toMatch(/NaN|Infinity/)
  await page.unrouteAll({ behavior: 'wait' })
})
