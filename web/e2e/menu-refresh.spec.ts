// DOG-028: a live refresh never replaces or closes an open overflow menu or its items.
//
// The session pane rebuilt its "Session options" menu whenever the connection state changed, so a menu that was open (or
// being clicked) was detached and the click on its item was lost (terminal.spec.ts, firefox, "#terminal-close ... detached").
// Each test opens one menu, forces refreshes of the view that owns it while the menu is open (waiting for each refresh to
// show, never a fixed sleep), and requires that the same menu and item elements are still attached, the menu is still open,
// and a click on the item works.
import type { Locator, Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { MISSION_PATH, MISSIONS, mockApp, type Mock, type MockOptions } from "./mock";
import { showTab } from "./redesign/harness";

const IN: MockOptions = { auth: "in", digest: true };
const RUNNING = MISSIONS[0]!.mission.id;
const ROW = `[data-entity="mission:alpha/${RUNNING}"]`;
const notice = (id: string) => ({ type: "event", worker: "alpha", cursor: 41, count: 1, first_event_id: 41, last_event_id: 41, missions: [{ id, events: 1, last_event_id: 41, summary: false }] });
const connected = (page: Page, handle: Awaited<ReturnType<Locator["elementHandle"]>>) => page.evaluate((el) => (el as Node).isConnected, handle);

/** Open the menu of `trigger`, run `refreshes` (each returns when its refresh is on screen), then check the menu and click `item`. */
async function holdOpen(page: Page, trigger: Locator, name: string, refreshes: Array<() => Promise<void>>, item = '[role="menuitem"]:not(:disabled)'): Promise<void> {
  await trigger.scrollIntoViewIfNeeded();
  await trigger.click();
  const menu = page.getByRole("menu", { name });
  const entry = menu.locator(item).first();
  await expect(entry).toBeVisible();
  const menuHandle = await menu.elementHandle();
  const entryHandle = await entry.elementHandle();
  for (const refresh of refreshes) {
    await refresh();
    expect(await connected(page, menuHandle), "the open menu is still attached after a refresh").toBe(true);
    expect(await connected(page, entryHandle), "its item is still attached after a refresh").toBe(true);
    await expect(trigger).toHaveAttribute("aria-expanded", "true");
  }
  await entry.click();
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
}

/** Refreshes of a mission: rename it and announce it, then wait until `shown` has the new name. */
const renames = (mock: Mock, shown: Locator): Array<() => Promise<void>> =>
  ["Refactor the parser (a)", "Refactor the parser (b)", "Refactor the parser (c)"].map((name) => async () => {
    mock.patchMission(RUNNING, { name });
    mock.emitStream(notice(RUNNING));
    await expect(shown).toContainText(name);
  });

test("Mission header: the open More mission actions menu survives refreshes of the header", async ({ page }) => {
  const mock = await mockApp(page, IN);
  await page.goto(MISSION_PATH);
  await holdOpen(page, page.getByRole("button", { name: "More mission actions" }), "More mission actions", renames(mock, page.locator(".mission-head h1")), '[role="menuitem"]:has-text("Copy mission ID")');
});

test("Mission tasks: an open task row menu survives refreshes of the task list", async ({ page }) => {
  const mock = await mockApp(page, IN);
  mock.setTasks(RUNNING, ["COMPLETED", "PAUSED", "QUEUED"]);
  await page.goto(MISSION_PATH);
  await showTab(page, "Tasks");
  const trigger = page.locator('[data-entity^="task:"] [aria-haspopup="menu"]').first();
  await expect(trigger).toBeVisible();
  const name = await trigger.getAttribute("aria-label");
  const reads = (): number => mock.requestCounts().tasks;
  const refreshes = [0, 1, 2].map((n) => async () => {
    const before = reads();
    mock.setTasks(RUNNING, ["COMPLETED", "PAUSED", n % 2 ? "QUEUED" : "READY"]);
    mock.pushEvent({ kind: "task-status", task_id: 3, mission_id: RUNNING, data: { from: "QUEUED", to: "READY" } });
    await expect.poll(reads, { message: "the page read the task list again", timeout: 10_000 }).toBeGreaterThan(before);
  });
  await holdOpen(page, trigger, name!, refreshes);
});

for (const [title, path] of [["Missions", "/missions"], ["Inbox", "/"]] as const) {
  test(`${title}: an open row menu survives refreshes of the list`, async ({ page }) => {
    const mock = await mockApp(page, IN);
    await page.goto(path);
    const row = page.locator(ROW).first();
    const trigger = row.locator('[aria-haspopup="menu"]');
    await expect(trigger).toBeVisible();
    await holdOpen(page, trigger, (await trigger.getAttribute("aria-label"))!, renames(mock, row), '[role="menuitem"]:has-text("Copy ID")');
  });
}

test("Workers: an open worker menu survives refreshes of the list", async ({ page }) => {
  const mock = await mockApp(page, IN);
  await page.goto("/workers");
  const trigger = page.locator('[data-worker="alpha"] [aria-haspopup="menu"]');
  await expect(trigger).toBeVisible();
  const reads = (): number => mock.requests().filter((r) => r.method === "GET" && r.path === "/workers").length;
  const refreshes = [0, 1, 2].map(() => async () => {
    const before = reads();
    mock.emitStream({ type: "worker", worker: "alpha", state: "online" });
    await expect.poll(reads, { message: "the page read the worker list again", timeout: 10_000 }).toBeGreaterThan(before);
  });
  await holdOpen(page, trigger, (await trigger.getAttribute("aria-label"))!, refreshes, '[role="menuitem"]:has-text("Copy ID")');
});

test("Session pane: the open Session options menu survives connection state changes", async ({ page }) => {
  const mock = await mockApp(page, { ...IN, terminalScreen: true });
  await page.goto(MISSION_PATH);
  const status = page.locator("#terminal-status");
  await expect(status).toHaveAttribute("data-state", "live");
  await page.locator("#terminal-control").click();
  await expect(status).toHaveAttribute("data-mode", "write");
  // Each dropped connection changes the state twice (reconnecting, live again) and therefore the items of the menu.
  const drops = [0, 1].map(() => async () => {
    mock.closeTerminals();
    await expect(status).not.toHaveAttribute("data-state", "live");
    await expect(status).toHaveAttribute("data-state", "live", { timeout: 20_000 });
  });
  await holdOpen(page, page.locator("#session-menu"), "Session options", drops, "#terminal-close");
  await expect(status).toHaveAttribute("data-state", "idle");
});
