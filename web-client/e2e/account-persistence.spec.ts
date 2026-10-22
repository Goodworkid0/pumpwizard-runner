import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'
import { resetSavedAccounts } from './savedAccounts'

test.beforeEach(async ({ page }) => { await resetSavedAccounts(page) })
const password = 'server account fixture password'

async function createAccount(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'Create a new wallet' }).click()
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByLabel('Confirm password').fill(password)
  await page.getByRole('button', { name: 'Create encrypted wallet' }).click()
  await expect(page.getByRole('heading', { name: 'Save your backup' })).toBeVisible()
  const record = await page.evaluate(async () => (await (await fetch('/api/v1/app-state')).json()).wallets[0])
  await page.getByLabel(/I saved the encrypted backup/).check()
  await page.getByRole('button', { name: 'Choose a wallet' }).click()
  return record
}

test('saved accounts work in a fresh browser with client storage blocked', async ({ page, browser }) => {
  const record = await createAccount(page)
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain(record.address)
  const context = await browser.newContext()
  await context.addInitScript(() => {
    for (const name of ['localStorage', 'sessionStorage']) {
      Object.defineProperty(window, name, { get: () => { throw new Error('Browser persistence blocked') } })
    }
  })
  const reopened = await context.newPage()
  await reopened.goto(page.url())
  await expect(reopened.getByRole('button', { name: `Unlock PumpWizard Wallet ${record.address}` })).toBeVisible()
  await reopened.getByRole('button', { name: /Unlock PumpWizard Wallet/ }).click()
  await reopened.getByLabel('Wallet password').fill('wrong password')
  await reopened.getByRole('button', { name: 'Unlock & sign in' }).click()
  await expect(reopened.getByRole('status')).toContainText('password is incorrect')
  await reopened.getByLabel('Wallet password').fill(password)
  await reopened.getByRole('button', { name: 'Unlock & sign in' }).click()
  await reopened.locator('.welcome-dialog').getByRole('button', { name: 'Skip', exact: true }).click()
  await expect(reopened.getByRole('main', { name: 'Wallet dashboard' })).toBeVisible()
  await expect.poll(() => reopened.evaluate(async () => JSON.parse((await (await fetch('/api/v1/app-state')).json()).preferences['pumpwizard.wallet-session/v1'] || '{}').address)).toBe(record.address)
  await reopened.reload()
  await expect(reopened.getByRole('main', { name: 'Wallet dashboard' })).toBeVisible()
  await expect(reopened.getByRole('region', { name: 'Wallet connection' })).toContainText('signing locked')
  await context.close()
})

test('legacy accounts migrate only after a confirmed server commit', async ({ page }) => {
  const record = await createAccount(page)
  await page.evaluate(async (wallet) => {
    await fetch('/api/v1/app-state/wallets/remove', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ address: wallet.address }) })
    localStorage.setItem('pumpwizard.browser-wallet/v1', JSON.stringify(wallet))
    localStorage.setItem('pumpwizard.welcome.last-shown', '1789420828423')
    localStorage.setItem('pumpwizard.activity-muted', 'false')
  }, record)
  await page.route('**/api/v1/app-state/migrate', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'Server storage unavailable' }) }))
  await page.reload()
  await expect(page.getByRole('alert')).toContainText('Server storage unavailable')
  expect(await page.evaluate(() => localStorage.getItem('pumpwizard.browser-wallet/v1'))).not.toBeNull()
  expect(await page.evaluate(async () => (await (await fetch('/api/v1/app-state')).json()).wallets)).toEqual([])
  await page.unroute('**/api/v1/app-state/migrate')
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('button', { name: /Unlock PumpWizard Wallet/ })).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem('pumpwizard.browser-wallet/v1'))).toBeNull()
  expect(await page.evaluate(() => localStorage.getItem('pumpwizard.welcome.last-shown'))).toBeNull()
  expect(await page.evaluate(async () => (await (await fetch('/api/v1/app-state')).json()).preferences['pumpwizard.welcome.last-shown'])).toBe('1789420828423')
  expect(await page.evaluate(async () => (await (await fetch('/api/v1/app-state')).json()).wallets)).toEqual([record])
})

test('corrupt legacy accounts are visible errors and existing server accounts survive', async ({ page }) => {
  const record = await createAccount(page)
  await page.evaluate(() => localStorage.setItem('pumpwizard.browser-wallets/v2', '{"schema":"invalid","wallets":[]}'))
  await page.reload()
  await expect(page.getByRole('alert')).toContainText('Existing encrypted wallet collection is invalid')
  expect(await page.evaluate(async () => (await (await fetch('/api/v1/app-state')).json()).wallets)).toEqual([record])
  expect(await page.evaluate(() => localStorage.getItem('pumpwizard.browser-wallets/v2'))).not.toBeNull()
  await page.getByRole('button', { name: 'Use saved app accounts' }).click()
  await expect(page.getByRole('button', { name: `Unlock PumpWizard Wallet ${record.address}` })).toBeVisible()
})

test('a failed account save does not claim success or fall back to browser storage', async ({ page }) => {
  await page.goto('/')
  await page.route('**/api/v1/app-state/wallets', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'Account was not saved' }) }))
  await page.getByRole('button', { name: 'Create a new wallet' }).click()
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByLabel('Confirm password').fill(password)
  await page.getByRole('button', { name: 'Create encrypted wallet' }).click()
  await expect(page.getByRole('status')).toContainText('Account was not saved')
  await expect(page.getByRole('heading', { name: 'Create a wallet' })).toBeVisible()
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0)
  expect(await page.evaluate(async () => (await (await fetch('/api/v1/app-state')).json()).wallets)).toEqual([])
})
