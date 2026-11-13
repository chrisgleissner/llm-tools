// Screenshots for UXR evidence. With UXR_EVIDENCE=1 a test saves the page to
//   docs/distributed/ux-evidence/<UXR-id>/<name>.<project>.<theme>.png
// for every UXR row it proves (the path the SPEC_STATUS check in tests/test_distributed_spec_status.py looks for).
// Without the flag it does nothing, so normal runs stay fast and write nothing into the repository.
//
//   UXR_EVIDENCE=1 npx playwright test e2e/redesign/uxr-mission.spec.ts --project=chromium-desktop --project=chromium-mobile
//
// Run it in the Playwright image that makes the baselines (web/README.md) so the evidence matches the baselines.
import type { Page, TestInfo } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const root = resolve(import.meta.dirname, "../../..");
export const EVIDENCE_DIR = join(root, "docs", "distributed", "ux-evidence");

export const evidenceEnabled = (): boolean => process.env["UXR_EVIDENCE"] === "1";

export async function evidence(page: Page, info: TestInfo, uxrIds: string[], name: string, opts: { fullPage?: boolean } = {}): Promise<string[]> {
  if (!evidenceEnabled()) return [];
  const theme = await page.evaluate(() => (document.documentElement.getAttribute("data-theme") ?? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")));
  const file = `${name.replace(/[^a-z0-9-]+/gi, "-").toLowerCase()}.${info.project.name}.${theme}.png`;
  const written: string[] = [];
  const buffer = await page.screenshot({ fullPage: opts.fullPage ?? false, animations: "disabled", caret: "hide" });
  for (const id of uxrIds) {
    const dir = join(EVIDENCE_DIR, id);
    mkdirSync(dir, { recursive: true });
    const path = join(dir, file);
    writeFileSync(path, buffer);
    written.push(path);
  }
  return written;
}
