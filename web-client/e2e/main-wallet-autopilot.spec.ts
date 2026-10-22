import { resetSavedAccounts } from './savedAccounts'
import { expect, test } from '@playwright/test'
import nacl from 'tweetnacl'

test.beforeEach(async ({ page }) => { await resetSavedAccounts(page) })

const password = 'autopilot synthetic wallet password'

async function openGeneratedWalletDashboard(page: import('@playwright/test').Page) {
  await page.goto('/')
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
  await expect(page.getByRole('main', { name: 'Wallet dashboard' })).toBeVisible()
}

for (const kind of ['generated', 'private-key'] as const) {
  test(`wallet-approved auto trading enrolls and starts a ${kind} wallet`, async ({ page }) => {
    await page.goto('/')
    const identity = await page.evaluate(async () => (await (await fetch('/api/v1/service/status')).json()).build_identity)
    test.skip(identity.package_kind === 'source', 'Live wallet approval binds the installed package.')
    if (kind === 'generated') await openGeneratedWalletDashboard(page)
    else {
      const pair = nacl.sign.keyPair.fromSeed(new Uint8Array(32).fill(117))
      await page.getByRole('button', { name: 'Sign in with private key or recovery phrase' }).click()
      await page.getByLabel('Solana private key', { exact: true }).fill(JSON.stringify([...pair.secretKey]))
      await page.getByRole('button', { name: 'Review wallet', exact: true }).click()
      await page.getByLabel('New wallet password', { exact: true }).fill(password)
      await page.getByLabel('Confirm wallet password', { exact: true }).fill(password)
      await page.getByRole('button', { name: 'Save & sign in' }).click()
      await page.locator('.welcome-dialog').getByRole('button', { name: 'Skip', exact: true }).click()
    }
    const seed = await page.evaluate(async () => {
      const collection = await (await fetch('/api/v1/app-state')).json()
      const sender = collection.wallets[0].address
      const sessionResponse = await fetch('/api/v1/test-strategy', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sender, service: true, workspace: true, label: 'Wallet-approved fixture', policy_positions: 5, strategy_positions: 2 }),
      })
      if (!sessionResponse.ok) throw new Error(await sessionResponse.text())
      const session = await sessionResponse.json()
      const runtimeResponse = await fetch('/api/v1/test-service-runtime', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'wallet-approved' }),
      })
      if (!runtimeResponse.ok) throw new Error(await runtimeResponse.text())
      return { session, sender, before: JSON.stringify(collection.wallets[0]) }
    })
    try {
      await page.reload()
      // Reload restores the public wallet view; re-open its signing session explicitly.
      await page.getByRole('button', { name: 'Unlock signing', exact: true }).click()
      const unlock = page.getByRole('dialog', { name: 'Unlock wallet signing' })
      await unlock.getByLabel('Wallet password').fill(password)
      await unlock.getByRole('button', { name: 'Unlock signing', exact: true }).click()
      const row = page.locator(`[data-session-id="${seed.session.id}"]`)
      await row.getByRole('button', { name: 'Manage Wallet-approved fixture' }).click()
      await row.getByRole('button', { name: 'Open monitor' }).click()
      await page.getByRole('dialog', { name: 'Session monitor' }).getByRole('button', { name: 'Set up auto trading' }).click()
      const dialog = page.getByRole('dialog', { name: 'Background service controls' })
      await dialog.getByRole('button', { name: 'Set up trading wallet' }).click()
      const enrollment = dialog.getByRole('group', { name: 'Enroll local wallet for auto trading' })
      const servicePassword = 'wallet-approved fixture service password'
      await enrollment.getByLabel('Current wallet password').fill(password)
      await enrollment.getByLabel('New service wallet password').fill(servicePassword)
      await enrollment.getByLabel('Confirm service wallet password').fill(servicePassword)
      await enrollment.getByRole('checkbox').check()
      await enrollment.getByRole('button', { name: 'Encrypt & enroll wallet' }).click()
      await dialog.getByRole('button', { name: 'Review exact allowance' }).click()
      await expect(dialog).toContainText('Review ready')
      await dialog.getByLabel('Service allowance confirmation').fill(`ALLOW SERVICE ${seed.session.id.slice(0, 8).toUpperCase()}`)
      await dialog.getByRole('button', { name: 'Authorize inactive allowance' }).click()
      await expect(dialog.getByRole('button', { name: 'Start auto trading', exact: true })).toBeDisabled()
      const approval = dialog.getByRole('group', { name: 'Approve auto trading' })
      await approval.getByRole('button', { name: 'Approve auto trading', exact: true }).click()
      await expect(dialog).toContainText('Wallet-approved auto trading ready')
      const beforeStart = await page.evaluate(async (sender) => (await fetch('/api/v1/test-chain', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender }),
      })).json(), seed.sender)
      expect(beforeStart.sent).toBe(0)
      await dialog.getByRole('button', { name: 'Start auto trading', exact: true }).click()
      const start = dialog.getByRole('group', { name: 'Start auto trading', exact: true })
      await start.getByLabel('Service wallet password').fill(servicePassword)
      await start.getByLabel('Service start confirmation').fill(`START ${seed.session.id.slice(0, 8).toUpperCase()}`)
      await start.getByRole('checkbox').check()
      await start.getByRole('button', { name: 'Confirm auto-trading start' }).click()
      await expect(dialog).toContainText('Main Wallet Service · Unlocked')
      const live = await page.evaluate(async () => (await fetch('/api/v1/service/status')).json())
      expect(live.trading.production_activation).toBe('active')
      expect(live.trading.activation_capability.mode).toBe('acceptance')
      expect(live.jobs.find((job: { id: string }) => job.id === 'strategy-monitor').enabled).toBe(true)
      expect(live.trading.activation_capability.approval.routes).toHaveLength(4)
      const scoped = await page.evaluate(async (sender) => (await (await fetch(`/api/v1/service/signing?sender=${sender}&network=mainnet-beta`)).json()).allowances, seed.sender)
      expect(scoped.find((allowance: { session_id: string }) => allowance.session_id === seed.session.id).scope.max_open_positions).toBe(2)
      const stored = await page.evaluate(async () => ({
        collection: await (await fetch('/api/v1/app-state')).json(), storage: JSON.stringify({ ...localStorage, ...sessionStorage }),
      }))
      expect(JSON.stringify(stored.collection.wallets[0])).toBe(seed.before)
      expect(stored.storage).not.toContain(servicePassword)
    } finally {
      await page.evaluate(async () => {
        await fetch('/api/v1/service/revoke-trading', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
        await fetch('/api/v1/test-service-runtime', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'synthetic' }) })
      })
    }
  })
}

