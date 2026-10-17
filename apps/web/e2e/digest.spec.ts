import { expect, type Page, test } from "@playwright/test";
import { digestMinutes, emptyDigest, resetApi } from "./support";

// The pretend API (e2e/fixture-api.mjs) has three made-up picks, in this order: item 1 for the
// focus goal (14 minutes), item 2 as a recent save, and item 5 as the random pick. Items 2 and
// 5 have no estimate, so each counts as 5 minutes.

test.beforeEach(async ({ request }) => {
  await resetApi(request);
});

// Only the visible line: after a new choice, the page keeps the old line hidden for a moment.
const summaryOf = (page: Page) =>
  page.locator("p", { hasText: "minutes." }).filter({ visible: true });
// Only the line above each card, not the lines inside the cards.
const reasonsOf = (page: Page) =>
  page.getByRole("list", { name: "Digest" }).locator(":scope > li > p");

test("the digest shows each pick under the reason that chose it", async ({ page, request }) => {
  await page.goto("/digest");

  await expect(summaryOf(page)).toHaveText(
    "3 items, about 24 of 60 minutes. Focus goal: Database internals.",
  );
  await expect(reasonsOf(page)).toHaveText([
    "For your focus goal: Database internals",
    "You saved it in the last 7 days",
    "A random pick from your inbox",
  ]);
  await expect(page.getByRole("list", { name: "Digest" }).getByRole("heading")).toHaveText([
    "Leases for Job Queues in Postgres",
    "صف کارها در پستگرس",
    "A note with a strange address",
  ]);
  await expect(page.getByRole("link", { name: "Digest" })).toHaveAttribute("aria-current", "page");
  expect(await digestMinutes(request)).toEqual([60]);
});

test("a shorter time to read asks the API for a smaller digest", async ({ page, request }) => {
  await page.goto("/digest");
  await expect(summaryOf(page)).toContainText("of 60 minutes");

  await page.getByLabel("Time to read").selectOption("15");
  await page.getByRole("button", { name: "Show" }).click();

  await expect(page).toHaveURL(/\/digest\?minutes=15$/);
  await expect(summaryOf(page)).toHaveText(
    "1 item, about 14 of 15 minutes. Focus goal: Database internals.",
  );
  await expect(reasonsOf(page)).toHaveText(["For your focus goal: Database internals"]);
  await expect(page.getByLabel("Time to read")).toHaveValue("15");
  expect(await digestMinutes(request)).toEqual([60, 15]);
});

test("a reading time that the page does not offer becomes one hour", async ({ page, request }) => {
  await page.goto("/digest?minutes=7");

  await expect(page.getByLabel("Time to read")).toHaveValue("60");
  await expect(summaryOf(page)).toContainText("of 60 minutes");
  expect(await digestMinutes(request)).toEqual([60]);
});

test("an empty digest says when an item comes back", async ({ page, request }) => {
  await emptyDigest(request);

  await page.goto("/digest");

  await expect(page.getByText("Nothing to bring back now.")).toBeVisible();
  await expect(page.getByRole("list", { name: "Digest" })).toHaveCount(0);
});
