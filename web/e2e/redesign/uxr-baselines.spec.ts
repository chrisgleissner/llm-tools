// T42 (UXR-038, UXR-030): every screen listed in e2e/screens/*.ts has a visual baseline in light and dark on the two Chromium
// projects. The screens are rendered and compared by e2e/screenshots.spec.ts (one runner, so one set of baseline files);
// this spec checks the lists themselves: they are found, names are unique, each screen has its baseline files, and the Inbox
// and Mission detail lists cover the empty, sparse, dense and error states.
import { existsSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { expect, uxrStatic } from "./gate";

interface ScreenLike { name: string; narrowOnly?: boolean; wideOnly?: boolean }
const dir = join(import.meta.dirname, "..", "screens");
const baselines = join(import.meta.dirname, "..", "__screenshots__");

/** Every `e2e/screens/<package>.ts` (not index.ts, types.ts) with the screens it exports as `SCREENS`. */
async function loadScreens(): Promise<Array<{ pkg: string; screens: ScreenLike[] }>> {
  if (!existsSync(dir)) return [];
  const out: Array<{ pkg: string; screens: ScreenLike[] }> = [];
  for (const file of readdirSync(dir).filter((f) => f.endsWith(".ts") && f !== "index.ts" && f !== "types.ts").sort()) {
    const mod = (await import(pathToFileURL(join(dir, file)).href)) as { SCREENS?: ScreenLike[] };
    out.push({ pkg: file.replace(/\.ts$/, ""), screens: mod.SCREENS ?? [] });
  }
  return out;
}

uxrStatic({ t: "T42", uxr: ["UXR-038"], needs: ["WP-0"] }, "e2e/screens/*.ts export SCREENS, names are unique, and the lists are not empty", async () => {
  const lists = await loadScreens();
  const all = lists.flatMap((l) => l.screens.map((s) => s.name));
  expect(lists.length, "screen lists found in web/e2e/screens (each exports SCREENS)").toBeGreaterThan(0);
  expect(all.length, "screens found in those lists").toBeGreaterThan(0);
  for (const l of lists) expect(l.screens.length, `${l.pkg}.ts exports at least one screen in SCREENS`).toBeGreaterThan(0);
  expect(all.filter((n, i) => all.indexOf(n) !== i), "screen names that appear twice").toEqual([]);
});

uxrStatic({ t: "T42", uxr: ["UXR-038", "UXR-030"], needs: ["WP-0"] }, "every screen has a baseline in light and dark for the Chromium projects it runs on", async () => {
  const screens = (await loadScreens()).flatMap((l) => l.screens);
  expect(screens.length, "screens found").toBeGreaterThan(0);
  const missing: string[] = [];
  for (const [project, narrow] of [["chromium-desktop", false], ["chromium-mobile", true]] as const)
    for (const s of screens) {
      if ((s.narrowOnly && !narrow) || (s.wideOnly && narrow)) continue;
      for (const theme of ["light", "dark"]) if (!existsSync(join(baselines, project, `${s.name}-${theme}.png`))) missing.push(`${project}/${s.name}-${theme}.png`);
    }
  expect(missing, "baseline files missing (regenerate in the Playwright image, web/README.md)").toEqual([]);
});

// The Inbox and Mission detail each need empty, sparse, dense and error variants. A variant is recognised by the screen name.
const VARIANT: Record<string, RegExp> = { empty: /(^|-)(empty|none|no-)/, sparse: /sparse/, dense: /dense/, error: /error/ };
for (const [pkg, wp] of [["inbox", "WP-4"], ["mission", "WP-3"]] as const) {
  uxrStatic({ t: "T42", uxr: ["UXR-038"], needs: ["WP-0", wp] }, `${pkg} screens cover the empty, sparse, dense and error states`, async () => {
    const entry = (await loadScreens()).find((l) => l.pkg === pkg);
    expect(entry, `e2e/screens/${pkg}.ts exists`).toBeDefined();
    const names = entry!.screens.map((s) => s.name);
    for (const [variant, pattern] of Object.entries(VARIANT)) expect(names.some((n) => pattern.test(n)), `a ${pkg} screen for the ${variant} state (names: ${names.join(", ")})`).toBe(true);
  });
}
