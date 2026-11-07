// T08, T09 (UXR-003, UXR-015, UXR-016): one truncation rule, ids shortened in code.
import { test as pw } from "@playwright/test";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { LONG_MISSIONS } from "../mock";
import { expect, uxr, uxrStatic } from "./gate";
import { MISSION_ID, MISSION_URL, openApp } from "./harness";
import { needsFor, PAGES, type PageDef } from "./pages";

const WIDTHS = [{ width: 320, height: 568 }, { width: 412, height: 915 }, { width: 1440, height: 900 }];
const LONG_TITLE_START = "Migrate the entire legacy ingestion pipeline";

/** Pages that show long names: the long-names data set puts long titles, an unbroken slug and long worker names on them. */
const TEXT_PAGES: Array<PageDef & { long: string }> = PAGES
  .filter((p) => p.id !== "usage")
  .map((p) => ({ ...p, long: p.id === "mission" ? `/missions/desktop-workstation/${LONG_MISSIONS[0]!.mission.id}` : p.path, mock: { auth: "in", dataset: "long-names" } }));

for (const def of TEXT_PAGES) {
  for (const size of WIDTHS) {
    uxr({ t: "T08", uxr: ["UXR-003", "UXR-015"], needs: needsFor(def) }, `${def.id} at ${size.width}px: text is truncated at most once, short fields never wrap mid-word, titles clamp at two lines`, async ({ page }) => {
      await page.setViewportSize(size);
      await openApp(page, def.mock, def.long);
      await page.locator(def.ready).first().waitFor();
      const report = await page.evaluate((longTitle) => {
        const main = document.querySelector<HTMLElement>("main") ?? document.body;
        const truncating = (el: Element): boolean => {
          const s = getComputedStyle(el);
          const clamp = s.getPropertyValue("-webkit-line-clamp");
          return (s.textOverflow === "ellipsis" && s.overflowX !== "visible") || (clamp !== "" && clamp !== "none" && s.overflowY !== "visible");
        };
        const describe = (el: Element) => `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}${el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).join(".") : ""}`;
        const nested: string[] = [];
        for (const el of main.querySelectorAll("*")) {
          if (!truncating(el)) continue;
          for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) if (truncating(a)) { nested.push(`${describe(el)} inside ${describe(a)}`); break; }
        }
        // Leaf elements holding one token (worker, provider, state, time, count): must render on one line.
        const wrapped: string[] = [];
        const midWord: string[] = [];
        const lineHeight = (el: Element) => { const s = getComputedStyle(el); return parseFloat(s.lineHeight) || parseFloat(s.fontSize) * 1.3; };
        for (const el of main.querySelectorAll<HTMLElement>("*")) {
          if (el.children.length > 0 || el.closest("[hidden], .xterm, pre, code, .long-text, .sr-only")) continue;
          const text = (el.textContent ?? "").trim();
          const box = el.getBoundingClientRect();
          if (box.width === 0 || !text) continue;
          if (/^[\w.:+-]+$/.test(text) && text.length <= 30 && box.height > lineHeight(el) * 1.6) wrapped.push(`"${text}" wraps (${Math.round(box.height)}px high)`);
        }
        const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
          const parent = node.parentElement;
          if (!parent || parent.closest("[hidden], .xterm, pre, code, .long-text, .sr-only")) continue;
          const text = node.textContent ?? "";
          for (const m of text.matchAll(/[^\s​-]{2,30}/g)) {
            const range = document.createRange();
            range.setStart(node, m.index ?? 0);
            range.setEnd(node, (m.index ?? 0) + m[0].length);
            const rects = [...range.getClientRects()].filter((r) => r.width > 0);
            if (rects.length > 1 && new Set(rects.map((r) => Math.round(r.top))).size > 1) midWord.push(`"${m[0]}" is broken across lines`);
          }
        }
        // Titles: every clamped element clamps at two lines and is no taller than two lines.
        const clampProblems: string[] = [];
        for (const el of main.querySelectorAll<HTMLElement>("*")) {
          const clamp = getComputedStyle(el).getPropertyValue("-webkit-line-clamp");
          if (clamp && clamp !== "none" && clamp !== "2") clampProblems.push(`${describe(el)} clamps at ${clamp} lines`);
        }
        const titleEl = [...main.querySelectorAll<HTMLElement>("*")].filter((el) => el.children.length === 0 && (el.textContent ?? "").startsWith(longTitle) && el.getBoundingClientRect().width > 0)[0];
        const title = titleEl ? { height: titleEl.getBoundingClientRect().height, line: lineHeight(titleEl), full: titleEl.title || titleEl.closest("[title]")?.getAttribute("title") || "" } : null;
        return { nested, wrapped, midWord, clampProblems, title };
      }, LONG_TITLE_START);
      expect(report.nested, "an ellipsized or clamped element nested in another one").toEqual([]);
      expect(report.wrapped, "short fields that wrap").toEqual([]);
      expect(report.midWord, "words broken mid-word").toEqual([]);
      expect(report.clampProblems, "clamps other than two lines").toEqual([]);
      if (def.id !== "workers" && def.id !== "sessions") {
        expect(report.title, "the long title is on the page").not.toBeNull();
        expect(report.title!.height, "a long title is at most two lines high").toBeLessThanOrEqual(report.title!.line * 2 + 2);
        expect(report.title!.full, "the full title is available in the title attribute").toContain(LONG_TITLE_START);
      }
    });
  }
}

