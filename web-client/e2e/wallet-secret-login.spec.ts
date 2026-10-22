import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import nacl from 'tweetnacl'
import { toBase58 } from '../src/walletSecrets'
import { resetSavedAccounts } from './savedAccounts'

test.beforeEach(async ({ page }) => { await resetSavedAccounts(page) })
const password = 'imported fixture wallet password'
const phrase = `${'abandon '.repeat(11)}about`
const fixture = nacl.sign.keyPair.fromSeed(new Uint8Array(32).fill(7))
const address = toBase58(fixture.publicKey)
const privateKey = toBase58(fixture.secretKey)
const choice = 'Sign in with private key or recovery phrase'

async function state(page: Page) {
  return page.evaluate(async () => (await (await fetch('/api/v1/app-state')).json()) as { wallets: { address: string; cipher: { ciphertext: string } }[]; preferences: Record<string, string> })
}

async function enterSecret(page: Page, value: string, recovery = false) {
  await page.getByRole('button', { name: choice }).click()
  if (recovery) await page.getByRole('button', { name: 'Recovery phrase', exact: true }).click()
  await page.getByLabel(recovery ? 'Recovery phrase' : 'Solana private key', { exact: true }).fill(value)
  await page.getByRole('button', { name: 'Review wallet' }).click()
}

async function saveAndEnter(page: Page) {
  await page.getByLabel('New wallet password', { exact: true }).fill(password)
  await page.getByLabel('Confirm wallet password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Save & sign in' }).click()
  await expect(page.locator('.welcome-dialog')).toBeVisible()
  await page.locator('.welcome-dialog').getByRole('button', { name: 'Skip', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Wallet connection' })).toContainText('CONNECTED WALLET')
}

test('private key sign-in saves only ciphertext, survives a new browser, and unlocks with its password', async ({ page, browser }, testInfo) => {
  const outbound: string[] = []
  page.on('request', (request) => outbound.push(`${request.url()} ${request.postData() || ''}`))
  await page.goto('/')
  await enterSecret(page, privateKey)
  await expect(page.locator('.stored-address')).toContainText(address)
  await expect(page.getByLabel('Solana private key')).toHaveCount(0)
  expect((await state(page)).wallets).toHaveLength(0)
  await saveAndEnter(page)
  const saved = (await state(page)).wallets
  expect(saved).toHaveLength(1)
  expect(saved[0].address).toBe(address)
  expect(Buffer.from(saved[0].cipher.ciphertext, 'base64')).toHaveLength(80)
  for (const text of [...outbound, JSON.stringify(await state(page)), await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))]) {
    expect(text).not.toContain(privateKey)
    expect(text).not.toContain(password)
    expect(text).not.toContain(JSON.stringify([...fixture.secretKey]))
  }
  await page.reload()
  await expect(page.getByRole('region', { name: 'Wallet connection' })).toContainText('signing locked')
  await page.getByRole('button', { name: 'Sign out of wallet' }).click()
  await expect.poll(async () => (await state(page)).preferences['pumpwizard.research-mode/v1']).toBe('enabled')
  await expect.poll(async () => (await state(page)).preferences['pumpwizard.wallet-session/v1'] || null).toBeNull()
  const processId = async () => page.evaluate(async (sender) => (await (await fetch('/api/v1/test-chain', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender, changes: {} }) })).json()).process_id as string, address)
  const beforeRestart = await processId()
  await page.evaluate(async () => { await fetch('/api/v1/test-restart', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }) })
  await expect.poll(async () => {
    try { await page.request.get('/'); return await processId() !== beforeRestart } catch { return false }
  }, { timeout: 15_000 }).toBe(true)
  const fresh = await browser.newContext()
  const reopened = await fresh.newPage()
  await reopened.goto(page.url())
  await expect(reopened.getByRole('button', { name: 'Connect wallet', exact: true }).or(reopened.getByRole('button', { name: `Unlock PumpWizard Wallet ${address}` }))).toBeVisible()
  if (await reopened.getByRole('button', { name: 'Connect wallet', exact: true }).isVisible()) await reopened.getByRole('button', { name: 'Connect wallet', exact: true }).click()
  await reopened.getByRole('button', { name: `Unlock PumpWizard Wallet ${address}` }).click()
  await reopened.getByLabel('Wallet password').fill('incorrect password')
  await reopened.getByRole('button', { name: 'Unlock & sign in' }).click()
  await expect(reopened.getByRole('status')).toContainText('incorrect')
  await reopened.getByLabel('Wallet password').fill(password)
  await reopened.getByRole('button', { name: 'Unlock & sign in' }).click()
  await expect(reopened.getByRole('region', { name: 'Wallet connection' })).toContainText('CONNECTED WALLET')
  expect((await state(reopened)).wallets).toEqual(saved)
  await reopened.screenshot({ path: testInfo.outputPath('imported-wallet-unlocked.png'), fullPage: true })
  await fresh.close()
})