async function openSelectedStrategyControls(page: import('@playwright/test').Page) {
  if (await page.getByRole('main', { name: 'Wallet dashboard' }).isVisible()) {
    await page.locator('.trading-focus-header').getByRole('button', { name: 'Strategy controls', exact: true }).click()
  }
  await expect(page.getByRole('dialog', { name: 'Session monitor' })).toBeVisible()
}

for (const variant of [
  { label: 'Interactive funded', mode: 'Trade · Interactive', flags: {}, legacy: false },
  { label: 'Service Autopilot funded', mode: 'Trade · Full Autopilot', flags: { service: true }, legacy: false },
  { label: 'Legacy Autopilot funded', mode: 'Trade · Full Autopilot', flags: { autopilot: true }, legacy: true },
]) {
  test(`funded ${variant.label} strategy omits paper controls and retains its allocation`, async ({ page }) => {
    await openGeneratedWalletDashboard(page)
    if (variant.legacy) await page.route('**/api/v1/sessions**', async (route) => {
      if (route.request().method() !== 'GET') return route.continue()
      const response = await route.fetch({ headers: { ...route.request().headers(), Origin: new URL(route.request().url()).origin } })
      const data = await response.json()
      const sessions = Array.isArray(data.rows) ? data.rows : [data]
      for (const session of sessions) if (session.brief) delete session.brief.session_mode
      await route.fulfill({ response, json: data })
    })
    const session = await page.evaluate(async ({ flags, label }) => {
      const collection = (await (await fetch('/api/v1/app-state')).json()) as { wallets: { address: string }[] }
      const response = await fetch('/api/v1/test-strategy', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sender: collection.wallets[0].address, workspace: true, label, ...flags }),
      })
      if (!response.ok) throw new Error(await response.text())
      return response.json() as Promise<{ id: string }>
    }, variant)
    await page.reload()
    const row = page.locator(`[data-session-id="${session.id}"]`)
    await row.getByRole('button', { name: `Manage ${variant.label}`, exact: true }).click()
    const actions = page.getByRole('group', { name: `${variant.label} session actions`, exact: true })
    await expect(actions.getByRole('button', { name: 'Paper trading', exact: true })).toHaveCount(0)
    await actions.getByRole('button', { name: 'Open monitor', exact: true }).click()
    const monitor = page.getByRole('dialog', { name: 'Session monitor', exact: true })
    await expect(monitor).toContainText(variant.mode)
    await expect(monitor.getByRole('button', { name: 'Paper trading', exact: true })).toHaveCount(0)
    await expect(monitor.getByText('Paper budget', { exact: true })).toHaveCount(0)
    await expect(monitor.locator('.session-brief-summary')).toContainText('0.3 SOL shared by session')
    await expect(monitor.getByRole('button', { name: 'Review session evidence', exact: true })).toBeVisible()
    await expect(monitor.getByRole('button', { name: 'Review strategy orders', exact: true })).toBeVisible()
    if (variant.flags.service) await expect(monitor.getByRole('button', { name: 'Set up auto trading', exact: true })).toBeVisible()
    if (variant.flags.autopilot) await expect(monitor.getByRole('button', { name: 'Review Autopilot allowance', exact: true })).toBeVisible()
    const research = await page.evaluate(async (sourceSessionId) => {
      const snapshot = await (await fetch(`/api/v1/research/sessions/${sourceSessionId}`)).json()
      const blocked = await fetch('/api/v1/research/paper-runs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ schema: 'pumpwizard.paper-start-request/v1', source_session_id: sourceSessionId,
          strategy_sha256: snapshot.source_strategy_sha256, starting_capital_usd: 500,
          confirmation: 'START PAPER RESEARCH', idempotency_key: 'funded-paper-rejection' }),
      })
      const freeze = await fetch('/api/v1/research/datasets/freeze', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ schema: 'pumpwizard.research-freeze-request/v1', source_session_id: sourceSessionId,
          idempotency_key: 'funded-evidence-review' }),
      })
      if (!freeze.ok) throw new Error(await freeze.text())
      return { allowed: snapshot.paper_research_allowed, status: blocked.status, error: await blocked.text() }
    }, session.id)
    expect(research.allowed).toBe(false)
    expect(research.status).toBe(422)
    expect(research.error).toContain('only in monitoring sessions')
    await monitor.getByRole('button', { name: 'Review session evidence', exact: true }).click()
    const review = page.getByRole('dialog', { name: `Research workspace for ${variant.label}`, exact: true })
    await expect(review.getByRole('navigation', { name: 'Research workspace sections' })).toBeVisible()
    await expect(review.getByRole('button', { name: 'Forward paper', exact: true })).toHaveCount(0)
    await expect(review.getByRole('button', { name: 'Campaigns', exact: true })).toHaveCount(0)
    await expect(review.getByRole('button', { name: 'history', exact: true })).toBeVisible()
    await expect(review.getByRole('button', { name: 'results', exact: true })).toBeVisible()
    const bank = await page.evaluate(async () => {
      const collection = (await (await fetch('/api/v1/app-state')).json()) as { wallets: { address: string }[] }
      return (await fetch('/api/v1/test-chain', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sender: collection.wallets[0].address }),
      })).json()
    })
    expect(bank.sent).toBe(0)
  })
}

