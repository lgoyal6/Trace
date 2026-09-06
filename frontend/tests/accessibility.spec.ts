import { expect, test, type Page } from "@playwright/test";

/* The states a keyboard or screen-reader user meets on the primary path.
 *
 * These are behavioural, not markup greps: the accessible name is read out of
 * the accessibility tree, the focus ring is read out of the computed style
 * after a real Tab, and the error is asserted through the alert role rather
 * than through its text position on screen. Every one of them failed on the
 * build before this suite existed; the record in
 * project-strengthening_materials/RECORD_frontend.md has the before/after.
 *
 * axe-core covers none of this: it accepts a placeholder as a name, has no
 * rule about a suppressed focus ring, and cannot see whether a message is
 * announced. */

const api = "http://localhost:8000";
const query = "asymmetric parietal hypometabolism on FDG-PET";

// Next injects its own `role="alert"` route announcer on every page, so an
// unfiltered alert locator matches two elements and fails strict mode.
const alertSaying = (page: Page, text: string | RegExp) =>
  page.getByRole("alert").filter({ hasText: text });

// One worker, like experience.spec.ts: the home page mounts two canvases and
// several fetches, and five of these in parallel time out on a loaded machine.
test.describe.configure({ mode: "serial" });

async function failTheQuery(page: Page) {
  await page.route(`${api}/query/stream`, (route) => route.abort("connectionrefused"));
}

test("the query box carries a name, not just a placeholder", async ({ page }) => {
  await page.goto("/");
  // A placeholder is an example and it disappears on the first keystroke.
  const box = page.getByRole("textbox", { name: "Describe the finding" });
  await expect(box).toBeVisible();
  await box.fill(query);
  await expect(page.getByRole("textbox", { name: "Describe the finding" })).toHaveValue(query);
});

test("the query box shows a focus ring when it is reached by keyboard", async ({ page }) => {
  await page.goto("/");
  const box = page.getByRole("textbox", { name: "Describe the finding" });
  await box.click();
  // A click is not keyboard focus. Shift+Tab away, then Tab back, so
  // :focus-visible is genuinely in play.
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  const ring = await box.evaluate((element) => {
    const style = getComputedStyle(element);
    return { style: style.outlineStyle, width: style.outlineWidth, focused: document.activeElement === element };
  });
  expect(ring.focused).toBe(true);
  expect(ring.style).not.toBe("none");
  expect(parseFloat(ring.width)).toBeGreaterThanOrEqual(2);
});

test("a search in flight is announced, not only drawn", async ({ page }) => {
  await page.route(`${api}/query/stream`, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 4000));
    await route.continue();
  });
  await page.goto("/");
  await page.getByRole("textbox", { name: "Describe the finding" }).fill(query);
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByRole("status")).toContainText(/Searching|Step \d of \d/, { timeout: 10_000 });
  await expect(page.locator('form[aria-busy="true"]')).toHaveCount(1);
});

test("a failed search is announced and attached to the field that failed", async ({ page }) => {
  await failTheQuery(page);
  await page.goto("/");
  await page.getByRole("textbox", { name: "Describe the finding" }).fill(query);
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(alertSaying(page, "Retrieval degraded")).toBeVisible({ timeout: 15_000 });
  const describedBy = await page
    .getByRole("textbox", { name: "Describe the finding" })
    .getAttribute("aria-describedby");
  expect(describedBy).toContain("query-error");
});

test("a finished search moves the caret to the answer", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("textbox", { name: "Describe the finding" }).fill(query);
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Every sentence carries the paper it came from." }),
  ).toBeVisible({ timeout: 30_000 });
  // Without this the keyboard user is still parked on Search while the page
  // has scrolled somewhere they cannot see.
  const landed = await page.evaluate(() =>
    document.activeElement?.textContent?.includes("Sourced summary") ?? false);
  expect(landed).toBe(true);
});

test("the cost page has a level-one heading and a named question field", async ({ page }) => {
  await page.goto("/cost");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Trace");
  await expect(page.getByRole("textbox", { name: "Ask the ledger a question" })).toBeVisible();
  // The window switcher said which window was chosen with a background colour
  // and nothing else.
  await expect(page.getByRole("button", { name: "24 h" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("button", { name: "1 h" })).toHaveAttribute("aria-pressed", "false");
});

test("a cost page that cannot reach the ledger stops claiming it is loading", async ({ page }) => {
  await page.route(`${api}/economics/summary**`, (route) => route.abort("connectionrefused"));
  await page.goto("/cost");
  await expect(alertSaying(page, "Ledger summary is unavailable.")).toBeVisible({ timeout: 15_000 });
  // It used to show the error banner and "Loading ledger." at the same time,
  // for good, with a stat row of dashes that read as zeroes.
  await expect(page.getByText("Loading ledger.")).toHaveCount(0);
  await expect(page.getByText(/unknown rather than zero/)).toBeVisible();
});
