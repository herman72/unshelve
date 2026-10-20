import { expect, type Page, test } from "@playwright/test";
import {
  changeItem,
  changeSearch,
  failOverview,
  overviews,
  resetApi,
  searches,
} from "./support";

// The pretend API (e2e/fixture-api.mjs) searches its made-up items: words in the titles, the
// summaries and a few saved texts, and a short list of words for each item in place of its
// meaning. "item" finds item 5 by its summary, then item 1000 by its title; 1000 is the only
// item that was read. "postgres" finds item 1, then item 2, which is Persian.

test.beforeEach(async ({ request }) => {
  await resetApi(request);
});

const results = (page: Page) => page.getByRole("region", { name: "Results" });
const hits = (page: Page) => results(page).getByRole("article");
const hitTitles = (page: Page) => results(page).getByRole("heading", { level: 3 });
const overview = (page: Page) => page.getByRole("region", { name: "Overview" });

test("the page asks for words before it searches", async ({ page, request }) => {
  await page.goto("/search");

  await expect(page.getByText("Type a few words to search the items you saved.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Search" })).toHaveAttribute("aria-current", "page");
  expect(await searches(request)).toEqual([]);
});

test("each hit says why search found it, and whether you read it", async ({ page }) => {
  await page.goto("/search?q=item");

  await expect(hitTitles(page)).toHaveText(["A note with a strange address", "Item one thousand"]);
  const [note, thousand] = [hits(page).nth(0), hits(page).nth(1)];

  // Found by a word of its summary: the word is marked, and the rest is plain text.
  await expect(note.getByRole("link", { name: "A note with a strange address" })).toHaveAttribute(
    "href",
    "/items/5",
  );
  await expect(note).toContainText("Unread");
  await expect(note.locator("figcaption")).toHaveText("In the summary");
  await expect(note.locator("blockquote")).toHaveText(
    "The address of this item is not a web address.",
  );
  await expect(note.locator("mark")).toHaveText(["item"]);
  await expect(note).toContainText("Found by words (1st) and meaning (1st).");

  // Found by its title, so it has no snippet. It was read, and the mark says when.
  await expect(thousand).toContainText("Read on 18 Oct 2026");
  await expect(thousand).not.toContainText("Unread");
  await expect(thousand.locator("time")).toHaveAttribute("datetime", "2026-10-18T19:00:00Z");
  await expect(thousand.locator("blockquote")).toHaveCount(0);
  await expect(thousand).toContainText("Found by words (2nd) and spelling (1st).");
});

test("a hit of the title shows the summary of the item, with no snippet", async ({ page }) => {
  await page.goto("/search?q=strange");

  await expect(hitTitles(page)).toHaveText(["A note with a strange address"]);
  await expect(hits(page).locator("figure")).toHaveCount(0);
  const summary = hits(page).getByText("The address of this item is not a web address.");
  await expect(summary).toBeVisible();
  await expect(hits(page)).toContainText("Found by words (1st) and spelling (1st).");
});

test("a Persian snippet runs right to left, with each hit marked", async ({ page }) => {
  await page.goto(`/search?q=${encodeURIComponent("اجاره")}`);

  await expect(hits(page)).toHaveCount(1);
  await expect(hits(page).locator("figcaption")).toHaveText("In the saved text");
  await expect(hits(page).locator("mark")).toHaveText(["اجاره", "اجاره"]);
  const quote = hits(page).locator("blockquote");
  expect(await quote.evaluate((element) => element.matches(":dir(rtl)"))).toBe(true);
});

test("an item found by its meaning only shows its closest passage", async ({ page }) => {
  await page.goto("/search?q=contention");

  await expect(hitTitles(page)).toHaveText([
    "Leases for Job Queues in Postgres",
    "صف کارها در پستگرس",
  ]);
  const first = hits(page).first();
  await expect(first.locator("figcaption")).toHaveText("The closest passage in meaning");
  await expect(first.locator("blockquote")).toHaveText(
    "Each worker claims a job with SKIP LOCKED and holds a lease on it.",
  );
  // The match is the meaning: no word of the query is in the passage, so nothing is marked.
  await expect(results(page).locator("mark")).toHaveCount(0);
  await expect(first).toContainText("Found by meaning (1st).");
});

test("saved text that looks like HTML stays text", async ({ page }) => {
  let dialogs = 0;
  page.on("dialog", async (dialog) => {
    dialogs += 1;
    await dialog.dismiss();
  });
  await page.goto("/search?q=markup");

  await expect(hits(page).locator("blockquote")).toHaveText(
    "Saved from a page with markup: <img src=x onerror=alert(1)> and <b>bold</b> stay text.",
  );
  await expect(hits(page).locator("mark")).toHaveText(["markup"]);
  await expect(hits(page).locator("img, b")).toHaveCount(0);
  expect(dialogs).toBe(0);
});

