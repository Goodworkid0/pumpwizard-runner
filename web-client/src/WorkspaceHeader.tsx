export type WorkspaceDestination = 'dashboard' | 'research'

export function WorkspaceHeader({
  destination, select,
}: {
  destination: WorkspaceDestination
  select: (destination: WorkspaceDestination) => void
}) {
  return <nav className="command-board-navigation" aria-label="Primary navigation">
      <button className={`workspace-action${destination === 'dashboard' ? ' selected' : ''}`} aria-current={destination === 'dashboard' ? 'page' : undefined} onClick={() => select('dashboard')}>Dashboard</button>
  </nav>
}
