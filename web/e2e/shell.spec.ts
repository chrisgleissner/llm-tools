// Shell behaviour that is not a page's business (PRD-037 WP-1): the debounced connection banner, the user menu, skip link order,
// the needs-you count in the Inbox badge, tab title and favicon, the browser notification offer, and the landscape header.
import { expect, test } from "./fixtures";
import { MISSIONS, mockApp } from "./mock";
import { IN } from "./screens/types";

test("a reconnect shorter than the debounce never shows the banner; a longer outage shows it with the last sync time and clears on recovery", async ({ page }) => {
  const app = await mockApp(page, { ...IN, fakeClock: true });
  await page.goto("/");
  await expect(page.locator("#conn")).toContainText("Live");
  const banner = page.locator("#conn-banner");
  app.dropStream();
  await page.clock.runFor(1500);
  app.restoreStream();
  await page.clock.runFor(6000);
  await expect(page.locator("#conn")).toContainText("Live");
  await expect(banner, "a 1.5 s outage").toBeHidden();
  app.dropStream();
  await page.clock.runFor(3000);
  await expect(banner, "before the debounce has passed").toBeHidden();
  await page.clock.runFor(2500);
  await expect(banner).toBeVisible();
  await expect(banner).toContainText(/as of \d{1,2}:\d{2} \(\d+s ago\)/);
  await expect(page.locator("main")).toHaveAttribute("data-conn", /reconnecting|offline/);
  await expect(page.locator("#conn")).toContainText(/Reconnecting|Offline/);
  app.restoreStream();
  await page.clock.runFor(20_000);
  await expect(banner).toBeHidden();
  await expect(page.locator("#conn")).toContainText("Live");
});

test("the user menu holds account, diagnostics, theme, density, shortcuts and sign out", async ({ page }) => {
  await page.addInitScript(() => {
    (window as unknown as { Notification: unknown }).Notification = Object.assign(function Notification() {}, { permission: "default", requestPermission: () => Promise.resolve("default") });
  });
  await mockApp(page, IN);
  await page.goto("/");
  await page.locator("#account-button").click();
  const names = await page.getByRole("menuitem").allInnerTexts();
  expect(names.map((n) => n.trim().split("\n")[0])).toEqual(["Account", "Diagnostics", "Theme: auto", "Density: comfortable", "Turn on browser notifications", "Keyboard shortcuts", "Sign out"]);
  await page.getByRole("menuitem", { name: /^Density/ }).click();
  await expect(page.locator("html")).toHaveAttribute("data-density", "compact");
  await page.reload();
  await expect(page.locator("html"), "the choice is remembered").toHaveAttribute("data-density", "compact");
  await page.locator("#account-button").click();
  await page.getByRole("menuitem", { name: "Diagnostics" }).click();
  await expect(page).toHaveURL(/\/diagnostics$/);
});

test("the skip link is the first Tab stop, then the brand and the search button", async ({ page }) => {
  await mockApp(page, IN);
  await page.goto("/");
  await expect(page.locator("#conn")).toContainText("Live");
  await page.evaluate(() => { const m = document.createElement("span"); m.tabIndex = -1; document.body.prepend(m); m.focus(); m.remove(); });
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "LLM Bridge" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator("#palette-open")).toBeFocused();
});

test("what needs the operator shows in the Inbox badge, the tab title and the favicon", async ({ page }) => {
  await mockApp(page, IN);
  await page.goto("/");
  await expect(page).toHaveTitle(/^\(2\) /);
  await expect(page.locator(".nav-count")).toHaveText(/^2\s*need you$/);
  expect(decodeURIComponent((await page.locator('link[rel~="icon"]').getAttribute("href")) ?? "")).toContain("circle");
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Workers" }).click();
  await expect(page).toHaveTitle(/^\(2\) .*LLM Bridge$/);
});

