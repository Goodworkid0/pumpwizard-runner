// Offline compiler oracle. No RPC, wallet, private key or signatures.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
require('node:net').Socket.prototype.connect = () => { throw new Error('Network forbidden'); };
const base = process.env.PUMP_SDK_NODE_MODULES;
if (!base) throw new Error('Set PUMP_SDK_NODE_MODULES to an isolated node_modules directory');
const {Message, PublicKey, Transaction} = require(path.join(base, '@solana/web3.js'));
const version = require(path.join(base, '@solana/web3.js/package.json')).version;
const cases = [];
for (let variant = 0; variant < 8; variant++) {
  const keys = Array.from({length: 28}, (_, i) => new PublicKey(crypto.createHash('sha256').update(`wallet-layout-${variant}-${i}`).digest()));
  // Case-only differences exercise the SDK's secondary collation rule.
  keys[5] = new PublicKey('A' + 'a'.repeat(42));
  keys[6] = new PublicKey('a' + 'A'.repeat(42));
  const required = variant % 2 ? 3 : 1;
  const header = {numRequiredSignatures:required, numReadonlySignedAccounts:required === 3 ? 1 : 0, numReadonlyUnsignedAccounts:9};
  const instructions = [
    {programIdIndex:27, accounts:Array.from({length:27},(_,i)=>i).reverse(), data:'1112'},
    {programIdIndex:26, accounts:[0,1,0,5,6,8], data:'1234'},
  ];
  const input = new Message({header,accountKeys:keys.map(k=>k.toBase58()), recentBlockhash:new PublicKey(Buffer.alloc(32,7)).toBase58(),instructions});
  const parsed = Transaction.populate(input);
  const rebuilt = new Transaction({feePayer:parsed.feePayer,recentBlockhash:parsed.recentBlockhash}).add(...parsed.instructions);
  cases.push({name:`privileges-and-collation-${variant}`,input_message:input.serialize().toString('base64'),wallet_message:rebuilt.serializeMessage().toString('base64')});
}
const result = {provenance:{sdk:`@solana/web3.js@${version}`,kind:'offline-legacy-rebuild',note:'Unsigned synthetic identities, all privilege groups and repeated instruction accounts; no RPC or signing'},cases};
fs.writeFileSync(path.join(__dirname,'../tests/fixtures/pump-route/wallet-message-layout.json'),JSON.stringify(result,null,2)+'\n');
console.log(`Captured ${cases.length} independent web3.js legacy message cases`);