test("the filters go to the API, and the form keeps them", async ({ page, request }) => {
  await page.goto("/search?q=postgres");
  await expect(hitTitles(page)).toHaveCount(2);
  const form = page.getByRole("search");
  // The archived goals are choices too, in a group of their own.
  await expect(form.locator("optgroup[label='Archived goals'] option")).toHaveText(["Run a 10k"]);

  await form.getByRole("checkbox", { name: "Inbox" }).check();
  await form.getByRole("combobox", { name: "Kind" }).selectOption({ label: "PDF" });
  await form.getByRole("combobox", { name: "Goal" }).selectOption({ label: "Database internals" });
  await form.getByRole("button", { name: "Search" }).click();

  await expect(page).toHaveURL(/\/search\?q=postgres&status=inbox&kind=pdf&goal_id=1$/);
  await expect(hitTitles(page)).toHaveText(["Leases for Job Queues in Postgres"]);
  expect((await searches(request)).at(-1)).toEqual({
    q: "postgres",
    status: ["inbox"],
    kind: ["pdf"],
    goal_id: "1",
    cursor: null,
  });
  // After the navigation, the form shows the search that the page made.
  await expect(form.getByRole("checkbox", { name: "Inbox" })).toBeChecked();
  await expect(form.getByRole("checkbox", { name: "Done" })).not.toBeChecked();
  await expect(form.getByRole("combobox", { name: "Kind" })).toHaveValue("pdf");
  await expect(form.getByRole("combobox", { name: "Goal" })).toHaveValue("1");
  // The overview cannot filter by kind or goal, and it says so.
  await expect(overview(page)).toContainText(
    "It reads items of every kind and goal: of your filters, only the status applies.",
  );

  // Back shows the earlier search, and one form that shows it too.
  await page.goBack();
  await expect(page).toHaveURL(/\/search\?q=postgres$/);
  await expect(hitTitles(page)).toHaveCount(2);
  await expect(page.getByRole("search")).toHaveCount(1);
  await expect(form.getByRole("checkbox", { name: "Inbox" })).not.toBeChecked();
  await expect(form.getByRole("combobox", { name: "Kind" })).toHaveValue("");
});

test("a search without hits says so, and the form shows each filter it used", async ({
  page,
  request,
}) => {
  // Goal 99 does not exist: the API finds nothing for it.
  await page.goto("/search?q=postgres&status=done&goal_id=99");

  await expect(
    page.getByText("Nothing you saved matches that search. Try other words, or fewer filters."),
  ).toBeVisible();
  await expect(overview(page)).toHaveCount(0);
  const goal = page.getByRole("search").getByRole("combobox", { name: "Goal" });
  await expect(goal).toHaveValue("99");
  await expect(goal.locator("option:checked")).toHaveText("Goal 99, not found");
  expect((await searches(request)).at(-1)).toMatchObject({ status: ["done"], goal_id: "99" });
});

test("the page says when search compared words only", async ({ page, request }) => {
  await changeSearch(request, { vector_search: "failed" });
  await page.goto("/search?q=postgres");
  await expect(page.getByRole("note")).toHaveText(
    "Found by words only this time: meanings were not compared, because the embedder did not " +
      "answer in time.",
  );
  // The words still find both items.
  await expect(hitTitles(page)).toHaveCount(2);

  await changeSearch(request, { vector_search: "no_vectors" });
  await page.goto("/search?q=postgres&status=inbox");
  await expect(page.getByRole("note")).toHaveText(
    "Found by words only: meanings were not compared, because your items have no vectors of " +
      "this embedder yet.",
  );

  await changeSearch(request, { vector_search: "used" });
  await page.goto("/search?q=lease");
  await expect(hitTitles(page)).toHaveCount(1);
  await expect(page.getByRole("note")).toHaveCount(0);
});

test("more results open the next page of the same search", async ({ page, request }) => {
  await changeSearch(request, { page_size: 1 });
  await page.goto("/search?q=postgres&status=inbox");
  await expect(hitTitles(page)).toHaveText(["Leases for Job Queues in Postgres"]);

  await page.getByRole("link", { name: "More results" }).click();

  // The cursor goes with the same words and filters: the API refuses it for another search.
  await expect(page).toHaveURL(/\/search\?q=postgres&status=inbox&cursor=p1$/);
  await expect(results(page).getByRole("heading", { level: 2 })).toHaveText("More results");
  await expect(hitTitles(page)).toHaveText(["صف کارها در پستگرس"]);
  await expect(page.getByRole("link", { name: "More results" })).toHaveCount(0);
  expect((await searches(request)).at(-1)).toEqual({
    q: "postgres",
    status: ["inbox"],
    kind: [],
    goal_id: null,
    cursor: "p1",
  });

  await page.getByRole("link", { name: "Back to the best results" }).click();
  await expect(page).toHaveURL(/\/search\?q=postgres&status=inbox$/);
  await expect(hitTitles(page)).toHaveText(["Leases for Job Queues in Postgres"]);
});

