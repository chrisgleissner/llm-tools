// Regression tests for web review findings of docs/distributed/ux-review-web.md that had no test, or only a partial one:
// WUX-004, 006, 009, 023, 027 and 033. Each asserts the behaviour the fix introduced, so it fails when the fix is reverted.
import { expect, test } from "./fixtures";
import { MISSIONS, mockApp } from "./mock";

const IN = { auth: "in" } as const;

test("WUX-004: the sign-in lockout message is written once; only the button label counts down", async ({ page }) => {
  // The clock must run: with the mock's frozen time the remaining seconds never change and the countdown always sits on a 30 s boundary.
  await mockApp(page, { auth: "login", login: "limited", fakeClock: true });
  await page.goto("/login");
  await page.getByLabel("Username", { exact: true }).fill("admin");
  await page.getByLabel("Password", { exact: true }).fill("wrong password here");
  await page.getByRole("button", { name: "Sign in" }).click();
  // The message is in a live region (`role=status` while the lockout lasts, so it is polite and not assertive).
  const alert = page.locator("#form-error");
  await expect(alert).toContainText(/Too many failed sign-in attempts/);
  await expect(alert).toHaveAttribute("role", "status");
  // Count changes of the live region and of its text for 3 seconds (the finding measured 5 changes in 5.2 s).
  const changes = await alert.evaluate(
    (el) =>
      new Promise<number>((resolve) => {
        let n = 0;
        new MutationObserver((list) => (n += list.length)).observe(el, { childList: true, characterData: true, subtree: true, attributes: true });
        setTimeout(() => resolve(n), 3000);
      }),
  );
  expect(changes, "mutations of the assertive live region during the lockout").toBeLessThanOrEqual(1);
  await expect(page.getByRole("button", { name: /Try again in/ })).toBeDisabled();
});

test("WUX-006: a mission on an offline worker shows the offline notice and no 'No tasks match'", async ({ page }) => {
  await mockApp(page, IN);
  const beta = MISSIONS.find((m) => m.worker === "beta")!.mission;
  await page.goto(`/missions/beta/${beta.id}`);
  await expect(page.getByRole("heading", { level: 1, name: beta.name })).toBeVisible();
  await expect(page.getByText("The worker is offline, so tasks cannot be loaded.")).toBeVisible();
  await expect(page.getByText("No tasks match")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Clear search and filters" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Take control" }), "no control of a terminal that is not connected").toBeDisabled();
});

test("WUX-009: the tab title of a mission page is the mission name, not its id", async ({ page }) => {
  await mockApp(page, IN);
  const m = MISSIONS[0]!.mission;
  await page.goto(`/missions/alpha/${m.id}`);
  await expect(page).toHaveTitle(/Refactor the parser/);
  await expect(page).not.toHaveTitle(new RegExp(m.id.slice(0, 12)));
  await expect(page).toHaveTitle(/LLM Bridge$/);
});

test("WUX-023: Diagnostics shows uptime in units, a worker count, and a health sentence", async ({ page }) => {
  await mockApp(page, IN);
  await page.goto("/diagnostics");
  await expect(page.getByRole("heading", { level: 1, name: "Diagnostics" })).toBeVisible();
  const value = (term: string) => page.locator("dt", { hasText: new RegExp(`^${term}$`) }).first().locator("xpath=following-sibling::dd[1]");
  await expect(value("Up for")).toHaveText(/^\d+ (min|h|d)/);
  await expect(value("Database")).toHaveText(/^\d+(\.\d+)? (KB|MB)$/);
  await expect(value("Memory")).toHaveText(/^\d+(\.\d+)? (KB|MB)$/);
  await expect(page.getByRole("main").locator("p").first(), "a sentence that says whether the coordinator is healthy").toContainText(/has been up .* with \d+ workers? connected/);
  expect(await page.getByRole("main").innerText()).not.toMatch(/\b\d{4,} s\b/);
});

test("WUX-027: Account says when the session ends once, and the cards do not stretch", async ({ page }) => {
  await mockApp(page, IN);
  await page.goto("/account");
  await expect(page.getByRole("heading", { level: 1, name: "Account" })).toBeVisible();
  const text = await page.getByRole("main").innerText();
  expect(text.match(/Signs out/g)?.length, "the sign-out time is stated once").toBe(1);
  expect(text, "the duplicate sentence is gone").not.toMatch(/That is in about/);
  await expect(page.getByRole("region", { name: "API tokens" })).toBeVisible();
  const session = (await page.getByRole("region", { name: "This session" }).boundingBox())!;
  const rows = await page.getByRole("region", { name: "This session" }).locator("dt").count();
  expect(rows, "the session block lists who, device, sign-out time and idle limit").toBe(4);
  expect(session.height, "the session card is as tall as its content, not stretched").toBeLessThan(400);
});

test("WUX-033: in forced-colors mode the current navigation item has a border and status pills have an outline", async ({ page, browserName }) => {
  test.skip(browserName === "webkit", "Playwright's WebKit cannot emulate forced-colors");
  await page.emulateMedia({ forcedColors: "active" });
  await mockApp(page, IN);
  await page.goto("/workers");
  const current = page.getByRole("navigation", { name: "Main" }).locator('a[aria-current="page"]');
  await expect(current).toHaveCount(1);
  const border = await current.evaluate((el) => {
    const s = getComputedStyle(el);
    return { width: parseFloat(s.borderInlineStartWidth), style: s.borderInlineStartStyle };
  });
  expect(border.style).not.toBe("none");
  expect(border.width, "the current item is marked by a 4px border, which survives forced colours").toBeGreaterThanOrEqual(4);
  const other = page.getByRole("navigation", { name: "Main" }).locator("a:not([aria-current])").first();
  expect(await other.evaluate((el) => parseFloat(getComputedStyle(el).borderInlineStartWidth))).toBeLessThan(4);
});
