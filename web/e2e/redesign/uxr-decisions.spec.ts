// T48 (D28): the six operator-approved redesign decisions and the overseer's findings R1 to R6 (UXR-047 to UXR-052).
// Each test says in its title which decision or finding it guards; the revert that makes it fail is recorded in docs/distributed/DECISIONS.md (D28).
import { test as pw } from "@playwright/test";
import { DENSE_CALM_URL, DENSE_MISSIONS, MISSIONS } from "../mock";
import { evidence } from "./evidence";
import { expect, uxr } from "./gate";
import { isPhone, MISSION_URL, openApp, showTab, viewport } from "./harness";

const NEEDS = ["WP-0", "WP-1", "WP-3", "WP-4"] as const;
const DENSE = { auth: "in", dataset: "dense" } as const;
const LIVE = { auth: "in", terminalScreen: true } as const;
const denseUrl = (i: number) => `/missions/${DENSE_MISSIONS[i]!.worker}/${DENSE_MISSIONS[i]!.mission.id}`;

// ---- decision 1: Inbox order ---------------------------------------------------------------------------------------------
uxr({ t: "T48", uxr: ["UXR-011"], needs: [...NEEDS] }, "D28.1: failures of the last 24 hours come first, then approval, then input; no older-failures row while every failure is recent", async ({ page }) => {
  await openApp(page, DENSE, "/");
  await page.locator("#show-more-needs-you").click();
  const states = await page.locator('[data-section="needs-you"] li[data-state]').evaluateAll((els) => els.map((e) => e.getAttribute("data-state")));
  expect(states.length, "all rows are shown").toBeGreaterThanOrEqual(12);
  const order = ["failed", "needs-approval", "needs-input"];
  expect(states.map((s) => order.indexOf(s ?? "")), "failed, approval, input").toEqual([...states.map((s) => order.indexOf(s ?? ""))].sort((a, b) => a - b));
  await expect(page.locator("[data-older-failures]")).toHaveCount(0);
});

uxr({ t: "T48", uxr: ["UXR-011"], needs: [...NEEDS] }, "D28.1: failures older than 24 hours are one row after the input rows, 'N older failures', and Show expands them", async ({ page }, info) => {
  await openApp(page, { ...DENSE, fakeClock: true }, "/");
  await expect(page.locator('[data-section="needs-you"] li[data-state="failed"]').first()).toBeVisible();
  // Three of the five failures are 3 to 4 hours old; 22 hours later they are older than a day, the other two are not.
  await page.clock.fastForward("22:00:00");
  await page.locator("#show-more-needs-you").click();
  const group = page.locator("[data-older-failures]");
  await expect(group).toHaveText(/3 older failures/);
  // The preview shows a row that is on the screen, not the oldest failure that was folded into the group.
  if (await page.locator(".inbox-preview").isVisible()) await expect(page.locator(".inbox-preview .preview-title")).not.toContainText("Docs migration 35");
  const sequence = () => page.locator('[data-section="needs-you"] li').evaluateAll((els) => els.map((e) => (e.hasAttribute("data-older-failures") ? "older" : (e.getAttribute("data-state") ?? "?"))));
  const before = await sequence();
  expect(before.slice(0, 2), "the two recent failures lead").toEqual(["failed", "failed"]);
  expect(before.indexOf("needs-approval"), "approvals follow the recent failures").toBe(2);
  expect(before.at(-1), "the older failures are the last row, after the input rows").toBe("older");
  expect(before.lastIndexOf("needs-input"), "after the input rows").toBe(before.length - 2);
  expect(before.filter((s) => s === "failed"), "older failures are not listed until Show").toHaveLength(2);
  await expect(page.locator('[data-section="needs-you"] h2')).toHaveText(/Needs you \(1[0-9]\+?\)/);
  await evidence(page, info, ["UXR-011"], "older-failures-collapsed");
  await group.getByRole("button", { name: "Show" }).click();
  const after = await sequence();
  expect(after.at(-4), "Show lists the older failures after the group row").toBe("older");
  expect(after.slice(-3), "three failures follow the row").toEqual(["failed", "failed", "failed"]);
  expect(after.filter((s) => s === "failed")).toHaveLength(5);
  expect(after.indexOf("older"), "the group row stays where it was").toBe(before.indexOf("older"));
  await group.getByRole("button", { name: "Hide" }).click();
  expect((await sequence()).filter((s) => s === "failed")).toHaveLength(2);
});

