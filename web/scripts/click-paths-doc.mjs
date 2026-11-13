// Writes (or with --check verifies) the click-path table of docs/distributed/ux-audit.md from e2e/redesign/click-paths-data.ts.
// The data file has no imports, so Node strips its types and loads it directly.
//   node scripts/click-paths-doc.mjs          rewrite the table between the markers
//   node scripts/click-paths-doc.mjs --check  exit 1 when the document is out of date
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const { renderTable, TABLE_BEGIN, TABLE_END } = await import(join(here, "../e2e/redesign/click-paths-data.ts"));
const file = join(here, "../../docs/distributed/ux-audit.md");
const doc = readFileSync(file, "utf8");
const begin = doc.indexOf(TABLE_BEGIN);
const end = doc.indexOf(TABLE_END);
if (begin < 0 || end < begin) {
  console.error("ux-audit.md has no click-path table markers; add the begin and end marker lines where the table belongs");
  process.exit(1);
}
const next = doc.slice(0, begin) + renderTable() + doc.slice(end + TABLE_END.length);
if (process.argv.includes("--check")) {
  if (next !== doc) {
    console.error("docs/distributed/ux-audit.md is out of date: run node scripts/click-paths-doc.mjs in web/");
    process.exit(1);
  }
} else if (next !== doc) writeFileSync(file, next);
