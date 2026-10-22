import { mnemonicToSeed, validateMnemonic } from '@scure/bip39'
import { wordlist } from '@scure/bip39/wordlists/english'
import nacl from 'tweetnacl'

export type LocalKeypair = { publicKey: Uint8Array; secretKey: Uint8Array }
export type RecoveryPath = 'bip44-change' | 'bip44' | 'legacy'
const alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'

export function toBase58(bytes: Uint8Array) {
  let value = 0n
  for (const byte of bytes) value = value * 256n + BigInt(byte)
  let encoded = ''
  while (value > 0n) {
    encoded = alphabet[Number(value % 58n)] + encoded
    value /= 58n
  }
  for (const byte of bytes) {
    if (byte !== 0) break
    encoded = `1${encoded}`
  }
  return encoded || '1'
}

export function privateKeyWallet(input: string): LocalKeypair {
  const text = input.trim()
  let bytes: Uint8Array
  const invalid = 'Use a complete 64-byte Solana private key in base58 or a JSON byte array.'
  if (text.length > 2048) throw new Error(invalid)
  if (text.startsWith('[')) {
    let values: unknown
    try { values = JSON.parse(text) } catch { throw new Error(invalid) }
    if (!Array.isArray(values) || values.length !== 64
      || !values.every((value) => Number.isInteger(value) && value >= 0 && value <= 255)) throw new Error(invalid)
    bytes = Uint8Array.from(values)
    values.fill(0)
  } else {
    if (!text || text.length > 88) throw new Error(invalid)
    let value = 0n
    for (const character of text) {
      const digit = alphabet.indexOf(character)
      if (digit < 0) throw new Error(invalid)
      value = value * 58n + BigInt(digit)
    }
    const decoded: number[] = []
    while (value > 0n) {
      decoded.push(Number(value % 256n))
      value /= 256n
    }
    for (const character of text) {
      if (character !== '1') break
      decoded.push(0)
    }
    bytes = Uint8Array.from(decoded.reverse())
    decoded.fill(0)
  }
  let pair: LocalKeypair | undefined
  try {
    // A public address or a bare seed is not a complete exported Solana keypair.
    if (bytes.length !== 64) throw new Error(invalid)
    const seed = bytes.slice(0, 32)
    try { pair = nacl.sign.keyPair.fromSeed(seed) } finally { seed.fill(0) }
    let mismatch = 0
    for (let index = 0; index < 64; index++) mismatch |= bytes[index] ^ pair.secretKey[index]
    if (mismatch) throw new Error('The private key’s public address does not match its secret bytes.')
    return pair
  } catch (error) {
    pair?.secretKey.fill(0)
    throw error
  } finally { bytes.fill(0) }
}

export function recoveryDerivationPath(kind: RecoveryPath, account: number) {
  if (!Number.isInteger(account) || account < 0 || account > 99) throw new Error('Use an account index from 0 to 99.')
  if (kind === 'legacy') return "m/44'/501'"
  if (kind === 'bip44') return `m/44'/501'/${account}'`
  if (kind === 'bip44-change') return `m/44'/501'/${account}'/0'`
  throw new Error('Choose a supported Solana derivation path.')
}

async function hmac(keyBytes: Uint8Array, data: Uint8Array) {
  const key = await crypto.subtle.importKey('raw', keyBytes, { name: 'HMAC', hash: 'SHA-512' }, false, ['sign'])
  return new Uint8Array(await crypto.subtle.sign('HMAC', key, data))
}

/** Ed25519 hardened derivation from SLIP-0010, using platform HMAC-SHA512. */
export async function deriveEd25519Seed(seed: Uint8Array, path: string) {
  if (!/^m(?:\/\d+')*$/.test(path) || path.length > 200) throw new Error('Ed25519 requires a hardened derivation path.')
  const indices = path.split('/').slice(1).map((part) => Number(part.slice(0, -1)))
  if (indices.some((index) => !Number.isSafeInteger(index) || index < 0 || index >= 0x80000000)) throw new Error('The derivation index is invalid.')
  let material = await hmac(new TextEncoder().encode('ed25519 seed'), seed)
  try {
    for (const index of indices) {
      const data = new Uint8Array(37)
      data.set(material.subarray(0, 32), 1)
      new DataView(data.buffer).setUint32(33, index + 0x80000000, false)
      try {
        const next = await hmac(material.subarray(32), data)
        material.fill(0)
        material = next
      } finally { data.fill(0) }
    }
    return material.slice(0, 32)
  } finally { material.fill(0) }
}

export async function recoveryPhraseWallet(phrase: string, passphrase = '', kind: RecoveryPath = 'bip44-change', account = 0): Promise<LocalKeypair> {
  const path = recoveryDerivationPath(kind, account)
  const normalized = phrase.normalize('NFKD').trim().toLowerCase().split(/\s+/).join(' ')
  if (normalized.length > 1024 || !validateMnemonic(normalized, wordlist)) {
    throw new Error('Enter a valid English recovery phrase with 12, 15, 18, 21 or 24 words. Check the words and their order.')
  }
  if (passphrase.length > 1024) throw new Error('The recovery passphrase is too long.')
  const seed = await mnemonicToSeed(normalized, passphrase)
  let derived: Uint8Array | undefined
  try {
    derived = await deriveEd25519Seed(seed, path)
    return nacl.sign.keyPair.fromSeed(derived)
  } finally {
    seed.fill(0)
    derived?.fill(0)
  }
}
