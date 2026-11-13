// T10 to T15 and T21 (UXR-004 to UXR-009, UXR-018, UXR-020): the mission detail workspace.
// Fixtures: the default data set (mission 1 runs and has a task awaiting approval) and the dense data set
// (DENSE_* missions: one per header state).
import type { Page } from "@playwright/test";
import { DENSE_APPROVAL_URL, DENSE_CALM_URL, DENSE_MISSIONS } from "../mock";
import { evidence } from "./evidence";
import { expect, uxr } from "./gate";
import { type App, closeKeyboard, insideFold, isPhone, MISSION_URL, openApp, openKeyboard, openTab, pillsIn, viewport } from "./harness";

const ALL_MISSION_WPS = ["WP-0", "WP-1", "WP-2", "WP-3"] as const;
const composerField = (page: Page) => page.locator("[data-composer]").locator("textarea, input[type=text], input:not([type])").first();
const missionUrl = (index: number) => `/missions/${DENSE_MISSIONS[index]!.worker}/${DENSE_MISSIONS[index]!.mission.id}`;

/** Make the task rows visible (a tab on phones) and, when `app` is given, restart the interaction count after the tab switch. */
async function showTasks(page: Page, app?: App): Promise<void> {
  if (!(await page.locator('[data-entity^="task:"]').first().isVisible().catch(() => false))) await openTab(page, "Tasks", app?.ix);
  await page.locator('[data-entity^="task:"]').first().waitFor();
}

// ---- T10: arrival --------------------------------------------------------------------------------------------------
uxr({ t: "T10", uxr: ["UXR-004", "UXR-024"], needs: [...ALL_MISSION_WPS] }, "on arrival the session pane is above the fold, watching read-only, with no interaction", async ({ page }, info) => {
  const app = await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
  const pane = page.locator('[data-pane="session"]');
  await expect(pane).toBeVisible();
  const status = page.locator("#terminal-status");
  await expect(status).toHaveAttribute("data-state", "live");
  await expect(status).toHaveAttribute("data-mode", "view");
  await expect(status).toContainText(/watching/i);
  expect(await app.ix.count(), "interactions needed to see the live session").toBe(0);
  const vp = viewport(page);
  const box = (await pane.boundingBox())!;
  const visible = Math.min(box.y + box.height, vp.height) - Math.max(box.y, 0);
  if (!isPhone(page) && vp.width >= 1280) {
    expect(visible / vp.height, "share of the viewport height the session pane covers").toBeGreaterThanOrEqual(0.6);
  } else if (isPhone(page)) {
    const rowHeight = await page.evaluate(() => (document.querySelector<HTMLElement>("#terminal-host .xterm-rows > div")?.offsetHeight ?? 15));
    expect(visible / rowHeight, `terminal rows that fit in the pane at ${vp.width}x${vp.height}`).toBeGreaterThanOrEqual(14);
  }
  await evidence(page, info, ["UXR-004"], "session-on-arrival");
});

// ---- T11: read-only default, explicit control, permission unchanged -----------------------------------------------
uxr({ t: "T11", uxr: ["UXR-005", "UXR-041"], needs: [...ALL_MISSION_WPS] }, "typing in the pane sends nothing until Take control; control is requested with mode=write", async ({ page }) => {
  const app = await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
  await page.locator("#terminal-host .xterm-helper-textarea").focus({ timeout: 2000 }).catch(() => {});
  await page.keyboard.type("must not be sent");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(300);
  expect(app.terminalInputCount(), "input frames sent in view mode").toBe(0);
  expect(app.terminalSockets().map((s) => s.query["mode"]), "every connection so far is a view connection").not.toContain("write");
  await page.locator('[data-action="take-control"]').click();
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-mode", "write");
  expect(app.terminalSockets().some((s) => s.query["mode"] === "write"), "Take control opens a mode=write connection").toBe(true);
});

uxr({ t: "T11", uxr: ["UXR-005"], needs: [...ALL_MISSION_WPS] }, "a worker without control shows a disabled Take control with the reason", async ({ page }) => {
  await openApp(page, { auth: "in", terminalScreen: true, control: "deny" }, MISSION_URL);
  const button = page.locator('[data-action="take-control"]');
  await expect(button).toBeVisible();
  await expect(button).toBeDisabled();
  await expect(page.locator("[data-composer]")).toContainText("This worker allows viewing only");
});

