import { expect, type Page, test } from "@playwright/test";
import { changeStats, resetApi } from "./support";

// The pretend API (e2e/fixture-api.mjs) answers with a busy afternoon: 16 cards wait for a later
// try, 2 links failed for good, and one vector job has waited 95 seconds. $0.41 of the daily
// budget of $1.00 is spent.

test.beforeEach(async ({ request }) => {
  await resetApi(request);
});

/** The values of one number on the page: the number, then its note or its warning. */
const figure = (page: Page, label: string) =>
  page.locator("dl > div").filter({ has: page.locator("dt", { hasText: label }) }).locator("dd");

const jobs = (page: Page) => page.getByRole("group", { name: /The jobs of each kind/ });
/** The cells of one kind: ready, scheduled, running, dead, retrying, done and dead in 24 h, wait */
const cellsOf = (page: Page, kind: string) =>
  jobs(page).getByRole("row", { name: new RegExp(`^${kind}`) }).getByRole("cell");
const rowHeader = (page: Page, kind: string) =>
  jobs(page).getByRole("rowheader", { name: new RegExp(`^${kind}`) });
/** The list of counts under a heading. */
const counts = (page: Page, title: string) =>
  page.getByRole("heading", { name: title }).locator("xpath=following-sibling::dl[1]");

test("the stats show the queue, the costs and the items of one moment", async ({ page }) => {
  await page.goto("/stats");

  await expect(page.locator("time")).toHaveText("26 October 2026 at 14:00:05 UTC");
  await expect(page.locator("time")).toHaveAttribute("datetime", "2026-10-26T14:00:05.250000Z");
  await expect(page.getByRole("link", { name: "Stats" })).toHaveAttribute("aria-current", "page");

  // The queue: the depth leaves the 2 dead jobs out.
  await expect(figure(page, "Jobs to do")).toHaveText(["18", "Ready, scheduled and running"]);
  await expect(figure(page, "Dead jobs")).toHaveText(["2", "No worker tries them again"]);
  await expect(figure(page, "Oldest wait")).toHaveText(["1 min 35 s", "Of a ready job"]);
  await expect(page.getByText("1 kind needs a look: fetch_item.")).toBeVisible();
  await expect(rowHeader(page, "fetch_item")).toContainText("Needs a look: 2 dead, 2 in 24 h");
  await expect(cellsOf(page, "fetch_item")).toHaveText("0 0 0 2 0 36 2 none".split(" "));
  await expect(rowHeader(page, "enrich_item")).not.toContainText("Needs a look");
  await expect(cellsOf(page, "enrich_item")).toHaveText("0 16 0 0 16 20 0 none".split(" "));
  await expect(cellsOf(page, "embed_item")).toHaveText([
    ..."1 0 1 0 0 18 0".split(" "),
    "1 min 35 s",
  ]);
  await expect(jobs(page).getByRole("row")).toHaveCount(11);

  // The costs: below the budget, so no warning. Less than a cent keeps two digits.
  await expect(page.getByText("of the daily budget of $1.00 spent today.")).toContainText("$0.41");
  await expect(page.getByText("The daily budget is used.")).toHaveCount(0);
  await expect(figure(page, "Last 30 days")).toHaveText(["$3.20", "With the answers of batches"]);
  const tasks = page.getByRole("group", { name: /each task/ }).getByRole("row");
  await expect(tasks).toHaveText([
    "TaskCallsInput tokensOutput tokensCost",
    "enrich_item4121,236,000185,400$2.81",
    "score_item640512,00032,000$0.32",
    "overview927,0003,600$0.07",
    "read_image11,500120$0.00027",
  ]);

  // The items.
  await expect(figure(page, "Read in the last 7 days")).toHaveText(["5"]);
  await expect(figure(page, "Median age of the unread items")).toHaveText([
    "4.5 days",
    "Since the last save, without the archived items",
  ]);
  const statuses = counts(page, "By reading status");
  await expect(statuses.locator("dt")).toHaveText([
    "Inbox",
    "Queued",
    "Reading",
    "Done",
    "Archived",
  ]);
  await expect(statuses.locator("dd")).toHaveText("31 3 1 12 4".split(" "));
  const states = counts(page, "By processing state");
  await expect(states.locator("dt")).toHaveText([
    "Waits for the fetch",
    "Waits for the card",
    "Waits for the vectors",
    "Ready",
    "Failed",
  ]);
  await expect(states.locator("dd")).toHaveText("0 16 2 31 2".split(" "));
});

