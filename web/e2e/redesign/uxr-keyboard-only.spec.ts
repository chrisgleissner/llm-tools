// UXR-046 (keyboard paths): every task of the inventory is completed on a desktop with the keyboard alone: Tab, Enter, Escape, arrows,
// the shortcuts of the `?` dialog and the command palette, plus text entry. The test fails if any step presses a pointer, and the
// outcome of each path is asserted exactly as in the pointer test.
import { test as pw } from "@playwright/test";
import { openPath, RUNS } from "./click-paths";
import { PATH_ROWS } from "./click-paths-data";
import { expect, uxr } from "./gate";
import { isPhone } from "./harness";

const NEEDS = ["WP-0", "WP-1", "WP-2", "WP-3", "WP-4", "WP-5"] as const;

for (const run of RUNS) {
  const row = PATH_ROWS.find((r) => r.id === run.id)!;
  uxr({ t: "T46", uxr: ["UXR-046"], needs: [...NEEDS] }, `${row.task} (${run.id}) is completed with the keyboard alone: ${row.keys}`, async ({ page }) => {
    pw.skip(isPhone(page), "the keyboard paths are for desktops; phones are covered by the pointer and reach tests");
    const ctx = await openPath(page, run);
    await run.keys(ctx);
    const used = await ctx.app.ix.log();
    expect(used.filter((k) => k === "pointer"), `pointer presses in the keyboard path of "${row.task}"`).toEqual([]);
  });
}