test('recovery phrase sign-in previews the selected account and keeps recovery inputs off the network', async ({ page }, testInfo) => {
  const outbound: string[] = []
  page.on('request', (request) => outbound.push(request.postData() || ''))
  await page.goto('/')
  await page.getByRole('button', { name: choice }).click()
  await page.getByRole('button', { name: 'Recovery phrase', exact: true }).click()
  const input = page.getByLabel('Recovery phrase', { exact: true })
  await expect(input).toHaveAttribute('type', 'password')
  await input.fill(phrase)
  await expect(page.getByLabel('Wallet account')).toBeHidden()
  await page.getByText('Advanced recovery — address doesn’t match?', { exact: true }).click()
  await expect(page.getByLabel('Wallet account')).toHaveValue('0')
  await page.getByLabel('Wallet account').selectOption({ label: 'Account 2' })
  await page.getByRole('button', { name: 'Review wallet' }).click()
  await expect(page.locator('.stored-address')).toContainText('Hh8QwFUA6MtVu1qAoq12ucvFHNwCcVTV7hpWjeY1Hztb')
  await expect(page.locator('.stored-address')).toContainText('Account 2')
  const recoveryDetails = page.locator('details.recovery-options')
  await expect(recoveryDetails.locator('small')).toBeHidden()
  await page.getByText('Advanced recovery details', { exact: true }).click()
  await expect(recoveryDetails).toContainText("m/44'/501'/1'/0'")
  await page.getByRole('button', { name: 'Back to secret entry' }).click()
  await expect(input).toHaveValue('')
  await input.fill(phrase)
  await page.getByText('Advanced recovery — address doesn’t match?', { exact: true }).click()
  await page.getByLabel('Wallet account').selectOption({ label: 'Account 1 (default)' })
  await page.getByLabel('Recovery passphrase (optional)').fill('TREZOR')
  await page.getByRole('button', { name: 'Review wallet' }).click()
  await expect(page.locator('.stored-address')).toContainText('7zSmbu6gKkb6HB7UDPtHYjwCWuBHU1D4TpNZFm4sndQe')
  await page.screenshot({ path: testInfo.outputPath('recovery-address-review.png'), fullPage: true })
  await saveAndEnter(page)
  for (const text of [...outbound, JSON.stringify(await state(page)), await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))]) {
    expect(text).not.toContain(phrase)
    expect(text).not.toContain('TREZOR')
  }
})

test('existing-key sign-in preserves the original encrypted record and password', async ({ page }) => {
  await page.goto('/')
  await enterSecret(page, privateKey)
  await saveAndEnter(page)
  const original = (await state(page)).wallets
  await page.getByRole('button', { name: 'Sign out of wallet' }).click()
  if (await page.getByRole('button', { name: 'Connect wallet', exact: true }).isVisible()) await page.getByRole('button', { name: 'Connect wallet', exact: true }).click()
  await enterSecret(page, JSON.stringify([...fixture.secretKey]))
  await expect(page.getByText('This wallet is already saved.', { exact: false })).toBeVisible()
  await expect(page.getByLabel('New wallet password')).toHaveCount(0)
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Wallet connection' })).toContainText('CONNECTED WALLET')
  expect((await state(page)).wallets).toEqual(original)
})

