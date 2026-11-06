// Visual regression baselines and accessibility scans for every page and state, in light and dark, on deterministic
// data (e2e/mock.ts) with a fixed clock and animations disabled (WEB-003, WEB-009, TEST-011).
//
// Baselines exist for the two Chromium projects (desktop 1440x900 and Pixel 4). They are generated in the official
// Playwright Docker image so that font rendering matches CI (see web/README.md). The same states are scanned with
// axe-core on all four projects, in both themes, and must have no violations of any impact.
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "./fixtures";
import { mockApp, MISSION_PATH, MISSION_START_PATH, type MockOptions } from "./mock";
import type { Page } from "@playwright/test";

interface Screen {
  name: string;
  mock: MockOptions;
  path: string;
  /** Drive the page into the state to capture; return a locator text that proves it is ready. */
  run: (page: Page) => Promise<void>;
  narrowOnly?: boolean;
  wideOnly?: boolean;
}

const IN: MockOptions = { auth: "in" };
const heading = (page: Page, name: string) => expect(page.getByRole("heading", { name, exact: true }).first()).toBeVisible();
const live = (page: Page) => expect(page.locator("#conn")).toContainText("Live");
const fillSetup = async (page: Page) => {
  await page.getByLabel("Setup token").fill("e2e-token");
  await page.getByLabel("Administrator username").fill("admin");
  await page.getByLabel("New password", { exact: true }).fill("correct horse battery");
  await page.getByLabel("Repeat password").fill("correct horse battery");
};
const signInAttempt = async (page: Page) => {
  await page.getByLabel("Username", { exact: true }).fill("admin");
  await page.getByLabel("Password", { exact: true }).fill("not the password");
  await page.getByRole("button", { name: "Sign in" }).click();
};
const nav = async (page: Page, name: string) => {
  const link = page.getByRole("navigation", { name: "Main" }).getByRole("link", { name, exact: true });
  if (await link.isVisible()) await link.click();
  else {
    await page.locator("#more-button").click();
    await page.locator("#more-sheet").getByRole("link", { name, exact: true }).click();
  }
};

