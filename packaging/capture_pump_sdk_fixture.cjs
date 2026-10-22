// Offline development fixture. Run with PUMP_SDK_NODE_MODULES pointing at an
// isolated npm install of @pump-fun/pump-sdk@2.0.0 (rpc-websockets@9.2.0 for Node 20).
// No connections, wallet secrets or signing; public synthetic identities only.
const path = require('node:path');
const fs = require('node:fs');
require('node:net').Socket.prototype.connect = () => { throw new Error('Network forbidden'); };
const base = process.env.PUMP_SDK_NODE_MODULES;
if (!base) throw new Error('Set PUMP_SDK_NODE_MODULES to the isolated node_modules directory');
const { PUMP_SDK, holderRewardsPda } = require(path.join(base, '@pump-fun/pump-sdk'));
const { PublicKey } = require(path.join(base, '@solana/web3.js'));
const { TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, NATIVE_MINT } = require(path.join(base, '@solana/spl-token'));
const token2022 = process.env.PUMP_TOKEN_2022 === '1';
const holderReward = process.env.PUMP_HOLDER_REWARD === '1';
const BN = require(path.join(base, 'bn.js'));
const pub = n => new PublicKey(Buffer.alloc(32, n));
const input = {user: pub(1), mint: pub(9), creator: holderReward ? holderRewardsPda(pub(9)) : pub(7), feeRecipient: pub(8),
  buybackFeeRecipient: pub(6), tokenProgram: token2022 ? TOKEN_2022_PROGRAM_ID : TOKEN_PROGRAM_ID,
  quoteMint: NATIVE_MINT, quoteTokenProgram: TOKEN_PROGRAM_ID};
function record(ix) {
  return {program: ix.programId.toBase58(), data: ix.data.toString('hex'), accounts: ix.keys.map(
    k => ({address: k.pubkey.toBase58(), signer: k.isSigner, writable: k.isWritable}))};
}
(async () => {
  const buy = await PUMP_SDK.getBuyV2InstructionRaw({...input, amount: new BN(100000000), quoteAmount: new BN(990000000)});
  buy.data = PUMP_SDK.offlinePumpProgram.coder.instruction.encode('buyExactQuoteInV2', {
    spendableQuoteIn: new BN(100000000), minTokensOut: new BN(990000000)});
  const sell = await PUMP_SDK.getSellV2InstructionRaw({...input, amount: new BN(400000000), quoteAmount: new BN(39000000)});
  const result = {provenance: {sdk: '@pump-fun/pump-sdk@2.0.0', kind: 'offline-sdk-instructions',
    note: 'SDK raw instruction builders and its Anchor instruction coder; no RPC or funds'},
    inputs: Object.fromEntries(Object.entries(input).map(([k,v])=>[k,v.toBase58()])),
    buy: record(buy), sell: record(sell)};
  if (holderReward) result.provenance.reward_mode = 'holders; creator from official holderRewardsPda(mint)';
  const filename = `sdk-${holderReward ? 'holder-' : ''}${token2022 ? 'token2022-' : ''}instructions.json`;
  fs.writeFileSync(path.join(__dirname, '../tests/fixtures/pump-route', filename), JSON.stringify(result,null,2)+'\n');
  console.log('Captured independent official SDK buy/sell instruction fixtures');
})().catch(err => { console.error(err); process.exit(1); });
