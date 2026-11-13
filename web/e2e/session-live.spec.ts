// PRD-015: the session view shows model, the task, the elapsed time of the run, the last output line and the turn history, and offers search and copy
// in the scrollback. Only fields the worker already returns are used (see views/mission/facts.ts).
import { durationText, sessionFacts, turnLine, turnsFromEvents } from "../src/views/mission/facts";
import { findMatches, matchLabel, stepMatch } from "../src/transcript-search";
import { expect, test } from "./fixtures";
import { MISSION_URL, openApp } from "./redesign/harness";

// ---- pure functions

test("findMatches and stepMatch: case-insensitive, non-overlapping, wrapping", () => {
  expect(findMatches("Abc abc ABC", "abc").map((r) => r.start)).toEqual([0, 4, 8]);
  expect(findMatches("aaaa", "aa").map((r) => [r.start, r.end])).toEqual([[0, 2], [2, 4]]);
  expect(findMatches("text", "")).toEqual([]);
  expect(stepMatch(2, 1, 3)).toBe(0);
  expect(stepMatch(0, -1, 3)).toBe(2);
  expect(stepMatch(0, 1, 0)).toBe(0);
  expect(matchLabel("x", 1, 4)).toBe("2 of 4");
  expect(matchLabel("x", 0, 0)).toBe("No matches");
  expect(matchLabel("", 0, 0)).toBe("");
});

test("findMatches keeps offsets in the original text when case folding changes a character's length", () => {
  const text = "İx İx \u{1F600}x";
  const found = findMatches(text, "x");
  expect(found.map((r) => text.slice(r.start, r.end))).toEqual(["x", "x", "x"]);
  expect(found.map((r) => r.start)).toEqual([1, 4, 8]);
  expect(findMatches("ÄÖ äö", "äö").map((r) => r.start)).toEqual([0, 3]);
});

test("sessionFacts reads provider, model, task and turns from the projection and leaves out what is not reported", () => {
  const f = sessionFacts({
    station_observation: { station: "codex", model: "gpt-5" },
    current_task: { ordinal: 2, title: " Tokenizer " },
    operations: [
      { ordinal: 1, station: "codex", model: "gpt-5", status: "EXITED", exit_code: 0, created_at: "2026-11-05T10:00:00Z", updated_at: "2026-11-05T10:20:00Z" },
      { ordinal: 2, station: "codex", model: "unknown", status: "RUNNING", exit_code: null, created_at: "2026-11-05T11:30:00Z", updated_at: "2026-11-05T11:30:00Z" },
    ],
  });
  expect([f.provider, f.model, f.task?.title]).toEqual(["codex", "gpt-5", "Tokenizer"]);
  expect(f.turns.map((t) => t.ordinal)).toEqual([2, 1]);
  expect(f.runningSinceMs).toBe(Date.parse("2026-11-05T11:30:00Z"));
  expect(turnLine(f.turns[1]!, 0)).toBe("Task 1, codex gpt-5, exited 0, 20m");
  expect(turnLine(f.turns[0]!, Date.parse("2026-11-05T12:00:00Z"))).toBe("Task 2, codex, running, 30m");
  expect(sessionFacts(null)).toEqual({ provider: "", model: "", task: null, runningSinceMs: null, turns: [] });
  expect(durationText(0, 65 * 60000)).toBe("1h 05m");
});

test("turnsFromEvents pairs each exit with the open run of its task, newest first", () => {
  const ev = (kind: string, task_id: number, at: string, data: Record<string, unknown>) => ({ kind, task_id, created_at: `2026-11-05T${at}:00Z`, data });
  const turns = turnsFromEvents(
    [ev("operation-exited", 1, "10:20", { exit_code: 0 }), ev("operation-running", 1, "10:00", { route_id: "codex", model: "gpt-5" }), ev("operation-running", 2, "11:30", { route_id: "claude" }), ev("controller-heartbeat", 2, "11:31", {})],
    (id) => id,
  );
  expect(turns.map((t) => [t.ordinal, t.provider, t.outcome])).toEqual([[2, "claude", "running"], [1, "codex", "exited 0"]]);
  expect(turns[1]!.endedMs).toBe(Date.parse("2026-11-05T10:20:00Z"));
});

