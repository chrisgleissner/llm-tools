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

const fresh = () => ({
  task: { priority: 0, revision: 1, prompt: "Write the changelog entry in detail" },
  patchStatus: 200,
  showFails: false,
  failPut: false,
  rotation: ["codex", "claude"],
  settings: { auto_continue: false, suspend_when_rate_limited: false, execution_policy: { teams_enabled: false, token_spend: "medium", legacy: false } },
});
/** What the simulated worker holds. A test changes it to play another operator, or a failure. */
let world = fresh();

/** Answer the mutating requests of this spec in the page and keep what was sent. */
async function worker(page: Page): Promise<Seen[]> {
  const seen: Seen[] = [];
  const answer = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  const parse = (route: Route): Record<string, unknown> => {
    try {
      return JSON.parse(route.request().postData() ?? "{}") as Record<string, unknown>;
    } catch {
      return {};
    }
  };
  await page.route(/\/api\/v1\/missions\/[^/]+\/(finish|stop|tasks\/\d+)(\?|$)/, (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^.*\/missions\/[^/]+/, "");
    if (req.method() === "GET") {
      if (path !== "/tasks/5") return route.fallback();
      if (world.showFails) return answer(route, { error: { code: "unavailable", message: "worker unavailable" } }, 503);
      return answer(route, done("task.show", { task: { ordinal: 5, title: "Write the changelog entry", ...world.task } }));
    }
    const body = parse(route);
    seen.push({ method: req.method(), path, body });
    if (path.startsWith("/tasks/") && world.patchStatus === 412) return answer(route, { error: { code: "stale-revision", message: "the task changed since the supplied revision; fetch it again and retry" } }, 412);
    if (typeof body["priority"] === "number") world.task = { ...world.task, priority: body["priority"] };
    return answer(route, done("task.change"));
  });
  await page.route(/\/api\/v1\/commands$/, (route) => {
    const request = ((parse(route)["request"] ?? {}) as { operation?: string; params?: Record<string, unknown> });
    const op = request.operation ?? "";
    if (!op.startsWith("mission.rotation.") && !op.startsWith("mission.settings.")) return route.fallback();
    if (op.endsWith(".put") || op.endsWith(".patch")) {
      seen.push({ method: "COMMAND", path: op, body: request.params ?? {} });
      if (world.failPut) return answer(route, { ...ROW, operation: op, result: { outcome: "rejected", result: null, error: { code: "invalid-argument", message: "the worker refused it" } } });
    }
    if (op === "mission.rotation.put") world.rotation = String(request.params?.["set"] ?? "").split(",");
    if (op === "mission.settings.patch") Object.assign(world.settings, request.params ?? {});
    const result = op.startsWith("mission.rotation.") ? { mode: "providers", entries: world.rotation.map((name, i) => ({ position: i + 1, name })), selected: world.rotation[0], applies: "Applies from the next provider selection." } : op === "mission.settings.get" ? world.settings : {};
    return answer(route, done(op, result));
  });
  return seen;
}

/** The mock answers first-registered last, so the worker routes are added after it. */
async function open(page: Page, opts: MockOptions, path: string): Promise<Seen[]> {
  world = fresh();
  await mockApp(page, opts);
  const seen = await worker(page);
  await page.goto(path);
  await settle(page);
  return seen;
}

const moreOn = (page: Page, task: number) => page.getByRole("button", { name: `More actions for task ${task}` });
const undo = (page: Page) => page.locator("#toasts").getByRole("button", { name: "Undo" });

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

/** Open the priority dialog of task 5 and set `value`. */
async function setPriority(page: Page, value: string): Promise<void> {
  await showTab(page, "Tasks");
  await moreOn(page, 5).click();
  await page.getByRole("menuitem", { name: "Change priority" }).click();
  await page.getByLabel("Priority", { exact: true }).fill(value);
  await page.getByRole("dialog").locator("[data-confirm]").click();
}

test("Change priority starts from the worker's value and revision, and Undo restores that value with the revision it read", async ({ page }) => {
  const seen = await open(page, { auth: "in" }, MISSION_URL);
  world.task = { ...world.task, priority: 3, revision: 9 }; // the list the page loaded says priority 0, revision 1
  await showTab(page, "Tasks");
  await moreOn(page, 5).click();
  await page.getByRole("menuitem", { name: "Change priority" }).click();
  await expect(page.getByLabel("Priority", { exact: true })).toHaveValue("3");
  await page.getByLabel("Priority", { exact: true }).fill("7");
  await page.getByRole("dialog").locator("[data-confirm]").click();
  await expect.poll(() => seen.length).toBe(1);
  expect(seen[0]).toMatchObject({ method: "PATCH", path: "/tasks/5", body: { priority: 7, expected_revision: 9 } });
  await undo(page).click();
  await expect.poll(() => seen.length).toBe(2);
  expect(seen[1]!.body).toMatchObject({ priority: 3, expected_revision: 9 });
});

