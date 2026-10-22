import { resetSavedAccounts } from './savedAccounts'
import { expect, request as playwrightRequest, test } from '@playwright/test'
import type { Page, Route } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import nacl from 'tweetnacl'
import { ComputeBudgetInstruction, ComputeBudgetProgram, PublicKey, Transaction, TransactionInstruction, TransactionMessage, VersionedTransaction } from '@solana/web3.js'

const recordingPath = resolve('../examples/core-frequency-replay.json')
const recording = JSON.parse(readFileSync(recordingPath, 'utf8'))
const currentVersion = readFileSync(resolve('../pyproject.toml'), 'utf8').match(/^version = "([^"]+)"$/m)?.[1]
if (!currentVersion) throw new Error('Runner version is missing from pyproject.toml')
const strategy = {
  schema: 'pumpwizard.strategy-handoff/v1',
  strategy: { id: 'core-frequency', label: 'Browser E2E core' },
  configuration: recording.configuration,
}
test.beforeEach(async ({ page }) => { await resetSavedAccounts(page) })

const password = 'correct horse battery staple'

async function openSelectedStrategyControls(page: import('@playwright/test').Page) {
  if (await page.getByRole('main', { name: 'Wallet dashboard' }).isVisible()) {
    await page.locator('.trading-focus-header').getByRole('button', { name: 'Strategy controls', exact: true }).click()
  }
  await expect(page.getByRole('dialog', { name: 'Session monitor' })).toBeVisible()
}

test('configures and tests a private RPC without a wallet or browser-stored key', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'RPC connection', exact: true }).click()
  let dialog = page.getByRole('dialog', { name: 'RPC connection' })
  await expect(dialog).toContainText('Solana public RPC')
  await expect(dialog.getByLabel('Mainnet RPC URL')).toHaveAttribute('type', 'password')
  await dialog.getByLabel('Mainnet RPC URL').fill('https://wrong-network.example.com')
  await dialog.getByRole('button', { name: 'Test & save' }).click()
  await expect(dialog.getByRole('alert')).toContainText('network does not match')
  await expect(dialog).toContainText('Solana public RPC')
  const secret = 'synthetic-browser-rpc-key'
  await dialog.getByLabel('Mainnet RPC URL').fill(`https://mainnet.helius-rpc.com/?api-key=${secret}`)
  const response = page.waitForResponse((result) => result.url().endsWith('/api/v1/rpc') && result.request().method() === 'POST')
  await dialog.getByRole('button', { name: 'Test & save' }).click()
  expect(await (await response).text()).not.toContain(secret)
  await expect(dialog.getByRole('status')).toContainText('verified and saved')
  await expect(dialog.getByLabel('Mainnet RPC URL')).toHaveValue('')
  expect(await page.evaluate(async () => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain(secret)
  await page.reload()
  await page.getByRole('button', { name: 'RPC connection', exact: true }).click()
  dialog = page.getByRole('dialog', { name: 'RPC connection' })
  await expect(dialog).toContainText('Helius · saved on this computer')
  await dialog.getByRole('button', { name: 'Test connection', exact: true }).click()
  await expect(dialog.getByRole('status')).toContainText('blockhash verified')
  await dialog.getByRole('button', { name: 'Remove saved connection' }).click()
  await expect(dialog).toContainText('Solana public RPC')
})

test('enters authenticated research and creates a monitoring workspace without wallet effects', async ({ page }) => {
  await page.goto('/')
  const capabilities = await page.evaluate(async () => {
    const response = await fetch('/api/v1/capabilities')
    return response.json() as Promise<{ capabilities: {
      schema: string
      authentication: { state: string; wallet_identity: boolean }
      research: { state: string; wallet_required: boolean; ai_provider_required: boolean }
      production_execution: { state: string; automatic_execution: boolean }
      read_effects: Record<string, boolean>
    } }>
  })
  expect(capabilities.capabilities).toEqual(expect.objectContaining({
    schema: 'pumpwizard.first-use-capabilities/v1',
    authentication: expect.objectContaining({ state: 'authenticated', wallet_identity: false }),
    research: expect.objectContaining({ state: 'available', wallet_required: false, ai_provider_required: false }),
    production_execution: expect.objectContaining({ state: 'blocked', automatic_execution: false }),
  }))
  expect(Object.values(capabilities.capabilities.read_effects).every((value) => value === false)).toBe(true)
  const counters = async () => page.evaluate(async () => {
    const response = await fetch('/api/v1/test-batch7-read-only-counters', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    return response.json() as Promise<Record<string, number | string>>
  })
  const before = await counters()

  await page.getByRole('button', { name: 'Explore research' }).click()
  await expect(page.getByRole('heading', { name: 'Understand one strategy before risking funds.' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Connect wallet', exact: true })).toBeVisible()
  await expect(page.getByText('Opening Research does not start collection')).toBeVisible()

  const serviceButton = page.getByRole('button', { name: 'System status and service controls', exact: true })
  await expect(serviceButton).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open background service controls' })).toBeVisible()
  await serviceButton.click()
  const serviceDialog = page.getByRole('dialog', { name: 'Background service controls' })
  await expect(serviceDialog).toContainText('Dashboard and service checked')
  await expect(serviceDialog).toContainText('Monitoring disabled')
  await expect(serviceDialog).toContainText('Connect a local wallet only if you choose to configure service custody.')
  await serviceDialog.getByRole('button', { name: 'Done' }).click()

  await page.getByRole('button', { name: 'Create monitoring session', exact: true }).click()
  const modePicker = page.getByRole('dialog', { name: 'Create session' })
  await modePicker.getByRole('button', { name: /LEARN FIRST Monitor/ }).click()
  const confirmation = page.getByRole('dialog', { name: 'Confirm monitoring session' })
  await expect(confirmation).toContainText('No signing path')
  const creationResponse = page.waitForResponse((response) => response.url().endsWith('/api/v1/session-workspaces') && response.request().method() === 'POST')
  await confirmation.getByRole('button', { name: 'Confirm monitoring session', exact: true }).click()
  const created = await (await creationResponse).json() as { session: { id: string } }
  await expect(page.getByRole('article', { name: 'Monitoring session 1 session' }).getByRole('img', { name: 'Not running' })).toBeVisible()

  const afterCreate = await counters()
  expect(afterCreate).toEqual(before)
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Understand one strategy before risking funds.' })).toBeVisible()
  expect(await counters()).toEqual(before)
  expect(await page.evaluate(async () => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toMatch(/secret|private.key|seed/i)

  const removed = await page.evaluate(async (workspaceId) => {
    const response = await fetch(`/api/v1/session-workspaces/${encodeURIComponent(workspaceId)}/remove`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: workspaceId }),
    })
    return response.ok
  }, created.session.id)
  expect(removed).toBe(true)
})

test('keeps walletless research keyboard-usable without mobile overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  const explore = page.getByRole('button', { name: 'Explore research' })
  await explore.focus()
  await expect(explore).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'Understand one strategy before risking funds.' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Create monitoring session', exact: true })).toBeVisible()
  await expect.poll(() => page.evaluate(async () => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  const activity = page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Dashboard', exact: true })
  await activity.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('region', { name: 'Trading dashboard' })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Overview', exact: true })).toHaveAttribute('aria-selected', 'true')
})

test('keeps walletless entry unavailable when the capability contract fails', async ({ page }) => {
  await page.route('**/api/v1/capabilities', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'Synthetic capability outage' }) }))
  await page.goto('/')
  await expect(page.getByRole('alert')).toContainText('Research entrance unavailable')
  await page.getByRole('button', { name: 'Explore research' }).click()
  await expect(page.getByRole('heading', { name: 'Understand one strategy before risking funds.' })).toHaveCount(0)
})

function fromBase58(value: string) {
  const alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
  let number = 0n
  for (const character of value) {
    const digit = alphabet.indexOf(character)
    if (digit < 0) throw new Error('Invalid base58 address')
    number = number * 58n + BigInt(digit)
  }
  const bytes: number[] = []
  while (number > 0n) { bytes.unshift(Number(number % 256n)); number /= 256n }
  for (const character of value) { if (character !== '1') break; bytes.unshift(0) }
  return Uint8Array.from(bytes)
}

function unsignedLegacyTransaction(publicKey: Uint8Array) {
  if (publicKey.length !== 32) throw new Error('Expected a 32-byte Solana key')
  return Uint8Array.from([1, ...new Uint8Array(64), 1, 0, 0, 1, ...publicKey, ...new Uint8Array(32), 0])
}

async function createAndSignIn(page: Page) {
  await page.goto('/')
  await expect(page.getByText('OPEN SOURCE · TRADER COMMUNITY', { exact: true })).toBeVisible()
  await expect(page.getByRole('img', { name: 'PumpWizard' })).toBeVisible()
  await expect(page.locator('.onboarding-hero-wizard')).toBeVisible()
  await expect(page.getByText('Solana Mainnet', { exact: true })).toBeVisible()
  await expect(page.getByText('Devnet', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: 'Create a new wallet' }).click()
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByLabel('Confirm password').fill(password)
  await page.getByRole('button', { name: 'Create encrypted wallet' }).click()
  await expect(page.getByRole('heading', { name: 'Save your backup' })).toBeVisible()
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download encrypted backup' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toMatch(/^pumpwizard-wallet-.*\.json$/)
  await page.getByLabel(/I saved the encrypted backup/).check()
  await page.getByRole('button', { name: 'Choose a wallet' }).click()
  await expect(page.getByRole('heading', { name: 'Choose how to begin' })).toBeVisible()
  await page.getByRole('button', { name: /Unlock PumpWizard Wallet/ }).click()
  await expect(page.getByRole('button', { name: 'Back to wallet selection' })).toBeVisible()
  await page.getByLabel('Wallet password').fill(password)
  await page.getByRole('button', { name: 'Unlock & sign in' }).click()
  const welcome = page.locator('.welcome-dialog')
  await expect(welcome).toHaveAccessibleName(/Welcome to PumpWizard/)
  return welcome
}

