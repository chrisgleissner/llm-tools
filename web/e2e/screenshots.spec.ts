// Visual regression baselines and accessibility scans for every page and state, in light and dark, on deterministic
// data (e2e/mock.ts) with a fixed clock and animations disabled (WEB-003, WEB-009, TEST-011).
//
// Baselines exist for the two Chromium projects (desktop 1440x900 and Pixel 4). They are generated in the official
// Playwright Docker image so that font rendering matches CI (see web/README.md). The same states are scanned with
// axe-core on all four projects, in both themes, and must have no violations of any impact.
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "./fixtures";
import { DENSE_CALM_URL, mockApp, MISSION_PATH, MISSION_START_PATH } from "./mock";
import { settle, showTab } from "./redesign/harness";
import { SCREENS } from "./screens";
import { IN } from "./screens/types";
import { currentWorkSummary } from "../src/current-work";
import type { Page } from "@playwright/test";

for (const theme of ["light", "dark"] as const) {
  for (const s of SCREENS) {
    test(`${s.name} (${theme})`, async ({ page, browserName }, info) => {
      const narrow = (page.viewportSize()?.width ?? 1000) <= 720;
      test.skip(Boolean(s.narrowOnly) && !narrow, "phone only");
      test.skip(Boolean(s.wideOnly) && narrow, "desktop only");
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await mockApp(page, s.mock);
      await page.goto(s.path);
      await s.run(page);
      if (s.mock.auth === "in" && s.path !== "/missions" && !s.name.startsWith("login")) await page.locator("#conn").waitFor();
      // A toast that the screen captures dismisses itself after 4 s (8 s for Undo), and settle() may take longer than that on a loaded host. The page holds
      // a toast while the pointer is on it (src/ui.ts), so send the toast the same `mouseenter`; nothing changes in production code.
      await page.evaluate(() => document.querySelectorAll(".toast").forEach((toast) => toast.dispatchEvent(new MouseEvent("mouseenter"))));
      await settle(page); // the data is loaded and drawn (see harness.ts), so the baseline does not depend on timing
      await page.mouse.move(0, 0);
      // Focus rings and the caret must not depend on timing.
      await page.evaluate(() => (document.activeElement as HTMLElement | null)?.matches("input, textarea") || (document.activeElement as HTMLElement | null)?.blur?.());
      const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]).disableRules(["region"]).analyze();
      expect(axe.violations.map((v) => `${v.impact} ${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`), `axe (${s.name}, ${theme})`).toEqual([]);
      if (process.env["BRIDGE_SKIP_VISUAL"] !== "1" && info.project.name.startsWith("chromium") && browserName === "chromium") await expect(page).toHaveScreenshot(`${s.name}-${theme}.png`);
    });
  }
}