// ---- in the page

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const original = window.fetch.bind(window);
    const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    window.fetch = async (input, init) => {
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.href);
      const method = init?.method ?? (input instanceof Request ? input.method : "GET");
      if (method === "POST" && url.pathname === "/api/v1/commands") {
        const sent = JSON.parse(String(init?.body ?? "{}")) as { request?: { operation?: string } };
        if (sent.request?.operation === "mission.activity_line")
          return json({ request_id: "x", target: "alpha", operation: "mission.activity_line", state: "done", result: { outcome: "ok", result: { available: true, line: "Running cargo test", provider: "codex", task: 2 }, error: null } });
      }
      const res = await original(input, init);
      return res;
    };
  });
});

test("the Session details show provider, model, task, elapsed time, last output and the run history without asking the worker for the status", async ({ page }) => {
  const app = await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
  const box = page.locator("#session-details");
  await expect(box.locator('[data-q="session-glance"]')).toHaveText("codex gpt-5 · task 2 · 52m");
  await box.locator("summary").click();
  await expect(box).toContainText("Codex");
  await expect(box.locator("dd", { hasText: "gpt-5" })).toBeVisible();
  await expect(box).toContainText("Implement the new tokenizer");
  await expect(box.locator("dt", { hasText: "Running for" })).toBeVisible();
  await expect(box.locator('[data-q="last-line"]')).toHaveText("Running cargo test");
  const turns = box.locator(".session-turns li");
  await expect(turns).toHaveCount(1);
  await expect(turns.first()).toHaveText("Task 2, codex gpt-5, running, 52m");
  expect(app.requestCounts().status, "the mission page never asks the worker for the status").toBe(0);
});

async function openSearch(page: import("@playwright/test").Page): Promise<void> {
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
  await expect(page.locator("#terminal-host .xterm-rows")).toContainText("Reading src/tokenizer.rs");
  await page.locator("#session-menu").click();
  await page.getByRole("menuitem", { name: "Search scrollback" }).click();
}

test("Search scrollback marks every match, steps through them and wraps", async ({ page }) => {
  await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
  await openSearch(page);
  const input = page.getByLabel("Search scrollback", { exact: true }).first();
  await expect(input).toBeFocused();
  await input.fill("src/tokenizer.rs");
  const marks = page.locator("pre.transcript mark");
  await expect(marks).toHaveCount(2);
  await expect(page.locator(".transcript-count")).toHaveText("1 of 2");
  await expect(marks.first()).toHaveClass(/current/);
  await page.locator('[data-action="search-next"]').click();
  await expect(page.locator(".transcript-count")).toHaveText("2 of 2");
  await expect(marks.last()).toHaveClass(/current/);
  await input.press("Enter");
  await expect(page.locator(".transcript-count")).toHaveText("1 of 2");
  await input.fill("no such text");
  await expect(page.locator(".transcript-count")).toHaveText("No matches");
  await expect(marks).toHaveCount(0);
});

test("Copy scrollback puts the scrollback text on the clipboard", async ({ page, context, browserName }) => {
  test.skip(browserName !== "chromium", "clipboard permissions are a Chromium feature");
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
  await openSearch(page);
  await page.locator('[data-action="copy-scrollback"]').click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain("128 passed, 0 failed");
});

test("Search rebuilds the transcript once typing pauses, not on every key", async ({ page }) => {
  await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
  await openSearch(page);
  const input = page.getByLabel("Search scrollback", { exact: true }).first();
  await page.evaluate(() => {
    const w = window as unknown as { __rebuilds: number };
    w.__rebuilds = 0;
    new MutationObserver((list) => (w.__rebuilds += list.length)).observe(document.querySelector("pre.transcript")!, { childList: true });
  });
  await input.pressSequentially("tokenizer", { delay: 0 });
  await expect(page.locator("pre.transcript mark").first()).toBeVisible();
  await page.waitForTimeout(300);
  expect(await page.evaluate(() => (window as unknown as { __rebuilds: number }).__rebuilds), "transcript rebuilds for nine keys; without the delay there are nine").toBeLessThanOrEqual(4);
});