async function createAndEnter(page: Page) {
  const welcome = await createAndSignIn(page)
  await welcome.getByRole('button', { name: 'Skip', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Wallet connection' })).toContainText('PumpWizard Wallet')
  const primary = page.getByRole('navigation', { name: 'Primary navigation' })
  await expect(primary.getByRole('button', { name: /Dashboard/ })).toHaveAttribute('aria-current', 'page')
  await expect(page.getByRole('tab', { name: /Wallet/ })).toBeVisible()
  await expect(primary.getByRole('button', { name: 'Dashboard', exact: true })).toBeVisible()
  await openActivity(page)
}

test('unifies the wallet and command board with portfolio tools in Dashboard', async ({ page }, testInfo) => {
  const welcome = await createAndSignIn(page)
  await welcome.getByRole('button', { name: 'Skip', exact: true }).click()
  const dashboard = page.getByRole('main', { name: 'Wallet dashboard' })
  await expect(dashboard).toBeVisible()
  await expect(page.getByRole('region', { name: 'Wallet balance and valuation summary' })).toContainText('1.25 SOL')
  await expect(page.getByRole('heading', { name: 'PumpWizard' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Add API key', exact: true })).toBeVisible()
  await expect(dashboard.getByRole('button', { name: 'Add session' })).toBeVisible()
  await expect(page.getByRole('group', { name: 'Wallet actions' })).toBeVisible()
  const primary = page.getByRole('navigation', { name: 'Primary navigation' })
  await expect(primary.getByRole('button')).toHaveText(['Dashboard'])
  await expect(page.getByRole('region', { name: 'Trading dashboard' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Wallet balance and valuation summary' })).toHaveCount(1)
  const connection = page.getByRole('region', { name: 'Wallet connection' })
  await expect(connection).toContainText('PumpWizard Wallet connected')
  await expect(page.locator('.workspace-toolbar, .wallet-summary-metrics')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Background status and controls' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Connect optional AI' })).toHaveCount(0)
  const guides = page.getByRole('button', { name: 'Guides', exact: true })
  await expect(guides).toBeVisible()
  const guidesBox = await guides.boundingBox()
  const statusBox = await page.locator('.command-board-overview').boundingBox()
  const connectionBox = await connection.boundingBox()
  expect(guidesBox!.height).toBeGreaterThanOrEqual(66)
  const compact = page.getByRole('region', { name: 'Wallet balance and valuation summary' })
  const compactBox = await compact.boundingBox()
  const signOutBox = await page.getByRole('button', { name: 'Sign out of wallet' }).boundingBox()
  expect(Math.abs(guidesBox!.height - compactBox!.height)).toBeLessThan(1)
  expect(Math.abs(guidesBox!.height - signOutBox!.height)).toBeLessThan(1)
  expect(guidesBox!.x).toBeGreaterThanOrEqual(compactBox!.x + compactBox!.width)
  expect(signOutBox!.x).toBeGreaterThanOrEqual(guidesBox!.x + guidesBox!.width)
  expect(signOutBox!.y).toBe(guidesBox!.y)
  await expect(compact.getByRole('button', { name: 'Deposit', exact: true })).toBeVisible()
  await expect(compact.getByRole('button', { name: 'Withdraw', exact: true })).toBeVisible()
  await expect(compact).not.toContainText('RUNNER TRADING P/L')
  await expect(compact).not.toContainText('24H WORTH MOVE')
  expect(Math.abs(connectionBox!.height - statusBox!.height)).toBeLessThan(1)
  expect(connectionBox!.x).toBeGreaterThanOrEqual(statusBox!.x + statusBox!.width)
  expect(connectionBox!.y).toBeLessThan(statusBox!.y + statusBox!.height)
  const commandBox = await page.getByRole('region', { name: 'System and optional AI' }).boundingBox()
  const walletBox = await compact.boundingBox()
  const tradingBox = await dashboard.boundingBox()
  expect(walletBox!.x).toBeGreaterThanOrEqual(commandBox!.x)
  expect(walletBox!.x + walletBox!.width).toBeLessThanOrEqual(commandBox!.x + commandBox!.width)
  expect(walletBox!.y).toBeGreaterThan(commandBox!.y)
  await expect(dashboard.getByRole('tab')).toHaveText(['OverviewStrategy performance', 'Activity0Orders and decisions', 'Wallet0All assets and wallet history'])
  expect(tradingBox!.y).toBeGreaterThanOrEqual(commandBox!.y + commandBox!.height)
  await openPortfolio(page)
  await expect(dashboard).toBeVisible()
  await expect(dashboard.getByRole('region', { name: 'Wallet holdings and tools' })).toBeVisible()
  await expect(primary.getByRole('button', { name: 'Dashboard', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(page.getByRole('button', { name: 'Add API key', exact: true })).toBeVisible()
  await page.locator('.command-board-details > summary').click()
  await expect(page.getByRole('log', { name: 'Status history' })).toBeHidden()
  await expect(page.getByRole('region', { name: 'Wallet balance and valuation summary' })).toBeVisible()
  await page.locator('.command-board-details > summary').click()
  await expect(page.getByRole('log', { name: 'Status history' })).toBeVisible()
  await primary.getByRole('button', { name: 'Dashboard', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Understand one strategy before risking funds.' })).toHaveCount(0)
  for (const width of [1600, 2048]) {
    await page.setViewportSize({ width, height: 1000 })
    const finance = await compact.boundingBox()
    const status = await page.locator('.command-board-overview').boundingBox()
    const guide = await guides.boundingBox()
    const signOut = await page.getByRole('button', { name: 'Sign out of wallet' }).boundingBox()
    expect(finance!.y).toBe(status!.y)
    expect(guide!.x).toBeGreaterThanOrEqual(finance!.x + finance!.width)
    expect(signOut!.x).toBeGreaterThanOrEqual(guide!.x + guide!.width)
    expect(Math.abs(finance!.height - guide!.height)).toBeLessThan(1)
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  }
  await page.setViewportSize({ width: 1600, height: 1000 })
  await page.screenshot({ path: testInfo.outputPath('wallet-dashboard-desktop.png'), fullPage: true })
  await page.reload()
  await expect(dashboard).toBeVisible()
  await expect(page.getByRole('region', { name: 'Wallet connection' })).toContainText('signing locked')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(async () => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('wallet-dashboard-mobile.png'), fullPage: true })
  await openPortfolio(page)
  await expect(dashboard.getByRole('region', { name: 'Wallet holdings and tools' })).toBeVisible()
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  expect((await syntheticControl(page)).sent).toBe(0)
})

async function openSessions(page: Page) {
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Dashboard', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Add session' })).toBeVisible()
}

async function openActivity(page: Page) {
  const primary = page.getByRole('navigation', { name: 'Primary navigation' })
  await primary.getByRole('button', { name: 'Dashboard', exact: true }).click()
  const dashboard = page.getByRole('region', { name: 'Trading dashboard' })
  const allWallet = dashboard.getByRole('button', { name: 'All wallet activity', exact: true })
  if (await allWallet.count()) await allWallet.click()
  await dashboard.getByRole('tab', { name: /Activity/ }).click()
  await expect(dashboard.getByRole('heading', { name: 'Trade History' })).toBeVisible()
  await expect(dashboard.getByRole('tab', { name: /Positions/ })).toHaveCount(0)
  await expect(dashboard.getByRole('tab', { name: /Observations/ })).toHaveCount(0)
  await expect(dashboard.getByRole('button', { name: /New order/i })).toBeVisible()
  await expect(dashboard.getByRole('tab', { name: /Activity/ })).toBeVisible()
}

async function openCommandBoard(page: Page) {
  const dialog = page.getByRole('region', { name: 'System and optional AI' })
  await expect(dialog).toBeVisible()
  return dialog
}

async function openServiceControls(page: Page, expand = true) {
  await page.getByRole('button', { name: 'System status and service controls', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Background service controls' })
  await expect(dialog).toBeVisible()
  if (expand) {
    for (const label of ['Trading setup and limits', 'Technical details']) {
      const details = dialog.locator('details.service-disclosure').filter({ has: page.locator('summary', { hasText: label }) })
      if (await details.getAttribute('open') === null) await details.locator(':scope > summary').click()
    }
  }
  return dialog
}

async function openWalletActions(page: Page) {
  return page.getByRole('group', { name: 'Wallet tools and guides' })
}

test('reports one background service and keeps sign-out separate from service control', async ({ browser, context, page }, testInfo) => {
  const root = await page.request.get('/')
  const actualStatus = await page.request.get('/api/v1/service/status', { headers: { Origin: new URL(root.url()).origin } })
  const runtimeIdentity = (await actualStatus.json()).build_identity
  let instanceId = 'service-instance-a'
  let instanceRevision = 1
  let serviceAvailable = true
  let collectorEnabled = false
  let enrollment: null | { schema: string; vault_id: string; custody: string; address: string; network: string; created_at: string } = null
  let allowances: { id: string; session_id: string; status: string; expires_at: number; entries_paused: boolean }[] = []
  let orderControls = {
    schema: 'pumpwizard.service-order-controls/v1',
    cancelled_reviews: [] as { order_id: string; session_id: string; side: string; status: string; created_at: number; cancelled_at: number; detail: string }[],
    unresolved_submissions: [] as { order_id: string; session_id: string; side: string; status: string; created_at: number; submission_state: string; signature: string }[],
    authority: { orders: false, signer: false, submission: false },
  }
  let trading = {
    schema: 'pumpwizard.service-trading-state/v1',
    custody_mode: 'main-wallet-service',
    signer_state: 'unlocked',
    authorization_status: 'active',
    authorization_id: 'synthetic-authorization',
    authorization_expires_at: Date.now() / 1000 + 900,
    session_id: 'session123456789',
    entries_paused: false,
    recovery_required: false,
    production_activation: 'blocked',
    blockers: ['Production service signing has not passed funded acceptance'],
  }
  const commands: { path: string; body: unknown }[] = []
  const servicePayload = () => ({
    schema: 'pumpwizard.web/v1',
    version: currentVersion,
    build_identity: runtimeIdentity,
    service: {
      schema: 'pumpwizard.background-service-status/v1',
      instance_id: instanceId,
      instance_revision: instanceRevision,
      data_directory_id: 'synthetic-data-directory',
      version: currentVersion,
      state: 'running',
      started_at: Date.now() / 1000 - 120,
      heartbeat_at: Date.now() / 1000,
      current_activity: 'idle',
      last_success_at: Date.now() / 1000 - 5,
      health: 'healthy',
      stop_reason: '',
      production_trading: 'blocked',
    },
    jobs: [{
      schema: 'pumpwizard.service-job/v1', id: 'strategy-monitor', kind: 'read-only-monitor', enabled: collectorEnabled,
      interval_seconds: 15, revision: collectorEnabled ? 2 : 1, next_at: Date.now() / 1000 + 15,
      consecutive_failures: 0, current_activity: 'idle', last_started_at: Date.now() / 1000 - 6,
      last_success_at: Date.now() / 1000 - 5, last_error_at: null, last_error: '',
    }],
    coverage: [{
      schema: 'pumpwizard.collector-coverage/v1', id: 'coverage-1', scan_key: 'pumpfun:0:500000', source: 'pump.fun public market',
      status: 'completed', quote_count: 12, gap_count: 0, requested_at: Date.now() / 1000 - 7,
      started_at: Date.now() / 1000 - 6, received_at: Date.now() / 1000 - 5,
      earliest_source_at: Date.now() / 1000 - 60, latest_source_at: Date.now() / 1000 - 5,
      consecutive_failures: 0, next_retry_at: null,
    }],
    trading,
  })
  const serviceRoute = async (route: Route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (!serviceAvailable) {
      await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'Synthetic service unavailable' }) })
      return
    }
    if (request.method() === 'GET' && path === '/api/v1/service/status') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(servicePayload()) })
      return
    }
    if (request.method() === 'GET' && path === '/api/v1/service/signing') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ schema: 'pumpwizard.web/v1', version: currentVersion, trading, enrollment, allowances, order_controls: orderControls }) })
      return
    }
    const body = request.postDataJSON()
    commands.push({ path, body })
    if (path === '/api/v1/service/signing/enroll') {
      const requestBody = body as { sender: string; network: string; envelope: { vault_id: string; created_at: string } }
      enrollment = { schema: 'pumpwizard.service-wallet-vault/v1', vault_id: requestBody.envelope.vault_id, custody: 'main-wallet-service', address: requestBody.sender, network: requestBody.network, created_at: requestBody.envelope.created_at }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ schema: 'pumpwizard.web/v1', version: currentVersion, enrollment }) })
      return
    }
    if (path === '/api/v1/service/signing/review') {
      const requestBody = body as { session_id: string; plan: { duration_seconds: number; budget_lamports: number; max_order_debit_lamports: number; max_daily_debit_lamports: number; max_orders: number; max_trades_per_day: number; max_open_positions: number; slippage_bps: number } }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        schema: 'pumpwizard.web/v1',
        version: currentVersion,
        review: {
          id: 'review-service-allowance',
          session_id: requestBody.session_id,
          strategy_digest: 'synthetic-service-strategy-digest',
          policy_revision: 1,
          cash_hold: 'synthetic-service-cash-hold',
          ...requestBody.plan,
          exit_headroom_lamports: 20_100_000,
          expires_at: Date.now() / 1000 + 60,
          ready: true,
          preflight_blockers: [],
          activation_blockers: ['Production service signing has not passed funded acceptance'],
        },
      }) })
      return
    }
    if (path === '/api/v1/service/signing/authorize') {
      const requestBody = body as { session_id: string }
      allowances = [{ id: 'allowance-service-inactive', session_id: requestBody.session_id, status: 'authorized', expires_at: Date.now() / 1000 + 900, entries_paused: false }]
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ schema: 'pumpwizard.web/v1', version: currentVersion, allowance: allowances[0] }) })
      return
    }
    if (path === '/api/v1/service/jobs/strategy-monitor') {
      collectorEnabled = Boolean((body as { enabled?: boolean }).enabled)
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ schema: 'pumpwizard.web/v1', version: currentVersion, job: servicePayload().jobs[0] }) })
      return
    }
    if (path === '/api/v1/service/revoke-trading') {
      trading = { ...trading, signer_state: 'locked', authorization_status: 'revoked', entries_paused: true }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ schema: 'pumpwizard.web/v1', version: currentVersion, trading }) })
      return
    }
    if (path === '/api/v1/service/pause-entries') {
      trading = { ...trading, authorization_status: 'paused', entries_paused: true }
      orderControls = {
        ...orderControls,
        cancelled_reviews: [{ order_id: 'unsigned-review-one', session_id: 'session123456789', side: 'buy', status: 'cancelled', created_at: Date.now() / 1000 - 10, cancelled_at: Date.now() / 1000, detail: 'Entries paused from authenticated dashboard' }],
        unresolved_submissions: [{ order_id: 'submitted-order-one', session_id: 'session123456789', side: 'sell', status: 'submitted', created_at: Date.now() / 1000 - 20, submission_state: 'unknown', signature: 'synthetic-signature-awaiting-reconciliation' }],
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ schema: 'pumpwizard.web/v1', version: currentVersion, trading }) })
      return
    }
    if (path === '/api/v1/service/resume-entries') {
      trading = { ...trading, authorization_status: 'active', entries_paused: false }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ schema: 'pumpwizard.web/v1', version: currentVersion, trading }) })
      return
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ schema: 'pumpwizard.web/v1', version: currentVersion, trading }) })
  }
  await context.route('**/api/v1/sessions?*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      schema: 'pumpwizard.web/v1',
      version: currentVersion,
      rows: [{
        instance: {
          id: 'svc1234567890',
          workspace_id: null,
          strategy_digest: 'synthetic-service-strategy-digest',
          label: 'Service custody acceptance',
          status: 'monitoring',
          entry_mode: 'disabled',
          exit_mode: 'manual',
        },
        brief: {
          schema: 'pumpwizard.session-brief/v4',
          strategy_digest_at_creation: 'synthetic-service-strategy-digest',
          goal: 'validate-strategy',
          intent: 'Verify an inactive bounded service allowance.',
          mode: 'trade-autopilot',
          session_mode: 'trade',
          execution_style: 'autopilot',
          automation_scope: 'entries-and-exits',
          custody_mode: 'main-wallet-service',
          profit_management: 'strategy-managed',
          permissions: {
            prepare_orders: true,
            automatic_entries: true,
            automatic_protective_exits: true,
            automatic_profit_exits: true,
            winner_handoff: false,
            user_signature_each_order: false,
            automation_wallet_required: false,
            generated_wallet_required: true,
            activation_required: true,
            custody: 'main-wallet-service',
            service_vault_required: true,
            continues_without_browser: true,
          },
          paper_budget_usd: 0,
          paper_order_usd: 0,
          max_session_loss_usd: 0,
          max_positions: 1,
          review_after_seconds: 3600,
          created_at: Date.now() / 1000,
          review_at: Date.now() / 1000 + 3600,
          answers: [],
          preferences: null,
          limits: 'Synthetic service acceptance fixture.',
        },
        tracking: { fault: '', enabled: 0, next_at: 0 },
        state: { label: 'Ready', tone: 'muted', reason: 'Awaiting explicit activation.', action: 'Review the allowance.' },
        health: { last_run: 'never' },
        positions: [],
        exit_plan: { approval: 'automatic', stop_loss_percent: 10, take_profit_percent: 20, trailing_drawdown_percent: null, max_hold_seconds: 3600, targets: [] },
        monitor_plan: { entry_style: 'immediate', entry_cap_min: null, entry_cap_max: null, observation_seconds: null, minimum_samples: null, minimum_price_changes: null, order_usd: null, max_positions: 1, min_curve_liquidity_usd: null, min_amm_liquidity_usd: null, scan_interval_seconds: 15 },
        risk_policy: { allocation_lamports: 300_000_000, buys_enabled: true, sells_enabled: true, min_trade_lamports: 10_000_000, max_trade_lamports: 50_000_000, max_daily_buy_lamports: 100_000_000, max_trades_per_day: 2, max_daily_loss_lamports: 30_000_000, max_trade_loss_bps: 1_000, max_daily_loss_bps: 1_000, max_open_positions: 1 },
        entry_considerations: [],
      }],
    }) })
  })
  await context.route('**/api/v1/service/**', serviceRoute)

  await createAndEnter(page)
  const commandBoard = await openCommandBoard(page)
  const overview = commandBoard.locator('.command-board-overview')
  await expect(overview).toContainText(`Runner v${currentVersion} · Running`)
  await expect(overview).toContainText('Service healthy · RPC verified')
  await expect(overview).toContainText('Trading active')
  await expect(commandBoard.getByRole('button', { name: 'Pause or stop service', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Service', exact: true })).toHaveCount(0)
  await expect(commandBoard.getByRole('button', { name: 'Open background service controls' })).toContainText(`Runner v${currentVersion} · healthy`)
  await expect(commandBoard.getByRole('button', { name: 'Open background service controls' })).toContainText('pump.fun public market: 12 quotes, 0 gaps')
  await commandBoard.getByRole('button', { name: 'Open background service controls' }).click()
  const dialog = page.getByRole('dialog', { name: 'Background service controls' })
  await expect(dialog.getByRole('button', { name: 'Stop & lock trading', exact: true })).toBeVisible()
  await expect(dialog.getByRole('region', { name: 'Background trading', exact: true })).toContainText('Running')
  await expect(dialog).toContainText(`Runner v${currentVersion}`)
  await expect(dialog).toContainText('SERVICE INSTANCE')
  await expect(dialog).toContainText('Closing or signing out of Dashboard does not revoke')
  const setupDetails = dialog.locator('details.service-disclosure').filter({ has: page.locator('summary', { hasText: 'Trading setup and limits' }) })
  if (await setupDetails.getAttribute('open') === null) await setupDetails.locator(':scope > summary').click()
  await dialog.getByRole('button', { name: 'Set up trading wallet' }).click()
  const enrollmentForm = dialog.getByRole('group', { name: 'Enroll local wallet for auto trading' })
  await enrollmentForm.getByLabel('Current wallet password').fill('wrong wallet password')
  await enrollmentForm.getByLabel('New service wallet password').fill('service custody secret')
  await enrollmentForm.getByLabel('Confirm service wallet password').fill('service custody secret')
  await enrollmentForm.getByRole('checkbox').check()
  await enrollmentForm.getByRole('button', { name: 'Encrypt & enroll wallet' }).click()
  await expect(dialog.locator('.service-error')).toContainText('wallet password is incorrect')
  expect(commands.filter((item) => item.path === '/api/v1/service/signing/enroll')).toHaveLength(0)
  await enrollmentForm.getByLabel('Current wallet password').fill(password)
  await enrollmentForm.getByLabel('New service wallet password').fill('service custody secret')
  await enrollmentForm.getByLabel('Confirm service wallet password').fill('service custody secret')
  await enrollmentForm.getByRole('button', { name: 'Encrypt & enroll wallet' }).click()
  await expect(dialog).toContainText('Encrypted enrollment stored')
  const enrollCommand = commands.find((item) => item.path === '/api/v1/service/signing/enroll')?.body as { envelope: { cipher: { ciphertext: string } }; service_unlock_secret: string }
  expect(enrollCommand.service_unlock_secret).toBe('service custody secret')
  expect(enrollCommand.envelope.cipher.ciphertext).not.toContain(password)
  expect(await page.evaluate(async () => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('service custody secret')
  await expect(dialog.getByRole('button', { name: 'Start auto trading' })).toBeDisabled()
  const serviceSlippage = dialog.getByLabel('Service allowance slippage')
  await serviceSlippage.fill('501')
  await expect(serviceSlippage).toHaveAttribute('aria-invalid', 'true')
  await expect(dialog.getByRole('button', { name: 'Review exact allowance' })).toBeDisabled()
  await serviceSlippage.fill('100')
  await expect(serviceSlippage).toHaveAttribute('aria-invalid', 'false')
  await dialog.getByRole('button', { name: 'Review exact allowance' }).click()
  await expect(dialog).toContainText('Review ready')
  const reviewCommand = commands.find((item) => item.path === '/api/v1/service/signing/review')?.body
  expect(reviewCommand).toEqual(expect.objectContaining({
    network: 'mainnet-beta',
    session_id: 'svc1234567890',
    plan: {
      duration_seconds: 900,
      budget_lamports: 300_000_000,
      max_order_debit_lamports: 70_100_000,
      max_daily_debit_lamports: 140_200_000,
      max_orders: 2,
      max_trades_per_day: 2,
      max_open_positions: 1,
      slippage_bps: 100,
    },
  }))
  const allowanceConfirmation = dialog.getByLabel('Service allowance confirmation')
  await allowanceConfirmation.fill('wrong confirmation')
  await expect(dialog.getByRole('button', { name: 'Authorize inactive allowance' })).toBeDisabled()
  await allowanceConfirmation.fill('ALLOW SERVICE SVC12345')
  await dialog.getByRole('button', { name: 'Authorize inactive allowance' }).click()
  expect(commands.find((item) => item.path === '/api/v1/service/signing/authorize')?.body).toEqual(expect.objectContaining({
    network: 'mainnet-beta',
    session_id: 'svc1234567890',
    review_id: 'review-service-allowance',
    confirmation: 'ALLOW SERVICE SVC12345',
  }))
  await expect(dialog.locator('[aria-label="Service trading allowances"]')).toContainText('Authorized')
  await dialog.evaluate((element) => { element.scrollTop = 0 })
  await page.screenshot({ path: testInfo.outputPath('background-service-controls.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(async () => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  expect((await dialog.boundingBox())?.width).toBeLessThanOrEqual(390)
  await page.setViewportSize({ width: 1280, height: 720 })

  await dialog.getByRole('button', { name: 'Start monitoring' }).click()
  await expect.poll(() => commands.filter((item) => item.path === '/api/v1/service/jobs/strategy-monitor').length).toBe(1)
  expect(commands.find((item) => item.path === '/api/v1/service/jobs/strategy-monitor')?.body).toEqual({ enabled: true, interval_seconds: 15 })
  await expect(dialog.getByRole('button', { name: 'Stop monitoring' })).toBeVisible()

  await dialog.getByRole('button', { name: 'Pause entries' }).click()
  expect(commands.find((item) => item.path === '/api/v1/service/pause-entries')?.body).toEqual({})
  const orderOutcomes = dialog.getByRole('region', { name: 'Service order control outcomes' })
  await expect(orderOutcomes).toContainText('Unsigned BUY review revoked')
  await expect(orderOutcomes).toContainText('Cancelled before submission')
  await expect(orderOutcomes).toContainText('Submitted SELL requires reconciliation')
  await expect(orderOutcomes).toContainText('Unknown')
  await expect(dialog.getByRole('region', { name: 'Background trading', exact: true })).toContainText('Entries paused')
  const resumeConfirmation = dialog.getByLabel('Resume entries confirmation')
  await resumeConfirmation.fill('wrong confirmation')
  await expect(dialog.getByRole('button', { name: 'Resume entries' })).toBeDisabled()
  await resumeConfirmation.fill('RESUME SESSION1')
  await dialog.getByRole('button', { name: 'Resume entries' }).click()
  expect(commands.find((item) => item.path === '/api/v1/service/resume-entries')?.body).toEqual({ allowance_id: 'synthetic-authorization', confirmation: 'RESUME SESSION1' })

  await dialog.getByRole('button', { name: 'Stop & lock trading' }).click()
  const stopConfirmation = dialog.getByRole('alertdialog', { name: 'Confirm stop and lock trading' })
  await expect(stopConfirmation).toContainText('already submitted to Solana cannot be cancelled')
  expect(commands.filter((item) => item.path === '/api/v1/service/revoke-trading')).toHaveLength(0)
  await stopConfirmation.getByRole('button', { name: 'Confirm stop & lock' }).click()
  await expect.poll(() => commands.filter((item) => item.path === '/api/v1/service/revoke-trading').length).toBe(1)
  expect(commands.find((item) => item.path === '/api/v1/service/revoke-trading')?.body).toEqual({})

  await page.evaluate(async () => sessionStorage.setItem('pumpwizard.background-service-approval/v1', 'stale-client-approval'))
  instanceId = 'service-instance-b'
  instanceRevision = 2
  await dialog.getByRole('button', { name: 'Refresh status' }).click()
  await expect(dialog.getByRole('alert')).toContainText('Background service restarted')
  await expect(overview).toContainText('Restart needs review')
  await expect(dialog.getByRole('alert')).toContainText('freshly unlocked and authorized')
  await expect.poll(() => page.evaluate(async () => sessionStorage.getItem('pumpwizard.background-service-approval/v1'))).toBeNull()
  await dialog.evaluate((element) => { element.scrollTop = 0 })
  await page.screenshot({ path: testInfo.outputPath('background-service-restart.png'), fullPage: true })

  const inspectionTab = await context.newPage()
  await inspectionTab.goto(new URL(page.url()).origin)
  await expect(inspectionTab.getByRole('main', { name: 'Wallet dashboard' })).toBeVisible()
  await expect(inspectionTab.getByRole('button', { name: 'Unlock signing', exact: true })).toBeVisible()
  const inspectionBoard = await openCommandBoard(inspectionTab)
  await expect(inspectionBoard.getByRole('button', { name: 'Open background service controls' })).toContainText('collector enabled')
  expect(commands.filter((item) => item.path === '/api/v1/service/jobs/strategy-monitor')).toHaveLength(1)
  await inspectionTab.close()

  const baseUrl = new URL(page.url()).origin
  const storedDashboard = await context.storageState()
  await page.close()
  expect(collectorEnabled).toBe(true)
  expect(commands.filter((item) => item.path === '/api/v1/service/jobs/strategy-monitor')).toHaveLength(1)

  const reopenedContext = await browser.newContext({ storageState: storedDashboard })
  await reopenedContext.route('**/api/v1/service/**', serviceRoute)
  const reopenedPage = await reopenedContext.newPage()
  await reopenedPage.goto(baseUrl)
  await expect(reopenedPage.getByRole('main', { name: 'Wallet dashboard' })).toBeVisible()
  await expect(reopenedPage.getByRole('button', { name: 'Unlock signing', exact: true })).toBeVisible()
  const reopenedBoard = await openCommandBoard(reopenedPage)
  await expect(reopenedBoard.getByRole('button', { name: 'Open background service controls' })).toContainText('collector enabled')
  expect(commands.filter((item) => item.path === '/api/v1/service/jobs/strategy-monitor')).toHaveLength(1)
  await reopenedBoard.getByRole('button', { name: 'Open background service controls' }).click()
  const reopenedDialog = reopenedPage.getByRole('dialog', { name: 'Background service controls' })
  serviceAvailable = false
  await reopenedDialog.getByRole('button', { name: 'Refresh status' }).click()
  await expect(reopenedDialog.getByRole('status')).toContainText('Service state is unknown')
  await expect(reopenedPage.locator('.command-board-overview')).toContainText('Service offline')
  await expect(reopenedPage.locator('.command-board-overview')).toHaveAttribute('data-state', 'attention')
  await expect(reopenedDialog).toContainText('Disconnected · state unknown')
  await reopenedContext.close()
})

test('runs a synthetic service entry and exit after every browser closes, then locks across restart', async ({ browser, context, page }, testInfo) => {
  test.setTimeout(180_000)
  await createAndEnter(page)
  const browserBackup = await page.evaluate(async () => {
    const collection = (await (await fetch('/api/v1/app-state')).json()) as { wallets: unknown[] }
    return JSON.stringify(collection.wallets[0])
  })
  const session = await createServiceAutopilot(page)
  await page.reload()
  await unlockAndEnter(page)

  const serviceSecret = 'isolated service acceptance secret'
  let dialog = await openServiceControls(page)
  await expect(dialog).toContainText(`Runner v${currentVersion}`)
  const setupDetails = dialog.locator('details.service-disclosure').filter({ has: page.locator('summary', { hasText: 'Trading setup and limits' }) })
  if (await setupDetails.getAttribute('open') === null) await setupDetails.locator(':scope > summary').click()
  await dialog.getByRole('button', { name: 'Set up trading wallet' }).click()
  const enrollment = dialog.getByRole('group', { name: 'Enroll local wallet for auto trading' })
  await enrollment.getByLabel('Current wallet password').fill(password)
  await enrollment.getByLabel('New service wallet password').fill(serviceSecret)
  await enrollment.getByLabel('Confirm service wallet password').fill(serviceSecret)
  await enrollment.getByRole('checkbox').check()
  await enrollment.getByRole('button', { name: 'Encrypt & enroll wallet' }).click()
  await expect(dialog).toContainText('Encrypted enrollment stored')

  await dialog.getByRole('button', { name: 'Review exact allowance' }).click()
  await expect(dialog).toContainText('Review ready')
  await dialog.getByLabel('Service allowance confirmation').fill(`ALLOW SERVICE ${session.id.slice(0, 8).toUpperCase()}`)
  await dialog.getByRole('button', { name: 'Authorize inactive allowance' }).click()
  await expect(dialog.locator('[aria-label="Service trading allowances"]')).toContainText('Authorized')
  await expect(dialog.getByRole('button', { name: 'Start auto trading' })).toBeEnabled()

  const scheduled = await serviceLifecycle(page, { action: 'start', session_id: session.id })
  expect(['starting', 'waiting-for-browser-close']).toContain(scheduled.phase)
  await dialog.getByRole('button', { name: 'Start auto trading' }).click()
  const startForm = dialog.getByRole('group', { name: 'Start auto trading' })
  await expect(startForm).toContainText('isolated synthetic acceptance runtime')
  await startForm.getByLabel('Service wallet password').fill(serviceSecret)
  await startForm.getByLabel('Service start confirmation').fill(`START ${session.id.slice(0, 8).toUpperCase()}`)
  await startForm.getByRole('checkbox').check()
  await startForm.getByRole('button', { name: 'Confirm auto-trading start' }).click()
  await expect(dialog).toContainText('Main Wallet Service · Unlocked')
  await expect(dialog).toContainText('Active')
  expect(await page.evaluate(async () => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain(serviceSecret)
  // Pause before installing the synthetic unsigned review so the live worker
  // cannot consume this test-only row between fixture setup and the UI action.
  await dialog.getByRole('button', { name: 'Pause entries' }).click()
  await expect(dialog.getByLabel('Resume entries confirmation')).toBeVisible()
  expect(await serviceControlOrder(page, session.id, 'issue-review')).toMatchObject({ status: 'review' })
  const repeatedPause = await page.evaluate(async () => {
    const response = await fetch('/api/v1/service/pause-entries', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    return { ok: response.ok, body: await response.text() }
  })
  expect(repeatedPause.ok, repeatedPause.body).toBe(true)
  await dialog.getByRole('button', { name: 'Refresh status' }).click()
  const pausedOutcomes = dialog.getByRole('region', { name: 'Service order control outcomes' })
  await expect(pausedOutcomes).toContainText('Unsigned BUY review revoked')
  await expect(pausedOutcomes).toContainText('Cancelled before submission')
  const pausedControl = await serviceControlOrder(page, session.id, 'status') as {
    orders: { id: string; status: string }[]
    events: { kind: string; payload: string }[]
  }
  expect(pausedControl.orders).toContainEqual({ id: 'browser-unsigned-service-review', status: 'cancelled' })
  expect(pausedControl.events.some((event) => event.kind === 'unsigned-reviews-cancelled'
    && JSON.parse(event.payload).order_ids.includes('browser-unsigned-service-review'))).toBe(true)
  const resumeConfirmation = dialog.getByLabel('Resume entries confirmation')
  await resumeConfirmation.fill(`RESUME ${session.id.slice(0, 8).toUpperCase()}`)
  await dialog.getByRole('button', { name: 'Resume entries' }).click()
  await expect(dialog).toContainText('Active')
  await expect.poll(async () => (await serviceLifecycle(page, { action: 'status' })).phase).toBe('waiting-for-browser-close')

  const baseUrl = new URL(page.url()).origin
  const storedDashboard = await context.storageState()
  const observer = await playwrightRequest.newContext({
    baseURL: baseUrl,
    storageState: storedDashboard,
    extraHTTPHeaders: { Origin: baseUrl },
  })
  await Promise.all(context.pages().map((openPage) => openPage.close()))
  const browserClosedAt = Date.now() / 1000
  const release = await observer.post('/api/v1/test-service-lifecycle', { data: { action: 'release' } })
  expect(release.ok()).toBe(true)
  let closedResult: { phase: string; detail: string; sent: number; released_at: number | null; completed_at: number | null } | null = null
  await expect.poll(async () => {
    const response = await observer.post('/api/v1/test-service-lifecycle', { data: { action: 'status' } })
    if (!response.ok()) return 'observer-error'
    closedResult = await response.json()
    return closedResult?.phase
  }, { timeout: 15_000 }).toBe('completed')
  expect(closedResult).toMatchObject({ sent: 2, detail: 'synthetic entry and full exit finalized' })
  expect(closedResult?.released_at || 0).toBeGreaterThanOrEqual(browserClosedAt)
  expect(closedResult?.completed_at || 0).toBeGreaterThanOrEqual(closedResult?.released_at || 0)
  await observer.dispose()

  const reopenedContext = await browser.newContext({ storageState: storedDashboard })
  const reopenedPage = await reopenedContext.newPage()
  await reopenedPage.goto(baseUrl)
  await unlockAndEnter(reopenedPage)
  expect(await serviceLifecycle(reopenedPage, { action: 'status' })).toMatchObject({ sent: 2, detail: 'synthetic entry and full exit finalized' })
  dialog = await openServiceControls(reopenedPage)
  await expect(dialog).toContainText('Main Wallet Service · Unlocked')
  await reopenedPage.screenshot({ path: testInfo.outputPath('service-browserless-entry-exit.png'), fullPage: true })
  expect(await serviceControlOrder(reopenedPage, session.id, 'issue-submitted')).toMatchObject({ status: 'submitted' })
  await dialog.getByRole('button', { name: 'Refresh status' }).click()
  const submittedOutcomes = dialog.getByRole('region', { name: 'Service order control outcomes' })
  await expect(submittedOutcomes).toContainText('Submitted SELL requires reconciliation')
  await expect(submittedOutcomes).toContainText('Unknown')
  await dialog.getByRole('button', { name: 'Done' }).click()

  const beforeRestart = await syntheticControl(reopenedPage)
  await reopenedPage.evaluate(async () => {
    await fetch('/api/v1/test-restart', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  })
  await expect.poll(async () => {
    try { return (await syntheticControl(reopenedPage)).process_id !== beforeRestart.process_id } catch { return false }
  }, { timeout: 15_000 }).toBe(true)
  await reopenedPage.reload()
  await unlockAndEnter(reopenedPage)
  dialog = await openServiceControls(reopenedPage)
  await expect(dialog).toContainText('Main Wallet Service · Locked')
  await expect(dialog.locator('[aria-label="Service trading allowances"]')).toContainText('Locked')
  await expect(dialog.getByRole('region', { name: 'Service order control outcomes' })).toContainText('Submitted SELL requires reconciliation')
  await expect.poll(async () => (await syntheticControl(reopenedPage)).sent).toBe(2)
  expect(await serviceControlOrder(reopenedPage, session.id, 'resolve-submitted')).toMatchObject({ status: 'failed' })
  await dialog.getByRole('button', { name: 'Refresh status' }).click()
  await expect(dialog).not.toContainText('Submitted SELL requires reconciliation')
  await dialog.getByRole('button', { name: 'Done' }).click()

  await (await openWalletActions(reopenedPage)).getByRole('button', { name: 'Sign out of wallet', exact: true }).click()
  await reopenedPage.evaluate(async () => {
    const state = await (await fetch('/api/v1/app-state')).json()
    for (const wallet of state.wallets) await fetch('/api/v1/app-state/wallets/remove', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ address: wallet.address }) })
  })
  await reopenedPage.reload()
  await reopenedPage.getByRole('button', { name: 'Connect wallet', exact: true }).click()
  const restoredWallet = reopenedPage.getByRole('region', { name: 'Wallet sign in' })
  await restoredWallet.locator('input[type="file"]').setInputFiles({
    name: 'pumpwizard-wallet-restored.json',
    mimeType: 'application/json',
    buffer: Buffer.from(browserBackup),
  })
  await expect(restoredWallet).toContainText('Encrypted backup restored')
  await restoredWallet.getByRole('button', { name: /Unlock PumpWizard Wallet/ }).click()
  await restoredWallet.getByLabel('Wallet password').fill(password)
  await restoredWallet.getByRole('button', { name: 'Unlock & sign in' }).click()
  const restoredWelcome = reopenedPage.getByRole('dialog', { name: /Welcome to PumpWizard|Runner updated to/ })
  await restoredWelcome.waitFor({ state: 'visible', timeout: 2_000 }).catch(() => undefined)
  if (await restoredWelcome.isVisible()) await restoredWelcome.getByRole('button', { name: 'Skip', exact: true }).click()
  dialog = await openServiceControls(reopenedPage)
  await expect(dialog).toContainText('Encrypted enrollment stored')
  await expect(dialog).toContainText('Main Wallet Service · Locked')

  await dialog.getByRole('button', { name: 'Review exact allowance' }).click()
  await expect(dialog).toContainText('Review ready')
  await dialog.getByLabel('Service allowance confirmation').fill(`ALLOW SERVICE ${session.id.slice(0, 8).toUpperCase()}`)
  await dialog.getByRole('button', { name: 'Authorize inactive allowance' }).click()
  await expect(dialog.locator('[aria-label="Service trading allowances"]')).toContainText('Authorized')
  await dialog.getByRole('button', { name: 'Start auto trading' }).click()
  const restartedStart = dialog.getByRole('group', { name: 'Start auto trading' })
  await restartedStart.getByLabel('Service wallet password').fill(serviceSecret)
  await restartedStart.getByLabel('Service start confirmation').fill(`START ${session.id.slice(0, 8).toUpperCase()}`)
  await restartedStart.getByRole('checkbox').check()
  await restartedStart.getByRole('button', { name: 'Confirm auto-trading start' }).click()
  await expect(dialog).toContainText('Main Wallet Service · Unlocked')
  await expect.poll(async () => (await syntheticControl(reopenedPage)).sent).toBe(2)
  await dialog.getByRole('button', { name: 'Stop & lock trading' }).click()
  await dialog.getByRole('alertdialog', { name: 'Confirm stop and lock trading' }).getByRole('button', { name: 'Confirm stop & lock' }).click()
  await expect(dialog).toContainText('Main Wallet Service · Locked')
  await cleanupServiceAutopilot(reopenedPage, session.id)
  await Promise.all(reopenedContext.pages().map((openPage) => openPage.close()))
})

test('uses large mode cards and confirms monitoring without trade preferences', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1007, height: 597 })
  await createAndEnter(page)
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Dashboard', exact: true }).click()
  await page.getByRole('button', { name: 'Add session' }).click()
  const modePicker = page.getByRole('dialog', { name: 'Create session' })
  const cards = modePicker.locator('.session-mode-step > button')
  await expect(cards).toHaveCount(2)
  const monitoringBox = await cards.first().boundingBox()
  expect(monitoringBox?.width).toBeGreaterThanOrEqual(400)
  expect(monitoringBox?.height).toBeGreaterThanOrEqual(420)
  await page.screenshot({ path: testInfo.outputPath('enlarged-session-mode-cards.png'), fullPage: true })

  await modePicker.getByRole('button', { name: /LEARN FIRST Monitor/ }).click()
  const confirmation = page.getByRole('dialog', { name: 'Confirm monitoring session' })
  await expect(confirmation.getByRole('heading', { name: 'Confirm monitoring session' })).toBeVisible()
  await expect(confirmation).toContainText('No reservation')
  await expect(confirmation).toContainText('No signing path')
  await expect(confirmation).toContainText('Nothing starts from this confirmation')
  await expect(confirmation).not.toContainText('Investment size')
  await expect(confirmation).not.toContainText('Time horizon')
  const confirmationBox = await confirmation.boundingBox()
  expect(confirmationBox?.y).toBeGreaterThanOrEqual(0)
  expect((confirmationBox?.y || 0) + (confirmationBox?.height || 0)).toBeLessThanOrEqual(597)
  await page.screenshot({ path: testInfo.outputPath('monitoring-confirmation.png'), fullPage: true })

  const creationResponse = page.waitForResponse((response) => response.url().endsWith('/api/v1/session-workspaces') && response.request().method() === 'POST')
  await confirmation.getByRole('button', { name: 'Confirm monitoring session', exact: true }).click()
  const createdSession = await (await creationResponse).json() as { session: { id: string } }
  await expect(confirmation).toHaveCount(0)
  const activeSession = page.getByRole('article', { name: 'Monitoring session 1 session' })
  await expect(activeSession).toBeVisible()
  await expect(page.getByRole('log', { name: 'Status history' }).locator(`[data-check-id="workspace-created:${createdSession.session.id}"]`)).toContainText('Session created')
  const sessionToggle = activeSession.getByRole('button', { name: /Monitoring session 1/ }).first()
  await expect(sessionToggle).toHaveAttribute('aria-expanded', 'true')
  await expect(sessionToggle).toHaveAttribute('title', 'Monitoring session 1 · Observations · Not running')
  await sessionToggle.click()
  await expect(sessionToggle).toHaveAttribute('aria-expanded', 'false')
  await expect(activeSession.getByLabel('Monitoring session 1 strategies', { exact: true })).toBeHidden()
  await expect(activeSession.getByRole('button', { name: /Add strategy to/ })).toBeVisible()
  await sessionToggle.press('ArrowRight')
  await expect(sessionToggle).toHaveAttribute('aria-expanded', 'true')
  await expect(activeSession.getByLabel('Monitoring session 1 strategies', { exact: true })).toBeVisible()
  await expect(activeSession).not.toContainText('Nothing started')
  await expect(page.locator('.trade-rail')).toContainText('SESSIONS')
  await page.screenshot({ path: testInfo.outputPath('created-monitoring-session.png'), fullPage: true })

  await activeSession.getByRole('button', { name: 'Add strategy to Monitoring session 1' }).click()
  const strategyLibrary = page.getByRole('dialog', { name: 'Local strategy library' })
  await expect(strategyLibrary).toContainText('Monitoring session · no wallet funds')
  await expect(strategyLibrary).toContainText('No saved strategies in this wallet')
  await expect(strategyLibrary.getByRole('navigation', { name: 'Strategy sources' }).getByRole('button', { name: 'Public Lab Data', exact: true })).toBeVisible()
  await expect(strategyLibrary).not.toContainText('balanced risk')
  await expect(strategyLibrary).not.toContainText('swing · small sizing')
  await strategyLibrary.getByRole('button', { name: 'Close dialog' }).click()
  const removed = await page.evaluate(async (workspaceId) => {
    const response = await fetch(`/api/v1/session-workspaces/${encodeURIComponent(workspaceId)}/remove`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: workspaceId }),
    })
    return response.ok
  }, createdSession.session.id)
  expect(removed).toBe(true)
})

test('shows welcome after sign-in, reopens it, and limits update visits to what changed', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  let welcome = await createAndSignIn(page)
  await expect.poll(() => page.evaluate(async () => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await expect(welcome).toContainText('Local development · Draft Community note')
  await expect(welcome).toContainText(/Runner v0\.2\.0\.dev\d+/)
  await expect(welcome.getByRole('link', { name: /matching Community page/ })).toHaveAttribute('href', /pumpwizard\.fun\/community#pumpwizard-runner-/)

  const next = welcome.getByRole('button', { name: 'Next', exact: true })
  await next.focus()
  await page.keyboard.press('Enter')
  await expect(welcome.getByRole('heading', { name: 'Four checks before you interpret a result' })).toBeVisible()
  await expect(welcome).toContainText('Check coverage')
  await expect(welcome).toContainText('Opening Research never starts collection')
  await welcome.getByRole('button', { name: 'Next', exact: true }).click()
  await expect(welcome.getByRole('link', { name: /PumpWizard project/ })).toHaveAttribute('href', 'https://it.pumpwizard.fun')
  await expect(welcome.locator('.welcome-community').getByRole('link', { name: /@PumpWizardLab/ })).toHaveAttribute('href', 'https://x.com/PumpWizardLab')
  await expect(welcome.getByRole('link', { name: /Source/ })).toHaveAttribute('href', 'https://github.com/Goodworkid0/pumpwizard-runner')
  await expect(welcome.getByRole('link', { name: /Documentation/ })).toHaveAttribute('href', 'https://github.com/Goodworkid0/pumpwizard-runner/tree/main/docs')
  await expect(welcome.getByRole('link', { name: /Issues/ })).toHaveAttribute('href', 'https://github.com/Goodworkid0/pumpwizard-runner/issues')
  await welcome.getByRole('button', { name: 'Open Dashboard' }).click()

  const version = await page.evaluate(async () => (await (await fetch('/api/v1/release-note')).json()).version as string)
  await expect.poll(() => page.evaluate(async () => (await (await fetch('/api/v1/app-state')).json()).preferences['pumpwizard.welcome-acknowledged-version/v1'])).toBe(version)
  await expect(page.getByRole('main', { name: 'Wallet dashboard' })).toBeVisible()
  const guides = page.getByRole('button', { name: 'Guides', exact: true })
  await guides.focus()
  await page.keyboard.press('Enter')
  welcome = page.locator('.welcome-dialog')
  await expect(welcome.getByRole('button', { name: 'Usage tips' })).toBeVisible()
  await welcome.getByRole('button', { name: 'Skip', exact: true }).click()
  await page.reload()
  await expect(page.getByRole('main', { name: 'Wallet dashboard' })).toBeVisible()
  await expect(page.getByRole('dialog', { name: /PumpWizard Runner/ })).toHaveCount(0)

  await (await openWalletActions(page)).getByRole('button', { name: 'Sign out of wallet', exact: true }).click()
  await page.getByRole('button', { name: 'Connect wallet', exact: true }).click()
  await page.getByRole('button', { name: /Unlock PumpWizard Wallet/ }).click()
  await page.getByLabel('Wallet password').fill(password)
  await page.getByRole('button', { name: 'Unlock & sign in' }).click()
  welcome = page.getByRole('dialog', { name: /Welcome to PumpWizard/ })
  await expect(welcome).toBeVisible()
  const acknowledged = page.waitForResponse((response) => response.url().endsWith('/api/v1/app-state/preferences') && response.request().method() === 'POST' && response.request().postDataJSON()?.key === 'pumpwizard.welcome-acknowledged-version/v1' && response.request().postDataJSON()?.value === version)
  await welcome.getByRole('button', { name: 'Skip', exact: true }).click()
  expect((await acknowledged).ok()).toBe(true)

  await page.evaluate(async () => fetch('/api/v1/app-state/preferences', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: 'pumpwizard.welcome-acknowledged-version/v1', value: '0.2.0.dev1' }) }))
  await page.reload()
  welcome = page.getByRole('dialog', { name: /Runner updated to/ })
  await expect(welcome).toBeVisible()
  await expect(welcome.getByRole('button', { name: 'Usage tips' })).toHaveCount(0)
  await expect(welcome.getByRole('button', { name: 'Open Dashboard' })).toBeVisible()
})

test('continues to the Dashboard when welcome acknowledgement storage is unavailable', async ({ page }) => {
  const welcome = await createAndSignIn(page)
  await page.evaluate(async () => {
    const original = Storage.prototype.setItem
    Storage.prototype.setItem = function (key: string, value: string) {
      if (key === 'pumpwizard.welcome-acknowledged-version/v1') throw new DOMException('blocked', 'SecurityError')
      return original.call(this, key, value)
    }
  })
  await welcome.getByRole('button', { name: 'Skip', exact: true }).click()
  await expect(page.getByRole('main', { name: 'Wallet dashboard' })).toBeVisible()
})