const SCREENS: Screen[] = [
  { name: "setup-empty", mock: { auth: "setup" }, path: "/", run: (p) => heading(p, "First-run setup") },
  { name: "setup-filled", mock: { auth: "setup" }, path: "/", run: async (p) => { await fillSetup(p); await expect(p.locator("#strength-text")).toContainText("Strong"); } },
  { name: "setup-weak-password", mock: { auth: "setup" }, path: "/", run: async (p) => { await p.getByLabel("New password", { exact: true }).fill("short"); await expect(p.locator("#strength-text")).toContainText("Too short"); } },
  { name: "setup-validation-errors", mock: { auth: "setup" }, path: "/", run: async (p) => { await p.getByLabel("New password", { exact: true }).fill("short"); await p.getByRole("button", { name: "Create administrator" }).click(); await expect(p.locator("#setup-token-error")).toContainText("Enter the setup token"); } },
  { name: "setup-wrong-token", mock: { auth: "setup", setup: "bad-token" }, path: "/", run: async (p) => { await fillSetup(p); await p.getByRole("button", { name: "Create administrator" }).click(); await expect(p.locator("#setup-token-error")).toContainText("not the setup token"); } },
  { name: "login-empty", mock: { auth: "login" }, path: "/login", run: (p) => heading(p, "Sign in") },
  { name: "login-wrong-password", mock: { auth: "login", login: "wrong" }, path: "/login", run: async (p) => { await signInAttempt(p); await expect(p.locator("#form-error")).toContainText("not correct"); } },
  { name: "login-rate-limited", mock: { auth: "login", login: "limited" }, path: "/login", run: async (p) => { await signInAttempt(p); await expect(p.locator("#form-error")).toContainText("try again in 5 min 0 s (at 12:05:00)"); } },
  { name: "login-caps-lock", mock: { auth: "login" }, path: "/login", run: async (p) => { await p.getByLabel("Password", { exact: true }).focus(); await p.getByLabel("Password", { exact: true }).evaluate((el) => el.dispatchEvent(new KeyboardEvent("keydown", { key: "A", modifierCapsLock: true, bubbles: true }))); await expect(p.locator("#password-caps")).toBeVisible(); } },
  { name: "login-signed-out", mock: IN, path: "/", run: async (p) => { await live(p); await p.locator("#account-button").click(); await p.locator("#logout").click(); await expect(p.locator("#login-notice")).toContainText("You signed out"); } },
  { name: "overview", mock: IN, path: "/", run: async (p) => { await live(p); await expect(p.locator('[data-attention="approval"]')).toBeVisible(); await expect(p.locator("#activity li").first()).toBeVisible(); } },
  { name: "overview-no-workers", mock: { auth: "in", workers: "none" }, path: "/", run: async (p) => { await expect(p.getByRole("heading", { name: "No workers yet" })).toBeVisible(); } },
  { name: "workers", mock: IN, path: "/workers", run: async (p) => { await expect(p.locator("#workers-table tbody tr")).toHaveCount(3); } },
  { name: "workers-none", mock: { auth: "in", workers: "none" }, path: "/workers", run: (p) => expect(p.getByRole("heading", { name: "No workers enrolled" })).toBeVisible() },
  { name: "workers-add-dialog", mock: IN, path: "/workers", run: async (p) => { await p.locator("#add-worker-button").click(); await p.getByLabel("Worker name").fill("Bad Name"); await p.locator("#add-worker").getByRole("button", { name: "Add worker" }).click(); await expect(p.locator("#worker-name-error")).toContainText("lowercase"); } },
  { name: "missions", mock: IN, path: "/missions", run: async (p) => { await expect(p.locator("#missions-table tbody tr")).toHaveCount(8); } },
  { name: "missions-filtered", mock: IN, path: "/missions?status=RUNNING", run: async (p) => { await expect(p.locator("#missions-table tbody tr")).toHaveCount(2); } },
  { name: "missions-no-match", mock: IN, path: "/missions?q=zzz", run: (p) => expect(p.getByRole("heading", { name: "No missions match" })).toBeVisible() },
  { name: "missions-none", mock: { auth: "in", missions: "none" }, path: "/missions", run: (p) => expect(p.getByRole("heading", { name: "No missions yet" })).toBeVisible() },
  { name: "mission-detail", mock: IN, path: MISSION_PATH, run: async (p) => { await expect(p.locator("#tasks-table tbody tr")).toHaveCount(6); await expect(p.locator("#events-list li").first()).toBeVisible(); await expect(p.locator("#mission-status")).toContainText("healthy"); } },
  { name: "mission-confirm-reject", mock: IN, path: MISSION_PATH, run: async (p) => { await expect(p.locator("#tasks-table tbody tr")).toHaveCount(6); await p.getByRole("button", { name: "Reject task 2" }).click(); await expect(p.getByRole("dialog")).toBeVisible(); } },
  { name: "mission-toast-applied", mock: IN, path: MISSION_PATH, run: async (p) => { await expect(p.locator("#tasks-table tbody tr")).toHaveCount(6); await p.getByRole("button", { name: "Pause task 5" }).click(); await expect(p.locator(".toast.applied")).toBeVisible(); } },
  { name: "sessions", mock: IN, path: "/sessions", run: (p) => expect(p.locator("#sessions-table tbody tr")).toHaveCount(2) },
  { name: "usage", mock: IN, path: "/usage", run: (p) => expect(p.locator("#usage-table tbody tr")).toHaveCount(3) },
  { name: "diagnostics", mock: IN, path: "/diagnostics", run: (p) => expect(p.getByRole("heading", { name: "Coordinator" })).toBeVisible() },
  { name: "account", mock: IN, path: "/account", run: async (p) => { await heading(p, "Account"); await p.getByLabel("New password", { exact: true }).fill("a fairly long passphrase"); } },
  { name: "account-menu", mock: { ...IN, sessionRemembered: true }, path: "/", run: async (p) => { await live(p); await p.locator("#account-button").click(); await expect(p.locator("#account-panel")).toBeVisible(); } },
  { name: "shortcut-help", mock: IN, path: "/", run: async (p) => { await live(p); await p.locator("#account-button").click(); await p.locator("#menu-shortcuts").click(); await expect(p.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible(); } },
  { name: "more-sheet", mock: IN, path: "/", narrowOnly: true, run: async (p) => { await live(p); await p.locator("#more-button").click(); await expect(p.locator("#more-sheet")).toBeVisible(); } },
  { name: "not-found", mock: IN, path: "/nowhere", run: (p) => heading(p, "Not found") },
];

for (const theme of ["light", "dark"] as const) {
  for (const s of SCREENS) {
    test(`${s.name} (${theme})`, async ({ page, browserName }, info) => {
      const narrow = (page.viewportSize()?.width ?? 1000) <= 720;
      test.skip(Boolean(s.narrowOnly) && !narrow, "phone only");
      test.skip(Boolean(s.wideOnly) && narrow, "desktop only");
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await mockApp(page, s.mock);
      await page.goto(s.path);
      await s.run(page);
      if (s.mock.auth === "in" && s.path !== "/missions" && !s.name.startsWith("login")) await page.locator("#conn").waitFor();
      await page.evaluate(() => document.fonts.ready);
      await page.mouse.move(0, 0);
      // Focus rings and the caret must not depend on timing.
      await page.evaluate(() => (document.activeElement as HTMLElement | null)?.matches("input, textarea") || (document.activeElement as HTMLElement | null)?.blur?.());
      const axe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]).disableRules(["region"]).analyze();
      expect(axe.violations.map((v) => `${v.impact} ${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`), `axe (${s.name}, ${theme})`).toEqual([]);
      if (process.env["BRIDGE_SKIP_VISUAL"] !== "1" && info.project.name.startsWith("chromium") && browserName === "chromium") await expect(page).toHaveScreenshot(`${s.name}-${theme}.png`);
    });
  }
}

