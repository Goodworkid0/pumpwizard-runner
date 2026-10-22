import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import type { HoldingValuation, WalletPortfolioState } from '../src/useWalletPortfolio'
import type { PortfolioTradeState } from '../src/PortfolioWorkspace'
import { appendPortfolioSample, openPositionPnl, positionPnlReason, walletValuation, walletWorth, worthMovement } from '../src/portfolioMetrics'
import { aggregatePerformance, orderOwner, signedSol, strategyOperatingState, strategyResults } from '../src/dashboardData'
import type { PortfolioSession, SessionWorkspace } from '../src/PortfolioWorkspace'
import type { ServiceConnectionState } from '../src/serviceControl'
import { holdingGraphPoints, holdingPriceSeries } from '../src/holdingPriceHistory'
import { groupSavedStrategyVersions, sessionStrategyAssociation, strategyTradingSetup } from '../src/strategySetup'
import type { LocalStrategy } from '../src/StrategySelection'
import type { SessionPlan } from '../src/SessionPlanDialog'
import type { CoinDetailData } from '../src/TokenDetail'
import type { WalletAsset } from '../src/useWalletPortfolio'
import { resetSavedAccounts } from './savedAccounts'

function valuation(): HoldingValuation {
  return {
    schema: 'pumpwizard.holding-valuation/v1',
    quantity: { state: 'known', amount: '1000', unit: 'TOKEN', atoms: '1000000000', decimals: 6 },
    basis: { state: 'known', amount: '45', currency: 'USD', source: 'synthetic receipt' },
    mark: { state: 'available', price: '.05', currency: 'USD', per_unit: 'TOKEN', kind: 'retained_estimate', source: 'synthetic', observed_at: Date.now() / 1000, source_slot: 1, age_seconds: 0, slot_lag: 0, freshness: 'fresh', executable: false },
    current_value: { state: 'estimated', amount: '50', currency: 'USD', calculation: 'quantity × retained mark' },
    unrealized_result: { state: 'estimated_before_exit_costs', amount: '5', percent: '11.11', currency: 'USD' },
    realized_proceeds: { state: 'unknown', amount: null, currency: 'USD', profit_treatment: 'not profit' },
    executable_liquidation: { state: 'unavailable', amount: null, currency: 'USD', reason: 'review a fresh quote' },
    ownership: { purchase_attribution: 'session', purchase_owner_id: 'synthetic', current_management: 'session', manager_id: 'synthetic' },
    fees: { state: 'unknown', amount: null, currency: null, exit_costs_included: false },
    external_flows: { strategy_profit_treatment: 'excluded' }, blockers: [], authority: { quote: false, signer: false, orders: false, submission: false },
  }
}

test('portfolio metrics preserve unknown valuation and fixed history buckets', () => {
  const portfolio = { estimated_usd: 125, assets: [{ valuation: valuation() }], coverage_gaps: [] } as unknown as WalletPortfolioState
  expect(walletWorth(portfolio)).toBe(175)
  portfolio.assets[0].valuation.mark.freshness = 'stale'
  expect(walletWorth(portfolio)).toBeNull()
  expect(walletValuation(portfolio)).toMatchObject({ total: null, known: 125, partial: true, reason: '1 token price is stale' })
  portfolio.assets[0].valuation.mark.freshness = 'fresh'
  portfolio.coverage_gaps.push('Undecoded account')
  expect(walletWorth(portfolio)).toBeNull()
  expect(walletValuation(portfolio)).toMatchObject({ total: null, known: 175, partial: true })
  portfolio.estimated_usd = null
  expect(walletValuation(portfolio)).toMatchObject({ total: null, known: 50, partial: true })
  portfolio.assets[0].valuation.current_value.amount = null
  expect(walletValuation(portfolio)).toMatchObject({ total: null, known: null, partial: true })
  const trades = { positions: [{ status: 'open', valuation: valuation() }, { status: 'closed', valuation: valuation() }] } as unknown as PortfolioTradeState
  expect(openPositionPnl(trades)).toBe(5)
  trades.positions[0].valuation!.unrealized_result.state = 'unknown'
  expect(openPositionPnl(trades)).toBeNull()
  expect(openPositionPnl({ ...trades, positions: [] })).toBe(0)
  expect(openPositionPnl(null)).toBeNull()
  expect(positionPnlReason(null)).toBe('Trading records not loaded')
  expect(positionPnlReason({ ...trades, positions: [] })).toBe('No open Runner-recorded positions')
  trades.positions[0].valuation!.basis.source = 'missing_entry_fx'
  trades.positions[0].valuation!.mark.freshness = 'stale'
  expect(positionPnlReason(trades)).toBe('Purchase-time SOL/USD rate missing · Open-position price is stale')
  let rows = appendPortfolioSample([], { at: 900, usd: 20, pnl: -2 })
  rows = appendPortfolioSample(rows, { at: 950, usd: 21, pnl: -1 })
  expect(rows).toHaveLength(1)
  rows = appendPortfolioSample(rows, { at: 1800, usd: null, pnl: null })
  rows = appendPortfolioSample(rows, { at: 2700, usd: 22, pnl: 1 })
  expect(rows).toHaveLength(3)
  expect(rows[1].usd).toBeNull()
  expect(worthMovement(rows, 2700, 22)).toBeNull()
  expect(worthMovement([{ at: 1000, usd: 0, pnl: 0 }], 87400, 10)).toEqual({ usd: 10, percent: null })
})

