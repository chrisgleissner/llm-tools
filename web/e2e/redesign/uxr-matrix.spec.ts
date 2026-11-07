// T18, T29 to T35 (UXR-024 to UXR-027, UXR-029 to UXR-031, UXR-037, UXR-045): the device matrix.
// These specs read the viewport of the Playwright project they run in, so they cover the four existing projects and the
// nine matrix projects of playwright.config.ts (2560x1440, 1280x720, 768x1024, 1024x768, 430x932, 360x640, 320x568 and
// the landscape phones 740x360 and 568x320). Viewports the addendum table does not list skip the fold budget (reported
// as skipped with the reason, never silently).
import { test as pw } from "@playwright/test";
import { evidence } from "./evidence";
import { expect, uxr } from "./gate";
import { axeViolations, reflowProblems, FOLD, foldClass, foldLine, insideFold, isLandscapePhone, isPhone, l0Rows, MISSION_URL, obscuredFocus, openApp, openKeyboard, viewport } from "./harness";
import { needsFor, PAGES } from "./pages";

const rowHeightOf = async (page: import("@playwright/test").Page): Promise<number> => page.evaluate(() => document.querySelector<HTMLElement>("#terminal-host .xterm-rows > div")?.offsetHeight ?? 16);
/** Terminal rows the visible part of the session pane can hold. */
async function paneRows(page: import("@playwright/test").Page): Promise<number> {
  const box = await page.locator('[data-pane="session"]').boundingBox();
  if (!box) return 0;
  const vh = viewport(page).height;
  return (Math.min(box.y + box.height, vh) - Math.max(box.y, 0)) / (await rowHeightOf(page));
}

/** Terminal rows (without the 8 px padding of the host) that are above the fold: the usable height of the terminal itself, not of the pane around it. */
async function terminalRowsAboveFold(page: import("@playwright/test").Page, line: number): Promise<number> {
  const box = await page.locator("#terminal-host").boundingBox();
  if (!box) return 0;
  const visible = Math.min(box.y + box.height, line) - Math.max(box.y, 0);
  return Math.max(0, visible - 16) / (await rowHeightOf(page));
}

// ---- T18: the Inbox queue is first and its action is above the fold -----------------------------------------------
uxr({ t: "T18", uxr: ["UXR-012", "UXR-024"], needs: ["WP-0", "WP-1", "WP-4"] }, "the first Needs you row and its primary action are fully above the fold, ahead of Running", async ({ page }, info) => {
  const app = await openApp(page, { auth: "in", dataset: "dense" }, "/");
  const row = page.locator('[data-section="needs-you"] [data-entity^="mission:"]').first();
  await expect(row).toBeVisible();
  const line = await foldLine(page);
  expect(await insideFold(row, line), "the first needs-you row is inside the fold").toBe(true);
  const action = row.locator("[data-action]").first();
  await expect(action).toBeVisible();
  expect(await insideFold(action, line), "its primary action is inside the fold").toBe(true);
  const order = await page.evaluate(() => {
    const needs = document.querySelector('[data-section="needs-you"]');
    const running = document.querySelector('[data-section="running"]');
    return !!needs && !!running && !!(needs.compareDocumentPosition(running) & Node.DOCUMENT_POSITION_FOLLOWING);
  });
  expect(order, "Needs you precedes Running in the document").toBe(true);
  expect(await app.ix.count(), "interactions needed").toBe(0);
  await evidence(page, info, ["UXR-012", "UXR-024"], "inbox-fold");
});

