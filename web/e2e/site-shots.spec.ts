// Screenshots for the project site (docs/site). Not part of the normal run: set SITE_SHOTS_DIR to capture.
// They use the deterministic mock of e2e/mock.ts, so they show sample data, never a real installation.
import { expect, test } from "./fixtures";
import { MISSION_PATH, mockApp } from "./mock";
import { settle } from "./redesign/harness";

const dir = process.env["SITE_SHOTS_DIR"];
test.skip(!dir, "set SITE_SHOTS_DIR to capture the site screenshots");
test.use({ deviceScaleFactor: 2 });

for (const theme of ["dark", "light"] as const) {
  test(`site screenshots (${theme})`, async ({ page }, info) => {
    const phone = info.project.name === "chromium-mobile";
    test.skip(!phone && info.project.name !== "chromium-desktop", "Chromium projects only");
    if (!phone) await page.setViewportSize({ width: 1100, height: 680 });
    const shot = async (name: string) => (await settle(page), page.screenshot({ path: `${dir}/${name}-${theme}.png` }));
    await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
    await mockApp(page, { auth: "in", terminalScreen: true });
    await page.goto("/");
    await expect(page.locator('[data-section="needs-you"] [data-action="approve"]').first()).toBeVisible();
    await expect(page.locator("#conn")).toContainText("Live");
    await page.addStyleTag({ content: ".toasts { display: none !important; }" });
    await page.evaluate(() => document.fonts.ready);
    if (phone) {
      await shot("web-phone");
      return;
    }
    await shot("web-overview");
    await page.goto(MISSION_PATH);
    await expect(page.locator("#tasks-table tbody tr")).toHaveCount(6);
    await page.locator("#mission-tasks").scrollIntoViewIfNeeded();
    await page.evaluate(() => document.getElementById("mission-tasks")?.scrollIntoView({ block: "start" }));
    await page.evaluate(() => window.scrollBy(0, -70));
    await shot("web-approval");
    await page.locator("#terminal-control").click();
    await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
    await expect(page.locator("#terminal-host .xterm-rows")).toContainText("ready for review");
    await page.evaluate(() => document.getElementById("terminal-region")?.scrollIntoView({ block: "start" }));
    await page.evaluate(() => window.scrollBy(0, -80));
    await shot("web-terminal");
  });
}
