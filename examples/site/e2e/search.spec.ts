import { expect, type Page, test } from "@playwright/test";

// The page searches the fixture feed of packages/site-search: six public items, the newest
// first, that build_feed() of the server wrote.

function results(page: Page) {
  return page.locator(".unshelve-search-result");
}

function searchField(page: Page) {
  return page.getByRole("searchbox", { name: "Search" });
}

let errors: string[] = [];

test.beforeEach(async ({ page }) => {
  errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`);
  });
  await page.goto("/");
});

test.afterEach(() => {
  expect(errors, "errors in the browser console, and failed requests").toEqual([]);
});

test("the page shows each item of the feed, the newest first, with its facts", async ({ page }) => {
  await expect(page.getByRole("status")).toHaveText("6 of 6 items");
  await expect(results(page)).toHaveCount(6);

  const meetup = results(page).first();
  await expect(meetup.getByRole("link")).toHaveText("İstanbul Postgres Meetup: Queues at Scale");
  await expect(meetup.getByRole("link")).toHaveAttribute(
    "href",
    "https://video.example.com/istanbul-meetup",
  );
  await expect(meetup.locator(".unshelve-search-facts")).toHaveText(
    "video · video.example.com · 22 October 2026 · 38 min",
  );
  await expect(meetup.locator(".unshelve-search-tags li")).toHaveText(["databases", "talks"]);

  // The note has no title and no link: its text is the item.
  const note = results(page).last();
  await expect(note.getByRole("heading")).toHaveCount(0);
  await expect(note.locator(".unshelve-search-note")).toHaveText(
    "SKIP LOCKED needs an index on the columns of the queue.",
  );
});

test("a query typed with the Arabic kaf finds the title with the Persian kaf", async ({ page }) => {
  await searchField(page).fill("صف كارها");

  await expect(page.getByRole("status")).toHaveText("1 of 6 items");
  await expect(results(page).getByRole("link")).toHaveText(["صف کارها در پستگرس"]);
});

test("a query with a space finds two words joined by a zero-width non-joiner", async ({ page }) => {
  await searchField(page).fill("ایندکس ها");

  await expect(results(page).getByRole("link")).toHaveText(["۱۰ نکته درباره‌ی ایندکس‌ها"]);
});

test("Arabic-Indic digits find the Persian digits of a title", async ({ page }) => {
  await searchField(page).fill("١٠");

  await expect(results(page).getByRole("link")).toHaveText(["۱۰ نکته درباره‌ی ایندکس‌ها"]);
});

test("the list follows each key press, and an empty field shows each item again", async ({
  page,
}) => {
  await searchField(page).pressSequentially("lease");
  await expect(page.getByRole("status")).toHaveText("1 of 6 items");
  await expect(results(page).getByRole("link")).toHaveText(["Leases for Job Queues in Postgres"]);

  await searchField(page).fill("");
  await expect(page.getByRole("status")).toHaveText("6 of 6 items");
});

test("a Persian item reads from right to left, an English one from left to right", async ({
  page,
}) => {
  const persian = results(page).nth(1);
  await expect(persian).toHaveAttribute("lang", "fa");
  expect(await persian.evaluate((element) => getComputedStyle(element).direction)).toBe("rtl");
  expect(
    await results(page)
      .first()
      .evaluate((element) => getComputedStyle(element).direction),
  ).toBe("ltr");
});

test("a word that no item has shows no item", async ({ page }) => {
  await searchField(page).fill("kubernetes");

  await expect(page.getByRole("status")).toHaveText("0 of 6 items");
  await expect(results(page)).toHaveCount(0);
});

test("only a web address becomes a link", async ({ page }) => {
  await page.goto("/unsafe-feed");

  await expect(results(page).getByRole("heading")).toHaveText([
    "An address with a script",
    "An address with data",
    "An address on this site",
    "A web address",
  ]);
  await expect(results(page).getByRole("link")).toHaveText(["A web address"]);
});
