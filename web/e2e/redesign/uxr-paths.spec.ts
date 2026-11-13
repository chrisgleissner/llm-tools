// UXR-043 and UXR-044: the click-path inventory, measured. Every operator task of click-paths-data.ts is performed with the
// pointer in the real application on the deterministic mock, and the interactions it takes (pointer presses, runs of typing,
// non-modifier key presses: interactions.ts) must equal the number in the inventory for the viewport: more is a regression, fewer
// means the inventory (and docs/distributed/ux-audit.md) has to be lowered. The documentation table is checked against the data
// by the static test at the end, so the document, the data and the measurement cannot drift apart.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { PATH_ROWS, renderTable, TABLE_BEGIN, TABLE_END } from "./click-paths-data";
import { openPath, RUNS } from "./click-paths";
import { expect, uxr, uxrStatic } from "./gate";
import { isPhone } from "./harness";

const NEEDS = ["WP-0", "WP-1", "WP-2", "WP-3", "WP-4", "WP-5"] as const;

for (const run of RUNS) {
  const row = PATH_ROWS.find((r) => r.id === run.id);
  if (!row) throw new Error(`click-paths.ts has a path "${run.id}" that click-paths-data.ts does not list`);
  uxr({ t: "T44", uxr: ["UXR-043", "UXR-044"], needs: [...NEEDS] }, `${row.task} (${run.id}): interactions equal the inventory`, async ({ page }, info) => {
    const ctx = await openPath(page, run);
    await run.run(ctx);
    const used = await ctx.app.ix.log();
    const phone = isPhone(page);
    const allowed = phone ? row.phone : row.desktop;
    if (process.env["UXR_PATHS_REPORT"] === "1") console.log(`PATHCOUNT ${run.id} ${info.project.name} ${used.length} ${used.join(",")}`);
    expect(allowed, `${run.id} has a count in the inventory for ${phone ? "phones" : "desktop"}`).not.toBeNull();
    expect(used.length, `interactions for "${row.task}" (${used.join(", ")}); the inventory allows ${allowed}`).toBeLessThanOrEqual(allowed!);
    expect(used.length, `"${row.task}" now takes fewer interactions (${used.join(", ")}) than the inventory says (${allowed}): lower the row in click-paths-data.ts and regenerate the table with node scripts/click-paths-doc.mjs`).toBe(allowed);
  });
}

uxrStatic({ t: "T44", uxr: ["UXR-043", "UXR-044"], needs: [] }, "every inventory row has a path that the tests perform, or says that the console does not offer the task", async () => {
  const ids = new Set(RUNS.map((r) => r.id));
  const missing = PATH_ROWS.filter((r) => r.desktop !== null && !ids.has(r.id)).map((r) => r.id);
  expect(missing, "inventory rows without a measured path").toEqual([]);
  const unavailable = PATH_ROWS.filter((r) => r.desktop === null || r.phone === null);
  for (const r of unavailable) expect(r.desktop === null && r.phone === null && r.path.startsWith("not offered"), `${r.id} is marked not offered on both sizes with a reason`).toBe(true);
});

uxrStatic({ t: "T44", uxr: ["UXR-043"], needs: [] }, "the table in docs/distributed/ux-audit.md is the one generated from click-paths-data.ts", async () => {
  const doc = readFileSync(join(import.meta.dirname, "../../../docs/distributed/ux-audit.md"), "utf8");
  const begin = doc.indexOf(TABLE_BEGIN);
  const end = doc.indexOf(TABLE_END);
  expect(begin, "the begin marker of the generated table is in ux-audit.md").toBeGreaterThanOrEqual(0);
  expect(end, "the end marker").toBeGreaterThan(begin);
  expect(doc.slice(begin, end + TABLE_END.length), "the table in ux-audit.md differs from the data: run node scripts/click-paths-doc.mjs in web/").toBe(renderTable());
});
