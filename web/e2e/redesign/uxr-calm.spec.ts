// T26 to T28 (UXR-011, UXR-022, UXR-042): calm design, the interrupt ladder and the disconnected last-known state.
import { MISSIONS } from "../mock";
import { evidence } from "./evidence";
import { expect, uxr } from "./gate";
import { openApp } from "./harness";
import { needsFor, PAGES } from "./pages";

const FOUNDATIONS = ["WP-0", "WP-1"] as const;

// ---- T26 -----------------------------------------------------------------------------------------------------------
for (const def of PAGES) {
  uxr({ t: "T26", uxr: ["UXR-022"], needs: needsFor(def) }, `${def.id}: at most one running animation (none with reduced motion)`, async ({ page }) => {
    await openApp(page, def.mock, def.path);
    await page.locator(def.ready).first().waitFor();
    await page.waitForTimeout(600); // let finished transitions end
    const running = () => page.evaluate(() => document.getAnimations().filter((a) => a.playState === "running" && !((a.effect as KeyframeEffect | null)?.target as Element | null)?.closest(".xterm")).map((a) => `${(a as CSSAnimation).animationName ?? (a as CSSTransition).transitionProperty ?? "animation"} on ${((a.effect as KeyframeEffect | null)?.target as Element | null)?.className ?? "?"}`));
    expect((await running()).length, `running animations: ${(await running()).join("; ")}`).toBeLessThanOrEqual(1);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.reload();
    await page.locator(def.ready).first().waitFor();
    await page.waitForTimeout(400);
    expect(await running(), "running animations with prefers-reduced-motion: reduce").toEqual([]);
  });
}

uxr({ t: "T26", uxr: ["UXR-022", "UXR-002"], needs: [...FOUNDATIONS, "WP-4"] }, "only rank 1 and 2 rows have a saturated background or leading bar", async ({ page }, info) => {
  await openApp(page, { auth: "in", dataset: "dense" }, "/missions");
  await page.locator('[data-entity^="mission:"]').first().waitFor();
  const report = await page.evaluate(() => {
    const colour = (token: string) => { const s = document.createElement("span"); s.style.color = `var(${token})`; document.body.append(s); const c = getComputedStyle(s).color; s.remove(); return c; };
    const alert = new Set(["--alert-attention-bg", "--alert-attention-bar", "--alert-failed-bg", "--alert-failed-bar"].map(colour));
    const rows = [...document.querySelectorAll<HTMLElement>("[data-entity]")].map((row) => {
      const pill = row.querySelector<HTMLElement>(".pill");
      const s = getComputedStyle(row);
      const before = getComputedStyle(row, "::before");
      const colours = [s.backgroundColor, s.borderInlineStartColor, before.backgroundColor, pill ? getComputedStyle(pill).backgroundColor : ""];
      return { entity: row.dataset["entity"], tier: pill?.dataset["tier"] ?? "", state: pill?.dataset["state"] ?? "", saturated: colours.some((c) => alert.has(c)) };
    });
    return rows;
  });
  expect(report.length, "rows on the page").toBeGreaterThan(0);
  expect(report.filter((r) => r.tier !== "alert" && r.saturated).map((r) => `${r.entity} (${r.state})`), "quiet rows with an alert colour").toEqual([]);
  expect(report.filter((r) => r.tier === "alert" && !r.saturated).map((r) => `${r.entity} (${r.state})`), "alert rows without an alert colour").toEqual([]);
  await evidence(page, info, ["UXR-022"], "calm-missions");
});

// ---- T27 -----------------------------------------------------------------------------------------------------------
uxr({ t: "T27", uxr: ["UXR-042", "UXR-011"], needs: [...FOUNDATIONS, "WP-4"] }, "the tab title carries the needs-you count and the favicon shows a dot only when it is above zero", async ({ page }) => {
  await openApp(page, { auth: "in" }, "/");
  await expect(page).toHaveTitle(/^\(\d+\) /);
  const withDot = await page.locator('link[rel~="icon"]').first().getAttribute("href");
  expect(decodeURIComponent(withDot ?? ""), "favicon with something that needs the operator").toContain("circle");
  const quiet = await page.context().newPage();
  await openApp(quiet, { auth: "in", missions: "none" }, "/");
  await expect(quiet).not.toHaveTitle(/^\(\d+\) /);
  const without = await quiet.locator('link[rel~="icon"]').first().getAttribute("href");
  expect(without, "favicon without a dot differs from the one with it").not.toBe(withDot);
  await quiet.close();
});

