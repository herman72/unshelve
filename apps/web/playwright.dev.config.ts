import path from "node:path";
import { defineConfig } from "@playwright/test";
import { DEV_API_PORT, DEV_API_URL, DEV_LOG, DEV_WEB_PORT } from "./e2e-dev/support";

// A check of each page under `next dev`, against the pretend API (e2e/fixture-api.mjs).
// `next dev` checks more than the production build: it renders each page in stages, as a
// prerender would see it, and it logs an error for what a prerender must not do. The end-to-end
// tests (playwright.config.ts) run the production build, so they cannot see these errors. This
// config sends the output of `next dev` into a file, and the spec reads it.
//
//     pnpm --filter @unshelve/web test:dev
//
// Next.js allows one `next dev` for each folder: stop your own before you run the check. When
// the server does not start, the reason is in test-results/next-dev.log.
export default defineConfig({
  testDir: "./e2e-dev",
  outputDir: "test-results/dev",
  workers: 1,
  forbidOnly: !!process.env.CI,
  reporter: "list",
  // `next dev` compiles each page at its first visit, which can take seconds on a CI runner.
  timeout: 180_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: `http://127.0.0.1:${DEV_WEB_PORT}`,
    channel: "chrome",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: `node e2e/fixture-api.mjs ${DEV_API_PORT}`,
      url: `${DEV_API_URL}/api/v1/readyz`,
      reuseExistingServer: false,
    },
    {
      command: `mkdir -p "${path.dirname(DEV_LOG)}" && exec next dev -p ${DEV_WEB_PORT} -H 127.0.0.1 > "${DEV_LOG}" 2>&1`,
      // Not /, which the spec opens first: a route error of / would then be logged twice.
      url: `http://127.0.0.1:${DEV_WEB_PORT}/login`,
      env: { UNSHELVE_API_INTERNAL_URL: DEV_API_URL },
      timeout: 120_000,
      reuseExistingServer: false,
    },
  ],
});
