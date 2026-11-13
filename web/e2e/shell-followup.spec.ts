// Review follow-up for the palette, the two alerts and the focus after a route change (PRD-017, PRD-018, PRD-019).
import type { Page } from "@playwright/test";
import { expect, test } from "./fixtures";
import { MISSIONS, mockApp } from "./mock";

const mod = process.platform === "darwin" ? "Meta" : "Control";
const IN = { auth: "in" } as const;
const MISSION = MISSIONS[0]!.mission;
/** The page's clock (a fake one in these tests), in epoch seconds. */
const pageNow = (page: Page): Promise<number> => page.evaluate(() => Math.floor(Date.now() / 1000));
const OFFLINE_TEXT = /Worker alpha has been offline for/;

async function openPalette(page: Page): Promise<void> {
  await expect(page.locator("#conn")).toContainText("Live");
  await page.keyboard.press(`${mod}+k`);
  await expect(page.getByRole("dialog", { name: "Command palette" }).getByRole("combobox")).toBeFocused();
}

test.describe("palette page actions", () => {
  test("an action whose button was redrawn away does nothing and says so", async ({ page }) => {
    const app = await mockApp(page, IN);
    await page.goto(`/missions/alpha/${MISSION.id}`);
    await expect(page.getByRole("heading", { level: 1, name: MISSION.name })).toBeVisible();
    await openPalette(page);
    await page.keyboard.type("pause the open");
    await expect(page.getByRole("option").first()).toContainText("Pause the open mission");
    // The page is redrawn between the listing and the run: the button the palette saw is replaced by a detached copy that no longer exists in the page.
    await page.evaluate(() => {
      const b = document.querySelector<HTMLElement>('#main [data-action="pause"]');
      (window as unknown as { __old: HTMLElement }).__old = b!;
      b!.remove();
    });
    await page.keyboard.press("Enter");
    await expect(page.locator("#toasts")).toContainText("That action is no longer available.");
    expect(app.operations(), "nothing was sent").not.toContain("mission.pause");
  });

  test("an action that two buttons now match is not run", async ({ page }) => {
    const app = await mockApp(page, IN);
    await page.goto(`/missions/alpha/${MISSION.id}`);
    await expect(page.getByRole("heading", { level: 1, name: MISSION.name })).toBeVisible();
    await openPalette(page);
    await page.keyboard.type("pause the open");
    await expect(page.getByRole("option").first()).toContainText("Pause the open mission");
    await page.evaluate(() => {
      const b = document.querySelector<HTMLElement>('#main [data-action="pause"]')!;
      b.after(b.cloneNode(true));
    });
    await page.keyboard.press("Enter");
    await expect(page.locator("#toasts")).toContainText("no longer available");
    expect(app.operations()).not.toContain("mission.pause");
  });

  test("Approve from the palette asks for the same confirmation as the button and sends nothing before it", async ({ page }) => {
    const app = await mockApp(page, IN);
    await page.goto(`/missions/alpha/${MISSION.id}`);
    await expect(page.getByRole("heading", { level: 1, name: MISSION.name })).toBeVisible();
    await expect(page.locator('#main [data-action="approve"]')).toHaveCount(1);
    await openPalette(page);
    await page.keyboard.type("approve");
    await expect(page.getByRole("option").first()).toContainText("Approve");
    await page.keyboard.press("Enter");
    const confirm = page.getByRole("dialog", { name: /Approve task/ });
    await expect(confirm).toBeVisible();
    expect(app.operations(), "no approval before the confirmation").not.toContain("task.approve");
    await confirm.getByRole("button", { name: "Go back" }).click();
    await expect(confirm).toBeHidden();
    expect(app.operations(), "a cancelled confirmation sends nothing").not.toContain("task.approve");
  });
});

