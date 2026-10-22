import { getWallets } from '@wallet-standard/app'
import nacl from 'tweetnacl'
import { toBase58 } from './walletSecrets'
import type { LocalKeypair } from './walletSecrets'
import { removeWalletRecord, savedWalletRecords, saveWalletRecord, serverStorage } from './serverStorage'

type ApiEnvelope<T> = { schema: string; version: string } & T
type Registry = ReturnType<typeof getWallets>
export type StandardWallet = ReturnType<Registry['get']>[number]
type StandardAccount = StandardWallet['accounts'][number]
type ConnectFeature = { connect(options?: { silent?: boolean }): Promise<{ accounts: readonly StandardAccount[] }> }
type DisconnectFeature = { disconnect(): Promise<void> }
type EventsFeature = { on(event: 'change', listener: (properties: { accounts?: readonly StandardAccount[]; chains?: readonly string[] }) => void): () => void }
type SignMessageFeature = { signMessage(...inputs: { message: Uint8Array; account: StandardAccount }[]): Promise<readonly { signedMessage: Uint8Array; signature: Uint8Array }[]> }
type SignTransactionFeature = { signTransaction(...inputs: { transaction: Uint8Array; account: StandardAccount; chain?: string }[]): Promise<readonly { signedTransaction: Uint8Array }[]> }
type WalletChallenge = { challenge_id: string; message: string; expires_at: number }
export type WalletInspection = { sender: string; network: WalletNetwork; genesis: string; balance: string; reserved_lamports: string; unallocated_lamports: string; buy_capacity_lamports: string; buy_buffer_lamports: string; entry_blocks: string[]; wallet_risk?: { policy: null | { max_buy_lamports: number; max_exposure_lamports: number; reserve_lamports: number; daily_loss_lamports: number; drawdown_lamports: number; portfolio_drawdown_lamports: number; entries_paused: boolean } }; inspected_at: number }

export type WalletNetwork = 'mainnet-beta' | 'devnet'
export type TradingMarket = 'pumpfun'
export type SigningDiagnostic = { wire_bytes: number; signature_count: number | null; message: string | null; valid_signature: boolean | null; value_type: 'typed-array' | 'array-buffer' | 'array' | 'other' }
export type WalletSession = {
  kind: 'extension' | 'local'
  name: string
  address: string
  network: WalletNetwork
  balance: string
  signingAvailable: boolean
  publicKey?: Uint8Array
  signMessage: (message: Uint8Array) => Promise<Uint8Array>
  signTransaction: (transaction: Uint8Array) => Promise<Uint8Array>
  checkTransaction?: (transaction: Uint8Array) => Promise<SigningDiagnostic>
  assertActive: () => void
  onInvalidated?: (listener: () => void) => () => void
  disconnect?: () => Promise<void>
}

export type MainWalletAutopilotRuntime = {
  allowance_id: string
  runtime_token: string
  authorization_revision: number
  heartbeat_seconds: number
  heartbeat_deadline: number
  expires_at: number
  mode: 'synthetic'
}

export type MainWalletSigningTicket = {
  schema: 'pumpwizard.main-wallet-signing-ticket/v1'
  id: string
  allowance_id: string
  order_id: string
  sender: string
  authorization_revision: number
  message_sha256: string
  wire_sha256: string
  unsigned_transaction: string
  created_at: number
  expires_at: number
}

export type EncryptedWallet = {
  schema: 'pumpwizard.browser-wallet/v1'
  address: string
  created_at: string
  kdf: { name: 'PBKDF2'; hash: 'SHA-256'; iterations: number; salt: string }
  cipher: { name: 'AES-GCM'; iv: string; ciphertext: string }
}

export type ServiceVaultEnrollment = {
  schema: 'pumpwizard.service-wallet-vault/v1'
  vault_id: string
  custody: 'main-wallet-service'
  address: string
  network: WalletNetwork
  created_at: string
  kdf: { name: 'PBKDF2'; hash: 'SHA-256'; iterations: 310000; salt: string }
  cipher: { name: 'AES-256-GCM'; nonce: string; ciphertext: string }
}

