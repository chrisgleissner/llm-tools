// UXR-040: generates the screenshots that the critique in docs/distributed/ux-audit.md cites (checked by T46 in
// uxr-critique.spec.ts). It writes files only with CRITIQUE_CAPTURE=1, so normal runs skip it:
//
//   CRITIQUE_CAPTURE=1 npx playwright test e2e/redesign/uxr-critique-capture.spec.ts --project=chromium-desktop
//
// Output: docs/distributed/ux-evidence/UXR-040/<page>-<width>-<theme>.png, on the deterministic mock (e2e/mock.ts).
import { mkdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { expect, test } from "../fixtures";
import { mockApp, type MockOptions } from "../mock";
import { settle, MISSION_URL } from "./harness";

const OUT = join(resolve(import.meta.dirname, "../../.."), "docs", "distributed", "ux-evidence", "UXR-040");
const PAGES: Array<{ id: string; path: string; mock: MockOptions }> = [
  { id: "inbox", path: "/", mock: { auth: "in", dataset: "dense" } },
  { id: "missions", path: "/missions", mock: { auth: "in", dataset: "dense" } },
  { id: "mission", path: MISSION_URL, mock: { auth: "in", terminalScreen: true } },
  { id: "sessions", path: "/sessions", mock: { auth: "in" } },
  { id: "workers", path: "/workers", mock: { auth: "in" } },
  { id: "usage", path: "/usage", mock: { auth: "in" } },
  { id: "diagnostics", path: "/diagnostics", mock: { auth: "in" } },
  { id: "account", path: "/account", mock: { auth: "in" } },
  { id: "sign-in", path: "/login", mock: { auth: "login" } },
];
const SIZES = [{ width: 1440, height: 900 }, { width: 360, height: 640 }];

test.describe("UXR-040 critique screenshots", () => {
  test.skip(process.env["CRITIQUE_CAPTURE"] !== "1", "set CRITIQUE_CAPTURE=1 to write the screenshots");
  for (const size of SIZES) for (const theme of ["light", "dark"] as const) for (const p of PAGES) {
    test(`${p.id} ${size.width} ${theme}`, async ({ page }) => {
      mkdirSync(OUT, { recursive: true });
      await page.setViewportSize(size);
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await mockApp(page, p.mock);
      await page.goto(p.path);
      await settle(page);
      await page.mouse.move(0, 0);
      await expect(page.locator("main, form").first()).toBeVisible();
      await page.screenshot({ path: join(OUT, `${p.id}-${size.width}-${theme}.png`), animations: "disabled", caret: "hide" });
    });
  }
});
