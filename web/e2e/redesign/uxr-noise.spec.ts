// T16, T17 (UXR-010, UXR-011): routine events are silent; toasts only answer the operator's own actions.
import { MISSIONS } from "../mock";
import { evidence } from "./evidence";
import { expect, uxr } from "./gate";
import { DENSE_CALM_URL } from "../mock";
import { isPhone, MISSION_URL, openApp, openTab } from "./harness";

const FOUNDATIONS = ["WP-0", "WP-1"] as const;

uxr({ t: "T16", uxr: ["UXR-010"], needs: [...FOUNDATIONS, "WP-3"] }, "ten minutes of heartbeats and capacity refreshes: 0 toasts, 0 visible plumbing rows, header unchanged", async ({ page }, info) => {
  const app = await openApp(page, { auth: "in", fakeClock: true }, MISSION_URL);
  await openTab(page, "Activity");
  const headerBefore = await page.evaluate(() => ({
    pills: [...document.querySelectorAll('[data-entity^="mission:"] .pill')].map((p) => p.textContent?.trim()),
    actions: [...document.querySelectorAll("[data-action]")].filter((a) => (a as HTMLElement).offsetWidth > 0).map((a) => a.getAttribute("data-action")),
  }));
  for (let minute = 1; minute <= 10; minute += 1) {
    app.pushEvent({ kind: "controller-heartbeat", data: { controller_state: "RUNNING" } });
    app.pushEvent({ kind: "capacity-refresh", data: {} });
    await page.clock.runFor(60_000);
    await page.waitForTimeout(60);
    expect(await page.locator(".toast").count(), `toasts after minute ${minute}`).toBe(0);
    expect(await page.locator("[data-event-class=plumbing]:visible").count(), `visible plumbing rows after minute ${minute}`).toBe(0);
  }
  const headerAfter = await page.evaluate(() => ({
    pills: [...document.querySelectorAll('[data-entity^="mission:"] .pill')].map((p) => p.textContent?.trim()),
    actions: [...document.querySelectorAll("[data-action]")].filter((a) => (a as HTMLElement).offsetWidth > 0).map((a) => a.getAttribute("data-action")),
  }));
  expect(headerAfter, "the header (state pill and actions) is unchanged by routine events").toEqual(headerBefore);
  const footer = page.locator("[data-plumbing-count]");
  await expect.poll(async () => Number((await footer.getAttribute("data-plumbing-count")) ?? "0"), { message: "the hidden count grows" }).toBeGreaterThanOrEqual(20);
  await evidence(page, info, ["UXR-010"], "after-ten-minutes");
});

uxr({ t: "T16", uxr: ["UXR-010", "UXR-012"], needs: [...FOUNDATIONS, "WP-4"] }, "one approval event adds exactly one Inbox row, at most one toast and no dialog", async ({ page }) => {
  const app = await openApp(page, { auth: "in", fakeClock: true }, "/");
  const rows = page.locator('[data-section="needs-you"] [data-entity^="mission:"]');
  await expect(rows.first()).toBeVisible();
  const before = await rows.count();
  const quiet = MISSIONS[5]!.mission.id; // "Add telemetry opt-out", created, nothing needed
  app.setNeeds(quiet, { kind: "approval", tasks: [1], message: "task 1 awaits approval" });
  app.pushEvent({ kind: "task-status", mission_id: quiet, task_id: 1, data: { from: "RUNNING", to: "VERIFYING" } });
  await page.clock.runFor(2000);
  await expect(rows).toHaveCount(before + 1);
  expect(await page.locator(".toast").count(), "toasts for a new needs-you item").toBeLessThanOrEqual(1);
  expect(await page.locator('dialog[open], [role="dialog"]:visible').count(), "dialogs that opened by themselves").toBe(0);
});

