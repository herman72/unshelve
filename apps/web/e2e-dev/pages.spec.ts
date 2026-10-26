import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { stripVTControlCharacters } from "node:util";
import { expect, test } from "@playwright/test";
import { DEV_LOG } from "./support";

// Each page of the app, with the made-up items of e2e/fixture-api.mjs: item 1 is in the inbox
// and item 1000 is done. /dev/preview exists only under `next dev`. A new page needs an entry
// here: the last test of this file fails when a page of src/app has none.
const PAGES = [
  "/",
  "/goals",
  "/items/1",
  "/items/1000",
  "/search?q=postgres",
  "/queue",
  "/digest",
  "/stats",
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

const APP_DIR = path.join(__dirname, "..", "src", "app");

/**
 * The route of each page file of the app, from its folders: src/app/items/[id]/page.tsx is
 * /items/[id]. A route group, (name), adds no segment; a private folder, _name, has no route.
 */
function appRoutes(dir = APP_DIR, route = ""): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    if (entry.isFile()) return /^page\.[jt]sx?$/.test(entry.name) ? [route || "/"] : [];
    if (!entry.isDirectory() || entry.name.startsWith("_")) return [];
    const segment = /^\(.+\)$/.test(entry.name) ? "" : `/${entry.name}`;
    return appRoutes(path.join(dir, entry.name), route + segment);
  });
}

/** A pattern for the addresses of a route: [id] is one segment, [...slug] is one or more. */
function addressesOf(route: string): RegExp {
  const segments = route.split("/").map((segment) => {
    if (segment.startsWith("[...")) return ".+";
    if (segment.startsWith("[")) return "[^/]+";
    return segment.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  });
  return new RegExp(`^${segments.join("/")}$`);
}

test("the list of pages has an entry for each page of the app", () => {
  const routes = appRoutes();
  // A wrong folder would find no page, and then each page would seem to have an entry.
  expect(routes, `the pages in ${APP_DIR}`).toContain("/");
  const paths = PAGES.map((page) => new URL(page, "http://localhost").pathname);
  const missing = routes.filter((route) => !paths.some((p) => addressesOf(route).test(p)));
  expect(missing, "pages of src/app with no entry in PAGES").toEqual([]);
});