// ---- T12: composer -------------------------------------------------------------------------------------------------
uxr({ t: "T12", uxr: ["UXR-006", "UXR-020"], needs: [...ALL_MISSION_WPS] }, "the composer stays in view while the page scrolls and with the keyboard open", async ({ page }) => {
  await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
  const composer = page.locator("[data-composer]");
  await expect(composer).toBeVisible();
  const inView = async () => {
    const box = (await composer.boundingBox())!;
    const h = await page.evaluate(() => window.innerHeight);
    return box.y >= -0.5 && box.y + box.height <= h + 0.5;
  };
  expect(await inView(), "composer in view at the top").toBe(true);
  await page.evaluate(() => { window.scrollTo(0, document.documentElement.scrollHeight); for (const el of document.querySelectorAll<HTMLElement>("*")) if (el.scrollHeight > el.clientHeight + 40 && getComputedStyle(el).overflowY.match(/auto|scroll/)) el.scrollTop = el.scrollHeight; });
  expect(await inView(), "composer in view after scrolling the page and the side column to the end").toBe(true);
  const original = viewport(page);
  // A keyboard opens for a field that takes typing: the view-only composer is disabled, so take control first.
  await page.locator('[data-action="take-control"]').click();
  await expect(composerField(page)).toBeEnabled();
  await openKeyboard(page, 300);
  await composerField(page).focus();
  expect(await inView(), "composer above a 300 px on-screen keyboard").toBe(true);
  await closeKeyboard(page, original);
});

uxr({ t: "T12", uxr: ["UXR-020", "UXR-005"], needs: [...ALL_MISSION_WPS] }, "Take control, type, Enter is 3 interactions and sends one input frame; the text is kept while the socket is closed", async ({ page }) => {
  const app = await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
  await page.locator('[data-action="take-control"]').click();
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-mode", "write");
  await expect(composerField(page), "the composer has focus after Take control").toBeFocused();
  await page.keyboard.type("run the tests");
  await page.keyboard.press("Enter");
  await expect.poll(() => app.terminalInputCount(), { message: "input frames after sending once" }).toBe(1);
  expect(await app.ix.count(), "interactions: Take control, type, Enter").toBe(3);
  // Text is kept until the socket accepts the frame.
  await page.keyboard.type("keep me");
  app.closeTerminals();
  await expect(page.locator("#terminal-status")).not.toHaveAttribute("data-state", "live");
  await page.keyboard.press("Enter");
  await expect(composerField(page)).toHaveValue("keep me");
});

// ---- T13: pause and stop -------------------------------------------------------------------------------------------
const HEADER_STATES: Array<{ name: string; url: string; visible: string[]; absent: string[] }> = [
  { name: "needs approval", url: missionUrl(0), visible: ["pause", "stop"], absent: [] },
  { name: "needs input", url: missionUrl(1), visible: ["take-control", "pause", "stop"], absent: [] },
  { name: "running", url: missionUrl(3), visible: ["pause", "stop"], absent: ["resume", "start"] },
  { name: "paused", url: missionUrl(5), visible: ["resume", "stop"], absent: ["pause"] },
  { name: "failed", url: missionUrl(2), visible: ["retry"], absent: ["pause"] },
  { name: "stopped", url: missionUrl(7), visible: ["start"], absent: ["pause", "stop"] },
  { name: "done", url: missionUrl(6), visible: [], absent: ["pause", "stop", "start", "resume"] },
];
for (const state of HEADER_STATES) {
  uxr({ t: "T13", uxr: ["UXR-007", "UXR-023"], needs: [...ALL_MISSION_WPS] }, `header actions in state "${state.name}" are visible without scrolling and never in the overflow menu`, async ({ page }, info) => {
    await openApp(page, { auth: "in", dataset: "dense" }, state.url);
    const line = await page.evaluate(() => window.innerHeight);
    for (const action of state.visible) {
      const el = page.locator(`[data-action="${action}"]`).first();
      await expect(el, `${action} is visible`).toBeVisible();
      expect(await insideFold(el, line), `${action} is inside the first viewport`).toBe(true);
      expect(await el.evaluate((node) => !!node.closest("[role=menu]")), `${action} is not inside a menu`).toBe(false);
    }
    for (const action of state.absent) await expect(page.locator(`[data-action="${action}"]:visible`), `${action} is absent`).toHaveCount(0);
    await evidence(page, info, ["UXR-007"], `header-${state.name.replace(/ /g, "-")}`);
  });
}

