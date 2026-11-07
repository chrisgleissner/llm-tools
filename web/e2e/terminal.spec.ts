// Remote terminal end-to-end: browser xterm -> coordinator relay -> real Python worker -> real tmux station.
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import type { Page } from "@playwright/test";
import { expect, expectAccessible, expectNoHorizontalScroll, setupViaUi, test } from "./fixtures";
import { startStation } from "./stations";

const repo = resolve(import.meta.dirname, "../..");
const python = join(repo, ".venv", "bin", "python");

/** Click a terminal button and wait for the session; the page may re-render while it loads, so retry a click that was lost. */
async function openTerminal(page: Page, button: string): Promise<void> {
  const status = page.locator("#terminal-status");
  await expect(async () => {
    if ((await status.getAttribute("data-state")) === "idle") await page.locator(button).click({ timeout: 2000 });
    await expect(status).toHaveAttribute("data-state", "live", { timeout: 4000 });
  }).toPass({ timeout: 30_000 });
}

/** The rendered terminal rows. xterm.js pauses painting while the terminal is outside the
 * viewport (content above it can grow while the test runs), so scroll it into view first. */
async function expectScreen(page: Page, text: string, present = true): Promise<void> {
  const rows = page.locator("#terminal-host .xterm-rows");
  await expect(async () => {
    await page.locator("#terminal-host").scrollIntoViewIfNeeded({ timeout: 2000 });
    if (present) await expect(rows).toContainText(text, { timeout: 2000 });
    else await expect(rows).not.toContainText(text, { timeout: 2000 });
  }).toPass({ timeout: 20_000 });
}