async function openPortfolio(page: Page) {
  const primary = page.getByRole('navigation', { name: 'Primary navigation' })
  await primary.getByRole('button', { name: 'Dashboard', exact: true }).click()
  await page.getByRole('tab', { name: /Wallet/ }).click()
  await expect(page.getByRole('heading', { name: 'Wallet holdings', exact: true })).toBeVisible()
  await expect(primary.getByRole('button', { name: 'Dashboard', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(page.getByRole('tab', { name: /Wallet/ })).toHaveAttribute('aria-selected', 'true')
}

async function backToDashboard(page: Page, strategyId?: string) {
  await page.getByRole('navigation', { name: 'Primary navigation' }).getByRole('button', { name: 'Dashboard', exact: true }).click()
  const dashboard = page.getByRole('region', { name: 'Trading dashboard' })
  if (strategyId) {
    await page.locator(`.trade-rail [data-session-id="${strategyId}"] .rail-row`).click()
    await dashboard.getByRole('tab', { name: /Positions/ }).click()
    await expect(page.getByRole('heading', { name: 'Open Positions' })).toBeVisible()
  } else {
    const allWallet = dashboard.getByRole('button', { name: 'All wallet activity', exact: true })
    if (await allWallet.count()) await allWallet.click()
    await dashboard.getByRole('tab', { name: /Overview/ }).click()
  }
}

async function openOrder(page: Page) {
  const dashboard = page.getByRole('region', { name: 'Trading dashboard' })
  await dashboard.getByRole('button', { name: /New order/i }).click()
  const order = dashboard.getByRole('region', { name: 'New order' })
  await expect(order).toBeVisible()
  return order
}

async function closeOrder(page: Page) {
  const dashboard = page.getByRole('region', { name: 'Trading dashboard' })
  await dashboard.getByRole('tab', { name: /Wallet/ }).click()
  await expect(page.getByRole('heading', { name: 'Wallet holdings', exact: true })).toBeVisible()
}

test('system diagnostics show only actionable signals', async ({ page }) => {
  await createAndEnter(page)

  let commandBoard = await openCommandBoard(page)
  let signals = commandBoard.getByLabel('Actionable system signals')
  await expect(signals).not.toContainText('services ready')
  await expect(signals).not.toContainText('sessions')
  await expect(signals).not.toContainText('monitoring')
  await expect(signals.getByRole('button', { name: 'AI setup needed — connect provider' })).toHaveCount(0)
  await expect(commandBoard.getByRole('button', { name: 'Guides', exact: true })).toBeVisible()
  await expect(commandBoard.locator('.command-connect').getByRole('button', { name: /^(Connect AI|Add API key)$/ })).toBeVisible()
  await expect(commandBoard.locator('.command-board-overview')).toBeVisible()
  await expect(commandBoard.getByText('SYSTEM STATUS', { exact: true })).toHaveCount(1)
  await expect(commandBoard.getByText('Runner startup', { exact: true })).toHaveCount(0)
  await expect(commandBoard).toContainText('RPC sync test')
  await expect(commandBoard).toContainText('Mainnet identity and current blockhash verified')

  await page.route('**/api/v1/workspace', (route) => route.fulfill({ status: 503, json: { error: 'Runner unavailable for test' } }))
  await page.reload()
  commandBoard = await openCommandBoard(page)
  signals = commandBoard.getByLabel('Actionable system signals')
  await expect(commandBoard.getByRole('region', { name: 'Wallet connection' }).getByRole('button', { name: 'Unlock signing', exact: true })).toBeVisible()
  await expect(commandBoard.getByText('Runner startup', { exact: true })).toBeVisible()
  await expect(commandBoard).toContainText('Unavailable')
  await expect(commandBoard.locator('.command-board-overview')).toContainText(`Runner v${currentVersion} · Unavailable`)
  await expect(commandBoard.locator('.command-board-overview')).toContainText('Runner startup')
  await expect(commandBoard.locator('.command-board-overview')).toHaveAttribute('data-state', 'attention')
  await expect(commandBoard.locator('.agent-terminal-bar').getByLabel('Actionable system signals')).toHaveCount(0)
  const actions = commandBoard.getByRole('region', { name: 'System activity actions' })
  await expect(actions.getByRole('button', { name: 'Retry startup', exact: true })).toBeVisible()
  await commandBoard.locator('.command-board-details > summary').click()
  await expect(actions.getByRole('button', { name: 'Retry startup', exact: true })).not.toBeVisible()
  const detached = page.waitForEvent('popup')
  await page.getByRole('button', { name: 'Open in new window' }).click()
  const activityWindow = await detached
  await expect(activityWindow.getByRole('region', { name: 'System activity actions' }).getByRole('button', { name: 'Retry startup', exact: true })).toBeVisible()
  await activityWindow.getByRole('button', { name: 'Return to Dashboard' }).click()
  await expect(actions.getByRole('button', { name: 'Retry startup', exact: true })).toBeVisible()

  await page.unroute('**/api/v1/workspace')
  await page.route('**/api/v1/rpc', (route) => route.fulfill({ status: 503, json: { error: 'RPC unavailable for test' } }))
  await page.reload()
  await expect(page.locator('.command-board-overview')).toContainText('RPC needs attention')
  await expect(page.locator('.command-board-overview')).toHaveAttribute('data-state', 'attention')
  await expect(page.getByRole('button', { name: 'Fix RPC connection', exact: true })).toBeVisible()
})

test('dashboard keeps server-saved 24-hour worth movement separate from trading P/L', async ({ page }) => {
  await createAndEnter(page)
  await page.getByRole('tab', { name: /Wallet/ }).click()
  let summary = page.locator('.portfolio-dashboard')
  await expect(summary).toContainText('$125.00')
  await expect(summary).toContainText('No complete snapshot from 24 hours ago')
  await expect(summary).toContainText('Current open Runner positions · USD estimate before exit costs')
  await page.evaluate(async () => {
    const prefs = (await (await fetch('/api/v1/app-state')).json()).preferences
    const key = Object.keys(prefs).find((item) => item.startsWith('pumpwizard.portfolio-history/v1:'))
    if (!key) throw new Error('Expected an initial server-saved portfolio sample')
    const current = JSON.parse(prefs[key] || '[]')[0]
    await fetch('/api/v1/app-state/preferences', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key, value: JSON.stringify([{ at: current.at - 86_400, usd: 100, pnl: null }]) }) })
  })
  await page.reload()
  await page.getByRole('tab', { name: /Wallet/ }).click()
  summary = page.locator('.portfolio-dashboard')
  await expect(summary).toContainText('+$25.00')
  await expect(summary).toContainText('+25.00%')
  await expect(summary).toContainText('includes deposits and withdrawals')
  await expect(summary).toContainText('Current open Runner positions · USD estimate before exit costs')
})

test('creates, backs up, locks, unlocks, deposits, and opens manual trading', async ({ page }) => {
  await createAndEnter(page)

  const tabSession = await page.evaluate(async () => JSON.parse((await (await fetch('/api/v1/app-state')).json()).preferences['pumpwizard.wallet-session/v1'] || '{}'))
  expect(tabSession).toMatchObject({ schema: 'pumpwizard.wallet-session/v1', kind: 'local', network: 'mainnet-beta', market: 'pumpfun' })
  expect(JSON.stringify(tabSession)).not.toContain(password)
  await page.reload()
  await expect(page.getByRole('main', { name: 'Wallet dashboard' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Choose a wallet' })).toHaveCount(0)
  const lockedBalance = page.getByRole('region', { name: 'Wallet connection' })
  await expect(lockedBalance).toContainText('signing locked')
  await openCommandBoard(page)
  await lockedBalance.getByRole('button', { name: 'Unlock signing', exact: true }).click()
  const sessionUnlock = page.getByRole('dialog', { name: 'Unlock wallet signing' })
  await expect(sessionUnlock).toContainText('terminal session survived the refresh')
  await sessionUnlock.getByLabel('Wallet password').fill(password)
  await sessionUnlock.getByRole('button', { name: 'Unlock signing' }).click()
  await expect(sessionUnlock).toHaveCount(0)
  await expect(lockedBalance.getByRole('button', { name: 'Unlock signing', exact: true })).toHaveCount(0)
  await expect(lockedBalance).toContainText('CONNECTED WALLET')
  await openActivity(page)
  await openOrder(page)
  await closeOrder(page)

  await openPortfolio(page)
  await expect(page.getByRole('region', { name: 'Wallet connection' })).toContainText('CONNECTED WALLET')
  await expect(page.getByRole('group', { name: 'Wallet tools', exact: true })).toHaveCount(0)
  await (await openWalletActions(page)).getByRole('button', { name: 'Deposit', exact: true }).click()
  const deposit = page.getByRole('dialog', { name: 'Deposit SOL' })
  await expect(deposit).toContainText('Solana Mainnet')
  await expect(deposit.getByRole('img', { name: /QR code for/ })).toBeVisible()
  const address = await deposit.locator('.deposit-address').textContent()
  expect(address).toMatch(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/)
  await deposit.getByRole('button', { name: 'Refresh balance' }).click()
  await expect(deposit).toContainText('Balance refreshed.')
  await expect(deposit).toContainText('PumpWizard Wallet')
  await deposit.getByRole('button', { name: 'Export key' }).click()
  const keyExport = page.getByRole('dialog', { name: 'Export private key' })
  await keyExport.getByLabel('Wallet password').fill('incorrect password')
  await keyExport.getByRole('button', { name: 'Reveal private key' }).click()
  await expect(keyExport).toContainText('password is incorrect')
  await keyExport.getByLabel('Wallet password').fill(password)
  await keyExport.getByRole('button', { name: 'Reveal private key' }).click()
  const privateKey = await keyExport.getByLabel('Base58 private key').inputValue()
  expect(privateKey).toMatch(/^[1-9A-HJ-NP-Za-km-z]{80,90}$/)
  expect(fromBase58(privateKey)).toHaveLength(64)
  const keyDownloadPromise = page.waitForEvent('download')
  await keyExport.getByRole('button', { name: 'Download JSON keypair' }).click()
  const keyDownload = await keyDownloadPromise
  expect(keyDownload.suggestedFilename()).toMatch(/^pumpwizard-keypair-.*\.json$/)
  await keyExport.getByRole('button', { name: 'Done' }).click()
  await deposit.getByRole('button', { name: 'Close dialog' }).click()
  await backToDashboard(page)

  const publicKey = fromBase58(address || '')
  const unsigned = unsignedLegacyTransaction(publicKey)
  let validSignature = false
  await page.route('**/api/v1/trades/preview', async (route) => {
    const now = Date.now() / 1000
    await route.fulfill({ json: { schema: 'pumpwizard.runner-web/v1', version: 'e2e', preview: {
      id: 'browser-signing-e2e', network: 'mainnet-beta', sender: address,
      decimals: 6, priority_fee: "0", mint: address, side: 'buy', amount: 10_000_000, slippage_bps: 100,
      position_id: null, route: 'pump-v2-native-sol-spl-v1', created_at: now,
      expires_at: now + 60, fee: 5_000, minimum_output: 1_000_000,
      expected_output: 1_100_000, expected_wallet_delta: -10_005_000,
      max_wallet_debit: 10_005_000,
      unsigned_transaction: Buffer.from(unsigned).toString('base64'),
    } } })
  })
  await page.route('**/api/v1/trades/submit', async (route) => {
    const payload = route.request().postDataJSON() as { signed_transaction: string }
    const signed = Uint8Array.from(Buffer.from(payload.signed_transaction, 'base64'))
    validSignature = nacl.sign.detached.verify(signed.subarray(65), signed.subarray(1, 65), publicKey)
    await route.fulfill({ json: { schema: 'pumpwizard.runner-web/v1', version: 'e2e', signature: 'e2e-signature', state: 'submitted' } })
  })
  const trading = await openOrder(page)
  await expect(trading).toContainText('PumpWizard Wallet')
  await expect(trading.getByRole('button', { name: 'Review order' })).toBeDisabled()
  await trading.getByLabel('Token mint').fill(address || '')
  await trading.getByLabel(/Spend amount/).fill('0.01')
  await trading.getByRole('button', { name: 'Review order' }).click()
  await expect(trading).toContainText('ORDER REVIEW')
  await trading.getByRole('button', { name: 'Sign & submit' }).click()
  await expect.poll(() => validSignature).toBe(true)
  await closeOrder(page)

  await expect(page.getByLabel('Search retained market records')).toHaveCount(0)
  await expect(page.locator('.coin-table')).toHaveCount(0)

  await openPortfolio(page)
  await (await openWalletActions(page)).getByRole('button', { name: 'Sign out of wallet', exact: true }).click()
  await page.getByRole('button', { name: 'Connect wallet', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Choose how to begin' })).toBeVisible()
  await page.getByRole('button', { name: /Unlock PumpWizard Wallet/ }).click()
  await expect(page.getByRole('heading', { name: 'Unlock your wallet' })).toBeVisible()
  await page.getByLabel('Wallet password').fill('incorrect password')
  await page.getByRole('button', { name: 'Unlock & sign in' }).click()
  await expect(page.getByText(/password is incorrect/)).toBeVisible()
  await page.getByLabel('Wallet password').fill(password)
  await page.getByRole('button', { name: 'Unlock & sign in' }).click()
  const restoredWelcome = page.getByRole('dialog', { name: /Welcome to PumpWizard|Runner updated to/ })
  await restoredWelcome.waitFor({ state: 'visible', timeout: 2_000 }).catch(() => undefined)
  if (await restoredWelcome.isVisible()) await restoredWelcome.getByRole('button', { name: 'Skip', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Wallet connection' })).toContainText('PumpWizard Wallet')
  await openPortfolio(page)
  await expect(page.getByRole('region', { name: 'Wallet balance and valuation summary' })).toContainText('1.25 SOL')
})

// Mirrors the captured Firefox response: fill an absent price with 375,000
// micro-lamports/CU. A zero price is deliberate and must survive an SDK rebuild.
function rebuildTestWallet(wire: number[], mode: string) {
  const original = Transaction.from(Uint8Array.from(wire))
  if (original.signatures.some(({ signature }) => signature !== null)) throw new Error('Only unsigned test messages may be rebuilt')
  const rebuilt = new Transaction({ feePayer: original.feePayer, recentBlockhash: original.recentBlockhash }).add(...original.instructions)
  const priceIndex = rebuilt.instructions.findIndex((ix) => ix.programId.equals(ComputeBudgetProgram.programId) && ComputeBudgetInstruction.decodeInstructionType(ix) === 'SetComputeUnitPrice')
  if (priceIndex < 0) rebuilt.add(ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 375_000 }))
  else if (mode === 'priority-fee') rebuilt.instructions[priceIndex] = ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 375_000 })
  if (mode === 'lighthouse') rebuilt.add(new TransactionInstruction({ programId: new PublicKey('L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95'), keys: [], data: Buffer.from([1]) }))
  if (mode === 'v0') return Array.from(new VersionedTransaction(new TransactionMessage({ payerKey: original.feePayer!, recentBlockhash: original.recentBlockhash!, instructions: rebuilt.instructions }).compileToV0Message()).serialize())
  return Array.from(rebuilt.serialize({ requireAllSignatures: false, verifySignatures: false }))
}

test('wallet fee autofill reproduces the captured 12-byte change and respects explicit zero', () => {
  const original = new Transaction({ feePayer: new PublicKey(new Uint8Array(32).fill(4)), recentBlockhash: '11111111111111111111111111111111' }).add(ComputeBudgetProgram.setComputeUnitLimit({ units: 400_000 }))
  const serialize = (tx: Transaction) => Array.from(tx.serialize({ requireAllSignatures: false, verifySignatures: false }))
  const wire = serialize(original)
  const augmented = rebuildTestWallet(wire, 'accept')
  expect(augmented.length).toBe(wire.length + 12)
  const price = Transaction.from(augmented).instructions.at(-1)!
  expect(price.data.toString('hex')).toBe('03d8b8050000000000')
  expect(ComputeBudgetInstruction.decodeSetComputeUnitPrice(price).microLamports).toBe(375_000n)
  original.add(ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 0 }))
  const explicitZero = serialize(original)
  expect(rebuildTestWallet(explicitZero, 'accept')).toEqual(explicitZero)
})

async function installTestExtensions(page: Page, persistFixture = false) {
  await page.exposeFunction('__rebuildLegacyTransaction', rebuildTestWallet)
  await page.addInitScript((persistFixture) => {
    const base58 = (bytes: Uint8Array) => {
      const alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
      let value = 0n
      for (const byte of bytes) value = value * 256n + BigInt(byte)
      let encoded = ''
      while (value > 0n) { encoded = alphabet[Number(value % 58n)] + encoded; value /= 58n }
      for (const byte of bytes) { if (byte !== 0) break; encoded = `1${encoded}` }
      return encoded || '1'
    }
    const restored = persistFixture ? sessionStorage.getItem('e2e-only-extension-key') : null
    const keysPromise = restored ? (async () => {
      const saved = JSON.parse(restored)
      return { privateKey: await crypto.subtle.importKey('jwk', saved.privateKey, { name: 'Ed25519' }, true, ['sign']), publicKey: await crypto.subtle.importKey('jwk', saved.publicKey, { name: 'Ed25519' }, true, ['verify']) }
    })() : crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify'])
    keysPromise.then(async (keys) => {
      if (persistFixture) sessionStorage.setItem('e2e-only-extension-key', JSON.stringify({ privateKey: await crypto.subtle.exportKey('jwk', keys.privateKey), publicKey: await crypto.subtle.exportKey('jwk', keys.publicKey) }))
      const publicKey = new Uint8Array(await crypto.subtle.exportKey('raw', keys.publicKey))
      const account = Object.freeze({
        address: base58(publicKey),
        publicKey,
        chains: ['solana:mainnet', 'solana:devnet'],
        features: ['solana:signMessage', 'solana:signTransaction'],
      })
      type Change = { accounts?: (typeof account)[]; chains?: string[] }
      const listeners = new Set<(properties: Change) => void>()
      const control = {
        address: account.address,
        mode: 'accept',
        signRequests: 0,
        release: () => {},
        change: (properties: Change) => listeners.forEach((listener) => listener(properties)),
      }
      Object.assign(window, { __testWallet: control })
      const wallet = Object.freeze({
        version: '1.0.0',
        name: 'E2E Phantom compatible',
        icon: 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4=',
        chains: ['solana:mainnet', 'solana:devnet'],
        accounts: [account],
        features: {
          'standard:connect': { version: '1.0.0', connect: async () => ({ accounts: [account] }) },
          'standard:events': { version: '1.0.0', on: (_event: string, listener: (properties: Change) => void) => { listeners.add(listener); return () => listeners.delete(listener) } },
          'solana:signMessage': { version: '1.0.0', signMessage: async (...inputs: { message: Uint8Array }[]) => Promise.all(inputs.map(async ({ message }) => ({ signedMessage: message, signature: new Uint8Array(await crypto.subtle.sign({ name: 'Ed25519' }, keys.privateKey, message)) }))) },
          'solana:signTransaction': { version: '1.0.0', supportedTransactionVersions: [0, 'legacy'], signTransaction: async (...inputs: { transaction: Uint8Array }[]) => {
            control.signRequests += 1
            if (control.mode === 'reject') throw new Error('User rejected the wallet request')
            if (control.mode === 'wait') await new Promise<void>((resolve) => { control.release = resolve })
            return Promise.all(inputs.map(async ({ transaction }) => {
              const rebuild = (window as unknown as { __rebuildLegacyTransaction: (wire: number[], mode: string) => Promise<number[]> }).__rebuildLegacyTransaction
              const signed = Uint8Array.from(await rebuild(Array.from(transaction), control.mode))
              if (control.mode === 'message-change') signed[signed.length - 1] ^= 1
              if (control.mode === 'input-mutation') {
                transaction[transaction.length - 1] ^= 1
                signed[signed.length - 1] = transaction[transaction.length - 1]
              }
              signed.set(new Uint8Array(await crypto.subtle.sign({ name: 'Ed25519' }, keys.privateKey, signed.subarray(65))), 1)
              if (control.mode === 'bad-signature') signed.fill(0, 1, 65)
              return { signedTransaction: signed }
            }))
          } },
        },
      })
      const secondWallet = Object.freeze({ ...wallet, name: 'E2E Solflare compatible' })
      window.addEventListener('wallet-standard:app-ready', ((event: CustomEvent) => {
        event.detail.register(wallet)
        event.detail.register(secondWallet)
      }) as EventListener)
      window.dispatchEvent(new CustomEvent('wallet-standard:register-wallet', { detail: (api: { register: (value: object) => void }) => {
        api.register(wallet)
        api.register(secondWallet)
      } }))
    })
  }, persistFixture)
}

async function connectTestExtension(page: Page, persistFixture = false) {
  await installTestExtensions(page, persistFixture)
  await page.goto('/')
  await expect(page.getByText(/2 extensions detected automatically/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Connect E2E Phantom compatible' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Connect E2E Solflare compatible' })).toBeVisible()
  await page.getByRole('button', { name: 'Connect E2E Phantom compatible' }).click()
  await page.getByRole('dialog', { name: /Welcome to PumpWizard/ }).getByRole('button', { name: 'Skip', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Wallet connection' })).toContainText('E2E Phantom compatible')
  await expect(page.getByRole('region', { name: 'Wallet connection' })).toContainText('E2E Phantom compatible wallet connected')
  await openActivity(page)
}

test('connects and proves ownership through a Wallet Standard extension', async ({ page }) => {
  await connectTestExtension(page)
  await page.getByRole('button', { name: 'Add session', exact: true }).click()
  const wizard = page.getByRole('dialog', { name: 'Create session' })
  await wizard.getByRole('button', { name: /Trading session/ }).click()
  await expect(wizard.getByRole('button', { name: /Interactive/ })).toBeEnabled()
  await expect(wizard.getByRole('button', { name: /Automatic entries only/ })).toBeDisabled()
  await expect(wizard.getByRole('button', { name: /Full Autopilot/ })).toBeDisabled()
})

test('shows generated and connected extension wallets together after creation', async ({ page }, testInfo) => {
  await installTestExtensions(page)
  await page.goto('/')
  await page.getByRole('button', { name: 'Create a new wallet' }).click()
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByLabel('Confirm password').fill(password)
  await page.getByRole('button', { name: 'Create encrypted wallet' }).click()
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download encrypted backup' }).click()
  await downloadPromise
  await page.getByLabel(/I saved the encrypted backup/).check()
  await page.getByRole('button', { name: 'Choose a wallet' }).click()
  await expect(page.getByRole('heading', { name: 'Choose how to begin' })).toBeVisible()
  await expect(page.getByRole('button', { name: /Unlock PumpWizard Wallet/ })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Connect E2E Phantom compatible' })).toContainText('Connected in extension')
  await expect(page.getByRole('button', { name: 'Connect E2E Solflare compatible' })).toBeVisible()
  await expect(page.getByText(/3 wallets available/)).toBeVisible()
  await page.evaluate(async () => {
    const collection = (await (await fetch('/api/v1/app-state')).json())
    localStorage.setItem('pumpwizard.browser-wallet/v1', JSON.stringify(collection.wallets[0]))
    localStorage.removeItem('pumpwizard.browser-wallets/v2')
  })
  await page.reload()
  await expect(page.getByRole('button', { name: /Unlock PumpWizard Wallet/ })).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Connect E2E Phantom compatible' })).toBeVisible()
  await page.getByRole('button', { name: 'Create a new wallet' }).click()
  await page.getByLabel('Password', { exact: true }).fill('second generated wallet password')
  await page.getByLabel('Confirm password').fill('second generated wallet password')
  await page.getByRole('button', { name: 'Create encrypted wallet' }).click()
  const secondDownload = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download encrypted backup' }).click()
  await secondDownload
  await page.getByLabel(/I saved the encrypted backup/).check()
  await page.getByRole('button', { name: 'Choose a wallet' }).click()
  await expect(page.getByRole('button', { name: /Unlock PumpWizard Wallet/ })).toHaveCount(2)
  await expect(page.getByText(/4 wallets available/)).toBeVisible()
  expect(await page.evaluate(async () => localStorage.getItem('pumpwizard.browser-wallet/v1'))).toBeNull()
  await page.screenshot({ path: testInfo.outputPath('wallet-chooser.png'), fullPage: true })
  await page.getByRole('button', { name: 'Connect E2E Phantom compatible' }).click()
  await expect(page.getByRole('dialog', { name: /Welcome to PumpWizard/ })).toBeVisible()
})

test('imports, configures, monitors, and restores the portfolio workspace', async ({ page }, testInfo) => {
  await createAndEnter(page)

  const workspace = await page.evaluate(async () => {
    const response = await fetch('/api/v1/workspace')
    return response.json()
  })

  const primary = page.getByRole('navigation', { name: 'Primary navigation' })
  await expect(primary.getByRole('button', { name: 'Dashboard', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(page.getByRole('button', { name: 'Research & reports', exact: true })).toHaveCount(0)
  await expect(page.getByRole('tab', { name: /Wallet/ })).toBeVisible()
  await openPortfolio(page)
  const walletSummary = page.getByRole('region', { name: 'Wallet balance and valuation summary' })
  await expect(walletSummary).toContainText('1.25 SOL')
  await expect(walletSummary).toContainText('$125.00')
  await expect(walletSummary).not.toContainText('RUNNER TRADING P/L')
  await expect(walletSummary).not.toContainText('24H WORTH MOVE')
  await expect(page.getByRole('button', { name: /Open portfolio/ })).toHaveCount(0)
  await openSessions(page)
  await expect(page.locator('.trade-rail')).toContainText('SESSIONS')
  await expect(page.locator('.trade-rail')).not.toContainText('OPEN POSITIONS')
  await openActivity(page)
  const positionsTerminal = page.getByRole('region', { name: 'Trading dashboard' })
  await expect(positionsTerminal).toContainText('No Runner trade history')
  await expect(positionsTerminal.getByRole('button', { name: 'View portfolio' })).toHaveCount(0)
  await expect(positionsTerminal.getByRole('tab', { name: /Observations/ })).toHaveCount(0)
  await positionsTerminal.getByRole('tab', { name: /Activity/ }).click()
  await expect(positionsTerminal.getByRole('heading', { name: 'Trade History' })).toBeVisible()
  await expect(positionsTerminal).toContainText('No Runner trade history')
  await positionsTerminal.getByRole('tab', { name: /Overview/ }).click()
  await expect(page.getByLabel('Search retained market records')).toHaveCount(0)
  await expect(page.locator('.coin-table')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('positions-terminal-empty.png'), fullPage: true })
  let agentMonitor = await openCommandBoard(page)
  await expect(page.getByRole('link', { name: `v${workspace.version} ↗` })).toHaveCount(0)
  await expect(agentMonitor).toContainText('PumpWizard')
  let commandSignals = agentMonitor.getByLabel('Actionable system signals')
  await expect(commandSignals).not.toContainText('services ready')
  await expect(commandSignals).not.toContainText('monitoring')
  await expect(commandSignals.getByRole('button', { name: 'AI setup needed — connect provider' })).toHaveCount(0)
  await expect(agentMonitor.getByRole('button', { name: 'Guides', exact: true })).toBeVisible()
  await expect(agentMonitor.getByText('Runner startup', { exact: true })).toHaveCount(0)
  await expect(agentMonitor).toContainText(`Runner v${workspace.version}`)
  await expect(agentMonitor).toContainText('RPC sync test')
  await expect(agentMonitor).toContainText('Connect a provider only if you want command-board chat')
  await agentMonitor.getByRole('button', { name: 'Add API key' }).click()
  const assistantSetup = page.getByRole('dialog', { name: 'Connect AI provider' })
  await expect(assistantSetup.getByLabel('Provider')).toContainText('Anthropic Claude')
  await expect(assistantSetup.getByLabel('Provider')).toContainText('DeepSeek API')
  await assistantSetup.getByLabel('Provider').selectOption('anthropic')
  await expect(assistantSetup.getByLabel('Model')).toHaveValue('claude-sonnet-5')
  await assistantSetup.getByLabel('Provider').selectOption('deepseek')
  await expect(assistantSetup.getByLabel('Model')).toHaveValue('deepseek-flash')
  await assistantSetup.getByLabel('API key').fill('browser-test-provider-key')
  await assistantSetup.getByRole('button', { name: 'Save local connection' }).click()
  await expect(agentMonitor).toContainText('PumpWizard')
  await expect(agentMonitor.locator('.command-connect')).toHaveCount(0)
  await expect(agentMonitor.locator('[data-check-id=assistant][data-current=true]').getByRole('heading', { name: 'AI provider' })).toBeVisible()
  await expect(agentMonitor).toContainText('Chat ready')
  await expect(agentMonitor).toContainText('DeepSeek · deepseek-flash')
  const assistantMessage = page.getByLabel('Assistant message')
  await expect(assistantMessage).toBeVisible()
  await assistantMessage.fill('Summarize the current command-board status.')
  await agentMonitor.getByRole('button', { name: 'Review send' }).click()
  const assistantApproval = agentMonitor.getByRole('region', { name: 'Approve AI request' })
  await expect(assistantApproval).toContainText('PROVIDER APPROVAL REQUIRED')
  await expect(assistantApproval).toContainText('No local strategy or coin data is attached yet')
  await assistantApproval.getByRole('button', { name: 'Cancel' }).click()
  await expect(agentMonitor).toContainText('No provider request was sent')
  await page.reload()
  agentMonitor = await openCommandBoard(page)
  commandSignals = agentMonitor.getByLabel('Actionable system signals')
  await expect(agentMonitor.getByRole('region', { name: 'Wallet connection' }).getByRole('button', { name: 'Unlock signing', exact: true })).toBeVisible()
  const historyChoice = agentMonitor.getByRole('region', { name: 'Previous chat history' })
  await expect(historyChoice).toContainText('I found 1 previous local chat')
  await expect(historyChoice.getByRole('button', { name: 'Read selected chat' })).toBeVisible()
  await expect(historyChoice.getByRole('button', { name: 'Start fresh' })).toBeVisible()
  await expect(page.getByLabel('Assistant message')).toBeDisabled()
  await historyChoice.getByRole('button', { name: 'Read selected chat' }).click()
  await expect(agentMonitor).toContainText('Summarize the current command-board status.')
  await agentMonitor.getByRole('button', { name: 'New chat' }).click()
  await expect(page.getByLabel('Assistant message')).toBeEnabled()
  await openPortfolio(page)
  const holdingsSummary = page.getByRole('region', { name: 'Wallet balance and valuation summary' })
  await expect(holdingsSummary).toContainText('1.25 SOL')
  await expect(holdingsSummary).toContainText('$125.00')
  await expect(page.getByRole('region', { name: 'Portfolio valuation context' })).toContainText('$100.00 per SOL')
  await expect(page.getByText('No current token holdings')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('portfolio-sol-value.png'), fullPage: true })
  await backToDashboard(page)
  await openSessions(page)
  await page.getByRole('button', { name: 'Add session' }).click()
  let sessionPlan = page.getByRole('dialog', { name: 'Create session' })
  await expect(sessionPlan).toContainText('Monitor')
  await expect(sessionPlan).toContainText('Trading session')
  const modeCards = sessionPlan.locator('.session-mode-step > button')
  await expect(modeCards).toHaveCount(2)
  expect((await modeCards.first().boundingBox())?.width).toBeGreaterThanOrEqual(400)
  expect((await modeCards.first().boundingBox())?.height).toBeGreaterThanOrEqual(420)
  await sessionPlan.getByRole('button', { name: /LEARN FIRST Monitor/ }).click()
  const monitoringConfirmation = page.getByRole('dialog', { name: 'Confirm monitoring session' })
  await expect(monitoringConfirmation.getByRole('heading', { name: 'Confirm monitoring session' })).toBeVisible()
  await expect(monitoringConfirmation).toContainText('No reservation')
  await expect(monitoringConfirmation).toContainText('No signing path')
  await expect(monitoringConfirmation).toContainText('Nothing starts from this confirmation')
  await expect(monitoringConfirmation).not.toContainText('Investment size')
  await expect(monitoringConfirmation).not.toContainText('Time horizon')
  await monitoringConfirmation.getByRole('button', { name: 'Back' }).click()
  sessionPlan = page.getByRole('dialog', { name: 'Create session' })
  await page.locator('.modal-backdrop.detail-layer').click({ position: { x: 5, y: 5 } })
  await expect(sessionPlan).toHaveCount(0)
  await page.getByRole('button', { name: 'Add session' }).click()
  sessionPlan = page.getByRole('dialog', { name: 'Create session' })
  await sessionPlan.getByRole('button', { name: /Trading session/ }).click()
  await expect(sessionPlan.getByRole('button', { name: /Interactive/ })).toHaveAttribute('aria-pressed', 'true')
  await expect(sessionPlan).toContainText('Full Autopilot')
  await expect(sessionPlan).toContainText('Automatic entries only')
  await sessionPlan.getByRole('button', { name: /Full Autopilot/ }).click()
  await sessionPlan.getByRole('button', { name: /Automatic entries only/ }).click()
  await expect(sessionPlan).toContainText('You manage every exit')
  await expect(sessionPlan).not.toContainText('Safer automatic mode')
  await sessionPlan.getByRole('button', { name: /Full Autopilot/ }).click()
  await expect(sessionPlan).toContainText('Entries and exits')
  await expect(sessionPlan).toContainText('RUNS IN THE BACKGROUND SERVICE')
  await expect(sessionPlan.getByRole('button', { name: /This tab only/ })).toHaveCount(0)
  await sessionPlan.getByRole('button', { name: /Interactive/ }).click()
  await sessionPlan.getByRole('button', { name: 'Continue' }).click()
  await expect(sessionPlan).toContainText('Main wallet')
  await expect(sessionPlan).toContainText('1.25 SOL')
  await sessionPlan.getByLabel('Session allocation (SOL)').fill('0.2')
  await expect(sessionPlan).toContainText('STEP 3 OF 3')
  await expect(sessionPlan).not.toContainText('Choose position sizing')
  const workspaceResponse = page.waitForResponse((response) => response.url().endsWith('/api/v1/session-workspaces') && response.request().method() === 'POST')
  await sessionPlan.getByRole('button', { name: 'Create session' }).click()
  const tradingWorkspace = await (await workspaceResponse).json() as { session: { id: string; allocation_lamports: string } }
  expect(tradingWorkspace.session.allocation_lamports).toBe('200000000')
  await expect(sessionPlan).toHaveCount(0)
  await expect(page.getByRole('dialog', { name: 'Local strategy library' })).toHaveCount(0)
  const emptySession = page.locator('.rail-workspace-item').filter({ hasText: '0.2 SOL allocated' })
  await expect(emptySession.getByRole('button', { name: /Add strategy to/ })).toBeVisible()
  await expect(emptySession).toContainText('No strategies')
  await emptySession.getByRole('button', { name: /Add strategy to/ }).click()
  const strategyLibrary = page.getByRole('dialog', { name: 'Local strategy library' })
  await expect(strategyLibrary).toContainText('0.2 SOL allocated · Interactive')
  await expect(strategyLibrary.getByRole('button', { name: 'Public Lab Data', exact: true })).toBeVisible()
  await expect(strategyLibrary.getByRole('button', { name: /Create strategy/ })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('strategy-options.png') })
  await strategyLibrary.getByRole('button', { name: 'Public Lab Data', exact: true }).click()
  const simulations = page.getByRole('region', { name: 'Public Lab Data' })
  await expect(page.getByRole('dialog', { name: 'Local strategy library' })).toHaveCount(1)
  await simulations.getByRole('button', { name: 'Next →' }).click()
  await expect(simulations.locator('.simulation-pagination')).toContainText('Page 2')
  await expect(simulations.locator('.simulation-result-list > button.selected')).toHaveCount(1)
  await simulations.getByLabel('Search simulations').fill('early-frequency')
  await expect(simulations.locator('.simulation-result-list > button').first()).toContainText('Early frequency')
  await simulations.locator('.simulation-result-list > button').first().click()
  await simulations.getByRole('button', { name: '24 hours' }).click()
  await expect(simulations.locator('.simulation-metric-grid')).toContainText('24 HOURS P&L')
  await expect(simulations).toContainText('Configuration summary')
  await page.screenshot({ path: testInfo.outputPath('simulation-library.png') })
  await simulations.getByRole('button', { name: 'Add to session & edit' }).click()
  const importedLibrary = page.getByRole('dialog', { name: 'Local strategy library' })
  await expect(importedLibrary.getByRole('form', { name: 'Edit saved strategy' })).toBeVisible()
  await importedLibrary.getByRole('button', { name: 'Back to strategies', exact: true }).click()
  await expect(importedLibrary.getByRole('button', { name: /^Current|^History/ })).toHaveCount(0)
  await expect(importedLibrary).toContainText('Early frequency')
  await expect(importedLibrary.getByRole('heading', { name: 'Choose a strategy' })).toBeVisible()
  await expect(importedLibrary).toContainText('Configuration at a glance')
  await expect(importedLibrary).toContainText('Use strategy controls to start monitoring and review trading')
  await page.screenshot({ path: testInfo.outputPath('local-strategy-library.png') })
  await importedLibrary.getByRole('button', { name: /Create strategy/ }).click()
  const creator = page.getByRole('dialog', { name: 'Create research draft' })
  await expect(creator.getByLabel('Strategy name')).toBeVisible()
  await expect(creator.getByLabel('Entry style')).toBeVisible()
  await expect(creator.getByRole('group', { name: 'Strategy goal' })).toContainText('Short-squeeze watch')
  await expect(creator.getByRole('group', { name: 'Strategy goal' })).toContainText('Day trading')
  await expect(creator.getByRole('group', { name: 'Strategy goal' })).toContainText('Longer hold')
  await expect(creator.getByRole('group', { name: 'Strategy goal' })).toContainText('Unicorn hunter')
  await creator.getByRole('button', { name: /Longer hold/ }).click()
  await expect(creator.getByLabel('Entry style')).toHaveValue('slow_burn')
  await expect(creator).toContainText('do not establish fundamentals')
  await creator.getByRole('button', { name: /Short-squeeze watch/ }).click()
  await expect(creator.getByLabel('Entry style')).toHaveValue('squeeze')
  await expect(creator).toContainText('no short-interest or liquidation feed')
  await creator.getByRole('button', { name: 'Cancel' }).click()
  await page.getByLabel('Import strategy configuration').setInputFiles({
    name: 'browser-e2e-strategy.json',
    mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify(strategy)),
  })
  const strategyCard = importedLibrary.getByRole('article').filter({ hasText: 'Browser E2E core' })
  await expect(strategyCard).toContainText('Can add', { timeout: 15_000 })
  await strategyCard.getByRole('button').click()
  await expect(importedLibrary).toContainText('$10,000 – $25,000')
  await importedLibrary.getByText('Initial order limits', { exact: true }).click()
  await expect(importedLibrary).toContainText('Maximum buy 0.02 SOL')
  const addedResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/v1/sessions' && response.request().method() === 'POST')
  await importedLibrary.getByRole('button', { name: 'Add to session & edit', exact: true }).click()
  const added = await addedResponse
  expect(added.request().postDataJSON().brief.preferences).toBeNull()
  expect(added.request().postDataJSON().workspace_id).toBe(tradingWorkspace.session.id)
  await expect(importedLibrary.getByRole('form', { name: 'Edit saved strategy' })).toBeVisible()
  await importedLibrary.getByRole('button', { name: 'Close dialog' }).click()
  await expect(importedLibrary).toHaveCount(0)
  await expect(page.getByRole('dialog', { name: 'Create session brief' })).toHaveCount(0)
  await expect(page.getByRole('dialog', { name: 'Session monitor' })).toHaveCount(0)
  const railStrategy = page.locator('.rail-strategy-item').filter({ hasText: 'Browser E2E core' })
  await expect(railStrategy.getByRole('img', { name: /^Configured:/ })).toBeVisible()
  await expect(page.getByRole('log', { name: 'Status history' }).getByRole('button', { name: 'Open Strategy: Strategy added' }).filter({ hasText: 'Browser E2E core' })).toContainText('Browser E2E core')
  const tradingSession = page.locator('.rail-workspace-item').filter({ has: railStrategy })
  const tradingToggle = tradingSession.locator('.rail-workspace-toggle')
  await expect(tradingToggle).toHaveAttribute('title', /0.2 SOL allocated/)
  await tradingToggle.click()
  await expect(railStrategy).toBeHidden()
  await tradingToggle.press('ArrowRight')
  await railStrategy.locator('.rail-row').click()
  await openSelectedStrategyControls(page)
  await expect(page.getByRole('dialog', { name: 'Session monitor' })).toBeVisible()
  await page.getByRole('dialog', { name: 'Session monitor' }).getByRole('button', { name: 'Close dialog' }).click()
  await expect(railStrategy.locator('.rail-row')).toHaveAttribute('aria-pressed', 'true')
  const addSharedStrategy = tradingSession.getByRole('button', { name: /Add strategy to Trading session/ })
  await expect(addSharedStrategy).toBeVisible()
  await addSharedStrategy.click()
  const sharedStrategyLibrary = page.getByRole('dialog', { name: 'Local strategy library' })
  await expect(sharedStrategyLibrary).toContainText('0.2 SOL allocated · Interactive')
  await sharedStrategyLibrary.getByRole('article').filter({ hasText: 'Browser E2E core' }).getByRole('button').click()
  await expect(sharedStrategyLibrary.getByRole('button', { name: 'Edit session strategy' })).toBeEnabled()
  await sharedStrategyLibrary.getByRole('button', { name: 'Close dialog' }).click()
  await page.evaluate(async () => window.scrollTo(0, 0))
  await page.screenshot({ path: testInfo.outputPath('agent-monitor-connected.png'), fullPage: true })

  await railStrategy.getByRole('button', { name: 'Manage Browser E2E core' }).click()
  await railStrategy.getByRole('button', { name: 'Order safety' }).click()
  const controls = page.getByRole('dialog', { name: 'Reviewed order safety' })
  const monitor = controls.getByRole('article').filter({ hasText: 'Browser E2E core' })
  await expect(monitor).toContainText('limits configured')
  await monitor.getByRole('button', { name: 'Edit limits' }).click()
  await expect(controls.getByLabel('Assigned SOL')).toHaveValue('0.2')
  await expect(controls.getByLabel('Minimum buy (SOL)')).toHaveValue('0.005')
  await expect(controls.getByLabel('Maximum buy (SOL)')).toHaveValue('0.02')
  await expect(controls.getByLabel('Maximum buys per day (SOL)')).toHaveValue('0.07')
  await expect(controls.getByLabel('Daily loss cap (SOL)')).toHaveValue('0.02')
  await expect(controls.getByLabel('Allow reviewed buys')).not.toBeChecked()
  await controls.getByRole('button', { name: 'Cancel' }).click()
  await expect(controls).toContainText('limits configured')
  const savedControls = await page.evaluate(async () => {
    const collection = (await (await fetch('/api/v1/app-state')).json())
    const vault = collection.wallets[0]
    const query = new URLSearchParams({ sender: vault.address, network: 'mainnet-beta' })
    const response = await fetch(`/api/v1/strategy-controls?${query}`)
    return response.json()
  })
  expect(savedControls.allocated_lamports).toBe('200000000')
  expect(savedControls.controls[0].policy.max_trade_loss_bps).toBe(1500)
  await controls.getByRole('button', { name: 'Close dialog' }).click()

  await railStrategy.getByRole('button', { name: 'Manage Browser E2E core' }).click()
  const railActions = railStrategy.locator('.rail-strategy-actions')
  await railActions.getByRole('button', { name: 'Rename' }).click()
  await railActions.getByLabel('Session display name').fill('Browser E2E core renamed')
  await railActions.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(railStrategy).toContainText('Browser E2E core renamed')
  await railActions.getByRole('button', { name: 'Remove' }).click()
  await expect(railActions).not.toContainText('SOL will return')
  await expect(railActions.getByRole('button', { name: 'Remove', exact: true })).toBeEnabled()
  await railActions.getByRole('button', { name: 'Cancel' }).click()
  await expect(railActions).toContainText('MAX LOSS / POSITION')
  await expect(railActions).toContainText('15%')
  await expect(railActions).toContainText('momentum')
  await page.screenshot({ path: testInfo.outputPath('strategy-action-menu.png') })
  await railActions.getByRole('button', { name: 'Open monitor' }).click()
  const activity = page.getByRole('dialog', { name: 'Session monitor' })
  await expect(activity.getByRole('heading', { name: 'Browser E2E core renamed' })).toBeVisible()
  await expect(activity).toContainText('What this strategy is observing')
  await expect(activity).toContainText('MAX LOSS / POSITION')
  await expect(activity).toContainText('Wallet limit · strategy exit 30%')
  await expect(activity).toContainText('CURRENT STRATEGY')
  await activity.getByRole('button', { name: 'Run monitor' }).click()
  await expect(activity.getByRole('button', { name: /^(Stop monitor|Start scanning)$/ })).toBeVisible()
  await activity.getByRole('button', { name: 'Scan now' }).click()
  await expect(activity).toContainText('0 observations')
  await page.screenshot({ path: testInfo.outputPath('strategy-monitor.png') })
  await activity.getByRole('button', { name: 'Done' }).click()
  await expect(railStrategy.locator('.rail-row')).toHaveAttribute('aria-pressed', 'true')

  await openPortfolio(page)
  await expect(page.getByText('No current token holdings')).toBeVisible()

  const walletState = await page.evaluate(async () => {
    const response = await fetch('/api/v1/wallet-state')
    const state = await response.json()
    const collection = (await (await fetch('/api/v1/app-state')).json())
    return { ...state, current_sender: collection.wallets[0].address }
  })
  expect(walletState.transactions.filter((row: { sender: string }) => row.sender === walletState.current_sender)).toEqual([])
  const currentReservations = walletState.reservations.filter((row: { sender: string }) => row.sender === walletState.current_sender)
  expect(currentReservations).toHaveLength(1)
  expect(currentReservations[0]).toMatchObject({ status: 'held' })
  expect(walletState.signing_available).toBe(false)
})

test('keeps wallet onboarding usable on a compact screen and by keyboard', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await expect.poll(() => page.evaluate(async () => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  const create = page.getByRole('button', { name: 'Create a new wallet' })
  await create.focus()
  await expect(create).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'Create a wallet' })).toBeVisible()
  await page.getByLabel('Password', { exact: true }).focus()
  await expect(page.getByLabel('Password', { exact: true })).toBeFocused()
})