// ---- T29: fold budgets --------------------------------------------------------------------------------------------
for (const def of PAGES) {
  uxr({ t: "T29", uxr: ["UXR-024"], needs: needsFor(def) }, `${def.id}: every level-0 element ends above the fold and the row budget of the addendum is met`, async ({ page }, info) => {
    const cls = foldClass(page);
    pw.skip(cls === null, "this viewport is not in the addendum's fold table");
    await openApp(page, def.mock, def.path);
    await page.locator(def.ready).first().waitFor();
    const line = await foldLine(page);
    const l0 = page.locator("[data-l0]");
    const names = await l0.evaluateAll((els) => els.map((e) => e.getAttribute("data-l0") ?? ""));
    expect(names.length, "the page marks its level-0 elements with data-l0").toBeGreaterThan(0);
    const outside: string[] = [];
    for (const [i, name] of names.entries()) {
      if (/(^|[.-])row$/.test(name)) continue; // repeated list rows: counted below, not bounded
      const el = l0.nth(i);
      if (!(await el.isVisible())) continue;
      if (!(await insideFold(el, line))) outside.push(name);
    }
    expect(outside, `level-0 elements that end below the fold (${line}px)`).toEqual([]);
    const budget = FOLD[def.id]?.[cls!];
    for (const [section, need] of Object.entries(budget ?? {})) {
      const rows = l0Rows(page, def.id, section);
      const total = await rows.count();
      let inside = 0;
      for (let i = 0; i < total; i += 1) if (await insideFold(rows.nth(i), line)) inside += 1;
      expect(inside, `${def.id} ${section} rows above the fold (budget ${need}, page has ${total}) at ${viewport(page).width}x${viewport(page).height}`).toBeGreaterThanOrEqual(Math.min(need, total));
    }
    if (def.id === "mission") {
      const need = { xxl: 60, xl: 40, lg: 30, md: 38, sm: 20, xs: viewport(page).height >= 640 ? 14 : 12 }[cls!];
      expect(await paneRows(page), "terminal rows above the fold").toBeGreaterThanOrEqual(need - 1);
      // The pane rows above include the pane's own chrome (toolbar, composer, key bar). On the two smallest phones the header used to take so much
      // that the terminal itself kept a sliver; it must keep at least six rows of its own, above the fold, in the Needs approval state.
      if (cls === "xs") expect(await terminalRowsAboveFold(page, line), `terminal rows of the terminal itself above the fold at ${viewport(page).width}x${viewport(page).height}`).toBeGreaterThanOrEqual(6);
    }
    await evidence(page, info, ["UXR-024", "UXR-037"], `fold-${def.id}`);
  });
}

// ---- T30: no horizontal scroll -------------------------------------------------------------------------------------
const ROUTES: Array<[string, string]> = [["inbox", "/"], ["missions", "/missions"], ["mission", MISSION_URL], ["sessions", "/sessions"], ["workers", "/workers"], ["usage", "/usage"], ["diagnostics", "/diagnostics"], ["account", "/account"]];
for (const [name, path] of ROUTES) {
  uxr({ t: "T30", uxr: ["UXR-025", "UXR-029"], needs: [] }, `${name}: the page never scrolls horizontally`, async ({ page }) => {
    await openApp(page, { auth: "in", terminalScreen: true }, path);
    await expect(page.locator("main h1, main h2, main [data-q]").first()).toBeVisible();
    const over = await page.evaluate(() => {
      const offenders = [...document.querySelectorAll<HTMLElement>("body *")].filter((e) => !e.closest(".xterm, pre, [data-scroll-x], .diff") && e.getBoundingClientRect().right > window.innerWidth + 1 && e.offsetWidth > 0).slice(0, 5).map((e) => `${e.tagName.toLowerCase()}.${e.className}`);
      return { overflow: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - window.innerWidth, offenders };
    });
    expect(over.overflow, `horizontal overflow in px (elements past the right edge: ${over.offenders.join(", ")})`).toBeLessThanOrEqual(0);
  });
}