test("a spend equal to the daily budget says that new calls wait for midnight", async ({
  page,
  request,
}) => {
  // The ledger stops a call when the spend is at least the budget (>=), not only above it.
  await changeStats(request, { llm: { spent_today_usd: 1, daily_budget_usd: 1 } });

  await page.goto("/stats");

  await expect(page.getByText("of the daily budget of $1.00 spent today.")).toContainText("$1.00");
  await expect(page.getByText("The daily budget is used.")).toBeVisible();
  const midnight = /^New model calls wait for midnight in UNSHELVE_TIMEZONE, when the budget/;
  await expect(page.getByText(midnight)).toBeVisible();
});

test("a ready job that waits over 5 minutes is marked, and one of 5 minutes is not", async ({
  page,
  request,
}) => {
  await changeStats(request, { by_kind: [{ kind: "embed_item", oldest_ready_seconds: 300 }] });
  await page.goto("/stats");

  await expect(figure(page, "Oldest wait")).toHaveText(["5 min", "Of a ready job"]);
  await expect(rowHeader(page, "embed_item")).not.toContainText("Needs a look");

  await changeStats(request, { by_kind: [{ kind: "embed_item", oldest_ready_seconds: 750 }] });
  await page.reload();

  await expect(figure(page, "Oldest wait")).toHaveText([
    "12 min 30 s",
    "Over 5 min: is a worker running?",
  ]);
  await expect(rowHeader(page, "embed_item")).toContainText("Needs a look: waits long");
  await expect(page.getByText("2 kinds need a look: fetch_item, embed_item.")).toBeVisible();
});

test("an old dead job is still marked: no worker ends it", async ({ page, request }) => {
  await changeStats(request, { by_kind: [{ kind: "fetch_item", dead_last_24_hours: 0 }] });

  await page.goto("/stats");

  await expect(rowHeader(page, "fetch_item")).toContainText("Needs a look: 2 dead");
  await expect(rowHeader(page, "fetch_item")).not.toContainText("in 24 h");
  await expect(page.getByText("1 kind needs a look: fetch_item.")).toBeVisible();
});

test("a new instance has nothing to mark and nothing to count", async ({ page, request }) => {
  await changeStats(request, { empty: true });

  await page.goto("/stats");

  await expect(figure(page, "Jobs to do")).toHaveText(["0", "Ready, scheduled and running"]);
  await expect(figure(page, "Dead jobs")).toHaveText(["0", "Failed for good"]);
  await expect(figure(page, "Oldest wait")).toHaveText(["None", "No job is ready"]);
  await expect(page.getByText("No dead jobs, and no job waits longer than 5 min.")).toBeVisible();
  await expect(page.getByText("No paid model calls in the last 30 days.")).toBeVisible();
  await expect(figure(page, "Median age of the unread items")).toHaveText([
    "None",
    "No item waits to be read",
  ]);
});

test("on a phone, the table of jobs scrolls in its box and the page does not", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/stats");
  await expect(cellsOf(page, "fetch_item")).toHaveCount(8);

  const whole = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    width: document.documentElement.clientWidth,
  }));
  expect(whole.scroll).toBeLessThanOrEqual(whole.width);
  const box = await jobs(page).evaluate((element) => ({
    scroll: element.scrollWidth,
    width: element.clientWidth,
  }));
  expect(box.scroll).toBeGreaterThan(box.width);

  // The box takes the focus, so the arrow keys can scroll it without a mouse. Chrome 132 and
  // newer focus a box that scrolls also without tabindex, so only the attribute shows that the
  // page asks for it in each browser.
  await expect(jobs(page)).toHaveAttribute("tabindex", "0");
  await jobs(page).focus();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => jobs(page).evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
});