// ---- decision 2: capacity chip ---------------------------------------------------------------------------------------------
// Mock usage: Codex is ready (5h 75% left), Claude is not (5h 4% left, resets in 25 minutes).
uxr({ t: "T48", uxr: ["UXR-014"], needs: [...NEEDS] }, "D28.2: a chip is neutral while its provider is ready; the waiting provider has the attention tone and 'resets HH:MM' in its text", async ({ page }, info) => {
  await openApp(page, { auth: "in" }, "/");
  const waiting = page.locator("#capacity .cap-chip.waiting");
  await expect(waiting).toHaveCount(1);
  await expect(waiting).toContainText("Claude");
  expect(await waiting.textContent(), "the text says when capacity returns").toMatch(/resets \d{2}:\d{2}/);
  const tone = (sel: string) => page.locator(sel).first().evaluate((e) => getComputedStyle(e).backgroundColor);
  const attention = await page.evaluate(() => { const probe = document.createElement("i"); probe.style.background = "var(--alert-attention-bg)"; document.body.append(probe); const c = getComputedStyle(probe).backgroundColor; probe.remove(); return c; });
  expect(await tone("#capacity .cap-chip.waiting"), "the attention tone").toBe(attention);
  if (viewport(page).width >= 480) {
    const ready = page.locator("#capacity .cap-chip:not(.waiting)");
    await expect(ready).toContainText("Codex");
    expect(await ready.textContent(), "a ready provider says nothing about a reset").not.toMatch(/resets/);
    expect(await tone("#capacity .cap-chip:not(.waiting)"), "a ready chip is neutral").not.toBe(attention);
  }
  await evidence(page, info, ["UXR-014"], "capacity-waiting");
});

uxr({ t: "T48", uxr: ["UXR-014", "UXR-029"], needs: [...NEEDS] }, "D28.2: the waiting chip, with its reset time, fits the header at 320 px", async ({ page }) => {
  await openApp(page, { auth: "in" }, "/");
  await page.setViewportSize({ width: 320, height: 568 });
  await page.waitForTimeout(200);
  const fit = await page.evaluate(() => {
    const box = document.querySelector("#capacity")!.getBoundingClientRect();
    const chip = document.querySelector("#capacity .cap-chip")!.getBoundingClientRect();
    return { inside: chip.left >= box.left - 0.5 && chip.right <= box.right + 0.5, text: document.querySelector<HTMLElement>("#capacity .cap-chip")!.innerText };
  });
  expect(fit.inside, "the chip is not clipped by the header").toBe(true);
  expect(fit.text).toMatch(/resets \d{2}:\d{2}/);
});

// ---- decision 3: Stop beside Reject, Undo for Pause ----------------------------------------------------------------------------
uxr({ t: "T48", uxr: ["UXR-007"], needs: [...NEEDS] }, "D28.3: Stop has no danger outline beside a shown Reject and stays visible", async ({ page }) => {
  await openApp(page, { auth: "in" }, MISSION_URL);
  const stop = page.locator('[data-l0="mission.header"] [data-action="stop"]');
  const reject = page.locator('[data-l0="mission.header"] [data-action="reject"]');
  await expect(stop).toBeVisible();
  await expect(stop).toBeEnabled();
  // Below 30rem Reject is in the menu, so nothing red stands beside Stop and it keeps its outline.
  if (await reject.isVisible()) {
    await expect(reject).toHaveClass(/danger/);
    await expect(stop, "Stop beside Reject is plain").not.toHaveClass(/danger/);
  } else await expect(stop, "Reject is in the menu: Stop keeps its outline").toHaveClass(/danger/);
});

uxr({ t: "T48", uxr: ["UXR-007"], needs: [...NEEDS] }, "D28.3: without Reject, Stop keeps its danger outline", async ({ page }) => {
  await openApp(page, DENSE, DENSE_CALM_URL);
  await expect(page.locator('[data-l0="mission.header"] [data-action="reject"]')).toHaveCount(0);
  await expect(page.locator('[data-l0="mission.header"] [data-action="stop"]')).toHaveClass(/danger/);
});

