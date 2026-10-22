// Official SPL layout/metadata codecs; synthetic identities, no network or signing.
const fs = require('node:fs');
const path = require('node:path');
require('node:net').Socket.prototype.connect = () => { throw new Error('Network forbidden'); };
const base = process.env.PUMP_SDK_NODE_MODULES;
if (!base) throw new Error('Set PUMP_SDK_NODE_MODULES');
const spl = require(path.join(base, '@solana/spl-token'));
const {pack} = require(path.join(base, '@solana/spl-token-metadata'));
const {PublicKey} = require(path.join(base, '@solana/web3.js'));
const mint = new PublicKey(Buffer.alloc(32,9)), owner = new PublicKey(Buffer.alloc(32,1));
const mintBase = Buffer.alloc(166), accountBase = Buffer.alloc(166);
spl.MintLayout.encode({mintAuthorityOption: 0, mintAuthority: PublicKey.default,
  supply: 1000000000000000n, decimals: 6, isInitialized: true,
  freezeAuthorityOption: 0, freezeAuthority: PublicKey.default}, mintBase);
mintBase[165] = spl.AccountType.Mint;
const pointer = Buffer.alloc(spl.METADATA_POINTER_SIZE);
spl.MetadataPointerLayout.encode({authority: PublicKey.default, metadataAddress: mint}, pointer);
const metadata = Buffer.from(pack({mint, name: 'Synthetic fixture', symbol: 'TEST',
  uri: 'https://example.invalid/never-fetched.json', additionalMetadata: [['fixture','offline']]}));
function tlv(kind, data) {
  const header = Buffer.alloc(4); header.writeUInt16LE(kind); header.writeUInt16LE(data.length,2);
  return Buffer.concat([header,data]);
}
spl.AccountLayout.encode({mint, owner, amount: 123456789n, delegateOption: 0,
  delegate: PublicKey.default, state: spl.AccountState.Initialized, isNativeOption: 0,
  isNative: 0n, delegatedAmount: 0n, closeAuthorityOption: 0,
  closeAuthority: PublicKey.default}, accountBase);
accountBase[165] = spl.AccountType.Account;
const result = {
  provenance: {kind: 'offline-official-spl-codecs', spl_token: require(path.join(base,'@solana/spl-token/package.json')).version,
    spl_token_metadata: require(path.join(base,'@solana/spl-token-metadata/package.json')).version},
  mint: mint.toBase58(), owner: owner.toBase58(), token_program: spl.TOKEN_2022_PROGRAM_ID.toBase58(),
  mint_base64: Buffer.concat([mintBase,tlv(spl.ExtensionType.MetadataPointer,pointer),tlv(spl.ExtensionType.TokenMetadata,metadata)]).toString('base64'),
  token_base64: Buffer.concat([accountBase,tlv(spl.ExtensionType.ImmutableOwner,Buffer.alloc(0))]).toString('base64'),
};
fs.writeFileSync(path.join(__dirname,'../tests/fixtures/pump-route/token2022-layouts.json'), JSON.stringify(result,null,2)+'\n');
console.log('Captured Token-2022 layouts using official SPL codecs');
