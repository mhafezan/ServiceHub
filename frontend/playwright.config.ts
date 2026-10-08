// Run deterministic Telegram Mini App journeys against Vite in a mobile Chromium context.

import {defineConfig, devices} from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  timeout: 30_000,
  expect: {timeout: 5_000},
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:5173',
    viewport: {width: 390, height: 844},
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium-mobile',
      use: {...devices['Desktop Chrome'], viewport: {width: 390, height: 844}},
    },
  ],
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5173 --strictPort',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
