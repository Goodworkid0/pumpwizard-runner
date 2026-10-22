// Durable state belongs to the local Runner. This cache lasts only for this page.
type State = { wallets: unknown[]; preferences: Record<string, string> }
let state: State = { wallets: [], preferences: {} }
let committedPreferences: Record<string, string> = {}
const revisions = new Map<string, number>()
let writes: Promise<void> = Promise.resolve()
const walletKeys = ['pumpwizard.browser-wallets/v2', 'pumpwizard.browser-wallet/v1']
const preferenceKeys = new Set([
  'pumpwizard.research-mode/v1', 'pumpwizard.welcome-acknowledged-version/v1',
  'pumpwizard.wallet-session/v1', 'pumpwizard.background-service-instance/v1',
  'pumpwizard.activity-muted', 'pumpwizard.welcome.last-shown',
])

async function request<T>(path: string, body?: object): Promise<T> {
  const response = await fetch(path, body === undefined ? { cache: 'no-store' } : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  const result = await response.json() as T & { error?: string }
  if (!response.ok) throw new Error(result.error || 'Local app storage is unavailable.')
  return result
}

function report(error: unknown) {
  window.dispatchEvent(new CustomEvent('pumpwizard-storage-error', { detail: error instanceof Error ? error.message : 'Local app storage is unavailable.' }))
}

function writePreference(key: string, value: string | null) {
  const revision = (revisions.get(key) || 0) + 1
  revisions.set(key, revision)
  if (value === null) delete state.preferences[key]
  else state.preferences[key] = value
  const operation = writes.then(async () => {
    await request('/api/v1/app-state/preferences', { key, value })
    if (value === null) delete committedPreferences[key]
    else committedPreferences[key] = value
  })
  writes = operation.catch((error) => {
    if (revisions.get(key) === revision) {
      if (committedPreferences[key] === undefined) delete state.preferences[key]
      else state.preferences[key] = committedPreferences[key]
    }
    report(error)
  })
  return operation
}

export const serverStorage = {
  getItem(key: string): string | null { return state.preferences[key] ?? null },
  setItem(key: string, value: string): void {
    void writePreference(key, value).catch(() => undefined)
  },
  removeItem(key: string): void {
    void writePreference(key, null).catch(() => undefined)
  },
  setItemAsync: writePreference,
}

export async function flushServerStorage() { await writes }
export function savedWalletRecords() { return state.wallets }
export async function saveWalletRecord(wallet: unknown) {
  const result = await request<State>('/api/v1/app-state/wallets', { wallet })
  state.wallets = result.wallets
}
export async function removeWalletRecord(address: string) {
  const result = await request<State>('/api/v1/app-state/wallets/remove', { address })
  state.wallets = result.wallets
}

export async function initializeServerStorage(migrateLegacy = true) {
  state = await request<State>('/api/v1/app-state')
  committedPreferences = { ...state.preferences }
  if (!migrateLegacy) return
  const wallets: unknown[] = []
  const preferences: Record<string, string> = {}
  const migrated: { storage: Storage; key: string; value: string }[] = []
  // Browser storage is read only for upgrading old installations. Unavailable
  // browser storage does not affect new server-owned accounts.
  for (const name of ['localStorage', 'sessionStorage'] as const) {
    let storage: Storage
    try { storage = window[name]; void storage.length } catch { continue }
    for (let index = 0; index < storage.length; index++) {
      const key = storage.key(index)
      if (!key) continue
      const value = storage.getItem(key)
      if (value === null) continue
      if (walletKeys.includes(key)) {
        const record = JSON.parse(value) as { schema?: string; wallets?: unknown[] }
        if (key === walletKeys[0]) {
          if (record.schema !== 'pumpwizard.browser-wallets/v2' || !Array.isArray(record.wallets)) throw new Error('Existing encrypted wallet collection is invalid. Its browser copy was kept.')
          wallets.push(...record.wallets)
        } else wallets.push(record)
      } else if (preferenceKeys.has(key) || key.startsWith('pumpwizard.wallet-worth-history/v1:') || key.startsWith('research:') || key.startsWith('forward-campaign:')) {
        preferences[key] ??= value
      } else continue
      migrated.push({ storage, key, value })
    }
  }
  if (migrated.length) {
    // Sending duplicates as separate records would obscure conflicting backups.
    const unique = new Map<string, unknown>()
    for (const record of wallets) {
      const address = (record as { address?: string })?.address
      if (!address) throw new Error('Existing encrypted wallet is invalid. Its browser copy was kept.')
      const previous = unique.get(address)
      if (previous && JSON.stringify(previous) !== JSON.stringify(record)) throw new Error('Conflicting encrypted wallet copies were kept. Restore the correct backup to continue.')
      unique.set(address, record)
    }
    state = await request<State>('/api/v1/app-state/migrate', { wallets: [...unique.values()], preferences })
    committedPreferences = { ...state.preferences }
    // Commit first; remove only the exact legacy values that were migrated.
    for (const { storage, key, value } of migrated) {
      if (storage.getItem(key) === value) storage.removeItem(key)
    }
  }
  // Obsolete approvals are never migrated or treated as authority.
  try { window.sessionStorage.removeItem('pumpwizard.background-service-approval/v1') } catch { /* unavailable legacy storage */ }
}