test('invalid keys and phrases, cancellation, and failed saves leave no account or plaintext fallback', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: choice }).click()
  await page.getByLabel('Solana private key').fill(address)
  await page.getByRole('button', { name: 'Review wallet' }).click()
  await expect(page.getByRole('status')).toContainText('complete 64-byte')
  await page.getByRole('button', { name: 'Recovery phrase', exact: true }).click()
  await page.getByLabel('Recovery phrase', { exact: true }).fill('abandon '.repeat(12))
  await page.getByRole('button', { name: 'Review wallet' }).click()
  await expect(page.getByRole('status')).toContainText('valid English recovery phrase')
  await page.getByRole('button', { name: 'Private key', exact: true }).click()
  await expect(page.getByLabel('Solana private key')).toHaveValue('')
  await page.getByLabel('Solana private key').fill(privateKey)
  await page.getByRole('button', { name: 'Back', exact: true }).click()
  await enterSecret(page, privateKey)
  await page.getByLabel('New wallet password', { exact: true }).fill(password)
  await page.getByLabel('Confirm wallet password', { exact: true }).fill('different password')
  await page.getByRole('button', { name: 'Save & sign in' }).click()
  await expect(page.getByRole('status')).toContainText('do not match')
  expect((await state(page)).wallets).toHaveLength(0)
  await page.route('**/api/v1/app-state/wallets', (route) => route.fulfill({ status: 503, json: { error: 'Fixture storage unavailable.' } }))
  await page.getByLabel('Confirm wallet password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Save & sign in' }).click()
  await expect(page.getByRole('status')).toContainText('Fixture storage unavailable')
  await expect(page.getByLabel('Solana private key')).toHaveValue('')
  expect((await state(page)).wallets).toHaveLength(0)
  expect(await page.evaluate(() => Object.keys(localStorage).concat(Object.keys(sessionStorage)))).toEqual([])
})

test('secret login remains usable on a narrow screen', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await page.getByRole('button', { name: choice }).click()
  await page.getByRole('button', { name: 'Recovery phrase', exact: true }).click()
  await page.getByText('Advanced recovery — address doesn’t match?', { exact: true }).click()
  await page.getByLabel('Derivation path').selectOption('legacy')
  await page.getByLabel('Recovery phrase', { exact: true }).fill(phrase)
  await page.getByRole('button', { name: 'Review wallet' }).click()
  await expect(page.locator('.stored-address')).toContainText('D2PPQSYFe83nDzk96FqGumVU8JA7J8vj2Rhjc2oXzEi5')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('mobile-secret-login.png'), fullPage: true })
  await page.getByRole('button', { name: 'Back to secret entry' }).click()
  await expect(page.getByLabel('Recovery phrase', { exact: true })).toHaveValue('')
})

test('a failed ownership check retains the saved ciphertext and permits a clean retry', async ({ page }) => {
  await page.goto('/')
  await enterSecret(page, privateKey)
  await page.route('**/api/v1/wallet/verify', (route) => route.fulfill({ status: 401, json: { error: 'Fixture ownership check rejected.' } }), { times: 1 })
  await page.getByLabel('New wallet password', { exact: true }).fill(password)
  await page.getByLabel('Confirm wallet password', { exact: true }).fill(password)
  await page.getByRole('button', { name: 'Save & sign in' }).click()
  await expect(page.getByRole('status')).toContainText('Fixture ownership check rejected')
  await expect(page.getByLabel('Solana private key')).toHaveValue('')
  const saved = (await state(page)).wallets
  expect(saved).toHaveLength(1)
  await page.getByLabel('Solana private key').fill(privateKey)
  await page.getByRole('button', { name: 'Review wallet' }).click()
  await expect(page.getByLabel('New wallet password')).toHaveCount(0)
  await page.getByRole('button', { name: 'Sign in', exact: true }).click()
  await expect(page.locator('.welcome-dialog')).toBeVisible()
  expect((await state(page)).wallets).toEqual(saved)
})