test.describe("alert duplicates", () => {
  test("a worker that drops out again within 30 minutes is not announced again", async ({ page }) => {
    const app = await mockApp(page, { ...IN, fakeClock: true });
    await page.goto("/");
    await expect(page.locator("#conn")).toContainText("Live");
    await page.clock.runFor(2000);
    app.setWorker("alpha", { state: "offline" });
    app.emitStream({ type: "worker", worker: "alpha", state: "offline" });
    await page.clock.runFor(6 * 60_000);
    await expect(page.locator("#toasts").getByText(OFFLINE_TEXT)).toHaveCount(1);
    // One toast shows at a time: dismiss it, so that a second announcement would be visible.
    await page.getByRole("button", { name: "Dismiss notification" }).click();
    await expect(page.locator("#toasts").getByText(OFFLINE_TEXT)).toHaveCount(0);
    app.setWorker("alpha", { state: "online", last_seen: await pageNow(page) });
    app.emitStream({ type: "worker", worker: "alpha", state: "online" });
    await page.clock.runFor(5000);
    app.setWorker("alpha", { state: "offline", last_seen: await pageNow(page) });
    app.emitStream({ type: "worker", worker: "alpha", state: "offline" });
    await page.clock.runFor(6 * 60_000);
    await expect(page.locator("#toasts").getByText(OFFLINE_TEXT), "the second drop is inside the cooldown").toHaveCount(0);
    await page.clock.runFor(31 * 60_000);
    app.setWorker("alpha", { state: "online", last_seen: await pageNow(page) });
    app.emitStream({ type: "worker", worker: "alpha", state: "online" });
    await page.clock.runFor(5000);
    app.setWorker("alpha", { state: "offline", last_seen: await pageNow(page) });
    app.emitStream({ type: "worker", worker: "alpha", state: "offline" });
    await page.clock.runFor(6 * 60_000);
    await expect(page.locator("#toasts").getByText(OFFLINE_TEXT), "after the cooldown a new drop is announced").toHaveCount(1);
  });

  test("two tabs of one browser announce a worker that went offline once", async ({ page }) => {
    const second = await page.context().newPage();
    const a = await mockApp(page, { ...IN, fakeClock: true });
    const b = await mockApp(second, { ...IN, fakeClock: true });
    await page.goto("/");
    await second.goto("/");
    await expect(page.locator("#conn")).toContainText("Live");
    await expect(second.locator("#conn")).toContainText("Live");
    await page.clock.runFor(2000);
    await second.clock.runFor(2000);
    for (const [p, m] of [[page, a], [second, b]] as const) {
      m.setWorker("alpha", { state: "offline" });
      m.emitStream({ type: "worker", worker: "alpha", state: "offline" });
      await p.clock.runFor(6 * 60_000);
    }
    const first = await page.locator("#toasts").getByText(OFFLINE_TEXT).count();
    const other = await second.locator("#toasts").getByText(OFFLINE_TEXT).count();
    expect(first + other, "one tab announces, the other stays quiet").toBe(1);
  });
});

test("a limit that lifts on another worker is not the lifting of the first worker's limit", async ({ page }) => {
  const app = await mockApp(page, { ...IN, fakeClock: true });
  await page.goto("/");
  await expect(page.locator("#conn")).toContainText("Live");
  await page.clock.runFor(2000);
  await expect(page.locator("#capacity")).toContainText("Claude");
  // Claude waits on alpha (the worker the chips read). When alpha goes away the chips read gamma, where Claude is ready: that is another worker, not a lifted limit.
  app.setProviderReady("claude", true, "gamma");
  app.setWorker("alpha", { state: "offline" });
  app.emitStream({ type: "worker", worker: "alpha", state: "offline" });
  await page.clock.runFor(2 * 60_000);
  await expect(page.locator("#toasts")).not.toContainText("capacity again");
  // The same worker going from waiting to ready is announced.
  app.setProviderReady("claude", false, "gamma");
  await page.clock.runFor(61_000);
  app.setProviderReady("claude", true, "gamma");
  await page.clock.runFor(61_000);
  await expect(page.locator("#toasts")).toContainText("Claude has capacity again");
});

test.describe("focus after a route change", () => {
  test("the retry of a failed page load does not move the focus", async ({ page }) => {
    await mockApp(page, IN);
    await page.goto("/");
    await expect(page.locator("#conn")).toContainText("Live");
    let block = true;
    await page.route("**/chunk-*.js", (route) => (block ? route.abort() : route.continue()));
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Workers", exact: true }).click();
    const retry = page.getByRole("button", { name: "Retry" });
    await expect(retry).toBeVisible();
    block = false;
    await retry.click();
    const h1 = page.getByRole("main").getByRole("heading", { level: 1, name: "Workers" });
    await expect(h1).toBeVisible();
    await expect(h1, "a retry is not a navigation").not.toBeFocused();
  });

  test("a text field that has the focus when the page arrives keeps it", async ({ page }) => {
    await mockApp(page, IN);
    await page.goto("/");
    await expect(page.locator("#conn")).toContainText("Live");
    await page.route("**/chunk-*.js", async (route) => {
      await new Promise((r) => setTimeout(r, 1200));
      await route.continue();
    });
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Workers", exact: true }).click();
    await page.evaluate(() => {
      const probe = document.createElement("input");
      probe.id = "probe";
      probe.setAttribute("aria-label", "probe");
      document.body.append(probe);
      probe.focus();
    });
    await expect(page.getByRole("main").getByRole("heading", { level: 1, name: "Workers" })).toBeVisible();
    await expect(page.locator("#probe"), "focus stays in the field").toBeFocused();
  });
});