async function signIn(page: Page) {
  await resetSavedAccounts(page)
  await page.goto('/')
  const password = 'portfolio dashboard synthetic password'
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
}

test('dashboard accounting and states preserve attribution, precision and monitoring boundaries', () => {
  expect(signedSol('9007199254740993')).toBe('+9007199.254740993 SOL')
  expect(signedSol('-1')).toBe('−0.000000001 SOL')
  expect(signedSol(null)).toBe('Unavailable')
  const session = { instance: { id: 'a', label: 'A', entry_mode: 'automatic' }, brief: { execution_style: 'autopilot' }, tracking: { enabled: 1, fault: '' } } as PortfolioSession
  const workspace = { id: 'w', mode: 'trade', wallet_sender: 'sender', wallet_network: 'mainnet-beta', strategy_ids: ['a'], history_strategies: [{ id: 'a', label: 'A', removed: false }, { id: 'old', label: 'Old', removed: true }], strategy_performance: [{ id: 'a', label: 'A', removed: false, performance: { state: 'unavailable', realized_lamports: '100', unrealized_lamports: null, total_lamports: null } }, { id: 'old', label: 'Old', removed: true, performance: { state: 'estimated', realized_lamports: '50', unrealized_lamports: '0', total_lamports: '50' } }] } as SessionWorkspace
  const rows = strategyResults([session], [workspace], null)
  expect(rows).toHaveLength(2)
  expect(strategyOperatingState(rows[0])).toBe('Awaiting approval')
  expect(strategyOperatingState(rows[1])).toBe('Archived')
  const trades = { positions: [{ id: 'position', agent_id: 'old' }], orders: [] } as unknown as PortfolioTradeState
  expect(orderOwner({ agent_id: null, position_id: 'position' } as PortfolioTradeState['orders'][number], trades)).toBe('old')
  expect(aggregatePerformance(rows, trades, new Set(['a', 'old']))).toMatchObject({ realized: '150', unrealized: null, total: null })
  expect(aggregatePerformance(rows, null, null).total).toBeNull()
  expect(strategyResults([session], [workspace], { kind: 'strategy', id: 'old' })).toHaveLength(1)
  const now = Date.now() / 1000
  const service = { phase: 'connected', identityMismatch: '', checked_at: now, service: { health: 'healthy' }, trading: { session_id: 'a', sender: 'sender', network: 'mainnet-beta', authorization_status: 'active', signer_state: 'unlocked', recovery_required: false, entries_paused: false, authorization_expires_at: now + 60, signer_expires_at: now + 60 } } as ServiceConnectionState
  expect(strategyOperatingState(rows[0], service)).toBe('Trading')
  expect(strategyOperatingState(rows[0], { ...service, checked_at: now - 30 })).toBe('Awaiting approval')
  expect(strategyOperatingState(rows[0], { ...service, trading: { ...service.trading!, network: 'devnet' } })).toBe('Awaiting approval')
  expect(strategyOperatingState(rows[0], { ...service, trading: { ...service.trading!, signer_state: 'locked' } })).toBe('Awaiting approval')
})

test('holding graph uses recorded prices and time spacing, never percentage placeholders', () => {
  const asset = { trend_points: [], price_change_24h: -99 } as unknown as WalletAsset
  expect(holdingPriceSeries(asset, null)).toEqual([])
  asset.trend_points = [{ at: 110, price_usd: 3 }, { at: 100, price_usd: 1 }, { at: 100, price_usd: 2 }, { at: 200, price_usd: 4 }, { at: NaN, price_usd: 4 }, { at: 250, price_usd: 0 }, { at: Date.now() / 1000 + 3600, price_usd: 6 }]
  const series = holdingPriceSeries(asset, null)
  expect(series).toEqual([{ at: 100, price_usd: 2 }, { at: 110, price_usd: 3 }, { at: 200, price_usd: 4 }])
  expect(holdingGraphPoints(series).map((row) => row.x)).toEqual([5, 24, 195])
  expect(holdingGraphPoints([series[0]])).toMatchObject([{ x: 100, y: 35 }])
  expect(holdingGraphPoints([{ at: 100, price_usd: .0000000001 }, { at: 200, price_usd: .0000000001 }]).map((row) => row.y)).toEqual([35, 35])
  const detail = { history: [], candles: [{ opened_at: 80, close_usd: '8' }], pool_histories: [{ address: 'pool-a', candles: [{ opened_at: 50, close_usd: '5' }] }, { address: 'pool-b', candles: [{ opened_at: 60, close_usd: '6' }] }] } as unknown as CoinDetailData
  expect(holdingPriceSeries(asset, detail, 'pool-b')).toEqual([{ at: 60, price_usd: 6 }])
})

