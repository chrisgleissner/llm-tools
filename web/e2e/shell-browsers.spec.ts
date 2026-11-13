// PRD-017 (command palette reaches every page, mission, worker and action), PRD-018 (two more notification triggers) and
// PRD-021 (a plain message on a browser that lacks what the console needs). Runs on every project, WebKit included.
import { expect, test } from "./fixtures";
import { MISSIONS, mockApp } from "./mock";

const mod = process.platform === "darwin" ? "Meta" : "Control";
const IN = { auth: "in" } as const;

async function openPalette(page: import("@playwright/test").Page): Promise<import("@playwright/test").Locator> {
  await expect(page.locator("#conn")).toContainText("Live");
  await page.keyboard.press(`${mod}+k`);
  const dialog = page.getByRole("dialog", { name: "Command palette" });
  await expect(dialog.getByRole("combobox")).toBeFocused();
  return dialog;
}

test.describe("command palette", () => {
  const PAGES: Array<[string, RegExp]> = [
    ["Go to Inbox", /\/$/],
    ["Go to Missions", /\/missions$/],
    ["Go to Sessions", /\/sessions$/],
    ["Go to Workers", /\/workers$/],
    ["Go to Usage", /\/usage$/],
    ["Go to Diagnostics", /\/diagnostics$/],
    ["Go to Account", /\/account$/],
  ];
  for (const [title, url] of PAGES) {
    test(`reaches the page: ${title}`, async ({ page }) => {
      await mockApp(page, IN);
      await page.goto("/workers");
      const dialog = await openPalette(page);
      await page.keyboard.type(title);
      await expect(dialog.getByRole("option").first()).toContainText(title);
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(url);
    });
  }

  test("reaches a worker by name, with its state", async ({ page }) => {
    await mockApp(page, IN);
    await page.goto("/");
    const dialog = await openPalette(page);
    await page.keyboard.type("beta");
    const option = dialog.getByRole("option", { name: /Worker\s+beta/ });
    await expect(option).toContainText("offline");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/workers$/);
  });

  test("reaches a mission by name with the arrow keys, Home and End", async ({ page }) => {
    await mockApp(page, IN);
    await page.goto("/");
    const dialog = await openPalette(page);
    await page.keyboard.type("e");
    const options = dialog.getByRole("option");
    expect(await options.count()).toBeGreaterThan(2);
    await page.keyboard.press("End");
    await expect(options.last()).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("Home");
    await expect(options.first()).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("ArrowDown");
    await expect(options.nth(1)).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    const dialog2 = await openPalette(page);
    await page.keyboard.type(MISSIONS[0]!.mission.name);
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(new RegExp(`/missions/alpha/${MISSIONS[0]!.mission.id}`));
    await expect(dialog2).toBeHidden();
  });

  test("offers the actions of the open mission page (Approve, Pause) and runs them", async ({ page }) => {
    const app = await mockApp(page, IN);
    await page.goto(`/missions/alpha/${MISSIONS[0]!.mission.id}`);
    await expect(page.getByRole("heading", { level: 1, name: MISSIONS[0]!.mission.name })).toBeVisible();
    const dialog = await openPalette(page);
    await page.keyboard.type("pause");
    await expect(dialog.getByRole("option").first()).toContainText("Pause the open mission");
    await page.keyboard.press("Enter");
    await expect.poll(() => app.operations()).toContain("mission.pause");
  });

  test("the keys that the palette shows as hints are bound: g w goes to Workers, ? lists the palette key", async ({ page }) => {
    await mockApp(page, IN);
    await page.goto("/");
    const dialog = await openPalette(page);
    await expect(dialog.getByRole("option", { name: /Go to Workers/ })).toContainText("g w");
    await page.keyboard.press("Escape");
    await page.keyboard.press("g");
    await page.keyboard.press("w");
    await expect(page).toHaveURL(/\/workers$/);
    await page.keyboard.press("?");
    await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toContainText("Command palette");
  });
});

