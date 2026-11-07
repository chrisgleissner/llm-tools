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

test("the overview draws attention and running work from the digest and sends no status command", async ({ page }) => {
  const mock = await mockApp(page, IN);
  await page.goto("/");
  await expect(page.locator('[data-attention="approval"]').first()).toContainText("waiting for approval");
  await expect(page.locator(".running-list")).toContainText("Task 2 · Implement the new tokenizer · codex · gpt-5");
  for (let i = 0; i < 4; i += 1) mock.emitStream(notice([{ id: RUNNING, events: 1 }]));
  await page.waitForTimeout(2500); // the console's debounce is 1.2 s
  expect(mock.requestCounts().status, "an event notice must not make the overview ask the worker for mission status").toBe(0);
});

test("the mission list shows what each running mission is doing from the digest", async ({ page }) => {
  const mock = await mockApp(page, IN);
  await page.goto("/missions");
  await expect(page.locator(".now-doing").first()).toContainText("Task 2 · Implement the new tokenizer · codex · gpt-5");
  expect(mock.requestCounts().status).toBe(0);
});

test("a notice for the open mission reads events from the coordinator, not tasks or status from the worker", async ({ page }) => {
  const mock = await mockApp(page, IN);
  await page.goto(MISSION_PATH);
  await expect(page.locator("#tasks-table tbody tr")).toHaveCount(6);
  await expect(page.locator("#mission-status")).toContainText("RUNNING");
  await page.waitForTimeout(600);
  const before = mock.requestCounts();
  for (let i = 0; i < 3; i += 1) mock.emitStream(notice([{ id: RUNNING, events: 1 }]));
  await expect.poll(() => mock.requestCounts().events).toBeGreaterThan(before.events);
  await page.waitForTimeout(1500);
  const after = mock.requestCounts();
  expect(after.status, "the mission page never asks the worker for the status").toBe(0);
  expect(after.tasks).toBe(before.tasks);
});

test("a changed digest of the open mission reads its tasks once and shows the new counts without a status read", async ({ page }) => {
  const mock = await mockApp(page, IN);
  await page.goto(MISSION_PATH);
  await expect(page.locator("#tasks-table tbody tr")).toHaveCount(6);
  await page.waitForTimeout(600);
  const before = mock.requestCounts();
  mock.patchMission(RUNNING, { task_counts: { COMPLETED: 2, VERIFYING: 0, FAILED: 1, BLOCKED: 1, QUEUED: 1, PAUSED: 1 } });
  mock.emitStream(notice([{ id: RUNNING, events: 1 }]));
  await expect.poll(() => mock.requestCounts().tasks).toBe(before.tasks + 1);
  await expect(page.locator("#mission-status")).toContainText("completed 2");
  await page.waitForTimeout(1500);
  expect(mock.requestCounts().tasks).toBe(before.tasks + 1);
  expect(mock.requestCounts().status, "the status is drawn from the digest").toBe(0);
});