test("Codex queued-question attention opens the mission's live terminal", async ({ page }) => {
  await mockApp(page, { ...IN, providerQuestion: true });
  await page.goto("/");
  const row = page.locator('[data-section="needs-you"] [data-entity^="mission:"]', { hasText: "Should I continue?" }).first();
  await expect(row.locator(".pill")).toHaveAttribute("data-state", "needs-input");
  await row.locator("a.row-title").click();
  await expect(page).toHaveURL(/\/missions\/alpha\//);
  await expect(page.locator("#terminal-control")).toBeEnabled();
});

test("running mission streams output read-only without a click or sending input", async ({ page }) => {
  const mock = await mockApp(page, { ...IN, dataset: "dense", terminalScreen: true });
  const journeyStartedAt = Date.now();
  let summaryReceivedAt = 0;
  page.on("response", (response) => {
    if (new URL(response.url()).pathname.endsWith("/missions") && response.request().method() === "GET") summaryReceivedAt = Date.now();
  });
  await page.goto(DENSE_CALM_URL); // a calm running mission: the header leaves the terminal room on a phone
  const status = page.locator("#terminal-status");
  await expect(status).toHaveAttribute("data-state", "live");
  await expect(status).toHaveAttribute("data-mode", "view");
  await expect(status).toContainText("Watching, read only");
  await expect(page.locator("#terminal-host")).toBeVisible();
  await expect(page.locator("#terminal-host .xterm-rows")).toContainText("Reading src/tokenizer.rs");
  await expect(page.locator("#terminal-host .xterm-rows")).toBeInViewport({ ratio: 0.25 });
  const outputLatencyMs = Date.now() - summaryReceivedAt;
  console.log(`UX terminal task: mission navigation to visible output ${Date.now() - journeyStartedAt} ms; clicks 0`);
  console.log(`UX running mission: summary response to visible output ${outputLatencyMs} ms; clicks 0`);
  expect(outputLatencyMs).toBeLessThan(1000);
  await page.locator("#terminal-host .xterm-helper-textarea").focus();
  await page.keyboard.type("must-not-send");
  await page.keyboard.press("Enter");
  expect(mock.terminalInputCount()).toBe(0);
  await page.getByRole("button", { name: "Take control" }).click();
  await expect(status).toHaveAttribute("data-mode", "write");
  expect(mock.terminalInputCount()).toBe(0);
});

test("current-work summary omits missing provider and model data", async () => {
  expect(currentWorkSummary({ current_task: { ordinal: 7, title: "Investigate the timeout" }, station_observation: { station: "unknown", model: "unknown" }, station_context: { last_launched_station: "" } })).toBe("Task 7 · Investigate the timeout");
  expect(currentWorkSummary({ current_task: { ordinal: 3, title: "Run checks" }, station_context: { last_launched_station: "codex" }, station_observation: { station: "unknown", model: "unknown" }, station_config: { models: { codex: "gpt-5" } } })).toBe("Task 3 · Run checks · codex · gpt-5");
});

test("a running pill in a list never animates and keeps its geometry, with and without reduced motion", async ({ page }) => {
  await mockApp(page, { ...IN, dataset: "dense" });
  await page.goto("/");
  const ring = page.locator('.pill[data-state="running"] svg').first();
  await expect(ring).toBeVisible();
  await page.waitForTimeout(400); // the list settles (rows are re-read once after the first render)
  const before = await ring.boundingBox();
  expect(before, "the ring has a box").not.toBeNull();
  await page.waitForTimeout(250);
  const after = await ring.boundingBox();
  expect(after?.x).toBe(before?.x);
  expect(after?.y).toBe(before?.y);
  expect(after?.width).toBe(before?.width);
  expect(after?.height).toBe(before?.height);
  await page.emulateMedia({ reducedMotion: "no-preference" });
  expect(await ring.evaluate((el) => getComputedStyle(el).animationName), "list pills are calm").toBe("none");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect.poll(() => ring.evaluate((el) => getComputedStyle(el).animationName)).toBe("none");
});

test("Pixel 4 mission journey records layout shift and interaction latency", async ({ page }, info) => {
  test.skip(info.project.name !== "chromium-mobile", "INP and CLS capture uses Chromium Pixel 4 emulation");
  test.setTimeout(180_000);
  const context = page.context();
  const cdp = await context.newCDPSession(page);
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
  await page.addInitScript(() => {
    const metrics = { cls: 0, interactions: [] as number[], shifts: [] as Array<{ value: number; sources: string[] }> };
    (window as Window & { __uxMetrics?: typeof metrics }).__uxMetrics = metrics;
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries() as Array<PerformanceEntry & { value?: number; hadRecentInput?: boolean; sources?: Array<{ node?: Node }> }>) {
        if (entry.hadRecentInput) continue;
        metrics.cls += entry.value ?? 0;
        metrics.shifts.push({ value: entry.value ?? 0, sources: (entry.sources ?? []).map(({ node }) => node instanceof Element ? `${node.tagName.toLowerCase()}#${node.id}.${String(node.className).replace(/\s+/g, ".")}` : "unknown") });
      }
    }).observe({ type: "layout-shift", buffered: true });
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries() as Array<PerformanceEntry & { duration: number }>) metrics.interactions.push(entry.duration);
    }).observe({ type: "event", buffered: true, durationThreshold: 16 } as PerformanceObserverInit & { durationThreshold: number });
  });
  await mockApp(page, { ...IN, terminalScreen: true });
  const runs: Array<{ cls: number; inpMs: number; shifts: Array<{ value: number; sources: string[] }> }> = [];
  for (let i = 0; i < 5; i++) {
    await page.goto("/");
    const approval = page.locator('[data-section="needs-you"] [data-entity^="mission:"]', { has: page.locator('.pill[data-state="needs-approval"]') }).first();
    await expect(approval).toBeVisible();
    await approval.locator("a.row-title").click();
    await expect(page.locator("#terminal-status")).toHaveAttribute("data-mode", "view");
    await expect(page.locator("#terminal-host .xterm-rows")).toContainText("Reading src/tokenizer.rs");
    await page.waitForTimeout(100);
    runs.push(await page.evaluate(() => {
      const collected = (window as Window & { __uxMetrics?: { cls: number; interactions: number[]; shifts: Array<{ value: number; sources: string[] }> } }).__uxMetrics;
      return { cls: collected?.cls ?? 0, inpMs: Math.max(0, ...(collected?.interactions ?? [])), shifts: collected?.shifts ?? [] };
    }));
  }
  const median = (values: number[]) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)] ?? 0;
  const clsMedian = median(runs.map((r) => r.cls));
  const inpMedian = median(runs.map((r) => r.inpMs));
  console.log(`UX Pixel 4 at 4x CPU, 5 runs: clicks 1, CLS ${runs.map((r) => r.cls.toFixed(3)).join(",")}, median ${clsMedian.toFixed(3)}; max observed interaction-event duration (INP lab proxy) ${runs.map((r) => r.inpMs.toFixed(0)).join(",")} ms, median ${inpMedian.toFixed(0)} ms; shifts ${JSON.stringify(runs.map((r) => r.shifts))}`);
  expect(Math.max(...runs.map((r) => r.cls))).toBeLessThan(0.1);
  await cdp.detach();
});