test("mission task UI uses resource reads and idempotent resource mutations", async ({ page }) => {
  const requests: Array<{ method: string; path: string; key?: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.includes("/missions/") && url.pathname.includes("/tasks")) {
      requests.push({ method: request.method(), path: url.pathname, key: request.headers()["idempotency-key"] });
    }
  });
  await mockApp(page, IN);
  await page.goto(MISSION_PATH);
  await expect(page.locator("#tasks-table tbody tr")).toHaveCount(6);
  await page.getByRole("button", { name: "Pause task 5" }).click();
  await expect(page.locator(".toast.applied")).toBeVisible();
  expect(requests.some((request) => request.method === "GET" && request.path.endsWith("/tasks"))).toBe(true);
  expect(requests.some((request) => request.method === "POST" && request.path.endsWith("/tasks/5/pause") && request.key)).toBe(true);
  expect(requests.some((request) => request.path.endsWith("/commands"))).toBe(false);
});

test("a created mission can be started from its detail page through the resource API", async ({ page }) => {
  const requests: Array<{ method: string; path: string; key?: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.includes("/missions/")) requests.push({ method: request.method(), path: url.pathname, key: request.headers()["idempotency-key"] });
  });
  await mockApp(page, IN);
  await page.goto(MISSION_START_PATH);
  await expect(page.getByRole("heading", { name: "Add telemetry opt-out" })).toBeVisible();
  await page.getByRole("button", { name: "Start mission" }).click();
  await expect(page.locator(".toast.applied")).toContainText("Start mission");
  await expect(page.locator("#mission-status")).toContainText("RUNNING");
  expect(requests.some((request) => request.method === "POST" && request.path.endsWith("/start") && request.key)).toBe(true);
  expect(requests.some((request) => request.path.endsWith("/commands"))).toBe(false);
});
