// T42 (UXR-038, UXR-030): visual baselines for every screen listed in e2e/screens/*.ts, in light and dark, on the two Chromium
// projects (desktop and Pixel 4). Baselines are generated in the Playwright image (web/README.md), like screenshots.spec.ts,
// and are skipped when BRIDGE_SKIP_VISUAL=1 or the platform is not Linux.
import { test as base, expect } from "@playwright/test";
import { existsSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { test } from "../fixtures";
import { mockApp } from "../mock";
import { isLanded, uxr, type Wp } from "./gate";
import { settle } from "./harness";
import type { ScreenDef } from "./screens";

const dir = join(import.meta.dirname, "..", "screens");
const PACKAGE_OF: Record<string, Wp> = { shell: "WP-1", session: "WP-2", mission: "WP-3", inbox: "WP-4", fleet: "WP-5", auth: "WP-5" };

const files = existsSync(dir) ? readdirSync(dir).filter((f) => f.endsWith(".ts")) : [];
const loaded: Array<{ pkg: string; wp: Wp; screens: ScreenDef[] }> = [];
for (const file of files) {
  const pkg = file.replace(/\.ts$/, "");
  const mod = (await import(pathToFileURL(join(dir, file)).href)) as { screens?: ScreenDef[] };
  loaded.push({ pkg, wp: PACKAGE_OF[pkg] ?? "WP-0", screens: mod.screens ?? [] });
}

if (loaded.length === 0) {
  // Registered so the test is listed (and visible as fixme) before any package has added its screens.
  uxr({ t: "T42", uxr: ["UXR-038"], needs: ["WP-0"] }, "e2e/screens/*.ts lists the screens that get a baseline", async () => {
    expect(readdirSync(dir).filter((f) => f.endsWith(".ts")).length, "screen lists in web/e2e/screens").toBeGreaterThan(0);
  });
}

for (const { pkg, wp, screens } of loaded) {
  for (const s of screens) {
    for (const theme of ["light", "dark"] as const) {
      const title = `T42 UXR-038 UXR-030: ${s.name} (${theme})`;
      const run = isLanded("WP-0") && isLanded(wp);
      const body = async ({ page, browserName }: { page: import("@playwright/test").Page; browserName: string }, info: import("@playwright/test").TestInfo) => {
        const width = page.viewportSize()?.width ?? 1000;
        base.skip(Boolean((s.narrowOnly && width > 720) || (s.wideOnly && width <= 720)), "screen is for the other viewport class");
        await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
        await mockApp(page, s.mock);
        await page.goto(s.path);
        await settle(page);
        await s.run(page);
        if (process.env["BRIDGE_SKIP_VISUAL"] !== "1" && process.platform === "linux" && info.project.name.startsWith("chromium") && browserName === "chromium")
          await expect(page).toHaveScreenshot(`${s.name}-${theme}.png`);
      };
      const details = { tag: ["@T42", "@UXR-038", `@${wp}`], annotation: [{ type: "needs", description: `${wp} (${pkg})` }] };
      if (run) test(title, details, body);
      else test.fixme(`${title} [fixme until ${wp}]`, details, body);
    }
  }
}

// The Inbox and Mission detail each need empty, sparse, dense and error variants.
for (const [pkg, wp] of [["inbox", "WP-4"], ["mission", "WP-3"]] as const) {
  uxr({ t: "T42", uxr: ["UXR-038"], needs: ["WP-0", wp] }, `${pkg} has empty, sparse, dense and error screens`, async () => {
    const entry = loaded.find((l) => l.pkg === pkg);
    expect(entry, `e2e/screens/${pkg}.ts exists`).toBeDefined();
    const variants = new Set(entry?.screens.map((s) => s.variant));
    for (const v of ["empty", "sparse", "dense", "error"] as const) expect(variants.has(v), `${pkg} screen variant "${v}"`).toBe(true);
  });
}