test("the notification offer follows a new needs-you item and asks for permission only when the button is pressed", async ({ page }) => {
  await page.addInitScript(() => {
    const w = window as unknown as { __asks: number };
    w.__asks = 0;
    const stub = function Notification() {} as unknown as { permission: string; requestPermission: () => Promise<string> };
    stub.permission = "default";
    stub.requestPermission = () => { w.__asks += 1; return Promise.resolve("denied"); };
    (window as unknown as { Notification: unknown }).Notification = stub;
  });
  const app = await mockApp(page, IN);
  let calls = 0;
  await page.route("**/api/v1/missions", async (route) => {
    calls += 1;
    if (route.request().method() !== "GET" || calls < 3) return route.fallback();
    const missions = MISSIONS.map((m, i) => (i === 0 ? { ...m, mission: { ...m.mission, status: "FAILED" } } : m));
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ missions }) });
  });
  await page.goto("/");
  await expect(page.locator("#conn")).toContainText("Live");
  const offer = page.getByRole("region", { name: "Browser notifications" });
  await expect(offer, "the failed mission that was already there does not trigger the offer").toBeHidden();
  app.emitStream({ type: "event", worker: "alpha", cursor: 99, events: [] });
  await expect(offer).toBeVisible({ timeout: 15_000 });
  expect(await page.evaluate(() => (window as unknown as { __asks: number }).__asks), "no request before the click").toBe(0);
  await offer.getByRole("button", { name: /notifications/i }).click();
  expect(await page.evaluate(() => (window as unknown as { __asks: number }).__asks)).toBe(1);
  await expect(offer).toBeHidden();
});

test("on a landscape phone the header is one short line with the navigation inside it and no bottom bar", async ({ page }) => {
  await page.setViewportSize({ width: 568, height: 320 });
  await mockApp(page, IN);
  await page.goto("/");
  await expect(page.locator("#conn")).toContainText("Live");
  const header = (await page.locator("header").boundingBox())!;
  expect(header.height).toBeLessThanOrEqual(44);
  const nav = page.getByRole("navigation", { name: "Main" });
  const links = await nav.getByRole("link").evaluateAll((els) => els.map((e) => ({ text: (e.textContent ?? "").trim(), box: e.getBoundingClientRect(), clipped: e.scrollWidth > e.clientWidth })));
  expect(links.map((l) => l.text)).toEqual(["Inbox", "Missions", "Sessions", "Workers", "Usage"]);
  for (const l of links) {
    expect(l.box.bottom, `${l.text} sits in the header`).toBeLessThanOrEqual(header.height + 0.5);
    expect(l.clipped, `${l.text} is clipped`).toBe(false);
  }
  await expect(page.locator("#account-button")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("? lists the keyboard map and g i goes to the Inbox", async ({ page }) => {
  await mockApp(page, IN);
  await page.goto("/missions");
  await expect(page.locator("#conn")).toContainText("Live");
  await page.keyboard.press("?");
  await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.keyboard.press("g");
  await page.keyboard.press("i");
  await expect(page).toHaveURL(/\/$/);
});

test("p and c reach the page: through bridge:shortcut when a page handles it, else through its data-action button and data-composer field; never while typing", async ({ page }) => {
  await mockApp(page, IN);
  await page.goto("/workers");
  await expect(page.locator("#conn")).toContainText("Live");
  await page.evaluate(() => {
    const w = window as unknown as { __hits: string[] };
    w.__hits = [];
    const main = document.getElementById("main") as HTMLElement;
    main.insertAdjacentHTML("beforeend", '<button type="button" data-action="pause" id="t-pause">Pause</button><div data-composer><textarea id="t-composer"></textarea></div><input id="t-other">');
    document.getElementById("t-pause")?.addEventListener("click", () => w.__hits.push("click"));
  });
  const hits = () => page.evaluate(() => (window as unknown as { __hits: string[] }).__hits);
  await page.keyboard.press("p");
  expect(await hits(), "fallback: the button is clicked").toEqual(["click"]);
  await page.keyboard.press("c");
  await expect(page.locator("#t-composer")).toBeFocused();
  await page.keyboard.type("p");
  expect(await hits(), "typing in the composer does not fire shortcuts").toEqual(["click"]);
  await page.locator("h1").click();
  await page.evaluate(() => document.addEventListener("bridge:shortcut", (e) => { (window as unknown as { __hits: string[] }).__hits.push(`event:${(e as CustomEvent<{ name: string }>).detail.name}`); e.preventDefault(); }));
  await page.keyboard.press("p");
  expect(await hits(), "a page that handles the event suppresses the click").toEqual(["click", "event:pause"]);
});
