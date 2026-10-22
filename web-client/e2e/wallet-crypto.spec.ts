import { expect, test } from '@playwright/test'
import nacl from 'tweetnacl'
import { deriveEd25519Seed, privateKeyWallet, recoveryDerivationPath, recoveryPhraseWallet, toBase58 } from '../src/walletSecrets'

const phrase = `${'abandon '.repeat(11)}about`
const hex = (value: Uint8Array) => Buffer.from(value).toString('hex')

test('accepts complete base58 and JSON keys and rejects address-only or inconsistent exports', () => {
  const pair = nacl.sign.keyPair.fromSeed(new Uint8Array(32).fill(7))
  for (const input of [toBase58(pair.secretKey), JSON.stringify([...pair.secretKey])]) {
    expect(privateKeyWallet(input)).toEqual(pair)
  }
  for (const input of [toBase58(pair.publicKey), toBase58(pair.secretKey.subarray(0, 32)), '0badkey', '', '[1,2]', '1'.repeat(89), JSON.stringify([...pair.secretKey].map((value, index) => index === 0 ? -1 : value)), JSON.stringify([...pair.secretKey].map((value, index) => index === 0 ? String(value) : value))]) {
    expect(() => privateKeyWallet(input)).toThrow('complete 64-byte')
  }
  pair.secretKey[63] ^= 1
  expect(() => privateKeyWallet(toBase58(pair.secretKey))).toThrow('does not match')
})

test('matches official SLIP-0010 Ed25519 master and hardened child test vectors', async () => {
  // https://github.com/satoshilabs/slips/blob/master/slip-0010.md#test-vector-1-for-ed25519
  const seed = Uint8Array.from(Buffer.from('000102030405060708090a0b0c0d0e0f', 'hex'))
  const vectors = [
    ['m', '2b4be7f19ee27bbf30c667b642d5f4aa69fd169872f8fc3059c08ebae2eb19e7'],
    ["m/0'", '68e0fe46dfb67e368c75379acec591dad19df3cde26e63b93a8e704f1dade7a3'],
    ["m/0'/1'", 'b1d0bad404bf35da785a64ca1ac54b2617211d2777696fbffaf208f746ae84f2'],
    ["m/0'/1'/2'/2'/1000000000'", '8f94d394a8e8fd6b1bc2f3f49f5c47e385281d5c17e65324b0f62483e37e8793'],
  ]
  for (const [path, expected] of vectors) expect(hex(await deriveEd25519Seed(seed, path))).toBe(expected)
  for (const path of ['m/0', "m/2147483648'", "m/-1'", "m/1'/0"]) await expect(deriveEd25519Seed(seed, path)).rejects.toThrow()
})

test('derives known Solana addresses across account paths and the BIP39 TREZOR passphrase', async () => {
  // Independent Python hashlib.pbkdf2_hmac + HMAC-SHA512 + cryptography Ed25519
  // reference, using the published Trezor BIP39 zero-entropy mnemonic vector.
  const fixtures = [
    ['', 'bip44-change', 0, 'HAgk14JpMQLgt6rVgv7cBQFJWFto5Dqxi472uT3DKpqk'],
    ['TREZOR', 'bip44-change', 0, '7zSmbu6gKkb6HB7UDPtHYjwCWuBHU1D4TpNZFm4sndQe'],
    ['', 'bip44-change', 1, 'Hh8QwFUA6MtVu1qAoq12ucvFHNwCcVTV7hpWjeY1Hztb'],
    ['', 'bip44', 0, 'GjJyeC1r2RgkuoCWMyPYkCWSGSGLcz266EaAkLA27AhL'],
    ['', 'legacy', 0, 'D2PPQSYFe83nDzk96FqGumVU8JA7J8vj2Rhjc2oXzEi5'],
  ] as const
  for (const [passphrase, path, account, address] of fixtures) {
    const pair = await recoveryPhraseWallet(phrase, passphrase, path, account)
    expect(toBase58(pair.publicKey)).toBe(address)
    pair.secretKey.fill(0)
  }
  expect(toBase58((await recoveryPhraseWallet(`  ${phrase.toUpperCase().replaceAll(' ', '\n\t')}  `)).publicKey)).toBe(fixtures[0][3])
  expect(recoveryDerivationPath('bip44-change', 99)).toBe("m/44'/501'/99'/0'")
})

test('rejects invalid phrase lengths, words, checksum and unsupported account indices', async () => {
  for (const value of ['abandon '.repeat(12), 'abandon '.repeat(11), phrase.replace('about', 'nonsenseword')]) {
    await expect(recoveryPhraseWallet(value)).rejects.toThrow('valid English recovery phrase')
  }
  for (const account of [-1, 0.5, 100, Number.NaN]) await expect(recoveryPhraseWallet(phrase, '', 'bip44-change', account)).rejects.toThrow('account index')
  const valid24 = `${'abandon '.repeat(23)}art`
  const pair = await recoveryPhraseWallet(valid24)
  expect(pair.secretKey).toHaveLength(64)
  pair.secretKey.fill(0)
})