// ---- T17 -----------------------------------------------------------------------------------------------------------
uxr({ t: "T17", uxr: ["UXR-011"], needs: [...FOUNDATIONS, "WP-3"] }, "one operator action yields exactly one polite, dismissible toast; the stream echo adds none", async ({ page }) => {
  const app = await openApp(page, { auth: "in", dataset: "dense", fakeClock: true }, DENSE_CALM_URL);
  await expect(page.locator("#toasts")).toHaveAttribute("role", "status");
  await page.locator('[data-action="pause"]').click();
  await expect(page.locator(".toast")).toHaveCount(1);
  app.pushEvent({ kind: "mission-status", mission_id: DENSE_CALM_URL.split("/")[3]!, data: { from: "RUNNING", to: "PAUSED" } });
  await page.clock.runFor(1500);
  await expect(page.locator(".toast"), "the stream event of the same action adds no second toast").toHaveCount(1);
  await page.locator(".toast button").first().click();
  await expect(page.locator(".toast")).toHaveCount(0);
});

uxr({ t: "T17", uxr: ["UXR-011"], needs: [...FOUNDATIONS, "WP-3"] }, "toasts: at most two visible (one on a phone), success toasts expire, error toasts stay, none covers a primary control", async ({ page }) => {
  const app = await openApp(page, { auth: "in", dataset: "dense", fakeClock: true }, DENSE_CALM_URL);
  for (const action of ["pause", "resume", "pause", "resume"]) {
    await page.locator(`[data-action="${action}"]`).click();
    await page.waitForTimeout(80);
  }
  const limit = isPhone(page) ? 1 : 2;
  expect(await page.locator(".toast:visible").count(), "visible toasts").toBeLessThanOrEqual(limit);
  const overlaps = await page.evaluate(() => {
    const boxes = [...document.querySelectorAll<HTMLElement>(".toast")].map((t) => t.getBoundingClientRect());
    const targets = [...document.querySelectorAll<HTMLElement>("[data-action]:not(.toast *), [data-composer]")].filter((e) => e.offsetWidth > 0 && !e.closest(".toast")).map((e) => ({ name: e.getAttribute("data-action") ?? "composer", r: e.getBoundingClientRect() }));
    return targets.filter((t) => boxes.some((b) => b.left < t.r.right && b.right > t.r.left && b.top < t.r.bottom && b.bottom > t.r.top)).map((t) => t.name);
  });
  expect(overlaps, "primary controls covered by a toast").toEqual([]);
  await page.clock.runFor(10_000);
  await expect(page.locator(".toast"), "success toasts expire").toHaveCount(0);
  expect(app.operations().filter((o) => o === "mission.pause" || o === "mission.resume").length).toBeGreaterThanOrEqual(2);
});

uxr({ t: "T17", uxr: ["UXR-011"], needs: [...FOUNDATIONS, "WP-3"] }, "an error toast stays until it is dismissed", async ({ page }) => {
  await openApp(page, { auth: "in", dataset: "dense", fakeClock: true, failMutations: true }, DENSE_CALM_URL);
  await page.locator('[data-action="pause"]').click();
  await expect(page.locator(".toast")).toHaveCount(1);
  await page.clock.runFor(60_000);
  await expect(page.locator(".toast"), "an error toast does not time out").toHaveCount(1);
  await page.locator(".toast button").first().click();
  await expect(page.locator(".toast")).toHaveCount(0);
});

uxr({ t: "T17", uxr: ["UXR-011"], needs: [...FOUNDATIONS, "WP-3"] }, "Copy shows no toast", async ({ page, context, browserName }) => {
  if (browserName === "chromium") await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await openApp(page, { auth: "in" }, MISSION_URL);
  await page.locator('[data-l0="mission.header"] button[aria-haspopup="menu"]').click();
  await page.getByRole("menuitem", { name: /copy mission id/i }).click();
  await page.waitForTimeout(300);
  expect(await page.locator(".toast").count(), "toasts after Copy").toBe(0);
});
