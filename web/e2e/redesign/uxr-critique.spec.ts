// T46 (UXR-040): the fresh-eyes critique in docs/distributed/ux-audit.md cites an existing screenshot for every page at
// 1440 and 360 widths in both themes. The lead writes the critique; this test fixes the format it must have: a line
// (list item or table row) in the section headed "critique" that names the page, the width (1440 or 360), the theme
// (light or dark) and a path to a .png file that exists in the repository.
import { existsSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { expect, uxrStatic } from "./gate";

const repo = resolve(import.meta.dirname, "../../..");
const PAGES = ["inbox", "missions", "mission", "sessions", "workers", "usage", "diagnostics", "account", "sign-in"];

uxrStatic({ t: "T46", uxr: ["UXR-040"], needs: ["lead"] }, "the critique lists an existing screenshot for every page, width (1440, 360) and theme", async () => {
  const audit = readFileSync(join(repo, "docs", "distributed", "ux-audit.md"), "utf8");
  const start = audit.search(/^#{1,4}\s.*critique/im);
  expect(start, 'ux-audit.md has a section whose heading contains "critique"').toBeGreaterThanOrEqual(0);
  const rest = audit.slice(start);
  const next = rest.slice(1).search(/^#{1,2}\s/m);
  const section = next < 0 ? rest : rest.slice(0, next + 1);
  const lines = section.split("\n").filter((l) => /\.png/.test(l));
  const missing: string[] = [];
  for (const page of PAGES) for (const width of ["1440", "360"]) for (const theme of ["light", "dark"]) {
    const ok = lines.some((line) => {
      const lower = line.toLowerCase();
      if (!lower.includes(page) || !line.includes(width) || !lower.includes(theme)) return false;
      return [...line.matchAll(/([\w./-]+\.png)/g)].some((m) => existsSync(join(repo, m[1]!)));
    });
    if (!ok) missing.push(`${page} ${width} ${theme}`);
  }
  expect(missing, "pages, widths and themes without an existing screenshot in the critique").toEqual([]);
});
