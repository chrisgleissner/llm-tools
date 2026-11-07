// Dashboard, lists, toasts, dialogs and shortcuts (WEB-003, WEB-004, WEB-005, WEB-006, WEB-010, WEB-011).
import { expect, expectAccessible, expectNoHorizontalScroll, navTo, setupViaUi, test } from "./fixtures";
import { havePython, startSeededWorker } from "./seed";

test.describe("workers page", () => {
  test("adding a worker shows its token once; removing one asks for confirmation", async ({ page, coord }) => {
    await setupViaUi(page, coord);
    await navTo(page, "Workers");
    await expect(page.getByRole("heading", { name: "No workers enrolled" })).toBeVisible();
    await page.locator("#add-worker-button").click();
    const dialog = page.getByRole("dialog", { name: "Add a worker" });
    await dialog.getByLabel("Worker name").fill("Bad Name");
    await dialog.getByRole("button", { name: "Add worker" }).click();
    await expect(dialog.locator("#worker-name-error")).toContainText("lowercase letters");
    await dialog.getByLabel("Worker name").fill("build-1");
    await dialog.getByRole("button", { name: "Add worker" }).click();
    const result = page.getByRole("dialog", { name: "Worker added" });
    await expect(result.locator("#worker-token")).toHaveText(/^[A-Za-z0-9_-]{43}$/);
    await expect(result).toContainText("llm-bridge worker enroll --name build-1");
    await expect(page.locator(".toast.applied")).toContainText("Worker build-1 added");
    await expectAccessible(page, "worker added dialog");
    await result.getByRole("button", { name: "Done" }).click();
    const row = page.locator('tr[data-worker="build-1"]');
    await expect(row.locator('[data-label="State"]')).toContainText("offline");
    // Duplicate names are refused inline.
    await page.locator("#add-worker-button").click();
    await page.getByRole("dialog", { name: "Add a worker" }).getByLabel("Worker name").fill("build-1");
    await page.getByRole("dialog", { name: "Add a worker" }).getByRole("button", { name: "Add worker" }).click();
    await expect(page.locator("#worker-name-error")).toContainText("already exists");
    await page.keyboard.press("Escape");
    // Removing needs confirmation; declining changes nothing.
    await row.getByRole("button", { name: "Remove worker build-1" }).click();
    const confirm = page.getByRole("dialog");
    await expect(confirm).toContainText("Remove worker build-1?");
    await confirm.getByRole("button", { name: "Go back" }).click();
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "Remove worker build-1" }).click();
    await page.locator("#dialog-confirm").click();
    await expect(page.locator(".toast.applied", { hasText: "removed" })).toContainText("Worker build-1 removed");
    await expect(page.getByRole("heading", { name: "No workers enrolled" })).toBeVisible();
  });
});

test.describe("keyboard shortcuts", () => {
  test("a help overlay lists them; g-sequences navigate; / focuses search; t changes the theme", async ({ page, coord, isMobile }) => {
    test.skip(!!isMobile, "hardware keyboard shortcuts");
    await setupViaUi(page, coord);
    await page.locator("body").press("?");
    const help = page.getByRole("dialog", { name: "Keyboard shortcuts" });
    await expect(help).toContainText("Go to Workers");
    await expectAccessible(page, "shortcut help");
    await page.keyboard.press("Escape");
    await expect(help).toBeHidden();
    await page.locator("body").press("g");
    await page.locator("body").press("w");
    await expect(page).toHaveURL(/\/workers$/);
    await page.locator("body").press("g");
    await page.locator("body").press("m");
    await expect(page).toHaveURL(/\/missions$/);
    // The missions view builds its search box after navigation; on a slow runner "/" can arrive first.
    await expect(page.getByLabel("Search missions")).toBeVisible();
    await page.locator("body").press("/");
    await expect(page.getByLabel("Search missions")).toBeFocused();
    // Typing in a field never triggers a shortcut.
    await page.keyboard.type("gw?");
    await expect(page).toHaveURL(/\/missions/);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await page.locator("h1").click();
    await page.locator("body").press("t");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    // Via the account menu too.
    await page.locator("#account-button").click();
    await page.locator("#menu-shortcuts").click();
    await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
  });
});

