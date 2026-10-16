import { expect, test } from "@playwright/test";
import { changeItem, reprocessed, resetApi, verdicts } from "./support";

// The made-up items of e2e/fixture-api.mjs: 1 is ready (a PDF with each field), 2 is Persian,
// 3 is in the pipeline, 4 failed, 5 has an address that is no web address. 1000 is done, so it
// is not in the inbox.

test.beforeEach(async ({ request }) => {
  await resetApi(request);
});

// --- The inbox ---------------------------------------------------------------------------

test("the inbox shows each item, and the state of its pipeline when it is not ready", async ({
  page,
}) => {
  await page.goto("/");

  const cards = page.getByRole("article");
  await expect(cards).toHaveCount(5);
  await expect(cards.filter({ hasText: "example.com/talks/leases" })).toContainText(
    "Pipeline: Processing",
  );
  await expect(cards.filter({ hasText: "a-page-that-is-gone" })).toContainText("Pipeline: Failed");
  // Ready is the normal case and takes no room.
  await expect(cards.filter({ hasText: "Leases for Job Queues" })).not.toContainText("Pipeline:");
});

test("Persian text runs right to left, and English text left to right", async ({ page }) => {
  await page.goto("/");

  const persian = page.getByRole("heading", { name: "صف کارها در پستگرس" });
  const english = page.getByRole("heading", { name: "Leases for Job Queues in Postgres" });
  await expect(persian).toHaveAttribute("dir", "auto");
  expect(await persian.evaluate((element) => element.matches(":dir(rtl)"))).toBe(true);
  expect(await english.evaluate((element) => element.matches(":dir(ltr)"))).toBe(true);
});

test("the title of a card opens the page of its item", async ({ page }) => {
  await page.goto("/");

  await page.getByRole("link", { name: "Leases for Job Queues in Postgres" }).click();

  await expect(page).toHaveURL(/\/items\/1$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "Leases for Job Queues in Postgres",
  );
});

test("a saved address that is no web address is never a link", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("article")).toHaveCount(5);
  expect(await page.locator('a[href^="javascript:"]').count()).toBe(0);

  await page.goto("/items/1");
  // The link is shown as text, so nothing is hidden, but it opens nothing.
  await expect(page.getByText("javascript:alert(1)")).toBeVisible();
  expect(await page.locator('a[href^="javascript:"]').count()).toBe(0);
  await expect(page.getByRole("link", { name: "https://example.org/queue-data" })).toBeVisible();
});

// --- The page of an item -----------------------------------------------------------------

test("the page of an item shows its facts, its summary, its key points and its note", async ({
  page,
}) => {
  await page.goto("/items/1");

  const article = page.getByRole("article");
  await expect(article).toContainText("by A. Made-up and B. Example");
  await expect(article).toContainText("Pipeline: Ready");
  await expect(article).toContainText("14 min read");
  await expect(article).toContainText("A job queue on one Postgres table, made safe with leases.");
  await expect(page.getByRole("listitem").filter({ hasText: "A lease ends" })).toBeVisible();
  await expect(article).toContainText("Read before the design review.");
  await expect(page.getByRole("link", { name: "Open the source" })).toHaveAttribute(
    "href",
    "https://example.com/papers/leases.pdf",
  );
});

test("the Persian summary and key points of an item run right to left", async ({ page }) => {
  await page.goto("/items/2");

  const point = page.getByRole("listitem").filter({ hasText: "اجاره" });
  expect(await point.evaluate((element) => element.matches(":dir(rtl)"))).toBe(true);
  const summary = page.getByText("یک صف کار روی یک جدول Postgres.");
  expect(await summary.evaluate((element) => element.matches(":dir(rtl)"))).toBe(true);
});

test("an item that does not exist shows that it does not", async ({ page }) => {
  await page.goto("/items/999");

  await expect(page.getByText("No such item")).toBeVisible();
});

test("an address that is no id of an item is no item", async ({ page }) => {
  // "1e3" is a number for JavaScript (1000), but no id of the API: item 1000 exists, and the
  // page must not show it.
  await page.goto("/items/1e3");

  await expect(page.getByText("No such item")).toBeVisible();
  await expect(page.getByText("Item one thousand")).toHaveCount(0);
});

// --- Back to the start -------------------------------------------------------------------

