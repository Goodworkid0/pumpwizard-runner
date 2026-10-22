import { useEffect } from 'react'

// All dialogs, including wallet prompts, retain keyboard focus until dismissed.
export function useDialogFocus(ownerDocument: Document | null = document) {
  useEffect(() => {
    if (!ownerDocument) return
    const document = ownerDocument
    let active: HTMLElement | null = null
    const previous = new Map<HTMLElement, HTMLElement | null>()
    const dialogs = () => Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"][aria-modal="true"]')).filter((el) => el.getClientRects().length)
    const focusables = (dialog: HTMLElement) => Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], summary, [tabindex="0"]')).filter((el) => el.getClientRects().length)
    const update = () => {
      const rows = dialogs(), next = rows[rows.length-1] || null
      if (next === active) return
      if (active && !active.isConnected) {
        const restored = previous.get(active)
        if (restored?.isConnected) restored.focus()
        previous.delete(active)
      }
      active = next
      if (next) {
        if (!previous.has(next)) previous.set(next, document.activeElement as HTMLElement | null)
        if (!next.contains(document.activeElement)) { next.tabIndex = -1; (focusables(next)[0] || next).focus() }
      }
    }
    const keydown = (event: KeyboardEvent) => {
      if (!active) return
      if (event.key === 'Escape') { const close = active.querySelector<HTMLButtonElement>('button.close:not(:disabled)'); if (close) { event.preventDefault(); close.click() } }
      if (event.key !== 'Tab') return
      const rows = focusables(active), first = rows[0], last = rows[rows.length-1]
      if (!first) { event.preventDefault(); active.focus(); return }
      if (event.shiftKey && (document.activeElement === first || !active.contains(document.activeElement))) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && (document.activeElement === last || !active.contains(document.activeElement))) { event.preventDefault(); first.focus() }
    }
    const observer = new MutationObserver(update)
    observer.observe(document.body, { childList: true, subtree: true })
    document.addEventListener('keydown', keydown)
    update()
    return () => { observer.disconnect(); document.removeEventListener('keydown', keydown) }
  }, [ownerDocument])
}