test.describe("with a seeded worker", () => {
  test.skip(!havePython, "repo .venv python is missing");

  test("overview shows live counts, attention items and recent activity; missions can be searched, filtered, sorted and paged", async ({ page, coord }) => {
    const w = await startSeededWorker(coord, { missions: 30 });
    try {
      await setupViaUi(page, coord);
      await expect(page.locator('[data-tile="workers-online"] .tile-value')).toHaveText("1/1", { timeout: 20_000 });
      await expect(page.locator('[data-tile="missions-running"] .tile-value')).toHaveText("1");
      // Attention: a task waits for approval, one failed, one blocked, and a mission failed.
      const attention = page.locator("#attention");
      await expect(attention.locator('[data-attention="approval"]')).toContainText("1 task waiting for approval", { timeout: 45_000 });
      await expect(attention.locator('[data-attention="failed-task"]')).toContainText("1 failed task");
      await expect(attention.locator('[data-attention="blocked-task"]')).toContainText("1 blocked task");
      await expect(attention.locator('[data-attention="failed-mission"]')).toContainText("failed");
      await expect(page.locator('[data-tile="awaiting-approval"] .tile-value')).toHaveText("1");
      await expect(page.locator('[data-tile="failed-tasks"] .tile-value')).toHaveText("1");
      await expectAccessible(page, "overview with attention items");
      await expectNoHorizontalScroll(page);

      // Missions: paged (25 per page), searchable, filterable, sortable; the state is in the URL.
      await navTo(page, "Missions");
      await expect(page.locator("#missions-table tbody tr")).toHaveCount(25, { timeout: 20_000 });
      await expect(page.locator(".pager-count")).toHaveText("1-25 of 30");
      await page.getByRole("button", { name: "Next" }).click();
      await expect(page.locator("#missions-table tbody tr")).toHaveCount(5);
      await expect(page).toHaveURL(/page=2/);
      await page.getByRole("button", { name: "Previous" }).click();
      await page.getByLabel("Search missions").fill("docs sweep 04");
      await expect(page.locator("#missions-table tbody tr")).toHaveCount(1);
      await expect(page.getByLabel("Search missions"), "typing keeps focus while results arrive").toBeFocused();
      await expect(page.locator("#missions-table tbody tr")).toContainText("Docs sweep 04");
      await expect(page).toHaveURL(/q=docs/);
      await page.getByLabel("Search missions").fill("zzz-no-such-mission");
      await expect(page.getByRole("heading", { name: "No missions match" })).toBeVisible();
      await page.locator("#clear-filters").click();
      await expect(page.locator("#missions-table tbody tr")).toHaveCount(25);
      // Status filter chips come with counts.
      const failedChip = page.locator('#missions-controls [data-status="FAILED"]');
      await expect(failedChip).toContainText("1");
      await failedChip.click();
      await expect(page.locator("#missions-table tbody tr")).toHaveCount(1);
      await expect(failedChip).toHaveAttribute("aria-pressed", "true");
      await failedChip.click();
      // Sorting by name, both directions, through the control and the column header.
      await page.locator("#sort-select").selectOption("name");
      await expect(page.locator("#missions-table tbody tr").first()).toContainText("Docs sweep 01");
      await page.locator("#sort-order").click();
      await expect(page.locator("#missions-table tbody tr").first()).toContainText("Release prep 29");
      await expect(page.locator('#missions-table th[aria-sort="descending"]')).toHaveCount(1);
      // Relative and absolute timestamps, and copy buttons for IDs.
      const first = page.locator("#missions-table tbody tr").first();
      await expect(first.locator("time .rel")).toHaveText(/ago|just now|never/);
      await expect(first.locator("time .abs")).toHaveText(/^\d{4}-\d\d-\d\d \d\d:\d\d$/);
      await expectAccessible(page, "missions list");
    } finally {
      w.cleanup();
    }
  });

  test("mission detail: task search, filter, sort, copy ID and outcome toasts", async ({ page, coord, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => {});
    const w = await startSeededWorker(coord, { missions: 1 });
    try {
      await setupViaUi(page, coord);
      await navTo(page, "Missions");
      await page.getByRole("link", { name: /Parser refactor 00/ }).click();
      await expect(page.locator("#tasks-table tbody tr")).toHaveCount(5, { timeout: 20_000 });
      // Filter by status chip and search by title.
      await page.locator('#mission-tasks [data-status="VERIFYING"]').click();
      await expect(page.locator("#tasks-table tbody tr")).toHaveCount(1);
      await expect(page.locator("#tasks-table tbody tr")).toContainText("Implement it");
      await page.locator('#mission-tasks [data-status="VERIFYING"]').click();
      await page.getByLabel("Search tasks").fill("ship");
      await expect(page.locator("#tasks-table tbody tr")).toHaveCount(1);
      await page.getByLabel("Search tasks").fill("");
      await expect(page.locator("#tasks-table tbody tr")).toHaveCount(5);
      await page.locator("#task-sort").selectOption("title");
      await expect(page.locator("#tasks-table tbody tr").first()).toContainText("Implement it");
      // Copy the mission ID: the clipboard holds the full ID and a toast says so.
      await page.locator(".page-head").getByRole("button", { name: "Copy mission ID" }).click();
      await expect(page.locator(".toast").last()).toContainText("Copied mission ID");
      const id = await page.evaluate(() => navigator.clipboard.readText()).catch(() => "");
      if (id) expect(id).toMatch(/^[0-9a-f]{32}$/);
      // Rejecting a task asks for a reason, then reports the outcome in a toast that names it.
      const row = page.locator("#tasks-table tbody tr", { hasText: "Implement it" });
      await row.getByRole("button", { name: "Reject task 2" }).click();
      const dlg = page.getByRole("dialog");
      await expect(dlg).toBeVisible();
      await dlg.locator("#dialog-confirm").click(); // no reason: refused, dialog stays
      await expect(dlg).toBeVisible();
      await dlg.getByLabel("Reason").fill("needs another pass");
      await dlg.locator("#dialog-confirm").click();
      const toast = page.locator(".toast").filter({ hasText: "Reject task 2" });
      await expect(toast).toHaveAttribute("data-kind", /applied|rejected|accepted/, { timeout: 20_000 });
      await expect(toast).toContainText(/Applied|Rejected|Accepted/);
      await expect(toast).toContainText("Reject task 2");
    } finally {
      w.cleanup();
    }
  });
});