export const WALLET_STORAGE_KEY = 'pumpwizard.browser-wallet/v1'
export const WALLETS_STORAGE_KEY = 'pumpwizard.browser-wallets/v2'
export const WALLET_SESSION_STORAGE_KEY = 'pumpwizard.wallet-session/v1'
const KDF_ITERATIONS = 310_000

export type RestoredWalletSession = {
  session: WalletSession
  market: TradingMarket | null
}

export async function api<T>(path: string, body?: object): Promise<ApiEnvelope<T>> {
  const response = await fetch(path, body === undefined ? undefined : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const value = await response.json().catch(() => ({ error: `Runner API returned ${response.status}` })) as { error?: string }
  if (!response.ok) throw new Error(value.error || `Runner API returned ${response.status}`)
  return value as ApiEnvelope<T>
}

function feature<T>(wallet: StandardWallet, name: `${string}:${string}`): T | null {
  const value = wallet.features[name]
  return value && typeof value === 'object' ? value as T : null
}

export function chainFor(network: WalletNetwork) {
  return network === 'devnet' ? 'solana:devnet' : 'solana:mainnet'
}

export function supportsPumpWizard(wallet: StandardWallet) {
  return Boolean(feature<ConnectFeature>(wallet, 'standard:connect')
    && feature<SignMessageFeature>(wallet, 'solana:signMessage')
    && feature<SignTransactionFeature>(wallet, 'solana:signTransaction'))
}

export function detectedWallets() {
  return getWallets().get()
    .filter(supportsPumpWizard)
    .slice()
    .sort((left, right) => left.name.localeCompare(right.name))
}

export function watchWallets(update: (wallets: readonly StandardWallet[]) => void) {
  const registry = getWallets()
  const emit = () => update(detectedWallets())
  emit()
  const offRegister = registry.on('register', emit)
  const offUnregister = registry.on('unregister', emit)
  return () => { offRegister(); offUnregister() }
}

export function fromBase64(value: string) {
  const binary = atob(value)
  return Uint8Array.from(binary, (character) => character.charCodeAt(0))
}

export function toBase64(value: Uint8Array) {
  let binary = ''
  value.forEach((byte) => { binary += String.fromCharCode(byte) })
  return btoa(binary)
}

function shortVector(bytes: Uint8Array, offset: number) {
  let value = 0
  let shift = 0
  let cursor = offset
  while (cursor < bytes.length && shift <= 28) {
    const byte = bytes[cursor++]
    value |= (byte & 0x7f) << shift
    if ((byte & 0x80) === 0) return { value, cursor }
    shift += 7
  }
  throw new Error('The transaction uses an invalid compact length.')
}

export function signLegacyTransaction(wire: Uint8Array, pair: LocalKeypair) {
  const signatures = shortVector(wire, 0)
  if (signatures.value < 1 || signatures.value > 32) throw new Error('The transaction has an invalid signer count.')
  const messageOffset = signatures.cursor + signatures.value * 64
  if (messageOffset + 4 > wire.length) throw new Error('The transaction is truncated.')
  const message = wire.subarray(messageOffset)
  if (message[0] & 0x80) throw new Error('The browser wallet accepts only Runner legacy transactions.')
  const requiredSignatures = message[0]
  if (requiredSignatures !== signatures.value) throw new Error('The transaction signer header does not match its signatures.')
  const accounts = shortVector(message, 3)
  const accountBytes = accounts.value * 32
  if (accounts.value < requiredSignatures || accounts.cursor + accountBytes + 32 > message.length) {
    throw new Error('The transaction account list is invalid.')
  }
  let signerIndex = -1
  for (let index = 0; index < requiredSignatures; index += 1) {
    const start = accounts.cursor + index * 32
    if (pair.publicKey.every((byte, position) => message[start + position] === byte)) {
      signerIndex = index
      break
    }
  }
  if (signerIndex < 0) throw new Error('This wallet is not a required signer for the reviewed transaction.')
  const signed = new Uint8Array(wire)
  signed.set(nacl.sign.detached(message, pair.secretKey), signatures.cursor + signerIndex * 64)
  return signed
}

export class WalletTransactionError extends Error {
  diagnostic: SigningDiagnostic
  constructor(message: string, diagnostic: SigningDiagnostic) { super(message); this.diagnostic = diagnostic }
}

function signingDiagnostic(signed: Uint8Array, publicKey: Uint8Array, value: unknown): SigningDiagnostic {
  const detail: SigningDiagnostic = { wire_bytes: Math.min(signed.length, 16_384), signature_count: null, message: null, valid_signature: null,
    value_type: ArrayBuffer.isView(value) ? 'typed-array' : value instanceof ArrayBuffer ? 'array-buffer' : Array.isArray(value) ? 'array' : 'other' }
  try {
    const count = shortVector(signed, 0)
    const offset = count.cursor + count.value * 64
    if (count.cursor !== 1 || count.value < 1 || count.value > 32 || offset >= signed.length || signed.length > 16_384) return detail
    detail.signature_count = count.value
    // Only the message crosses the diagnostics API. No signature is retained.
    const message = signed.subarray(offset)
    detail.message = toBase64(message)
    if (count.value === 1 && publicKey.length === 32) detail.valid_signature = nacl.sign.detached.verify(message, signed.subarray(1, 65), publicKey)
  } catch { /* Malformed wallet results still have useful length/type evidence. */ }
  return detail
}

export function verifyReviewedTransaction(reviewed: Uint8Array, signed: Uint8Array, publicKey: Uint8Array, originalValue: unknown = signed) {
  // Compare a snapshot made before calling the wallet: an extension could mutate
  // the input buffer in place. Never repair or re-sign a changed transaction.
  if (reviewed.length <= 65 || reviewed[0] !== 1 || signed[0] !== 1 || signed.length !== reviewed.length) {
    throw new WalletTransactionError('The wallet returned a different transaction format. Runner did not submit it.', signingDiagnostic(signed, publicKey, originalValue))
  }
  const message = reviewed.subarray(65)
  if (!message.every((byte, index) => signed[65 + index] === byte)) {
    throw new WalletTransactionError('The wallet changed the transaction after review. Runner did not submit it.', signingDiagnostic(signed, publicKey, originalValue))
  }
  if (publicKey.length !== 32 || !nacl.sign.detached.verify(message, signed.subarray(1, 65), publicKey)) {
    throw new WalletTransactionError('The wallet returned an invalid signature for the reviewed transaction. Runner did not submit it.', signingDiagnostic(signed, publicKey, originalValue))
  }
}

async function sha256Hex(value: Uint8Array) {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', value))
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, '0')).join('')
}

