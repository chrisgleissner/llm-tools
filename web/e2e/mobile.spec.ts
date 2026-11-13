// Phone usability: no horizontal scroll, touch targets, input font size, dialogs and the terminal key bar
// (TEST-011, WEB-001, WEB-003, WEB-009). The size rules apply when the viewport is a phone (at most 720 px wide);
// on desktop the same tests check the layout and that the key bar stays hidden.
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";
import type { Page } from "@playwright/test";
import { expect, expectNoHorizontalScroll, navTo, setupViaUi, test } from "./fixtures";
import { startStation } from "./stations";

const python = join(resolve(import.meta.dirname, "../.."), ".venv", "bin", "python");

const isNarrow = (page: Page) => (page.viewportSize()?.width ?? 1000) <= 720;

/** Interactive controls that are smaller than 44x44 CSS px. Links inside running text are exempt (WCAG 2.5.8 inline exception). */
async function smallTargets(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    const sel = "button, a[href], input, select, textarea, summary, [role='button'], [role='tab']";
    for (const el of document.querySelectorAll<HTMLElement>(sel)) {
      const r = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      if (r.width <= 1 || r.height <= 1 || style.visibility === "hidden" || el.closest("[hidden], .sr-only") || el.classList.contains("skip")) continue;
      if (el.matches("a:not(.btn, .cell-link, .tile, .chip, .brand)") && el.closest("p, li, span, td, th, nav.crumbs") && !el.closest("nav:not(.crumbs), .tabbar, .side, .attention")) continue;
      if (el instanceof HTMLInputElement && el.type === "checkbox") {
        const l = el.closest("label")?.getBoundingClientRect();
        if (l && l.height >= 44) continue;
      }
      if (el.closest(".xterm")) continue;
      if (r.width < 43.5 || r.height < 43.5) out.push(`${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}.${el.className} "${(el.getAttribute("aria-label") ?? el.textContent ?? "").trim().slice(0, 30)}" ${Math.round(r.width)}x${Math.round(r.height)}`);
    }
    return out;
  });
}

async function smallInputs(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    for (const el of document.querySelectorAll<HTMLElement>("input:not([type=checkbox]), select, textarea")) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || el.closest("[hidden]")) continue;
      const px = parseFloat(getComputedStyle(el).fontSize);
      if (px < 16) out.push(`${el.id || el.tagName}: ${px}px`);
    }
    return out;
  });
}

async function audit(page: Page, label: string): Promise<void> {
  await expectNoHorizontalScroll(page);
  expect(await smallInputs(page), `${label}: inputs below 16px`).toEqual([]);
  if (isNarrow(page)) expect(await smallTargets(page), `${label}: touch targets below 44x44`).toEqual([]);
}

test("setup and sign-in pages fit the screen and have large targets and inputs", async ({ page }) => {
  await page.goto("/");
  await audit(page, "setup");
  await page.getByLabel("New password", { exact: true }).fill("correct horse battery");
  await page.getByRole("button", { name: "Create administrator" }).click();
  await audit(page, "setup with errors");
});

test("every page, menu and dialog fits the screen with large targets", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  await audit(page, "overview");
  for (const name of ["Workers", "Missions", "Sessions", "Usage", "Diagnostics", "Account"] as const) {
    await navTo(page, name);
    await expect(page.locator("main h1, main h2").first()).toBeVisible();
    await audit(page, name);
  }
  // Worker add and enrolment dialogs fit the viewport.
  await navTo(page, "Workers");
  await page.locator("#add-worker-button").click();
  const fits = async (id: string) => {
    const box = await page.locator(id).boundingBox();
    const vp = page.viewportSize() ?? { width: 0, height: 0 };
    expect(box, id).not.toBeNull();
    expect(box!.x, `${id} left`).toBeGreaterThanOrEqual(-0.5);
    expect(box!.y, `${id} top`).toBeGreaterThanOrEqual(-0.5);
    expect(box!.x + box!.width, `${id} right`).toBeLessThanOrEqual(vp.width + 0.5);
    expect(box!.y + box!.height, `${id} bottom`).toBeLessThanOrEqual(vp.height + 0.5);
  };
  await fits("#add-worker");
  await audit(page, "add worker dialog");
  await page.locator("#add-worker").getByLabel("Worker name").fill("phone-test");
  await page.locator("#add-worker").getByRole("button", { name: "Add worker" }).click();
  await expect(page.locator("#enrol-result")).toBeVisible();
  await fits("#enrol-result");
  await audit(page, "enrolment dialog");
  await page.getByRole("button", { name: "Done" }).click();
  // Confirmation dialog, with the on-screen keyboard's worth of space not needed: it is short.
  await page.getByRole("button", { name: "Actions for worker phone-test" }).click();
  await page.getByRole("menuitem", { name: "Remove worker" }).click();
  await fits("dialog.dialog[open]");
  await audit(page, "confirmation dialog");
  await page.locator("#dialog-cancel").click();
  // User menu (a bottom sheet on a phone), shortcut help and the command palette.
  await page.locator("#account-button").click();
  await audit(page, "user menu");
  await page.locator("#menu-shortcuts").click();
  await fits("#shortcut-help");
  await audit(page, "shortcut help");
  await page.keyboard.press("Escape");
  await page.locator("#palette-open").click();
  await fits("#palette");
  await audit(page, "command palette");
});