async function syntheticControl(page: Page, changes: Record<string, boolean | number> = {}) {
  return page.evaluate(async (changes) => {
    const collection = (await (await fetch('/api/v1/app-state')).json()) as { wallets: { address: string }[] }
    const address = collection.wallets[0]?.address
    const sender = address || (window as unknown as { __testWallet: { address: string } }).__testWallet.address
    const response = await fetch('/api/v1/test-chain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender, changes }) })
    if (!response.ok) throw new Error(await response.text())
    return response.json() as Promise<{ mint: string; sent: number; balance: string; tokens: string; process_id: string }>
  }, changes)
}

async function createReviewedSession(page: Page) {
  const source = await page.evaluate(async () => {
    const collection = (await (await fetch('/api/v1/app-state')).json()) as { wallets: { address: string }[] }
    const { address } = collection.wallets[0]
    const response = await fetch('/api/v1/test-strategy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender: address }) })
    if (!response.ok) throw new Error(await response.text())
    return response.json() as Promise<{ id: string }>
  })
  await openSessions(page)
  await expect(page.locator(`[data-session-id="${source.id}"] .rail-row`)).toBeVisible({ timeout: 15_000 })
  return source
}

async function createServiceAutopilot(page: Page) {
  return page.evaluate(async () => {
    const collection = (await (await fetch('/api/v1/app-state')).json()) as { wallets: { address: string }[] }
    const { address } = collection.wallets[0]
    const response = await fetch('/api/v1/test-strategy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender: address, service: true }) })
    if (!response.ok) throw new Error(await response.text())
    return response.json() as Promise<{ id: string }>
  })
}

async function serviceLifecycle(page: Page, request: { action: 'status' | 'release' } | { action: 'start'; session_id: string }) {
  return page.evaluate(async (input) => {
    const collection = (await (await fetch('/api/v1/app-state')).json()) as { wallets: { address: string }[] }
    const { address } = collection.wallets[0]
    const response = await fetch('/api/v1/test-service-lifecycle', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...input, ...(input.action === 'start' ? { sender: address } : {}) }) })
    if (!response.ok) throw new Error(await response.text())
    return response.json() as Promise<{ phase: string; detail: string; sent: number }>
  }, request)
}

async function serviceControlOrder(page: Page, sessionId: string, action: 'issue-review' | 'issue-submitted' | 'resolve-submitted' | 'status') {
  return page.evaluate(async ({ sessionId, action }) => {
    const collection = (await (await fetch('/api/v1/app-state')).json()) as { wallets: { address: string }[] }
    const { address } = collection.wallets[0]
    const response = await fetch('/api/v1/test-service-control-order', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, sender: address, session_id: sessionId }),
    })
    if (!response.ok) throw new Error(await response.text())
    return response.json() as Promise<Record<string, unknown>>
  }, { sessionId, action })
}

async function cleanupServiceAutopilot(page: Page, sessionId: string) {
  await page.evaluate(async (session_id) => {
    const response = await fetch('/api/v1/test-service-cleanup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id }) })
    if (!response.ok) throw new Error(await response.text())
  }, sessionId)
}

async function unlockAndEnter(page: Page) {
  const walletChoice = page.getByRole('button', { name: /Unlock PumpWizard Wallet/ })
  const commandBoard = page.getByRole('region', { name: 'System and optional AI' })
  await expect.poll(async () => Number(await walletChoice.isVisible()) + Number(await commandBoard.isVisible())).toBeGreaterThan(0)
  if (await walletChoice.isVisible()) {
    await walletChoice.click()
    await page.getByLabel('Wallet password').fill(password)
    await page.getByRole('button', { name: 'Unlock & sign in' }).click()
  } else {
    const commandBoard = await openCommandBoard(page)
    const restoredUnlock = commandBoard.getByRole('button', { name: 'Unlock signing', exact: true })
    await restoredUnlock.click()
    const signing = page.getByRole('dialog', { name: 'Unlock wallet signing' })
    await signing.getByLabel('Wallet password').fill(password)
    await signing.getByRole('button', { name: 'Unlock signing' }).click()
  }
  const welcome = page.getByRole('dialog', { name: /Welcome to PumpWizard|Runner updated to/ })
  await welcome.waitFor({ state: 'visible', timeout: 2_000 }).catch(() => undefined)
  if (await welcome.isVisible()) await welcome.getByRole('button', { name: 'Skip', exact: true }).click()
  await openSessions(page)
}

test('shows real bounded provider readiness without exposing provider endpoints', async ({ context, page }, testInfo) => {
  await createAndEnter(page)
  const fixture = await page.evaluate(async () => {
    const response = await fetch('/api/v1/test-provider-scenarios', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    if (!response.ok) throw new Error(await response.text())
    return response.json()
  })
  expect(fixture).toMatchObject({
    schema: 'pumpwizard.provider-browser-fixture/v1',
    states: ['healthy', 'empty', 'unavailable_route', 'partial', 'stale', 'failing'],
    wallet_authority: false,
  })
  const unexpectedWrites: string[] = []
  context.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (request.method() === 'POST' && /(?:assistant|signing|trades\/submit)/.test(path)) unexpectedWrites.push(path)
  })
  const dialog = await openServiceControls(page)
  await dialog.getByRole('button', { name: 'Refresh status' }).click()
  await expect(dialog).toContainText('Dashboard and service checked')
  await expect(dialog).toContainText('Usable source-linked quotes')
  await expect(dialog).toContainText('No matching provider universe')
  await expect(dialog).toContainText('Canonical route unavailable')
  await expect(dialog).toContainText('Read-only observation, not an executable quote')
  await expect(dialog).toContainText('canonical native-SOL, standard-SPL, creator-reward pools')
  await expect(dialog).toContainText('Partial provider coverage')
  await expect(dialog).toContainText('Source evidence is stale')
  await expect(dialog).toContainText('Provider attempt failed')
  await dialog.locator('.coverage-partial details').getByText('Inspect bounded provider stages').click()
  await expect(dialog).toContainText('Unsupported Shape: 1')
  await expect(dialog).not.toContainText('fixture.invalid')
  await dialog.screenshot({ path: testInfo.outputPath('provider-readiness-states.png') })
  expect(unexpectedWrites).toEqual([])
})

test('explains an exact retained decision locally without signing or provider approval', async ({ context, page }, testInfo) => {
  await createAndEnter(page)
  const source = await createReviewedSession(page)
  const writes: string[] = []
  context.on('request', (request) => {
    if (request.method() === 'POST') writes.push(new URL(request.url()).pathname)
  })
  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  const monitor = page.getByRole('dialog', { name: 'Session monitor' })
  await monitor.getByRole('button', { name: 'Explain evidence' }).first().click()
  const assistant = monitor.getByRole('region', { name: /Explain this retained decision/ })
  await assistant.getByRole('button', { name: 'Explain from local evidence' }).click()
  await expect(assistant).toContainText('LOCAL EVIDENCE ASSISTANT')
  await expect(assistant).toContainText('Typed facts and exact source identity')
  await expect(assistant.getByRole('link', { name: /Source/ }).first()).toHaveAttribute('href', new RegExp(`/api/v1/sessions/${source.id}`))
  await expect(assistant).toContainText('OPTIONAL AI EXPLANATION')
  expect(writes.filter((path) => path === '/api/v1/assistant/evidence')).toHaveLength(1)
  expect(writes.filter((path) => path === '/api/v1/assistant/evidence/prepare')).toHaveLength(0)
  expect(writes.filter((path) => /(?:approve|signing|trades\/submit)/.test(path))).toHaveLength(0)
  await assistant.screenshot({ path: testInfo.outputPath('local-decision-evidence.png') })
})

async function prepareBuy(page: Page, mint: string) {
  const dialog = await openOrder(page)
  await dialog.getByLabel('Token mint').fill(mint)
  await dialog.getByLabel('Spend amount').fill('0.1')
  await dialog.getByRole('button', { name: 'Review order' }).click()
  await expect(dialog).toContainText('ORDER REVIEW')
  return dialog
}

for (const holderRewards of [false, true]) test(`finalizes a ${holderRewards ? 'holder rewards' : 'creator rewards'} buy, recovers partial sell across restart, and exits after graduation`, async ({ page }, testInfo) => {
  await createAndEnter(page)
  const bank = await syntheticControl(page, { holder_rewards: holderRewards })
  let dialog = await prepareBuy(page, bank.mint)
  await expect(dialog.locator('.trade-reward-mode')).toContainText(holderRewards ? 'Token holders' : 'Creator / configured fee recipients')
  if (holderRewards) await page.screenshot({ path: testInfo.outputPath('holder-review.png'), fullPage: true })
  await expect(dialog).toContainText('−0.10204428 SOL')
  await expect(dialog).toContainText('Priority fee')
  await expect(dialog).toContainText('signs here when you approve')
  await dialog.getByRole('button', { name: 'Sign & submit' }).click()
  await expect(dialog.locator('.receipt-card')).toContainText('BUY · Pending')
  await expect(dialog).toContainText('No open positions')
  await expect(dialog.getByRole('button', { name: 'Review order' })).toBeDisabled()
  await closeOrder(page)
  await openPortfolio(page)
  await syntheticControl(page, { finalize: true })
  await page.getByRole('button', { name: 'Refresh holdings' }).click()
  await expect(page.locator('.portfolio-holding-row')).toContainText('1,000')
  await expect(page.locator('.portfolio-holding-row')).toContainText('Manual trade in PumpWizard')
  const portfolioHolding = page.locator('.portfolio-holding-row')
  await expect(portfolioHolding.getByRole('heading', { name: 'Alpha Coin' })).toBeVisible()
  await expect(portfolioHolding.getByRole('img', { name: 'Alpha Coin token artwork' })).toBeVisible()
  await expect(portfolioHolding).toContainText('Unavailable')
  await expect(portfolioHolding).toContainText('No fresh usable market price')
  await expect(portfolioHolding).toContainText('+12.50%')
  await expect(portfolioHolding).toContainText(/Stale mark|Invalid mark time/)
  await expect(portfolioHolding.getByRole('link', { name: 'View on Solscan ↗' })).toHaveAttribute('href', /^https:\/\/solscan.io\/token\//)
  await portfolioHolding.getByRole('button', { name: 'Copy CA for ALPHA' }).click()
  await expect(portfolioHolding.getByRole('button', { name: 'Copy CA for ALPHA' })).toContainText('Copied')
  await portfolioHolding.getByRole('button', { name: 'Show ALPHA price history and holding details' }).click()
  await expect(portfolioHolding).toContainText('HOLDING DETAILS')
  await expect(portfolioHolding).toContainText('Manual trade in PumpWizard')
  await expect(portfolioHolding).toContainText('Executable exit unavailable')
  await expect(portfolioHolding.getByRole('img', { name: 'Price candlestick history chart and USD volume' })).toBeVisible()
  await expect(portfolioHolding).toContainText('12 saved candles')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(portfolioHolding.getByRole('button', { name: /Sell ALPHA/ })).toBeVisible()
  expect(await page.locator('.wallet-balance-amounts b').evaluate((balance) => balance.scrollWidth <= balance.clientWidth)).toBe(true)
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('dashboard-holdings-mobile.png'), fullPage: true })
  await page.setViewportSize({ width: 1600, height: 1000 })

  await expect(portfolioHolding).toContainText('Manual trade in PumpWizard')
  await expect(portfolioHolding).toContainText('1,000')
  await expect(page.getByRole('tab', { name: /Positions/ })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('manual-holding-wallet.png'), fullPage: true })
  await portfolioHolding.getByRole('button', { name: /Sell ALPHA · Manual trade in PumpWizard/ }).click()
  await expect(page.getByRole('region', { name: 'New order' }).getByLabel('Quantity to sell')).toHaveValue('1000')
  await closeOrder(page)
  await openPortfolio(page)

  await page.getByRole('button', { name: /Sell ALPHA · Manual trade in PumpWizard/ }).click()
  dialog = page.getByRole('region', { name: 'New order' })
  await expect(dialog.getByLabel('Quantity to sell')).toHaveValue('1000')
  await dialog.getByLabel('Quantity to sell').fill('400')
  await dialog.getByRole('button', { name: 'Review order' }).click()
  await syntheticControl(page, { disconnect: true })
  // Two immediate clicks must still make one API request and one signed order.
  await dialog.getByRole('button', { name: 'Sign & submit' }).evaluate((button) => { (button as HTMLButtonElement).click(); (button as HTMLButtonElement).click() })
  await expect(dialog.locator('.receipt-card').first()).toContainText('SELL · Unknown')
  expect((await syntheticControl(page)).sent).toBe(2)
  await closeOrder(page)
  await openPortfolio(page)
  await expect(page.locator('.portfolio-holding-row')).toContainText('1,000')
  await (await openWalletActions(page)).getByRole('button', { name: 'Sign out of wallet', exact: true }).click()
  await page.getByRole('button', { name: 'Connect wallet', exact: true }).click()
  await unlockAndEnter(page)
  await openPortfolio(page)
  await expect(page.getByRole('button', { name: /Sell ALPHA/ })).toBeDisabled()
  await syntheticControl(page, { finalize: true, disconnect: false })
  await page.evaluate(async () => {
    await fetch('/api/v1/test-restart', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  })
  // The server re-execs Python with the same journal and synthetic chain state.
  await expect.poll(async () => {
    try { return (await syntheticControl(page)).process_id !== bank.process_id } catch { return false }
  }).toBe(true)
  await page.reload()
  await unlockAndEnter(page)
  await openPortfolio(page)
  await expect(page.locator('.portfolio-holding-row')).toContainText('600')
  await syntheticControl(page, { graduated: true })
  await page.getByRole('button', { name: /Sell ALPHA · Manual trade in PumpWizard/ }).click()
  dialog = page.getByRole('region', { name: 'New order' })
  await expect(dialog.getByLabel('Quantity to sell')).toHaveValue('600')
  await dialog.getByRole('button', { name: 'Review order' }).click()
  await expect(dialog).toContainText('Canonical PumpSwap exit')
  await expect(dialog.locator('.trade-reward-mode')).toContainText(holderRewards ? 'Token holders' : 'Creator / configured fee recipients')
  await dialog.getByRole('button', { name: 'Sign & submit' }).click()
  await expect(dialog.locator('.receipt-card').first()).toContainText('SELL · Pending')
  await closeOrder(page)
  await syntheticControl(page, { finalize: true })
  await openPortfolio(page)
  await page.getByRole('button', { name: 'Refresh holdings' }).click()
  await expect(page.getByText('No current token holdings')).toBeVisible()
  expect(await syntheticControl(page)).toMatchObject({ sent: 3, balance: '1247945720', tokens: '0' })
  await page.screenshot({ path: testInfo.outputPath('portfolio.png'), fullPage: true })
})

