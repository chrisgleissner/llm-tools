// UXR-045: every operator task that needs typing completes at 320x568, 360x640 and Pixel 4 size with an on-screen keyboard that covers
// half of the screen. The keyboard is simulated the way the viewport meta declares it (`interactive-widget=resizes-content`): the
// window height is halved while the field has focus (284 px at 320x568). The focused field, the control that submits it and any
// validation message must be inside the visible half and not covered by anything, without the test scrolling.
import type { Locator, Page } from "@playwright/test";
import { test as pw } from "@playwright/test";
import { evidence } from "./evidence";
import { RUNS, openPath, type Ctx } from "./click-paths";
import { expect, uxr } from "./gate";
import { isPhone, viewport } from "./harness";

/** Firefox cannot emulate a touch device (pointer: coarse) with a soft keyboard, which the layouts under test are conditional on: Chromium measures them. */
const NO_KEYBOARD = "phones with a soft keyboard: Chromium only";
const NEEDS = ["WP-0", "WP-1", "WP-2", "WP-3", "WP-4", "WP-5"] as const;
/** The paths that need typing, and so open the keyboard. */
export const TYPING = ["sign-in", "take-control", "steer-note", "answer-question", "reject", "add-task", "add-worker", "find-mission"];

interface Box { x: number; y: number; width: number; height: number }
const frames = (page: Page): Promise<void> => page.evaluate(() => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))));

/** Whether the element is entirely inside the visible half and is the topmost element at its centre (nothing sits over it). */
async function reachable(page: Page, el: Locator, visibleHeight: number): Promise<string | null> {
  const box: Box | null = await el.boundingBox();
  if (!box) return "has no box";
  if (box.y < -0.5 || box.y + box.height > visibleHeight + 0.5) return `is at ${Math.round(box.y)}-${Math.round(box.y + box.height)} px but the visible height is ${visibleHeight} px`;
  if (box.x < -0.5 || box.x + box.width > viewport(page).width + 0.5) return "is outside the width";
  const covered = await el.evaluate((n) => {
    const r = n.getBoundingClientRect();
    const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return !top || n.contains(top) || top.contains(n) ? null : `${top.tagName.toLowerCase()}.${top.className}`;
  });
  return covered ? `is covered by ${covered}` : null;
}

/** A focused field that the keyboard just covered is scrolled back into view by the browser: blur and focus again does the same scroll. */
const focusAgain = (field: Locator): Promise<void> => field.evaluate((n) => { (n as HTMLElement).blur(); (n as HTMLElement).focus(); });

/** The hook of a path: open the keyboard for this field and check the field, its submit control and the message. */
function keyboardHooks(page: Page, seen: string[]): Pick<Ctx, "field"> {
  return {
    field: async (field, submit, message) => {
      const full = viewport(page);
      const visible = Math.floor(full.height / 2);
      await page.setViewportSize({ width: full.width, height: visible });
      await focusAgain(field); // the browser scrolls a focused field into view when the keyboard opens; nothing else scrolls
      await frames(page);
      seen.push(await field.evaluate((n) => n.id || n.getAttribute("aria-label") || n.tagName));
      const problems: string[] = [];
      const check = async (what: string, el: Locator): Promise<void> => {
        const why = await reachable(page, el, visible);
        if (why) problems.push(`${what} ${why}`);
      };
      await check("the focused field", field);
      if (submit) await check("the submit control", submit);
      if (message && (await message.first().isVisible().catch(() => false))) await check("the message", message.first());
      expect(problems, `with the keyboard open at ${full.width}x${visible}`).toEqual([]);
    },
  };
}

for (const run of RUNS.filter((r) => TYPING.includes(r.id))) {
  uxr({ t: "T45", uxr: ["UXR-045"], needs: [...NEEDS] }, `${run.id}: the field, its submit control and the message stay visible above a keyboard that covers half the screen`, async ({ page, browserName }, info) => {
    pw.skip(!isPhone(page) || browserName === "firefox", NO_KEYBOARD);
    const seen: string[] = [];
    const ctx = await openPath(page, run, keyboardHooks(page, seen));
    await run.run(ctx);
    expect(seen.length, "the path opened the keyboard for at least one field").toBeGreaterThan(0);
    await evidence(page, info, ["UXR-045"], `keyboard-${run.id}`);
  });
}

