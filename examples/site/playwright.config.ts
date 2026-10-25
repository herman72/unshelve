import { defineConfig } from "@playwright/test";

const PORT = 3920;

// The example site, built for production, in the Google Chrome of the computer: nothing is
// downloaded.
//
//     pnpm --filter @unshelve/example-site test:e2e
export default defineConfig({
  testDir: "./e2e",
  forbidOnly: !!process.env.CI,
  reporter: "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    channel: "chrome",
    // UTC-10 all year: 07:00 UTC, the time of each fixture item, is 21:00 of the day before
    // here. A date that the page does not write in UTC is then another day in the browser than
    // on the server, and React reports it.
    timezoneId: "Pacific/Honolulu",
    trace: "retain-on-failure",
  },
  webServer: {
    command: `next build && next start -p ${PORT} -H 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}`,
    timeout: 300_000,
    reuseExistingServer: false,
  },
});