test('chooses full Autopilot and runs the synthetic exact-ticket signer end to end', async ({ page }) => {
  await openGeneratedWalletDashboard(page)
  await page.getByRole('button', { name: 'Add session' }).click()
  const wizard = page.getByRole('dialog', { name: 'Create session' })
  await wizard.getByRole('button', { name: /Trading session/ }).click()

  const autopilot = wizard.getByRole('button', { name: /Full Autopilot/ })
  await expect(autopilot).toBeEnabled()
  await autopilot.click()
  await expect(wizard).toContainText('even when the dashboard is closed')
  await expect(wizard).toContainText('RUNS IN THE BACKGROUND SERVICE')
  await expect(wizard).toContainText('Requires a PumpWizard wallet enrolled with the service')
  await expect(wizard.getByRole('button', { name: /Automatic entries only/ })).toBeEnabled()
  await expect(wizard).not.toContainText('Entry-only automation is intentionally unavailable')
  await expect(wizard).not.toContainText('isolated Automation Wallet')

  await wizard.getByRole('button', { name: 'Continue', exact: true }).click()
  await wizard.getByLabel('Session allocation (SOL)').fill('0.2')
  await expect(wizard).toContainText('STEP 3 OF 3')
  await expect(wizard.getByRole('button', { name: 'Create session', exact: true })).toBeEnabled()

  const session = await page.evaluate(async () => {
    const collection = (await (await fetch('/api/v1/app-state')).json()) as { wallets: { address: string }[] }
    const response = await fetch('/api/v1/test-strategy', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sender: collection.wallets[0].address, autopilot: true }),
    })
    if (!response.ok) throw new Error(await response.text())
    return response.json() as Promise<{ id: string }>
  })
  await wizard.getByRole('button', { name: 'Close dialog' }).click()
  const sessionRow = page.locator(`[data-session-id="${session.id}"]`)
  await expect(sessionRow.getByRole('button', { name: 'Manage Autopilot browser fixture' })).toBeVisible({ timeout: 15_000 })
  await sessionRow.locator('.rail-row').click()
  await openSelectedStrategyControls(page)
  const monitor = page.getByRole('dialog', { name: 'Session monitor' })
  await expect(monitor).toContainText('main-wallet allowance')
  await monitor.getByRole('button', { name: 'Review Autopilot allowance' }).click()
  const allowance = page.getByRole('dialog', { name: 'Main-wallet Autopilot allowance' })
  await expect(allowance).toContainText('Authorization is separate from session creation and activation')
  await allowance.getByRole('button', { name: 'Review current allowance' }).click()
  await expect(allowance).toContainText('Exact durable scope')
  await expect(allowance).toContainText('Synthetic acceptance runtime available')
  const phrase = `ALLOW ${session.id.slice(0, 8).toUpperCase()}`
  await allowance.getByLabel(new RegExp(phrase)).fill(phrase)
  await allowance.getByRole('button', { name: 'Authorize inactive allowance' }).click()
  await expect(allowance).toContainText('authorized')
  await allowance.getByRole('button', { name: 'Start synthetic Autopilot' }).click()
  await expect(allowance).toContainText('Original signature recorded for reconciliation', { timeout: 15_000 })
  await expect(allowance).toContainText('1 / 5 orders')
  await expect(allowance.getByRole('button', { name: 'Pause entries' })).toBeVisible()
  await allowance.getByRole('button', { name: 'Stop', exact: true }).click()
  await expect(allowance).toContainText('Autopilot stopped by the user')
})


test('automatic entries wizard clearly assigns every exit to the user', async ({ page }) => {
  await openGeneratedWalletDashboard(page)
  await page.getByRole('button', { name: 'Add session' }).click()
  const wizard = page.getByRole('dialog', { name: 'Create session' })
  await wizard.getByRole('button', { name: /Trading session/ }).click()
  await wizard.getByRole('button', { name: /Automatic entries only/ }).click()
  await expect(wizard).toContainText('Strategy buys · you sell')
  await expect(wizard).toContainText('maximum-hold sells will not run automatically')
  await wizard.getByRole('button', { name: 'Continue', exact: true }).click()
  await wizard.getByLabel('Session allocation (SOL)').fill('0.2')
  await expect(wizard).toContainText('STEP 3 OF 3')
  await expect(wizard.getByRole('button', { name: 'Create session', exact: true })).toBeEnabled()
})


test('new automatic sessions persist service custody without a browser option', async ({ page }) => {
  await openGeneratedWalletDashboard(page)
  await page.getByRole('button', { name: 'Add session' }).click()
  const wizard = page.getByRole('dialog', { name: 'Create session' })
  await wizard.getByRole('button', { name: /Trading session/ }).click()
  await expect(wizard).toContainText('Monitoring continues when you close the dashboard')
  await expect(wizard.getByRole('button', { name: /This tab only/ })).toHaveCount(0)
  await wizard.getByRole('button', { name: /Automatic entries only/ }).click()
  await wizard.getByRole('button', { name: 'Continue', exact: true }).click()
  await wizard.getByLabel('Session allocation (SOL)').fill('0.2')
  const created = page.waitForResponse((response) => response.url().includes('/api/v1/session-workspaces') && response.request().method() === 'POST')
  await wizard.getByRole('button', { name: /Create.*session|Choose strategies/ }).click()
  const response = await created
  expect(response.ok()).toBeTruthy()
  const request = response.request().postDataJSON()
  expect(request.plan.custody_mode).toBe('main-wallet-service')
  expect(request.plan.permissions.custody).toBe('main-wallet-service')
  expect(request.plan.permissions.continues_without_browser).toBe(true)
  expect(request.plan.permissions.automatic_protective_exits).toBe(false)
})