// ---- validation messages stay visible with the keyboard open --------------------------------------------------------
async function halve(page: Page, field: Locator): Promise<number> {
  const full = viewport(page);
  const visible = Math.floor(full.height / 2);
  await page.setViewportSize({ width: full.width, height: visible });
  await focusAgain(field);
  await frames(page);
  return visible;
}

uxr({ t: "T45", uxr: ["UXR-045"], needs: [...NEEDS] }, "sign-in: the error of a wrong password is visible with the keyboard open, with the password field and Sign in", async ({ page, browserName }, info) => {
  pw.skip(!isPhone(page) || browserName === "firefox", NO_KEYBOARD);
  const run = RUNS.find((r) => r.id === "sign-in")!;
  await openPath(page, { ...run, mock: { auth: "login", login: "wrong" } });
  const visible = await halve(page, page.getByLabel("Username"));
  await page.keyboard.type("admin");
  await page.keyboard.press("Tab");
  await page.keyboard.type("not the password");
  await page.keyboard.press("Enter");
  const error = page.locator("#form-error");
  await expect(error).toContainText(/(incorrect|invalid)/i);
  await frames(page);
  for (const [what, el] of [["the error", error], ["the password field", page.locator("#password")], ["Sign in", page.locator("#login-submit")]] as const) expect(await reachable(page, el, visible), `${what} with the keyboard open`).toBeNull();
  await evidence(page, info, ["UXR-045"], "keyboard-sign-in-error");
});

uxr({ t: "T45", uxr: ["UXR-045"], needs: [...NEEDS] }, "reject: the missing-reason message is visible with the keyboard open, with the field and Reject", async ({ page, browserName }, info) => {
  pw.skip(!isPhone(page) || browserName === "firefox", NO_KEYBOARD);
  const run = RUNS.find((r) => r.id === "reject")!;
  await openPath(page, run);
  await page.locator('[data-section="needs-you"] [data-action="reject"]').first().click();
  const d = page.getByRole("dialog");
  const reason = d.locator("textarea, input[type=text], input:not([type])").first();
  const visible = await halve(page, reason);
  await d.locator("[data-confirm]").click();
  const message = d.locator('[role="alert"], [aria-invalid="true"], .field-error').first();
  await expect(message).toBeVisible();
  await frames(page);
  for (const [what, el] of [["the message", message], ["the reason field", reason], ["Reject", d.locator("[data-confirm]")]] as const) expect(await reachable(page, el, visible), `${what} with the keyboard open`).toBeNull();
  await evidence(page, info, ["UXR-045"], "keyboard-reject-error");
});

uxr({ t: "T45", uxr: ["UXR-045"], needs: [...NEEDS] }, "add task: the empty-prompt message is visible with the keyboard open, with the field and Add", async ({ page, browserName }, info) => {
  pw.skip(!isPhone(page) || browserName === "firefox", NO_KEYBOARD);
  const run = RUNS.find((r) => r.id === "add-task")!;
  await openPath(page, run);
  await page.getByRole("tab", { name: /^Tasks\b/i }).click();
  await page.getByRole("button", { name: /add task/i }).click();
  const prompt = page.locator("#new-task");
  const visible = await halve(page, prompt);
  await page.keyboard.press("Enter");
  const message = page.locator("#new-task-error");
  await expect(message).toContainText(/enter the task prompt/i);
  await frames(page);
  for (const [what, el] of [["the message", message], ["the prompt field", prompt], ["Add", page.locator("#new-task-add")]] as const) expect(await reachable(page, el, visible), `${what} with the keyboard open`).toBeNull();
  await evidence(page, info, ["UXR-045"], "keyboard-add-task-error");
});

uxr({ t: "T45", uxr: ["UXR-045"], needs: [...NEEDS] }, "add worker: the invalid-name message is visible with the keyboard open, with the field and Add worker", async ({ page, browserName }, info) => {
  pw.skip(!isPhone(page) || browserName === "firefox", NO_KEYBOARD);
  const run = RUNS.find((r) => r.id === "add-worker")!;
  await openPath(page, run);
  await page.locator("#add-worker-button").click();
  const name = page.locator("#worker-name");
  const visible = await halve(page, name);
  await page.keyboard.type("Not A Valid Name");
  await page.keyboard.press("Enter");
  const message = page.locator("#worker-name-error");
  await expect(message).toContainText(/lowercase letters/i);
  await frames(page);
  const submit = page.getByRole("button", { name: "Add worker" }).last();
  for (const [what, el] of [["the message", message], ["the name field", name], ["Add worker", submit]] as const) expect(await reachable(page, el, visible), `${what} with the keyboard open`).toBeNull();
  await evidence(page, info, ["UXR-045"], "keyboard-add-worker-error");
});

