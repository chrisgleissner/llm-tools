// The console asks a worker nothing while it only waits for changes (DOG-021).
//
// The coordinator announces accepted event batches with a small notice (counts per mission, no events).
// The console re-reads the mission list, whose entries carry the replicated digest (task counts, current
// task, station, model), and reads new events from the coordinator. A worker command (the mission status or
// the task list) is sent only when the digest of the open mission changed. The mission page draws its
// status from the digest and sends no status command at all; a changed digest reads the task list once.
import { expect, test } from "./fixtures";
import { mockApp, MISSION_PATH, MISSIONS, type MockOptions } from "./mock";

const IN: MockOptions = { auth: "in", digest: true };
const RUNNING = MISSIONS[0]!.mission.id;
const notice = (missions: Array<{ id: string; events: number }>) => ({
  type: "event", worker: "alpha", cursor: 41, count: missions.length, first_event_id: 41, last_event_id: 41,
  missions: missions.map((m) => ({ ...m, last_event_id: 41, summary: false })),
});

const ROW = `[data-entity="mission:alpha/${RUNNING}"]`;

test("the Inbox draws attention and running work from the digest and sends no status command", async ({ page }) => {
  const mock = await mockApp(page, IN);
  await page.goto("/");
  await expect(page.locator(`[data-section="needs-you"] ${ROW}`)).toContainText("awaits your review");
  await expect(page.locator(`[data-section="needs-you"] ${ROW} .row-meta`)).toContainText("Codex gpt-5");
  for (let i = 0; i < 4; i += 1) mock.emitStream(notice([{ id: RUNNING, events: 1 }]));
  await page.waitForTimeout(2500); // the console's debounce is 1.2 s
  expect(mock.requestCounts().status, "an event notice must not make the Inbox ask the worker for mission status").toBe(0);
});

test("the mission list shows what each running mission is doing from the digest", async ({ page }) => {
  const mock = await mockApp(page, IN);
  await page.goto("/missions");
  await expect(page.locator(`${ROW} [data-cell="provider"]`)).toHaveText("Codex gpt-5");
  await expect(page.locator(`${ROW} [data-cell="progress"]`)).toHaveText("1/6");
  expect(mock.requestCounts().status).toBe(0);
});

test("a worker that sends no digest is asked for the status of each active mission", async ({ page }) => {
  const mock = await mockApp(page, { ...IN, digest: false, attention: false });
  await page.goto("/");
  await expect(page.locator(`[data-section="needs-you"] ${ROW}`)).toContainText("awaits your review");
  expect(mock.requestCounts().status, "the fallback reads the status of the active missions").toBeGreaterThan(0);
});

test("a notice for the open mission reads events from the coordinator, not tasks or status from the worker", async ({ page }) => {
  const mock = await mockApp(page, IN);
  await page.goto(MISSION_PATH);
  await expect(page.locator('[data-entity^="task:"]').first()).toBeAttached();
  await page.waitForTimeout(600);
  const before = mock.requestCounts();
  for (let i = 0; i < 3; i += 1) mock.emitStream(notice([{ id: RUNNING, events: 1 }]));
  await expect.poll(() => mock.requestCounts().events).toBeGreaterThan(before.events);
  await page.waitForTimeout(1500);
  const after = mock.requestCounts();
  expect(after.status, "the mission page never asks the worker for the status").toBe(0);
  expect(after.tasks).toBe(before.tasks);
});

test("a changed digest of the open mission reads its tasks once and shows the new state without a status read", async ({ page }) => {
  const mock = await mockApp(page, IN);
  await page.goto(MISSION_PATH);
  const meta = page.locator('[data-l0="mission.header"] .head-meta');
  await expect(meta).toContainText("Codex gpt-5");
  await page.waitForTimeout(600);
  const before = mock.requestCounts();
  mock.patchMission(RUNNING, { station: "claude", model: "opus" });
  mock.emitStream(notice([{ id: RUNNING, events: 1 }]));
  await expect.poll(() => mock.requestCounts().tasks).toBe(before.tasks + 1);
  await expect(meta).toContainText("Claude opus");
  await page.waitForTimeout(1500);
  expect(mock.requestCounts().tasks).toBe(before.tasks + 1);
  expect(mock.requestCounts().status, "the status is drawn from the digest").toBe(0);
});

// A button that is replaced between the mouse-down and the mouse-up of a click never receives the click (seen on Firefox).
// After a task action the coordinator relays several messages: the reply, a change notice per event and the changed digest.
// The tasks panel patches the rows it has: after a change of one task no row element and no button is replaced, the changed row
// shows the new state in the element it had, and notices that bring the same tasks change nothing in the DOM.
test("the tasks panel keeps its row and button elements when the task data changes", async ({ page }) => {
  const mock = await mockApp(page, IN);
  mock.setTasks(RUNNING, ["COMPLETED", "PAUSED", "QUEUED"]);
  await page.goto(MISSION_PATH);
  const row = (n: number) => page.locator('[data-entity^="task:"]', { hasText: `Task number ${n}` });
  await expect(row(3)).toContainText("Queued");
  await page.waitForTimeout(600);
  await page.evaluate(() => {
    const w = window as unknown as { __rows: Element[]; __added: number; __mutations: number };
    const box = document.querySelector(".task-list-box") as HTMLElement;
    // Remember every row and every button of the list; count the row elements and buttons that are added afterwards.
    w.__rows = [...box.querySelectorAll('[data-entity^="task:"], button')];
    w.__added = 0;
    w.__mutations = 0;
    new MutationObserver((records) => {
      for (const r of records) {
        w.__mutations += 1;
        for (const n of r.addedNodes) if (n instanceof Element && (n.matches('[data-entity^="task:"], button') || n.querySelector('[data-entity^="task:"], button'))) w.__added += 1;
      }
    }).observe(box, { childList: true, subtree: true, attributes: true, characterData: true });
  });
  const state = () => page.evaluate(() => {
    const w = window as unknown as { __rows: Element[]; __added: number; __mutations: number };
    const box = document.querySelector(".task-list-box") as HTMLElement;
    const now = [...box.querySelectorAll('[data-entity^="task:"], button')];
    return { added: w.__added, mutations: w.__mutations, kept: w.__rows.filter((el) => now.includes(el)).length, before: w.__rows.length };
  });
  // Task 3 starts running: its row says so, in the same element.
  mock.setTasks(RUNNING, ["COMPLETED", "PAUSED", "RUNNING"]);
  mock.pushEvent({ kind: "task-status", task_id: 3, mission_id: RUNNING, data: { from: "QUEUED", to: "READY" } });
  await expect(row(3)).toContainText("Running");
  let seen = await state();
  expect(seen.added, "no row element or button was added again").toBe(0);
  expect(seen.kept, "every row and button that was there is still there").toBe(seen.before);
  // Notices that bring the same tasks change nothing at all.
  const settled = seen.mutations;
  for (let i = 0; i < 4; i += 1) {
    mock.pushEvent({ kind: "task-status", task_id: 3, mission_id: RUNNING, data: { from: "QUEUED", to: "READY" } });
    await page.waitForTimeout(350);
  }
  seen = await state();
  expect(seen.mutations, "notices with unchanged tasks changed the DOM").toBe(settled);
  expect(seen.kept).toBe(seen.before);
});