test('shows only current wallet holdings and removes an externally sold balance', async ({ page }, testInfo) => {
  await createAndEnter(page)
  const bank = await syntheticControl(page)
  const dialog = await prepareBuy(page, bank.mint)
  await dialog.getByRole('button', { name: 'Sign & submit' }).click()
  await closeOrder(page)
  await syntheticControl(page, { finalize: true })
  await openPortfolio(page)
  await page.getByRole('button', { name: 'Refresh holdings' }).click()
  await expect(page.getByRole('region', { name: 'Current holdings' })).toContainText('1,000')
  await expect(page.getByRole('region', { name: 'Current holdings' })).toContainText('Manual trade in PumpWizard')
  await page.screenshot({ path: testInfo.outputPath('wallet-portfolio-current.png'), fullPage: true })
  await syntheticControl(page, { external_sell: true })
  await page.getByRole('button', { name: 'Refresh holdings' }).click()
  await expect(page.getByText('No current token holdings')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('wallet-portfolio-external-sale.png'), fullPage: true })
})

test('attributes and sells a wallet holding purchased outside PumpWizard', async ({ page }) => {
  await createAndEnter(page)
  const bank = await syntheticControl(page, { tokens: 1_000_000_000 })
  await openPortfolio(page)
  await page.getByRole('button', { name: 'Refresh holdings' }).click()
  const holding = page.locator('.portfolio-holding-row')
  await expect(holding).toContainText('Outside PumpWizard')
  await expect(holding).toContainText('1,000')
  await holding.getByRole('button', { name: /Sell ALPHA · Outside PumpWizard/ }).click()
  const dialog = page.getByRole('region', { name: 'New order' })
  await expect(dialog.getByRole('heading', { name: 'Sell wallet holding' })).toBeVisible()
  await expect(dialog.getByLabel('Quantity to sell')).toHaveValue('1000')
  await dialog.getByRole('button', { name: 'Review order' }).click()
  await dialog.getByRole('button', { name: 'Sign & submit' }).click()
  await expect(dialog.locator('.receipt-card')).toContainText('SELL · Pending')
  await closeOrder(page)
  await syntheticControl(page, { finalize: true })
  await openPortfolio(page)
  await page.getByRole('button', { name: 'Refresh holdings' }).click()
  await expect(page.getByText('No current token holdings')).toBeVisible()
  expect(await syntheticControl(page)).toMatchObject({ sent: 1, tokens: '0', mint: bank.mint })
})

test('requires a new review after quote expiry, balance change and unsigned simulation failure', async ({ page }) => {
  await createAndEnter(page)
  const bank = await syntheticControl(page)
  const dialog = await prepareBuy(page, bank.mint)
  await page.clock.install()
  await page.clock.fastForward(61_000)
  await expect(dialog.getByRole('button', { name: 'Sign & submit' })).toBeDisabled()
  await expect(dialog).toContainText('Quote expired')
  expect((await syntheticControl(page)).sent).toBe(0)
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
  await page.clock.setSystemTime(new Date())
  await dialog.getByRole('button', { name: 'Review order' }).click()
  await syntheticControl(page, { balance: 1_200_000_000 })
  await dialog.getByRole('button', { name: 'Sign & submit' }).click()
  await expect(dialog).toContainText('Wallet balance changed after review')
  await expect(dialog.getByRole('button', { name: 'Review order' })).toBeEnabled()
  await syntheticControl(page, { bad_sim: true })
  await dialog.getByRole('button', { name: 'Review order' }).click()
  await expect(dialog).toContainText('simulation')
  await expect(dialog.getByRole('button', { name: 'Sign & submit' })).toHaveCount(0)
  expect((await syntheticControl(page)).sent).toBe(0)
})

type TestExtension = { mode: string; signRequests: number; release: () => void; change: (properties: { accounts?: []; chains?: string[] }) => void }

for (const mode of ['accept', 'lighthouse', 'v0']) {
  test(`no-send wallet compatibility check records ${mode} without signatures or submission`, async ({ page }) => {
    await connectTestExtension(page)
    const bank = await syntheticControl(page)
    const dialog = await prepareBuy(page, bank.mint)
    await page.evaluate((mode) => { (window as unknown as { __testWallet: TestExtension }).__testWallet.mode = mode }, mode)
    let submissions = 0
    page.on('request', (request) => { if (request.url().endsWith('/api/v1/trades/submit')) submissions++ })
    const diagnosticRequest = page.waitForRequest((request) => request.url().endsWith('/api/v1/trades/signing-diagnostic'))
    await dialog.getByRole('button', { name: 'Check wallet compatibility', exact: true }).click()
    const body = (await diagnosticRequest).postDataJSON()
    expect(Object.keys(body).sort()).toEqual(['diagnostic', 'network', 'preview_id', 'sender'])
    expect(Object.keys(body.diagnostic).sort()).toEqual(['message', 'signature_count', 'valid_signature', 'value_type', 'wire_bytes'])
    const message = Buffer.from(body.diagnostic.message, 'base64')
    expect(body.diagnostic.wire_bytes).toBe(65 + message.length)
    expect(message[0]).toBe(mode === 'v0' ? 128 : 1)
    const result = dialog.getByRole('alert', { name: 'Wallet compatibility check', exact: true })
    await expect(result).toContainText(mode === 'accept' ? 'exact reviewed transaction' : mode === 'lighthouse' ? 'Lighthouse instruction' : 'v0')
    await expect(result).toContainText('No transaction was submitted')
    expect(submissions).toBe(0)
    expect(await syntheticControl(page)).toMatchObject({ sent: 0, balance: bank.balance, tokens: '0' })
    await expect(dialog).toContainText('No submitted orders')
  })
}

test('wallet SDK rebuild with fee autofill preserves buy, partial sell and migrated full sell receipts', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 1100 })
  await connectTestExtension(page)
  const bank = await syntheticControl(page)
  const dialog = await prepareBuy(page, bank.mint)
  await dialog.getByRole('button', { name: 'Sign & submit' }).click()
  await expect(dialog.locator('.receipt-card')).toContainText('BUY · Pending')
  await expect(dialog.getByRole('status', { name: 'Order status' })).toContainText('Waiting for the original receipt')
  await syntheticControl(page, { finalize: true })
  // Exercise background recovery, without manually replacing the old message.
  await page.evaluate(async () => document.dispatchEvent(new Event('visibilitychange')))
  await expect(dialog.locator('.receipt-card')).toContainText('BUY · Finalized')
  await expect(dialog.getByRole('status', { name: 'Order status' })).toContainText('Buy finalized')
  await expect(dialog.locator('.trade-message')).toHaveCount(0)
  await expect(dialog).not.toContainText('Waiting for the original receipt')
  await expect(dialog.getByRole('link', { name: /View signature/ })).toContainText('View transaction')
  const typedPosition = await page.evaluate(async () => {
    const sender = (window as unknown as { __testWallet: { address: string } }).__testWallet.address
    const query = new URLSearchParams({ sender, network: 'mainnet-beta' })
    const response = await fetch(`/api/v1/trades?${query}`)
    if (!response.ok) throw new Error(await response.text())
    const result = await response.json() as { positions: { valuation?: {
      schema: string
      basis: { native_state?: string; native_currency?: string | null }
      ownership: { purchase_attribution: string; current_management: string }
      authority: Record<string, boolean>
    } }[] }
    return result.positions[0]?.valuation
  })
  expect(typedPosition).toMatchObject({
    schema: 'pumpwizard.holding-valuation/v1',
    basis: { native_state: 'known', native_currency: 'lamports' },
    ownership: { purchase_attribution: 'user_manual', current_management: 'user' },
    authority: { quote: false, signer: false, orders: false, submission: false },
  })
  await dialog.screenshot({ path: testInfo.outputPath('finalized-order.png') })
  await dialog.getByRole('button', { name: /Select/ }).click()
  await dialog.getByLabel('Quantity to sell').fill('400')
  await dialog.getByRole('button', { name: 'Review order' }).click()
  await expect(dialog).toContainText('ORDER REVIEW')
  await dialog.getByRole('button', { name: 'Sign & submit' }).click()
  await expect(dialog.locator('.receipt-card').first()).toContainText('SELL · Pending')
  await syntheticControl(page, { finalize: true })
  await dialog.getByRole('button', { name: 'Refresh status' }).click()
  await expect(dialog.locator('.receipt-card').first()).toContainText('SELL · Finalized')
  expect((await syntheticControl(page)).sent).toBe(2)
  await syntheticControl(page, { graduated: true })
  await dialog.getByRole('button', { name: /Select/ }).click()
  await expect(dialog.getByLabel('Quantity to sell')).toHaveValue('600')
  await dialog.getByRole('button', { name: 'Review order' }).click()
  await expect(dialog).toContainText('Canonical PumpSwap exit')
  await dialog.getByRole('button', { name: 'Sign & submit' }).click()
  await expect(dialog.locator('.receipt-card').first()).toContainText('SELL · Pending')
  await syntheticControl(page, { finalize: true })
  await dialog.getByRole('button', { name: 'Refresh status' }).click()
  await expect(dialog.locator('.receipt-card').first()).toContainText('SELL · Finalized')
  await expect(dialog).toContainText('No open positions')
  expect((await syntheticControl(page)).sent).toBe(3)
  const dashboard = page.getByRole('region', { name: 'Trading dashboard' })
  await dashboard.getByRole('tab', { name: /Activity/ }).click()
  const history = dashboard.getByRole('table', { name: 'Runner trade history' })
  await expect(history.locator('.trade-history-row')).toHaveCount(3)
  await expect(history).toContainText('BUY')
  await expect(history).toContainText('SELL')
  await expect(history).toContainText('Finalized')
  await page.screenshot({ path: testInfo.outputPath('trade-history.png'), fullPage: true })
})

for (const mode of ['message-change', 'input-mutation', 'bad-signature', 'priority-fee']) {
  test(`wallet ${mode} is rejected before any submission request`, async ({ page }) => {
    await connectTestExtension(page)
    const bank = await syntheticControl(page)
    const dialog = await prepareBuy(page, bank.mint)
    let submissions = 0
    page.on('request', (request) => { if (request.url().endsWith('/api/v1/trades/submit')) submissions++ })
    await page.evaluate((mode) => { (window as unknown as { __testWallet: TestExtension }).__testWallet.mode = mode }, mode)
    await dialog.getByRole('button', { name: 'Sign & submit' }).click()
    const failure = dialog.getByRole('alert', { name: 'Order was not submitted' })
    await expect(failure).toBeVisible()
    await expect(failure).toContainText(mode === 'bad-signature' ? 'invalid signature' : 'changed the transaction')
    await expect(dialog).toContainText('No submitted orders')
    await expect(dialog).not.toContainText('Check submission status')
    expect(submissions).toBe(0)
    expect((await syntheticControl(page)).sent).toBe(0)
    expect(await page.evaluate(async () => (window as unknown as { __testWallet: TestExtension }).__testWallet.signRequests)).toBe(1)
  })
}

test('unsupported token review shows a focused error and never requests a wallet signature', async ({ page }, testInfo) => {
  await connectTestExtension(page)
  const bank = await syntheticControl(page, { unsupported_cashback: true })
  const dialog = await openOrder(page)
  await dialog.getByLabel('Token mint').fill(bank.mint)
  await dialog.getByLabel('Spend amount').fill('0.001')
  await dialog.getByRole('button', { name: 'Review order' }).click()
  const failure = dialog.getByRole('alert', { name: 'Could not prepare order review' })
  await expect(failure).toBeVisible()
  await expect(failure).toBeFocused()
  await expect(failure).toContainText('Mayhem and cashback curves are not supported by this route')
  await expect(failure).toContainText('Nothing was signed or submitted.')
  await expect(dialog.getByLabel('Token mint')).toHaveValue(bank.mint)
  await expect(dialog.getByLabel('Spend amount')).toHaveValue('0.001')
  await expect(dialog.getByRole('button', { name: 'Review order' })).toBeEnabled()
  await expect(dialog.getByRole('button', { name: 'Sign & submit' })).toHaveCount(0)
  await expect(dialog).toContainText('No submitted orders')
  expect(await syntheticControl(page)).toMatchObject({ sent: 0, balance: bank.balance, tokens: '0' })
  expect(await page.evaluate(async () => (window as unknown as { __testWallet: TestExtension }).__testWallet.signRequests)).toBe(0)
  await page.screenshot({ path: testInfo.outputPath('unsupported-review.png'), fullPage: true })

  // A new successful review clears the previous error without signing anything.
  await syntheticControl(page, { unsupported_cashback: false })
  await dialog.getByRole('button', { name: 'Review order' }).click()
  await expect(dialog).toContainText('ORDER REVIEW')
  await expect(failure).toHaveCount(0)
  await expect(dialog.getByRole('button', { name: 'Sign & submit' })).toBeEnabled()
  expect((await syntheticControl(page)).sent).toBe(0)
  expect(await page.evaluate(async () => (window as unknown as { __testWallet: TestExtension }).__testWallet.signRequests)).toBe(0)
})

test('wallet rejection discards the review without a submitted order', async ({ page }) => {
  await connectTestExtension(page)
  const bank = await syntheticControl(page)
  const dialog = await prepareBuy(page, bank.mint)
  await page.evaluate(async () => { (window as unknown as { __testWallet: TestExtension }).__testWallet.mode = 'reject' })
  await dialog.getByRole('button', { name: 'Sign & submit' }).click()
  await expect(dialog).toContainText('User rejected the wallet request')
  await expect(dialog).toContainText('No submitted orders')
  await expect(dialog.getByRole('button', { name: 'Sign & submit' })).toHaveCount(0)
  expect((await syntheticControl(page)).sent).toBe(0)
})

for (const change of ['account', 'network', 'expiry', 'lock']) {
  test(`blocks submission when ${change} changes while the wallet prompt is open`, async ({ page }) => {
    await connectTestExtension(page)
    const bank = await syntheticControl(page)
    const dialog = await prepareBuy(page, bank.mint)
    await page.evaluate(async () => { (window as unknown as { __testWallet: TestExtension }).__testWallet.mode = 'wait' })
    await dialog.getByRole('button', { name: 'Sign & submit' }).click()
    await expect(dialog).toContainText('Waiting for E2E Phantom compatible')
    if (change === 'expiry') {
      await page.clock.install()
      await page.clock.fastForward(61_000)
    } else if (change === 'lock') {
      // Model a lock that lands while the signing prompt owns focus. The action
      // remains in the full-height wallet panel.
      const lock = page.getByRole('button', { name: 'Sign out of wallet', exact: true })
      await lock.evaluate((button) => (button as HTMLButtonElement).click())
    } else {
      await page.evaluate((change) => {
        const wallet = (window as unknown as { __testWallet: TestExtension }).__testWallet
        wallet.change(change === 'account' ? { accounts: [] } : { chains: ['solana:devnet'] })
      }, change)
    }
    await page.evaluate(async () => (window as unknown as { __testWallet: TestExtension }).__testWallet.release())
    if (change === 'expiry') {
      await expect(dialog).toContainText('Quote expired or wallet, order or position changed')
    } else {
      await expect(page.getByRole('heading', { name: 'Understand one strategy before risking funds.' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Connect wallet', exact: true })).toBeVisible()
      await expect(dialog).toHaveCount(0)
    }
    expect((await syntheticControl(page)).sent).toBe(0)
  })
}

test('recovers a lost HTTP submission response and shows a finalized fee-only failure', async ({ page }) => {
  await createAndEnter(page)
  const bank = await syntheticControl(page, { failed: true })
  const dialog = await prepareBuy(page, bank.mint)
  await page.route('**/api/v1/trades/submit', async (route) => {
    await route.fetch()
    await route.abort('connectionreset')
  })
  await dialog.getByRole('button', { name: 'Sign & submit' }).click()
  await expect(dialog).toContainText('Submission outcome must be checked')
  await expect(dialog.locator('.receipt-card')).toContainText('Pending')
  await expect(dialog.getByRole('button', { name: 'Review order' })).toBeDisabled()
  await syntheticControl(page, { finalize: true })
  await dialog.getByRole('button', { name: 'Refresh status' }).click()
  await expect(dialog.locator('.receipt-card')).toContainText('BUY · Failed')
  await expect(dialog.locator('.receipt-card')).toContainText('−0.000005 SOL')
  await expect(dialog).toContainText('No open positions')
  expect(await syntheticControl(page)).toMatchObject({ sent: 1, balance: '1249995000', tokens: '0' })
})

test('reviews sparse session evidence and creates a confirmed inactive successor at 390px', async ({ page }) => {
  await createAndEnter(page)
  const source = await createReviewedSession(page)
  await expect(page.locator(`[data-session-id="${source.id}"] .rail-row`)).toBeVisible({ timeout: 15_000 })
  const sourceBefore = await page.evaluate(async (id) => (await (await fetch(`/api/v1/sessions/${id}`)).json()).instance, source.id)
  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  const monitor = page.getByRole('dialog', { name: 'Session monitor' })
  await expect(monitor).toContainText('Reviewed browser fixture')
  await page.setViewportSize({ width: 390, height: 844 })

  let failOnce = true
  await page.route('**/api/v1/research/sessions/*', async (route) => {
    await route.fulfill({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: 'Batch 3 route unavailable in legacy fixture' }) })
  })
  await page.route('**/api/v1/sessions/*/reviews', async (route) => {
    if (failOnce && route.request().method() === 'POST') {
      failOnce = false
      await route.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ error: 'Synthetic retryable comparison failure' }) })
      return
    }
    await route.continue()
  })
  await monitor.getByRole('button', { name: 'Review session evidence' }).click()
  let review = page.getByRole('dialog', { name: 'Session review' })
  const compare = review.getByRole('button', { name: 'Freeze & compare' })
  await compare.focus()
  await page.keyboard.press('Enter')
  await expect(review.getByRole('alert')).toContainText('Synthetic retryable comparison failure')
  await review.getByRole('button', { name: 'Freeze & compare' }).click()
  await expect(review).toContainText('Collect more evidence')
  await expect(review).toContainText('Historical evidence contains transition records')
  await expect(review).toContainText('Explicit round-trip assumptions')
  await expect(review.getByRole('row', { name: /Current strategy/ })).toBeVisible()
  await expect(review.getByRole('row', { name: /No-entry baseline/ })).toBeVisible()
  expect(await review.evaluate((node) => node.getBoundingClientRect().width <= window.innerWidth)).toBe(true)

  const suggestion = await page.evaluate(async (id) => {
    const reports = await (await fetch(`/api/v1/sessions/${id}/reviews`)).json()
    const response = await fetch('/api/v1/test-session-review-suggestion', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ report_id: reports.reports[0].id }),
    })
    if (!response.ok) throw new Error(await response.text())
    return response.json() as Promise<{ report_id: string; candidate_id: string }>
  }, source.id)
  expect(suggestion.candidate_id).toMatch(/^candidate-/)
  await review.getByRole('button', { name: 'Done' }).click()
  await monitor.getByRole('button', { name: 'Review session evidence' }).click()
  review = page.getByRole('dialog', { name: 'Session review' })
  await expect(review).toContainText('Bounded candidate to review')
  await review.getByRole('button', { name: 'Review draft action' }).click()
  const confirmation = review.getByRole('alertdialog', { name: 'Confirm inactive successor draft' })
  await expect(confirmation).toContainText('paused session with monitoring off')
  await expect(confirmation).toContainText('zero wallet allocation')
  await confirmation.getByRole('button', { name: 'Create inactive draft' }).click()

  const successorMonitor = page.getByRole('dialog', { name: 'Session monitor' })
  await expect(successorMonitor).toContainText('Reviewed browser fixture review draft')
  await expect(successorMonitor).toContainText('Stopped')
  const state = await page.evaluate(async ({ sourceId }) => {
    const sessions = await (await fetch('/api/v1/sessions')).json()
    const successor = sessions.rows.find((row: { instance: { label: string } }) => row.instance.label.endsWith('review draft'))
    return {
      source: (await (await fetch(`/api/v1/sessions/${sourceId}`)).json()).instance,
      successor,
    }
  }, { sourceId: source.id })
  expect(state.source).toEqual(sourceBefore)
  expect(state.successor.instance).toMatchObject({ status: 'paused', entry_mode: 'disabled', exit_mode: 'manual' })
  expect(state.successor.tracking.enabled).toBe(0)
  expect(state.successor.management).toBeUndefined()
  const successorDetail = await page.evaluate(async (id) => await (await fetch(`/api/v1/sessions/${id}`)).json(), state.successor.instance.id)
  expect(successorDetail.management.allocation_lamports).toBe('0')
})

test('real Batch 4 research service preserves queue, exposure, quality and browser-lost starts', async ({ browser, context, page }, testInfo) => {
  test.setTimeout(90_000)
  await createAndEnter(page)
  const source = await createReviewedSession(page)
  const fixture = await page.evaluate(async (session_id) => {
    const response = await fetch('/api/v1/test-research-batch3-fixture', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id }),
    })
    if (!response.ok) throw new Error(await response.text())
    return response.json() as Promise<{
      schema: string
      observations: number
      collection_runs: number
      wallet_authority: boolean
      signer_calls: number
      submission_calls: number
    }>
  }, source.id)
  expect(fixture).toMatchObject({
    schema: 'pumpwizard.research-browser-fixture/v1',
    wallet_authority: false,
    signer_calls: 0,
    submission_calls: 0,
  })
  expect(fixture.observations).toBeGreaterThanOrEqual(110)
  expect(fixture.collection_runs).toBe(fixture.observations)

  let signingOrSubmissionRequests = 0
  context.on('request', (request) => {
    if (request.method() === 'POST' && /(?:signing|trades\/submit)/.test(new URL(request.url()).pathname)) signingOrSubmissionRequests += 1
  })
  await expect(page.locator(`[data-session-id="${source.id}"] .rail-row`)).toBeVisible({ timeout: 15_000 })
  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  let monitor = page.getByRole('dialog', { name: 'Session monitor' })
  await monitor.getByRole('button', { name: 'Review session evidence' }).click()
  let workspace = page.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await expect(workspace).toContainText('No frozen evidence yet')
  await workspace.getByRole('button', { name: 'Freeze current evidence' }).click()
  await expect(workspace).toContainText(`${fixture.observations} independent observations`, { timeout: 15_000 })
  await expect(workspace).toContainText('Eligible for declared comparison policy')
  const queuedResponse = page.waitForResponse((response) =>
    response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/v1/research/jobs')
  await workspace.getByRole('button', { name: 'Start background comparison' }).click()
  expect(['queued', 'running']).toContain((await (await queuedResponse).json()).job.state)

  // The only browser page closes while the service owns the job. A new tab has
  // to unlock locally, then reopens the exact persisted source and result.
  await page.close()
  page = await context.newPage()
  await page.goto('/')
  await unlockAndEnter(page)
  await expect(page.locator(`[data-session-id="${source.id}"] .rail-row`)).toBeVisible({ timeout: 15_000 })
  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  monitor = page.getByRole('dialog', { name: 'Session monitor' })
  await monitor.getByRole('button', { name: 'Review session evidence' }).click()
  workspace = page.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await expect(workspace.getByRole('region', { name: 'Comparison job completed' })).toBeVisible({ timeout: 20_000 })
  await workspace.getByRole('button', { name: 'results', exact: true }).click()
  await expect(workspace.getByRole('table')).toContainText('Cash')
  await expect(workspace).toContainText('No wallet allocation, signing authority')
  const downloadPromise = page.waitForEvent('download')
  await workspace.getByRole('link', { name: 'Export JSON' }).click()
  expect((await downloadPromise).suggestedFilename()).toMatch(/^[a-f0-9]{64}\.json$/)

  await workspace.getByRole('button', { name: 'history', exact: true }).click()
  await expect(workspace).toContainText('Holdout exposed')
  await expect(workspace).toContainText('cannot be reused as unseen evidence')

  await workspace.getByRole('button', { name: 'Forward paper' }).click()
  await workspace.getByRole('button', { name: 'Review paper start' }).click()
  await workspace.getByRole('button', { name: 'START PAPER RESEARCH' }).click()
  await expect(workspace.getByText('RUNNING PAPER RUN')).toBeVisible({ timeout: 15_000 })
  const paperRunId = await page.evaluate(async (sessionId) => {
    const response = await fetch(`/api/v1/research/sessions/${sessionId}`)
    if (!response.ok) throw new Error(await response.text())
    return (await response.json()).paper_runs[0].id as string
  }, source.id)
  const baseUrl = new URL(page.url()).origin
  const storedDashboard = await context.storageState()
  const observer = await playwrightRequest.newContext({
    baseURL: baseUrl,
    storageState: storedDashboard,
    extraHTTPHeaders: { Origin: baseUrl },
  })
  await context.close()
  const qualityResponse = await observer.post('/api/v1/test-research-batch4-quality-fixture', {
    data: { session_id: source.id },
  })
  expect(qualityResponse.ok()).toBe(true)
  await expect.poll(async () => {
    const response = await observer.get(`/api/v1/research/paper-runs/${paperRunId}/history?limit=1000`)
    if (!response.ok()) return []
    const history = await response.json()
    return history.observations.flatMap((row: { quality?: { reasons?: string[] } }) => row.quality?.reasons || [])
  }, { timeout: 15_000 }).toContain('unsupported-price-precision')
  await observer.dispose()

  const reopenedContext = await browser.newContext({ storageState: storedDashboard })
  page = await reopenedContext.newPage()
  await page.goto('/')
  await unlockAndEnter(page)
  await expect(page.locator(`[data-session-id="${source.id}"] .rail-row`)).toBeVisible({ timeout: 15_000 })
  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  monitor = page.getByRole('dialog', { name: 'Session monitor' })
  await monitor.getByRole('button', { name: 'Review session evidence' }).click()
  workspace = page.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await workspace.getByRole('button', { name: 'Forward paper' }).click()
  await expect(workspace.getByText('RUNNING PAPER RUN')).toBeVisible({ timeout: 15_000 })
  await expect(workspace).toContainText('Worker health')
  await expect(workspace).toContainText('healthy · retry 0/3')
  await expect(workspace.getByText(/\d+ independent · \d+ excluded/)).toBeVisible()
  await workspace.getByRole('button', { name: 'Inspect durable decisions and outcomes' }).click()
  await expect(workspace).toContainText(/Durable decision and outcome history|No durable decisions have been recorded/)
  await expect(workspace).toContainText('unsupported-price-precision')
  await expect(workspace).toContainText('below the supported 1e-18 quote-per-token precision')
  await workspace.screenshot({ path: testInfo.outputPath('research-paper-quality-closed-browser.png') })
  await workspace.getByRole('button', { name: 'Pause', exact: true }).click()
  await expect(workspace.getByText('PAUSED PAPER RUN')).toBeVisible()
  await workspace.getByRole('button', { name: 'Resume same run' }).click()
  await expect(workspace.getByText('RUNNING PAPER RUN')).toBeVisible()
  await workspace.getByRole('button', { name: 'Stop', exact: true }).click()
  await expect(workspace.getByText('STOPPED PAPER RUN')).toBeVisible()
  const queueFixture = await page.evaluate(async (session_id) => {
    const response = await fetch('/api/v1/test-research-batch4-queue-fixture', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id }),
    })
    if (!response.ok) throw new Error(await response.text())
    return response.json()
  }, source.id)
  expect(queueFixture).toMatchObject({
    schema: 'pumpwizard.research-browser-queue-fixture/v1', wallet_authority: false,
  })
  const queueResult = await page.evaluate(async (fixture) => {
    const deadline = Date.now() + 20_000
    while (Date.now() < deadline) {
      const blocked = (await (await fetch(`/api/v1/research/jobs/${fixture.blocked_job_id}`)).json()).job
      const valid = (await (await fetch(`/api/v1/research/jobs/${fixture.valid_job_id}`)).json()).job
      if (blocked.state === 'failed' && valid.state === 'completed') {
        const result = (await (await fetch(`/api/v1/research/jobs/${fixture.valid_job_id}/result`)).json()).result
        return { blocked, valid, result }
      }
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
    throw new Error('Timed out waiting for the exact blocked and valid queue jobs')
  }, queueFixture)
  expect(queueResult.blocked.error_code).toBe('holdout_conflict')
  expect(queueResult.valid.error).toBe('')
  expect(queueResult.result.holdout_state).toBe('not_evaluated')
  expect(queueResult.result.detailed_comparison.holdout_results).toEqual([])
  expect(signingOrSubmissionRequests).toBe(0)
  await reopenedContext.close()
})

