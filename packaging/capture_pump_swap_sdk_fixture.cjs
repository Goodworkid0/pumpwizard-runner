// Offline instruction oracle. PUMP_SDK_NODE_MODULES must contain
// @pump-fun/pump-swap-sdk@1.20.0; no RPC, keys, signing, or funds.
const path = require('node:path');
const fs = require('node:fs');
require('node:net').Socket.prototype.connect = () => { throw new Error('Network forbidden'); };
const base = process.env.PUMP_SDK_NODE_MODULES;
if (!base) throw new Error('Set PUMP_SDK_NODE_MODULES');
const sdk = require(path.join(base, '@pump-fun/pump-swap-sdk'));
const { PublicKey, TransactionInstruction } = require(path.join(base, '@solana/web3.js'));
const { TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, NATIVE_MINT, getAssociatedTokenAddressSync: ata,
  createAssociatedTokenAccountInstruction } = require(path.join(base, '@solana/spl-token'));
const BN = require(path.join(base, 'bn.js'));
const token2022 = process.env.PUMP_TOKEN_2022 === '1';
const holderReward = process.env.PUMP_HOLDER_REWARD === '1';
const baseProgram = token2022 ? TOKEN_2022_PROGRAM_ID : TOKEN_PROGRAM_ID;
const pub = n => new PublicKey(Buffer.alloc(32, n));
const user = pub(1), mint = pub(9), fee = pub(8), buyback = pub(6);
const coinCreator = holderReward ? require(path.join(base, '@pump-fun/pump-sdk')).holderRewardsPda(mint) : pub(7);
const creator = sdk.pumpPoolAuthorityPda(mint);
const poolKey = sdk.poolPda(0, creator, mint, NATIVE_MINT);
const pool = {
  index: 0, creator, baseMint: mint, quoteMint: NATIVE_MINT,
  lpMint: sdk.lpMintPda(poolKey), poolBaseTokenAccount: ata(mint, poolKey, true, baseProgram),
  poolQuoteTokenAccount: ata(NATIVE_MINT, poolKey, true), coinCreator,
  isMayhemMode: false, isCashbackCoin: false, virtualQuoteReserves: new BN(0),
  creatorFeeBps: new BN(0), canEditCreatorFee: false, isHolderReward: holderReward,
};
const state = {
  user, poolKey, pool, baseMint: mint, baseTokenProgram: baseProgram,
  quoteTokenProgram: TOKEN_PROGRAM_ID,
  globalConfig: {protocolFeeRecipients: [fee], buybackFeeRecipients: [buyback]},
  poolAccountInfo: {data: Buffer.alloc(300), owner: sdk.PUMP_AMM_PROGRAM_ID},
  userBaseTokenAccount: ata(mint, user, true, baseProgram), userQuoteTokenAccount: ata(NATIVE_MINT, user, true),
  userBaseAccountInfo: {data: Buffer.alloc(165), owner: baseProgram}, userQuoteAccountInfo: null,
};
function record(ix) {
  return {program: ix.programId.toBase58(), data: ix.data.toString('hex'), accounts: ix.keys.map(
    k => ({address: k.pubkey.toBase58(), signer: k.isSigner, writable: k.isWritable}))};
}
(async () => {
  const instructions = await sdk.PUMP_AMM_SDK.sellInstructions(state, new BN(400000000), new BN(39000000));
  // Intentional stricter policy: atomically reject an existing wSOL account.
  instructions[0] = createAssociatedTokenAccountInstruction(user, state.userQuoteTokenAccount, user, NATIVE_MINT);
  const buyInstructions = await sdk.PUMP_AMM_SDK.buyInstructions(
    {...state, userBaseAccountInfo: null}, new BN(900000000), new BN(100000000));
  buyInstructions[0] = createAssociatedTokenAccountInstruction(
    user, state.userQuoteTokenAccount, user, NATIVE_MINT);
  const buyIndex = buyInstructions.findIndex(ix =>
    ix.programId.equals(sdk.PUMP_AMM_PROGRAM_ID) && ix.data.length > 8);
  if (buyIndex < 0) throw new Error('Official SDK did not build a PumpSwap buy instruction');
  buyInstructions[buyIndex] = new TransactionInstruction({
    programId: buyInstructions[buyIndex].programId,
    keys: buyInstructions[buyIndex].keys,
    data: sdk.OFFLINE_PUMP_AMM_PROGRAM.coder.instruction.encode('buyExactQuoteIn', {
      spendableQuoteIn: new BN(100000000),
      minBaseAmountOut: new BN(900000000),
      trackVolume: {0: true},
    }),
  });
  const result = {
    provenance: {sdk: '@pump-fun/pump-swap-sdk@1.20.0', kind: 'offline-sdk-instructions',
      sdk_tarball_sha256: 'c558b4fb8ca7f5cdcd7dfaa23a9b026212380ecf2ba54efe651f264745a7a9f1',
      ata_policy: 'Official SPL create (non-idempotent), to reject pre-existing wSOL atomically'},
    inputs: {sender: user.toBase58(), mint: mint.toBase58(), creator: coinCreator.toBase58(),
      fee: fee.toBase58(), buyback: buyback.toBase58(), pool: poolKey.toBase58(),
      global: sdk.GLOBAL_CONFIG_PDA.toBase58(), token_program: baseProgram.toBase58()},
    instructions: instructions.map(record),
    buy_instructions: buyInstructions.map(record),
  };
  if (holderReward) result.provenance.reward_mode = 'holders; creator from official holderRewardsPda(mint)';
  const filename = `swap-sdk-${holderReward ? 'holder-' : ''}${token2022 ? 'token2022-' : ''}instructions.json`;
  fs.writeFileSync(path.join(__dirname, '../tests/fixtures/pump-route', filename), JSON.stringify(result, null, 2) + '\n');
  console.log('Captured official SDK migration buy/sell instructions without network access');
})().catch(err => { console.error(err); process.exit(1); });
