// DOG-026: a live refresh never replaces a row element that the pointer or the keyboard focus is on.
//
// A click needs the pointer-down and the pointer-up on the same element. A refresh that rebuilt the rows between them
// lost the click (Firefox and Chromium alike; PR #70 found it through worker.spec.ts). Each test presses the primary
// button on a control inside a row of a live list, forces redraws of that list while the button is held (every 50 ms,
// with a change in the data of the list), and releases it after at least six redraws. The control must still react.
import type { Locator, Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { mockApp, MISSION_PATH, MISSIONS, type Mock, type MockOptions } from "./mock";

const IN: MockOptions = { auth: "in", digest: true };
const RUNNING = MISSIONS[0]!.mission.id;
const ROW = `[data-entity="mission:alpha/${RUNNING}"]`;
const notice = (id: string, tasks = false) => ({
  type: "event", worker: "alpha", cursor: 41, count: 1, first_event_id: 41, last_event_id: 41,
  missions: [{ id, events: 1, last_event_id: 41, summary: false }], ...(tasks ? { task_missions: [id] } : {}),
});

/** Hold the button on `target`, run `redraw` six times `gap` ms apart, then release: the click must reach the target. */
async function pressAcrossRedraws(page: Page, target: Locator, redraw: (n: number) => Promise<void> | void, gap = 50): Promise<void> {
  await target.scrollIntoViewIfNeeded();
  const box = await target.boundingBox();
  expect(box, "the control has a box").toBeTruthy();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  for (let n = 0; n < 6; n += 1) {
    await redraw(n);
    await page.waitForTimeout(gap);
  }
  await page.waitForTimeout(gap * 3);
  await page.mouse.up();
}

const rename = (mock: Mock, n: number) => {
  mock.patchMission(RUNNING, { name: n % 2 ? "Refactor the parser (a)" : "Refactor the parser (b)" });
  mock.emitStream(notice(RUNNING));
};

test("Mission tasks: Resume is clicked although the task list is refreshed between pointer-down and pointer-up", async ({ page }) => {
  const mock = await mockApp(page, IN);
  mock.setTasks(RUNNING, ["COMPLETED", "PAUSED", "QUEUED"]);
  await page.goto(MISSION_PATH);
  const resume = page.locator('[data-entity^="task:"] [data-action="resume"]');
  await expect(resume).toHaveCount(1);
  const reads = mock.requestCounts().tasks;
  // The page waits 150 ms after a change notice before it reads the tasks, so the notices come 200 ms apart to make it read after each.
  await pressAcrossRedraws(page, resume, (n) => {
    mock.setTasks(RUNNING, ["COMPLETED", "PAUSED", n % 2 ? "QUEUED" : "READY"]);
    mock.pushEvent({ kind: "task-status", task_id: 3, mission_id: RUNNING, data: { from: "QUEUED", to: "READY" } });
  }, 200);
  expect(mock.requestCounts().tasks - reads, "the task list was read and redrawn while the button was held").toBeGreaterThanOrEqual(4);
  await expect.poll(() => mock.operations()).toContain("task.resume");
});

test("Inbox: Approve opens its dialog although the row changes between pointer-down and pointer-up", async ({ page }) => {
  const mock = await mockApp(page, IN);
  await page.goto("/");
  const approve = page.locator(`[data-section="needs-you"] ${ROW} [data-action="approve"]`);
  await expect(approve).toBeVisible();
  // The page reads the mission list 400 ms after a notice, so the hold is long enough for several reads.
  await pressAcrossRedraws(page, approve, (n) => rename(mock, n), 150);
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("Missions: the row menu opens although the row changes between pointer-down and pointer-up", async ({ page }) => {
  const mock = await mockApp(page, IN);
  await page.goto("/missions");
  const trigger = page.locator(`${ROW} [aria-haspopup="menu"]`);
  await expect(trigger).toBeVisible();
  await pressAcrossRedraws(page, trigger, (n) => rename(mock, n), 150);
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
});

test("Workers: Details opens although the list is redrawn between pointer-down and pointer-up", async ({ page }) => {
  const mock = await mockApp(page, IN);
  await page.goto("/workers");
  const details = page.locator('[data-worker="alpha"] button.toggle');
  await expect(details).toBeVisible();
  const redraw = () => mock.emitStream({ type: "worker", worker: "alpha", state: "online" });
  await pressAcrossRedraws(page, details, redraw);
  await expect(details).toHaveAttribute("aria-expanded", "true");
  // The next redraw keeps the opened row open.
  redraw();
  await page.waitForTimeout(300);
  await expect(details).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator('[data-worker="alpha"] .toggle-body')).toBeVisible();
});

test("Sessions: Details opens although the list is redrawn between pointer-down and pointer-up", async ({ page }) => {
  await mockApp(page, IN);
  await page.goto("/sessions");
  const details = page.locator('[data-entity^="session:"] button.toggle').first();
  await expect(details).toBeVisible();
  await pressAcrossRedraws(page, details, () => page.evaluate(() => document.getElementById("view-refresh")?.click()));
  await expect(details).toHaveAttribute("aria-expanded", "true");
});