// ---- keyboard that overlays the page instead of resizing it (iOS Safari): only the visual viewport shrinks -----------------
uxr({ t: "T45", uxr: ["UXR-045"], needs: [...NEEDS] }, "a keyboard that overlays the page (visual viewport halved, layout viewport unchanged) leaves the composer and Send above it", async ({ page, browserName }, info) => {
  pw.skip(!isPhone(page) || browserName === "firefox", NO_KEYBOARD);
  const run = RUNS.find((r) => r.id === "take-control")!;
  const ctx = await openPath(page, run);
  await page.locator("#terminal-control").click();
  await expect(page.locator("[data-composer] textarea")).toBeFocused();
  const half = Math.floor(viewport(page).height / 2);
  await page.evaluate((h) => {
    const vv = window.visualViewport!;
    Object.defineProperty(vv, "height", { configurable: true, get: () => h });
    const cover = document.createElement("div");
    cover.id = "kb-sim";
    cover.style.cssText = `position:fixed;inset:auto 0 0 0;block-size:${window.innerHeight - h}px;background:#888;z-index:2147483647`;
    document.body.append(cover);
    vv.dispatchEvent(new Event("resize"));
  }, half);
  await page.locator("[data-composer] textarea").focus();
  await frames(page);
  for (const [what, el] of [["the message field", page.locator("[data-composer] textarea")], ["Send", page.locator("#composer-send")]] as const) expect(await reachable(page, el, half), `${what} above an overlaying keyboard`).toBeNull();
  void ctx;
  await evidence(page, info, ["UXR-045"], "keyboard-overlay-composer");
});

// ---- the composer and the key bar are above the fold before the keyboard opens -----------------------------------------
uxr({ t: "T45", uxr: ["UXR-045"], needs: [...NEEDS] }, "on arrival the composer (Take control, Steer) is inside the fold, above the bottom bar and the action dock", async ({ page, browserName }, info) => {
  pw.skip(!isPhone(page) || browserName === "firefox", NO_KEYBOARD);
  const run = RUNS.find((r) => r.id === "watch")!;
  await openPath(page, run);
  const edge = await page.evaluate(() => {
    const tops = [...document.querySelectorAll<HTMLElement>(".main-nav, .head-actions")].filter((e) => getComputedStyle(e).position === "fixed" && e.offsetHeight > 0).map((e) => e.getBoundingClientRect().top);
    return Math.min(window.innerHeight, ...tops);
  });
  for (const [what, el] of [["Take control", page.locator("#terminal-control")], ["Steer", page.locator('[data-composer] button[aria-haspopup="menu"]')]] as const) {
    const box = (await el.boundingBox())!;
    expect(box.y + box.height, `${what} ends at ${Math.round(box.y + box.height)} px; the fixed bars start at ${Math.round(edge)} px`).toBeLessThanOrEqual(edge + 0.5);
  }
  expect(await reachable(page, page.locator("#terminal-control"), edge), "Take control is not covered").toBeNull();
  await page.locator("#terminal-control").click(); // the message field takes focus: the pane is as tall as the screen and holds the composer and the key bar
  await expect(page.locator("[data-composer] textarea")).toBeFocused();
  const keys = page.locator("#terminal-keys");
  const keyBox = (await keys.boundingBox())!;
  expect(keyBox.y + keyBox.height, "the key bar ends inside the screen").toBeLessThanOrEqual(viewport(page).height + 0.5);
  const rows = await keys.evaluate((k) => new Set([...k.querySelectorAll<HTMLElement>(":scope > .key, :scope > .more")].map((b) => Math.round(b.getBoundingClientRect().top))).size);
  expect(rows, "the seven keys of the key bar are one row").toBe(1);
  expect(keyBox.width, "the key bar is as wide as the screen").toBeGreaterThan(viewport(page).width - 2);
  await evidence(page, info, ["UXR-045"], "composer-in-fold");
});
