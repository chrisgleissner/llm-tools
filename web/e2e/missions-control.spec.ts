// PRD-014: the Missions list groups what needs the operator, what is rate-limited and which worker is unhealthy, and stays bounded at 500 missions.
// The 500-mission checks count rendered nodes; they assert no wall-clock bound.
import { groupOf, withHeadings } from "../src/grouping";
import { windowByHeights, windowSlice } from "../src/virtual";
import { deriveState } from "../src/state";
import { expect, test } from "./fixtures";
import { mockApp } from "./mock";
import { openApp } from "./redesign/harness";

// ---- pure arithmetic

test("windowSlice builds a bounded number of entries for 500 and all of a short list", () => {
  const long = windowSlice(500, 48, 4800, 900, 8, 60);
  expect(long.end - long.start).toBeLessThanOrEqual(40);
  expect(long.start).toBeGreaterThan(0);
  expect(long.before).toBe(long.start * 48);
  expect(long.after).toBe((500 - long.end) * 48);
  expect(windowSlice(500, 48, 0, 900).start).toBe(0);
  expect(windowSlice(500, 48, 48 * 499, 900).end).toBe(500);
  const short = windowSlice(30, 48, 0, 400);
  expect([short.start, short.end, short.before, short.after]).toEqual([0, 30, 0, 0]);
});

test("missions are grouped: needs you, unhealthy worker, rate-limited, running, then paused and queued", () => {
  const state = (status: string, needsYou: { kind: string } | null, worker = "online") => deriveState({ status, needsYou, worker: { state: worker }, conn: "live" });
  expect(groupOf(state("RUNNING", { kind: "approval" }))).toBe("attention");
  expect(groupOf(state("FAILED", { kind: "failed" }))).toBe("attention");
  expect(groupOf(state("RUNNING", { kind: "capacity" }))).toBe("rate-limited");
  expect(groupOf(state("RUNNING", null, "offline"))).toBe("unhealthy");
  expect(groupOf(state("RUNNING", null))).toBe("running");
  expect(groupOf(state("PAUSED", null))).toBe("idle");
  const flat = withHeadings(["a", "b", "c"], (x) => (x === "c" ? "running" : "attention"));
  expect(flat.map((e) => (e.kind === "head" ? `${e.title}:${e.count}` : e.item))).toEqual(["Needs you:2", "a", "b", "Running:1", "c"]);
});

// ---- 500 missions in the page

const MIX = ["FAILED", "RUNNING", "RUNNING", "RUNNING", "RUNNING", "RUNNING", "PAUSED", "COMPLETED", "CREATED", "STOPPED"] as const;
const TOTAL = 500;
/** Needs-you or rate-limited ones among the 500, by index modulo 10: 0 failed, 1 approval, 5 capacity (rate-limited). */
const ATTENTION = TOTAL / 10 * 2;
const LIMITED = TOTAL / 10;

test.beforeEach(async ({ page }) => {
  await page.addInitScript(
    ({ total, mix }) => {
      const original = window.fetch.bind(window);
      const now = Date.parse("2026-11-05T12:00:00Z");
      const entries = Array.from({ length: total }, (_, i) => {
        const status = mix[i % 10]!;
        const live = ["RUNNING", "PAUSED", "CREATED"].includes(status);
        const needs = i % 10 === 0 ? { kind: "failed", message: "mission failed", tasks: [] } : i % 10 === 1 ? { kind: "approval", message: "task 2 awaits your review", tasks: [2] } : i % 10 === 5 ? { kind: "capacity", message: "waiting for capacity", tasks: [] } : null;
        return {
          worker: "alpha",
          stale: false,
          mission: {
            id: `${String(i).padStart(4, "0")}a1b2c3d4e5f60718293a4b5c6d7e8f`,
            name: `Mission ${String(i).padStart(3, "0")}`,
            status,
            folder: "bulk",
            updated_at: new Date(now - i * 60_000).toISOString(),
            task_counts: { COMPLETED: 1, QUEUED: 1 },
            attention: live && needs ? needs : null,
            ...(live ? { current_task: null, station: "codex", model: "gpt-5", needs_kind: needs?.kind ?? null, needs_tasks: needs?.tasks.length ?? 0, ...(needs ? { needs_you: needs } : {}) } : {}),
          },
        };
      });
      window.fetch = async (input, init) => {
        const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.href);
        if (url.pathname !== "/api/v1/missions" || (init?.method ?? "GET") !== "GET") return original(input, init);
        return new Response(JSON.stringify({ missions: entries, total, offset: 0, statuses: {} }), { status: 200, headers: { "content-type": "application/json" } });
      };
    },
    { total: TOTAL, mix: [...MIX] },
  );
});

const activeNodes = (page: import("@playwright/test").Page) => page.locator('[data-section="active"] > *').count();

