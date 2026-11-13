// T38 (UXR-028, UXR-041): the terminal on small screens. Phones only: wider viewports skip with a reason.
import { test as pw } from "@playwright/test";
import { evidence } from "./evidence";
import { expect, uxr } from "./gate";
import { isPhone, MISSION_URL, openApp } from "./harness";

const NEEDS = ["WP-0", "WP-1", "WP-2", "WP-3"] as const;

uxr({ t: "T38", uxr: ["UXR-028"], needs: [...NEEDS] }, "the terminal font is at least 12 px and the key bar has six keys plus '...', disabled until control", async ({ page }, info) => {
  pw.skip(!isPhone(page), "small-screen behaviour");
  await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
  const font = await page.evaluate(() => parseFloat(getComputedStyle(document.querySelector("#terminal-host .xterm-rows") as Element).fontSize));
  expect(font, "terminal font size in px").toBeGreaterThanOrEqual(12);
  const keys = page.locator("#terminal-keys button:visible");
  await expect(keys, "the key bar is hidden until control (D28.5)").toHaveCount(0);
  await page.locator('[data-action="take-control"]').click();
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-mode", "write");
  await expect(keys).toHaveCount(7);
  await expect(page.locator("#key-esc")).toBeEnabled();
  await evidence(page, info, ["UXR-028"], "key-bar");
});

uxr({ t: "T38", uxr: ["UXR-041", "UXR-028"], needs: [...NEEDS] }, "taking control on a phone sends no resize; the explicit resize item sends exactly one after confirmation", async ({ page }) => {
  pw.skip(!isPhone(page), "small-screen behaviour");
  const app = await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-state", "live");
  await page.locator('[data-action="take-control"]').click();
  await expect(page.locator("#terminal-status")).toHaveAttribute("data-mode", "write");
  await page.waitForTimeout(500);
  const resizes = () => app.terminalSockets().flatMap((s) => s.frames).filter((f) => f["t"] === "r").length;
  expect(resizes(), "resize frames after Take control on a phone").toBe(0);
  await page.locator('[data-pane="session"] button[aria-haspopup="menu"]').first().click();
  await page.getByRole("menuitem", { name: /resize/i }).click();
  await page.getByRole("dialog").locator("[data-confirm]").click();
  await expect.poll(resizes).toBe(1);
});

uxr({ t: "T38", uxr: ["UXR-028", "UXR-041"], needs: [...NEEDS] }, "the Transcript toggle shows the buffer as text; Expand and Escape round trip", async ({ page }, info) => {
  pw.skip(!isPhone(page), "small-screen behaviour");
  await openApp(page, { auth: "in", terminalScreen: true }, MISSION_URL);
  await expect(page.locator("#terminal-host .xterm-rows")).toContainText("Reading src/tokenizer.rs");
  await page.getByRole("button", { name: /transcript/i }).click();
  const transcript = page.locator('[data-pane="session"] pre[tabindex="0"]');
  await expect(transcript).toBeVisible();
  await expect(transcript).toContainText("Reading src/tokenizer.rs");
  await page.getByRole("button", { name: /transcript/i }).click();
  const expand = page.getByRole("button", { name: /expand|full screen/i }).first();
  await expand.click();
  await expect(page.locator("header").first(), "navigation chrome is hidden in full screen").toBeHidden();
  await evidence(page, info, ["UXR-028"], "full-screen");
  await page.keyboard.press("Escape");
  await expect(page.locator("header").first()).toBeVisible();
  await expect(expand, "focus returns to the Expand button").toBeFocused();
});