test('real forward campaign survives the closed browser and exposes report, quota alert, export and stop', async ({ browser, context, page }, testInfo) => {
  test.setTimeout(90_000)
  await createAndEnter(page)
  const source = await createReviewedSession(page)
  await expect(page.locator(`[data-session-id="${source.id}"] .rail-row`)).toBeVisible({ timeout: 15_000 })
  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  let monitor = page.getByRole('dialog', { name: 'Session monitor' })
  await monitor.getByRole('button', { name: 'Review session evidence' }).click()
  let workspace = page.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await workspace.getByRole('button', { name: 'Campaigns', exact: true }).click()
  await expect(workspace.getByRole('heading', { name: 'Configure a fair observation campaign' })).toBeVisible()
  await workspace.getByLabel('Request ceiling').fill('1')
  await workspace.getByRole('button', { name: 'Review exact campaign' }).click()
  const startResponse = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/v1/research/campaigns')
  await workspace.getByRole('button', { name: 'START FORWARD CAMPAIGN' }).click()
  const started = await (await startResponse).json() as { campaign: { id: string; authority: Record<string, boolean> } }
  expect(Object.values(started.campaign.authority).every((value) => value === false)).toBe(true)

  // Close the complete browser context immediately after Start. The one
  // background service, not any page or context, owns collection and durable
  // quota enforcement.
  const storedDashboard = await context.storageState()
  await context.close()
  await new Promise((resolve) => setTimeout(resolve, 1_000))
  const reopenedContext = await browser.newContext({ storageState: storedDashboard })
  page = await reopenedContext.newPage()
  await page.goto('/')
  await unlockAndEnter(page)
  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  monitor = page.getByRole('dialog', { name: 'Session monitor' })
  await monitor.getByRole('button', { name: 'Review session evidence' }).click()
  workspace = page.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await workspace.getByRole('button', { name: 'Campaigns', exact: true }).click()
  await expect(workspace.getByText('CAMPAIGN CAPPED')).toBeVisible({ timeout: 20_000 })
  await expect(workspace).toContainText('Provider request quota exhausted')
  await expect(workspace).toContainText('Observation only')

  await workspace.getByRole('button', { name: 'performance', exact: true }).click()
  await workspace.getByRole('button', { name: 'Save report snapshot' }).click()
  await expect(workspace).toContainText('REPRODUCIBLE CAMPAIGN ARTIFACTS', { timeout: 15_000 })
  await expect(workspace.getByRole('table', { name: /Matched backend results/ })).toBeVisible()
  await expect(workspace.getByRole('row', { name: /Cash/ })).toBeVisible()
  await expect(workspace).toContainText('Unknown provider/other costs remain')
  await expect(workspace.getByText('Operator guide: interpreting and extending this observation')).toBeVisible()
  const download = page.waitForEvent('download')
  await workspace.getByRole('link', { name: 'Export JSON' }).click()
  expect((await download).suggestedFilename()).toMatch(/^campaign-report-[a-f0-9]{32}\.json$/)

  await workspace.getByRole('button', { name: 'receipts', exact: true }).click()
  await expect(workspace).toContainText('Deposits, withdrawals and transferred inventory are external flows—not strategy P/L.')
  await expect(workspace).toContainText('MODELED PAPER')
  await expect(workspace).toContainText('RECORDED EXECUTION')
  await workspace.getByRole('button', { name: /alerts/i, exact: false }).click()
  await expect(workspace).toContainText('Campaign resource quota is exhausted')
  await workspace.getByRole('button', { name: 'Acknowledge review' }).click()
  await expect(workspace).toContainText('Acknowledged; condition remains active')
  await workspace.screenshot({ path: testInfo.outputPath('forward-campaign-closed-browser.png') })

  await workspace.getByRole('link', { name: 'Inspect provider' }).click()
  await expect(workspace.getByText('CAMPAIGN CAPPED')).toBeVisible()
  await workspace.getByRole('button', { name: 'Review stop' }).click()
  await workspace.getByLabel('Type campaign ID to stop').fill(started.campaign.id)
  await workspace.getByRole('button', { name: new RegExp(`Stop ${started.campaign.id.slice(0, 10)}`) }).click()
  await expect(workspace.getByText('CAMPAIGN STOPPED')).toBeVisible()
  await reopenedContext.close()
})

test('forward campaign controls survive a service restart and require explicit resume', async ({ context, page }) => {
  test.setTimeout(90_000)
  await createAndEnter(page)
  const source = await createReviewedSession(page)
  let signingOrSubmissionRequests = 0
  context.on('request', (request) => {
    if (request.method() === 'POST' && /(?:signing|trades\/submit)/.test(new URL(request.url()).pathname)) signingOrSubmissionRequests += 1
  })
  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  let monitor = page.getByRole('dialog', { name: 'Session monitor' })
  await monitor.getByRole('button', { name: 'Review session evidence' }).click()
  let workspace = page.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await workspace.getByRole('button', { name: 'Campaigns', exact: true }).click()
  await workspace.getByRole('button', { name: 'Review exact campaign' }).click()
  const startResponse = page.waitForResponse((response) => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/v1/research/campaigns')
  await workspace.getByRole('button', { name: 'START FORWARD CAMPAIGN' }).click()
  const started = await (await startResponse).json() as { campaign: { id: string } }
  await expect(workspace.getByText('CAMPAIGN RUNNING')).toBeVisible({ timeout: 20_000 })

  await workspace.getByRole('button', { name: 'Pause campaign' }).click()
  await expect(workspace.getByText('CAMPAIGN PAUSED')).toBeVisible()
  const readOnlyState = await page.evaluate(async (campaignId) => {
    const read = async () => {
      const envelope = await (await fetch('/api/v1/research/campaigns?limit=100')).json()
      return envelope.campaigns.find((campaign: { id: string }) => campaign.id === campaignId)
    }
    return { before: await read(), after: await read() }
  }, started.campaign.id)
  expect(readOnlyState.after.progress.events).toBe(readOnlyState.before.progress.events)
  expect(readOnlyState.after.request_count).toBe(readOnlyState.before.request_count)

  // A second view discovers the same durable campaign. It does not create or
  // resume anything merely by loading and polling.
  const observer = await context.newPage()
  await observer.goto('/')
  await unlockAndEnter(observer)
  await observer.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(observer)
  const observerMonitor = observer.getByRole('dialog', { name: 'Session monitor' })
  await observerMonitor.getByRole('button', { name: 'Review session evidence' }).click()
  const observerWorkspace = observer.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await observerWorkspace.getByRole('button', { name: 'Campaigns', exact: true }).click()
  await expect(observerWorkspace.getByText('CAMPAIGN PAUSED')).toBeVisible()
  const campaignIds = await observer.evaluate(async (sessionId) => {
    const envelope = await (await fetch('/api/v1/research/campaigns?limit=100')).json()
    return envelope.campaigns
      .filter((campaign: { arms: { source_session_id: string }[] }) => campaign.arms.some((arm) => arm.source_session_id === sessionId))
      .map((campaign: { id: string }) => campaign.id)
  }, source.id)
  expect(campaignIds).toEqual([started.campaign.id])
  await observer.close()

  await workspace.getByRole('button', { name: 'Resume same campaign' }).click()
  await expect(workspace.getByText('CAMPAIGN RUNNING')).toBeVisible()
  const beforeRestart = await syntheticControl(page)
  await page.evaluate(async () => {
    await fetch('/api/v1/test-restart', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
  })
  await expect.poll(async () => {
    try {
      return (await syntheticControl(page)).process_id !== beforeRestart.process_id
    } catch {
      return false
    }
  }, { timeout: 15_000 }).toBe(true)
  await page.reload()
  await unlockAndEnter(page)
  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  monitor = page.getByRole('dialog', { name: 'Session monitor' })
  await monitor.getByRole('button', { name: 'Review session evidence' }).click()
  workspace = page.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await workspace.getByRole('button', { name: 'Campaigns', exact: true }).click()
  await expect(workspace.getByText('CAMPAIGN PAUSED')).toBeVisible()
  await expect(workspace).toContainText('A service restart did not silently resume collection')
  await workspace.getByRole('button', { name: 'Resume same campaign' }).click()
  await expect(workspace.getByText('CAMPAIGN RUNNING')).toBeVisible()
  await workspace.getByRole('button', { name: 'Review stop' }).click()
  await workspace.getByLabel('Type campaign ID to stop').fill(started.campaign.id)
  await workspace.getByRole('button', { name: new RegExp(`Stop ${started.campaign.id.slice(0, 10)}`) }).click()
  await expect(workspace.getByText('CAMPAIGN STOPPED')).toBeVisible()
  expect(signingOrSubmissionRequests).toBe(0)
})

test('campaign setup remains narrow-screen and keyboard usable', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await createAndEnter(page)
  const source = await createReviewedSession(page)
  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  const monitor = page.getByRole('dialog', { name: 'Session monitor' })
  await monitor.getByRole('button', { name: 'Review session evidence' }).click()
  const workspace = page.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await workspace.getByRole('button', { name: 'Campaigns', exact: true }).click()
  await expect.poll(() => workspace.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true)
  const review = workspace.getByRole('button', { name: 'Review exact campaign' })
  await review.focus()
  await expect(review).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(workspace.getByRole('heading', { name: 'Confirm one bounded forward campaign' })).toBeVisible()
  const start = workspace.getByRole('button', { name: 'START FORWARD CAMPAIGN' })
  await start.focus()
  await expect(start).toBeFocused()
  await workspace.screenshot({ path: testInfo.outputPath('campaign-setup-390px.png') })
})

test('campaign Start retries a lost response with the exact identity', async ({ page }) => {
  test.setTimeout(60_000)
  await createAndEnter(page)
  const source = await createReviewedSession(page)
  const starts: { idempotency_key: string; campaign: { campaign_key: string } }[] = []
  let loseFirstResponse = true
  await page.route('**/api/v1/research/campaigns', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue()
      return
    }
    starts.push(route.request().postDataJSON())
    const response = await route.fetch()
    if (loseFirstResponse) {
      loseFirstResponse = false
      await route.abort('failed')
      return
    }
    await route.fulfill({ response })
  })
  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  const monitor = page.getByRole('dialog', { name: 'Session monitor' })
  await monitor.getByRole('button', { name: 'Review session evidence' }).click()
  const workspace = page.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await workspace.getByRole('button', { name: 'Campaigns', exact: true }).click()
  await workspace.getByRole('button', { name: 'Review exact campaign' }).click()
  await workspace.getByRole('button', { name: 'START FORWARD CAMPAIGN' }).click()
  const retry = workspace.getByRole('button', { name: 'Retry exact Start' })
  await expect(retry).toBeVisible()
  await retry.click()
  await expect(workspace.getByText('CAMPAIGN RUNNING')).toBeVisible()
  expect(starts).toHaveLength(2)
  expect(starts[1].idempotency_key).toBe(starts[0].idempotency_key)
  expect(starts[1].campaign.campaign_key).toBe(starts[0].campaign.campaign_key)
  const matching = await page.evaluate(async (sessionId) => {
    const envelope = await (await fetch('/api/v1/research/campaigns?limit=100')).json()
    return envelope.campaigns.filter((campaign: { arms: { source_session_id: string }[] }) => (
      campaign.arms.some((arm) => arm.source_session_id === sessionId)
    ))
  }, source.id)
  expect(matching).toHaveLength(1)
  const cleanup = await page.evaluate(async (campaignId) => fetch(`/api/v1/research/campaigns/${campaignId}/stop`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
      schema: 'pumpwizard.forward-campaign-command/v1', campaign_id: campaignId,
      command: 'stop', idempotency_key: `browser-cleanup-${campaignId}`, confirmation: campaignId,
    }),
  }).then((response) => response.ok), matching[0].id)
  expect(cleanup).toBe(true)
})

test('campaign dashboard keeps gaps, losing-to-cash and unresolved receipts visible', async ({ page }, testInfo) => {
  await createAndEnter(page)
  const source = await createReviewedSession(page)
  const sourceIdentity = await page.evaluate(async (sessionId) => {
    const envelope = await (await fetch('/api/v1/sessions')).json()
    return envelope.rows.find((row: { instance: { id: string } }) => row.instance.id === sessionId).instance
  }, source.id)
  const now = Math.floor(Date.now() / 1_000)
  const campaignId = 'campaign-quality-fixture'
  const campaign = {
    schema: 'pumpwizard.forward-campaign/v1', id: campaignId, campaign_key: 'quality-fixture',
    supersedes_campaign_id: null, request_sha256: 'b'.repeat(64), state: 'paused',
    created_at: now - 300, start_at: now - 300, updated_at: now - 20, stopped_at: null,
    duration_seconds: 86_400, next_scan_at: now + 15, request_count: 3, blocker: null,
    source: { provider: 'CompositePumpMarketSource', minimum_cap: 1_000, maximum_cap: 50_000, priority_mints: [], limit: 24, cadence_seconds: 15 },
    assumptions: { cost_model: 'declared strategy costs', execution_model: 'paper replay v3' },
    coverage: { max_source_age_seconds: 120, minimum_independent_tokens: 20, minimum_observations: 100, maximum_gap_seconds: 60 },
    quotas: { max_requests: 100, max_active_runtime_seconds: 86_400, max_storage_bytes: 1_000_000, max_concurrency: 1 },
    provider_cost: { currency: 'USD', marginal_cost_per_request: null, fixed_subscription_cost: null, price_basis: 'unknown', cost_status: 'unknown' },
    authority: { wallet_allocation: false, signer: false, orders: false, submission: false },
    progress: { planned_duration_seconds: 86_400, active_seconds: 190, wall_seconds: 280, events: 4, reports: 1 },
    usage: { requests: 3, response_bytes: 12_000, errors: 1, timeout_events: 1, rate_limit_events: 0, maximum_source_age_seconds: 300, last_request_at: now - 300 },
    operating_intervals: [
      { id: 'interval-1', started_at: now - 300, ended_at: now - 200, reason: 'campaign-start' },
      { id: 'interval-2', started_at: now - 170, ended_at: now - 80, reason: 'campaign-resume' },
    ],
    arms: [{
      id: 'arm-losing', source_session_id: source.id, strategy_sha256: sourceIdentity.strategy_digest,
      simulated_capital_usd: 1_000, paper_run_id: 'paper-losing', state: 'paused',
      cursor: [now - 300, 'evidence-1'], counts: { observations: 12, decisions: 4 }, fault: null,
    }],
  }
  const comparison = (overrides: Record<string, unknown>) => ({
    id: 'cash', kind: 'cash', label: 'Cash', arm_id: null, comparable: true,
    comparability_reason: null, starting_capital_usd: 1_000, ending_capital_usd: 1_000,
    modeled_net_pnl_usd: 0, modeled_realized_pnl_usd: 0, modeled_unrealized_pnl_usd: 0,
    maximum_drawdown_fraction: 0, exposure_fraction: 0, turnover_usd: 0, opportunities: 4,
    independent_tokens: 12, coverage_fraction: 1, operating_seconds: 190,
    unpriced_positions: 0, censored_positions: 0, known_costs_usd: 0,
    estimated_costs_usd: null, cost_status: 'unknown',
    curve: [{ at: now - 300, equity_usd: 1_000, segment_id: 'segment-1', gap_reason: null }, { at: now - 80, equity_usd: 1_000, segment_id: 'segment-2', gap_reason: null }],
    ...overrides,
  })
  const report = {
    schema: 'pumpwizard.forward-performance-report/v1', id: 'report-quality-fixture', campaign_id: campaignId,
    manifest_digest: campaign.request_sha256, previous_report_id: null, created_at: now, as_of: now,
    actual_period: { first_at: now - 300, last_at: now - 80, operating_seconds: 190 },
    model_label: 'Prospective modeled paper evidence; no funded fills', engine_versions: ['paper-replay/v3'],
    readiness: { state: 'not_ready', reasons: ['arm_coverage_mismatch', 'provider_cost_unknown', 'unpriced_positions'] },
    ranking: { comparison_id: 'cash', label: 'Cash remains the matched benchmark leader', reason: 'The strategy arm lost to cash on matched retained evidence.', fully_costed: false },
    comparisons: [
      comparison({}),
      comparison({ id: 'current', kind: 'current', label: 'Current strategy', ending_capital_usd: 950, modeled_net_pnl_usd: -50 }),
      comparison({ id: 'arm-losing', kind: 'arm', label: 'Losing paper arm', arm_id: 'arm-losing', comparable: false, comparability_reason: 'Arm uptime does not match the campaign window.', ending_capital_usd: 900, modeled_net_pnl_usd: -100, coverage_fraction: 0.5, operating_seconds: 95, unpriced_positions: 1 }),
      comparison({ id: 'holding', kind: 'holding', label: 'Declared holding benchmark', ending_capital_usd: null, modeled_net_pnl_usd: null, comparable: false, comparability_reason: 'One holding has no current mark.', unpriced_positions: 1 }),
    ],
    daily: [{ id: 'day-1', label: 'Fixture day', first_at: now - 300, last_at: now - 80, comparison_ids: ['cash', 'current', 'arm-losing', 'holding'], gaps: 1 }],
    gaps: [{ id: 'gap-1', started_at: now - 200, ended_at: now - 170, reason: 'service_restart', detail: 'No evidence was backfilled across restart.' }],
    limitations: ['Provider price is unknown.', 'One holding is unpriced.', 'Arm uptime is uneven.'],
    receipts: [
      { id: 'paper-receipt', evidence_class: 'modeled_paper', classification: 'finalized_success', record_kind: 'modeled fill', source_label: 'Paper run', occurred_at: now - 100, ownership: 'paper arm', cash_delta: '-100 USD', token_delta: '+10 tokens', fee: '1 USD modeled', discrepancy: null, source_href: '/research/paper/paper-losing' },
      { id: 'historical-receipt', evidence_class: 'retrospective', classification: 'finalized_success', record_kind: 'replay fill', source_label: 'Historical replay', occurred_at: now - 90, ownership: 'retrospective scenario', cash_delta: '-80 USD', token_delta: '+8 tokens', fee: 'estimated', discrepancy: null, source_href: '/research/replay/history' },
      { id: 'recorded-receipt', evidence_class: 'recorded_execution', classification: 'unresolved', record_kind: 'network receipt', source_label: 'Trade journal', occurred_at: now - 85, ownership: 'wallet record', cash_delta: '+500 USD external deposit', token_delta: null, fee: null, discrepancy: 'Cash conservation mismatch remains unresolved.', source_href: '/trades/recorded-receipt' },
    ],
    reconciliation: { status: 'mismatch', external_cash_flow: '+500 USD deposit', unresolved_count: 1, reasons: ['cash_does_not_conserve'] },
  }
  const alert = {
    schema: 'pumpwizard.evidence-alert/v1', id: 'alert-accounting', campaign_id: campaignId,
    kind: 'accounting-mismatch', severity: 'critical', state: 'open', title: 'Accounting reconciliation mismatch',
    message: 'Recorded cash and retained receipt evidence do not reconcile.', repair: 'Inspect the source-linked receipt.',
    source: { type: 'accounting', kind: 'accounting', id: 'recorded-receipt', href: '/research/campaigns/campaign-quality-fixture/receipts/recorded-receipt' },
    first_occurred_at: now - 20, last_occurred_at: now - 20, occurrence_count: 1, recurrence_count: 0,
    occurrence_generation: 1, acknowledged_at: null, resolved_at: null, local_only: true, external_delivery: false,
  }
  await page.route('**/api/v1/research/campaigns**', async (route) => {
    const url = new URL(route.request().url())
    if (route.request().method() === 'GET' && url.pathname.endsWith('/reports')) {
      await route.fulfill({ json: { campaign_reports: [report] } })
    } else if (route.request().method() === 'GET' && url.pathname === '/api/v1/research/campaigns') {
      await route.fulfill({ json: { campaigns: [campaign] } })
    } else {
      await route.continue()
    }
  })
  await page.route('**/api/v1/research/alerts**', (route) => route.fulfill({ json: { alerts: [alert] } }))
  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  const monitor = page.getByRole('dialog', { name: 'Session monitor' })
  await monitor.getByRole('button', { name: 'Review session evidence' }).click()
  const workspace = page.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await workspace.getByRole('button', { name: 'Campaigns', exact: true }).click()
  await expect(workspace).toContainText('Not priced as free')
  await expect(workspace).toContainText('Campaign collection paused')
  await expect(workspace).toContainText('5 minutes source age')
  await workspace.getByRole('button', { name: 'performance', exact: true }).click()
  await expect(workspace).toContainText('Cash remains the matched benchmark leader')
  await expect(workspace).toContainText('Arm uptime does not match the campaign window.')
  await expect(workspace).toContainText('One holding has no current mark.')
  await expect(workspace).toContainText('service_restart')
  await workspace.getByRole('button', { name: 'receipts', exact: true }).click()
  await expect(workspace).toContainText('+500 USD deposit')
  await expect(workspace).toContainText('Cash conservation mismatch remains unresolved.')
  await expect(workspace).toContainText('external flows—not strategy P/L')
  await workspace.getByRole('button', { name: /alerts/i }).click()
  await workspace.getByRole('link', { name: 'Inspect accounting' }).click()
  await expect(workspace).toContainText('RECORDED EXECUTION')
  await workspace.screenshot({ path: testInfo.outputPath('campaign-quality-and-receipts.png') })
})

test('research retries a lost paper-start response, rotates explicit new-run identity, and rejects late refresh state', async ({ context, page }) => {
  await createAndEnter(page)
  const source = await createReviewedSession(page)
  const now = Math.floor(Date.now() / 1_000)
  let independentObservations = 10
  let paperState: 'running' | 'stopped' | null = null
  let paperId = ''
  const startKeys: string[] = []
  let abortFirstStart = true
  let reorder = false
  let reorderedGets = 0
  let releaseFirstRefresh = () => undefined
  let markFirstRefreshSeen = () => undefined
  let markFirstRefreshFulfilled = () => undefined
  const firstRefreshGate = new Promise<void>((resolve) => { releaseFirstRefresh = resolve })
  const firstRefreshSeen = new Promise<void>((resolve) => { markFirstRefreshSeen = resolve })
  const firstRefreshFulfilled = new Promise<void>((resolve) => { markFirstRefreshFulfilled = resolve })
  const snapshot = () => ({
    schema: 'pumpwizard.research-workspace/v1',
    paper_research_allowed: true,
    source_session_id: source.id,
    source_label: 'Reviewed browser fixture',
    dataset_preview: null,
    datasets: [{
      schema: 'pumpwizard.research-dataset/v1', id: 'retry-dataset', content_sha256: 'd'.repeat(64),
      source_session_id: source.id, source_strategy_sha256: 'a'.repeat(64), created_at: now,
      event_range: { first_at: now - 60, last_at: now - 1 },
      first_known_range: { first_at: now - 59, last_at: now, basis: 'recorder-first-receipt' },
      source_age_seconds: 1,
      counts: { observations: independentObservations, decisions: 0, outcomes: 0, independent_observations: independentObservations, unknown_first_known: 0 },
      quality: {}, gaps: [], supported_family: true, family: 'ladder-v1', eligible: true, blockers: [],
      evidence_policy: 'Prospective fixture with explicit first-known time.',
    }],
    experiments: [], attempts: [], jobs: [], reports: [],
    paper_runs: paperState === null ? [] : [{
      schema: 'pumpwizard.paper-run/v1', id: paperId, source_session_id: source.id,
      strategy_sha256: 'a'.repeat(64), state: paperState,
      starting_capital_usd: 1_000, simulated_capital_usd: 1_000,
      started_at: now, first_known_at: null, updated_at: now,
      decisions: 0, scored_outcomes: 0, unknown_outcomes: 0,
      independent_observations: 0, processing_state: 'healthy',
      retry: { count: 0, maximum: 3, next_at: null }, gaps: [],
      assumptions: ['No wallet allocation, signer or submission authority.'],
    }],
  })
  await context.route('**/api/v1/research/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (request.method() === 'GET') {
      const response = JSON.stringify(snapshot())
      if (reorder && ++reorderedGets === 1) {
        markFirstRefreshSeen()
        await firstRefreshGate
        await route.fulfill({ status: 200, contentType: 'application/json', body: response })
        markFirstRefreshFulfilled()
        return
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: response })
      return
    }
    const body = request.postDataJSON() as { idempotency_key: string }
    if (path === '/api/v1/research/paper-runs') {
      startKeys.push(body.idempotency_key)
      paperId ||= 'paper-retry-fixture'
      paperState = 'running'
      if (abortFirstStart) {
        abortFirstStart = false
        await route.abort('failed')
        return
      }
    } else if (path.endsWith('/stop')) {
      paperState = 'stopped'
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ notice: 'Accepted without execution authority.' }) })
  })

  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  await page.getByRole('dialog', { name: 'Session monitor' }).getByRole('button', { name: 'Review session evidence' }).click()
  const workspace = page.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await workspace.getByRole('button', { name: 'Forward paper' }).click()
  const storageFailureInstalled = await page.evaluate(async () => {
    try {
      Object.defineProperties(window.sessionStorage, {
        getItem: { configurable: true, value: () => { throw new Error('blocked') } },
        setItem: { configurable: true, value: () => { throw new Error('blocked') } },
        removeItem: { configurable: true, value: () => { throw new Error('blocked') } },
      })
      window.sessionStorage.getItem('probe')
      return false
    } catch {
      return true
    }
  })
  expect(storageFailureInstalled).toBe(true)
  await workspace.getByRole('button', { name: 'Review paper start' }).click()
  await workspace.getByRole('button', { name: 'START PAPER RESEARCH' }).click()
  await expect(workspace.getByText('Local app storage is unavailable', { exact: false })).toHaveCount(0)
  await expect(workspace.getByRole('button', { name: 'START PAPER RESEARCH' })).toBeEnabled()
  await workspace.getByRole('button', { name: 'START PAPER RESEARCH' }).click()
  await expect(workspace.getByText('RUNNING PAPER RUN')).toBeVisible()
  expect(startKeys[1]).toBe(startKeys[0])

  await workspace.getByRole('button', { name: 'Stop', exact: true }).click()
  await expect(workspace.getByText('STOPPED PAPER RUN')).toBeVisible()
  await workspace.getByRole('button', { name: 'Review paper start' }).click()
  await workspace.getByRole('button', { name: 'START PAPER RESEARCH' }).click()
  await expect(workspace.getByText('RUNNING PAPER RUN')).toBeVisible()
  expect(startKeys[2]).not.toBe(startKeys[1])

  await workspace.getByRole('button', { name: 'Stop', exact: true }).click()
  await workspace.getByRole('button', { name: 'evidence', exact: true }).click()

  reorder = true
  independentObservations = 11
  const refresh = workspace.locator('.session-review-footer button').first()
  await refresh.click()
  await firstRefreshSeen
  independentObservations = 22
  await refresh.click()
  await expect(workspace).toContainText('22 independent observations')
  releaseFirstRefresh()
  await firstRefreshFulfilled
  await expect(workspace).toContainText('22 independent observations')
  await expect(workspace).not.toContainText('11 independent observations')
})

