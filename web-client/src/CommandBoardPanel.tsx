import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useDialogFocus } from './useDialogFocus'
import './command-board-panel.css'

type DetachedWindow = { window: Window; root: HTMLDivElement }

// Move the existing React view, rather than starting another app or wallet session.
export function CommandBoardPanel({ children, settings, scope }: {
  children: ReactNode
  settings: ReactNode
  scope: string
}) {
  const [detached, setDetached] = useState<DetachedWindow | null>(null)
  const [expanded, setExpanded] = useState(true)
  const [error, setError] = useState('')
  const detachButton = useRef<HTMLButtonElement>(null)
  useDialogFocus(detached?.root.ownerDocument ?? null)

  useEffect(() => {
    if (!detached) return
    const popup = detached.window
    const popupDocument = detached.root.ownerDocument
    const returnFocus = detachButton.current
    const restore = () => setDetached((current) => current === detached ? null : current)
    const close = () => popup.close()
    const copyStyles = () => {
      popupDocument.head.querySelectorAll('[data-command-board-style]').forEach((node) => node.remove())
      document.head.querySelectorAll('style, link[rel="stylesheet"]').forEach((node) => {
        const copy = node.cloneNode(true) as HTMLElement
        if (node instanceof HTMLLinkElement) (copy as HTMLLinkElement).href = node.href
        copy.dataset.commandBoardStyle = ''
        popupDocument.head.append(copy)
      })
    }
    copyStyles()
    const styles = new MutationObserver(copyStyles)
    styles.observe(document.head, { childList: true, subtree: true, characterData: true })
    window.addEventListener('pagehide', close)
    window.addEventListener('beforeunload', close)
    // Firefox sends pagehide while initializing a newly opened about:blank.
    // An unload notification can also precede a cancelled navigation. Check
    // the actual window/document instead of treating either event as closure.
    const closedCheck = window.setInterval(() => {
      let unavailable = popup.closed
      if (!unavailable) {
        try { unavailable = popup.document !== popupDocument || !detached.root.isConnected }
        catch { unavailable = true }
      }
      if (unavailable) restore()
    }, 500)
    popupDocument.querySelector<HTMLElement>('.command-chat input:not(:disabled), .command-board-popout-bar button')?.focus()
    return () => {
      styles.disconnect()
      window.clearInterval(closedCheck)
      window.removeEventListener('pagehide', close)
      window.removeEventListener('beforeunload', close)
      popup.close()
      returnFocus?.focus()
    }
  }, [detached])

  const detach = () => {
    if (detached && !detached.window.closed) {
      detached.window.focus()
      return
    }
    // An empty URL keeps the initial document. Explicit about:blank starts an
    // asynchronous navigation in Firefox and discards our new portal root.
    const popup = window.open('', '_blank', 'popup=yes,width=1000,height=720,resizable=yes,scrollbars=yes')
    if (!popup) {
      setError('The new window was blocked. Allow pop-ups for this app, then try again.')
      return
    }
    popup.document.title = 'PumpWizard — System activity & AI chat'
    popup.document.documentElement.lang = document.documentElement.lang || 'en'
    const viewport = popup.document.createElement('meta')
    viewport.name = 'viewport'
    viewport.content = 'width=device-width, initial-scale=1'
    popup.document.head.append(viewport)
    popup.document.body.className = 'command-board-popout-body'
    const root = popup.document.createElement('div')
    root.id = 'command-board-root'
    popup.document.body.append(root)
    setError('')
    setDetached({ window: popup, root })
    popup.focus()
  }

  const restore = () => {
    setExpanded(true)
    setDetached(null)
    window.focus()
  }
  const content = <div className="command-board-content">{children}{settings}</div>
  return <div className="command-board-panel">
    <details className="command-board-details" open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary>System activity &amp; AI chat{detached && <span className="command-board-detached-label"> · separate window</span>}</summary>
      {detached
        ? <div className="command-board-detached-placeholder"><span>Activity and chat are open in a separate window.</span><button onClick={restore}>Return here</button></div>
        : content}
    </details>
    <button ref={detachButton} className="command-board-detach-button" onClick={detach} title={detached ? 'Focus the activity and chat window' : 'Open only activity and chat in a movable window'}>{detached ? 'Focus window' : 'Open in new window'} <span aria-hidden="true">↗</span></button>
    {error && <p className="command-board-window-error" role="alert">{error}</p>}
    {detached && createPortal(<main className="assistant-header command-board command-board-popout" aria-label="System activity and AI chat window">
      <div className="agent-terminal">
        <header className="command-board-popout-bar"><div><h1>System activity &amp; AI chat</h1><p>{scope} · Live with your Dashboard. Keep it open.</p></div><button onClick={restore}>Return to Dashboard</button></header>
        {content}
      </div>
    </main>, detached.root)}
  </div>
}
