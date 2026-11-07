// T22 to T24 (UXR-014 to UXR-017): capacity, Missions, Sessions, Workers.
import { evidence } from "./evidence";
import { expect, uxr } from "./gate";
import { isPhone, openApp, viewport } from "./harness";

const FOUNDATIONS = ["WP-0", "WP-1"] as const;

// ---- T22: capacity chips and Usage -------------------------------------------------------------------------------
// Mock usage: Codex 5h 75% left (weekly 90%), Claude 5h 4% left, so Claude is the limiting provider.
uxr({ t: "T22", uxr: ["UXR-014"], needs: [...FOUNDATIONS, "WP-4"] }, "the header shows provider, scope and percent with 0 interactions; the reset time is in the title", async ({ page }, info) => {
  const app = await openApp(page, { auth: "in" }, "/");
  const header = page.locator("header").first();
  await expect(header).toContainText(/Claude\s+5h\s+4%/);
  const vp = viewport(page);
  if (vp.width >= 480) await expect(header).toContainText(/Codex\s+5h\s+75%/);
  expect(await header.locator('[title*="reset" i]').count(), "a chip carries the reset time in its title").toBeGreaterThan(0);
  expect(await app.ix.count(), "interactions needed to read capacity").toBe(0);
  await evidence(page, info, ["UXR-014"], "capacity-chips");
});

uxr({ t: "T22", uxr: ["UXR-014"], needs: [...FOUNDATIONS, "WP-4"] }, "below 30rem only the limiting provider shows; above it both do", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 640 });
  await openApp(page, { auth: "in" }, "/");
  const header = page.locator("header").first();
  await expect(header).toContainText(/Claude/);
  await expect(header, "the non-limiting provider is hidden on a narrow header").not.toContainText(/Codex/);
});

uxr({ t: "T22", uxr: ["UXR-014"], needs: [...FOUNDATIONS, "WP-4", "WP-5"] }, "one click on the chip opens Usage, where every meter has a text value and a reset time", async ({ page }) => {
  const app = await openApp(page, { auth: "in" }, "/");
  await page.locator("header").first().getByText(/Claude/).first().click();
  await expect(page).toHaveURL(/\/usage/);
  expect(await app.ix.count(), "interactions: the chip").toBe(1);
  const meters = page.locator('[role="meter"]');
  await expect(meters.first()).toBeVisible();
  const problems = await meters.evaluateAll((els) => els.map((m) => {
    const card = m.closest("[data-entity]") as HTMLElement | null;
    const text = (card ?? (m.parentElement as HTMLElement)).innerText;
    return { ok: m.hasAttribute("aria-valuenow") && /\d+%/.test(text) && /reset/i.test(text), text: text.slice(0, 80) };
  }).filter((r) => !r.ok).map((r) => r.text));
  expect(problems, "meters without a text value, aria-valuenow or reset time").toEqual([]);
});

// ---- T23: Missions -----------------------------------------------------------------------------------------------
const RANK = ["needs-approval", "needs-input", "failed", "offline", "waiting", "running", "paused", "queued"];

uxr({ t: "T23", uxr: ["UXR-015"], needs: [...FOUNDATIONS, "WP-4"] }, "needs-you and active rows precede the rest; finished missions are collapsed with a count", async ({ page }, info) => {
  await openApp(page, { auth: "in", dataset: "dense" }, "/missions");
  const rows = page.locator('[data-entity^="mission:"]:visible');
  await expect(rows.first()).toBeVisible();
  const states = await rows.evaluateAll((els) => els.map((e) => e.querySelector(".pill")?.getAttribute("data-state") ?? ""));
  const ranks = states.map((s) => RANK.indexOf(s));
  expect(ranks.filter((r) => r < 0), "visible rows in a finished state (Done, Stopped) before the finished group is opened").toEqual([]);
  expect(ranks, "rows are ordered by state rank").toEqual([...ranks].sort((a, b) => a - b));
  const finished = page.locator('details[data-section="finished"]');
  await expect(finished).toHaveCount(1);
  expect(await finished.evaluate((d) => (d as HTMLDetailsElement).open), "the finished group is collapsed").toBe(false);
  await expect(finished.locator("summary")).toContainText("10"); // dense data: 5 completed and 5 stopped
  await evidence(page, info, ["UXR-015"], "missions-list");
});

uxr({ t: "T23", uxr: ["UXR-015"], needs: [...FOUNDATIONS, "WP-4"] }, "rows are compact, there is no id column and no table", async ({ page }) => {
  await openApp(page, { auth: "in", dataset: "dense" }, "/missions");
  const rows = page.locator('[data-entity^="mission:"]:visible');
  await expect(rows.first()).toBeVisible();
  const heights = await rows.evaluateAll((els) => els.map((e) => e.getBoundingClientRect().height));
  const max = isPhone(page) ? 72 : 56;
  expect(heights.filter((h) => h > max + 0.5), `row heights above ${max}px`).toEqual([]);
  expect(await page.locator("table").count(), "tables on the Missions page").toBe(0);
  expect(await page.getByRole("columnheader", { name: /^id$/i }).count(), "an id column").toBe(0);
});

// ---- T24: Sessions and Workers ----------------------------------------------------------------------------------
uxr({ t: "T24", uxr: ["UXR-016"], needs: [...FOUNDATIONS, "WP-5"] }, "Sessions: active first, History collapsed with a count", async ({ page }, info) => {
  await openApp(page, { auth: "in" }, "/sessions");
  const first = page.locator('[data-entity^="session:"]:visible').first();
  await expect(first).toBeVisible();
  await expect(first.locator(".pill")).toHaveAttribute("data-state", "running");
  const history = page.locator("details", { has: page.locator("summary", { hasText: /history/i }) });
  await expect(history).toHaveCount(1);
  expect(await history.evaluate((d) => (d as HTMLDetailsElement).open), "History is collapsed").toBe(false);
  await expect(history.locator("summary")).toContainText(/\(1\)|\b1\b/); // the mock has one finished session
  await evidence(page, info, ["UXR-016"], "sessions");
});

uxr({ t: "T24", uxr: ["UXR-017"], needs: [...FOUNDATIONS, "WP-5"] }, "Workers: offline first, one pill each, last seen shown", async ({ page }, info) => {
  await openApp(page, { auth: "in" }, "/workers");
  const rows = page.locator('[data-entity^="worker:"]:visible');
  await expect(rows).toHaveCount(3);
  const states = await rows.evaluateAll((els) => els.map((e) => e.querySelector(".pill")?.getAttribute("data-state")));
  expect(states[0], "the offline worker (beta) comes first").toBe("offline");
  expect(states.slice(1), "the online workers follow").toEqual(["online", "online"]);
  await expect(rows.first()).toContainText(/last seen/i);
  await evidence(page, info, ["UXR-017"], "workers");
});