uxr({ t: "T48", uxr: ["UXR-011"], needs: [...NEEDS] }, "D28.3: Pause answers with an Undo toast that lasts 8 s and not longer; Undo resumes the mission", async ({ page }, info) => {
  const app = await openApp(page, { ...DENSE, fakeClock: true }, DENSE_CALM_URL);
  await page.locator('[data-action="pause"]').click();
  const toast = page.locator(".toast");
  await expect(toast).toHaveCount(1);
  await expect(toast).toContainText("Mission paused.");
  const undo = toast.getByRole("button", { name: "Undo" });
  await expect(undo).toBeVisible();
  await evidence(page, info, ["UXR-011"], "pause-undo");
  await page.clock.runFor(7000);
  await expect(toast, "still there after 7 s").toHaveCount(1);
  await page.clock.runFor(1500);
  await expect(toast, "gone after 8.5 s").toHaveCount(0);
  await page.locator('[data-action="resume"]').first().click();
  await page.locator('[data-action="pause"]').click();
  await toast.getByRole("button", { name: "Undo" }).click();
  await expect.poll(() => app.operations().filter((o) => o === "mission.resume").length, { message: "Undo sends mission.resume" }).toBeGreaterThanOrEqual(2);
});

// ---- decision 4 (replaced by the overseer): short labels, Pause and Stop always visible ------------------------------------------
uxr({ t: "T48", uxr: ["UXR-007"], needs: [...NEEDS] }, "D28.4: the primary action reads 'Approve' and keeps the task number in its accessible name", async ({ page }) => {
  await openApp(page, { auth: "in" }, MISSION_URL);
  const approve = page.locator('[data-l0="mission.header"] [data-action="approve"]');
  await expect(approve).toHaveText("Approve");
  await expect(approve).toHaveAccessibleName("Approve task 2");
});

uxr({ t: "T48", uxr: ["UXR-007"], needs: [...NEEDS] }, "D28.4: on a phone Pause and Stop are 44 px buttons with an icon and a short label in one row, icon only at 320 px", async ({ page }, info) => {
  pw.skip(!isPhone(page), "phone layout");
  const own = viewport(page).width; // the project's width: the loop changes the viewport
  for (const [state, url, opts] of [["approval", MISSION_URL, { auth: "in" as const }], ["input", denseUrl(1), DENSE]] as const) {
    await openApp(page, opts, url);
    for (const width of [own, 360]) {
      await page.setViewportSize({ width, height: 745 });
      await page.waitForTimeout(150);
      const row = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>(".mission-head .head-actions button")].filter((b) => b.offsetWidth > 0).map((b) => ({ a: b.getAttribute("data-action") ?? "menu", top: Math.round(b.getBoundingClientRect().top), w: b.offsetWidth, h: b.offsetHeight, label: b.querySelector("span")?.offsetWidth ?? 0 })));
      expect(new Set(row.map((b) => b.top)).size, `${state} at ${width}: one row (${JSON.stringify(row)})`).toBe(1);
      expect(row.every((b) => b.w >= 44 && b.h >= 44), `${state} at ${width}: 44 px targets`).toBe(true);
      for (const a of ["pause", "stop"]) expect(row.find((b) => b.a === a)!.label, `${state} at ${width}: the ${a} label is visible`).toBeGreaterThan(10);
    }
    await page.setViewportSize({ width: 320, height: 568 });
    await page.waitForTimeout(150);
    const narrow = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>(".mission-head .head-actions button")].filter((b) => b.offsetWidth > 0).map((b) => ({ a: b.getAttribute("data-action") ?? "menu", top: Math.round(b.getBoundingClientRect().top), label: b.querySelector("span")?.offsetWidth ?? 0 })));
    expect(new Set(narrow.map((b) => b.top)).size, `${state} at 320: one row`).toBe(1);
    for (const a of ["pause", "stop"]) expect(narrow.find((b) => b.a === a)!.label, `${state} at 320: icon only`).toBeLessThanOrEqual(1);
    await expect(page.locator('.mission-head [data-action="pause"]')).toHaveAccessibleName("Pause");
    await expect(page.locator('.mission-head [data-action="stop"]')).toHaveAccessibleName("Stop");
    if (state === "input") await evidence(page, info, ["UXR-007"], "phone-actions-320");
  }
});