for (const { id, state } of [
  { id: 1, state: "Ready" },
  { id: 4, state: "Failed" },
]) {
  test(`an item that is ${state.toLowerCase()} goes back to the start`, async ({
    page,
    request,
  }) => {
    await page.goto(`/items/${id}`);
    await expect(page.getByRole("article")).toContainText(`Pipeline: ${state}`);

    await page.getByRole("button", { name: "Process again" }).click();

    // The page fetches the item again: it is in the pipeline now, and has no button.
    await expect(page.getByRole("article")).toContainText("Pipeline: Processing");
    await expect(page.getByText("The worker is at work on this item.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Process again" })).toHaveCount(0);
    expect(await reprocessed(request)).toEqual([id]);
  });
}

test("an item in the pipeline cannot be sent back to the start", async ({ page }) => {
  await page.goto("/items/3");

  await expect(page.getByRole("article")).toContainText("Pipeline: Processing");
  await expect(page.getByText("The worker is at work on this item.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Process again" })).toHaveCount(0);
});

test("a click after the worker started shows the state that won", async ({ page, request }) => {
  await page.goto("/items/1");
  // The page shows the button; then the worker starts on the item, before the click.
  await changeItem(request, 1, { enrich_state: "fetched" });

  await page.getByRole("button", { name: "Process again" }).click();

  // The API answers 409, and the page fetches the item again: it shows the state that won.
  await expect(page.getByRole("article")).toContainText("Pipeline: Processing");
  expect(await reprocessed(request)).toEqual([]);
});

// --- Scores against the goals ------------------------------------------------------------

test("the page of an item shows its score and the reason for each goal", async ({ page }) => {
  await page.goto("/items/1");

  const scores = page.getByRole("list", { name: "Scores" }).getByRole("listitem");
  await expect(scores).toHaveCount(2);
  await expect(scores.nth(0)).toContainText("Database internals: Directly useful (3 of 3)");
  await expect(scores.nth(0)).toContainText("one Postgres table holds a job queue");
  await expect(scores.nth(1)).toContainText("Learn Finnish: No link (0 of 3)");
});

test("the card of an item in the inbox names its top goal", async ({ page }) => {
  await page.goto("/");

  const card = page.getByRole("article").filter({ hasText: "Leases for Job Queues in Postgres" });
  await expect(card).toContainText("Top goal: Database internals (3 of 3)");
  // An item without a score has no top goal.
  await expect(page.getByRole("article").filter({ hasText: "Top goal" })).toHaveCount(1);
});

test("an item without scores has no section of goals", async ({ page }) => {
  await page.goto("/items/2");

  await expect(page.getByRole("heading", { name: "Reading" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Goals" })).toHaveCount(0);
});

// --- Your verdicts on the scores ---------------------------------------------------------

test("a verdict on a score shows on the page, and Undo takes it back", async ({
  page,
  request,
}) => {
  await page.goto("/items/1");
  const first = page.getByRole("list", { name: "Scores" }).getByRole("listitem").nth(0);

  await first.getByRole("button", { name: "Right" }).click();

  await expect(first).toContainText("You said: right.");
  await first.getByRole("button", { name: "Undo" }).click();
  await expect(first.getByRole("button", { name: "Wrong" })).toBeVisible();
  expect(await verdicts(request)).toEqual([
    { item: 1, goal: 1, label: "confirmed" },
    { item: 1, goal: 1, label: null },
  ]);
});

test("a verdict on a score that changed since the page showed it is refused", async ({
  page,
  request,
}) => {
  await page.goto("/items/1");
  // A new score for the goal while the page is open: 1, not the 3 that the page shows.
  await changeItem(request, 1, {
    goals: [
      {
        goal_id: 1,
        goal_slug: "database-internals",
        goal_title: "Database internals",
        score: 1,
        reason: "It touches the goal.",
        label: null,
      },
    ],
  });
  const first = page.getByRole("list", { name: "Scores" }).getByRole("listitem").nth(0);

  await first.getByRole("button", { name: "Wrong" }).click();

  await expect(page.getByRole("status").filter({ hasText: "Not saved" })).toHaveText(
    "Not saved: the score changed since you saw it: it is 1 now; look at it again.",
  );
  await expect(first).toContainText("Touches the goal (1 of 3)");
  expect(await verdicts(request)).toEqual([]);
});