test("mission activity humanizes, filters, and collapses system events", async ({ page }) => {
  const { mockApp, MISSION_PATH } = await import("./mock");
  const mock = await mockApp(page, { auth: "in", terminalClose: true });
  await page.goto("/");
  const overviewActivity = page.locator("#activity");
  await expect(overviewActivity).toContainText("Mission created");
  await expect(overviewActivity).toContainText("Command queued: task.verify");
  await expect(overviewActivity).toContainText("Selected codex (gpt-5)");
  await expect(overviewActivity).not.toContainText("Controller heartbeat");
  await page.getByRole("button", { name: /Show \d+ system events/ }).click();
  await expect(overviewActivity).toContainText("controller alive");

  await page.goto(MISSION_PATH);
  const events = page.locator("#events-list");
  await expect(events).toContainText("Terminal opened");
  await expect(events).toContainText("Terminal closed · Terminal closed by test");
  await expect(events).toContainText("Mission created");
  await expect(events).toContainText("Task updated · Task 2 · RUNNING → VERIFYING");
  await expect(events).not.toContainText("do-not-render-this");
  await expect(events).toContainText("Command queued: task.verify");
  await expect(events).toContainText("Selected codex (gpt-5)");
  await expect(events.locator('a[href="/api/v1/commands/safe-request-01"]')).toHaveText("Command details");
  await expect(events).not.toContainText("controller alive");
  await page.getByRole("button", { name: /Show \d+ system events/ }).click();
  await expect(events).toContainText("controller alive");

  await page.getByLabel("Filter events").selectOption("provider");
  await expect(events).toContainText("Started provider · codex · Task 2 · gpt-5");
  await expect(events).not.toContainText("Task status changed");
  await page.getByLabel("Filter events").selectOption("all");
  await page.getByLabel("Filter by event kind").selectOption("task-status");
  await expect(events).toContainText("Task updated · Task 2 · RUNNING → VERIFYING");
  await expect(events).not.toContainText("Write a regression test");
  await page.getByLabel("Filter by event kind").selectOption("task-added");
  await expect(events).toContainText("Task added · Task 3 · Write a regression test");
  await expect(events).not.toContainText("RUNNING → VERIFYING");
  await page.getByLabel("Filter by event kind").selectOption("all");
  mock.emitStream({ type: "worker", worker: "alpha", state: "offline" });
  await expect(events).toContainText("Worker offline · alpha");
  await page.getByLabel("Filter events").selectOption("terminal");
  await expect(events).toContainText("Terminal opened");
  await expect(events).not.toContainText("Worker offline");
  await page.getByLabel("Filter by event kind").selectOption("terminal-closed");
  await expect(events).toContainText("Terminal closed · Terminal closed by test");
  await expect(events).not.toContainText("Terminal opened");
  await page.getByLabel("Filter by event kind").selectOption("all");
  await page.getByLabel("Filter events").selectOption("worker");
  await expect(events).toContainText("Worker offline · alpha");
  await expect(events).not.toContainText("Terminal opened");
  await page.getByLabel("Filter events").selectOption("all");
  await page.getByLabel("Filter by event kind").selectOption("all");
  await page.getByLabel("Filter events").selectOption("task");
  await expect(events).toContainText("Task updated · Task 2 · RUNNING → VERIFYING");
  await expect(events).not.toContainText("Started provider");
});