test.describe("notifications for a worker that is offline and a rate limit that lifted", () => {
  const toasts = (page: import("@playwright/test").Page) => page.locator("#toasts");

  test("a worker that drops out is announced once it has been offline for 5 minutes, not before", async ({ page }) => {
    const app = await mockApp(page, { ...IN, fakeClock: true });
    await page.goto("/");
    await expect(page.locator("#conn")).toContainText("Live");
    await page.clock.runFor(2000);
    app.setWorker("alpha", { state: "offline" });
    app.emitStream({ type: "worker", worker: "alpha", state: "offline" });
    await page.clock.runFor(3 * 60_000);
    await expect(toasts(page), "3 minutes: not yet").not.toContainText("offline for");
    await page.clock.runFor(3 * 60_000);
    await expect(toasts(page)).toContainText(/Worker alpha has been offline for \d+ min/);
    await toasts(page).getByRole("button", { name: "Open workers" }).click();
    await expect(page).toHaveURL(/\/workers$/);
  });

  test("a worker that was already offline when the console opened is not announced", async ({ page }) => {
    await mockApp(page, { ...IN, fakeClock: true });
    await page.goto("/");
    await expect(page.locator("#conn")).toContainText("Live");
    await page.clock.runFor(10 * 60_000);
    await expect(toasts(page)).not.toContainText("beta");
  });

  test("the time is a setting: 15 minutes in storage delays the announcement, and the palette changes it", async ({ page }) => {
    const app = await mockApp(page, { ...IN, fakeClock: true });
    await page.addInitScript(() => localStorage.setItem("bridge-notify-offline-min", "15"));
    await page.goto("/");
    await expect(page.locator("#conn")).toContainText("Live");
    await page.clock.runFor(2000);
    app.setWorker("alpha", { state: "offline" });
    app.emitStream({ type: "worker", worker: "alpha", state: "offline" });
    await page.clock.runFor(10 * 60_000);
    await expect(toasts(page), "10 minutes with a 15 minute setting").not.toContainText("offline for");
    await page.clock.runFor(6 * 60_000);
    await expect(toasts(page)).toContainText("Worker alpha has been offline for");
    const dialog = await openPalette(page);
    await page.keyboard.type("offline alert");
    await expect(dialog.getByRole("option").first()).toContainText("Change the worker offline alert time");
    await page.keyboard.press("Enter");
    await expect.poll(() => page.evaluate(() => localStorage.getItem("bridge-notify-offline-min"))).toBe("30");
  });

  test("a rate limit that lifts is announced, with a button to Usage", async ({ page }) => {
    const app = await mockApp(page, { ...IN, fakeClock: true });
    await page.goto("/");
    await expect(page.locator("#conn")).toContainText("Live");
    await page.clock.runFor(2000);
    await expect(page.locator("#capacity")).toContainText("Claude");
    await expect(toasts(page), "a provider that waits from the start is not news").not.toContainText("capacity again");
    app.setProviderReady("claude", true);
    await page.clock.runFor(61_000);
    await expect(toasts(page)).toContainText("Claude has capacity again");
    await toasts(page).getByRole("button", { name: "Open usage" }).click();
    await expect(page).toHaveURL(/\/usage$/);
  });

  test("a hidden tab with permission gets a browser notification for a lifted rate limit", async ({ page }) => {
    await page.addInitScript(() => {
      const w = window as unknown as { __notes: string[] };
      w.__notes = [];
      const stub = function Notification(this: { onclick: null; close: () => void }, title: string) {
        w.__notes.push(title);
        this.onclick = null;
        this.close = () => {};
      } as unknown as { permission: string; requestPermission: () => Promise<string> };
      stub.permission = "granted";
      stub.requestPermission = () => Promise.resolve("granted");
      (window as unknown as { Notification: unknown }).Notification = stub;
      Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    });
    const app = await mockApp(page, { ...IN, fakeClock: true });
    await page.goto("/");
    await expect(page.locator("#conn")).toContainText("Live");
    await page.clock.runFor(2000);
    app.setProviderReady("claude", true);
    await page.clock.runFor(61_000);
    await expect.poll(() => page.evaluate(() => (window as unknown as { __notes: string[] }).__notes)).toContain("Claude has capacity again. The rate limit has lifted.");
  });
});

test("a browser without the dialog element gets a plain message instead of a broken page", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", { value: undefined, configurable: true });
  });
  await mockApp(page, IN);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "This browser is not supported" })).toBeVisible();
  await expect(page.getByRole("main")).toContainText("the dialog element");
  await expect(page.getByRole("main")).toContainText("Chrome, Edge, Firefox or Safari");
  await expect(page).toHaveTitle(/not supported/);
});

test("a supported browser gets no such message", async ({ page }) => {
  await mockApp(page, IN);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();
  await expect(page.locator("[data-unsupported]")).toHaveCount(0);
});