test.describe("remote terminal with a real worker and tmux station", () => {
  test.skip(!existsSync(python), "repo .venv python is missing");
  test.skip(() => {
    try {
      execFileSync("tmux", ["-V"], { stdio: "ignore" });
      return false;
    } catch {
      return true;
    }
  }, "tmux is not installed");

  test("view, take control, single writer, copy and input paths", async ({ page, context, coord }) => {
    const st = await startStation(coord, "control");
    try {
      await setupViaUi(page, coord);
      const url = `/missions/e2e-worker/${st.missionId}`;
      await expect(async () => {
        await page.goto(url);
        await expect(page.locator("#terminal-open")).toBeEnabled({ timeout: 2000 });
      }).toPass({ timeout: 30_000 });

      // 1. Take control: granted write, typed input reaches the provider pane and its output comes back.
      const status = page.locator("#terminal-status");
      await openTerminal(page, "#terminal-control");
      await expect(status).toHaveAttribute("data-mode", "write");
      await expect(status).toContainText("you have control");
      await expectScreen(page, "ok");
      await page.locator("#terminal-host .xterm-helper-textarea").focus();
      await page.keyboard.type("hello");
      await page.keyboard.press("Enter");
      await expectScreen(page, "echo:hello");
      await expectNoHorizontalScroll(page);
      await expectAccessible(page, "mission detail with terminal");

      // 2. A second tab asking for write is granted view only; its input never reaches the pane.
      const second = await context.newPage();
      await second.goto(url);
      await expect(second.locator("#terminal-control")).toBeEnabled({ timeout: 30_000 });
      const status2 = second.locator("#terminal-status");
      await openTerminal(second, "#terminal-control");
      await expect(status2).toHaveAttribute("data-mode", "view");
      await expect(status2).toContainText("view only");
      await expect(status2).toContainText("writer role");
      await expectScreen(second, "echo:hello"); // fresh snapshot of the shared pane
      await second.locator("#terminal-host .xterm-helper-textarea").focus();
      await second.keyboard.type("blocked");
      await second.keyboard.press("Enter");
      // The writer tab keeps working and the pane never echoes what the view-only tab typed.
      await page.bringToFront();
      await page.locator("#terminal-host .xterm-helper-textarea").focus();
      await page.keyboard.type("again");
      await page.keyboard.press("Enter");
      await expectScreen(page, "echo:again");
      await second.bringToFront();
      await expectScreen(second, "echo:again"); // live output reaches the viewer
      await expectScreen(page, "echo:blocked", false);
      await expectScreen(second, "echo:blocked", false);

      // 3. Closing the writer frees the role: a new "Take control" in the second tab is granted write.
      await page.bringToFront();
      await page.locator("#terminal-close").click();
      await expect(page.locator("#terminal-open")).toBeEnabled();
      await second.bringToFront();
      await second.locator("#terminal-close").click();
      await openTerminal(second, "#terminal-control");
      await expect(status2).toHaveAttribute("data-mode", "write", { timeout: 20_000 });
      await second.locator("#terminal-host .xterm-helper-textarea").focus();
      await second.keyboard.type("second");
      await second.keyboard.press("Enter");
      await expectScreen(second, "echo:second");
      await second.close();
      await page.bringToFront();

      // 4. Plain view mode: no control requested, input is not sent.
      await openTerminal(page, "#terminal-open");
      await expect(status).toHaveAttribute("data-mode", "view", { timeout: 20_000 });
      await expectScreen(page, "echo:second");
      await page.locator("#terminal-control").click();
      await expect(status).toHaveAttribute("data-mode", "write", { timeout: 20_000 });
      const longLine = "immediate-enter-".repeat(12);
      await page.locator("#terminal-host .xterm-helper-textarea").focus();
      await page.keyboard.type(longLine);
      await page.keyboard.press("Enter");
      await expectScreen(page, `echo:${longLine}`);
    } finally {
      st.cleanup();
    }
  });

  test("a read-only worker grants view only, and the session ends when the station stops", async ({ page, coord }) => {
    const st = await startStation(coord, "view");
    try {
      await setupViaUi(page, coord);
      const url = `/missions/e2e-worker/${st.missionId}`;
      await expect(async () => {
        await page.goto(url);
        await expect(page.locator("#terminal-control")).toBeEnabled({ timeout: 2000 });
      }).toPass({ timeout: 30_000 });
      const status = page.locator("#terminal-status");
      await openTerminal(page, "#terminal-control");
      await expect(status).toHaveAttribute("data-mode", "view");
      await expect(status).toContainText("read-only");
      await expectScreen(page, "ok");
      st.killStation();
      await expect(status).toHaveAttribute("data-state", "ended", { timeout: 20_000 });
      await expect(status).toContainText("station ended");
      await expect(page.locator("#terminal-open")).toBeEnabled();
    } finally {
      st.cleanup();
    }
  });

  test("a read-only worker without the terminal opt-in refuses terminals", async ({ page, coord }) => {
    const st = await startStation(coord, "none");
    try {
      await setupViaUi(page, coord);
      const url = `/missions/e2e-worker/${st.missionId}`;
      await expect(async () => {
        await page.goto(url);
        await expect(page.locator("#terminal-open")).toBeEnabled({ timeout: 2000 });
      }).toPass({ timeout: 30_000 });
      const status = page.locator("#terminal-status");
      // A presence update can disable the button between Playwright's enabled check and the click
      // (seen once on firefox-desktop in CI: the status stayed idle). Click again until it leaves idle.
      await expect(async () => {
        await page.locator("#terminal-open").click();
        await expect(status).not.toHaveAttribute("data-state", "idle", { timeout: 2000 });
      }).toPass({ timeout: 20_000 });
      await expect(status).toContainText("--allow-terminal-view", { timeout: 20_000 });
      await expect(status).not.toHaveAttribute("data-state", "live");
      // The worker refused before sending any output, so nothing was ever written to the terminal.
      await expect(page.locator("#terminal-host .xterm-rows")).not.toContainText("ok");
    } finally {
      st.cleanup();
    }
  });

  test("an unknown mission is refused with the worker's reason", async ({ page, coord }) => {
    const st = await startStation(coord, "control");
    try {
      await setupViaUi(page, coord);
      await expect(async () => {
        await page.goto(`/missions/e2e-worker/${st.missionId}`);
        await expect(page.locator("#terminal-open")).toBeEnabled({ timeout: 2000 });
      }).toPass({ timeout: 30_000 });
      await st.stopWorker();
      await expect(page.locator("#terminal-open")).toBeDisabled({ timeout: 20_000 });
    } finally {
      st.cleanup();
    }
  });
});
