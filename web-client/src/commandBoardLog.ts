export type BoardStatus = 'checking' | 'ready' | 'info' | 'attention'
export type BoardSnapshot = {
  id: string
  label: string
  status: BoardStatus
  headline: string
  detail: string
  fingerprint?: string
  sessionId?: string
}
export type BoardLogEntry = BoardSnapshot & { sequence: number; at: number }
export type BoardLog = { entries: BoardLogEntry[]; latest: Record<string, { fingerprint: string; sequence: number }>; sequence: number }

export function appendStatusChanges(log: BoardLog, snapshot: BoardSnapshot[], at: number): BoardLog {
  const additions: BoardLogEntry[] = []
  const latest = { ...log.latest }
  let sequence = log.sequence
  for (const item of snapshot) {
    if (item.status === 'checking') continue
    const fingerprint = item.fingerprint ?? JSON.stringify([item.label, item.status, item.headline, item.detail])
    if (latest[item.id]?.fingerprint === fingerprint) continue
    const entry = { ...item, sequence: ++sequence, at }
    latest[item.id] = { fingerprint, sequence }
    additions.push(entry)
  }
  return additions.length ? { entries: [...log.entries, ...additions], latest, sequence } : log
}

export type BoardEvent = BoardSnapshot & { at: number }

export function appendBoardEvents(log: BoardLog, events: BoardEvent[]): BoardLog {
  let next = log
  for (const event of events) {
    // Event identity is immutable; renames and ordinary refreshes cannot replay it.
    if (next.latest[event.id]) continue
    next = appendStatusChanges(next, [event], event.at)
  }
  return next
}
