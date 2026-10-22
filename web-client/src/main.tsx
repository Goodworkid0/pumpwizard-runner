import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.tsx'
import { initializeServerStorage } from './serverStorage'

const root = createRoot(document.getElementById('root')!)
async function start(migrateLegacy = true) {
  root.render(<p role="status">Loading saved accounts from the local app…</p>)
  try {
    await initializeServerStorage(migrateLegacy)
    root.render(<StrictMode><App /></StrictMode>)
  } catch (error) {
    root.render(<div role="alert"><p>{error instanceof Error ? error.message : 'Local app storage is unavailable.'}</p><p>Existing saved accounts were kept. Try again to finish migration, or use accounts already saved in the local app.</p><button onClick={() => void start()}>Try again</button><button onClick={() => void start(false)}>Use saved app accounts</button></div>)
  }
}
void start()