test('session allocation creates only the funded workspace and rejects invalid amounts', async ({ page }) => {
  await openGeneratedWalletDashboard(page)
  await page.getByRole('button', { name: 'Add session' }).click()
  const wizard = page.getByRole('dialog', { name: 'Create session' })
  await wizard.getByRole('button', { name: /Trading session/ }).click()
  await wizard.getByRole('button', { name: 'Continue', exact: true }).click()
  for (const value of ['0', '-1', '999']) {
    await wizard.getByLabel('Session allocation (SOL)').fill(value)
    await expect(wizard.getByRole('button', { name: 'Create session', exact: true })).toBeDisabled()
  }
  await wizard.getByLabel('Session allocation (SOL)').fill('0.2')
  const response = page.waitForResponse((r) => r.url().endsWith('/api/v1/session-workspaces') && r.request().method() === 'POST')
  await wizard.getByRole('button', { name: 'Create session', exact: true }).click()
  const data = await (await response).json()
  expect(data.session.strategy_count).toBe(0)
  expect(data.session.allocation_lamports).toBe('200000000')
  await expect(wizard).toHaveCount(0)
  await expect(page.getByRole('dialog', { name: 'Local strategy library' })).toHaveCount(0)
  await expect(page.getByRole('article', { name: `${data.session.label} session`, exact: true })).toContainText('0.2 SOL allocated')
})


test('adds public strategy directly and edits its session rules and limits', async ({ page }, testInfo) => {
  await openGeneratedWalletDashboard(page)
  await page.getByRole('button', { name: 'Add session' }).click()
  const wizard = page.getByRole('dialog', { name: 'Create session' })
  await wizard.getByRole('button', { name: /Trading session/ }).click()
  await wizard.getByRole('button', { name: 'Continue', exact: true }).click()
  await wizard.getByLabel('Session allocation (SOL)').fill('0.2')
  await wizard.getByRole('button', { name: 'Create session', exact: true }).click()
  await page.locator('.rail-workspace-item').filter({ hasText: '0.2 SOL allocated' }).getByRole('button', { name: /Add strategy to/ }).click()
  const library = page.getByRole('dialog', { name: 'Local strategy library' })
  await library.getByRole('button', { name: 'Public Lab Data', exact: true }).click()
  const presets = library.getByRole('region', { name: 'Public Lab Data' })
  await presets.getByLabel('Search simulations').fill('early-frequency')
  const addedResponse = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/v1/sessions' && r.request().method() === 'POST')
  await presets.getByRole('button', { name: 'Add to session & edit' }).click()
  const added = await addedResponse
  expect(added.ok()).toBe(true)
  const attached = await added.json()
  const editor = library.getByRole('form', { name: 'Edit saved strategy' })
  await expect(editor).toContainText('Budget comes from this session’s allocated SOL')
  await expect(editor.getByLabel('Research budget (USD)', { exact: true })).toHaveCount(0)
  await expect(editor.locator('.strategy-budget-summary')).toContainText('0.2 SOL')
  await expect(editor.locator('.strategy-budget-summary')).toContainText('$20.00')
  await editor.getByLabel('Strategy name', { exact: true }).fill('My edited wallet strategy')
  await editor.getByLabel('Trade size (% of strategy budget)', { exact: true }).fill('5')
  await expect(editor.locator('.strategy-budget-summary')).toContainText('0.01 SOL')
  await editor.getByLabel('Strategy maximum positions', { exact: true }).fill('0')
  await editor.getByRole('button', { name: 'Save session strategy' }).click()
  await expect(editor.getByRole('alert')).toContainText('positive whole numbers')
  await editor.getByLabel('Strategy maximum positions', { exact: true }).fill('3')
  await editor.getByRole('button', { name: 'Confirmation', exact: true }).click()
  await editor.getByLabel('Observation window (minutes)', { exact: true }).fill('1.5')
  await editor.getByRole('button', { name: 'Exits & targets', exact: true }).click()
  await editor.getByLabel('Stop loss (%)', { exact: true }).fill('12')
  await editor.getByLabel('Maximum hold (hours)', { exact: true }).fill('2')
  await editor.getByLabel('Target 1 sell (%)', { exact: true }).fill('35')
  await editor.getByRole('button', { name: 'Budget & trades', exact: true }).click()
  await editor.getByRole('heading', { name: 'My edited wallet strategy' }).scrollIntoViewIfNeeded()
  await page.screenshot({ path: testInfo.outputPath('saved-strategy-editor.png') })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await library.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('saved-strategy-editor-mobile.png') })
  await page.setViewportSize({ width: 1280, height: 720 })
  const saveResponse = page.waitForResponse((r) => new URL(r.url()).pathname.endsWith('/revisions') && r.request().method() === 'POST')
  await editor.getByRole('button', { name: 'Save session strategy' }).click()
  const saved = await saveResponse
  expect(saved.ok()).toBe(true)
  const submitted = saved.request().postDataJSON()
  expect(submitted.sizing).toMatchObject({ trade_percent: '5', allocation_lamports: '200000000' })
  expect(submitted.configuration).toMatchObject({ max_positions: 3, observation_seconds: 90, stop_loss_fraction: -.12, max_hold_seconds: 7200 })
  expect(submitted.configuration.targets[0].initial_fraction).toBe(.35)
  const result = await saved.json()
  expect(result.strategy.package.configuration).toMatchObject({ budget_usd: 20, order_usd: 1 })
  expect(result.sizing.trade_budget_sol).toBe('0.01')
  const revision = result.strategy.digest
  expect(result.instance_id).toBe(attached.instance.id)
  const detail = await page.evaluate(async (url) => (await fetch(url)).json(), `${new URL(added.request().url()).origin}/api/v1/sessions/${attached.instance.id}${new URL(added.request().url()).search}`)
  expect(detail.instance.strategy_digest).toBe(revision)
  expect(detail.instance.entry_mode).toBe('disabled')
  expect(detail.brief.paper_budget_usd).toBe(20)
  expect(detail.risk_policy.max_trade_lamports).toBe(10000000)
  await expect(editor).toHaveCount(0)
  await expect(library.getByRole('heading', { name: 'My edited wallet strategy' })).toBeVisible()
  await expect(library).toContainText('No attributed simulation report')
  await library.getByText('Initial order limits', { exact: true }).click()
  await library.getByRole('button', { name: 'Edit order limits', exact: true }).click()
  const limits = library.getByRole('form', { name: 'Edit initial order limits' })
  await limits.getByLabel('Maximum buys per day').fill('2')
  await limits.getByLabel('Maximum open positions').fill('1')
  await expect(limits.getByLabel('Maximum buy (SOL)', { exact: true })).toHaveValue('0.01')
  await expect(limits.getByLabel('Maximum buy (SOL)', { exact: true })).toHaveAttribute('readonly', '')
  await limits.getByRole('button', { name: 'Apply initial limits' }).click()
  await library.getByText('Initial order limits', { exact: true }).click()
  await expect(library).toContainText('2 trades/day · 1 positions')
  await expect(library.getByRole('region', { name: 'Trading setup' })).toContainText('Added to this session')
  await expect(library.getByRole('button', { name: 'Edit session strategy', exact: true })).toBeVisible()
  await expect(library.getByRole('button', { name: 'Add to session & edit', exact: true })).toHaveCount(0)
  const controls = await page.evaluate(async (url) => (await fetch(url)).json(), `/api/v1/sessions/${attached.instance.id}${new URL(added.request().url()).search}`)
  expect(controls.risk_policy).toMatchObject({ max_trades_per_day: 2, max_open_positions: 1, max_trade_lamports: 10000000 })
  const tampered = added.request().postDataJSON()
  tampered.digest = revision
  tampered.funding.max_trade_sol = '0.02'
  const rejected = await page.evaluate(async ({ url, body }) => {
    const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    return { status: response.status, result: await response.json() }
  }, { url: added.request().url(), body: tampered })
  expect(rejected.status).toBe(400)
  expect(rejected.result.error).toContain('saved percentage trade budget')
  await library.getByRole('button', { name: 'Public Lab Data', exact: true }).click()
  await presets.getByLabel('Search simulations').fill('early-frequency')
  await expect(presets.getByRole('button', { name: 'Already in this session' })).toBeDisabled()
  await page.screenshot({ path: testInfo.outputPath('public-lab-session-flow.png') })
  await library.getByRole('button', { name: 'Close dialog' }).click()
  await expect(library).toHaveCount(0)
  const dashboard = page.getByRole('region', { name: 'Trading dashboard' })
  await expect(dashboard.locator('.trading-focus-header')).toContainText('My edited wallet strategy')
  await expect(dashboard.getByRole('tab', { name: /Activity/ })).toBeVisible()
  await expect(dashboard.getByRole('button', { name: /New order/i })).toHaveCount(0)
})