test('research workspace UI-state fixture preserves gaps, lineage, paper identity and read-only navigation', async ({ context, page }, testInfo) => {
  await createAndEnter(page)
  const source = await createReviewedSession(page)
  await expect(page.locator(`[data-session-id="${source.id}"] .rail-row`)).toBeVisible({ timeout: 15_000 })
  const now = Math.floor(Date.now() / 1_000)
  let jobState: 'interrupted' | 'running' | 'cancelling' = 'interrupted'
  let paperState: 'interrupted' | 'running' | 'paused' | 'stopped' = 'interrupted'
  let eligibleDraft = false
  const commands: { path: string; body: Record<string, unknown> }[] = []
  const snapshot = () => ({
    schema: 'pumpwizard.research-workspace/v1',
    paper_research_allowed: true,
    source_session_id: source.id,
    source_label: 'Reviewed browser fixture',
    dataset_preview: null,
    datasets: [{
      schema: 'pumpwizard.research-dataset/v1', id: 'research-dataset-ui-fixture', content_sha256: 'd'.repeat(64),
      source_session_id: source.id, source_strategy_sha256: 'a'.repeat(64), created_at: now - 600,
      event_range: { first_at: now - 86_400, last_at: now - 3_600 },
      first_known_range: { first_at: now - 86_390, last_at: now - 3_590, basis: 'recorder-first-receipt with unknown rows retained' },
      source_age_seconds: 3_600,
      counts: { observations: 42, decisions: 8, outcomes: 5, independent_observations: 31, unknown_first_known: 2 },
      quality: { backfilled: 2, stale: 3, route_mismatch: 1 },
      gaps: [{ id: 'gap-1', started_at: now - 20_000, ended_at: now - 19_000, reason: 'collection outage', detail: 'No observations were available; values remain unknown.' }],
      supported_family: true, family: 'ladder-v1', eligible: true, blockers: [],
      evidence_policy: 'First-known records only; unknown receipt times remain visible but excluded.',
    }],
    experiments: [{
      schema: 'pumpwizard.research-experiment/v1', id: 'experiment-ui-fixture', digest: 'e'.repeat(64), dataset_id: 'research-dataset-ui-fixture', parent_experiment_id: null,
      created_at: now - 500, purpose: 'eligible', starting_capital: 1_000, capital_unit: 'USD', candidate_count: 3,
      candidate_package_sha256: ['a'.repeat(64), 'b'.repeat(64), 'c'.repeat(64)],
      cost_model: { label: 'Base route costs', capital_unit: 'USD', variable_fee_fraction_each_side: .01, slippage_fraction_each_side: .01, fixed_cost_each_side: 0, rent_each_entry: 0 },
      split: { train_end_at: now - 60_000, validation_end_at: now - 30_000, holdout_end_at: now - 3_600, embargo_seconds: 1_800 },
      evidence_policy: '20 independent episodes and 80% coverage required.', code_version: 'ui-fixture', replay_engine_version: 'fixture-ledger-v2', holdout_state: 'exposed', changed_from_parent: [],
    }],
    attempts: [
      { schema: 'pumpwizard.research-attempt/v1', id: 'attempt-1', experiment_id: 'experiment-ui-fixture', number: 1, state: 'failed', started_at: now - 480, finished_at: now - 470, error: 'Synthetic worker interruption', restart_count: 0 },
      { schema: 'pumpwizard.research-attempt/v1', id: 'attempt-2', experiment_id: 'experiment-ui-fixture', number: 2, state: jobState === 'interrupted' ? 'failed' : 'running', started_at: now - 460, finished_at: jobState === 'interrupted' ? now - 450 : null, error: jobState === 'interrupted' ? 'Service restarted before completion' : null, restart_count: 1 },
    ],
    jobs: [{ schema: 'pumpwizard.research-job/v1', id: 'job-ui-fixture', experiment_id: 'experiment-ui-fixture', attempt_id: 'attempt-2', state: jobState, progress: jobState === 'interrupted' ? .42 : .64, cancellation_requested: jobState === 'cancelling', restart_count: 1, report_id: 'report-losing-ui-fixture', error: jobState === 'interrupted' ? 'Service restart recorded; retry is explicit.' : null, created_at: now - 480, updated_at: now - 30 }],
    reports: [{
      schema: 'pumpwizard.research-replay-report/v1', id: 'report-losing-ui-fixture', experiment_id: 'experiment-ui-fixture', dataset_id: 'research-dataset-ui-fixture', created_at: now - 400,
      period: { first_at: now - 86_400, last_at: now - 3_600 }, capital_unit: 'USD',
      comparisons: [
        { id: 'cash', kind: 'cash', label: 'Cash', package_sha256: null, starting_capital: 1_000, ending_capital: 1_000, capital_unit: 'USD', modeled_net_pnl: 0, modeled_realized_pnl: 0, modeled_unrealized_pnl: 0, maximum_drawdown_fraction: 0, coverage_fraction: 1, independent_observations: 31, modeled_costs: 0, uncertainty: null, equity: [{ at: now - 86_400, equity: 1_000, gap_reason: null }, { at: now - 3_600, equity: 1_000, gap_reason: null }] },
        { id: 'current', kind: 'current', label: 'Current strategy', package_sha256: 'a'.repeat(64), starting_capital: 1_000, ending_capital: 914, capital_unit: 'USD', modeled_net_pnl: -86, modeled_realized_pnl: -60, modeled_unrealized_pnl: -26, maximum_drawdown_fraction: .14, coverage_fraction: .72, independent_observations: 22, modeled_costs: 34, uncertainty: 'Small sample; interval not available.', equity: [{ at: now - 86_400, equity: 1_000, gap_reason: null }, { at: now - 43_200, equity: null, gap_reason: 'unpriced route change' }, { at: now - 3_600, equity: 914, gap_reason: null }] },
      ],
      ledger: [{ id: 'ledger-1', comparison_id: 'current', opened_at: now - 50_000, closed_at: null, symbol: 'GAP', side: 'entry', quantity: 10, price: 10, value: null, cost: 4, pnl: null, state: 'unknown-exit', explanation: 'Route changed without a supported migration; no exit value was invented.' }],
      cost_stress: [{ id: 'stress-1', label: 'Adverse costs', comparison_id: 'current', modeled_net_pnl: -120, costs: 68, changed_recommendation: true }],
      exclusions: [{ reason: 'unsupported route change', count: 1, action: 'Inspect the pool lineage before another run.' }],
      recommendation: eligibleDraft
        ? { action: 'create_inactive_draft', candidate_id: 'candidate-exact', reason: 'Candidate passed the declared validation and adverse-cost policy.', blockers: [] }
        : { action: 'prefer_cash', candidate_id: 'cash', reason: 'Every eligible trading alternative lost to cash after modeled costs.', blockers: [] },
      holdout_state: 'exposed', limitations: ['Synthetic UI-state data demonstrates mechanics, not profitability.'],
    }],
    paper_runs: [{ schema: 'pumpwizard.paper-run/v1', id: 'paper-ui-fixture', source_session_id: source.id, strategy_sha256: 'a'.repeat(64), state: paperState, starting_capital_usd: 1_000, simulated_capital_usd: 982, started_at: now - 300, first_known_at: now - 295, updated_at: now - 5, decisions: 4, scored_outcomes: 2, unknown_outcomes: 2, independent_observations: 18, gaps: [{ id: 'paper-gap', started_at: now - 100, ended_at: now - 90, reason: 'browser-independent collection outage' }], assumptions: ['Base costs are modeled; no wallet receipt exists.'] }],
  })
  await context.route('**/api/v1/research/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    if (request.method() === 'GET' && path.endsWith('/export')) {
      await route.fulfill({ status: 200, contentType: url.searchParams.get('format') === 'csv' ? 'text/csv' : 'application/json', headers: { 'Content-Disposition': `attachment; filename="report-losing-ui-fixture.${url.searchParams.get('format')}"` }, body: url.searchParams.get('format') === 'csv' ? 'report_id,capital_unit\nreport-losing-ui-fixture,USD\n' : JSON.stringify({ report_id: 'report-losing-ui-fixture', capital_unit: 'USD' }) })
      return
    }
    if (request.method() === 'GET' && path.endsWith('/history')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        observations: [{ evidence_id: 'paper-evidence-1', mint: 'fixture-mint', route: 'curve', pool: 'fixture-pool', event_at: now - 80, first_received_at: now - 78, source_age_seconds: 2 }],
        decisions: [{ decision_key: 'paper-decision-1', evidence_id: 'paper-evidence-1', decided_at: now - 78, decision: { kind: 'entry_recommended', mint: 'fixture-mint', reason: 'Recorded before the later outcome' } }],
        outcomes: [{ decision_key: 'paper-decision-1', horizon_seconds: 1_800, scored_at: now - 20, status: 'scored', modeled_return_fraction: -.04, entry_evidence_id: 'paper-evidence-1', outcome_evidence_id: 'paper-evidence-2' }],
        gaps: [{ gap_id: 'paper-gap', started_at: now - 100, ended_at: now - 90, reason: 'browser-independent collection outage' }],
      }) })
      return
    }
    if (request.method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(snapshot()) })
      return
    }
    const body = request.postDataJSON() as Record<string, unknown>
    commands.push({ path, body })
    if (path.endsWith('/retry')) jobState = 'running'
    if (path.endsWith('/cancel')) jobState = 'cancelling'
    if (path.endsWith('/resume')) paperState = 'running'
    if (path.endsWith('/pause')) paperState = 'paused'
    if (path.endsWith('/stop')) paperState = 'stopped'
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ notice: 'Synthetic UI-state command accepted.' }) })
  })
  let signingOrSubmissionRequests = 0
  page.on('request', (request) => {
    if (request.method() === 'POST' && /(?:signing|trades\/submit)/.test(new URL(request.url()).pathname)) signingOrSubmissionRequests += 1
  })
  await page.locator(`[data-session-id="${source.id}"] .rail-row`).click()
  await openSelectedStrategyControls(page)
  await page.getByRole('dialog', { name: 'Session monitor' }).getByRole('button', { name: 'Review session evidence' }).click()
  let workspace = page.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await expect(workspace).toContainText('31 independent observations')
  await expect(workspace).toContainText('unknown first-known time')
  await expect(workspace).toContainText('collection outage')
  expect(commands).toHaveLength(0)
  await workspace.getByRole('button', { name: 'Retry as another attempt' }).click()
  await expect(workspace).toContainText('64% complete')
  await workspace.getByRole('button', { name: 'Cancel comparison' }).click()
  await expect(workspace).toContainText('cancelling')
  await workspace.getByRole('button', { name: 'results', exact: true }).click()
  await expect(workspace).toContainText('Prefer cash')
  await expect(workspace.getByRole('row', { name: /Current strategy/ })).toContainText('-$86.00')
  await expect(workspace).toContainText('Lines stop at every backend-declared gap')
  await expect(workspace).toContainText('Route changed without a supported migration')
  await page.screenshot({ path: testInfo.outputPath('research-results-cash-preferred.png'), fullPage: true })
  const downloadPromise = page.waitForEvent('download')
  await workspace.getByRole('link', { name: 'Export CSV' }).click()
  expect((await downloadPromise).suggestedFilename()).toBe('report-losing-ui-fixture.csv')
  await workspace.getByRole('button', { name: 'history', exact: true }).click()
  await expect(workspace).toContainText('cannot be reused as unseen evidence')
  await expect(workspace).toContainText('#1 failed')
  await workspace.getByRole('button', { name: 'Forward paper' }).click()
  await expect(workspace).toContainText('No wallet, signer, allowance or order path')
  await workspace.getByRole('button', { name: 'Inspect durable decisions and outcomes' }).click()
  await expect(workspace.getByRole('table', { name: 'Decisions were persisted before any later outcome' })).toContainText('Recorded before the later outcome')
  await expect(workspace).toContainText('-4.0% modeled')
  await workspace.getByRole('button', { name: 'Resume same run' }).click()
  await expect(workspace.getByText('RUNNING PAPER RUN')).toBeVisible()
  await workspace.getByRole('button', { name: 'Pause', exact: true }).click()
  await expect(workspace.getByText('PAUSED PAPER RUN')).toBeVisible()
  await workspace.getByRole('button', { name: 'Stop', exact: true }).click()
  await expect(workspace.getByText('STOPPED PAPER RUN')).toBeVisible()
  eligibleDraft = true
  await workspace.getByRole('button', { name: 'results', exact: true }).click()
  await workspace.getByRole('button', { name: 'Refresh', exact: true }).click()
  await expect(workspace).toContainText('Eligible inactive draft')
  await workspace.getByRole('button', { name: 'Review draft action' }).click()
  await expect(workspace).toContainText('report-losing-ui-fixture')
  await expect(workspace).toContainText('candidate-exact')
  await workspace.getByRole('button', { name: 'Create exact inactive draft' }).click()
  await expect.poll(() => commands.filter((command) => command.path.endsWith('/draft')).length).toBe(1)
  await expect(workspace).not.toBeVisible()
  expect(signingOrSubmissionRequests).toBe(0)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('dialog', { name: 'Session monitor' }).getByRole('button', { name: 'Review session evidence' }).click()
  workspace = page.getByRole('dialog', { name: 'Research workspace for Reviewed browser fixture' })
  await expect(workspace).toBeVisible()
  expect(await workspace.evaluate((node) => node.getBoundingClientRect().width <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('research-workspace-390px.png'), fullPage: true })
})

test('reviews a strategy decision, preserves ownership, and explicitly hands its exit to the wallet', async ({ page }, testInfo) => {
  await createAndEnter(page)
  const source = await createReviewedSession(page)
  const sourceRail = page.locator(`[data-session-id="${source.id}"]`)
  await expect(sourceRail.getByRole('button', { name: 'Manage Reviewed browser fixture', exact: true })).toBeVisible({ timeout: 15_000 })
  await openActivity(page)
  await sourceRail.getByRole('button', { name: 'Manage Reviewed browser fixture', exact: true }).click()
  await page.getByRole('group', { name: 'Reviewed browser fixture session actions' }).getByRole('button', { name: 'Open monitor' }).click()
  const observations = page.getByRole('dialog', { name: 'Session monitor' })
  const entryTable = observations.getByRole('table', { name: 'Session entry observations' })
  await expect(observations).toContainText('Reviewed browser fixture')
  await expect(entryTable).toContainText('Enter')
  await expect(entryTable).toContainText('strategy entry signal')
  await page.screenshot({ path: testInfo.outputPath('session-entry-observations.png'), fullPage: true })
  await observations.getByRole('button', { name: 'Close dialog' }).click()
  await openSessions(page)
  await expect(sourceRail.locator('.rail-row')).toBeVisible()
  await sourceRail.getByRole('button', { name: 'Manage Reviewed browser fixture', exact: true }).click()
  await page.getByRole('group', { name: 'Reviewed browser fixture session actions' }).getByRole('button', { name: 'Orders' }).click()
  let orders = page.getByRole('dialog', { name: 'Strategy orders' })
  let orderSession = orders.locator(`[data-session-id="${source.id}"]`)
  await orderSession.getByRole('button', { name: 'Review entry' }).first().click()
  const review = page.getByRole('region', { name: 'New order' })
  await review.getByRole('button', { name: 'Review order' }).click()
  await expect(review).toContainText('strategy entry signal')
  await expect(review).toContainText('Limits:')
  await review.getByRole('button', { name: 'Sign & submit' }).click()
  await expect(review.locator('.receipt-card')).toContainText('BUY · Pending')
  await closeOrder(page)
  await syntheticControl(page, { finalize: true })
  await openPortfolio(page)
  await page.getByRole('button', { name: 'Refresh holdings' }).click()
  const reviewedHolding = page.locator('.portfolio-holding-row').filter({ hasText: 'Reviewed browser fixture' })
  await expect(reviewedHolding).toContainText('Reviewed browser fixture')
  await reviewedHolding.getByRole('button', { name: /price history and holding details/ }).click()
  await expect(reviewedHolding).toContainText('Linked PumpWizard records shown above')
  await expect(reviewedHolding).toContainText('Cost basis unavailable')
  await expect(reviewedHolding).toContainText('Executable exit unavailable')
  await expect(reviewedHolding.getByRole('button', { name: 'Review linked strategy' })).toBeVisible()
  await backToDashboard(page, source.id)
  const managedPosition = page.locator('.terminal-position-row').filter({ hasText: 'Reviewed browser fixture' })
  await expect(managedPosition.locator('[data-label="Management"]')).toContainText('sell 40%')
  await expect(managedPosition).toContainText('Session managed')
  await openSessions(page)
  await sourceRail.getByRole('button', { name: 'Manage Reviewed browser fixture', exact: true }).click()
  await page.getByRole('group', { name: 'Reviewed browser fixture session actions' }).getByRole('button', { name: 'Orders' }).click()
  orders = page.getByRole('dialog', { name: 'Strategy orders' })
  orderSession = orders.locator(`[data-session-id="${source.id}"]`)
  await orderSession.getByRole('button', { name: 'Take over exit manually' }).click()
  await expect(orderSession).toContainText('Ownership and past receipts remain attributed')
  await orderSession.getByRole('button', { name: 'Release position for manual exit' }).click()
  await expect(orderSession).toContainText('Released for manual exit in Portfolio.')
  await orders.getByRole('button', { name: 'Close dialog' }).click()
  await backToDashboard(page, source.id)
  const releasedPosition = page.locator('.terminal-position-row').filter({ hasText: 'Reviewed browser fixture' })
  await expect(releasedPosition.locator('.terminal-position-compact')).toContainText('Reviewed browser fixture')
  await expect(releasedPosition).toContainText('Purchased by session · now user-managed')
  await expect(releasedPosition).toContainText('Manual review')
})

test('runs startup checks and keeps operational tools out of Portfolio', async ({ page }, testInfo) => {
  await createAndEnter(page)
  const board = await openCommandBoard(page)
  await expect(board.getByText('SYSTEM STATUS', { exact: true })).toBeVisible()
  await expect(board.getByText('Runner startup', { exact: true })).toHaveCount(0)
  await expect(board).toContainText('Local storage')
  await expect(board).toContainText('RPC sync test')
  await expect(board).toContainText('Wallets')
  await expect(board).toContainText('Market cache')
  await expect(board).toContainText('Sessions')
  await expect(board).toContainText(/Signing ready|Signing locked/)
  await expect(board).toContainText(/\d+ wallet(?:s)? known/)
  await expect(board).toContainText(/No sessions yet|\d+ need review|\d+ strategies monitoring|Sessions configured/)
  await expect(board).toContainText('0 automation authorizations · 0 running · 0 paused')
  await openPortfolio(page)
  await expect(page.getByRole('tabpanel', { name: 'Wallet', exact: true }).getByRole('group', { name: 'Wallet tools' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Automation Wallet', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Diagnostics', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'RPC connection', exact: true })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('portfolio-without-operator-tools.png'), fullPage: true })
})