uxr({ t: "T27", uxr: ["UXR-011"], needs: [...FOUNDATIONS, "WP-4"] }, "no toast for done, a worker offline under 10 s, or a reconnect under 10 s", async ({ page }) => {
  const app = await openApp(page, { auth: "in", fakeClock: true }, "/");
  app.pushEvent({ kind: "mission-status", mission_id: MISSIONS[2]!.mission.id, data: { from: "RUNNING", to: "COMPLETED" } });
  app.emitStream({ type: "worker", worker: "alpha", state: "offline" });
  await page.clock.runFor(5000);
  app.emitStream({ type: "worker", worker: "alpha", state: "online" });
  app.dropStream();
  await page.clock.runFor(4000);
  app.restoreStream();
  await page.clock.runFor(8000);
  await page.waitForTimeout(100);
  expect(await page.locator(".toast").count(), "toasts for done, offline under 10 s and reconnect under 10 s").toBe(0);
});

uxr({ t: "T27", uxr: ["UXR-042"], needs: [...FOUNDATIONS, "WP-1"] }, "browser notification permission is requested only from a gesture", async ({ page }) => {
  await page.addInitScript(() => {
    const w = window as unknown as { __notifyAsks: number };
    w.__notifyAsks = 0;
    const stub = function Notification() {} as unknown as { permission: string; requestPermission: () => Promise<string> };
    stub.permission = "default";
    stub.requestPermission = () => { w.__notifyAsks += 1; return Promise.resolve("denied"); };
    (window as unknown as { Notification: unknown }).Notification = stub;
  });
  const app = await openApp(page, { auth: "in", fakeClock: true }, "/");
  app.pushEvent({ kind: "task-status", mission_id: MISSIONS[5]!.mission.id, task_id: 1, data: { from: "RUNNING", to: "VERIFYING" } });
  await page.clock.runFor(3000);
  expect(await page.evaluate(() => (window as unknown as { __notifyAsks: number }).__notifyAsks), "permission requests on load or from a stream event").toBe(0);
  const optIn = page.getByRole("button", { name: /notification/i }).first();
  if (await optIn.isVisible().catch(() => false)) {
    await optIn.click();
    expect(await page.evaluate(() => (window as unknown as { __notifyAsks: number }).__notifyAsks), "permission requests after the click").toBe(1);
  }
});

// ---- T28 -----------------------------------------------------------------------------------------------------------
uxr({ t: "T28", uxr: ["UXR-042", "UXR-034"], needs: [...FOUNDATIONS, "WP-3"] }, "when the stream drops the page keeps its data, shows 'as of', disables mutations and recovers", async ({ page }, info) => {
  // A fake clock, not the fixed one: the age of the data appears at 10 s (D28.6), so the clock has to move.
  const app = await openApp(page, { auth: "in", dataset: "dense", terminalScreen: true, fakeClock: true }, `/missions/alpha/${MISSIONS[0]!.mission.id}`);
  const header = page.locator('[data-entity^="mission:"]').first();
  await expect(header).toBeVisible();
  const pillBefore = await header.locator(".pill").first().textContent();
  const textBefore = (await page.locator("main").innerText()).length;
  app.dropStream();
  const banner = page.locator("#conn-banner");
  await page.clock.runFor(5500);
  await expect(banner).toBeVisible({ timeout: 20_000 });
  await expect(banner).toContainText(/as of \d{1,2}:\d{2}/i);
  await expect(banner, "no age before the data is 10 s old").not.toContainText(/ago/i);
  await page.clock.runFor(6000);
  await expect(banner, "the age appears at 10 s").toContainText(/ago/i);
  expect(await page.locator("[data-asof]").count(), "live regions carry data-asof").toBeGreaterThan(0);
  expect((await header.locator(".pill").first().textContent()), "the last-known state is kept, not recomputed to Offline").toBe(pillBefore);
  expect((await page.locator("main").innerText()).length, "the page is not blanked").toBeGreaterThan(textBefore * 0.6);
  const mutate = page.locator('[data-action="pause"], [data-action="stop"]').first();
  if (await mutate.count()) {
    await expect(mutate).toBeDisabled();
    expect(`${await mutate.getAttribute("title")} ${await page.locator("main").innerText()}`, "mutations are disabled with the reason").toMatch(/disconnected|offline|reconnect/i);
  }
  await evidence(page, info, ["UXR-042"], "disconnected");
  app.restoreStream();
  await page.clock.runFor(20_000);
  await expect(banner).toBeHidden({ timeout: 30_000 });
});

uxr({ t: "T28", uxr: ["UXR-042"], needs: [...FOUNDATIONS, "WP-4"] }, "the Inbox keeps its rows while disconnected", async ({ page }) => {
  const app = await openApp(page, { auth: "in", dataset: "dense" }, "/");
  const rows = page.locator("[data-entity]");
  await expect(rows.first()).toBeVisible();
  const before = await rows.count();
  app.dropStream();
  await expect(page.locator("#conn-banner")).toBeVisible({ timeout: 20_000 });
  expect(await rows.count(), "rows while disconnected").toBe(before);
});