test('strategy choices separate session membership, saved versions, funds and trading approval', () => {
  const current = { digest: 'current', strategy_id: 'same-id', label: 'Same name', compatibility: 'monitoring-ready', imported_at: '2026-10-01' } as LocalStrategy
  const revision = { ...current, digest: 'revision', imported_at: '2026-10-03' }
  const session = { instance: { id: 'live', workspace_id: 'w', strategy_digest: current.digest, label: 'Live', entry_mode: 'automatic' }, brief: { strategy_digest_at_creation: 'original', execution_style: 'autopilot' }, tracking: { enabled: 1, fault: '' } } as PortfolioSession
  const plan = { session_mode: 'trade', workspace_id: 'w', execution_style: 'autopilot' } as SessionPlan
  const workspace = { id: 'w', mode: 'trade', wallet_sender: 'sender', wallet_network: 'mainnet-beta', funding: { state: 'insufficient', checked_at: Date.now() / 1000, shortfall_lamports: '1000000' } } as SessionWorkspace
  expect(groupSavedStrategyVersions([revision, current], [session], 'w')[0].map((row) => row.digest)).toEqual(['current', 'revision'])
  expect(sessionStrategyAssociation([session], undefined, current.digest)).toBeNull()
  expect(sessionStrategyAssociation([session], 'w', 'original')).toBe(session)
  expect(sessionStrategyAssociation([session], 'other', current.digest)).toBeNull()
  const input = { strategy: current, session, workspace, plan, walletSigning: true }
  expect(strategyTradingSetup(input)).toMatchObject({ heading: 'Session needs funds', active: false })
  workspace.funding!.state = 'sufficient'
  expect(strategyTradingSetup(input)).toMatchObject({ heading: 'Trading approval required', active: false })
  expect(strategyTradingSetup({ ...input, plan: { ...plan, execution_style: 'interactive' } })).toMatchObject({ heading: 'Ready to review an order', active: false })
  expect(strategyTradingSetup({ ...input, plan: { ...plan, execution_style: 'interactive' }, walletSigning: false }).heading).toBe('Unlock wallet signing')
  workspace.funding!.checked_at = Date.now() / 1000 - 120
  expect(strategyTradingSetup(input).heading).toBe('Check session funds')
  expect(strategyTradingSetup({ ...input, plan: { ...plan, session_mode: 'monitoring' } }).heading).toBe('Monitoring only')
})