/** Sign only the exact transaction carried by a server-issued allowance ticket.
 *
 * This adapter intentionally has no raw-transaction argument. Callers must hold
 * the ephemeral runtime binding, and only an unlocked generated wallet can use
 * it. The server independently repeats the exact-message and signature checks.
 */
export async function signMainWalletAutopilotTicket(
  session: WalletSession,
  runtime: MainWalletAutopilotRuntime,
  ticket: MainWalletSigningTicket,
) {
  if (session.kind !== 'local' || !session.signingAvailable || !session.publicKey) {
    throw new Error('Autopilot signing requires an unlocked PumpWizard Wallet.')
  }
  if (runtime.mode !== 'synthetic') throw new Error('Live main-wallet Autopilot is not enabled in this build.')
  if (ticket.schema !== 'pumpwizard.main-wallet-signing-ticket/v1'
    || ticket.allowance_id !== runtime.allowance_id
    || ticket.authorization_revision !== runtime.authorization_revision
    || ticket.sender !== session.address) {
    throw new Error('The signing ticket does not match this wallet allowance revision.')
  }
  const now = Date.now() / 1000
  if (!Number.isFinite(ticket.expires_at) || now >= ticket.expires_at
    || now >= runtime.expires_at || now >= runtime.heartbeat_deadline) {
    throw new Error('The Autopilot allowance, heartbeat, or signing ticket expired.')
  }
  const reviewed = fromBase64(ticket.unsigned_transaction)
  const signatures = shortVector(reviewed, 0)
  if (signatures.cursor !== 1 || signatures.value !== 1 || reviewed.length <= 65
    || reviewed.subarray(1, 65).some((byte) => byte !== 0)) {
    throw new Error('The signing ticket is not a single-signer unsigned Runner transaction.')
  }
  const message = reviewed.subarray(65)
  if (await sha256Hex(reviewed) !== ticket.wire_sha256
    || await sha256Hex(message) !== ticket.message_sha256) {
    throw new Error('The signing ticket transaction does not match its reviewed hashes.')
  }
  session.assertActive()
  const signed = await session.signTransaction(new Uint8Array(reviewed))
  session.assertActive()
  verifyReviewedTransaction(reviewed, signed, session.publicKey)
  return signed
}

