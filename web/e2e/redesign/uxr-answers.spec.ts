// T43 (UXR-036, UXR-024): every page answers what is happening, does it need me, what next, inside the fold.
import { evidence } from "./evidence";
import { expect, uxr } from "./gate";
import { foldLine, insideFold, openApp } from "./harness";
import { needsFor, PAGES, type PageDef } from "./pages";

const EXTRA: PageDef[] = [
  { id: "usage", path: "/diagnostics", mock: { auth: "in" }, wp: "WP-5", ready: "main h1" },
  { id: "usage", path: "/account", mock: { auth: "in" }, wp: "WP-5", ready: "main h1" },
];

for (const def of [...PAGES, ...EXTRA]) {
  uxr({ t: "T43", uxr: ["UXR-036", "UXR-024"], needs: needsFor(def) }, `${def.path}: the what, need-me and next answers are inside the fold`, async ({ page }, info) => {
    await openApp(page, def.mock, def.path);
    await page.locator(def.ready).first().waitFor();
    const line = await foldLine(page);
    for (const q of ["what", "need-me", "next"]) {
      const el = page.locator(`[data-q="${q}"]`).first();
      await expect(el, `[data-q=${q}]`).toBeVisible();
      expect((await el.innerText()).trim().length, `[data-q=${q}] has text`).toBeGreaterThan(0);
      expect(await insideFold(el, line), `[data-q=${q}] is inside the fold`).toBe(true);
    }
    await evidence(page, info, ["UXR-036"], `answers-${def.path.replace(/\W+/g, "-") || "inbox"}`);
  });
}
