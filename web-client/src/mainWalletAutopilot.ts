import {
  api,
  signMainWalletAutopilotTicket,
} from './browserWallet'
import type {
  MainWalletAutopilotRuntime,
  MainWalletSigningTicket,
  WalletSession,
} from './browserWallet'

export type MainWalletRuntimeState = {
  phase: 'idle' | 'checking' | 'signing' | 'locked'
  detail: string
  last_check: number | null
  order_id?: string
}

type NextTicket = {
  ticket: MainWalletSigningTicket | null
  detail: string
}

/** Browser-held signer loop for an already activated synthetic allowance.
 *
 * The loop cannot accept arbitrary bytes: it only signs a typed, hash-bound
 * server ticket through signMainWalletAutopilotTicket. The runtime token and
 * generated-wallet key remain in memory and are never written to storage.
 */
export class MainWalletAutopilotBrowserRuntime {
  private timer: number | null = null
  private running = false
  private busy = false
  private invalidated: (() => void) | null = null
  private readonly wallet: WalletSession
  private readonly runtime: MainWalletAutopilotRuntime
  private readonly update: (state: MainWalletRuntimeState) => void
  private readonly pageHide = () => { this.lockOnPageHide() }

  constructor(
    wallet: WalletSession,
    runtime: MainWalletAutopilotRuntime,
    update: (state: MainWalletRuntimeState) => void,
  ) {
    if (wallet.kind !== 'local' || !wallet.signingAvailable) {
      throw new Error('Autopilot requires an unlocked PumpWizard Wallet.')
    }
    if (runtime.mode !== 'synthetic') {
      throw new Error('Live main-wallet Autopilot is not enabled in this build.')
    }
    this.wallet = wallet
    this.runtime = runtime
    this.update = update
  }

  start() {
    if (this.running) return
    this.running = true
    this.invalidated = this.wallet.onInvalidated?.(() => { void this.stop('Wallet disconnected or changed') }) || null
    window.addEventListener('pagehide', this.pageHide)
    this.timer = window.setInterval(
      () => { void this.cycle() },
      Math.max(5_000, this.runtime.heartbeat_seconds * 1_000),
    )
    void this.cycle()
  }

  get allowanceId() {
    return this.runtime.allowance_id
  }

  async pauseEntries(paused: boolean) {
    if (!this.running) throw new Error('Autopilot runtime is not active.')
    await api('/api/v1/main-wallet-autopilot/pause', {
      sender: this.wallet.address,
      network: this.wallet.network,
      allowance_id: this.runtime.allowance_id,
      runtime_token: this.runtime.runtime_token,
      authorization_revision: this.runtime.authorization_revision,
      paused,
    })
    this.update({
      phase: 'idle',
      detail: paused ? 'Entries paused; eligible managed exits remain active' : 'Entries resumed within the same allowance',
      last_check: Date.now() / 1000,
    })
  }

  async cycle() {
    if (!this.running || this.busy) return
    this.busy = true
    this.update({ phase: 'checking', detail: 'Refreshing allowance heartbeat', last_check: Date.now() / 1000 })
    try {
      this.wallet.assertActive()
      const heartbeat = await api<{ heartbeat_deadline: number }>(
        '/api/v1/main-wallet-autopilot/heartbeat',
        {
          sender: this.wallet.address,
          network: this.wallet.network,
          allowance_id: this.runtime.allowance_id,
          runtime_token: this.runtime.runtime_token,
          authorization_revision: this.runtime.authorization_revision,
        },
      )
      this.runtime.heartbeat_deadline = heartbeat.heartbeat_deadline
      const next = await api<NextTicket>('/api/v1/main-wallet-autopilot/next', {
        sender: this.wallet.address,
        network: this.wallet.network,
        allowance_id: this.runtime.allowance_id,
        runtime_token: this.runtime.runtime_token,
        authorization_revision: this.runtime.authorization_revision,
      })
      if (!next.ticket) {
        this.update({ phase: 'idle', detail: next.detail, last_check: Date.now() / 1000 })
        return
      }
      this.update({ phase: 'signing', detail: 'Verifying the exact prepared transaction', last_check: Date.now() / 1000, order_id: next.ticket.order_id })
      const signed = await signMainWalletAutopilotTicket(this.wallet, this.runtime, next.ticket)
      // A failed/uncertain response stops this loop. The server must recover the
      // original ticket/signature; this client never requests or signs a replacement.
      await api('/api/v1/main-wallet-autopilot/signed', {
        sender: this.wallet.address,
        network: this.wallet.network,
        allowance_id: this.runtime.allowance_id,
        runtime_token: this.runtime.runtime_token,
        authorization_revision: this.runtime.authorization_revision,
        ticket_id: next.ticket.id,
        signed_transaction: toBase64(signed),
      })
      this.update({ phase: 'idle', detail: 'Original signature recorded for reconciliation', last_check: Date.now() / 1000, order_id: next.ticket.order_id })
    } catch (reason) {
      await this.stop(reason instanceof Error ? reason.message : 'Autopilot runtime stopped')
    } finally {
      this.busy = false
    }
  }

  async stop(detail = 'Autopilot stopped') {
    if (!this.running) return
    this.running = false
    if (this.timer !== null) window.clearInterval(this.timer)
    this.timer = null
    this.invalidated?.()
    this.invalidated = null
    window.removeEventListener('pagehide', this.pageHide)
    try {
      await api('/api/v1/main-wallet-autopilot/lock', {
        sender: this.wallet.address,
        network: this.wallet.network,
        allowance_id: this.runtime.allowance_id,
        runtime_token: this.runtime.runtime_token,
      })
    } catch { /* Heartbeat expiry and server-side locking remain authoritative. */ }
    this.update({ phase: 'locked', detail, last_check: Date.now() / 1000 })
  }

  private lockOnPageHide() {
    if (!this.running) return
    this.running = false
    if (this.timer !== null) window.clearInterval(this.timer)
    const body = JSON.stringify({
      sender: this.wallet.address,
      network: this.wallet.network,
      allowance_id: this.runtime.allowance_id,
      runtime_token: this.runtime.runtime_token,
    })
    try {
      navigator.sendBeacon(
        '/api/v1/main-wallet-autopilot/lock',
        new Blob([body], { type: 'application/json' }),
      )
    } catch { /* The short heartbeat still fails closed when pagehide cannot send. */ }
  }
}

function toBase64(value: Uint8Array) {
  let binary = ''
  value.forEach((byte) => { binary += String.fromCharCode(byte) })
  return btoa(binary)
}