export function formatSol(lamports: string | number) {
  try {
    const value = BigInt(lamports)
    const absolute = value < 0n ? -value : value
    const whole = absolute / 1_000_000_000n
    const fraction = (absolute % 1_000_000_000n).toString().padStart(9, '0').replace(/0+$/, '')
    return `${value < 0n ? '−' : ''}${whole.toLocaleString()}${fraction ? `.${fraction}` : ''} SOL`
  } catch {
    return '—'
  }
}

export function errorText(error: unknown) {
  return error instanceof Error ? error.message : 'The wallet operation did not complete.'
}

async function authorize(
  address: string,
  network: WalletNetwork,
  signMessage: (message: Uint8Array) => Promise<Uint8Array>,
) {
  const challenge = await api<WalletChallenge>('/api/v1/wallet/challenge', { sender: address, network })
  const message = fromBase64(challenge.message)
  const signature = await signMessage(message)
  await api('/api/v1/wallet/verify', {
    sender: address,
    network,
    challenge_id: challenge.challenge_id,
    signature: toBase64(signature),
  })
  return inspectWallet(address, network)
}

export async function inspectWallet(address: string, network: WalletNetwork) {
  return api<WalletInspection>('/api/v1/wallet/inspect', { sender: address, network })
}

export async function connectStandardWallet(wallet: StandardWallet, network: WalletNetwork): Promise<WalletSession> {
  const connect = feature<ConnectFeature>(wallet, 'standard:connect')
  const signMessage = feature<SignMessageFeature>(wallet, 'solana:signMessage')
  const signTransaction = feature<SignTransactionFeature>(wallet, 'solana:signTransaction')
  if (!connect || !signMessage || !signTransaction) throw new Error(`${wallet.name} does not expose the required Wallet Standard features.`)
  const connected = await connect.connect()
  const chain = chainFor(network)
  const account = [...connected.accounts, ...wallet.accounts].find((candidate) =>
    candidate.chains.includes(chain)
    && candidate.features.includes('solana:signMessage')
    && candidate.features.includes('solana:signTransaction'))
  if (!account) throw new Error(`${wallet.name} has no signing account for ${network}.`)
  const inspection = await authorize(account.address, network, async (message) => {
    const output = await signMessage.signMessage({ message, account })
    if (output.length !== 1 || toBase64(new Uint8Array(output[0].signedMessage)) !== toBase64(message)) {
      throw new Error('The wallet changed the sign-in message.')
    }
    return new Uint8Array(output[0].signature)
  })
  let active = true
  const listeners = new Set<() => void>()
  const matches = (accounts: readonly StandardAccount[]) => accounts.some((candidate) =>
    candidate.address === account.address && candidate.chains.includes(chain)
    && candidate.features.includes('solana:signMessage')
    && candidate.features.includes('solana:signTransaction'))
  const invalidate = () => { active = false; listeners.forEach((listener) => listener()) }
  const off = feature<EventsFeature>(wallet, 'standard:events')?.on('change', (properties) => {
    if ((properties.accounts && !matches(properties.accounts))
      || (properties.chains && !properties.chains.includes(chain))) invalidate()
  })
  const assertActive = () => {
    if (!active || !matches(wallet.accounts) || !wallet.chains.includes(chain)) {
      invalidate()
      throw new Error('Wallet account or network changed. Connect again and request a fresh review.')
    }
  }
  return {
    kind: 'extension',
    name: wallet.name,
    address: account.address,
    network,
    balance: inspection.balance,
    signingAvailable: true,
    publicKey: new Uint8Array(account.publicKey),
    assertActive,
    onInvalidated: (listener) => { listeners.add(listener); return () => { listeners.delete(listener) } },
    signMessage: async (message) => {
      assertActive()
      const reviewed = new Uint8Array(message)
      const output = await signMessage.signMessage({ message: reviewed, account })
      assertActive()
      if (output.length !== 1
        || toBase64(new Uint8Array(output[0].signedMessage)) !== toBase64(reviewed)) {
        throw new Error('The wallet changed the acceptance message.')
      }
      const signature = new Uint8Array(output[0].signature)
      if (signature.length !== 64) throw new Error('The wallet returned an invalid message signature.')
      return signature
    },
    signTransaction: async (transaction) => {
      assertActive()
      const reviewed = new Uint8Array(transaction)
      const output = await signTransaction.signTransaction({ transaction, account, chain })
      assertActive()
      if (output.length !== 1) throw new Error('The wallet returned an unexpected signing result.')
      const signed = new Uint8Array(output[0].signedTransaction)
      verifyReviewedTransaction(reviewed, signed, new Uint8Array(account.publicKey), output[0].signedTransaction)
      return signed
    },
    checkTransaction: async (transaction) => {
      assertActive()
      const output = await signTransaction.signTransaction({ transaction, account, chain })
      assertActive()
      if (output.length !== 1) throw new Error('The wallet returned an unexpected signing result.')
      return signingDiagnostic(new Uint8Array(output[0].signedTransaction), new Uint8Array(account.publicKey), output[0].signedTransaction)
    },
    disconnect: async () => { active = false; off?.(); await feature<DisconnectFeature>(wallet, 'standard:disconnect')?.disconnect() },
  }
}