test("Priority Undo does nothing when another operator changed the task since", async ({ page }) => {
  const seen = await open(page, { auth: "in" }, MISSION_URL);
  await setPriority(page, "7");
  await expect.poll(() => seen.length).toBe(1);
  world.task = { ...world.task, priority: 8 };
  await undo(page).click();
  await expect(page.locator("#toasts")).toContainText("changed elsewhere; not undone");
  expect(seen).toHaveLength(1);
});

test("Priority Undo that meets a revision conflict says so and is not forced", async ({ page }) => {
  const seen = await open(page, { auth: "in" }, MISSION_URL);
  await setPriority(page, "7");
  await expect.poll(() => seen.length).toBe(1);
  world.patchStatus = 412;
  await undo(page).click();
  await expect(page.locator("#toasts")).toContainText("changed elsewhere; not undone");
  expect(seen).toHaveLength(2); // the one write that was refused; no second attempt without the revision
  expect(seen[1]!.body).toHaveProperty("expected_revision");
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

test("Amend prompt with an unreadable prompt shows an error and opens no dialog", async ({ page }) => {
  const seen = await open(page, { auth: "in" }, MISSION_URL);
  world.showFails = true;
  await showTab(page, "Tasks");
  await moreOn(page, 5).click();
  await page.getByRole("menuitem", { name: "Amend prompt" }).click();
  await expect(page.locator("#toasts")).toContainText("Could not read the prompt of task 5");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(seen).toEqual([]);
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
  await undo(page).click();
  await expect.poll(() => seen.filter((s) => s.path === "mission.rotation.put").length).toBe(2);
  expect(seen.filter((s) => s.path === "mission.rotation.put")[1]!.body).toEqual({ set: "codex,claude" });

  await page.locator("#setting-auto-continue").check();
  await page.locator("#setting-spend").selectOption("high");
  await page.locator("#settings-save").click();
  await expect.poll(() => seen.filter((s) => s.path === "mission.settings.patch").length).toBe(1);
  expect(seen.find((s) => s.path === "mission.settings.patch")!.body).toEqual({ auto_continue: true, token_spend: "high" });
});

test("Rotation Undo is refused when the worker's rotation is no longer what this save wrote", async ({ page }) => {
  const seen = await open(page, { auth: "in" }, MISSION_URL);
  await page.locator("#mission-control summary").click();
  await expect(page.locator("#rotation-input")).toHaveValue("codex,claude");
  await page.locator("#rotation-input").fill("claude,codex");
  await page.locator("#rotation-save").click();
  await expect.poll(() => seen.filter((s) => s.path === "mission.rotation.put").length).toBe(1);
  world.rotation = ["gemini"];
  await undo(page).click();
  await expect(page.locator("#toasts")).toContainText("Changed elsewhere; not undone");
  expect(seen.filter((s) => s.path === "mission.rotation.put")).toHaveLength(1);
});

test("Settings Undo is refused when a setting is no longer what this save wrote", async ({ page }) => {
  const seen = await open(page, { auth: "in" }, MISSION_URL);
  await page.locator("#mission-control summary").click();
  await expect(page.locator("#setting-auto-continue")).toBeVisible();
  await page.locator("#setting-auto-continue").check();
  await page.locator("#settings-save").click();
  await expect.poll(() => seen.filter((s) => s.path === "mission.settings.patch").length).toBe(1);
  world.settings = { ...world.settings, auto_continue: false };
  await undo(page).click();
  await expect(page.locator("#toasts")).toContainText("Changed elsewhere; not undone");
  expect(seen.filter((s) => s.path === "mission.settings.patch")).toHaveLength(1);
});

test("A refused rotation save keeps the form, the typed text and the field", async ({ page }) => {
  await open(page, { auth: "in" }, MISSION_URL);
  await page.locator("#mission-control summary").click();
  const input = page.locator("#rotation-input");
  await expect(input).toHaveValue("codex,claude");
  await input.evaluate((el) => el.setAttribute("data-same-element", "1"));
  await input.fill("a,b,typed");
  world.failPut = true;
  await page.locator("#rotation-save").click();
  await expect(page.locator("#toasts")).toContainText("Could not save the rotation");
  await expect(page.locator('#rotation-input[data-same-element="1"]')).toHaveValue("a,b,typed");
});