for (const viewport of [{ width: 1280, height: 1100 }, { width: 390, height: 844 }]) {
  test(`pool chart shows sparse history, accurate counts and gaps at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport)
    await connectTestExtension(page)
    const { mint } = await syntheticControl(page)
    const data = await page.evaluate(async (mint) => {
      const response = await fetch(`/api/v1/coins/${mint}`)
      if (!response.ok) throw new Error('Could not load the isolated chart fixture')
      return response.json()
    }, mint)
    await page.route('**/api/v1/coins/*', async (route) => {
      const at = Math.floor(Date.now() / 1000 / 14400) * 14400
      const candle = { source: 'fixture', resolution_seconds: 14400, opened_at: at, open_usd: '0.000025', high_usd: '0.000031', low_usd: '0.000023', close_usd: '0.00002994', volume_usd: '7157' }
      await route.fulfill({ json: { ...data, candles: [], next_before: null, next_candle_before: null,
        pool_histories: [
          { address: 'AHkGQbTgYZpUyBmEbm6F2dKfC1e5Pekm5Pp9zPG6hjCj', dex: 'pump-fun', source: 'fixture', created_at: new Date(at * 1000).toISOString(), candles: [candle], gaps: [] },
          { address: 'G7DVvKFjjxia8q79gKvFoBrDveZuVYvqdrnNW2xeordj', dex: 'pumpswap', source: 'fixture', created_at: '', candles: [
            { ...candle, opened_at: at - 14400 * 3, open_usd: '0.00002', high_usd: '0.00002', low_usd: '0.00002', close_usd: '0.00002', volume_usd: null },
            { ...candle, open_usd: '0.00002', high_usd: '0.00002', low_usd: '0.00002', close_usd: '0.00002', volume_usd: null },
          ], gaps: [] },
        ], history_backfill: { ...data.history_backfill, status: 'complete', row_count: 3, coverage: 'Two separately tracked pools.' },
      } })
    })
    const order = await prepareBuy(page, mint)
    await order.getByRole('button', { name: 'Sign & submit' }).click()
    await closeOrder(page)
    await openPortfolio(page)
    await syntheticControl(page, { finalize: true })
    await page.getByRole('button', { name: 'Refresh holdings' }).click()
    await openPortfolio(page)
    await page.getByRole('button', { name: 'Open ALPHA token details', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Token details' })
    await expect(dialog).toContainText('Only one candle available')
    await expect(dialog).toContainText('3 saved candles across 2 pools')
    await expect(dialog).toContainText('1 candle in this pool')
    await expect(dialog.getByRole('button', { name: '7D', exact: true })).toHaveAttribute('aria-pressed', 'true')
    await expect(dialog.getByLabel('Historical pool')).toHaveCSS('color-scheme', 'dark')
    expect(await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true)
    const bounds = await dialog.boundingBox()
    expect(bounds!.x).toBeGreaterThanOrEqual(0)
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport.width)
    const chart = dialog.getByRole('img', { name: 'Price candlestick history chart and USD volume' })
    const candle = chart.locator('g rect[rx="1"]')
    const candleBox = await candle.boundingBox(), chartBox = await chart.boundingBox()
    expect((candleBox!.x - chartBox!.x) / chartBox!.width).toBeGreaterThan(.3)
    expect((candleBox!.x - chartBox!.x) / chartBox!.width).toBeLessThan(.6)
    await dialog.screenshot({ path: testInfo.outputPath(`token-history-${viewport.width}.png`) })
    if (viewport.width < 640) {
      await chart.scrollIntoViewIfNeeded()
      await dialog.screenshot({ path: testInfo.outputPath('token-history-mobile-chart.png') })
    }
    await dialog.getByLabel('Historical pool').selectOption({ label: 'PumpSwap · G7DVvK…xeordj' })
    await expect(dialog).not.toContainText('Only one candle available')
    await expect(dialog).toContainText('2 candles in this pool')
    await expect(dialog.locator('.chart-footnote')).toContainText('1 gap')
    await expect(dialog.locator('.candle-summary')).toContainText('Unknown')
    expect(await chart.innerHTML()).not.toMatch(/NaN|Infinity/)
    await dialog.getByText('View candle values', { exact: false }).click()
    await expect(dialog.getByRole('table')).toContainText('Unknown')
    await dialog.getByRole('button', { name: 'Market cap', exact: true }).click()
    await expect(dialog.getByLabel('Historical pool')).toHaveCount(0)
    await expect(dialog.getByRole('img', { name: 'Market cap history chart' })).toBeVisible()
  })
}

test('graduated buy uses the canonical PumpSwap review and can be cancelled without signing', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 1100 })
  await connectTestExtension(page)
  const bank = await syntheticControl(page, { graduated: true })
  const dialog = await openOrder(page)
  await dialog.getByLabel('Token mint').fill(bank.mint)
  await dialog.getByLabel('Spend amount').fill('0.001')
  await dialog.getByRole('button', { name: 'Review order' }).click()
  await expect(dialog).toContainText('Canonical PumpSwap')
  await expect(dialog.getByRole('button', { name: 'Sign & submit' })).toBeEnabled()
  expect((await syntheticControl(page)).sent).toBe(0)
  expect(await page.evaluate(async () => (window as unknown as { __testWallet: TestExtension }).__testWallet.signRequests)).toBe(0)
  await dialog.screenshot({ path: testInfo.outputPath('graduated-pumpswap-buy-review.png') })
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(dialog.getByLabel('Token mint')).toHaveValue(bank.mint)
  await expect(dialog.getByLabel('Spend amount')).toHaveValue('0.001')
  await expect(dialog.getByRole('button', { name: 'Review order' })).toBeEnabled()
  expect((await syntheticControl(page)).sent).toBe(0)
  expect(await page.evaluate(async () => (window as unknown as { __testWallet: TestExtension }).__testWallet.signRequests)).toBe(0)
})


test('keeps everyday service controls readable with optional setup and technical details', async ({ page }, testInfo) => {
  await connectTestExtension(page)
  const writes: string[] = []
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname.startsWith('/api/v1/service/')) writes.push(request.url())
  })
  const dialog = await openServiceControls(page, false)
  await expect(dialog.getByRole('heading', { name: 'Background service', exact: true })).toBeVisible()
  await expect(dialog.getByRole('heading', { name: 'Strategy monitoring', exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: /^(Start|Stop) monitoring$/ })).toBeVisible()
  await expect(dialog.getByRole('region', { name: 'Background trading', exact: true })).toContainText('needs your approval in the browser')
  await expect(dialog.getByRole('region', { name: 'Background trading', exact: true })).toContainText('Off')
  await expect(dialog.getByText('SERVICE INSTANCE', { exact: true })).not.toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Start auto trading', exact: true })).not.toBeVisible()
  await expect(dialog.getByRole('heading', { name: '1. Enroll your trading wallet' })).not.toBeVisible()
  expect(await dialog.evaluate((element) => element.scrollHeight <= element.clientHeight + 2)).toBe(true)
  await dialog.screenshot({ path: testInfo.outputPath('service-simple-desktop.png') })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(async () => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await expect(dialog.getByRole('button', { name: 'Done', exact: true })).toBeInViewport()
  await dialog.screenshot({ path: testInfo.outputPath('service-simple-mobile.png') })
  const technical = dialog.locator('summary', { hasText: 'Technical details' })
  await technical.focus()
  await page.keyboard.press('Enter')
  await expect(dialog.getByText('SERVICE INSTANCE', { exact: true })).toBeVisible()
  expect(writes).toEqual([])
  expect((await syntheticControl(page)).sent).toBe(0)
})


test('keeps initial status rows and appends transitions without polling noise or startup retests', async ({ page }, testInfo) => {
  let collectorEnabled = false
  let serviceAvailable = true
  let rpcTests = 0
  let polls = 0
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/v1/rpc' && request.method() === 'POST' && request.postDataJSON()?.action === 'test') rpcTests++
  })
  await page.route('**/api/v1/service/status', async (route) => {
    if (!serviceAvailable) { await route.fulfill({ status: 503, json: { error: 'Test service offline' } }); return }
    const response = await route.fetch({ headers: { ...await route.request().allHeaders(), Origin: new URL(route.request().url()).origin, 'Sec-Fetch-Site': 'same-origin' } })
    const data = await response.json()
    if (!data.service) { await route.fulfill({ response }); return }
    polls++
    data.service.heartbeat_at = Date.now() / 1000
    data.jobs = data.jobs.map((job: { id: string }) => job.id === 'strategy-monitor'
      ? { ...job, enabled: collectorEnabled, last_success_at: Date.now() / 1000, next_at: Date.now() / 1000 + 15 } : job)
    data.coverage = [{ id: 'changing-scan', status: 'completed', source: 'Test source', quote_count: polls, gap_count: 0, received_at: Date.now() / 1000, latest_source_at: Date.now() / 1000 }]
    await route.fulfill({ response, json: data })
  })
  await createAndEnter(page)
  const history = page.getByRole('log', { name: 'Status history' })
  const rows = history.locator('.command-status-card')
  await expect(page.getByRole('button', { name: 'Open background service controls', exact: true })).toContainText(`Runner v${currentVersion}`)
  await expect(history.locator('[data-current=true]')).toHaveCount(7)
  const initialCount = await rows.count()
  const initial = await rows.allTextContents()
  const initialRpcTests = rpcTests
  await page.clock.install()
  const tick = async () => {
    const response = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/v1/service/status')
    await page.clock.fastForward(6_000)
    await response
    await page.clock.runFor(100)
  }
  for (let poll = 0; poll < 4; poll++) await tick()
  await expect(rows).toHaveCount(initialCount)
  expect(await rows.allTextContents()).toEqual(initial)
  expect(rpcTests).toBe(initialRpcTests)
  collectorEnabled = true
  await tick()
  await expect(rows).toHaveCount(initialCount + 1)
  await expect(rows.last()).toContainText('collector enabled')
  const stream = page.locator('.command-board-stream')
  await stream.evaluate((node) => { node.style.maxHeight = '180px'; node.scrollTop = 0; node.dispatchEvent(new Event('scroll')) })
  collectorEnabled = false
  await tick()
  await expect(rows).toHaveCount(initialCount + 2)
  await expect(rows.last()).toContainText('collector disabled')
  expect(await stream.evaluate((node) => node.scrollTop)).toBe(0)
  expect((await rows.allTextContents()).slice(0, initialCount)).toEqual(initial)
  await tick()
  await expect(rows).toHaveCount(initialCount + 2)
  expect(rpcTests).toBe(initialRpcTests)
  serviceAvailable = false
  await tick()
  await expect(rows).toHaveCount(initialCount + 3)
  await expect(rows.last()).toContainText('Disconnected · state unknown')
  await tick()
  await expect(rows).toHaveCount(initialCount + 3)
  serviceAvailable = true
  await tick()
  await expect(rows).toHaveCount(initialCount + 4)
  await expect(rows.last()).toContainText(`Runner v${currentVersion}`)
  expect((await rows.allTextContents()).slice(0, initialCount)).toEqual(initial)
  await history.screenshot({ path: testInfo.outputPath('status-transitions.png') })
  expect((await syntheticControl(page)).sent).toBe(0)
})


test('records RPC recovery after an explicit connection test without replacing the failed row', async ({ page }) => {
  let failing = true
  await page.route('**/api/v1/rpc', async (route) => {
    if (failing && route.request().method() === 'POST') {
      await route.fulfill({ status: 503, json: { error: 'Test RPC unavailable' } })
    } else await route.continue()
  })
  await createAndEnter(page)
  const history = page.getByRole('log', { name: 'Status history' })
  const rpcRows = history.locator('[data-check-id=rpc]')
  await expect(rpcRows).toHaveCount(1)
  await expect(rpcRows.first()).toContainText('Test RPC unavailable')
  const failed = await rpcRows.first().textContent()
  await page.getByRole('button', { name: 'Fix RPC connection', exact: true }).click()
  failing = false
  const dialog = page.getByRole('dialog', { name: 'RPC connection', exact: true })
  await dialog.getByRole('button', { name: 'Test connection', exact: true }).click()
  await expect(dialog.getByRole('status')).toContainText('current blockhash verified')
  await dialog.getByRole('button', { name: 'Close dialog' }).click()
  await expect(rpcRows).toHaveCount(2)
  await expect(rpcRows.last()).toContainText('Mainnet connected')
  expect(await rpcRows.first().textContent()).toBe(failed)
  await expect(page.getByRole('button', { name: 'Fix RPC connection', exact: true })).toHaveCount(0)
})

test('creates paper trading through Sessions Monitor and starts only on explicit review', async ({ page }, testInfo) => {
  await createAndEnter(page)
  await page.getByRole('button', { name: 'Add session', exact: true }).click()
  await page.getByRole('dialog', { name: 'Create session' }).getByRole('button', { name: /LEARN FIRST Monitor/ }).click()
  const confirmation = page.getByRole('dialog', { name: 'Confirm monitoring session' })
  await expect(confirmation.getByRole('button', { name: /Observations/ })).toHaveAttribute('aria-pressed', 'true')
  await confirmation.getByRole('button', { name: /Paper trading/ }).click()
  await page.screenshot({ path: testInfo.outputPath('monitor-activity-choice.png'), fullPage: true })
  const createdResponse = page.waitForResponse((response) => response.url().endsWith('/api/v1/session-workspaces') && response.request().method() === 'POST')
  await confirmation.getByRole('button', { name: 'Confirm monitoring session', exact: true }).click()
  const created = await (await createdResponse).json()
  expect(created.session.plan).toEqual({ monitor_activity: 'paper' })
  expect(created.session.allocation_lamports).toBe('0')
  await page.reload()
  const workspace = page.getByRole('article', { name: `${created.session.label} session`, exact: true })
  await expect(workspace.locator('.rail-workspace-toggle')).toHaveAttribute('title', /Paper trading/)
  await expect(workspace.getByRole('img', { name: 'Not running' })).toBeVisible()
  await workspace.getByRole('button', { name: /Add strategy to/ }).click()
  await page.getByLabel('Import strategy configuration').setInputFiles({ name: 'paper-strategy.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(strategy)) })
  const library = page.getByRole('dialog', { name: 'Local strategy library' })
  const card = library.getByRole('article').filter({ hasText: 'Browser E2E core' })
  await expect(card).toContainText('Can add', { timeout: 15_000 })
  await card.getByRole('button').click()
  const sessionResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/v1/sessions' && response.request().method() === 'POST')
  await library.getByRole('button', { name: 'Add to session & edit', exact: true }).click()
  const session = await (await sessionResponse).json()
  await expect(library.getByRole('form', { name: 'Edit saved strategy', exact: true })).toBeVisible()
  await library.getByRole('button', { name: 'Close dialog', exact: true }).click()
  await workspace.getByRole('button', { name: 'Manage Browser E2E core', exact: true }).click()
  await page.getByRole('group', { name: 'Browser E2E core session actions', exact: true }).getByRole('button', { name: 'Open monitor', exact: true }).click()
  const monitor = page.getByRole('dialog', { name: 'Session monitor', exact: true })
  await expect(monitor.getByText('Paper budget', { exact: true })).toBeVisible()
  await expect(monitor.locator('.session-brief-summary')).toContainText('None')
  await monitor.getByRole('button', { name: 'Paper trading', exact: true }).click()
  let paper = page.getByRole('dialog', { name: 'Research workspace for Browser E2E core', exact: true })
  await expect(paper.getByRole('button', { name: 'Forward paper', exact: true })).toHaveAttribute('aria-current', 'page')
  await expect(paper.getByRole('button', { name: 'Review paper start', exact: true })).toBeEnabled()
  const before = await page.evaluate(async (id) => (await fetch(`/api/v1/research/sessions/${id}`)).json(), session.instance.id)
  expect(before.paper_runs).toEqual([])
  expect(before.datasets).toEqual([])
  expect(before.source_strategy_sha256).toBeTruthy()
  await paper.getByRole('button', { name: 'Review paper start', exact: true }).click()
  await paper.getByRole('button', { name: 'START PAPER RESEARCH', exact: true }).click()
  await expect(paper).toContainText('RUNNING PAPER RUN')
  await paper.getByRole('button', { name: 'Pause', exact: true }).click()
  await expect(paper).toContainText('PAUSED PAPER RUN')
  await page.screenshot({ path: testInfo.outputPath('session-paper-run.png'), fullPage: true })
  await paper.getByRole('button', { name: 'Stop', exact: true }).click()
  await expect(paper).toContainText('STOPPED PAPER RUN')
  await paper.getByRole('button', { name: 'Done', exact: true }).click()
  await monitor.getByRole('button', { name: 'Done', exact: true }).click()
  await workspace.getByRole('button', { name: 'Manage Browser E2E core', exact: true }).click()
  await page.getByRole('group', { name: 'Browser E2E core session actions' }).getByRole('button', { name: 'Paper trading', exact: true }).click()
  paper = page.getByRole('dialog', { name: 'Research workspace for Browser E2E core', exact: true })
  await expect(paper).toContainText('STOPPED PAPER RUN')
  expect((await syntheticControl(page)).sent).toBe(0)
})

async function openAutomationWalletSetup(page: Page) {
  await page.getByRole('button', { name: 'Add session', exact: true }).click()
  const wizard = page.getByRole('dialog', { name: 'Create session' })
  await wizard.getByRole('button', { name: /Trading session/ }).click()
  await expect(wizard).toContainText('Want automatic buys or sells?')
  await wizard.getByRole('button', { name: 'Set up automation wallet', exact: true }).click()
  return page.getByRole('dialog', { name: 'Set up automation wallet' })
}

async function createAutomationDestination(page: Page) {
  const setup = await openAutomationWalletSetup(page)
  await setup.getByRole('button', { name: 'Create new PumpWizard wallet', exact: true }).click()
  await setup.getByLabel('Password', { exact: true }).fill(password)
  await setup.getByLabel('Confirm password', { exact: true }).fill(password)
  await setup.getByRole('button', { name: 'Create encrypted wallet', exact: true }).click()
  await expect(setup.getByRole('button', { name: 'Continue to wallet access' })).toBeDisabled()
  const download = page.waitForEvent('download')
  await setup.getByRole('button', { name: 'Download encrypted backup' }).click()
  await download
  await setup.getByLabel(/I saved the encrypted backup/).check()
  await setup.getByRole('button', { name: 'Continue to wallet access' }).click()
  await setup.getByLabel('Wallet password').fill('wrong password')
  await setup.getByRole('button', { name: 'Confirm wallet access' }).click()
  await expect(setup.getByRole('alert')).toContainText('password is incorrect')
  await expect(setup.getByRole('button', { name: 'Fund wallet' })).toHaveCount(0)
  await setup.getByLabel('Wallet password').fill(password)
  await setup.getByRole('button', { name: 'Confirm wallet access' }).click()
  await expect(setup.getByRole('button', { name: 'Fund wallet' })).toBeVisible()
  return setup
}

async function extensionChain(page: Page, changes: Record<string, boolean> = {}) {
  return page.evaluate(async (changes) => {
    const sender = (window as unknown as { __testWallet: TestExtension & { address: string } }).__testWallet.address
    return (await fetch('/api/v1/test-chain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender, changes }) })).json() as Promise<{ sent: number }>
  }, changes)
}

test('funds a new automation wallet with one reviewed extension transfer and resumes session setup', async ({ page }, testInfo) => {
  await connectTestExtension(page)
  const workspaceIdsBefore = await page.evaluate(async () => {
    const created = await fetch('/api/v1/session-workspaces', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'monitoring', label: 'Existing unrelated monitor' }) })
    if (!created.ok) throw new Error(await created.text())
    const current = await (await fetch('/api/v1/session-workspaces')).json()
    return current.rows.map((row: { id: string }) => row.id).sort() as string[]
  })
  const source = await page.evaluate(async () => (window as unknown as { __testWallet: { address: string } }).__testWallet.address)
  const setup = await createAutomationDestination(page)
  const target = await setup.locator('.stored-address code').textContent()
  expect(target).not.toBe(source)
  await page.screenshot({ path: testInfo.outputPath('automation-wallet-funding.png'), fullPage: true })
  expect((await extensionChain(page)).sent).toBe(0)
  await setup.getByLabel('Amount (SOL)').fill('0.01')
  await setup.getByRole('button', { name: 'Fund wallet' }).click()
  const transfer = page.getByRole('dialog', { name: 'Fund automation wallet' })
  await expect(transfer.getByLabel('Destination wallet')).toHaveCount(0)

  await expect(transfer).toContainText(source)
  await expect(transfer).toContainText(target!)
  await expect(transfer).toContainText('Total debit')
  await expect(transfer).toContainText('0.009995 SOL')
  await expect(transfer).toContainText('deducted from budget')
  await expect(transfer).toContainText('Minimum kept in source')
  expect((await extensionChain(page)).sent).toBe(0)
  await transfer.getByRole('button', { name: 'Sign & send SOL' }).click()
  await expect(transfer.getByRole('status')).toContainText('Submitted signature')
  await expect(transfer).toContainText('Awaiting confirmation')
  await expect(transfer.locator('.transfer-confirmation-spinner')).toBeVisible()
  await expect(transfer).toContainText('Checking Solana automatically')
  await expect(transfer).toContainText('0.009995 SOL')
  await extensionChain(page, { finalize: true })
  await expect(transfer.getByRole('button', { name: 'Check confirmation' })).toHaveCount(0)
  await expect(transfer.getByRole('heading')).toContainText('Transfer finalized')
  await expect(transfer.locator('.transfer-confirmation-spinner')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('funding-transfer-receipt.png'), fullPage: true })
  expect((await extensionChain(page)).sent).toBe(1)
  await transfer.getByRole('button', { name: 'Close dialog' }).click()
  await expect(setup.getByRole('button', { name: 'Fund wallet' })).toBeDisabled()
  await setup.getByRole('button', { name: 'Use this wallet for session' }).click()
  const wizard = page.getByRole('dialog', { name: 'Create session' })
  await expect(wizard).toContainText('Choose how trading decisions are approved')
  await expect(wizard.getByRole('button', { name: /Full Autopilot/ })).toBeEnabled()
  await expect(wizard.getByRole('button', { name: /Interactive/ })).toHaveAttribute('aria-pressed', 'true')
  await expect(wizard).not.toContainText('Want automatic buys or sells?')
  const saved = await page.evaluate(async () => JSON.parse((await (await fetch('/api/v1/app-state')).json()).preferences['pumpwizard.wallet-session/v1'] || '{}'))
  expect(saved.address).toBe(target)
  expect(saved.kind).toBe('local')
  expect(JSON.stringify(saved)).not.toContain(password)
  const workspaces = await page.evaluate(async () => (await fetch('/api/v1/session-workspaces')).json())
  expect(workspaces.rows.map((row: { id: string }) => row.id).sort()).toEqual(workspaceIdsBefore)
})

test('selects an existing automation wallet and can cancel funding without a transfer', async ({ page }) => {
  await connectTestExtension(page)
  let setup = await createAutomationDestination(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(async () => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  const target = await setup.locator('.stored-address code').textContent()
  await setup.getByRole('button', { name: 'Back to session' }).click()
  await page.getByRole('dialog', { name: 'Create session' }).getByRole('button', { name: 'Set up automation wallet' }).click()
  setup = page.getByRole('dialog', { name: 'Set up automation wallet' })
  await setup.getByRole('button', { name: /Use existing PumpWizard wallet/ }).click()
  await setup.getByLabel('Wallet password').fill(password)
  await setup.getByRole('button', { name: 'Confirm wallet access' }).click()
  await setup.getByLabel('Amount (SOL)').fill('0.01')
  await setup.getByRole('button', { name: 'Fund wallet' }).click()
  const transfer = page.getByRole('dialog', { name: 'Fund automation wallet' })

  await expect.poll(() => page.evaluate(async () => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await transfer.getByRole('button', { name: 'Cancel review' }).click()
  await expect(setup.getByLabel('Amount (SOL)')).toHaveValue('0.01')
  expect((await extensionChain(page)).sent).toBe(0)
  await setup.getByRole('button', { name: 'Use this wallet for session' }).click()
  await expect(page.getByRole('dialog', { name: 'Create session' }).getByRole('button', { name: /Automatic entries only/ })).toBeEnabled()
  expect(await page.evaluate(async () => JSON.parse((await (await fetch('/api/v1/app-state')).json()).preferences['pumpwizard.wallet-session/v1'] || '{}').address)).toBe(target)
})

for (const mode of ['reject', 'message-change'] as const) {
  test(`automation wallet funding refuses ${mode} without sending SOL`, async ({ page }) => {
    await connectTestExtension(page)
    const setup = await createAutomationDestination(page)
    await setup.getByLabel('Amount (SOL)').fill('0.01')
    await setup.getByRole('button', { name: 'Fund wallet' }).click()
    const transfer = page.getByRole('dialog', { name: 'Fund automation wallet' })

    await page.evaluate((mode) => { (window as unknown as { __testWallet: TestExtension }).__testWallet.mode = mode }, mode)
    await transfer.getByRole('button', { name: 'Sign & send SOL' }).click()
    await expect(transfer.getByRole('status')).toContainText('Nothing was submitted by Runner')
    expect((await extensionChain(page)).sent).toBe(0)
  })
}

test('automation funding keeps an uncertain submission from being sent again in the same setup', async ({ page }) => {
  await connectTestExtension(page)
  const setup = await createAutomationDestination(page)
  await setup.getByLabel('Amount (SOL)').fill('0.01')
  await setup.getByRole('button', { name: 'Fund wallet' }).click()
  const transfer = page.getByRole('dialog', { name: 'Fund automation wallet' })

  await extensionChain(page, { disconnect: true })
  await transfer.getByRole('button', { name: 'Sign & send SOL' }).click()
  await expect(transfer.getByRole('heading')).toContainText('Transfer not confirmed')
  await expect(transfer.getByRole('status')).not.toContainText('Submitted signature')
  await expect(transfer).toContainText('Check the original transfer')
  await expect(transfer.getByRole('button', { name: 'Sign & send SOL' })).toHaveCount(0)
  await expect.poll(async () => (await extensionChain(page)).sent).toBe(1)
  await transfer.getByRole('button', { name: 'Close dialog' }).click()
  await expect(setup.getByRole('button', { name: 'Fund wallet' })).toBeDisabled()
  await expect(setup).toContainText('outcome is unknown')
})

test('automation wallet cards show SOL, token holdings and unknown balances without inventing value', async ({ page }, testInfo) => {
  await connectTestExtension(page)
  const source = await page.evaluate(async () => (window as unknown as { __testWallet: { address: string } }).__testWallet.address)
  let unavailable = false
  let price: number | null = 200
  await page.route('**/api/v1/wallet/inventory?*', async (route) => {
    const url = new URL(route.request().url())
    const sender = url.searchParams.get('sender')!
    if (unavailable && sender !== source) return route.fulfill({ status: 503, json: { error: 'Fixture RPC unavailable' } })
    return route.fulfill({ json: { sender, network: 'mainnet-beta', balance: sender === source ? '1000000000' : '250000000', sol_usd_price: price, checked_at: Date.now() / 1000, coverage_gaps: [], assets: sender === source ? [] : [{ mint: source, amount: '42500000', decimals: 6, symbol: 'FIXTURE', name: 'Synthetic token', program: 'Token-2022' }] } })
  })
  const setup = await createAutomationDestination(page)
  const destination = setup.locator('.automation-wallet-selected')
  await expect(destination).toContainText('0.25 SOL')
  await expect(destination).toContainText('$50.00 in SOL')
  await destination.locator('summary').click()
  await expect(destination).toContainText('42.5')
  await expect(destination).toContainText('Token-2022')
  await setup.getByRole('button', { name: '25%', exact: true }).click()
  await expect(setup.getByLabel('Amount (SOL)')).toHaveValue('0.25')
  await page.screenshot({ path: testInfo.outputPath('automation-wallet-balances-and-funding.png'), fullPage: true })
  await setup.getByRole('button', { name: 'Choose another wallet' }).click()
  const saved = setup.locator('.automation-wallet-card').filter({ has: page.getByRole('button', { name: 'Use existing PumpWizard wallet' }) })
  await expect(saved).toContainText('0.25 SOL')
  await saved.locator('summary').click()
  await expect(saved).toContainText('FIXTURE')
  await expect(saved).toContainText('42.5')
  await page.screenshot({ path: testInfo.outputPath('automation-wallet-chooser.png'), fullPage: true })
  price = null
  await setup.getByRole('button', { name: 'Refresh balances' }).click()
  await expect(saved).toContainText('USD estimate unavailable')
  await expect(saved).not.toContainText('$50.00')
  unavailable = true
  await setup.getByRole('button', { name: 'Refresh balances' }).click()
  await expect(saved).toContainText('Balance unavailable')
  await expect(saved).not.toContainText('0 SOL')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(async () => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  expect((await extensionChain(page)).sent).toBe(0)
})


test('automation funding Max deducts the exact fee and keeps the wallet reserve', async ({ page }) => {
  await connectTestExtension(page)
  const setup = await createAutomationDestination(page)
  await setup.getByRole('button', { name: 'Max', exact: true }).click()
  await expect(setup.getByLabel('Amount (SOL)')).toHaveValue('1.25')
  await setup.getByRole('button', { name: 'Fund wallet' }).click()
  const review = page.getByRole('dialog', { name: 'Fund automation wallet' })
  await expect(review).toContainText('1.247995 SOL')
  await expect(review).toContainText('1.248 SOL')
  await expect(review).toContainText('0.002 SOL')
  await expect(review).toContainText('reduced to preserve your source reserve')
  expect((await extensionChain(page)).sent).toBe(0)
  await review.getByRole('button', { name: 'Cancel review' }).click()
})


test('automation funding returns from Phantom reconnect to the transfer review', async ({ page }) => {
  await connectTestExtension(page, true)
  await createAutomationDestination(page)
  await page.reload()
  await expect(page.getByRole('region', { name: 'Wallet connection' })).toContainText('signing locked')
  const setup = await openAutomationWalletSetup(page)
  await setup.getByRole('button', { name: 'Use existing PumpWizard wallet' }).click()
  await setup.getByLabel('Wallet password').fill(password)
  await setup.getByRole('button', { name: 'Confirm wallet access' }).click()
  await setup.getByLabel('Amount (SOL)').fill('0.05')
  await setup.getByRole('button', { name: 'Fund wallet', exact: true }).click()
  const unlock = page.getByRole('dialog', { name: 'Unlock wallet signing' })
  await unlock.getByRole('button', { name: 'Reconnect E2E Phantom compatible' }).click()
  const review = page.getByRole('dialog', { name: 'Fund automation wallet' })
  await expect(review.getByRole('heading')).toHaveText('Review transfer')
  await expect(review).toContainText('0.049995 SOL')
  expect((await extensionChain(page)).sent).toBe(0)
  await review.getByRole('button', { name: 'Cancel review' }).click()
  await expect(setup.getByLabel('Amount (SOL)')).toHaveValue('0.05')
})

test('automation funding shows a blocked review beside the funding button', async ({ page }) => {
  await connectTestExtension(page)
  const setup = await createAutomationDestination(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.route('**/api/v1/withdrawals/preview', (route) => route.fulfill({ status: 409, json: { error: 'Resolve the original wallet signature before another withdrawal' } }))
  await setup.getByLabel('Amount (SOL)').fill('0.05')
  await setup.getByRole('button', { name: 'Fund wallet', exact: true }).click()
  await expect(setup.getByRole('alert')).toContainText('Resolve the original wallet signature')
  await expect(setup.getByRole('alert')).toBeInViewport()
  await expect(setup.getByRole('button', { name: 'Fund wallet', exact: true })).toBeEnabled()
  expect((await extensionChain(page)).sent).toBe(0)
})

for (const recovered of [true, false]) {
  test(`automation funding recovery ${recovered ? 'releases the checked lock for a new review' : 'keeps the lock when RPC verification fails'}`, async ({ page }) => {
    await connectTestExtension(page)
    const setup = await createAutomationDestination(page)
    let resolved = false
    let checks = 0
    await page.route('**/api/v1/trades?*', async (route) => {
      const response = await route.fetch({ headers: { ...await route.request().allHeaders(), Origin: new URL(route.request().url()).origin, 'Sec-Fetch-Site': 'same-origin' } })
      const body = await response.json()
      body.withdrawals = [{ id: 'original-expired-transfer', signature: 'original-signature', status: resolved ? 'expired' : 'unknown', recipient: 'fixture-destination', lamports: '110000000', fee: '5000', detail: 'Fixture unknown transfer' }]
      await route.fulfill({ response, json: body })
    })
    await page.route('**/api/v1/withdrawals/preview', async (route) => {
      if (resolved) return route.continue()
      return route.fulfill({ status: 409, json: { error: 'Resolve the original wallet signature before another withdrawal' } })
    })
    await page.route('**/api/v1/withdrawals/resolve-expired', async (route) => {
      checks += 1
      expect(route.request().postDataJSON()).toMatchObject({ transfer_id: 'original-expired-transfer', signature: 'original-signature', network: 'mainnet-beta' })
      if (!recovered) return route.fulfill({ status: 503, json: { error: 'Independent RPC unavailable; lock remains' } })
      resolved = true
      return route.fulfill({ json: { transfer: { status: 'expired', detail: 'Expired and resolved' } } })
    })
    await setup.getByLabel('Amount (SOL)').fill('0.05')
    await setup.getByRole('button', { name: 'Fund wallet', exact: true }).click()
    await setup.getByRole('button', { name: 'Check & resolve previous transfer' }).click()
    if (recovered) {
      await expect(setup).toContainText('Nothing was resent')
      await expect(setup.getByRole('button', { name: 'Check & resolve previous transfer' })).toHaveCount(0)
      await expect(page.getByRole('dialog', { name: 'Fund automation wallet' })).toHaveCount(0)
      await expect(setup.getByLabel('Amount (SOL)')).toHaveValue('0.05')
      await setup.getByRole('button', { name: 'Fund wallet', exact: true }).click()
      await expect(page.getByRole('dialog', { name: 'Fund automation wallet' })).toContainText('0.049995 SOL')
    } else {
      await expect(setup.getByRole('alert')).toContainText('lock remains')
      await expect(setup.getByRole('button', { name: 'Check & resolve previous transfer' })).toBeEnabled()
    }
    expect(checks).toBe(1)
    expect((await extensionChain(page)).sent).toBe(0)
  })
}

test('automation funding checks confirmation automatically and shows a failed result without resending', async ({ page }) => {
  await connectTestExtension(page)
  const setup = await createAutomationDestination(page)
  await setup.getByLabel('Amount (SOL)').fill('0.01')
  const previewResponse = page.waitForResponse((response) => response.url().endsWith('/api/v1/withdrawals/preview'))
  await setup.getByRole('button', { name: 'Fund wallet', exact: true }).click()
  const { preview } = await (await previewResponse).json()
  let fail = false
  await page.route('**/api/v1/trades/reconcile', (route) => {
    if (!fail) return route.fulfill({ status: 503, json: { error: 'Fixture RPC temporarily unavailable' } })
    return route.fulfill({ json: { sender: preview.sender, network: preview.network, withdrawals: [{ id: preview.id, signature: 'fixture-failed-signature', status: 'failed', detail: 'Network finalized the transaction with an error' }] } })
  })
  const transfer = page.getByRole('dialog', { name: 'Fund automation wallet' })
  await transfer.getByRole('button', { name: 'Sign & send SOL' }).click()
  await expect(transfer.locator('.transfer-confirmation-spinner')).toBeVisible()
  await expect(transfer.getByRole('status')).toContainText('Checking again automatically')
  await expect(transfer.getByRole('button', { name: 'Check confirmation' })).toHaveCount(0)
  fail = true
  await expect(transfer.getByRole('heading')).toHaveText('Transfer failed', { timeout: 10000 })
  await expect(transfer.locator('.transfer-confirmation-spinner')).toHaveCount(0)
  expect((await extensionChain(page)).sent).toBe(1)
  await transfer.getByRole('button', { name: 'Back to wallet setup' }).click()
  await expect(transfer).toHaveCount(0)
  await expect(setup.getByRole('button', { name: 'Fund wallet', exact: true })).toBeDisabled()
})


test('command board appends entry activity once, retries reads, and opens its strategy without AI', async ({ page }, testInfo) => {
  await createAndEnter(page)
  const source = await createReviewedSession(page)
  const history = page.getByRole('log', { name: 'Status history' })
  await expect(history.locator(`[data-check-id="strategy-added:${source.id}"]`)).toContainText('Strategy added')
  let polls = 0
  await page.route('**/api/v1/session-activity*', async (route) => {
    polls++
    if (polls === 1) { await route.fulfill({ status: 503, json: { error: 'Temporary activity read failure' } }); return }
    await route.fulfill({ json: { cursor: 99, has_more: false, rows: [{
      id: 'decision:board-entry-test', session_id: source.id, strategy: 'Reviewed browser fixture', workspace: 'Test monitoring session',
      at: Date.now() / 1000, kind: 'entry_recommended', mint: 'TestMint', symbol: 'TEST', reason: 'strategy_entry_signal',
    }, {
      id: 'decision:board-skip-test', session_id: source.id, strategy: 'Reviewed browser fixture', workspace: 'Test monitoring session',
      at: Date.now() / 1000, kind: 'entry_blocked', mint: 'SkippedMint', symbol: 'SKIP', reason: 'liquidity_below_minimum',
    }] } })
  })
  const signal = history.locator('[data-check-id="decision:board-entry-test"]')
  const skipped = history.locator('[data-check-id="decision:board-skip-test"]')
  await expect(signal).toContainText('Entry signal · TEST', { timeout: 15_000 })
  await expect(signal).toContainText('strategy entry signal')
  await expect(skipped).toContainText('Entry skipped · SKIP')
  await expect(skipped).toContainText('liquidity below minimum')
  const original = await signal.textContent()
  await expect.poll(() => polls, { timeout: 15_000 }).toBeGreaterThanOrEqual(4)
  await expect(signal).toHaveCount(1)
  await expect(signal).toHaveText(original!)
  await expect(skipped).toHaveCount(1)
  await history.screenshot({ path: testInfo.outputPath('session-entry-activity.png') })
  await signal.click()
  await expect(page.getByRole('dialog', { name: 'Session monitor' })).toContainText('Reviewed browser fixture')
  expect((await syntheticControl(page)).sent).toBe(0)
})

test('wallet switches isolate sessions strategies and AI history then restore the original workspace', async ({ page }, testInfo) => {
  await createAndEnter(page)
  const walletA = await page.evaluate(async () => (await (await fetch('/api/v1/app-state')).json()).wallets[0].address as string)
  const fixture = await createReviewedSession(page)
  const board = page.getByRole('region', { name: 'System and optional AI' })
  const privateRules = { ...strategy, strategy: { id: 'wallet-private-test', label: 'Wallet A saved rules' } }
  const exportedRules = await page.evaluate(async ({ sender, rules }) => {
    const q = new URLSearchParams({ sender, network: 'mainnet-beta' })
    const saved = await fetch(`/api/v1/strategies/import?${q}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(rules) })
    if (!saved.ok) throw new Error(await saved.text())
    const digest = (await saved.json()).strategy.digest
    const exported = await fetch(`/api/v1/strategies/${digest}/export?${q}`)
    if (!exported.ok) throw new Error(await exported.text())
    return (await exported.json()).package
  }, { sender: walletA, rules: privateRules })
  expect(exportedRules).toEqual(privateRules)
  // Create a real wallet-owned monitoring workspace through the same API as the wizard.
  const ownWorkspace = await page.evaluate(async (sender) => {
    const result = await fetch('/api/v1/session-workspaces', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'monitoring', label: 'Wallet A research', owner: { sender, network: 'mainnet-beta' } }) })
    if (!result.ok) throw new Error(await result.text())
    return (await result.json()).session.id as string
  }, walletA)
  await expect(page.locator(`[data-session-id="${fixture.id}"]`)).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText('Wallet A research', { exact: true }).first()).toBeVisible({ timeout: 15_000 })
  await page.evaluate(async (sender) => {
    await fetch('/api/v1/assistant/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: 'openai', model: 'test-model', api_key: 'synthetic-only', endpoint: '' }) })
    const query = new URLSearchParams({ sender, network: 'mainnet-beta' })
    const response = await fetch(`/api/v1/assistant/prepare?${query}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ conversation: null, message: 'Wallet A private conversation', max_output: 128 }) })
    const pending = await response.json()
    if (!response.ok) throw new Error(JSON.stringify(pending))
    await fetch(`/api/v1/assistant/decline?${query}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ request_id: pending.request_id }) })
  }, walletA)
  await page.reload()
  await board.getByRole('button', { name: 'Read selected chat' }).click()
  await expect(board).toContainText('Wallet A private conversation')
  await page.locator(`[data-session-id="${fixture.id}"] .rail-row`).click()
  await expect(page.locator('.trading-focus-header')).toContainText('Reviewed browser fixture')
  await page.getByRole('tab', { name: /Activity/ }).click()
  await (await openWalletActions(page)).getByRole('button', { name: 'Sign out of wallet', exact: true }).click()
  await expect(page.locator(`[data-session-id="${fixture.id}"]`)).toHaveCount(0)
  await expect(board).not.toContainText('Wallet A private conversation')
  await page.getByRole('button', { name: 'Connect wallet', exact: true }).click()
  await page.getByRole('button', { name: 'Create a new wallet' }).click()
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByLabel('Confirm password').fill(password)
  await page.getByRole('button', { name: 'Create encrypted wallet' }).click()
  await page.getByLabel(/I saved the encrypted backup/).check()
  await page.getByRole('button', { name: 'Choose a wallet' }).click()
  const walletB = await page.evaluate(async (original) => (await (await fetch('/api/v1/app-state')).json()).wallets.find((w: { address: string }) => w.address !== original).address as string, walletA)
  await page.getByRole('button', { name: `Unlock PumpWizard Wallet ${walletB}`, exact: true }).click()
  await page.getByLabel('Wallet password').fill(password)
  await page.getByRole('button', { name: 'Unlock & sign in' }).click()
  await page.locator('.welcome-dialog').getByRole('button', { name: 'Skip', exact: true }).click()
  await expect(board.getByLabel('Assistant message')).toBeEnabled()
  await expect(page.locator('.trading-focus-header')).toHaveCount(0)
  await expect(page.getByRole('tab', { name: /Overview/ })).toHaveAttribute('aria-selected', 'true')
  await page.getByRole('button', { name: 'Add session' }).click()
  await page.getByRole('dialog', { name: 'Create session' }).getByRole('button', { name: /LEARN FIRST Monitor/ }).click()
  const createdMonitor = page.waitForResponse((r) => r.url().endsWith('/api/v1/session-workspaces') && r.request().method() === 'POST')
  await page.getByRole('button', { name: 'Confirm monitoring session', exact: true }).click()
  const newMonitor = (await (await createdMonitor).json()).session
  await page.getByRole('button', { name: `Add strategy to ${newMonitor.label}`, exact: true }).click()
  const picker = page.getByRole('dialog', { name: 'Local strategy library' })
  await expect(picker).toContainText('No saved strategies in this wallet')
  await expect(picker).not.toContainText('Wallet A saved rules')
  await expect(picker.getByRole('button', { name: /^Current|^History/ })).toHaveCount(0)
  await expect(picker.getByRole('button', { name: /Create strategy/ })).toBeVisible()
  await expect(picker.getByRole('button', { name: /Import JSON/ })).toBeVisible()
  await picker.getByRole('button', { name: 'Public Lab Data', exact: true }).click()
  await expect(picker.getByRole('region', { name: 'Public Lab Data' })).toBeVisible()
  await expect(page.getByRole('dialog')).toHaveCount(1)
  await picker.getByRole('button', { name: /My Strategies/ }).first().click()
  await page.getByLabel('Import strategy configuration').setInputFiles({ name: 'from-wallet-a.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(exportedRules)) })
  await expect(picker.getByRole('article').filter({ hasText: 'Wallet A saved rules' })).toBeVisible()
  await picker.screenshot({ path: testInfo.outputPath('wallet-strategy-picker.png') })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(picker.getByRole('button', { name: /Import JSON/ })).toBeVisible()
  expect(await picker.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true)
  await picker.screenshot({ path: testInfo.outputPath('wallet-strategy-picker-mobile.png') })
  await page.setViewportSize({ width: 1280, height: 720 })
  await picker.getByRole('button', { name: 'Close dialog' }).click()
  await expect(board).not.toContainText('Wallet A private conversation')
  await expect(page.getByText('Wallet A research', { exact: true })).toHaveCount(0)
  await expect(page.locator(`[data-session-id="${fixture.id}"]`)).toHaveCount(0)
  expect(await page.evaluate(async ({ sender, id }) => {
    const q = new URLSearchParams({ sender, network: 'mainnet-beta' })
    return (await (await fetch(`/api/v1/session-workspaces?${q}`)).json()).rows.some((row: { id: string }) => row.id === id)
  }, { sender: walletB, id: ownWorkspace })).toBe(false)
  await (await openWalletActions(page)).getByRole('button', { name: 'Sign out of wallet', exact: true }).click()
  await page.getByRole('button', { name: 'Connect wallet', exact: true }).click()
  await page.getByRole('button', { name: `Unlock PumpWizard Wallet ${walletA}`, exact: true }).click()
  await page.getByLabel('Wallet password').fill(password)
  await page.getByRole('button', { name: 'Unlock & sign in' }).click()
  await page.locator('.welcome-dialog').getByRole('button', { name: 'Skip', exact: true }).click()
  await expect(page.locator(`[data-session-id="${fixture.id}"]`)).toBeVisible()
  await expect(page.getByText('Wallet A research', { exact: true }).first()).toBeVisible({ timeout: 15_000 })
  await board.getByRole('button', { name: 'Read selected chat' }).click()
  await expect(board).toContainText('Wallet A private conversation')
})
