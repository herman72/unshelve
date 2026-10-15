import { defineConfig } from "@playwright/test";
import { API_URL, API_PORT, WEB_PORT } from "./e2e/support";

// The end-to-end tests run the production build of the web app against a pretend API
// (e2e/fixture-api.mjs). They use the Google Chrome of the computer, so nothing is downloaded.
//
//     pnpm --filter @unshelve/web test:e2e
export default defineConfig({
  testDir: "./e2e",
  // The pretend API keeps its items in memory, and each test starts them again: one at a time.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  reporter: "list",
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    channel: "chrome",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: `node e2e/fixture-api.mjs ${API_PORT}`,
      url: `${API_URL}/api/v1/readyz`,
      reuseExistingServer: false,
    },
    {
      // The address of the API is part of the build (the rewrite of /api in next.config.ts),
      // so the tests build the app for the pretend API. The standalone server of the build
      // needs the static files next to it.
      command:
        "next build && cp -r .next/static .next/standalone/apps/web/.next/ && node .next/standalone/apps/web/server.js",
      url: `http://127.0.0.1:${WEB_PORT}`,
      env: { UNSHELVE_API_INTERNAL_URL: API_URL, PORT: String(WEB_PORT), HOSTNAME: "127.0.0.1" },
      timeout: 300_000,
      reuseExistingServer: false,
    },
  ],
});