async function vaultKey(password: string, salt: Uint8Array, usage: KeyUsage[]) {
  const passwordKey = await crypto.subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey'])
  return crypto.subtle.deriveKey(
    { name: 'PBKDF2', hash: 'SHA-256', salt, iterations: KDF_ITERATIONS },
    passwordKey,
    { name: 'AES-GCM', length: 256 },
    false,
    usage,
  )
}

function readVault(value: unknown): EncryptedWallet {
  if (!value || typeof value !== 'object') throw new Error('The encrypted wallet backup is invalid.')
  const vault = value as Partial<EncryptedWallet>
  if (vault.schema !== 'pumpwizard.browser-wallet/v1'
    || typeof vault.address !== 'string'
    || typeof vault.created_at !== 'string'
    || vault.kdf?.name !== 'PBKDF2'
    || vault.kdf.hash !== 'SHA-256'
    || vault.kdf.iterations !== KDF_ITERATIONS
    || typeof vault.kdf.salt !== 'string'
    || vault.cipher?.name !== 'AES-GCM'
    || typeof vault.cipher.iv !== 'string'
    || typeof vault.cipher.ciphertext !== 'string') {
    throw new Error('The encrypted wallet backup is invalid or uses an unsupported format.')
  }
  return vault as EncryptedWallet
}

export function storedWallets(): EncryptedWallet[] {
  return savedWalletRecords().map(readVault)
}

async function saveBrowserWallet(vault: EncryptedWallet) {
  await saveWalletRecord(vault)
}

async function encryptAndSaveWallet(pair: LocalKeypair, password: string) {
  if (password.length < 10) throw new Error('Use at least 10 characters for the wallet password.')
  const salt = crypto.getRandomValues(new Uint8Array(16))
  const iv = crypto.getRandomValues(new Uint8Array(12))
  const key = await vaultKey(password, salt, ['encrypt'])
  const ciphertext = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, pair.secretKey))
  const vault: EncryptedWallet = {
    schema: 'pumpwizard.browser-wallet/v1',
    address: toBase58(pair.publicKey),
    created_at: new Date().toISOString(),
    kdf: { name: 'PBKDF2', hash: 'SHA-256', iterations: KDF_ITERATIONS, salt: toBase64(salt) },
    cipher: { name: 'AES-GCM', iv: toBase64(iv), ciphertext: toBase64(ciphertext) },
  }
  await saveBrowserWallet(vault)
  return vault
}

export async function createBrowserWallet(password: string) {
  const pair = nacl.sign.keyPair()
  try { return await encryptAndSaveWallet(pair, password) } finally { pair.secretKey.fill(0) }
}

/** Transfers the transient keypair to the session only after server persistence
 * succeeds. Existing ciphertext and its password are never replaced. */
