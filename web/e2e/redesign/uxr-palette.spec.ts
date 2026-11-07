// T25 (UXR-021): command palette and keyboard shortcuts.
import { expect, uxr } from "./gate";
import { MISSION_ID, MISSION_URL, openApp } from "./harness";
import { evidence } from "./evidence";

const NEEDS = ["WP-0", "WP-1"] as const;
const mod = process.platform === "darwin" ? "Meta" : "Control";

uxr({ t: "T25", uxr: ["UXR-021"], needs: [...NEEDS, "WP-4"] }, "Ctrl/Cmd+K finds a mission by name and by id; open, type, Enter is 3 key groups", async ({ page }, info) => {
  const app = await openApp(page, { auth: "in" }, "/");
  await page.keyboard.press(`${mod}+k`);
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("combobox")).toBeFocused();
  await expect(dialog.getByRole("listbox")).toBeVisible();
  await page.keyboard.type("Refactor the parser");
  const option = dialog.getByRole("option").first();
  await expect(option).toContainText("Refactor the parser");
  await evidence(page, info, ["UXR-021"], "palette");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`/missions/alpha/${MISSION_ID}`));
  expect(await app.ix.count(), "key groups: open, type, Enter").toBe(3);
  await page.goto("/");
  await page.keyboard.press(`${mod}+k`);
  await page.keyboard.type(MISSION_ID.slice(0, 8));
  await expect(page.getByRole("dialog").getByRole("option").first()).toContainText("Refactor the parser");
});

uxr({ t: "T25", uxr: ["UXR-021"], needs: [...NEEDS, "WP-4"] }, "the palette finds a session and an action", async ({ page }) => {
  await openApp(page, { auth: "in" }, "/");
  await page.keyboard.press(`${mod}+k`);
  await page.keyboard.type("release notes");
  const options = page.getByRole("dialog").getByRole("option");
  await expect.poll(() => options.count(), { message: "a mission and a session match 'release notes'" }).toBeGreaterThanOrEqual(2);
  await page.keyboard.press("Control+a");
  await page.keyboard.type("theme");
  await expect(options.first()).toContainText(/theme/i);
  const before = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
  await page.keyboard.press("Enter");
  expect(await page.evaluate(() => document.documentElement.getAttribute("data-theme")), "Toggle theme ran").not.toBe(before);
});

const NAV_KEYS: Array<[string[], RegExp]> = [[["g", "m"], /\/missions$/], [["g", "s"], /\/sessions$/], [["g", "w"], /\/workers$/], [["g", "u"], /\/usage$/], [["g", "i"], /\/$/]];

uxr({ t: "T25", uxr: ["UXR-021"], needs: [...NEEDS, "WP-4", "WP-5"] }, "every key in the ? dialog is bound and every bound key is listed", async ({ page }) => {
  await openApp(page, { auth: "in" }, "/");
  await page.keyboard.press("?");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  const text = await dialog.innerText();
  for (const key of ["g", "i", "m", "s", "w", "u", "j", "k", "Enter", "a", "r", "p", "c", "/", "t", "?"]) expect(text, `the dialog lists ${key}`).toContain(key);
  const listed = await dialog.locator("kbd").allInnerTexts();
  const known = new Set(["g", "i", "m", "s", "w", "u", "j", "k", "Enter", "a", "r", "p", "c", "/", "t", "?", "Ctrl", "Cmd", "K", "Esc", "Escape", "Shift", "Space", "+", "or"]);
  expect(listed.map((k) => k.trim()).filter((k) => k && !known.has(k)), "keys listed in the dialog that no test (and so no handler we know of) covers").toEqual([]);
  await page.keyboard.press("Escape");
  for (const [keys, url] of NAV_KEYS) {
    await page.goto("/missions");
    for (const key of keys) await page.keyboard.press(key);
    await expect(page, `g ${keys[1]} navigates`).toHaveURL(url);
  }
  const before = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
  await page.keyboard.press("t");
  expect(await page.evaluate(() => document.documentElement.getAttribute("data-theme")), "t toggles the theme").not.toBe(before);
});

uxr({ t: "T25", uxr: ["UXR-021", "UXR-046"], needs: [...NEEDS, "WP-2", "WP-3"] }, "mission page shortcuts: c focuses the composer, p pauses", async ({ page }) => {
  const app = await openApp(page, { auth: "in", dataset: "dense" }, "/missions");
  await page.goto(MISSION_URL);
  await page.keyboard.press("c");
  // The field when it takes typing; while the pane is view-only or connecting, Take control or the pane itself (spec 8.2): focus is inside the session pane either way.
  await expect.poll(() => page.evaluate(() => Boolean(document.activeElement?.closest('[data-composer], [data-pane="session"]'))), { message: "focus moves into the session pane" }).toBe(true);
  await page.locator("body").click({ position: { x: 2, y: 2 } });
  await page.keyboard.press("p");
  await expect.poll(() => app.operations()).toContain("mission.pause");
});