// ---- decision 5: key bar -----------------------------------------------------------------------------------------------------
uxr({ t: "T48", uxr: ["UXR-028"], needs: [...NEEDS, "WP-2"] }, "D28.5: on a phone the key bar is hidden until control, at 745 px and at 900 px tall, and visible and enabled after Take control", async ({ page }) => {
  pw.skip(!isPhone(page), "phone layout");
  await openApp(page, LIVE, MISSION_URL);
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
  for (const height of [viewport(page).height, 900]) {
    await page.setViewportSize({ width: viewport(page).width, height });
    await page.waitForTimeout(150);
    await expect(page.locator("#terminal-keys"), `hidden before control at ${height} px tall`).toBeHidden();
  }
  await page.locator('[data-action="take-control"]').click();
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-mode", "write");
  await expect(page.locator("#terminal-keys button:visible")).toHaveCount(7);
  await expect(page.locator("#key-esc")).toBeEnabled();
});

// ---- decision 6 is guarded in shell.spec.ts (banner without age, then with it) --------------------------------------------------------

// ---- R1: a phone opens the live session, also while a task awaits approval (UXR-047) -----------------------------------------------
uxr({ t: "T48", uxr: ["UXR-047", "UXR-004"], needs: [...NEEDS, "WP-2"] }, "R1: a mission that awaits approval opens on the Session tab on a phone, with Approve in reach and 14 rows of output", async ({ page }, info) => {
  pw.skip(!isPhone(page), "phone layout");
  await openApp(page, LIVE, MISSION_URL);
  await expect(page.locator('[data-l0="mission.header"] [data-action="approve"]')).toBeVisible();
  await expect(page.getByRole("tab", { name: /^Session/ })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
  const rows = await page.evaluate(() => { const pane = document.querySelector<HTMLElement>("#terminal-host")!; const row = document.querySelector<HTMLElement>("#terminal-host .xterm-rows > div")?.offsetHeight ?? 15; return pane.getBoundingClientRect().height / row; });
  expect(rows, "terminal rows visible on arrival").toBeGreaterThanOrEqual(14);
  await evidence(page, info, ["UXR-047"], "phone-arrival-awaiting-approval");
});

// ---- R2: one place for the primary action per viewport (UXR-048) ----------------------------------------------------------------------
uxr({ t: "T48", uxr: ["UXR-048"], needs: [...NEEDS] }, "R2: Approve and Reject are offered once, in the header, for the task the header is about; its row and the evidence offer neither", async ({ page }, info) => {
  await openApp(page, { auth: "in" }, MISSION_URL);
  await showTab(page, "Tasks");
  await expect(page.locator('[data-task="2"]')).toBeVisible();
  await expect(page.locator(".evidence-verifier")).toBeVisible();
  await expect(page.locator('[data-action="approve"]:visible'), "Approve is on the page once").toHaveCount(1);
  await expect(page.locator('[data-task="2"] [data-action="approve"], [data-task="2"] [data-action="reject"], .evidence [data-action]')).toHaveCount(0);
  await expect(page.locator('[data-task="2"] [aria-haspopup="menu"]'), "the row keeps its menu").toBeVisible();
  await expect(page.locator('[data-task="3"] [data-action="retry"]'), "a row that is not the header's subject keeps its inline action").toBeVisible();
  await evidence(page, info, ["UXR-048"], "one-place");
});

uxr({ t: "T48", uxr: ["UXR-048"], needs: [...NEEDS] }, "R2: with several tasks awaiting approval the header says 'Review N tasks' and each row keeps its own Approve and Reject", async ({ page }) => {
  const app = await openApp(page, { auth: "in" }, MISSION_URL);
  app.setTasks(MISSIONS[0]!.mission.id, ["VERIFYING", "VERIFYING"]);
  app.setNeeds(MISSIONS[0]!.mission.id, { kind: "approval", tasks: [1, 2], message: "tasks 1, 2 await your review" });
  await page.reload();
  await showTab(page, "Tasks");
  await expect(page.locator('[data-l0="mission.header"] [data-action="review"]')).toBeVisible();
  await expect(page.locator('[data-task] [data-action="approve"]')).toHaveCount(2);
});

// ---- R3: one number everywhere (UXR-049) ---------------------------------------------------------------------------------------------------
uxr({ t: "T48", uxr: ["UXR-049"], needs: [...NEEDS] }, "R3: the Inbox badge, the answer line and the Needs you heading show one number; '+' only while missions are unread", async ({ page }) => {
  await openApp(page, DENSE, "/");
  const figure = async (text: string | null): Promise<string> => (text ?? "").match(/\d+\+?/)?.[0] ?? "";
  const numbers = async () => [await figure(await page.locator(".main-nav .nav-count").textContent()), await figure(await page.locator("[data-q=need-me]").textContent()), await figure(await page.locator('[data-section="needs-you"] h2').textContent())];
  await expect.poll(async () => new Set(await numbers()).size, { message: "badge, answer line and heading agree" }).toBe(1);
  expect((await numbers())[0], "more missions are unread than the fan-out reads: a lower bound says so").toMatch(/\+$/);
});

uxr({ t: "T48", uxr: ["UXR-049"], needs: [...NEEDS] }, "R3: when every mission has been read the number is exact, with no '+', in all three places", async ({ page }) => {
  await openApp(page, { auth: "in" }, "/");
  await expect(page.locator("[data-q=need-me]")).toHaveText("2 need you");
  await expect(page.locator(".main-nav .nav-count")).toHaveText(/^2\b(?!\+)/);
  await expect(page.locator('[data-section="needs-you"] h2')).toHaveText("Needs you (2)");
});

// ---- R4: the summary says what the rows are, not an instruction (UXR-050) ------------------------------------------------------------------------------
uxr({ t: "T48", uxr: ["UXR-050"], needs: [...NEEDS] }, "R4: the Inbox summary names the kinds that need you instead of 'Retry or open'", async ({ page }) => {
  await openApp(page, { auth: "in" }, "/");
  await expect(page.locator("[data-q=next]")).toHaveText("1 failed, 1 awaits approval");
  await expect(page.locator('[data-l0="inbox.answer"]')).not.toContainText(/Retry or open|Open to retry|Approve or reject/);
});

// ---- R5: the pill column is as wide as the pills (UXR-051) ----------------------------------------------------------------------------------------------
uxr({ t: "T48", uxr: ["UXR-051"], needs: [...NEEDS] }, "R5: at 1440 px an Inbox title starts close to its pill, and no pill runs into a title", async ({ page }, info) => {
  pw.skip(viewport(page).width < 1400, "the one-line row layout of a wide list");
  await openApp(page, DENSE, "/");
  await page.locator("summary", { hasText: "Waiting, paused and offline" }).click();
  const gaps = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>("[data-section] li.mrow")].map((li) => {
    const pill = li.querySelector<HTMLElement>(".pill")!.getBoundingClientRect();
    const title = li.querySelector<HTMLElement>(".row-title")!.getBoundingClientRect();
    return { section: li.closest("[data-section]")!.getAttribute("data-section")!, state: li.getAttribute("data-state")!, lead: Math.round(title.left - li.getBoundingClientRect().left), clear: title.left - pill.right };
  }));
  for (const g of gaps) expect(g.clear, `${g.state}: the pill ends before the title starts`).toBeGreaterThanOrEqual(4);
  expect(Math.max(...gaps.filter((g) => g.section === "needs-you").map((g) => g.lead)), "needs-you title offset in px").toBeLessThanOrEqual(175);
  expect(Math.max(...gaps.filter((g) => g.section === "running").map((g) => g.lead)), "running title offset in px").toBeLessThanOrEqual(150);
  await evidence(page, info, ["UXR-051"], "pill-column");
});

// ---- R6: the phone search button looks like the other header icons (UXR-052) --------------------------------------------------------------------------------
uxr({ t: "T48", uxr: ["UXR-052"], needs: [...NEEDS] }, "R6: on a phone the search button has no fill and no border at rest, like the account button", async ({ page }, info) => {
  pw.skip(!isPhone(page), "phone header");
  await openApp(page, { auth: "in" }, "/");
  await page.mouse.move(0, 400);
  const look = (sel: string) => page.locator(sel).evaluate((e) => { const s = getComputedStyle(e); return { bg: s.backgroundColor, border: s.borderTopColor }; });
  const [search, account] = [await look("#palette-open"), await look("#account-button")];
  expect(search.bg, "search fill equals the account button's").toBe(account.bg);
  expect(search.border, "search border equals the account button's").toBe(account.border);
  await evidence(page, info, ["UXR-052"], "phone-header");
});