test('session pause and sell-all retain attribution and finalize one reviewed liquidation', async ({ page }, testInfo) => {
  await openGeneratedWalletDashboard(page)
  const source = await page.evaluate(async () => {
    const sender = (await (await fetch('/api/v1/app-state')).json()).wallets[0].address
    const response = await fetch('/api/v1/test-strategy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender, workspace: true }) })
    if (!response.ok) throw new Error(await response.text())
    return response.json() as Promise<{ id: string; workspace_id: string }>
  })
  const rail = page.locator(`[data-session-id="${source.id}"]`)
  await expect(rail).toBeVisible({ timeout: 15_000 })
  const folder = page.getByRole('article', { name: 'Controls fixture session', exact: true })
  await expect(folder).toContainText('0.3 SOL allocated')
  await expect(folder.getByLabel('All-time P&L +0 SOL')).toBeVisible()
  const sessionHeader = folder.locator('.rail-workspace-header')
  const rowLayout = await sessionHeader.evaluate((element) => {
    const row = element.getBoundingClientRect()
    const controls = [...element.querySelectorAll('button, .rail-workspace-metrics')].map((child) => child.getBoundingClientRect())
    return { height: row.height, singleRow: controls.every((child) => child.top >= row.top && child.bottom <= row.bottom), fits: element.scrollWidth <= element.clientWidth }
  })
  expect(rowLayout).toMatchObject({ singleRow: true, fits: true })
  expect(rowLayout.height).toBeLessThan(100)
  expect((await page.locator('.trade-rail').boundingBox())!.width).toBeGreaterThanOrEqual(280)
  await folder.getByRole('button', { name: 'Collapse Controls fixture' }).click()
  await expect(rail).toBeHidden()
  await expect(folder.getByRole('button', { name: 'Review sells', exact: true })).toBeVisible()
  await folder.getByRole('button', { name: 'Expand Controls fixture' }).click()
  await rail.getByRole('button', { name: 'Manage Reviewed browser fixture', exact: true }).click()
  await page.getByRole('group', { name: 'Reviewed browser fixture session actions' }).getByRole('button', { name: 'Orders', exact: true }).click()
  await page.getByRole('dialog', { name: 'Strategy orders' }).getByRole('button', { name: 'Review entry' }).first().click()
  const review = page.getByRole('region', { name: 'New order' })
  await review.getByRole('button', { name: 'Review order' }).click()
  await review.getByRole('button', { name: 'Sign & submit' }).click()
  await expect(review.locator('.receipt-card')).toContainText('BUY · Pending')
  const control = async (changes: Record<string, boolean>) => page.evaluate(async (changes) => {
    const sender = (await (await fetch('/api/v1/app-state')).json()).wallets[0].address
    const response = await fetch('/api/v1/test-chain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender, changes }) })
    if (!response.ok) throw new Error(await response.text())
    return response.json()
  }, changes)
  await control({ finalize: true })
  await expect(review.getByRole('status', { name: 'Order status' })).toContainText('Buy finalized', { timeout: 15_000 })
  await rail.locator('.rail-row').click()
  await page.getByRole('tab', { name: /Positions/ }).click()
  const positions = rail.getByRole('list', { name: 'Open positions for Reviewed browser fixture' })
  await expect(positions.getByRole('button')).toHaveCount(1)
  await expect(positions).toContainText('Open')
  const positionRow = positions.getByRole('button')
  const positionId = await positionRow.getAttribute('data-position-id')
  await positionRow.click()
  await expect(page.locator(`[id="position-${positionId}"]`)).toBeFocused()
  await expect(page.getByRole('dialog', { name: /Unlock/ })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('session-tree-desktop.png') })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.locator('.trade-rail').scrollIntoViewIfNeeded()
  expect(await page.evaluate(async () => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  const narrowRow = await sessionHeader.evaluate((element) => ({ height: element.getBoundingClientRect().height, fits: element.scrollWidth <= element.clientWidth }))
  expect(narrowRow.fits).toBe(true)
  expect(narrowRow.height).toBeLessThan(100)
  await page.screenshot({ path: testInfo.outputPath('session-tree-mobile.png') })
  await page.setViewportSize({ width: 1280, height: 720 })
  const beforePause = await page.evaluate(async (id) => {
    const sender = (await (await fetch('/api/v1/app-state')).json()).wallets[0].address
    return (await fetch(`/api/v1/sessions/${id}?sender=${sender}&network=mainnet-beta`)).json()
  }, source.id)
  const paused = page.waitForResponse((r) => new URL(r.url()).pathname.endsWith('/pause-entries') && r.request().method() === 'POST')
  await folder.getByRole('button', { name: 'Ⅱ Pause entries', exact: true }).click()
  expect((await paused).ok()).toBe(true)
  const state = await page.evaluate(async (id) => {
    const sender = (await (await fetch('/api/v1/app-state')).json()).wallets[0].address
    return (await fetch(`/api/v1/sessions/${id}?sender=${sender}&network=mainnet-beta`)).json()
  }, source.id)
  expect(state.instance).toMatchObject({ status: 'monitoring', entry_mode: 'disabled', exit_mode: 'manual' })
  expect(state.tracking.enabled).toBe(beforePause.tracking.enabled)
  await expect(folder.getByLabel('Controls fixture positions')).toContainText('1 open')
  await folder.getByRole('button', { name: 'Review sells', exact: true }).click()
  const batch = page.getByRole('dialog', { name: 'Sell all session positions' })
  await expect(batch).toContainText('Strategy-managed')
  await page.screenshot({ path: testInfo.outputPath('session-controls.png') })
  await batch.getByRole('button', { name: 'Stop strategies & review sells' }).click()
  await expect(batch.getByRole('region', { name: 'New order' })).toBeVisible()
  await expect(batch.getByLabel('Quantity to sell')).toHaveAttribute('readonly', '')
  await expect(batch.getByRole('button', { name: 'Buy', exact: true })).toHaveCount(0)
  await batch.getByRole('button', { name: 'Review order' }).click()
  await expect(batch).toContainText('ORDER REVIEW')
  await batch.getByRole('button', { name: 'Sign & submit' }).click()
  await expect(batch.getByRole('button', { name: 'Back to remaining positions' })).toBeDisabled()
  await expect(batch.locator('.receipt-card').filter({ hasText: 'SELL · Pending' })).toBeVisible()
  await control({ finalize: true })
  await expect(batch.getByRole('status', { name: 'Order status' })).toContainText('Sell finalized', { timeout: 15_000 })
  await batch.getByRole('button', { name: 'Continue to remaining positions' }).click()
  await expect(batch).toContainText('No open live positions in this session')
  await batch.getByRole('button', { name: 'Done', exact: true }).click()
  await expect(folder).not.toContainText('P&L Unavailable')
  await expect(positions).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('session-liquidated.png') })
  const final = await page.evaluate(async () => {
    const sender = (await (await fetch('/api/v1/app-state')).json()).wallets[0].address
    return (await fetch(`/api/v1/trades?sender=${sender}&network=mainnet-beta`)).json()
  })
  expect(final.positions[0]).toMatchObject({ status: 'closed', agent_id: source.id, managed: false })
  expect(final.orders.filter((order: { status: string }) => order.status === 'finalized')).toHaveLength(2)
})

test('session and strategy focus isolate holdings and every order state by ownership', async ({ page }, testInfo) => {
  test.setTimeout(60_000)
  await openGeneratedWalletDashboard(page)
  const fixture = await page.evaluate(async () => {
    const sender = (await (await fetch('/api/v1/app-state')).json()).wallets[0].address
    const create = async (body: Record<string, unknown>) => {
      const response = await fetch('/api/v1/test-strategy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender, ...body }) })
      if (!response.ok) throw new Error(await response.text())
      return response.json() as Promise<{ id: string; workspace_id: string }>
    }
    const first = await create({ workspace: true, label: 'Focus momentum', workspace_label: 'Focus session' })
    const second = await create({ workspace_id: first.workspace_id, label: 'Focus breakout' })
    const other = await create({ workspace: true, label: 'Other strategy', workspace_label: 'Other session' })
    return { sender, first, second, other }
  })
  const mint = 'cGFhiC6Kgg3FpKzgVfwgscsWrtc4BaP2fzuXRQPizuN'
  const now = Date.now() / 1000
  let archived = false
  const position = (id: string, owner: string | null, status = 'open') => ({
    id, genesis: 'synthetic', sender: fixture.sender, mint, opened_at: now,
    quantity: '2000000', initial_quantity: '2000000', decimals: 6,
    remaining_cost: '1000000', original_cost: '1000000', realized_proceeds: '0', realized_cost: '0',
    transaction_fees: '0', realized_pnl: '0', status, last_signature: '', managed: false,
    agent_id: owner, entry_fx: null, sold_stages: [], peak_price_usd: null, last_usable_at: null,
    strategy_label: null, exit_plan: null,
  })
  const order = (id: string, owner: string | null, status = 'finalized', positionId: string | null = null, side = 'buy') => ({
    id, agent_id: owner, position_id: positionId, status, side, mint, amount: '1000000', decimals: 6,
    network: 'mainnet-beta', route: 'Synthetic', signature: '', created_at: now, detail: 'Synthetic focus fixture', receipt: null,
  })
  await page.route('**/api/v1/trades?**', async (route) => {
    if (new URL(route.request().url()).searchParams.get('sender') !== fixture.sender) return route.continue()
    await route.fulfill({ json: { sender: fixture.sender, network: 'mainnet-beta', withdrawals: [], pending: true,
      positions: [position('a-open', fixture.first.id), position('a-closed', fixture.first.id, 'closed'), position('b-open', fixture.second.id, archived ? 'closed' : 'open'), position('other-buy', fixture.other.id), position('manual-buy', null)],
      orders: [order('a-pending', fixture.first.id, 'pending'), order('a-open', fixture.first.id), order('a-closed', fixture.first.id), order('a-manual-sell', null, 'finalized', 'a-closed', 'sell'), order('b-failed', fixture.second.id, 'failed'), order('b-open', fixture.second.id), order('other-buy', fixture.other.id), order('manual-buy', null)],
    } })
  })
  const dashboard = page.getByRole('region', { name: 'Trading dashboard' })
  await dashboard.getByRole('tab', { name: /Overview/ }).click()
  await expect(dashboard.getByRole('tab')).toHaveCount(3)
  await expect(dashboard.getByRole('tab', { name: /Positions/ })).toHaveCount(0)
  const session = page.getByRole('article', { name: 'Focus session session', exact: true })
  await session.getByRole('button', { name: 'Select Focus session', exact: true }).click()
  await expect(dashboard.getByRole('heading', { name: 'Focus session', exact: true })).toBeVisible()
  await dashboard.getByRole('tab', { name: /Overview/ }).click()
  await expect(dashboard.getByRole('tab', { name: /Overview/ })).toHaveAttribute('aria-selected', 'true')
  await expect(dashboard.getByRole('button', { name: /New order/i })).toHaveCount(0)
  await expect(dashboard.getByRole('table', { name: 'Open positions preview' }).locator('tbody tr')).toHaveCount(2)
  await expect(dashboard.getByRole('table', { name: 'Open positions preview' })).toContainText('Focus momentum')
  await expect(dashboard.getByRole('table', { name: 'Open positions preview' })).toContainText('Focus breakout')
  await expect(dashboard.locator('[data-order-id="a-pending"]')).toContainText('Pending')
  await expect(dashboard.locator('[data-order-id="b-failed"]')).toContainText('Failed')
  await expect(dashboard.locator('[data-order-id="a-manual-sell"]')).toContainText('SELL')
  await expect(dashboard.locator('[data-order-id="other-buy"], [data-order-id="manual-buy"]')).toHaveCount(0)
  await session.getByRole('button', { name: 'Edit funds for Focus session' }).click()
  const funds = page.getByRole('dialog', { name: 'Edit session funds' })
  await expect(funds.locator('.session-funds-summary')).toContainText('0.3 SOL')
  await funds.getByRole('button', { name: 'Stop strategies to edit funds', exact: true }).click()
  await expect(funds).toContainText('Strategies stopped. You can now adjust funds.')
  await funds.getByRole('textbox', { name: 'Session allocation (SOL)' }).fill('0.4')
  await expect(funds.locator('.session-funds-preview')).toContainText('0.1 SOL')
  await funds.getByRole('button', { name: 'Update allocation', exact: true }).click()
  await expect(funds).toContainText('Session funds updated')
  await expect(funds.getByRole('textbox', { name: 'Session allocation (SOL)' })).toHaveValue('0.4')
  await page.screenshot({ path: testInfo.outputPath('session-funds.png') })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(funds.getByRole('button', { name: 'Done', exact: true })).toBeInViewport()
  expect(await page.evaluate(async () => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('session-funds-mobile.png') })
  await page.setViewportSize({ width: 1280, height: 720 })
  await funds.getByRole('textbox', { name: 'Session allocation (SOL)' }).fill('0.3')
  await expect(funds.locator('.session-funds-preview')).toContainText('Release to wallet')
  await funds.getByRole('button', { name: 'Update allocation', exact: true }).click()
  await expect(funds.getByRole('textbox', { name: 'Session allocation (SOL)' })).toHaveValue('0.3')
  await funds.getByRole('button', { name: 'Done', exact: true }).click()
  await expect(session.getByRole('button', { name: 'Edit funds for Focus session' })).toContainText('0.3 SOL')
  await dashboard.getByRole('tab', { name: /Activity/ }).click()
  await dashboard.getByRole('button', { name: 'Decisions', exact: true }).click()
  await expect(dashboard.locator('.session-observations')).toHaveCount(1)
  await expect(dashboard.getByLabel('Decision strategy')).toHaveCount(1)
  await dashboard.getByRole('tab', { name: /Overview/ }).click()
  await page.setViewportSize({ width: 2048, height: 1100 })
  await dashboard.scrollIntoViewIfNeeded()
  await page.screenshot({ path: testInfo.outputPath('session-focus.png') })
  await dashboard.getByRole('tab', { name: /Activity/ }).click()
  await expect(dashboard.locator('.trade-history-row')).toHaveCount(6)
  await expect(dashboard.locator('[data-order-id="a-pending"]')).toContainText('Focus momentum')
  await expect(dashboard.locator('[data-order-id="b-failed"]')).toContainText('Focus breakout')
  const firstRail = session.locator(`[data-session-id="${fixture.first.id}"]`)
  await firstRail.locator('.rail-row').click()
  await expect(page.getByRole('dialog', { name: 'Session monitor' })).toHaveCount(0)
  await expect(dashboard.getByRole('heading', { name: 'Focus momentum', exact: true })).toBeVisible()
  await expect(dashboard.getByRole('tab')).toHaveCount(4)
  await dashboard.getByRole('tab', { name: /Positions/ }).click()
  await expect(dashboard.locator('.terminal-position-row')).toHaveCount(1)
  await expect(dashboard.locator('[id="position-wallet-a-open"]')).toBeVisible()
  await expect(dashboard.locator('[id="position-wallet-b-open"], [id="position-wallet-other-buy"]')).toHaveCount(0)
  await dashboard.getByRole('tab', { name: /Activity/ }).click()
  await expect(dashboard.locator('.trade-history-row')).toHaveCount(4)
  await expect(dashboard.locator('[data-order-id="a-manual-sell"]')).toContainText('Focus momentum')
  await expect(dashboard.locator('[data-order-id="b-failed"]')).toHaveCount(0)
  await dashboard.getByRole('tab', { name: /Activity/ }).click()
  await dashboard.getByRole('button', { name: 'Decisions', exact: true }).click()
  await expect(dashboard.locator('.session-observations')).toHaveCount(1)
  await expect(dashboard.locator('.session-observations')).toContainText('Focus momentum')
  await expect(dashboard.getByRole('table', { name: 'Session entry observations' })).toBeVisible()
  await dashboard.getByRole('tab', { name: /Activity/ }).click()
  await expect(firstRail.locator('.rail-row')).toHaveAttribute('aria-pressed', 'true')
  await session.getByRole('button', { name: 'Collapse Focus session', exact: true }).click()
  await expect(dashboard.getByRole('heading', { name: 'Focus momentum', exact: true })).toBeVisible()
  await session.getByRole('button', { name: 'Expand Focus session', exact: true }).click()
  await dashboard.locator('.trading-focus-header').getByRole('button', { name: 'Strategy controls', exact: true }).click()
  await expect(page.getByRole('dialog', { name: 'Session monitor' })).toContainText('Focus momentum')
  await page.getByRole('dialog', { name: 'Session monitor' }).getByRole('button', { name: 'Close dialog' }).click()
  await page.screenshot({ path: testInfo.outputPath('strategy-focus-history.png') })
  await dashboard.getByRole('navigation', { name: 'Trading selection' }).getByRole('button', { name: 'Focus session', exact: true }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await dashboard.scrollIntoViewIfNeeded()
  expect(await page.evaluate(async () => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('session-focus-mobile.png') })
  await page.setViewportSize({ width: 1280, height: 720 })
  await dashboard.getByRole('tab', { name: /Positions/ }).click()
  await expect(dashboard.locator('.terminal-position-row')).toHaveCount(2)
  await dashboard.getByRole('button', { name: 'All wallet activity', exact: true }).click()
  await expect(dashboard.locator('.trading-focus-header')).toHaveCount(0)
  await expect(dashboard.getByRole('button', { name: /New order/i })).toBeVisible()
  await expect(dashboard.getByRole('tab')).toHaveCount(3)
  await expect(dashboard.getByRole('tab', { name: /Positions/ })).toHaveCount(0)
  await expect(dashboard.getByRole('tab', { name: /Overview/ })).toHaveAttribute('aria-selected', 'true')
  await expect(dashboard.getByRole('region', { name: 'Open positions preview' })).toHaveCount(0)
  await dashboard.getByRole('tab', { name: /Activity/ }).click()
  await expect(dashboard.locator('.trade-history-row')).toHaveCount(8)
  archived = true
  await page.evaluate(async ({ sender, first, second }) => {
    const query = new URLSearchParams({ sender, network: 'mainnet-beta' })
    const pause = await fetch(`/api/v1/session-workspaces/${first.workspace_id}/pause?${query}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    if (!pause.ok) throw new Error(await pause.text())
    const remove = await fetch(`/api/v1/sessions/${second.id}/remove?${query}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: second.id, release_allocation: true }) })
    if (!remove.ok) throw new Error(await remove.text())
  }, fixture)
  await expect(page.locator(`[data-session-id="${fixture.second.id}"]`)).toHaveCount(0, { timeout: 15_000 })
  await session.getByRole('button', { name: 'Select Focus session', exact: true }).click()
  await dashboard.getByRole('tab', { name: /Overview/ }).click()
  await expect(dashboard.getByRole('table', { name: 'Strategy comparison' })).not.toContainText('Focus breakout')
  await dashboard.getByRole('tab', { name: /Overview/ }).click()
  await dashboard.getByRole('button', { name: 'All history', exact: true }).click()
  await expect(dashboard.getByRole('table', { name: 'Strategy comparison' }).locator('tr').filter({ hasText: 'Focus breakout' })).toContainText('Archived')
  await dashboard.getByRole('button', { name: 'Focus breakout ↗', exact: true }).click()
  await dashboard.getByRole('tab', { name: /Activity/ }).click()
  await expect(dashboard.locator('.trading-focus-header')).toContainText('Archived strategy')
  await expect(dashboard.getByRole('button', { name: 'Strategy controls', exact: true })).toHaveCount(0)
  await expect(dashboard.locator('.trade-history-row')).toHaveCount(2)
  await expect(dashboard.locator('[data-order-id="b-failed"]')).toContainText('Focus breakout')
})
