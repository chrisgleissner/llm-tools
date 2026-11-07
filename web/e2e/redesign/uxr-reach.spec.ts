// UXR-046 (thumb reach): on a phone the primary control of every state is reachable by the thumb of the hand that holds the phone,
// without moving the grip. The measured rule: the centre of the control is in the lower 60% of the visible viewport (its y is at least
// 0.4 of the height). 60% of a 568 to 745 px tall screen is about 9 cm on a 6 inch phone, the arc a thumb sweeps from the lower corner
// of the grip (Hoober, "How do users really hold mobile devices?"). A control inside a scrolling list (an Inbox row) may be higher on
// first view, because the list scrolls it into the zone; the test then requires that the page can scroll far enough to do so.
import { test as pw } from "@playwright/test";
import { evidence } from "./evidence";
import { openPath, RUNS } from "./click-paths";
import { PATH_ROWS } from "./click-paths-data";
import { expect, uxr } from "./gate";
import { isPhone, viewport } from "./harness";

const NEEDS = ["WP-0", "WP-1", "WP-2", "WP-3", "WP-4", "WP-5"] as const;
export const REACH = 0.4;

for (const run of RUNS) {
  const row = PATH_ROWS.find((r) => r.id === run.id)!;
  uxr({ t: "T46", uxr: ["UXR-046"], needs: [...NEEDS] }, `${row.task} (${run.id}): the primary control is in the lower 60% of the screen, or a list row that scrolls into it`, async ({ page }, info) => {
    pw.skip(!isPhone(page), "thumb reach is for phones");
    const seen: Array<{ what: string; y: number; inList: boolean; scrollable: number }> = [];
    const ctx = await openPath(page, run, {
      primary: async (el, what) => {
        const box = (await el.boundingBox())!;
        const inList = await el.evaluate((n) => Boolean(n.closest("[data-section] li, [data-section] [data-entity]")));
        const scrollable = await page.evaluate(() => document.documentElement.scrollHeight - window.innerHeight - window.scrollY);
        seen.push({ what, y: box.y + box.height / 2, inList, scrollable });
      },
    });
    await run.run(ctx);
    const h = viewport(page).height;
    for (const s of seen) {
      const needed = REACH * h - s.y; // how far the control must move down to enter the zone
      const ok = s.y >= REACH * h || (s.inList && needed <= s.scrollable);
      expect(ok, `${s.what} is centred at ${Math.round(s.y)} px of ${h} px (${Math.round((s.y / h) * 100)}%); the zone starts at ${Math.round(REACH * h)} px${s.inList ? `; the list can scroll ${Math.round(s.scrollable)} px` : ""}`).toBe(true);
    }
    if (seen.length) await evidence(page, info, ["UXR-046"], `reach-${run.id}`);
  });
}
