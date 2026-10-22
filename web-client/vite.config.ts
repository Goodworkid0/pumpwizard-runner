import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const projectRoot = fileURLToPath(new URL('..', import.meta.url))
const versionMatch = readFileSync(`${projectRoot}/pyproject.toml`, 'utf8').match(/^version = "([^"]+)"$/m)
if (!versionMatch) throw new Error('Runner version is missing from pyproject.toml')
const runnerVersion = process.env.PUMPWIZARD_FRONTEND_VERSION || versionMatch[1]
const frontendBuild = {
  schema: 'pumpwizard.build-identity/v1',
  build_id: process.env.PUMPWIZARD_BUILD_ID || 'development',
  version: runnerVersion,
  source_checkpoint: process.env.PUMPWIZARD_SOURCE_CHECKPOINT || 'development',
  packaged_at: process.env.PUMPWIZARD_PACKAGED_AT || 'development',
  package_kind: process.env.PUMPWIZARD_PACKAGE_KIND || 'source',
} as const

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const { PUMPWIZARD_BROWSER_TOKEN: browserToken } = loadEnv(mode, '.', 'PUMPWIZARD_')
  return {
    plugins: [react()],
    define: {
      __PUMPWIZARD_FRONTEND_BUILD__: JSON.stringify(frontendBuild),
    },
    server: {
      proxy: {
        '/api': {
          target: 'http://127.0.0.1:8765',
          changeOrigin: false,
          configure(proxy) {
            proxy.on('proxyReq', (request) => {
              request.setHeader('Origin', 'http://127.0.0.1:5173')
              if (browserToken) request.setHeader('X-PumpWizard-Browser-Token', browserToken)
            })
          },
        },
      },
    },
  }
})