// ---- T09 ----------------------------------------------------------------------------------------------------------
uxrStatic({ t: "T09", uxr: ["UXR-003"], needs: ["WP-0"] }, "shortId keeps the first 6 and last 4 characters of ids longer than 12 characters", async () => {
  const mod = (await import(pathToFileURL(resolve(import.meta.dirname, "../../src/state.ts")).href)) as { shortId: (id: string) => string };
  expect(mod.shortId("0b0f6d3e-1c2a-4f5b-8d7e-123456789abc")).toBe("0b0f6d…9abc");
  expect(mod.shortId("0123456789ab")).toBe("0123456789ab");
  expect(mod.shortId("short")).toBe("short");
  expect(mod.shortId("0123456789abc")).toBe("012345…9abc");
});

for (const def of PAGES.filter((p) => p.id === "inbox" || p.id === "missions" || p.id === "sessions")) {
  uxr({ t: "T09", uxr: ["UXR-003", "UXR-016"], needs: needsFor(def) }, `${def.id}: list rows show no identifier`, async ({ page }) => {
    await openApp(page, def.mock, def.path);
    await page.locator(def.ready).first().waitFor();
    const ids = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>("[data-entity]")].map((e) => e.innerText.match(/\b[0-9a-f]{8,}(?:-[0-9a-f]{4,})*\b/i)?.[0] ?? "").filter(Boolean));
    expect(ids, "identifiers shown in list rows (ids belong in tooltips, detail blocks and Copy ID)").toEqual([]);
  });
}

uxr({ t: "T09", uxr: ["UXR-003"], needs: ["WP-0", "WP-1", "WP-4"] }, 'missions: the row menu "Copy ID" copies the full id', async ({ page, context, browserName }) => {
  pw.skip(browserName !== "chromium", "clipboard permissions need Chromium");
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await openApp(page, { auth: "in" }, "/missions");
  const row = page.locator(`[data-entity="mission:alpha/${MISSION_ID}"]`);
  await row.waitFor();
  await row.locator('button[aria-haspopup="menu"]').click();
  await page.getByRole("menuitem", { name: /copy id/i }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(MISSION_ID);
});

uxr({ t: "T09", uxr: ["UXR-003"], needs: ["WP-0", "WP-1", "WP-3"] }, "mission page: the About block shows a shortened id whose title holds the full id", async ({ page }) => {
  await openApp(page, { auth: "in" }, MISSION_URL);
  await page.locator("summary", { hasText: /about/i }).first().click();
  const el = page.locator(`[title="${MISSION_ID}"]`).first();
  await expect(el).toBeVisible();
  expect((await el.textContent())?.trim()).toBe(`${MISSION_ID.slice(0, 6)}…${MISSION_ID.slice(-4)}`);
});
