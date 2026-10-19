import { expect, test } from "@playwright/test";
import { addEvent, changeItem, moves, resetApi } from "./support";

// The made-up items of e2e/fixture-api.mjs: 1 to 5 are in the inbox, 6 is a merged copy of 1,
// and 1000 is done. The pretend API has the table of moves of the real API, and a reading
// queue of 5 slots.

test.beforeEach(async ({ request }) => {
  await resetApi(request);
});

const buttonsOf = (page: import("@playwright/test").Page) =>
  page.getByRole("group", { name: "Move the item" }).getByRole("button");

test("the page shows a button for each move that the item has", async ({ page }) => {
  await page.goto("/items/1");
  await expect(page.getByText("In the inbox.", { exact: true })).toBeVisible();
  await expect(buttonsOf(page)).toHaveText([
    "Add to the queue",
    "Start reading",
    "Mark as read",
    "Archive",
  ]);

  await page.goto("/items/1000");
  await expect(page.getByText("Read.", { exact: true })).toBeVisible();
  await expect(buttonsOf(page)).toHaveText(["Read it again", "Archive"]);
});

test("a move changes the status, the buttons and the history of the item", async ({
  page,
  request,
}) => {
  await page.goto("/items/1");
  await expect(page.getByText("Nothing in the history yet.")).toBeVisible();

  await page.getByRole("button", { name: "Add to the queue" }).click();

  // The page fetches the item and its history again.
  await expect(page.getByRole("article")).toContainText("Status: queued");
  await expect(page.getByText("In the reading queue.", { exact: true })).toBeVisible();
  await expect(buttonsOf(page)).toHaveText([
    "Remove from the queue",
    "Start reading",
    "Mark as read",
    "Archive",
  ]);
  await expect(page.getByRole("list", { name: "History" })).toContainText(
    "Moved from inbox to queued, through the API",
  );
  expect(await moves(request)).toEqual([{ id: 1, to: "queued" }]);
});

test("an item marked as read says when it was read", async ({ page }) => {
  await page.goto("/items/2");

  await page.getByRole("button", { name: "Mark as read" }).click();

  await expect(buttonsOf(page)).toHaveText(["Read it again", "Archive"]);
  await expect(page.locator("p", { hasText: "Read on" }).locator("time")).toHaveCount(1);
});

test("a move into a full queue says why it was refused", async ({ page, request }) => {
  for (const id of [2, 3, 4, 5]) await changeItem(request, id, { status: "queued" });
  await changeItem(request, 1000, { status: "reading" });
  await page.goto("/items/1");

  await page.getByRole("button", { name: "Start reading" }).click();

  await expect(page.getByRole("status").filter({ hasText: "Not moved" })).toHaveText(
    "Not moved: the reading queue is full: all 5 slots are used; move an item out of it first.",
  );
  await expect(page.getByText("In the inbox.", { exact: true })).toBeVisible();
});

test("a click after the item moved in another place shows where it is now", async ({
  page,
  request,
}) => {
  await page.goto("/items/1");
  // In Telegram, or in a second tab, the item is archived before the click.
  await changeItem(request, 1, { status: "archived" });

  await page.getByRole("button", { name: "Mark as read" }).click();

  await expect(page.getByRole("status").filter({ hasText: "Not moved" })).toHaveText(
    "Not moved: cannot move from 'archived' to 'done'.",
  );
  await expect(page.getByText("Archived.", { exact: true })).toBeVisible();
  await expect(buttonsOf(page)).toHaveText(["Move to the inbox"]);
});

test("a merged copy has no buttons, and links to the item that it is a copy of", async ({
  page,
}) => {
  await page.goto("/items/6");

  await expect(page.getByRole("group", { name: "Move the item" })).toHaveCount(0);
  await page.getByRole("link", { name: "item 1" }).click();
  await expect(page).toHaveURL(/\/items\/1$/);
});

test("the queue lists the item being read first, then the items that wait", async ({ page }) => {
  await page.goto("/items/2");
  await page.getByRole("button", { name: "Add to the queue" }).click();
  await expect(page.getByText("In the reading queue.", { exact: true })).toBeVisible();
  await page.goto("/items/1");
  await page.getByRole("button", { name: "Start reading" }).click();
  await expect(page.getByText("You are reading it.", { exact: true })).toBeVisible();

  await page.goto("/queue");

  await expect(page.getByText("2 of 5 queue slots used.")).toBeVisible();
  const cards = page.getByRole("article");
  await expect(cards).toHaveCount(2);
  await expect(cards.nth(0)).toContainText("Leases for Job Queues in Postgres");
  await expect(cards.nth(1)).toContainText("صف کارها در پستگرس");
});

test("the history says when a digest showed the item and what the stale prompt asked", async ({
  page,
  request,
}) => {
  await addEvent(request, 3, {
    type: "surfaced",
    payload: { period: "2026-W43", reason: "focus_goal", goal_id: 1 },
  });
  await addEvent(request, 3, { type: "surfaced", payload: { period: "try-2026-10-19T17:00:00" } });
  await addEvent(request, 3, { type: "stale_asked", payload: { period: "2026-W44" } });
  await addEvent(request, 3, { type: "kept", actor: "user", payload: { via: "telegram" } });

  await page.goto("/items/3");

  // Newest first.
  await expect(page.getByRole("list", { name: "History" }).getByRole("listitem")).toContainText([
    "Kept in the inbox, in Telegram",
    "Asked in Telegram: archive it, or keep it?",
    "Shown in a digest",
    "Shown in your digest of week 43, 2026, for your focus goal",
  ]);
});