uxr({ t: "T13", uxr: ["UXR-007"], needs: [...ALL_MISSION_WPS] }, "Pause takes 1 interaction, Stop takes 2 with a confirmation, and the pill changes within 500 ms", async ({ page }) => {
  const app = await openApp(page, { auth: "in", dataset: "dense", mutationDelayMs: 1500 }, DENSE_CALM_URL);
  const pill = pillsIn(page.locator('[data-entity^="mission:"]')).first();
  await expect(pill).toContainText("Running");
  await page.locator('[data-action="pause"]').click();
  await expect(pill, "optimistic pill after Pause").toContainText("Paused", { timeout: 500 });
  expect(await app.ix.count(), "interactions for Pause").toBe(1);
  await expect.poll(() => app.operations()).toContain("mission.pause");
  await page.reload();
  await app.ix.reset();
  await page.locator('[data-action="stop"]').click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  expect(await app.operations().includes("mission.stop"), "Stop is not sent before the confirmation").toBe(false);
  await dialog.locator("[data-confirm]").click();
  expect(await app.ix.count(), "interactions for Stop").toBe(2);
  await expect.poll(() => app.operations()).toContain("mission.stop");
});

// ---- T14: task panel -----------------------------------------------------------------------------------------------
uxr({ t: "T14", uxr: ["UXR-008", "UXR-023"], needs: [...ALL_MISSION_WPS] }, "task rows hold at most two visible buttons; other actions are in the row menu with the keyboard model", async ({ page }, info) => {
  await openApp(page, { auth: "in" }, MISSION_URL);
  await showTasks(page);
  const rows = page.locator('[data-entity^="task:"]');
  const counts = await rows.evaluateAll((els) => els.map((e) => ({ id: e.getAttribute("data-entity"), buttons: [...e.querySelectorAll<HTMLElement>("button, [role=button]")].filter((b) => b.offsetWidth > 0 && b.getAttribute("aria-haspopup") !== "menu").map((b) => (b.getAttribute("aria-label") ?? b.textContent ?? "").trim()) })));
  for (const row of counts) {
    expect(row.buttons.length, `visible buttons on ${row.id}: ${row.buttons.join(", ")}`).toBeLessThanOrEqual(2);
    expect(row.buttons.filter((b) => /^(pause|cancel|copy)/i.test(b)), `${row.id} shows an action that belongs in the menu`).toEqual([]);
  }
  const queued = page.locator('[data-entity^="task:"]', { hasText: "Write the changelog entry" });
  const trigger = queued.locator('button[aria-haspopup="menu"]');
  await trigger.focus();
  await page.keyboard.press("Enter");
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  const items = page.getByRole("menuitem");
  await expect(items.filter({ hasText: /pause/i })).toHaveCount(1);
  await expect(items.filter({ hasText: /cancel/i })).toHaveCount(1);
  await expect(items.filter({ hasText: /copy/i }).first()).toBeVisible();
  await page.keyboard.press("ArrowDown");
  const first = await page.evaluate(() => document.activeElement?.textContent ?? "");
  await page.keyboard.press("ArrowDown");
  expect(await page.evaluate(() => document.activeElement?.textContent ?? ""), "ArrowDown moves between items").not.toBe(first);
  await page.keyboard.press("End");
  expect(await page.evaluate(() => (document.activeElement as HTMLElement | null)?.getAttribute("role")), "End focuses the last item").toBe("menuitem");
  await evidence(page, info, ["UXR-008"], "task-menu-open");
  await page.keyboard.press("Escape");
  await expect(trigger).toHaveAttribute("aria-expanded", "false");
  await expect(trigger, "focus returns to the trigger").toBeFocused();
});

