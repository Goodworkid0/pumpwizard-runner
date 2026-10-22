import type { WalletSession } from './browserWallet'

export function WalletIdentity({ wallet, connectWallet, unlockSigning }: {
  wallet: WalletSession | null
  connectWallet: () => void
  unlockSigning: () => void
}) {
  const name = wallet ? /\bwallet$/i.test(wallet.name.trim()) ? wallet.name : `${wallet.name} wallet` : ''
  const address = wallet ? `${wallet.address.slice(0, 8)}…${wallet.address.slice(-8)}` : ''

  return <section className={`command-board-wallet${wallet && !wallet.signingAvailable ? ' signing-locked' : ''}`} aria-label="Wallet connection">
    <div className="command-board-wallet-details">
      <small>{wallet ? wallet.signingAvailable ? 'CONNECTED WALLET' : 'RESTORED WALLET VIEW' : 'WALLET'}</small>
      <b className={wallet ? 'wallet-address' : undefined} title={wallet?.address}>{wallet ? <><span aria-hidden="true">{address}</span><span className="sr-only">{wallet.address}</span></> : 'No wallet connected'}</b>
      {wallet && <em>{name}{wallet.signingAvailable ? ' connected' : ''} · {wallet.network}{wallet.signingAvailable ? '' : ' · signing locked'}</em>}
    </div>
    {wallet
      ? !wallet.signingAvailable && <button type="button" className="wallet-unlock" onClick={unlockSigning}><i aria-hidden="true" />Unlock signing</button>
      : <button type="button" onClick={connectWallet}>Connect wallet</button>}
  </section>
}