test("on a phone the navigation is a bottom bar within thumb reach; on desktop it is a rail", async ({ page, coord }) => {
  await setupViaUi(page, coord);
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav.getByRole("link")).toHaveCount(5);
  const box = (await nav.boundingBox())!;
  const vp = page.viewportSize()!;
  if (isNarrow(page)) {
    expect(box.y + box.height).toBeCloseTo(vp.height, 0);
    expect(box.height).toBeGreaterThanOrEqual(56);
    expect(box.width).toBeCloseTo(vp.width, 0);
  } else {
    expect(box.x).toBeLessThanOrEqual(1);
    expect(box.height).toBeGreaterThan(vp.height / 2);
    expect(box.width).toBeLessThan(vp.width / 4);
  }
});

test.describe("terminal on a phone", () => {
  test.skip(!existsSync(python), "repo .venv python is missing");
  test.skip(() => {
    try {
      execFileSync("tmux", ["-V"], { stdio: "ignore" });
      return false;
    } catch {
      return true;
    }
  }, "tmux is not installed");

  test("is usable in portrait and landscape, and the key bar sends Esc, Tab, Ctrl and arrow bytes in write mode only", async ({ page, coord }) => {
    const st = await startStation(coord, "control");
    try {
      const sent: number[][] = [];
      page.on("websocket", (ws) => {
        if (!ws.url().includes("/api/v1/terminal")) return;
        ws.on("framesent", (f) => {
          try {
            const m = JSON.parse(String(f.payload)) as { t?: string; d?: string };
            if (m.t === "i" && m.d) sent.push([...Buffer.from(m.d, "base64")]);
          } catch {
            /* not a frame we care about */
          }
        });
      });
      await setupViaUi(page, coord);
      await page.goto(`/missions/e2e-worker/${st.missionId}`);
      await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live", { timeout: 30_000 });
      const keys = page.locator("#terminal-keys");
      const status = page.locator("#terminal-status");
      // View mode: the key bar is shown on a phone but every key is disabled (nothing could be sent).
      await expect(async () => {
        if ((await status.getAttribute("data-state")) === "idle") await page.locator("#terminal-open").click({ timeout: 2000 });
        await expect(status).toHaveAttribute("data-state", "live", { timeout: 4000 });
      }).toPass({ timeout: 30_000 });
      if (isNarrow(page)) await expect(page.locator("#key-esc")).toBeDisabled();
      await page.locator("#session-menu").click();
      await page.locator("#terminal-close").click();
      await expect(async () => {
        if ((await status.getAttribute("data-state")) === "idle") await page.locator("#terminal-control").click({ timeout: 2000 });
        await expect(status).toHaveAttribute("data-mode", "write", { timeout: 4000 });
      }).toPass({ timeout: 30_000 });

      if (!isNarrow(page)) {
        await expect(keys).toBeHidden(); // a desktop with a hardware keyboard does not need it
        return;
      }
      await expect(keys).toBeVisible();
      for (const k of ["esc", "tab", "ctrl", "up", "down"]) await expect(page.locator(`#key-${k}`)).toBeVisible();
      await page.locator("#key-more").click(); // Left and Right are on the second row
      for (const k of ["left", "right"]) await expect(page.locator(`#key-${k}`)).toBeVisible();
      expect(await smallTargets(page)).toEqual([]);
      await expectNoHorizontalScroll(page);

      const tap = async (k: string) => page.locator(`#key-${k}`).click();
      const last = () => sent[sent.length - 1];
      await tap("esc");
      await expect.poll(last).toEqual([0x1b]);
      await tap("tab");
      await expect.poll(last).toEqual([0x09]);
      for (const [k, code] of [["up", 0x41], ["down", 0x42], ["right", 0x43], ["left", 0x44]] as const) {
        await tap(k);
        await expect.poll(last).toEqual([0x1b, 0x5b, code]);
      }
      // Ctrl is sticky for one key: Ctrl then "a" is 0x01, and the next "a" is plain.
      await tap("ctrl");
      await expect(page.locator("#key-ctrl")).toHaveAttribute("aria-pressed", "true");
      await page.locator("#terminal-host .xterm-helper-textarea").focus();
      await page.keyboard.type("a");
      await expect.poll(last).toEqual([0x01]);
      await expect(page.locator("#key-ctrl")).toHaveAttribute("aria-pressed", "false");
      await page.keyboard.type("a");
      await expect.poll(last).toEqual([0x61]);

      // Portrait and landscape: the terminal and the key bar stay inside the viewport.
      const vp = page.viewportSize()!;
      for (const [w, h] of [[vp.width, vp.height], [vp.height, vp.width]] as const) {
        await page.setViewportSize({ width: w, height: h });
        await page.locator("#terminal-host").scrollIntoViewIfNeeded();
        await expectNoHorizontalScroll(page);
        const host = await page.locator("#terminal-host").boundingBox();
        expect(host!.width, `terminal width at ${w}x${h}`).toBeLessThanOrEqual(w);
        expect(host!.height, `terminal height at ${w}x${h}`).toBeGreaterThan(80);
        await expect(keys).toBeVisible();
        await expect(page.locator("#terminal-host .xterm-rows")).toContainText("ok");
        const cols = await page.evaluate(() => document.querySelectorAll("#terminal-host .xterm-rows > div").length);
        expect(cols, "visible terminal rows").toBeGreaterThanOrEqual(3);
      }
      // Without the writer role the keys send nothing.
      await page.locator("#session-menu").click();
      await page.locator("#terminal-close").click();
      await expect(page.locator("#key-esc")).toBeDisabled();
    } finally {
      st.cleanup();
    }
  });
});