test("the overview cites its sources, and each number leads to the quote", async ({
  page,
  request,
}) => {
  await page.goto("/search?q=item");
  const panel = overview(page);
  // Nothing is asked of the model before the click: an overview costs money.
  expect(await overviews(request)).toEqual([]);

  await panel.getByRole("button", { name: "Write an overview" }).click();

  // Item 5 is cited. Its saved text has markup in it, and the answer shows it as characters.
  await expect(panel).toContainText(
    "From your saved items: Saved from a page with markup: <img src=x onerror=alert(1)> and " +
      "<b>bold</b> stay text.",
  );
  await expect(panel.locator("img, b")).toHaveCount(0);
  await expect(panel.getByRole("link", { name: /^Source/ })).toHaveText(["[1]"]);
  await expect(panel.getByRole("link", { name: "Source 1" })).toHaveAttribute("href", "#source-1");
  // The focus moves to the answer, where the button was.
  await expect(panel.locator("[tabindex='-1']")).toBeFocused();
  expect(await overviews(request)).toEqual([{ q: "item", status: null, limit: 12 }]);

  // The model read both items, the best first. Item 1000 has no text: read, but not cited.
  const sources = panel.getByRole("list").getByRole("listitem");
  await expect(sources).toHaveCount(2);
  await expect(sources.nth(0).getByRole("link")).toHaveText("A note with a strange address");
  await expect(sources.nth(0).getByRole("link")).toHaveAttribute("href", "/items/5");
  await expect(sources.nth(0).locator("blockquote")).toHaveText(
    "Saved from a page with markup: <img src=x onerror=alert(1)> and <b>bold</b> stay text.",
  );
  await expect(sources.nth(1)).toHaveText("[2] Item one thousand");

  await panel.getByRole("link", { name: "Source 1" }).click();
  await expect(page).toHaveURL(/#source-1$/);
  await expect(sources.nth(0)).toBeInViewport();

  // A new search starts the overview again from its button.
  await page.getByRole("searchbox").fill("postgres");
  await page.getByRole("searchbox").press("Enter");
  await expect(hitTitles(page)).toHaveCount(2);
  await expect(panel.getByRole("button", { name: "Write an overview" })).toBeVisible();
  await expect(panel).not.toContainText("From your saved items");
});

test("the next page of the same search keeps the overview", async ({ page, request }) => {
  await changeSearch(request, { page_size: 1 });
  await page.goto("/search?q=postgres");
  await overview(page).getByRole("button", { name: "Write an overview" }).click();
  await expect(overview(page)).toContainText("From your saved items:");

  await page.getByRole("link", { name: "More results" }).click();

  await expect(hitTitles(page)).toHaveText(["صف کارها در پستگرس"]);
  // The key of the results is the search without its cursor: the panel stays, with its answer.
  await expect(overview(page)).toContainText("From your saved items:");
  expect(await overviews(request)).toHaveLength(1);
});

test("each number stands after its statement, also after a Persian one", async ({ page }) => {
  await page.goto("/search?q=postgres");
  await overview(page).getByRole("button", { name: "Write an overview" }).click();
  await expect(overview(page).getByRole("link", { name: /^Source/ })).toHaveText(["[1]", "[2]"]);

  // The answer: an English statement [1], then a Persian statement [2], on one line.
  const [statement, number] = await overview(page).evaluate((panel) => {
    const answer = panel.querySelector("p[dir=auto]");
    const walker = document.createTreeWalker(answer ?? panel, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node.textContent?.includes("اجاره")) range.selectNodeContents(node);
    }
    const marker = panel.querySelector("a[aria-label='Source 2']");
    const box = (rect: DOMRect) => ({ left: rect.left, right: rect.right, top: rect.top });
    return [box(range.getBoundingClientRect()), box(marker!.getBoundingClientRect())];
  });
  // In this left-to-right paragraph, [2] comes after the Persian statement: on its right.
  expect(Math.abs(number.top - statement.top)).toBeLessThan(20);
  expect(number.left).toBeGreaterThanOrEqual(statement.right - 1);
});

test("the overview says why the model gave no answer, and the results stay", async ({
  page,
  request,
}) => {
  await failOverview(request, "the model gave no answer: the daily budget of $1.00 is used");
  await page.goto("/search?q=postgres");
  const panel = overview(page);

  await panel.getByRole("button", { name: "Write an overview" }).click();

  await expect(panel.getByRole("status")).toHaveText(
    "The model gave no answer: the daily budget of $1.00 is used. " +
      "The results below do not need it.",
  );
  await expect(panel.getByRole("button", { name: "Try again" })).toBeEnabled();
  await expect(hitTitles(page)).toHaveCount(2);
});

test("an overview with nothing to read says so", async ({ page, request }) => {
  await page.goto("/search?q=postgres&status=inbox");
  await expect(hitTitles(page)).toHaveCount(2);
  // In Telegram, or in another tab, both items are archived before the click.
  await changeItem(request, 1, { status: "archived" });
  await changeItem(request, 2, { status: "archived" });

  await overview(page).getByRole("button", { name: "Write an overview" }).click();

  await expect(overview(page)).toContainText(
    "None of your saved items could be read for this question.",
  );
  expect(await overviews(request)).toEqual([{ q: "postgres", status: ["inbox"], limit: 12 }]);
});
