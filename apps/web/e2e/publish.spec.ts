import { expect, test } from "@playwright/test";
import { changeItem, resetApi, visibilities } from "./support";

// The made-up items of e2e/fixture-api.mjs: 1 has a private note and a private tag ("queues"),
// 2 is Persian, 6 is a copy of 1. The rules of a publication are tested in the API
// (tests/api/test_visibility.py); these tests check what the page shows and sends.

test.beforeEach(async ({ request }) => {
  await resetApi(request);
});

test("a preview shows the public record, and Publish makes exactly that record public", async ({
  page,
  request,
}) => {
  await page.goto("/items/1");

  await page.getByRole("button", { name: "Preview the public record" }).click();

  const record = page.getByRole("region", { name: "The public record" });
  await expect(record).toContainText("leases-for-job-queues-in-postgres");
  await expect(record).toContainText("A job queue on one Postgres table, made safe with leases.");
  // The private note and the private tag stay out: the record is all that the site gets.
  await expect(record).not.toContainText("Read before the design review.");
  // The tags of the record: the private tag "queues" is not one of them.
  await expect(record.getByRole("definition").nth(6)).toHaveText("—");

  // The record as JSON, as the page shows it.
  const shown: unknown = JSON.parse((await record.locator("pre").textContent()) ?? "");

  await record.getByRole("button", { name: "Publish" }).click();

  await expect(page.getByRole("status").filter({ hasText: "Published." })).toBeVisible();
  await expect(page.getByText("Public as leases-for-job-queues-in-postgres since")).toBeVisible();
  // The page sent the record that it showed, field for field.
  expect(await visibilities(request)).toEqual([{ id: 1, visibility: "public", record: shown }]);
});

test("a record that changed after the preview is not published, and the record of now is shown", async ({
  page,
  request,
}) => {
  await page.goto("/items/1");
  await page.getByRole("button", { name: "Preview the public record" }).click();
  const record = page.getByRole("region", { name: "The public record" });
  await expect(record).toContainText("made safe with leases");
  // The worker writes a new summary while the preview is open.
  await changeItem(request, 1, { summary: "A new summary of the leases." });

  await record.getByRole("button", { name: "Publish" }).click();

  const status = page.getByRole("status").filter({ hasText: "changed after your preview" });
  await expect(status).toHaveText(
    "The public record changed after your preview. This is the record now: look at it, then " +
      "publish again.",
  );
  await expect(record).toContainText("A new summary of the leases.");
  await expect(page.getByText("Public as")).toHaveCount(0);

  await record.getByRole("button", { name: "Publish" }).click();

  await expect(page.getByText("Public as leases-for-job-queues-in-postgres since")).toBeVisible();
  expect((await visibilities(request)).map((call) => call.visibility)).toEqual([
    "public",
    "public",
  ]);
});

test("a public item can be made private again, and it keeps its name and its time", async ({
  page,
  request,
}) => {
  await changeItem(request, 1, {
    visibility: "public",
    public_slug: "leases-for-job-queues-in-postgres",
    published_at: "2026-10-20T08:00:00Z",
  });
  await page.goto("/items/1");
  await expect(
    page.getByText("Public as leases-for-job-queues-in-postgres since 20 October 2026 at 08:00 UTC"),
  ).toBeVisible();

  await page.getByRole("button", { name: "Make private" }).click();

  await expect(page.getByRole("status").filter({ hasText: "Private again." })).toBeVisible();
  await page.getByRole("button", { name: "Preview the public record" }).click();
  const record = page.getByRole("region", { name: "The public record" });
  await expect(record).toContainText("leases-for-job-queues-in-postgres");
  await expect(record).toContainText("20 October 2026 at 08:00 UTC");
  expect(await visibilities(request)).toEqual([{ id: 1, visibility: "private", record: null }]);
});

test("Cancel closes the preview and sends nothing", async ({ page, request }) => {
  await page.goto("/items/1");
  await page.getByRole("button", { name: "Preview the public record" }).click();

  await page.getByRole("button", { name: "Cancel" }).click();

  await expect(page.getByRole("region", { name: "The public record" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Preview the public record" })).toBeVisible();
  expect(await visibilities(request)).toEqual([]);
});

test("a copy is not published, and the page names the item to publish", async ({ page }) => {
  await page.goto("/items/6");

  const section = page.getByRole("heading", { name: "Public" }).locator("..");
  await expect(section).toContainText("A copy is not published. Publish item 1 instead.");
  // One link to item 1 on the page: the one of the section "Reading".
  await expect(page.getByRole("link", { name: "item 1" })).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Preview the public record" })).toHaveCount(0);
});

test("the public name of a Persian item keeps its letters and runs right to left", async ({
  page,
}) => {
  await page.goto("/items/2");

  await page.getByRole("button", { name: "Preview the public record" }).click();

  const name = page
    .getByRole("region", { name: "The public record" })
    .getByText("صف-کارها-در-پستگرس", { exact: true });
  await expect(name).toHaveAttribute("dir", "auto");
  expect(await name.evaluate((element) => element.matches(":dir(rtl)"))).toBe(true);
});
