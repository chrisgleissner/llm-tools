// The operator tasks of the click-path inventory (click-paths-data.ts), each as a runnable path: how to open the starting state,
// how to perform the task with the pointer (counted by interactions.ts), and how to perform it with the keyboard alone.
// Used by uxr-paths.spec.ts (UXR-044), uxr-keyboard.spec.ts (UXR-045) and uxr-reach.spec.ts (UXR-046).
import type { Locator, Page } from "@playwright/test";
import { DENSE_MISSIONS, type MockOptions } from "../mock";
import { expect } from "../fixtures";
import { type App, isPhone, MISSION_URL, openApp, openTab, showTab } from "./harness";

export interface Ctx {
  page: Page;
  app: App;
  phone: boolean;
  /**
   * A field is about to take typing. The keyboard test (UXR-045) opens the on-screen keyboard here and checks that the field,
   * the control that submits it and any validation message stay visible; in the other tests this does nothing.
   */
  field(field: Locator, submit: Locator | null, message?: Locator): Promise<void>;
  /** The primary control of the state is about to be pressed (UXR-046 measures where it is). */
  primary(el: Locator, what: string): Promise<void>;
}

export interface PathRun {
  id: string;
  /** What to mock and where to start. */
  mock: MockOptions;
  path: string;
  /** Work done before counting starts (for example opening a tab that is not part of the task). */
  prepare?: (ctx: Ctx) => Promise<void>;
  /** Perform the task with the pointer (and the keys a pointer user needs). Must end by asserting the outcome. */
  run: (ctx: Ctx) => Promise<void>;
  /** Perform the task with the keyboard alone (Tab, Enter, Escape, arrows, shortcuts and text entry). */
  keys: (ctx: Ctx) => Promise<void>;
}

const mod = "Control";
const dense = (i: number): string => `/missions/${DENSE_MISSIONS[i]!.worker}/${DENSE_MISSIONS[i]!.mission.id}`;
const IN = { auth: "in" } as const;
const DENSE = { auth: "in", dataset: "dense" } as const;
const LIVE = { auth: "in", terminalScreen: true } as const;

const nav = (page: Page, name: string): Locator => page.getByRole("navigation", { name: "Main" }).getByRole("link", { name });
const dialog = (page: Page): Locator => page.getByRole("dialog");
const needsRow = (page: Page, action: string): Locator => page.locator('[data-section="needs-you"] [data-entity^="mission:"]', { has: page.locator(`[data-action="${action}"]`) }).first();
const composerField = (page: Page): Locator => page.locator("[data-composer]").locator("textarea, input[type=text], input:not([type])").first();
const focused = (el: Locator): Promise<boolean> => el.evaluate((n) => n === document.activeElement || n.contains(document.activeElement)).catch(() => false);

/** Press Tab (at most `max` times) until `el` has focus: proves that the control is reachable by keyboard. */
export async function tabTo(page: Page, el: Locator, max = 120): Promise<number> {
  for (let n = 0; n < max; n += 1) {
    if (await focused(el)) return n;
    await page.keyboard.press("Tab");
  }
  throw new Error(`Tab never reached ${el} in ${max} presses`);
}

/** Open the Steer menu with the keyboard and choose an item: Tab to the menu button, Enter, arrows to the item, Enter. */
async function steerByKeys(page: Page, kind: string): Promise<void> {
  await page.keyboard.press("c"); // focus moves into the session pane
  await tabTo(page, page.locator('[data-composer] button[aria-haspopup="menu"]'));
  await page.keyboard.press("Enter");
  const item = page.locator(`[data-steer="${kind}"]`);
  await expect(item).toBeVisible();
  for (let n = 0; n < 8 && !(await focused(item)); n += 1) await page.keyboard.press("ArrowDown");
  expect(await focused(item), `${kind} reached with the arrow keys`).toBe(true);
  await page.keyboard.press("Enter");
}

