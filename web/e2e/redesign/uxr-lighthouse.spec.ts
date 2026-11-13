// T37 (UXR-032): Lighthouse mobile preset on /, /missions and a mission page, against a real coordinator with a seeded Python
// worker (the job `web-lighthouse` in .github/workflows/test.yml). The budgets live in web/scripts/lighthouse.mjs.
import { execFileSync } from "node:child_process";
import { join, resolve } from "node:path";
import { test as pw } from "@playwright/test";
import { expect, setupViaUi } from "../fixtures";
import { havePython, startSeededWorker } from "../seed";
import { uxr } from "./gate";

const script = resolve(import.meta.dirname, "../../scripts/lighthouse.mjs");

uxr({ t: "T37", uxr: ["UXR-032"], needs: ["WP-0", "WP-1", "WP-2", "WP-3", "WP-4", "WP-5"] }, "Lighthouse: performance >= 90, accessibility 100, best practices >= 95, LCP <= 2.5 s, TBT <= 200 ms", async ({ page, context, coord, browserName }, info) => {
  pw.skip(!havePython, "repo .venv python is missing (the seeded worker needs it)");
  pw.skip(browserName !== "chromium", "Lighthouse drives Chromium");
  info.setTimeout(300_000);
  const seeded = await startSeededWorker(coord, { missions: 6 });
  try {
    await setupViaUi(page, coord);
    await page.goto("/missions");
    const href = await page.locator('a[href^="/missions/seed-worker/"]').first().getAttribute("href", { timeout: 30_000 });
    expect(href, "a seeded mission is listed").toBeTruthy();
    const cookie = (await context.cookies()).map((c) => `${c.name}=${c.value}`).join("; ");
    const out = execFileSync("node", [script, "--url", coord.url, "--cookie", cookie, "--paths", `/,/missions,${href}`], { encoding: "utf8", cwd: join(import.meta.dirname, "../..") });
    info.annotations.push({ type: "lighthouse", description: out.trim().replace(/\n/g, " | ") });
  } finally {
    await seeded.stopWorker();
    seeded.cleanup();
  }
});
