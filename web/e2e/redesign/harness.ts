// Shared helpers for the PRD-037 acceptance specs: open the real application on the deterministic mock, viewport
// classes and the fold budgets of the addendum (section 3.1), and small DOM probes used by several specs.
import AxeBuilder from "@axe-core/playwright";
import type { Locator, Page } from "@playwright/test";
import { expect } from "../fixtures";
import { MISSIONS, mockApp, type Mock, type MockOptions } from "../mock";
import { trackInteractions, type Interactions } from "./interactions";

export const IN: MockOptions = { auth: "in" };
export const MISSION_ID = MISSIONS[0]!.mission.id;
export const MISSION_URL = `/missions/alpha/${MISSION_ID}`;

export interface App extends Mock {
  ix: Interactions;
}

/** Mock the API, open `path` signed in and wait until the page has settled (network idle, fonts, two frames). */
export async function openApp(page: Page, opts: MockOptions = IN, path = "/"): Promise<App> {
  const ix = await trackInteractions(page);
  const mock = await mockApp(page, { ...IN, ...opts });
  await page.goto(path);
  await settle(page);
  await ix.reset();
  return { ...mock, ix };
}

export async function settle(page: Page): Promise<void> {
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.evaluate(() => document.fonts.ready.then(() => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r())))));
}

export const viewport = (page: Page) => page.viewportSize() ?? { width: 1440, height: 900 };
export const isPhone = (page: Page): boolean => viewport(page).width < 768;
export const isLandscapePhone = (page: Page): boolean => viewport(page).width > viewport(page).height && viewport(page).height <= 512;

/** Fold bucket of the addendum table; null for viewports it does not list (landscape phones, 1024x768). */
export type FoldClass = "xxl" | "xl" | "lg" | "md" | "sm" | "xs";
export function foldClass(page: Page): FoldClass | null {
  const { width: w, height: h } = viewport(page);
  if (w >= 2000) return "xxl";
  if (w >= 1400) return "xl";
  if (w >= 1200) return "lg";
  if (w >= 700 && w <= 800 && h > w) return "md";
  if (w >= 400 && w <= 440 && h > w) return "sm";
  if (w < 400 && h >= w) return "xs";
  return null;
}

/** Fold line: the viewport height minus a bottom navigation bar when one is fixed to the bottom edge. */
export async function foldLine(page: Page): Promise<number> {
  return page.evaluate(() => {
    let line = window.innerHeight;
    for (const nav of document.querySelectorAll<HTMLElement>("nav")) {
      const r = nav.getBoundingClientRect();
      const fixed = getComputedStyle(nav).position === "fixed" || getComputedStyle(nav).position === "sticky";
      if (fixed && r.width > window.innerWidth * 0.6 && r.top > window.innerHeight / 2 && r.height > 0) line = Math.min(line, r.top);
    }
    return line;
  });
}

/**
 * Minimum level-0 rows per page, section and fold class (addendum section 3.1, "up to N" capped by what the fixture holds).
 * Row sections are the `<section>` part of `[data-l0="<page>.<section>-row"]`.
 */
export const FOLD: Record<string, Partial<Record<FoldClass, Record<string, number>>>> = {
  inbox: { xxl: { "needs-you": 6, running: 5 }, xl: { "needs-you": 6, running: 5 }, lg: { "needs-you": 5, running: 3 }, md: { "needs-you": 6, running: 4 }, sm: { "needs-you": 3 }, xs: { "needs-you": 2 } },
  missions: { xxl: { mission: 28 }, xl: { mission: 14 }, lg: { mission: 10 }, md: { mission: 12 }, sm: { mission: 8 }, xs: { mission: 4 } },
  sessions: { xxl: { session: 12 }, xl: { session: 12 }, lg: { session: 9 }, md: { session: 12 }, sm: { session: 7 }, xs: { session: 4 } },
  workers: { xxl: { worker: 10 }, xl: { worker: 10 }, lg: { worker: 6 }, md: { worker: 8 }, sm: { worker: 5 }, xs: { worker: 3 } },
  usage: { xxl: { provider: 6 }, xl: { provider: 6 }, lg: { provider: 4 }, md: { provider: 6 }, sm: { provider: 3 }, xs: { provider: 1 } },
};

/** Rows of `section` as the page marks them: `[data-l0="<page>.<section>-row"]`. */
export const l0Rows = (page: Page, pageId: string, section: string): Locator => page.locator(`[data-l0="${pageId}.${section}-row"]`);

/** True when the whole box of the element is inside [0, line] vertically and the viewport horizontally. */
export async function insideFold(el: Locator, line: number): Promise<boolean> {
  const box = await el.boundingBox();
  if (!box) return false;
  const vw = await el.page().evaluate(() => window.innerWidth);
  return box.y >= -0.5 && box.y + box.height <= line + 0.5 && box.x >= -0.5 && box.x + box.width <= vw + 0.5;
}

/** Rendered pill elements of one entity container. */
export const pillsIn = (root: Locator): Locator => root.locator(".pill");

/** Open a segmented tab of the mission page ("Tasks", "Activity", "Session") whatever role the control has. */
export async function openTab(page: Page, name: "Session" | "Tasks" | "Activity"): Promise<void> {
  const pattern = new RegExp(`^${name}\\b`, "i");
  for (const role of ["tab", "button", "link"] as const) {
    const control = page.getByRole(role, { name: pattern }).first();
    if (await control.isVisible().catch(() => false)) {
      await control.click();
      return;
    }
  }
  throw new Error(`no visible "${name}" tab on the mission page`);
}

/** The element that currently has focus, as a short description (for failure messages). */
export const activeElement = (page: Page): Promise<string> => page.evaluate(() => { const a = document.activeElement; return a ? `${a.tagName.toLowerCase()}${a.id ? "#" + a.id : ""}` : "none"; });

/** Names of the elements whose box is obscured by a fixed or sticky element at the point of their centre. */
export async function obscuredFocus(page: Page): Promise<string | null> {
  return page.evaluate(() => {
    const a = document.activeElement as HTMLElement | null;
    if (!a || a === document.body) return null;
    const r = a.getBoundingClientRect();
    if (r.width === 0) return null;
    const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    if (!top || a.contains(top) || top.contains(a)) return null;
    return `${a.tagName.toLowerCase()} covered by ${top.tagName.toLowerCase()}.${top.className}`;
  });
}

/** Simulate an on-screen keyboard that covers `px` of the viewport height: the layout viewport shrinks (interactive-widget=resizes-content). */
export async function openKeyboard(page: Page, px = 300): Promise<void> {
  const { width, height } = viewport(page);
  await page.setViewportSize({ width, height: height - px });
}
export async function closeKeyboard(page: Page, original: { width: number; height: number }): Promise<void> {
  await page.setViewportSize(original);
}

/** axe violations (rule id, impact, selectors) in both colour schemes; transitions are frozen first so contrast is measured at rest. */
export async function axeViolations(page: Page, label: string): Promise<string[]> {
  await page.addStyleTag({ content: "*, *::before, *::after { transition: none !important; animation: none !important; }" });
  const out: string[] = [];
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.evaluate((s) => document.documentElement.setAttribute("data-theme", s), scheme);
    await page.evaluate(() => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r()))));
    const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]).analyze();
    for (const v of result.violations) out.push(`${label} (${scheme}) ${v.impact} ${v.id}: ${v.help} -> ${v.nodes.map((n) => n.target.join(" ")).slice(0, 4).join(" | ")}`);
  }
  await page.evaluate(() => document.documentElement.removeAttribute("data-theme"));
  return out;
}

export { expect };