test("500 missions: the list holds a bounded number of row nodes and the groups come first", async ({ page }) => {
  await openApp(page, { auth: "in", dataset: "dense" }, "/missions");
  await expect(page.locator('[data-section="active"] .mrow').first()).toBeVisible();
  await expect.poll(() => page.locator('[data-section="active"] .mrow').count(), "rendered mission rows of 500").toBeLessThanOrEqual(60);
  expect(await activeNodes(page), "child nodes of the active list").toBeLessThanOrEqual(70);
  // Headings keep the attention items first: the first heading is Needs you with every such mission counted, though most rows are not in the DOM.
  const first = page.locator('[data-section="active"] .mgroup').first();
  await expect(first).toContainText("Needs you");
  await expect(first).toContainText(`(${ATTENTION})`);
  await expect(page.locator('[data-section="active"] .mrow').first()).toHaveAttribute("data-tier", "alert");
  void LIMITED;
  // Scrolling to the end shows the last active mission and still holds a bounded number of nodes.
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect.poll(() => page.locator('[data-section="active"] .mrow').count()).toBeLessThanOrEqual(60);
  await expect(page.locator('[data-section="active"] .mrow[data-state="paused"], [data-section="active"] .mrow[data-state="queued"]').last()).toBeVisible();
  expect(await activeNodes(page)).toBeLessThanOrEqual(70);
});

test("windowByHeights uses each entry's own height and keeps before and after consistent", () => {
  const heights = Array.from({ length: 200 }, (_, i) => (i % 2 ? 100 : 30)); // alternating tall and short rows
  const total = heights.reduce((a, b) => a + b, 0);
  const w = windowByHeights(heights, 3000, 800, 200, 60);
  const shown = heights.slice(w.start, w.end).reduce((a, b) => a + b, 0);
  expect(w.before + shown + w.after).toBe(total);
  expect(w.before).toBeLessThanOrEqual(2800);
  expect(w.before + shown).toBeGreaterThanOrEqual(4000);
  expect(w.end - w.start).toBeLessThan(40);
  expect(windowByHeights(heights.slice(0, 30), 0, 800, 200, 60)).toEqual({ start: 0, end: 30, before: 0, after: 0 });
});

test("500 missions: rows announce their place in the whole list", async ({ page }) => {
  await openApp(page, { auth: "in", dataset: "dense" }, "/missions");
  const active = 400;
  await expect.poll(() => page.locator('[data-section="active"] .mrow').count()).toBeLessThanOrEqual(60);
  const first = page.locator('[data-section="active"] .mrow').first();
  await expect(first).toHaveAttribute("aria-setsize", String(active));
  await expect(first).toHaveAttribute("aria-posinset", "1");
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect(page.locator('[data-section="active"] .mrow[aria-posinset]').last()).toHaveAttribute("aria-posinset", String(active));
});

test("500 missions: the row with the keyboard focus stays in the list when it scrolls far out of the window", async ({ page }) => {
  await openApp(page, { auth: "in", dataset: "dense" }, "/missions");
  await expect.poll(() => page.locator('[data-section="active"] .mrow').count()).toBeLessThanOrEqual(60);
  const key = await page.locator('[data-section="active"] .mrow').first().getAttribute("data-row-key");
  await page.locator('[data-section="active"] .mrow .row-title').first().focus();
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect(page.locator('[data-section="active"] .mrow[aria-posinset="400"]')).toHaveCount(1);
  await expect(page.locator(`[data-section="active"] [data-row-key="${key}"]`)).toHaveCount(1);
  expect(await page.evaluate(() => document.activeElement?.closest("[data-row-key]")?.getAttribute("data-row-key"))).toBe(key);
  expect(await activeNodes(page)).toBeLessThanOrEqual(75);
});

test("500 missions: until the window module has loaded the whole list is shown, not its first 60 rows", async ({ page }) => {
  let armed = false;
  let release = (): void => {};
  const gate = new Promise<void>((resolve) => (release = resolve));
  // The chunks of the first load are requested before the document is ready; the window module is requested after the list has arrived.
  page.on("domcontentloaded", () => (armed = true));
  await page.route(/\/chunk-[A-Z0-9]+\.js$/, async (route) => {
    if (armed) await gate; // the window module stays pending until the test lets it through
    await route.continue();
  });
  await mockApp(page, { auth: "in", dataset: "dense" });
  await page.goto("/missions"); // not openApp: it waits for the network to be idle, which includes the delayed module
  await expect(page.locator('[data-section="active"] .mrow').first()).toBeVisible();
  // Both counts are taken in one step, so the window module cannot arrive between them.
  const snapshot = () => page.evaluate(() => [document.querySelectorAll('[data-section="active"] .mrow').length, document.querySelectorAll('[data-section="active"] .mgroup').length]);
  expect(await snapshot()).toEqual([400, 0]);
  release();
  await expect(page.locator('[data-section="active"] .mgroup').first()).toBeVisible();
  await expect.poll(async () => (await snapshot())[0]).toBeLessThanOrEqual(60);
});