// ---- T15: activity -------------------------------------------------------------------------------------------------
uxr({ t: "T15", uxr: ["UXR-009"], needs: [...ALL_MISSION_WPS] }, "activity shows milestones only; plumbing is counted and hidden; Raw shows allowlisted fields only", async ({ page }, info) => {
  const app = await openApp(page, { auth: "in" }, MISSION_URL);
  for (let i = 0; i < 4; i += 1) app.pushEvent({ kind: "controller-heartbeat", data: { controller_state: "RUNNING" } });
  for (let i = 0; i < 2; i += 1) app.pushEvent({ kind: "capacity-refresh", data: {} });
  await openTab(page, "Activity");
  const rows = page.locator("[data-event-class]");
  await expect(rows.first()).toBeVisible();
  expect(await rows.count(), "rows in the default activity list").toBeLessThanOrEqual(20);
  expect(await page.locator("[data-event-class]:not([data-event-class=milestone]):visible").count(), "visible non-milestone rows by default").toBe(0);
  const footer = page.locator("[data-plumbing-count]");
  await expect(footer).toBeVisible();
  const hidden = 1 + 4 + 2; // one heartbeat in the fixture, four heartbeats and two capacity refreshes pushed
  await expect.poll(async () => `${await footer.getAttribute("data-plumbing-count")} ${await footer.textContent()}`).toContain(String(hidden));
  await evidence(page, info, ["UXR-009"], "activity-default");
  await footer.getByRole("button", { name: /show/i }).click();
  await expect(page.locator("[data-event-class=plumbing]:visible")).toHaveCount(hidden);
  for (const summary of await page.locator("summary", { hasText: /^raw/i }).all()) await summary.click();
  const text = await page.locator("main").innerText();
  expect(text, "a field outside the allowlist is rendered").not.toContain("do-not-render-this");
  expect(text, "the raw block shows the event kind").toMatch(/task-status/);
});

// ---- T21: add task and steer ---------------------------------------------------------------------------------------
uxr({ t: "T21", uxr: ["UXR-020"], needs: [...ALL_MISSION_WPS] }, "Add task takes at most 3 interactions plus typing and posts task.add", async ({ page }, info) => {
  const app = await openApp(page, { auth: "in" }, MISSION_URL);
  await showTasks(page, app);
  await page.getByRole("button", { name: /add task/i }).click();
  await expect.poll(() => page.evaluate(() => /^(textarea|input)$/i.test(document.activeElement?.tagName ?? "")), { message: "the new-task field takes focus" }).toBe(true);
  await page.keyboard.type("Write the migration guide");
  await evidence(page, info, ["UXR-020"], "add-task");
  await page.keyboard.press("Enter");
  await expect.poll(() => app.operations()).toContain("task.add");
  expect(await app.ix.count(), "interactions: Add task, type, Enter").toBeLessThanOrEqual(3);
  const posted = app.requests().find((r) => r.operation === "task.add");
  expect(JSON.stringify(posted?.body), "the typed prompt is in the request").toContain("Write the migration guide");
});

const STEER_KINDS = ["replace", "review", "verify", "pause-after-iteration", "stop-after-task", "handoff"];
for (const kind of STEER_KINDS) {
  uxr({ t: "T21", uxr: ["UXR-020"], needs: [...ALL_MISSION_WPS] }, `Steer "${kind}" posts mission.steer with that kind in at most 3 interactions`, async ({ page }) => {
    const app = await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
    await page.locator('[data-composer] button[aria-haspopup="menu"]').click();
    await page.locator(`[data-steer="${kind}"]`).click();
    // Every kind but pause-after-iteration asks what it needs first; the dialog's code is a lazy chunk, so wait for it.
    if (kind !== "pause-after-iteration") {
      const dialog = page.getByRole("dialog");
      await dialog.waitFor();
      const field = dialog.locator("textarea, input[type=text], input:not([type])").first();
      if (await field.isVisible().catch(() => false)) await field.fill("please");
      await dialog.locator("[data-confirm]").click();
    }
    await expect.poll(() => app.operations()).toContain("mission.steer");
    expect(app.requests().find((r) => r.operation === "mission.steer")?.body?.["kind"], "steer kind in the request body").toBe(kind);
    expect(await app.ix.count(), "interactions to steer").toBeLessThanOrEqual(3);
    await expect(page.locator("[data-queued-steer]"), "the queued steering chip").toBeVisible();
  });
}
