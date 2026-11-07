// T36 (UXR-033, UXR-034): INP lab proxy, typing latency under a terminal flood and CLS at Pixel 4 with 4x CPU throttling.
// Chromium only (CDP): the chromium-mobile project. INP is approximated by the Event Timing durations of the real
// pointer and key events of each interaction (input delay + handlers + next paint), which is what INP reports.
import { test as pw, type CDPSession, type Page } from "@playwright/test";
import { evidence } from "./evidence";
import { expect, uxr } from "./gate";
import { MISSION_URL, openApp, viewport } from "./harness";
import { needsFor, PAGES } from "./pages";

const RUNS = 5;
const INP_MAX = 200;
const INP_TARGET = 100;
const TYPING_P95_MAX = 50;
const CLS_MAX = 0.02;

async function throttle(page: Page): Promise<CDPSession> {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
  return cdp;
}
const onPixel4 = (page: Page, project: string): boolean => project === "chromium-mobile" && viewport(page).width <= 400;

const OBSERVE = `
(() => {
  const w = window;
  w.__ev = [];
  new PerformanceObserver((list) => { for (const e of list.getEntries()) w.__ev.push({ name: e.name, start: e.startTime, duration: e.duration }); }).observe({ type: "event", durationThreshold: 16, buffered: true });
  w.__cls = 0;
  new PerformanceObserver((list) => { for (const e of list.getEntries()) if (!e.hadRecentInput) w.__cls += e.value; }).observe({ type: "layout-shift", buffered: true });
})();
`;

/** Longest Event Timing duration among events that started after `since`. */
async function worst(page: Page, since: number): Promise<number> {
  await page.waitForTimeout(250);
  return page.evaluate((t) => ((window as unknown as { __ev: Array<{ start: number; duration: number }> }).__ev).filter((e) => e.start >= t).reduce((m, e) => Math.max(m, e.duration), 0), since);
}
const now = (page: Page) => page.evaluate(() => performance.now());
const median = (xs: number[]): number => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)] ?? 0;
const p95 = (xs: number[]): number => [...xs].sort((a, b) => a - b)[Math.max(0, Math.ceil(xs.length * 0.95) - 1)] ?? 0;

uxr({ t: "T36", uxr: ["UXR-033"], needs: ["WP-0", "WP-1", "WP-2", "WP-3", "WP-4"] }, "INP proxy at most 200 ms per interaction (open row, type, send, approve, switch tab), median reported against 100 ms", async ({ page }, info) => {
  pw.skip(!onPixel4(page, info.project.name), "measured on the chromium-mobile (Pixel 4) project");
  info.setTimeout(240_000);
  await page.addInitScript(OBSERVE);
  await throttle(page);
  const steps: Record<string, number[]> = { "open row": [], "type": [], "send": [], "approve": [], "switch tab": [] };
  for (let run = 0; run < RUNS; run += 1) {
    await openApp(page, { auth: "in", dataset: "dense", terminalScreen: true }, "/missions");
    let t = await now(page);
    await page.locator('[data-entity^="mission:"] a').first().click();
    await page.locator('[data-pane="session"]').waitFor();
    steps["open row"]!.push(await worst(page, t));
    await page.locator('[data-action="take-control"]').click();
    await expect(page.locator("#terminal-status")).toHaveAttribute("data-mode", "write");
    const field = page.locator("[data-composer]").locator("textarea, input").first();
    await field.focus();
    t = await now(page);
    await page.keyboard.type("go", { delay: 40 });
    steps["type"]!.push(await worst(page, t));
    t = await now(page);
    await page.keyboard.press("Enter");
    steps["send"]!.push(await worst(page, t));
    t = await now(page);
    const tabs = page.getByRole("tab", { name: /^tasks/i }).first();
    if (await tabs.isVisible().catch(() => false)) await tabs.click();
    steps["switch tab"]!.push(await worst(page, t));
    const approve = page.locator('[data-action="approve"]').first();
    t = await now(page);
    if (await approve.isVisible().catch(() => false)) await approve.click();
    steps["approve"]!.push(await worst(page, t));
  }
  const failures: string[] = [];
  for (const [name, values] of Object.entries(steps)) {
    info.annotations.push({ type: `INP ${name}`, description: `median ${median(values).toFixed(0)} ms (target ${INP_TARGET}), worst ${Math.max(...values).toFixed(0)} ms (limit ${INP_MAX})` });
    if (Math.max(...values) > INP_MAX) failures.push(`${name}: worst ${Math.max(...values).toFixed(0)} ms`);
  }
  expect(failures, "interactions above the INP limit at 4x CPU throttling").toEqual([]);
  await evidence(page, info, ["UXR-033"], "inp-mission");
});

uxr({ t: "T36", uxr: ["UXR-033"], needs: ["WP-0", "WP-1", "WP-2", "WP-3"] }, "typing under a 200 lines per second terminal stream stays under 50 ms per key (p95)", async ({ page }, info) => {
  pw.skip(!onPixel4(page, info.project.name), "measured on the chromium-mobile (Pixel 4) project");
  await page.addInitScript(OBSERVE);
  await throttle(page);
  await openApp(page, { auth: "in", terminalScreen: true, terminalFlood: true }, MISSION_URL);
  await page.locator('[data-action="take-control"]').click();
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-mode", "write");
  await page.locator("[data-composer]").locator("textarea, input").first().focus();
  const t = await now(page);
  await page.keyboard.type("the quick brown fox jumps over the lazy dog", { delay: 60 });
  await page.waitForTimeout(300);
  const keys = await page.evaluate((since) => ((window as unknown as { __ev: Array<{ name: string; start: number; duration: number }> }).__ev).filter((e) => e.name === "keydown" && e.start >= since).map((e) => e.duration), t);
  info.annotations.push({ type: "typing p95", description: `${p95(keys).toFixed(0)} ms over ${keys.length} slow-or-fast keys (limit ${TYPING_P95_MAX})` });
  expect(p95(keys), "p95 keydown duration under the flood (ms)").toBeLessThanOrEqual(TYPING_P95_MAX);
});

for (const def of PAGES.filter((p) => p.id === "inbox" || p.id === "missions" || p.id === "mission")) {
  uxr({ t: "T36", uxr: ["UXR-033", "UXR-034"], needs: needsFor(def) }, `${def.id}: CLS stays at most 0.02 through mount and live refresh`, async ({ page }, info) => {
    pw.skip(!onPixel4(page, info.project.name), "measured on the chromium-mobile (Pixel 4) project");
    await page.addInitScript(OBSERVE);
    await throttle(page);
    const app = await openApp(page, def.mock, def.path);
    await page.locator(def.ready).first().waitFor();
    for (let i = 0; i < 3; i += 1) { app.pushEvent({ kind: "task-status", data: { from: "RUNNING", to: "VERIFYING" } }); await page.waitForTimeout(600); }
    const cls = await page.evaluate(() => (window as unknown as { __cls: number }).__cls);
    info.annotations.push({ type: "CLS", description: cls.toFixed(4) });
    expect(cls, "cumulative layout shift").toBeLessThanOrEqual(CLS_MAX);
  });
}