export async function signInWithImportedWallet(pair: LocalKeypair, password: string, network: WalletNetwork) {
  try {
    const address = toBase58(pair.publicKey)
    if (!storedWallets().some((wallet) => wallet.address === address)) await encryptAndSaveWallet(pair, password)
    return await localSession(pair, network)
  } catch (error) {
    pair.secretKey.fill(0)
    throw error
  }
}

export async function importBrowserWallet(value: unknown) {
  const vault = readVault(value)
  await saveBrowserWallet(vault)
  return vault
}

export async function removeBrowserWallet(address: string) {
  await removeWalletRecord(address)
}

async function decryptPair(vault: EncryptedWallet, password: string) {
  let plaintext: Uint8Array | undefined
  let pair: LocalKeypair | undefined
  try {
    const key = await vaultKey(password, fromBase64(vault.kdf.salt), ['decrypt'])
    plaintext = new Uint8Array(await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: fromBase64(vault.cipher.iv) },
      key,
      fromBase64(vault.cipher.ciphertext),
    ))
    pair = nacl.sign.keyPair.fromSecretKey(plaintext)
    if (toBase58(pair.publicKey) !== vault.address) throw new Error('Address mismatch')
    return pair
  } catch {
    pair?.secretKey.fill(0)
    throw new Error('The wallet password is incorrect or the backup is damaged.')
  } finally { plaintext?.fill(0) }
}

function serviceVaultAad(enrollment: Omit<ServiceVaultEnrollment, 'kdf' | 'cipher'>) {
  // Keep this order aligned with the service's canonical sorted JSON keys.
  return new TextEncoder().encode(JSON.stringify({
    address: enrollment.address,
    created_at: enrollment.created_at,
    custody: enrollment.custody,
    network: enrollment.network,
    schema: enrollment.schema,
    vault_id: enrollment.vault_id,
  }))
}

function randomHex(size: number) {
  return Array.from(crypto.getRandomValues(new Uint8Array(size)), (byte) => (
    byte.toString(16).padStart(2, '0')
  )).join('')
}

/** Prepare a service-specific encrypted envelope without persisting plaintext.
 *
 * The caller sends this envelope and the transient service unlock secret to the
 * authenticated local enrollment route. The existing browser vault is left
 * untouched and this adapter exposes no general signing or key-export API.
 */
export async function prepareServiceVaultEnrollment(
  address: string,
  walletPassword: string,
  serviceUnlockSecret: string,
  network: WalletNetwork,
): Promise<ServiceVaultEnrollment> {
  if (serviceUnlockSecret.length < 12) {
    throw new Error('Use at least 12 characters for the service wallet password.')
  }
  const vault = storedWallets().find((candidate) => candidate.address === address)
  if (!vault) throw new Error('The stored local wallet does not match this session.')
  const pair = await decryptPair(vault, walletPassword)
  const seed = pair.secretKey.slice(0, 32)
  try {
    const identity = {
      schema: 'pumpwizard.service-wallet-vault/v1' as const,
      vault_id: randomHex(16),
      custody: 'main-wallet-service' as const,
      address,
      network,
      created_at: new Date().toISOString(),
    }
    const salt = crypto.getRandomValues(new Uint8Array(16))
    const nonce = crypto.getRandomValues(new Uint8Array(12))
    const key = await vaultKey(serviceUnlockSecret, salt, ['encrypt'])
    const ciphertext = new Uint8Array(await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv: nonce, additionalData: serviceVaultAad(identity) },
      key,
      seed,
    ))
    return {
      ...identity,
      kdf: {
        name: 'PBKDF2',
        hash: 'SHA-256',
        iterations: KDF_ITERATIONS,
        salt: toBase64(salt),
      },
      cipher: {
        name: 'AES-256-GCM',
        nonce: toBase64(nonce),
        ciphertext: toBase64(ciphertext),
      },
    }
  } finally {
    seed.fill(0)
    pair.secretKey.fill(0)
  }
}

