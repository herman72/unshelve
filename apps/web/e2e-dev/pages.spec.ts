import { readFileSync } from "node:fs";
import { stripVTControlCharacters } from "node:util";
import { expect, test } from "@playwright/test";
import { DEV_LOG } from "./support";

// Each page of the app, with the made-up items of e2e/fixture-api.mjs: item 1 is in the inbox
// and item 1000 is done. /dev/preview exists only under `next dev`.
const PAGES = [
  "/",
  "/goals",
  "/items/1",
  "/items/1000",
  "/search?q=postgres",
  "/queue",
  "/digest",
  "/login",
  "/dev/preview",
];

// `next dev` logs a route error just after the answer: less than 0.1 s after it on a Mac, also
// with all its cores busy. This wait after the last page leaves a wide margin for a slow runner.
const LATE_ERRORS_MS = 3_000;

// A route error starts with `Error: Route "/path"`, as the Next.js docs show it. The pattern also
// lets a marker such as "⨯" come first.
const ROUTE_ERROR = /^\W*Error: Route "/;

/** The first line is enough to name an error: a server error comes with a long stack. */
function firstLine(text: string): string {
  return text.split("\n", 1)[0];
}

test("next dev logs no route error, and the browser no error, on each page", async ({ page }) => {
  const browserErrors: string[] = [];
  let current = "";
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(`${current}: ${firstLine(message.text())}`);
  });
  page.on("pageerror", (error) => browserErrors.push(`${current}: ${firstLine(error.message)}`));

  for (const path of PAGES) {
    current = path;
    const answer = await page.goto(path);
    expect.soft(answer?.status(), `the status of ${path}`).toBe(200);
    // The footer reads the pretend API at request time: when its line is there, the page has
    // rendered with data, not as an error page.
    await expect.soft(page.locator("footer")).toContainText("API ready, version fixture");
  }
  await page.waitForTimeout(LATE_ERRORS_MS);

  // Playwright starts `next dev` with FORCE_COLOR, so the lines can hold colour codes.
  const routeErrors = readFileSync(DEV_LOG, "utf8")
    .split(/\r?\n/)
    .map((line) => stripVTControlCharacters(line))
    .filter((line) => ROUTE_ERROR.test(line));
  expect.soft(routeErrors, `route errors in ${DEV_LOG}`).toEqual([]);
  expect(browserErrors, "errors in the browser console").toEqual([]);
});
