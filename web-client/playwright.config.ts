import { defineConfig } from '@playwright/test'
import { existsSync } from 'node:fs'

const systemChromium = process.env.PLAYWRIGHT_CHROMIUM_PATH
  || (existsSync('/usr/bin/chromium') ? '/usr/bin/chromium' : undefined)
const projectPython = process.env.PUMPWIZARD_E2E_PYTHON || 'python3'
const port = Number(process.env.PUMPWIZARD_E2E_PORT || 8766)
const webRoot = process.env.PUMPWIZARD_E2E_WEB_ROOT || 'dist'
const outputDir = process.env.PUMPWIZARD_E2E_OUTPUT_DIR || 'test-results'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: 'line',
  outputDir,
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    browserName: 'chromium',
    headless: true,
    launchOptions: systemChromium ? { executablePath: systemChromium } : {},
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `PYTHONPATH=../src ${projectPython} ../tests/browser_e2e_server.py --web-root ${webRoot} --port ${port}`,
    url: `http://127.0.0.1:${port}/`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
})