async function localSession(pair: LocalKeypair, network: WalletNetwork): Promise<WalletSession> {
  const address = toBase58(pair.publicKey)
  let inspection: WalletInspection
  try { inspection = await authorize(address, network, async (message) => nacl.sign.detached(message, pair.secretKey)) }
  catch (error) { pair.secretKey.fill(0); throw error }
  let active = true
  const assertActive = () => { if (!active) throw new Error('Wallet is locked. Unlock and request a fresh review.') }
  return {
    kind: 'local',
    name: 'PumpWizard Wallet',
    address,
    network,
    balance: inspection.balance,
    signingAvailable: true,
    publicKey: new Uint8Array(pair.publicKey),
    assertActive,
    signMessage: async (message) => {
      assertActive()
      return nacl.sign.detached(new Uint8Array(message), pair.secretKey)
    },
    signTransaction: async (wire) => { assertActive(); return signLegacyTransaction(wire, pair) },
    disconnect: async () => { active = false; pair.secretKey.fill(0) },
  }
}

export async function verifyBrowserWalletPassword(vault: EncryptedWallet, password: string) {
  const pair = await decryptPair(vault, password)
  pair.secretKey.fill(0)
}

export async function unlockBrowserWallet(vault: EncryptedWallet, password: string, network: WalletNetwork) {
  return localSession(await decryptPair(vault, password), network)
}

export function rememberWalletSession(session: WalletSession, market: TradingMarket | null) {
  try {
    serverStorage.setItem(WALLET_SESSION_STORAGE_KEY, JSON.stringify({
      schema: 'pumpwizard.wallet-session/v1',
      kind: session.kind,
      name: session.name,
      address: session.address,
      network: session.network,
      market,
    }))
  } catch { /* A blocked session store must not prevent an explicit wallet sign-in. */ }
}

export function forgetWalletSession() {
  try { serverStorage.removeItem(WALLET_SESSION_STORAGE_KEY) } catch { /* no stored session */ }
}

export async function restoreWalletSession(): Promise<RestoredWalletSession | null> {
  let raw: string | null = null
  try { raw = serverStorage.getItem(WALLET_SESSION_STORAGE_KEY) } catch { return null }
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as Record<string, unknown>
    if (value.schema !== 'pumpwizard.wallet-session/v1'
      || (value.kind !== 'local' && value.kind !== 'extension')
      || typeof value.name !== 'string' || !value.name || value.name.length > 120
      || typeof value.address !== 'string' || !/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(value.address)
      || (value.network !== 'mainnet-beta' && value.network !== 'devnet')
      || (value.market !== null && value.market !== 'pumpfun')) throw new Error('Invalid wallet session')
    // Restore the public dashboard without granting ownership proof or signing.
    // The service intentionally expires signing authorization across restarts.
    const inspection = await api<{ sender: string; network: string; balance: string }>(`/api/v1/wallet/balance?${new URLSearchParams({ sender: value.address, network: value.network })}`).catch(() => null)
    if (inspection && (inspection.sender !== value.address || inspection.network !== value.network)) throw new Error('Wallet balance belongs to another account')
    let active = true
    const assertActive = () => {
      if (!active) throw new Error('Wallet session is no longer active. Sign in again.')
    }
    const session: WalletSession = {
      kind: value.kind,
      name: value.name,
      address: value.address,
      network: value.network,
      balance: inspection?.balance || '',
      signingAvailable: false,
      assertActive,
      signMessage: async () => {
        assertActive()
        throw new Error('Unlock wallet signing after refresh before approving service acceptance.')
      },
      signTransaction: async () => {
        assertActive()
        throw new Error('Unlock wallet signing after refresh before approving a transaction.')
      },
      disconnect: async () => { active = false },
    }
    return { session, market: value.market }
  } catch {
    forgetWalletSession()
    return null
  }
}

export async function exportBrowserWalletSecret(address: string, password: string) {
  const vault = storedWallets().find((wallet) => wallet.address === address)
  if (!vault) throw new Error('The stored local wallet does not match this session.')
  const pair = await decryptPair(vault, password)
  const base58 = toBase58(pair.secretKey)
  const json = JSON.stringify(Array.from(pair.secretKey))
  pair.secretKey.fill(0)
  return { address, base58, json }
}

export async function revokeWallet(session: WalletSession) {
  await Promise.all([
    session.disconnect?.().catch(() => undefined),
    api('/api/v1/wallet/disconnect', { sender: session.address, network: session.network }).catch(() => undefined),
  ])
}

export function downloadVault(vault: EncryptedWallet) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(vault, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `pumpwizard-wallet-${vault.address.slice(0, 8)}.json`
  link.click()
  URL.revokeObjectURL(url)
}
