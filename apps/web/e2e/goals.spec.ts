import { expect, type Page, test } from "@playwright/test";
import { API_URL, changeGoal, resetApi } from "./support";

// The made-up goals of e2e/fixture-api.mjs: 1 "Database internals" is the focus, 2 "Learn
// Finnish" is active, and 3 "Run a 10k" is archived. The pretend API has the rules of the real
// one: a clash is a 409, at most 10 active goals, a new version for a new meaning.

test.beforeEach(async ({ request }) => {
  await resetApi(request);
});

const active = (page: Page) => page.getByRole("list", { name: "Active goals" });
const archived = (page: Page) => page.getByRole("list", { name: "Archived goals" });
const card = (page: Page, title: string) => page.getByRole("article").filter({ hasText: title });
const addForm = (page: Page) => page.getByRole("form", { name: "Add a goal" });

test("the page shows the active goals with the focus, and the archived goals apart", async ({
  page,
}) => {
  await page.goto("/goals");

  await expect(active(page).getByRole("article")).toHaveCount(2);
  await expect(card(page, "Database internals")).toContainText("Focus");
  await expect(card(page, "Database internals")).toContainText(
    "2026-11-01 to 2027-01-31 · Version 1",
  );
  await expect(archived(page).getByRole("article")).toHaveCount(1);
  await expect(archived(page)).toContainText("Run a 10k");
});

test("a new goal comes into the list, and the form is empty again", async ({ page }) => {
  await page.goto("/goals");
  const form = addForm(page);

  await form.getByLabel("Title").fill("Sleep eight hours");
  await form.getByLabel("What it means for you").fill("In bed before eleven.");
  await form.getByLabel("Keywords, with a comma between them").fill("sleep, health");
  await form.getByRole("button", { name: "Add the goal" }).click();

  const added = card(page, "Sleep eight hours");
  await expect(added).toContainText("In bed before eleven.");
  await expect(added.getByRole("list", { name: "Keywords" }).getByRole("listitem")).toHaveText([
    "sleep",
    "health",
  ]);
  await expect(form.getByLabel("Title")).toHaveValue("");
});

test("a goal with a title that exists is refused, and the typed text stays", async ({ page }) => {
  await page.goto("/goals");
  const form = addForm(page);

  await form.getByLabel("Title").fill("Learn Finnish");
  await form.getByRole("button", { name: "Add the goal" }).click();

  await expect(form.getByRole("status")).toHaveText(
    "Not saved: a goal with this title exists already: 'Learn Finnish'.",
  );
  await expect(form.getByLabel("Title")).toHaveValue("Learn Finnish");
});

test("a new description gives the goal a new version", async ({ page }) => {
  await page.goto("/goals");
  const goal = card(page, "Learn Finnish");

  await goal.getByRole("button", { name: "Edit" }).click();
  await goal.getByLabel("What it means for you").fill("Read the news in Finnish.");
  await goal.getByRole("button", { name: "Save the goal" }).click();

  await expect(goal).toContainText("Read the news in Finnish.");
  await expect(goal).toContainText("Version 2");
});

test("the focus moves to another goal", async ({ page }) => {
  await page.goto("/goals");

  await card(page, "Learn Finnish").getByRole("button", { name: "Make it the focus" }).click();

  await expect(card(page, "Learn Finnish")).toContainText("Focus");
  await expect(card(page, "Database internals")).not.toContainText("Focus");
  await expect(
    card(page, "Database internals").getByRole("button", { name: "Make it the focus" }),
  ).toBeVisible();
});

test("an archived goal goes to the archived goals, and a restored goal comes back", async ({
  page,
}) => {
  await page.goto("/goals");

  await card(page, "Database internals").getByRole("button", { name: "Archive" }).click();
  await expect(archived(page)).toContainText("Database internals");
  // An archived goal is not the focus: nobody works on it.
  await expect(card(page, "Database internals")).not.toContainText("Focus");

  await card(page, "Run a 10k").getByRole("button", { name: "Restore" }).click();
  await expect(active(page)).toContainText("Run a 10k");
});

test("an eleventh active goal is refused", async ({ page, request }) => {
  for (let n = 1; n <= 8; n++) {
    await request.post(`${API_URL}/api/v1/goals`, { data: { title: `Goal number ${n}` } });
  }
  await page.goto("/goals");
  await expect(active(page).getByRole("article")).toHaveCount(10);

  await addForm(page).getByLabel("Title").fill("One more");
  await addForm(page).getByRole("button", { name: "Add the goal" }).click();

  await expect(addForm(page).getByRole("status")).toHaveText(
    "Not saved: you have 10 active goals, the most; archive one first.",
  );
});

test("a click after the goal was archived in another tab shows where it is now", async ({
  page,
  request,
}) => {
  await page.goto("/goals");
  await changeGoal(request, 2, { archived_at: "2026-10-16T12:00:00Z" });

  await card(page, "Learn Finnish").getByRole("button", { name: "Make it the focus" }).click();

  // The API answers 409, and the page fetches the goals again: the goal is archived now.
  await expect(archived(page)).toContainText("Learn Finnish");
  await expect(active(page)).not.toContainText("Learn Finnish");
  await expect(card(page, "Learn Finnish").getByRole("button", { name: "Restore" })).toBeVisible();
});