test('wallet holding graphs, artwork fallback and details work without trading actions', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1820, height: 1050 })
  await signIn(page)
  const mint = 'So11111111111111111111111111111111111111112'
  const missing = '11111111111111111111111111111111'
  const now = Math.floor(Date.now() / 1000)
  const prices = [{ at: now - 900, price_usd: .015 }, { at: now - 650, price_usd: .025 }, { at: now - 610, price_usd: .020 }, { at: now - 200, price_usd: .035 }, { at: now - 160, price_usd: .021 }, { at: now - 20, price_usd: .05 }]
  let recoveredIcon = false
  let imported = false
  const writes: string[] = []
  page.on('request', (request) => { if (request.method() === 'POST') writes.push(new URL(request.url()).pathname) })
  const asset = (address: string, symbol: string): WalletAsset => ({ mint: address, amount: '1000000000', decimals: 6, symbol, name: symbol === 'GRAPH' ? 'Graph coin' : 'Missing-history coin', untracked_atoms: '1000000000', runner_atoms: '0', runner_position_ids: [], program: 'SPL Token', token_program: 'synthetic', accounts: 1, website: null, image_url: `https://backpack.exchange/${address === mint ? 'valid' : recoveredIcon ? 'recovered' : 'missing'}.png`, creator: null, metadata_source: 'synthetic', created_at: null, price_usd: '.05', observed_at: now, price_source: 'synthetic', price_slot_lag: 0, liquidity_usd: 10000, market_cap_usd: 50000, price_change_24h: -80, volume_change_24h: null, holder_count: null, organic_score: null, organic_score_label: null, suspicious: false, top_holders_percent: null, trend_points: address === mint ? prices : [], estimated_value_usd: '50', valuation_state: 'priced', valuation_detail: 'Synthetic fixture valuation', valuation: valuation() })
  await page.route('**/api/v1/wallet/portfolio?**', async (route) => {
    const response = await route.fetch({ headers: { ...await route.request().allHeaders(), Origin: new URL(route.request().url()).origin, 'Sec-Fetch-Site': 'same-origin' } })
    const portfolio = await response.json() as WalletPortfolioState
    await route.fulfill({ response, json: { ...portfolio, assets: [asset(mint, 'GRAPH'), asset(missing, 'EMPTY')], coverage_gaps: [] } })
  })
  await page.route('**/api/v1/token-images/**', async (route) => {
    if (new URL(route.request().url()).pathname.endsWith(`/${missing}`) && !recoveredIcon) return route.fulfill({ status: 404, body: 'Artwork unavailable' })
    await route.fulfill({ status: 200, contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"><rect width="64" height="64" rx="16" fill="#7665ff"/><circle cx="32" cy="32" r="16" fill="#63e5c2"/></svg>' })
  })
  await page.route('**/api/v1/coins/*?**', async (route) => {
    const address = new URL(route.request().url()).pathname.split('/').pop()
    const rows = address === mint ? prices.slice().reverse().map((row) => ({ observed_at: row.at, price_usd: String(row.price_usd), mint: address, symbol: 'GRAPH', market_cap_usd: '50000', liquidity_usd: '10000' })) : []
    const candles = address === mint && imported ? Array.from({ length: 4 }, (_, index) => ({ source: 'synthetic', resolution_seconds: 14400, opened_at: now - (index + 1) * 14400, open_usd: '.01', high_usd: '.04', low_usd: '.009', close_usd: String(.015 + index * .002), volume_usd: '100' })) : []
    await route.fulfill({ json: { schema: 'pumpwizard.runner-web/v1', history: rows, candles, pool_histories: [], history_backfill: { status: address === mint && !imported ? 'not_started' : 'complete', row_count: candles.length, error: '', coverage: 'Synthetic history' }, activity: [], metadata: {}, mint: address } })
  })
  await page.route('**/api/v1/coins/*/history/backfill', async (route) => { if (route.request().url().includes(mint)) imported = true; await route.fulfill({ json: { job: { status: 'complete' } } }) })
  await page.getByRole('tab', { name: /Wallet/ }).click()
  const holdings = page.getByRole('region', { name: 'Current holdings' })
  const graphRow = holdings.locator('.portfolio-holding-row').filter({ hasText: 'Graph coin' })
  const emptyRow = holdings.locator('.portfolio-holding-row').filter({ hasText: 'Missing-history coin' })
  await expect(graphRow.getByRole('img', { name: 'Graph coin token artwork' })).toBeVisible()
  await graphRow.getByRole('img', { name: 'Graph coin token artwork' }).scrollIntoViewIfNeeded()
  await expect.poll(() => graphRow.getByRole('img', { name: 'Graph coin token artwork' }).evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(64)
  await expect(emptyRow.getByRole('img', { name: 'Missing-history coin generated token icon' })).toBeVisible()
  const graph = graphRow.getByRole('img', { name: 'GRAPH recorded price history' })
  await expect(graph).toBeVisible()
  await expect(graph.locator('polyline')).toHaveCount(1)
  expect((await graph.locator('polyline').getAttribute('points'))!.split(' ').length).toBe(6)
  expect((await graphRow.locator('.portfolio-price-graph').getAttribute('class'))!).toContain('up')
  await expect(graphRow).toContainText('-80.00%')
  await graph.scrollIntoViewIfNeeded()
  const bounds = (await graph.boundingBox())!
  await page.mouse.move(bounds.x + bounds.width * .5, bounds.y + bounds.height * .5)
  await expect(graphRow.locator('.portfolio-price-graph-readout')).not.toContainText('recorded prices')
  await expect(emptyRow).toContainText('No price history')
  await expect(emptyRow.locator('.portfolio-price-graph svg')).toHaveCount(0)
  await graphRow.getByRole('button', { name: 'Open GRAPH price chart' }).click()
  await expect(graphRow).toContainText('HOLDING DETAILS')
  await expect(graphRow).not.toContainText('HOLDING EVIDENCE')
  await expect(graphRow).toContainText('Current wallet ownership')
  await expect(graphRow.getByRole('button', { name: 'Review linked strategy' })).toHaveCount(0)
  await expect(graphRow.getByRole('img', { name: 'Price candlestick history chart and USD volume' })).toBeVisible()
  await graphRow.getByRole('button', { name: 'Hide GRAPH price history and holding details' }).click()
  await holdings.screenshot({ path: testInfo.outputPath('holdings-graphs-desktop.png') })
  recoveredIcon = true
  await page.getByRole('button', { name: 'Refresh holdings' }).click()
  await expect(emptyRow.getByRole('img', { name: 'Missing-history coin token artwork' })).toBeVisible()
  await expect.poll(() => emptyRow.getByRole('img', { name: 'Missing-history coin token artwork' }).evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(64)
  await page.setViewportSize({ width: 390, height: 844 })
  await holdings.scrollIntoViewIfNeeded()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await expect(graphRow.getByRole('button', { name: 'Open GRAPH price chart' })).toBeVisible()
  await holdings.screenshot({ path: testInfo.outputPath('holdings-graphs-mobile.png') })
  expect(writes.filter((path) => /trades\/(?:submit|quote)|signing|service\/trading|automation\//.test(path))).toEqual([])
})

test('strategy picker groups saved versions and clearly reports session membership and trading blockers', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1550, height: 1000 })
  await signIn(page)
  const fixture = await page.evaluate(async () => {
    const post = async (path: string, body: object) => {
      const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    const state = await (await fetch('/api/v1/app-state')).json()
    const sender = state.wallets[0].address
    const item = await post('/api/v1/test-strategy', { sender, workspace: true, label: 'Saved revision example', workspace_label: 'Practice session' })
    const query = new URLSearchParams({ sender, network: 'mainnet-beta' })
    const rows = (await (await fetch(`/api/v1/strategies?${query}`)).json()).rows
    const base = rows.find((row: { label: string }) => row.label === 'Saved revision example')
    for (const max_positions of [3, 4]) await post(`/api/v1/strategies/${base.digest}/revisions?${query}`, { label: base.label, configuration: { ...base.configuration, max_positions } })
    return item as { id: string; workspace_id: string }
  })
  await page.route('**/api/v1/session-workspaces?**', async (route) => {
    const response = await route.fetch({ headers: { ...await route.request().allHeaders(), Origin: new URL(route.request().url()).origin, 'Sec-Fetch-Site': 'same-origin' } })
    const body = await response.json() as { rows: SessionWorkspace[] }
    for (const row of body.rows) if (row.id === fixture.workspace_id) {
      row.plan = { session_mode: 'trade', execution_style: 'interactive', allocation_sol: '0.3', recommended_families: ['core'], strategy_setup: 'catalog', min_trade_sol: '0.001', max_trade_sol: '0.01', max_daily_buy_sol: '0.05', max_trades_per_day: 5, max_daily_loss_sol: '0.01', max_daily_loss_percent: '10', max_trade_loss_percent: '10', max_open_positions: 2 }
      row.funding = { state: 'insufficient', balance_lamports: '290000000', required_lamports: '300000000', shortfall_lamports: '10000000', checked_at: Date.now() / 1000, paused_at: null, detail: 'Synthetic shortfall' }
    }
    await route.fulfill({ response, json: body })
  })
  await page.reload()
  await page.getByRole('button', { name: 'Add strategy to Practice session', exact: true }).click()
  const picker = page.getByRole('dialog', { name: 'Local strategy library' })
  await expect(picker).toContainText('1 strategy choice')
  await expect(picker).toContainText('3 saved versions')
  await expect(picker.getByRole('button', { name: 'Edit session strategy', exact: true })).toBeEnabled()
  await expect(picker.locator('.local-strategy-list > .local-strategy-version-group')).toHaveCount(1)
  await expect(picker.locator('.local-strategy-version-group > article')).toContainText('Added to this session')
  await expect(picker.getByRole('region', { name: 'Trading setup' })).toContainText('Session needs funds')
  await expect(picker.getByRole('region', { name: 'Trading setup' })).toContainText('Short 0.01 SOL')
  await expect(picker.getByRole('region', { name: 'Trading setup' })).toContainText('Approve each order in your wallet')
  await picker.getByText('Other saved versions (2)', { exact: true }).click()
  await expect(picker.locator('.local-other-versions article')).toHaveCount(2)
  await expect(picker.locator('.local-other-versions article').first()).toContainText('Can add')
  await picker.screenshot({ path: testInfo.outputPath('strategy-setup-desktop.png') })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await picker.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true)
  await picker.screenshot({ path: testInfo.outputPath('strategy-setup-mobile.png') })
  await page.setViewportSize({ width: 1550, height: 1000 })
  await picker.getByRole('button', { name: 'Edit session strategy', exact: true }).click()
  await expect(picker.getByRole('form', { name: 'Edit saved strategy' })).toBeVisible()
  await picker.getByRole('button', { name: 'Close dialog' }).click()
  const sent = await page.evaluate(async () => {
    const state = await (await fetch('/api/v1/app-state')).json()
    return (await (await fetch('/api/v1/test-chain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender: state.wallets[0].address, changes: {} }) })).json()).sent
  })
  expect(sent).toBe(0)
})

test('merged overview keeps performance cards, current and archived filters, position previews and scope on mobile', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1600, height: 1000 })
  await signIn(page)
  const fixture = await page.evaluate(async () => {
    const post = async (path: string, body: object) => {
      const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      if (!response.ok) throw new Error(await response.text())
      return response.json()
    }
    const collection = await (await fetch('/api/v1/app-state')).json() as { wallets: { address: string }[] }
    const sender = collection.wallets[0].address
    const active = await post('/api/v1/test-strategy', { sender, workspace: true, label: 'Momentum strategy', workspace_label: 'Trading session' }) as { id: string; workspace_id: string }
    await post(`/api/v1/sessions/${active.id}/tracking`, { enabled: true })
    const second = await post('/api/v1/test-strategy', { sender, workspace_id: active.workspace_id, label: 'Paused with position' }) as { id: string }
    await post('/api/v1/test-strategy', { sender, workspace: true, label: 'Empty paused strategy', workspace_label: 'Second session' })
    const { mint } = await post('/api/v1/test-chain', { sender, changes: {} }) as { mint: string }
    const at = Math.floor(Date.now() / 1000)
    const key = `pumpwizard.portfolio-history/v1:mainnet-beta:${sender}`
    const rows = [{ at: at - 87000, usd: 100, pnl: -4 }, { at: at - 2700, usd: 150, pnl: -3 }, { at: at - 1800, usd: null, pnl: null }, { at: at - 900, usd: 160, pnl: 2 }]
    await post('/api/v1/app-state/preferences', { key, value: JSON.stringify(rows) })
    return { sender, id: active.id, second: second.id, workspace: active.workspace_id, mint, key }
  })
  let stale = false
  await page.route('**/api/v1/session-workspaces?**', async (route) => {
    const response = await route.fetch({ headers: { ...await route.request().allHeaders(), Origin: new URL(route.request().url()).origin, 'Sec-Fetch-Site': 'same-origin' } })
    const body = await response.json() as { rows: SessionWorkspace[] }
    for (const row of body.rows) {
      if (row.id !== fixture.workspace) continue
      row.funding = { state: 'insufficient', balance_lamports: '9000000', required_lamports: '10000000', shortfall_lamports: '1000000', checked_at: Date.now() / 1000, paused_at: null, detail: 'Synthetic shortfall' }
      row.history_strategies!.push({ id: 'archived-fixture', label: 'Archived strategy', removed: true })
      row.strategy_performance = [
        { id: fixture.id, label: 'Momentum strategy', removed: false, performance: { state: stale ? 'unavailable' : 'estimated', realized_lamports: '20000000', unrealized_lamports: stale ? null : '50000000', total_lamports: stale ? null : '70000000', open_positions: 1, closed_positions: 1, detail: 'Synthetic receipt costs and retained marks' } },
        { id: fixture.second, label: 'Paused with position', removed: false, performance: { state: 'estimated', realized_lamports: '-10000000', unrealized_lamports: '0', total_lamports: '-10000000', open_positions: 1, closed_positions: 0, detail: 'Synthetic receipt' } },
        { id: 'archived-fixture', label: 'Archived strategy', removed: true, performance: { state: 'estimated', realized_lamports: '10000000', unrealized_lamports: '0', total_lamports: '10000000', open_positions: 0, closed_positions: 1, detail: 'Synthetic closed receipt' } },
      ]
    }
    await route.fulfill({ response, json: body })
  })
  await page.route('**/api/v1/wallet/portfolio?**', async (route) => {
    const response = await route.fetch({ headers: { ...await route.request().allHeaders(), Origin: new URL(route.request().url()).origin, 'Sec-Fetch-Site': 'same-origin' } })
    const portfolio = await response.json() as WalletPortfolioState
    const mark = valuation()
    if (stale) mark.mark.freshness = 'stale'
    await route.fulfill({ response, json: { ...portfolio, checked_at: Date.now() / 1000, assets: [{ mint: fixture.mint, amount: '1000000000', decimals: 6, symbol: 'TEST', name: 'Synthetic holding', untracked_atoms: '0', runner_atoms: '1000000000', runner_position_ids: ['dashboard-fixture-position'], program: 'SPL Token', token_program: 'synthetic', accounts: 1, website: null, image_url: null, creator: null, metadata_source: 'synthetic', created_at: null, price_usd: '.05', observed_at: Date.now() / 1000, price_source: 'synthetic', price_slot_lag: 0, liquidity_usd: null, market_cap_usd: null, price_change_24h: null, volume_change_24h: null, holder_count: null, organic_score: null, organic_score_label: null, suspicious: false, top_holders_percent: null, trend_points: [], estimated_value_usd: '50', valuation_state: stale ? 'stale' : 'priced', valuation_detail: 'Synthetic fixture valuation', valuation: mark }] } })
  })
  await page.route('**/api/v1/trades?**', async (route) => {
    const response = await route.fetch({ headers: { ...await route.request().allHeaders(), Origin: new URL(route.request().url()).origin, 'Sec-Fetch-Site': 'same-origin' } })
    const trades = await response.json() as PortfolioTradeState
    const mark = valuation()
    if (stale) { mark.mark.freshness = 'stale'; mark.unrealized_result = { ...mark.unrealized_result, state: 'unknown', amount: null, percent: null } }
    const position = { id: 'dashboard-fixture-position', genesis: 'synthetic', sender: fixture.sender, mint: fixture.mint, opened_at: Date.now() / 1000 - 1000, quantity: '1000000000', decimals: 6, initial_quantity: '1000000000', original_cost: '450000000', remaining_cost: '450000000', realized_proceeds: '0', realized_cost: '0', realized_pnl: '0', transaction_fees: '5000', status: 'open', managed: true, agent_id: fixture.id, strategy_label: 'Momentum strategy', sold_stages: [], exit_plan: null, valuation: mark }
    const order = { id: 'synthetic-order', agent_id: fixture.id, position_id: position.id, status: 'finalized', side: 'buy', mint: fixture.mint, amount: '450000000', decimals: 6, network: 'mainnet-beta', route: 'Synthetic', signature: '', created_at: Date.now() / 1000, detail: 'Synthetic fixture', receipt: null }
    await route.fulfill({ response, json: { ...trades, positions: [position, { ...position, id: 'second-position', agent_id: fixture.second, strategy_label: 'Paused with position', managed: false }], orders: [order] } })
  })
  await page.reload()
  const terminal = page.getByRole('region', { name: 'Trading dashboard' })
  const dashboard = terminal.locator('.operations-dashboard')
  await expect(terminal.getByRole('tab')).toHaveCount(3)
  await expect(terminal.getByRole('tab', { name: /Positions/ })).toHaveCount(0)
  await expect(terminal.getByRole('tab', { name: /Overview/ })).toHaveAttribute('aria-selected', 'true')
  await expect(terminal.getByRole('tab', { name: /New Order/i })).toHaveCount(0)
  await expect(dashboard).not.toContainText('Wallet SOL')
  await expect(dashboard.locator('.dashboard-metrics')).toContainText('+0.07 SOL')
  await expect(dashboard.getByRole('region', { name: 'Current strategy status' })).toContainText('0 trading')
  await expect(terminal.getByRole('tab', { name: /Performance/ })).toHaveCount(0)
  await expect(dashboard.locator('.dashboard-metrics')).toContainText('Realized P&L')
  await expect(dashboard.locator('.dashboard-metrics')).toContainText('Unrealized P&L')
  await expect(dashboard.locator('.dashboard-metrics')).toContainText('Closed positions')
  const comparison = dashboard.getByRole('table', { name: 'Strategy comparison' })
  await expect(comparison.locator('tbody tr')).toHaveCount(3)
  await expect(comparison).toContainText('Paused with position')
  await expect(comparison).toContainText('Empty paused strategy')
  await expect(comparison).not.toContainText('Archived strategy')
  await expect(comparison.locator('thead th')).toHaveCount(8)
  await expect(dashboard.getByRole('region', { name: 'Needs attention' }).getByRole('button', { name: 'Review session funds' })).toHaveCount(1)
  await expect(dashboard.getByRole('region', { name: 'Open positions preview' })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('dashboard-desktop.png'), fullPage: true })
  await terminal.getByRole('tab', { name: /Overview/ }).click()
  await dashboard.getByRole('button', { name: 'All history', exact: true }).click()
  await expect(comparison.locator('tbody tr')).toHaveCount(4)
  await expect(comparison).toContainText('Archived strategy')
  await expect(dashboard.locator('.dashboard-metrics')).toContainText('+0.07 SOL')
  await dashboard.getByRole('button', { name: 'Needs attention', exact: true }).click()
  await expect(comparison.locator('tbody tr')).toHaveCount(2)
  await expect(comparison).not.toContainText('Archived strategy')
  await dashboard.getByRole('button', { name: 'Current', exact: true }).click()
  await expect(comparison.locator('tbody tr')).toHaveCount(3)
  await expect(comparison).toContainText('Empty paused strategy')
  await dashboard.getByRole('button', { name: 'Sort by total P&L', exact: true }).click()
  await expect(comparison.locator('tbody tr').first()).toContainText('Momentum strategy')
  await terminal.getByRole('tab', { name: /Overview/ }).click()
  await expect(terminal.locator('.dashboard-metrics')).toContainText('+0.02 SOL')
  await expect(terminal.locator('.dashboard-metrics')).toContainText('+0.05 SOL')
  await terminal.getByRole('button', { name: 'Momentum strategy ↗', exact: true }).click()
  await expect(terminal.getByRole('tab', { name: /Overview/ })).toHaveAttribute('aria-selected', 'true')
  await expect(terminal.locator('.trading-focus-header')).toContainText('Momentum strategy')
  await expect(terminal.getByRole('table', { name: 'Strategy comparison' }).locator('tbody tr')).toHaveCount(1)
  await expect(terminal.getByRole('tab')).toHaveCount(4)
  await expect(dashboard.getByRole('table', { name: 'Open positions preview' })).toContainText('+$5.00')
  await terminal.getByRole('tab', { name: /Positions/ }).click()
  await expect(terminal.locator('.terminal-position-row')).toHaveCount(1)
  await expect(terminal.locator('.terminal-position-details')).not.toHaveAttribute('open', '')
  await terminal.getByText('Position details', { exact: true }).click()
  await expect(terminal.getByText('Quote freshness', { exact: true })).toBeVisible()
  await terminal.getByRole('tab', { name: /Activity/ }).click()
  await expect(terminal.locator('.trade-history-row')).toHaveCount(1)
  await terminal.getByRole('button', { name: 'Decisions', exact: true }).click()
  await expect(terminal.locator('.session-observations')).toHaveCount(1)
  await terminal.getByRole('tab', { name: /Overview/ }).click()
  await expect(terminal.locator('.trading-focus-header')).toContainText('Momentum strategy')
  await page.setViewportSize({ width: 390, height: 844 })
  await dashboard.scrollIntoViewIfNeeded()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('dashboard-mobile.png'), fullPage: true })
  await terminal.getByRole('tab', { name: /Wallet/ }).click()
  await expect(terminal.locator('.trading-focus-header')).toHaveCount(0)
  await expect(terminal.getByRole('tab')).toHaveCount(3)
  const wallet = terminal.locator('.wallet-overview')
  await expect(wallet).toContainText('$175.00')
  await expect(wallet).toContainText('includes deposits and withdrawals')
  const chart = wallet.getByRole('region', { name: 'Wallet value chart', exact: true })
  expect((await chart.locator('svg path').getAttribute('d'))!.match(/M/g)!.length).toBeGreaterThan(1)
  await chart.getByRole('img').focus()
  await page.keyboard.press('ArrowLeft')
  await expect(chart.locator('footer')).toContainText('$160.00')
  await wallet.getByRole('button', { name: '7d', exact: true }).click()
  await expect(wallet.getByRole('button', { name: '7d', exact: true })).toHaveAttribute('aria-pressed', 'true')
  stale = true
  await expect(wallet).toContainText('Known wallet value · partial', { timeout: 40_000 })
  await expect(wallet).toContainText('$125.00')
  await expect(wallet.getByLabel('24-hour wallet worth movement')).toContainText('Needs a complete current wallet value')
  await terminal.getByRole('tab', { name: /Overview/ }).click()
  await expect(terminal.locator('.dashboard-metrics')).toContainText('Unavailable')
  await expect(terminal.locator('.dashboard-metrics')).toContainText('+0.02 SOL')
  await dashboard.getByRole('button', { name: 'Review wallet →', exact: true }).click()
  await expect(terminal.getByRole('tab', { name: /Wallet/ })).toHaveAttribute('aria-selected', 'true')
  await terminal.getByRole('tab', { name: /Overview/ }).click()
  await terminal.getByRole('button', { name: 'Momentum strategy ↗', exact: true }).click()
  await terminal.getByRole('tab', { name: /Positions/ }).click()
  await expect(terminal.locator('.terminal-position-compact [data-label="Unrealized P&L"]').first()).toContainText('Unavailable')
  await expect(terminal.locator('.terminal-position-compact [data-label="Current value"]').first()).toContainText('—')
  await terminal.getByRole('button', { name: 'All wallet activity', exact: true }).click()
  await expect(terminal.getByRole('tab')).toHaveCount(3)
  await expect(terminal.getByRole('tab', { name: /Overview/ })).toHaveAttribute('aria-selected', 'true')
  await expect(terminal.locator('.terminal-position-row')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('wallet-dashboard-mobile.png'), fullPage: true })
  await terminal.getByRole('tab', { name: /Overview/ }).click()
  await expect(dashboard.getByRole('region', { name: 'Needs attention' })).toContainText('positions need fresh prices')
  await terminal.getByRole('button', { name: 'New order', exact: true }).click()
  await expect(terminal.getByRole('tabpanel', { name: 'New order', exact: true })).toBeVisible()
  await terminal.getByRole('button', { name: '← Overview', exact: true }).click()
  await page.reload()
  await expect(terminal.getByRole('tab', { name: /Overview/ })).toHaveAttribute('aria-selected', 'true')
  const saved = await page.evaluate(async (key) => {
    const state = await (await fetch('/api/v1/app-state')).json() as { preferences: Record<string, string> }
    return { rows: JSON.parse(state.preferences[key]), browser: Object.keys(localStorage).concat(Object.keys(sessionStorage)) }
  }, fixture.key)
  expect(saved.rows.some((row: { usd: number | null }) => row.usd === null)).toBe(true)
  expect(saved.browser.some((key) => key.includes('portfolio-history'))).toBe(false)
  const sent = await page.evaluate(async (sender) => (await (await fetch('/api/v1/test-chain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender, changes: {} }) })).json()).sent, fixture.sender)
  expect(sent).toBe(0)
  await page.unrouteAll({ behavior: 'wait' })
})
