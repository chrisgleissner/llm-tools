// Dashboard, lists, toasts, dialogs and shortcuts (WEB-003, WEB-004, WEB-005, WEB-006, WEB-010, WEB-011).
import { expect, expectAccessible, expectNoHorizontalScroll, navTo, setupViaUi, test } from "./fixtures";
import { showTab } from "./redesign/harness";
import { havePython, startSeededWorker } from "./seed";

test("responsive navigation waits for its destination to be shown", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  // The redesign has one navigation element for every width (no More sheet): a link that is not shown yet is waited for, never replaced by another control.
  await page.evaluate(() => {
    const workers = document.querySelector<HTMLAnchorElement>('nav[aria-label="Main"] a[href="/workers"]');
    if (!workers) throw new Error("main navigation controls were not rendered");
    workers.style.visibility = "hidden";
    window.setTimeout(() => { workers.style.visibility = ""; }, 150);
  });
  await navTo(page, "Workers");
  await expect(page.getByRole("heading", { name: "Workers", exact: true })).toBeVisible();
});

test.describe("workers page", () => {
  test("adding a worker shows its token once; removing one asks for confirmation", async ({ page, coord }) => {
    await setupViaUi(page, coord);
    await navTo(page, "Workers");
    await expect(page.getByRole("heading", { name: "No workers enrolled" })).toBeVisible();
    const coordinatorHost = new URL(page.url()).host;
    await expect(page.getByText(`llm-bridge worker enroll --name NAME --coordinator wss://${coordinatorHost}`)).toBeVisible();
    await expect(page.getByRole("link", { name: "Installation and operations guide" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Installation and operations guide" })).toHaveAttribute("href", /operations\.md#worker-service$/);
    await expect(page.getByText("appears in this list with an Online mark")).toBeVisible();
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
    // Escape and the close button do not discard a token that is shown once without asking first.
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog", { name: "Close without saving it?" })).toBeVisible();
    await page.getByRole("dialog", { name: "Close without saving it?" }).getByRole("button", { name: "Go back" }).click();
    await expect(result).toBeVisible();
    await result.getByRole("button", { name: "Done" }).click();
    const row = page.locator('[data-entity="worker:build-1"]');
    await expect(row.locator(".pill")).toContainText(/offline/i);
    // Duplicate names are refused inline.
    await page.locator("#add-worker-button").click();
    await page.getByRole("dialog", { name: "Add a worker" }).getByLabel("Worker name").fill("build-1");
    await page.getByRole("dialog", { name: "Add a worker" }).getByRole("button", { name: "Add worker" }).click();
    await expect(page.locator("#worker-name-error")).toContainText("already exists");
    await page.keyboard.press("Escape");
    // Removing needs confirmation; declining changes nothing.
    await row.getByRole("button", { name: "Actions for worker build-1" }).click();
    await page.getByRole("menuitem", { name: "Remove worker" }).click();
    const confirm = page.getByRole("dialog");
    await expect(confirm).toContainText("Remove worker build-1?");
    await confirm.getByRole("button", { name: "Go back" }).click();
    await expect(row).toBeVisible();
    await row.getByRole("button", { name: "Actions for worker build-1" }).click();
    await page.getByRole("menuitem", { name: "Remove worker" }).click();
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
    await expect(help).toContainText("Go to");
    await expect(help).toContainText("Workers");
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
    await expect(page.getByLabel("Search missions", { exact: true })).toBeVisible();
    await page.locator("body").press("/");
    await expect(page.getByLabel("Search missions", { exact: true })).toBeFocused();
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

  test("the Inbox lists what needs the operator with its actions; missions can be searched, filtered and sorted", async ({ page, coord, browserName, isMobile }) => {
    const w = await startSeededWorker(coord, { missions: 30 });
    try {
      await setupViaUi(page, coord);
      // Needs you: a task waits for approval (with Approve and Reject inline) and a mission or task failed.
      const needs = page.locator('[data-section="needs-you"]');
      const approvalRow = needs.locator('[data-entity^="mission:"]', { has: page.locator('.pill[data-state="needs-approval"]') });
      await expect(approvalRow).toHaveCount(1, { timeout: 45_000 });
      await expect(approvalRow.locator('[data-action="approve"]')).toBeVisible();
      await expect(approvalRow.locator('[data-action="reject"]')).toBeVisible();
      await expect(needs.locator('[data-entity^="mission:"]', { has: page.locator('.pill[data-state="failed"]') }).first()).toBeVisible();
      await expect(page.locator('[data-q="need-me"]')).toContainText(/need/);
      await expectAccessible(page, "inbox with attention items");
      await expectNoHorizontalScroll(page);

      const approvalLink = approvalRow.locator("a.row-title");
      const approvalMission = await approvalLink.getAttribute("href");
      const findStarted = Date.now();
      await approvalLink.click();
      await expect(page.locator(".mission-head h1")).toContainText("Parser refactor 00");
      const findElapsedMs = Date.now() - findStarted;
      if (browserName === "chromium") console.log(`UX task find approval: ${isMobile ? "Pixel 4" : "1440x900"}, one click, ${findElapsedMs} ms`);

      // Missions: one list (no paging below 500), searchable, filterable by state, sortable; the state is in the URL.
      await navTo(page, "Missions");
      const total = page.locator("#missions-total");
      await expect(total).toHaveText("30 missions", { timeout: 20_000 });
      await page.getByLabel("Search missions", { exact: true }).fill("docs sweep 04");
      await expect(total).toHaveText("1 mission");
      await expect(page.getByLabel("Search missions", { exact: true }), "typing keeps focus while results arrive").toBeFocused();
      await expect(page.locator('[data-entity^="mission:"]:visible')).toContainText("Docs sweep 04");
      await expect(page).toHaveURL(/q=docs/);
      await page.getByLabel("Search missions", { exact: true }).fill("zzz-no-such-mission");
      await expect(page.getByRole("heading", { name: "No missions match" })).toBeVisible();
      await page.locator("#clear-filters").click();
      await expect(total).toHaveText("30 missions");
      // State filter: the options carry counts; one failed mission.
      await page.locator("#state-filter").selectOption("failed");
      await expect(page.locator('[data-entity^="mission:"]:visible .pill[data-state="failed"]').first()).toBeVisible();
      await expect(page).toHaveURL(/state=failed/);
      await page.locator("#state-filter").selectOption("");
      // Sorting by name orders the active rows alphabetically.
      await page.locator("#sort-select").selectOption("name");
      const names = await page.locator('[data-section="active"] a.row-title').allInnerTexts();
      expect(names.length).toBeGreaterThan(1);
      expect(names).toEqual([...names].sort((x, y) => x.localeCompare(y)));
      await expectAccessible(page, "missions list");

      expect(approvalMission).toBeTruthy();
      await page.goto(new URL(approvalMission!, page.url()).toString());
      await showTab(page, "Tasks");
      const approvalTask = page.locator('[data-entity^="task:"]', { hasText: "Implement it" });
      await expect(approvalTask).toBeVisible();
      const approveStarted = Date.now();
      // One place for the primary action (UXR-048): the header of the mission offers Approve for the task it is about, its row has no copy.
      await expect(approvalTask.getByRole("button", { name: "Approve task 2" })).toHaveCount(0);
      await page.locator('[data-l0="mission.header"]').getByRole("button", { name: "Approve task 2" }).click();
      await page.locator("#dialog-confirm").click();
      await expect(page.locator(".toast", { hasText: /Approved task 2|approve task 2/ })).toBeVisible();
      const approveElapsedMs = Date.now() - approveStarted;
      if (browserName === "chromium") console.log(`UX task approve: ${isMobile ? "Pixel 4" : "1440x900"}, two clicks, ${approveElapsedMs} ms`);

      await page.goto("/");
      const workerStarted = Date.now();
      await navTo(page, "Workers");
      await expect(page.locator('[data-entity="worker:seed-worker"] .pill')).toContainText("Online");
      const workerElapsedMs = Date.now() - workerStarted;
      if (browserName === "chromium") console.log(`UX task worker health: ${isMobile ? "Pixel 4" : "1440x900"}, one click, ${workerElapsedMs} ms`);
    } finally {
      w.cleanup();
    }
  });

  test("mission detail: task rows, Copy mission ID, and the outcome toast of Reject", async ({ page, coord, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => {});
    const w = await startSeededWorker(coord, { missions: 1 });
    try {
      await setupViaUi(page, coord);
      await navTo(page, "Missions");
      await page.locator("a.row-title", { hasText: "Parser refactor 00" }).click();
      await showTab(page, "Tasks");
      const rows = page.locator('[data-entity^="task:"]');
      await expect(rows.first()).toBeVisible({ timeout: 20_000 });
      await expect(rows).toHaveCount(5);
      await expect(rows.filter({ hasText: "Implement it" })).toBeVisible();
      // The pane reports that no station runs for this seeded mission; wait for that announcement, so it cannot replace the copy confirmation below.
      await expect(page.locator("#live-status")).toContainText(/Terminal ended/, { timeout: 30_000 });
      // Copy the mission ID from the header menu: the clipboard holds the full ID, the live region says so; no toast opens.
      await page.locator(".mission-head").getByRole("button", { name: /more|actions/i }).last().click();
      await page.getByRole("menuitem", { name: "Copy mission ID" }).click();
      await expect(page.locator("#live-status")).toContainText("Copied mission ID");
      await expect(page.locator(".toast")).toHaveCount(0);
      const id = await page.evaluate(() => navigator.clipboard.readText()).catch(() => "");
      if (id) expect(id).toMatch(/^[0-9a-f]{32}$/);
      // Rejecting a task asks for a reason, then reports the outcome in a toast that names it.
      // Reject is in the header from 30rem, in the header menu below it (UXR-048).
      const rejectButton = page.locator('[data-l0="mission.header"]').getByRole("button", { name: "Reject", exact: true });
      if (await rejectButton.isVisible()) await rejectButton.click();
      else {
        await page.locator(".mission-head").getByRole("button", { name: "More mission actions" }).click();
        await page.getByRole("menuitem", { name: "Reject task 2" }).click();
      }
      const dlg = page.getByRole("dialog");
      await expect(dlg).toBeVisible();
      await dlg.locator("#dialog-confirm").click(); // no reason: refused, dialog stays
      await expect(dlg).toBeVisible();
      await dlg.getByLabel("Reason").fill("needs another pass");
      await dlg.locator("#dialog-confirm").click();
      const toast = page.locator(".toast").filter({ hasText: /reject(ed)? task 2/i });
      await expect(toast).toHaveAttribute("data-kind", /applied|rejected|accepted/, { timeout: 20_000 });
      await expect(toast).toContainText(/Rejected task 2|Asked the worker to reject task 2/);
    } finally {
      w.cleanup();
    }
  });
});

test("mission activity humanizes events, counts background events and shows only allowlisted raw fields", async ({ page }) => {
  const { mockApp, MISSION_PATH } = await import("./mock");
  const mock = await mockApp(page, { auth: "in", terminalClose: true });
  await page.goto(MISSION_PATH);
  await showTab(page, "Activity");
  const events = page.locator("#events-list");
  // Milestones by default: lifecycle and task changes; commands, terminal and provider selection are detail.
  await expect(events).toContainText("Mission created");
  await expect(events).toContainText("Task updated · Task 2 · RUNNING → VERIFYING");
  await expect(events).not.toContainText("Terminal opened");
  await expect(events).not.toContainText("Command queued");
  await expect(events).not.toContainText("controller alive");
  await expect(page.locator("[data-plumbing-count]")).toContainText("background event");
  // All activity adds the detail events with readable phrases.
  await page.getByRole("button", { name: "All activity" }).click();
  await expect(events).toContainText("Terminal opened");
  await expect(events).toContainText("Terminal closed · Terminal closed by test");
  await expect(events).toContainText("Command queued: task.verify");
  await expect(events).toContainText("Selected codex (gpt-5)");
  await expect(events).toContainText("Started provider · codex · Task 2 · gpt-5");
  await expect(events).toContainText("Task added · Task 3 · Write a regression test");
  // Background events stay hidden until asked for.
  await expect(events).not.toContainText("controller alive");
  await page.getByRole("button", { name: "Show background events" }).click();
  await expect(page.locator('[data-event-class="plumbing"]').first()).toBeVisible();
  // The Raw block of every event holds allowlisted fields only.
  for (const summary of await page.locator("summary", { hasText: /^Raw$/ }).all()) await summary.click();
  expect(await page.locator("main").innerText()).not.toContain("do-not-render-this");
  // A stream event appears as a detail row.
  mock.emitStream({ type: "worker", worker: "alpha", state: "offline" });
  await expect(events).toContainText("Worker offline · alpha");
});