// ---- T31: target sizes ---------------------------------------------------------------------------------------------
uxr({ t: "T31", uxr: ["UXR-027"], needs: [] }, "interactive elements are at least 44 px on coarse pointers and 24 px on fine pointers", async ({ page }, info) => {
  await openApp(page, { auth: "in" }, "/missions");
  await expect(page.locator("main h1, main h2").first()).toBeVisible();
  const small = await page.evaluate(() => {
    const coarse = matchMedia("(pointer: coarse)").matches;
    const min = coarse ? 44 : 24;
    const sel = "button, a[href], input:not([type=hidden]), select, textarea, summary, [role=button], [role=tab], [role=menuitem], [role=option]";
    const all = [...document.querySelectorAll<HTMLElement>(sel)].filter((el) => {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return r.width > 1 && r.height > 1 && s.visibility !== "hidden" && !el.closest("[hidden], .sr-only, .xterm") && !el.classList.contains("skip");
    });
    const inline = (el: HTMLElement) => el.matches("a") && !!el.closest("p, li, span, td, th, dd") && !el.closest("nav, .tabbar, .side, button, [role=tab]") && el.getBoundingClientRect().height < 30 && getComputedStyle(el).display === "inline";
    const boxes = all.map((el) => ({ el, r: el.getBoundingClientRect() }));
    const out: string[] = [];
    for (const { el, r } of boxes) {
      if (el instanceof HTMLInputElement && el.type === "checkbox" && (el.closest("label")?.getBoundingClientRect().height ?? 0) >= min) continue;
      if (inline(el)) continue;
      if (r.width >= min - 0.5 && r.height >= min - 0.5) continue;
      if (!coarse) { // WCAG 2.5.8 spacing exception: a 24 px circle centred on the target touches no other target
        const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        const clear = boxes.every(({ el: other, r: o }) => other === el || o.right < cx - 12 || o.left > cx + 12 || o.bottom < cy - 12 || o.top > cy + 12);
        if (clear) continue;
      }
      out.push(`${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""} "${(el.getAttribute("aria-label") ?? el.textContent ?? "").trim().slice(0, 24)}" ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
    return { coarse, out };
  });
  expect(small.out, `targets below ${small.coarse ? 44 : 24} px`).toEqual([]);
  await evidence(page, info, ["UXR-027"], "targets-missions");
});

// ---- T32: reflow -------------------------------------------------------------------------------------------------
const TEXT_SPACING = "* { line-height: 1.5 !important; letter-spacing: 0.12em !important; word-spacing: 0.16em !important; } p { margin-block-end: 2em !important; }";
for (const def of PAGES) {
  uxr({ t: "T32", uxr: ["UXR-026"], needs: needsFor(def) }, `${def.id}: reflows at 320x225 (400%), with 200% text, with text spacing and in both orientations`, async ({ page }, info) => {
    await openApp(page, def.mock, def.path);
    await page.locator(def.ready).first().waitFor();
    const problems = () => reflowProblems(page);
    await page.setViewportSize({ width: 320, height: 225 });
    expect(await problems(), "400% zoom (320x225)").toEqual([]);
    await evidence(page, info, ["UXR-026"], `reflow-400-${def.id}`);
    await page.setViewportSize({ width: 360, height: 640 });
    await page.addStyleTag({ content: "html { font-size: 200% !important; }" });
    expect(await problems(), "200% text size").toEqual([]);
    await page.addStyleTag({ content: TEXT_SPACING });
    expect(await problems(), "WCAG 1.4.12 text spacing").toEqual([]);
    for (const size of [{ width: 568, height: 320 }, { width: 320, height: 568 }]) {
      await page.setViewportSize(size);
      expect(await problems(), `orientation ${size.width}x${size.height}`).toEqual([]);
    }
    if (def.id === "mission") {
      await page.setViewportSize({ width: 320, height: 225 });
      for (const action of ["pause", "stop"]) {
        const el = page.locator(`[data-action="${action}"]`).first();
        if (await el.count()) { await el.scrollIntoViewIfNeeded(); await expect(el, `${action} reachable at 400%`).toBeVisible(); }
      }
      const composer = page.locator("[data-composer]");
      await composer.scrollIntoViewIfNeeded();
      await expect(composer, "composer operable at 400%").toBeVisible();
    }
  });
}

// ---- T33: axe -----------------------------------------------------------------------------------------------------
for (const def of PAGES) {
  uxr({ t: "T33", uxr: ["UXR-031", "UXR-030"], needs: needsFor(def) }, `${def.id}: axe finds no violations in either theme, also with the palette and the page's overlays open`, async ({ page }) => {
    await openApp(page, def.mock, def.path);
    await page.locator(def.ready).first().waitFor();
    const found: string[] = await axeViolations(page, `${def.id} page`);
    await page.keyboard.press("Control+k");
    await page.getByRole("dialog").waitFor({ timeout: 10_000 }).catch(() => undefined); // the palette is a lazy chunk
    if (await page.getByRole("dialog").isVisible().catch(() => false)) { found.push(...(await axeViolations(page, `${def.id} palette`))); await page.keyboard.press("Escape"); }
    if (def.id === "inbox" || def.id === "mission") {
      const approve = page.locator('[data-action="approve"]').first();
      if (await approve.isVisible().catch(() => false)) {
        await approve.click();
        found.push(...(await axeViolations(page, `${def.id} approval dialog`)));
        await page.keyboard.press("Escape");
      }
    }
    if (def.id === "mission") {
      await page.locator('[data-action="stop"]').first().click();
      found.push(...(await axeViolations(page, "stop confirmation")));
      await page.keyboard.press("Escape");
      const trigger = page.locator('[data-entity^="task:"] button[aria-haspopup="menu"]').first();
      if (await trigger.isVisible().catch(() => false)) { await trigger.click(); found.push(...(await axeViolations(page, "task menu"))); await page.keyboard.press("Escape"); }
      const expand = page.getByRole("button", { name: /expand|full screen/i }).first();
      if (await expand.isVisible().catch(() => false)) { await expand.click(); found.push(...(await axeViolations(page, "full-screen terminal"))); await page.keyboard.press("Escape"); }
    }
    expect(found, "axe violations").toEqual([]);
  });
}

// ---- T34: keyboard simulation (phones) ---------------------------------------------------------------------------
uxr({ t: "T34", uxr: ["UXR-006", "UXR-045"], needs: ["WP-0", "WP-1", "WP-2", "WP-3"] }, "with a 300 px keyboard open the composer is visible, nothing focused is covered, the bottom bar is hidden and enough terminal rows remain", async ({ page }, info) => {
  pw.skip(!isPhone(page) || isLandscapePhone(page), "keyboard simulation is for portrait phones");
  await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
  await page.locator('[data-pane="session"]').waitFor();
  await page.locator('[data-action="take-control"]').click();
  const visibleHeight = viewport(page).height - 300; // openKeyboard shrinks the viewport, so read the height first
  await openKeyboard(page, 300);
  const field = page.locator("[data-composer]").locator("textarea, input").first();
  await field.focus();
  await page.waitForTimeout(150);
  const vh = visibleHeight;
  const box = (await page.locator("[data-composer]").boundingBox())!;
  expect(box.y >= -0.5 && box.y + box.height <= vh + 0.5, "composer inside the visible part of the screen").toBe(true);
  expect(await obscuredFocus(page), "the focused element is covered by something").toBeNull();
  await expect(page.locator("body")).toHaveAttribute("data-kb", /.*/);
  const barVisible = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>("nav")].some((n) => getComputedStyle(n).position === "fixed" && n.getBoundingClientRect().top > window.innerHeight / 2 && n.offsetHeight > 0));
  expect(barVisible, "the bottom bar is hidden while typing").toBe(false);
  const need = viewport(page).width >= 400 ? 10 : viewport(page).height >= 640 ? 8 : 6;
  expect(await paneRows(page), "terminal rows with the keyboard open").toBeGreaterThanOrEqual(need - 0.5);
  await evidence(page, info, ["UXR-045", "UXR-006"], "keyboard-open");
});

// ---- T35: navigation -----------------------------------------------------------------------------------------------
uxr({ t: "T35", uxr: ["UXR-029", "UXR-042"], needs: ["WP-0", "WP-1"] }, "the five bottom bar labels are full words and are not clipped", async ({ page }, info) => {
  pw.skip(!isPhone(page), "the bottom bar exists below 768 px");
  await openApp(page, { auth: "in" }, "/");
  const bar = page.getByRole("navigation", { name: "Main" });
  const labels = await bar.getByRole("link").allInnerTexts();
  expect(labels.map((l) => l.trim()), "bottom bar labels").toEqual(["Inbox", "Missions", "Sessions", "Workers", "Usage"]);
  const clipped = await bar.evaluate((nav) => [nav, ...nav.querySelectorAll<HTMLElement>("a, a *")].filter((e) => (e as HTMLElement).scrollWidth > (e as HTMLElement).clientWidth + 0.5).map((e) => (e.textContent ?? "").trim()));
  expect(clipped, "clipped bottom bar elements").toEqual([]);
  await evidence(page, info, ["UXR-029"], "bottom-bar");
});

/** Header elements whose text is cut off: wider than their own box, or sticking out of the header, the viewport or an ancestor that clips. */
async function clippedTopBarText(page: import("@playwright/test").Page): Promise<string[]> {
  return page.evaluate(() => {
    const header = document.querySelector<HTMLElement>("header.top")!;
    const out: string[] = [];
    for (const e of header.querySelectorAll<HTMLElement>("*")) {
      if (![...e.childNodes].some((n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? "").trim() !== "")) continue;
      const r = e.getBoundingClientRect();
      if (r.width <= 2 || r.height <= 2) continue; // visually hidden text (screen reader only) or display: none
      const name = `${e.tagName.toLowerCase()}.${String(e.className)} "${(e.textContent ?? "").trim().slice(0, 24)}"`;
      const cs = getComputedStyle(e);
      if (cs.overflowX !== "visible" && e.scrollWidth > e.clientWidth + 0.5) out.push(`${name} is wider than its box`);
      const box = [{ left: 0, right: window.innerWidth, what: "the viewport" }, { left: header.getBoundingClientRect().left, right: header.getBoundingClientRect().right, what: "the header" }];
      for (let a = e.parentElement; a && a !== header; a = a.parentElement) {
        if (getComputedStyle(a).overflowX !== "visible") box.push({ left: a.getBoundingClientRect().left, right: a.getBoundingClientRect().right, what: `.${String(a.className)}` });
      }
      for (const b of box) if (r.left < b.left - 0.5 || r.right > b.right + 0.5) out.push(`${name} sticks out of ${b.what}`);
    }
    return out;
  });
}

uxr({ t: "T35", uxr: ["UXR-029", "UXR-042"], needs: ["WP-0", "WP-1"] }, "no top bar text is clipped, at the project viewport and at 320x568", async ({ page }) => {
  const sizes = [viewport(page), { width: 320, height: 568 }];
  await openApp(page, { auth: "in" }, "/");
  for (const size of sizes) {
    await page.setViewportSize(size);
    await page.waitForTimeout(150);
    if (!isLandscapePhone(page)) await expect(page.locator("#capacity .cap-chip").first(), `capacity chip at ${size.width}x${size.height}`).toBeVisible();
    expect(await clippedTopBarText(page), `clipped top bar text at ${size.width}x${size.height}`).toEqual([]);
  }
});

uxr({ t: "T35", uxr: ["UXR-029"], needs: ["WP-0", "WP-1", "WP-4"] }, "the last list item is not covered by the bottom bar after scrolling to the end", async ({ page }) => {
  pw.skip(!isPhone(page), "the bottom bar exists below 768 px");
  await openApp(page, { auth: "in", dataset: "dense" }, "/missions");
  await page.locator('[data-entity^="mission:"]').first().waitFor();
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await page.waitForTimeout(150);
  const rows = page.locator('[data-entity^="mission:"]:visible');
  const last = rows.last();
  const line = await foldLine(page);
  const box = (await last.boundingBox())!;
  expect(box.y + box.height, "bottom of the last row against the top of the bottom bar").toBeLessThanOrEqual(line + 0.5);
});
