import { expect, test } from "@playwright/test";

// A narrow phone has a column of 328 px (360 px less the gutters). A seventh link would not fit,
// and then this test asks for a decision: a shorter word, or a menu.
test("on a phone of 360 px, the links of the navigation stay on one line", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  await page.goto("/stats");

  const links = page.getByRole("navigation", { name: "Main" }).getByRole("link");
  await expect(links).toHaveText(["Inbox", "Queue", "Digest", "Search", "Goals", "Stats"]);
  const tops = await links.evaluateAll((all) =>
    all.map((link) => Math.round(link.getBoundingClientRect().top)),
  );
  expect(new Set(tops).size, `the tops of the links: ${tops.join(", ")}`).toBe(1);
});
