// PRD-016: operator actions of the console that the API already supports and the CLI offers (finish, task priority, prerequisite and prompt, rotation, settings).
// The worker is simulated by routes in the page: each test records the request the page sends and checks its path, operation and body.
import type { Page, Route } from "@playwright/test";
import { expect, test } from "./fixtures";
import { DENSE_CALM_URL, mockApp, type MockOptions } from "./mock";
import { MISSION_URL, settle, showTab } from "./redesign/harness";

interface Seen {
  method: string;
  path: string;
  body: Record<string, unknown>;
}

const ROW = { request_id: "r", target: "alpha", state: "done" };
const done = (operation: string, result: Record<string, unknown> = {}) => ({ ...ROW, operation, result: { outcome: "applied", result, error: null } });

/** Answer the mutating requests of this spec in the page and keep what was sent. */
async function worker(page: Page): Promise<Seen[]> {
  const seen: Seen[] = [];
  const answer = (route: Route, body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  const parse = (route: Route): Record<string, unknown> => {
    try {
      return JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>;
    } catch {
      return {};
    }
  };
  await page.route(/\/api\/v1\/missions\/[^/]+\/(finish|stop|tasks\/\d+)(\?|$)/, (route) => {
    const req = route.request();
    if (req.method() === "GET") return route.fallback();
    const url = new URL(req.url());
    seen.push({ method: req.method(), path: url.pathname.replace(/^.*\/missions\/[^/]+/, ""), body: parse(route) });
    return answer(route, done("task.change"));
  });
  const settings = { auto_continue: false, suspend_when_rate_limited: false, execution_policy: { teams_enabled: false, token_spend: "medium", legacy: false } };
  let order = ["codex", "claude"];
  await page.route(/\/api\/v1\/commands$/, (route) => {
    const request = ((parse(route)["request"] ?? {}) as { operation?: string; params?: Record<string, unknown> });
    const op = request.operation ?? "";
    if (!op.startsWith("mission.rotation.") && !op.startsWith("mission.settings.")) return route.fallback();
    seen.push({ method: "COMMAND", path: op, body: request.params ?? {} });
    if (op === "mission.rotation.put") order = String(request.params?.["set"] ?? "").split(",");
    if (op === "mission.settings.patch") Object.assign(settings, request.params ?? {});
    const result = op.startsWith("mission.rotation.") ? { mode: "providers", entries: order.map((name, i) => ({ position: i + 1, name })), selected: order[0], applies: "Applies from the next provider selection." } : op === "mission.settings.get" ? settings : {};
    return answer(route, done(op, result));
  });
  return seen;
}

/** The mock answers first-registered last, so the worker routes are added after it. */
async function open(page: Page, opts: MockOptions, path: string): Promise<Seen[]> {
  await mockApp(page, opts);
  const seen = await worker(page);
  await page.goto(path);
  await settle(page);
  return seen;
}

const moreOn = (page: Page, task: number) => page.getByRole("button", { name: `More actions for task ${task}` });

test("Finish asks for confirmation and sends mission.finish for the mission", async ({ page }) => {
  const seen = await open(page, { auth: "in", dataset: "dense" }, DENSE_CALM_URL);
  await page.getByRole("button", { name: "More mission actions" }).click();
  await page.getByRole("menuitem", { name: /Finish/ }).click();
  await page.getByRole("dialog").locator("[data-confirm]").click();
  await expect.poll(() => seen.filter((s) => s.path === "/finish").length).toBe(1);
  expect(seen.find((s) => s.path === "/finish")!.method).toBe("POST");
});

test("Force stop confirms first and sends stop with force", async ({ page }) => {
  const seen = await open(page, { auth: "in", dataset: "dense" }, DENSE_CALM_URL);
  await page.getByRole("button", { name: "More mission actions" }).click();
  await page.getByRole("menuitem", { name: "Force stop" }).click();
  await expect(page.getByRole("dialog")).toContainText("without waiting for its current step");
  await page.getByRole("dialog").locator("[data-confirm]").click();
  await expect.poll(() => seen.filter((s) => s.path === "/stop").length).toBe(1);
  expect(seen.find((s) => s.path === "/stop")!.body["force"]).toBe(true);
});

test("Change priority sends the new value with the task revision, and Undo writes the earlier value back", async ({ page }) => {
  const seen = await open(page, { auth: "in" }, MISSION_URL);
  await showTab(page, "Tasks");
  await moreOn(page, 5).click();
  await page.getByRole("menuitem", { name: "Change priority" }).click();
  await page.getByLabel("Priority", { exact: true }).fill("7");
  await page.getByRole("dialog").locator("[data-confirm]").click();
  await expect.poll(() => seen.length).toBe(1);
  expect(seen[0]).toMatchObject({ method: "PATCH", path: "/tasks/5", body: { priority: 7, expected_revision: 1 } });
  await page.locator("#toasts").getByRole("button", { name: "Undo" }).click();
  await expect.poll(() => seen.length).toBe(2);
  expect(seen[1]!.body["priority"]).toBe(0);
  expect(seen[1]!.body).not.toHaveProperty("expected_revision");
});

test("Add prerequisite sends the task number; a number that is not a task number sends nothing", async ({ page }) => {
  const seen = await open(page, { auth: "in" }, MISSION_URL);
  await showTab(page, "Tasks");
  await moreOn(page, 5).click();
  await page.getByRole("menuitem", { name: "Add prerequisite" }).click();
  await page.getByLabel("Task number").fill("5");
  await page.getByRole("dialog").locator("[data-confirm]").click();
  await expect(page.locator("#toasts")).toContainText("Enter the number of another task");
  expect(seen).toEqual([]);
  await moreOn(page, 5).click();
  await page.getByRole("menuitem", { name: "Add prerequisite" }).click();
  await page.getByLabel("Task number").fill("1");
  await page.getByRole("dialog").locator("[data-confirm]").click();
  await expect.poll(() => seen.length).toBe(1);
  expect(seen[0]).toMatchObject({ method: "PATCH", path: "/tasks/5", body: { depends_on: 1 } });
});

test("Amend prompt sends the new prompt as a revision", async ({ page }) => {
  const seen = await open(page, { auth: "in" }, MISSION_URL);
  await showTab(page, "Tasks");
  await moreOn(page, 5).click();
  await page.getByRole("menuitem", { name: "Amend prompt" }).click();
  const prompt = page.getByLabel("Prompt", { exact: true });
  await expect(prompt).toHaveValue(/Write the changelog entry/);
  await prompt.fill("Write the changelog entry and the migration note");
  await page.getByRole("dialog").locator("[data-confirm]").click();
  await expect.poll(() => seen.length).toBe(1);
  expect(seen[0]).toMatchObject({ method: "PATCH", path: "/tasks/5", body: { prompt: "Write the changelog entry and the migration note", requeue: false } });
});

test("Rotation and settings are read when opened and each save is one typed call that Undo reverses", async ({ page }) => {
  const seen = await open(page, { auth: "in" }, MISSION_URL);
  await page.locator("#mission-control summary").click();
  const rotation = page.locator("#rotation-input");
  await expect(rotation).toHaveValue("codex,claude");
  await rotation.fill("claude,codex");
  await page.locator("#rotation-save").click();
  await expect.poll(() => seen.filter((s) => s.path === "mission.rotation.put").length).toBe(1);
  expect(seen.find((s) => s.path === "mission.rotation.put")!.body).toEqual({ set: "claude,codex" });
  await expect(rotation).toHaveValue("claude,codex");
  await page.locator("#toasts").getByRole("button", { name: "Undo" }).click();
  await expect.poll(() => seen.filter((s) => s.path === "mission.rotation.put").length).toBe(2);
  expect(seen.filter((s) => s.path === "mission.rotation.put")[1]!.body).toEqual({ set: "codex,claude" });

  await page.locator("#setting-auto-continue").check();
  await page.locator("#setting-spend").selectOption("high");
  await page.locator("#settings-save").click();
  await expect.poll(() => seen.filter((s) => s.path === "mission.settings.patch").length).toBe(1);
  expect(seen.find((s) => s.path === "mission.settings.patch")!.body).toEqual({ auto_continue: true, token_spend: "high" });
});