async function takeControlByKeys(page: Page): Promise<void> {
  await page.keyboard.press("c");
  await expect(page.locator("#terminal-control")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-mode", "write");
  await expect(composerField(page)).toBeFocused();
}

/** Confirm the open dialog: the confirming button when it has focus, else Tab to it. */
async function confirmByKeys(page: Page): Promise<void> {
  const confirm = dialog(page).locator("[data-confirm]");
  await tabTo(page, confirm);
  await page.keyboard.press("Enter");
}

async function tasksTab(page: Page, ctx?: Ctx): Promise<void> {
  if (!(await page.locator('[data-entity^="task:"]').first().isVisible().catch(() => false))) {
    // part of the counted path on a phone
    await showTab(page, "Tasks");
  }
  await page.locator('[data-entity^="task:"]').first().waitFor();
  void ctx;
}

const ADD_TASK = "Write the migration guide";

export const RUNS: PathRun[] = [
  {
    id: "sign-in",
    mock: { auth: "login" },
    path: "/",
    run: async ({ page, field }) => {
      const user = page.getByLabel("Username");
      await expect(user).toBeFocused();
      await field(user, page.locator("#login-submit"));
      await page.keyboard.type("admin");
      await page.keyboard.press("Tab");
      await page.keyboard.type("correct horse battery");
      await page.keyboard.press("Enter");
      await expect(page.locator("header.top")).toBeVisible();
    },
    keys: async ({ page }) => {
      await expect(page.getByLabel("Username")).toBeFocused();
      await page.keyboard.type("admin");
      await page.keyboard.press("Tab");
      await page.keyboard.type("correct horse battery");
      await page.keyboard.press("Enter");
      await expect(page.locator("header.top")).toBeVisible();
    },
  },
  {
    id: "sign-out",
    mock: IN,
    path: "/",
    run: async ({ page }) => {
      await page.locator("#account-button").click();
      await page.locator("#logout").click();
      await expect(page.locator("#login-form")).toBeVisible();
    },
    keys: async ({ page }) => {
      await page.keyboard.press(`${mod}+k`);
      await expect(dialog(page).getByRole("combobox")).toBeFocused();
      await page.keyboard.type("sign out");
      await page.keyboard.press("Enter");
      await expect(page.locator("#login-form")).toBeVisible();
    },
  },
  {
    id: "needs-me",
    mock: DENSE,
    path: "/",
    run: async ({ page }) => {
      await expect(page.locator('[data-section="needs-you"] [data-entity^="mission:"]').first()).toBeVisible();
      await expect(page.locator('[data-q="need-me"]')).toContainText(/need you/);
    },
    keys: async ({ page }) => {
      await expect(page.locator('[data-section="needs-you"] [data-entity^="mission:"]').first()).toBeVisible();
    },
  },
  {
    id: "open-session",
    mock: { ...DENSE, terminalScreen: true },
    path: "/",
    run: async ({ page, primary }) => {
      const link = page.locator('[data-section="running"] [data-entity^="mission:"]').first().locator("a.row-title");
      await primary(link, "mission title");
      await link.click();
      await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
    },
    keys: async ({ page }) => {
      await page.keyboard.press(`${mod}+k`);
      await expect(dialog(page).getByRole("combobox")).toBeFocused();
      await page.keyboard.type("Fix the CI flake");
      await page.keyboard.press("Enter");
      await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
    },
  },
  {
    id: "open-session-list",
    mock: LIVE,
    path: "/missions",
    run: async ({ page }) => {
      await nav(page, "Sessions").click();
      const link = page.locator('[data-entity^="session:"]').first().locator('a[href*="/missions/"]').first();
      await link.click();
      await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
    },
    keys: async ({ page }) => {
      await page.keyboard.press("g");
      await page.keyboard.press("s");
      const link = page.locator('[data-entity^="session:"]').first().locator('a[href*="/missions/"]').first();
      await tabTo(page, link);
      await page.keyboard.press("Enter");
      await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
    },
  },
  {
    id: "watch",
    mock: LIVE,
    path: MISSION_URL,
    run: async ({ page }) => {
      await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
      await expect(page.locator("#terminal-status")).toHaveAttribute("data-mode", "view");
    },
    keys: async ({ page }) => {
      await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
    },
  },
  {
    id: "take-control",
    mock: LIVE,
    path: MISSION_URL,
    run: async ({ page, app, primary, field }) => {
      await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
      await primary(page.locator("#terminal-control"), "Take control");
      await page.locator("#terminal-control").click();
      await expect(composerField(page)).toBeFocused();
      await field(composerField(page), page.locator("#composer-send"));
      await page.keyboard.type("run the tests");
      await page.keyboard.press("Enter");
      await expect.poll(() => app.terminalInputCount()).toBe(1);
    },
    keys: async ({ page, app }) => {
      await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
      await takeControlByKeys(page);
      await page.keyboard.type("run the tests");
      await page.keyboard.press("Enter");
      await expect.poll(() => app.terminalInputCount()).toBe(1);
    },
  },
  {
    id: "steer-quick",
    mock: LIVE,
    path: MISSION_URL,
    run: async ({ page, app }) => {
      await page.locator('[data-composer] button[aria-haspopup="menu"]').click();
      await page.locator('[data-steer="pause-after-iteration"]').click();
      await expect.poll(() => app.operations()).toContain("mission.steer");
      await expect(page.locator("[data-queued-steer]")).toBeVisible();
    },
    keys: async ({ page, app }) => {
      await steerByKeys(page, "pause-after-iteration");
      await expect.poll(() => app.operations()).toContain("mission.steer");
    },
  },
  {
    id: "steer-note",
    mock: LIVE,
    path: MISSION_URL,
    run: async ({ page, app, field }) => {
      await page.locator('[data-composer] button[aria-haspopup="menu"]').click();
      await page.locator('[data-steer="replace"]').click();
      const d = dialog(page);
      await d.waitFor();
      const input = d.locator("textarea, input[type=text], input:not([type])").first();
      await field(input, d.locator("[data-confirm]"));
      await page.keyboard.type("please use the parser module");
      await d.locator("[data-confirm]").click();
      await expect.poll(() => app.operations()).toContain("mission.steer");
    },
    keys: async ({ page, app }) => {
      await steerByKeys(page, "replace");
      const d = dialog(page);
      await d.waitFor();
      await expect(d.locator("textarea, input[type=text], input:not([type])").first()).toBeFocused();
      await page.keyboard.type("please use the parser module");
      await confirmByKeys(page);
      await expect.poll(() => app.operations()).toContain("mission.steer");
    },
  },
  {
    id: "answer-question",
    mock: { ...IN, providerQuestion: true, terminalScreen: true },
    path: "/",
    run: async ({ page, app, primary, field }) => {
      const answer = page.locator('[data-section="needs-you"] [data-action="answer"]').first();
      await primary(answer, "Answer");
      await answer.click();
      await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
      await expect(page.locator("#terminal-control")).toBeFocused();
      await page.locator("#terminal-control").click();
      await expect(composerField(page)).toBeFocused();
      await field(composerField(page), page.locator("#composer-send"));
      await page.keyboard.type("2");
      await page.keyboard.press("Enter");
      await expect.poll(() => app.terminalInputCount()).toBe(1);
    },
    keys: async ({ page, app }) => {
      await page.keyboard.press("j");
      const answer = page.locator('[data-section="needs-you"] [data-action="answer"]').first();
      await tabTo(page, answer);
      await page.keyboard.press("Enter");
      await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
      await expect(page.locator("#terminal-control")).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(composerField(page)).toBeFocused();
      await page.keyboard.type("2");
      await page.keyboard.press("Enter");
      await expect.poll(() => app.terminalInputCount()).toBe(1);
    },
  },
  {
    id: "approve-inbox",
    mock: DENSE,
    path: "/",
    run: async ({ page, app, primary }) => {
      const approve = needsRow(page, "approve").locator('[data-action="approve"]');
      await primary(approve, "Approve");
      await approve.click();
      await dialog(page).locator("[data-confirm]").click();
      await expect.poll(() => app.operations()).toContain("task.verify");
    },
    keys: async ({ page, app }) => {
      await page.keyboard.press("j");
      await focusApprovalRow(page);
      await page.keyboard.press("a");
      await expect(dialog(page)).toBeVisible();
      await confirmByKeys(page);
      await expect.poll(() => app.operations()).toContain("task.verify");
    },
  },
  {
    id: "approve-mission",
    mock: { auth: "in" },
    path: MISSION_URL,
    run: async ({ page, app, primary }) => {
      const approve = page.locator('.head-actions [data-action="approve"]');
      await primary(approve, "Approve");
      await approve.click();
      await dialog(page).locator("[data-confirm]").click();
      await expect.poll(() => app.operations()).toContain("task.verify");
    },
    keys: async ({ page, app }) => {
      await page.locator('.head-actions [data-action="approve"]').waitFor();
      await page.keyboard.press("a");
      await expect(dialog(page)).toBeVisible();
      await confirmByKeys(page);
      await expect.poll(() => app.operations()).toContain("task.verify");
    },
  },
  {
    id: "reject",
    mock: DENSE,
    path: "/",
    run: async ({ page, app, primary, field }) => {
      const reject = needsRow(page, "reject").locator('[data-action="reject"]');
      await primary(reject, "Reject");
      await reject.click();
      const d = dialog(page);
      const reason = d.locator("textarea, input[type=text], input:not([type])").first();
      await expect(reason).toBeFocused();
      await field(reason, d.locator("[data-confirm]"), d.locator("[role=alert]"));
      await page.keyboard.type("The verifier output is not enough");
      await d.locator("[data-confirm]").click();
      await expect.poll(() => app.operations()).toContain("task.reject");
    },
    keys: async ({ page, app }) => {
      await page.keyboard.press("j");
      await focusApprovalRow(page);
      await page.keyboard.press("r");
      const d = dialog(page);
      await expect(d.locator("textarea, input[type=text], input:not([type])").first()).toBeFocused();
      await page.keyboard.type("The verifier output is not enough");
      await confirmByKeys(page);
      await expect.poll(() => app.operations()).toContain("task.reject");
    },
  },
  {
    id: "pause",
    mock: { ...DENSE, terminalScreen: true },
    path: dense(3),
    run: async ({ page, app, primary }) => {
      const pause = page.locator('.head-actions [data-action="pause"]');
      await primary(pause, "Pause");
      await pause.click();
      await expect.poll(() => app.operations()).toContain("mission.pause");
    },
    keys: async ({ page, app }) => {
      await page.locator('[data-action="pause"]').waitFor();
      await page.keyboard.press("p");
      await expect.poll(() => app.operations()).toContain("mission.pause");
    },
  },
  {
    id: "resume",
    mock: { ...DENSE, terminalScreen: true },
    path: dense(5),
    run: async ({ page, app, primary }) => {
      const resume = page.locator('.head-actions [data-action="resume"]');
      await primary(resume, "Resume");
      await resume.click();
      await expect.poll(() => app.operations()).toContain("mission.resume");
    },
    keys: async ({ page, app }) => {
      await tabTo(page, page.locator('.head-actions [data-action="resume"]'));
      await page.keyboard.press("Enter");
      await expect.poll(() => app.operations()).toContain("mission.resume");
    },
  },
  {
    id: "stop",
    mock: { ...DENSE, terminalScreen: true },
    path: dense(3),
    run: async ({ page, app, primary }) => {
      const stop = page.locator('.head-actions [data-action="stop"]');
      await primary(stop, "Stop");
      await stop.click();
      await dialog(page).locator("[data-confirm]").click();
      await expect.poll(() => app.operations()).toContain("mission.stop");
    },
    keys: async ({ page, app }) => {
      await tabTo(page, page.locator('.head-actions [data-action="stop"]'));
      await page.keyboard.press("Enter");
      await expect(dialog(page)).toBeVisible();
      await confirmByKeys(page);
      await expect.poll(() => app.operations()).toContain("mission.stop");
    },
  },
  {
    id: "start",
    mock: { ...DENSE, terminalScreen: true },
    path: dense(7),
    run: async ({ page, app, primary }) => {
      const start = page.locator('.head-actions [data-action="start"]');
      await primary(start, "Start");
      await start.click();
      await expect.poll(() => app.operations()).toContain("mission.start");
    },
    keys: async ({ page, app }) => {
      await tabTo(page, page.locator('.head-actions [data-action="start"]'));
      await page.keyboard.press("Enter");
      await expect.poll(() => app.operations()).toContain("mission.start");
    },
  },
  {
    id: "retry",
    mock: { ...DENSE, terminalScreen: true },
    path: dense(2),
    run: async ({ page, app, primary }) => {
      const retry = page.locator('.head-actions [data-action="retry"]');
      await primary(retry, "Retry");
      await retry.click();
      await expect.poll(() => app.operations()).toContain("task.retry");
    },
    keys: async ({ page, app }) => {
      await tabTo(page, page.locator('.head-actions [data-action="retry"]'));
      await page.keyboard.press("Enter");
      await expect.poll(() => app.operations()).toContain("task.retry");
    },
  },
  {
    id: "add-task",
    mock: LIVE,
    path: MISSION_URL,
    run: async ({ page, app, field }) => {
      await tasksTab(page);
      await page.getByRole("button", { name: /add task/i }).click();
      const prompt = page.locator("#new-task");
      await expect(prompt).toBeFocused();
      await field(prompt, page.locator("#new-task-add"), page.locator("#new-task-error"));
      await page.keyboard.type(ADD_TASK);
      await page.keyboard.press("Enter");
      await expect.poll(() => app.operations()).toContain("task.add");
    },
    keys: async ({ page, app }) => {
      await tabTo(page, page.locator("#add-task"));
      await page.keyboard.press("Enter");
      await expect(page.locator("#new-task")).toBeFocused();
      await page.keyboard.type(ADD_TASK);
      await page.keyboard.press("Enter");
      await expect.poll(() => app.operations()).toContain("task.add");
    },
  },
  {
    id: "cancel-task",
    mock: LIVE,
    path: MISSION_URL,
    run: async ({ page, app }) => {
      await tasksTab(page);
      const queued = page.locator('[data-entity^="task:"]', { hasText: "Write the changelog entry" });
      await queued.locator('button[aria-haspopup="menu"]').click();
      await page.getByRole("menuitem", { name: /cancel task/i }).click();
      await dialog(page).locator("[data-confirm]").click();
      await expect.poll(() => app.operations()).toContain("task.cancel");
    },
    keys: async ({ page, app }) => {
      const queued = page.locator('[data-entity^="task:"]', { hasText: "Write the changelog entry" });
      await tabTo(page, queued.locator('button[aria-haspopup="menu"]'));
      await page.keyboard.press("Enter");
      const item = page.getByRole("menuitem", { name: /cancel task/i });
      for (let n = 0; n < 6 && !(await focused(item)); n += 1) await page.keyboard.press("ArrowDown");
      await page.keyboard.press("Enter");
      await confirmByKeys(page);
      await expect.poll(() => app.operations()).toContain("task.cancel");
    },
  },
  {
    id: "task-evidence",
    mock: LIVE,
    path: MISSION_URL,
    run: async ({ page }) => {
      await tasksTab(page);
      await page.locator("[data-task-link]").nth(1).click();
      await expect(page.locator('section[aria-label="Evidence"]')).toBeVisible();
    },
    keys: async ({ page }) => {
      await tabTo(page, page.locator("[data-task-link]").nth(1));
      await page.keyboard.press("Enter");
      await expect(page.locator('section[aria-label="Evidence"]')).toBeVisible();
    },
  },
  {
    id: "activity",
    mock: LIVE,
    path: MISSION_URL,
    run: async ({ page }) => {
      await page.getByRole("tab", { name: /^Activity\b/i }).click();
      await expect(page.locator("[data-event-class]").first()).toBeVisible();
    },
    keys: async ({ page }) => {
      await tabTo(page, page.getByRole("tab", { name: /^Activity\b/i }));
      await page.keyboard.press("Enter");
      await expect(page.locator("[data-event-class]").first()).toBeVisible();
    },
  },
  {
    id: "find-mission",
    mock: IN,
    path: "/workers",
    run: async ({ page, field }) => {
      await page.locator("#palette-open").click();
      await expect(dialog(page).getByRole("combobox")).toBeFocused();
      await field(dialog(page).getByRole("combobox"), null);
      await page.keyboard.type("Refactor the parser");
      await expect(dialog(page).getByRole("option").first()).toContainText("Refactor the parser");
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/\/missions\/alpha\//);
    },
    keys: async ({ page }) => {
      await page.keyboard.press(`${mod}+k`);
      await page.keyboard.type("Refactor the parser");
      await expect(dialog(page).getByRole("option").first()).toContainText("Refactor the parser");
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(/\/missions\/alpha\//);
    },
  },
  {
    id: "capacity",
    mock: IN,
    path: "/",
    run: async ({ page }) => {
      await expect(page.locator("header").first()).toContainText(/Claude\s+5h\s+4%/);
    },
    keys: async ({ page }) => {
      await expect(page.locator("header").first()).toContainText(/Claude\s+5h\s+4%/);
    },
  },
  {
    id: "usage",
    mock: IN,
    path: "/",
    run: async ({ page, phone }) => {
      // On a phone the Usage item of the bottom bar is in reach of the thumb; the capacity chip in the top bar is the same one interaction.
      const chip = phone ? nav(page, "Usage") : page.locator("header .cap-chip").first();
      await chip.click();
      await expect(page).toHaveURL(/\/usage$/);
      await expect(page.locator('[data-entity^="provider:"]').first()).toBeVisible();
    },
    keys: async ({ page }) => {
      await page.keyboard.press("g");
      await page.keyboard.press("u");
      await expect(page).toHaveURL(/\/usage$/);
      await expect(page.locator('[data-entity^="provider:"]').first()).toBeVisible();
    },
  },
  {
    id: "worker-status",
    mock: IN,
    path: "/",
    run: async ({ page }) => {
      await nav(page, "Workers").click();
      await expect(page.locator('[data-entity^="worker:"]').first()).toBeVisible();
    },
    keys: async ({ page }) => {
      await page.keyboard.press("g");
      await page.keyboard.press("w");
      await expect(page.locator('[data-entity^="worker:"]').first()).toBeVisible();
    },
  },
  {
    id: "add-worker",
    mock: IN,
    path: "/workers",
    run: async ({ page, field }) => {
      await page.locator("#add-worker-button").click();
      const name = page.locator("#worker-name");
      await expect(name).toBeFocused();
      await field(name, page.getByRole("button", { name: "Add worker" }).last(), page.locator("#worker-name-error"));
      await page.keyboard.type("build-2");
      await page.keyboard.press("Enter");
      await expect(page.locator("#worker-token")).toContainText("lbr_wrk_");
    },
    keys: async ({ page }) => {
      await tabTo(page, page.locator("#add-worker-button"));
      await page.keyboard.press("Enter");
      await expect(page.locator("#worker-name")).toBeFocused();
      await page.keyboard.type("build-2");
      await page.keyboard.press("Enter");
      await expect(page.locator("#worker-token")).toContainText("lbr_wrk_");
    },
  },
  {
    id: "reset-hint",
    mock: { auth: "login" },
    path: "/",
    run: async ({ page }) => {
      await expect(page.locator("#forgot-password-hint")).toBeVisible();
      await page.locator("#forgot-password-hint summary").click();
      await expect(page.locator("#forgot-password-hint code")).toContainText("reset-password");
    },
    keys: async ({ page }) => {
      await tabTo(page, page.locator("#forgot-password-hint summary"));
      await page.keyboard.press("Enter");
      await expect(page.locator("#forgot-password-hint code")).toContainText("reset-password");
    },
  },
];

/** `j` selects the first row; the approval row of the dense set is the first needs-you row, so focus is already there. Otherwise step down to it. */
async function focusApprovalRow(page: Page): Promise<void> {
  const target = needsRow(page, "approve");
  for (let n = 0; n < 12 && !(await focused(target)); n += 1) await page.keyboard.press("j");
  expect(await focused(target), "j reaches a row that can be approved").toBe(true);
}

export const isPhoneProject = (page: Page): boolean => isPhone(page);

/** Open a path's starting state and return the context with the interaction counter at zero. */
export async function openPath(page: Page, run: PathRun, hooks: Partial<Pick<Ctx, "field" | "primary">> = {}): Promise<Ctx> {
  const app = await openApp(page, run.mock, run.path);
  const ctx: Ctx = { page, app, phone: isPhone(page), field: hooks.field ?? (async () => {}), primary: hooks.primary ?? (async () => {}) };
  await run.prepare?.(ctx);
  await app.ix.reset();
  return ctx;
}

export { openTab };