test("mission task UI uses resource reads and idempotent resource mutations", async ({ page }) => {
  const requests: Array<{ method: string; path: string; key?: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.includes("/missions/") && url.pathname.includes("/tasks")) {
      requests.push({ method: request.method(), path: url.pathname, key: request.headers()["idempotency-key"] });
    }
  });
  await mockApp(page, IN);
  await page.goto(MISSION_PATH);
  await showTab(page, "Tasks");
  await expect(page.locator('[data-entity^="task:"]')).toHaveCount(5); // the sixth task is completed and sits in the closed Completed group
  await page.getByRole("button", { name: "More actions for task 5" }).click();
  await page.getByRole("menuitem", { name: "Pause task" }).click();
  await expect(page.locator(".toast.applied")).toBeVisible();
  expect(requests.some((request) => request.method === "GET" && request.path.endsWith("/tasks"))).toBe(true);
  expect(requests.some((request) => request.method === "POST" && request.path.endsWith("/tasks/5/pause") && request.key)).toBe(true);
  expect(requests.some((request) => request.path.endsWith("/commands"))).toBe(false);
});

test("a created mission can be started from its detail page through the resource API", async ({ page }) => {
  const requests: Array<{ method: string; path: string; key?: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.includes("/missions/")) requests.push({ method: request.method(), path: url.pathname, key: request.headers()["idempotency-key"] });
  });
  await mockApp(page, IN);
  await page.goto(MISSION_START_PATH);
  await expect(page.getByRole("heading", { name: "Add telemetry opt-out" })).toBeVisible();
  await page.getByRole("button", { name: "Start mission" }).click();
  await expect(page.locator(".toast.applied")).toContainText("Mission started");
  await expect(page.locator(".mission-head .pill")).not.toHaveAttribute("data-state", "queued"); // the header no longer offers Start
  expect(requests.some((request) => request.method === "POST" && request.path.endsWith("/start") && request.key)).toBe(true);
  expect(requests.some((request) => request.path.endsWith("/commands"))).toBe(false);
});
