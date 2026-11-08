// PRD-038: "Delete mission" in the web console, end to end with the real Python worker and the real coordinator.
// The action exists only for finished or never-started missions, asks for confirmation with the mission title, removes the
// mission on the worker (SQLite rows) and from the coordinator's copy, and returns to the missions list with a toast.
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { expect, expectAccessible, navTo, setupViaUi, test, type Coordinator } from "./fixtures";

const repo = resolve(import.meta.dirname, "../..");
const python = join(repo, ".venv", "bin", "python");

function cleanEnv(state: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) if (!k.startsWith("LLM_") && k !== "TMUX" && k !== "TMUX_PANE") env[k] = v;
  env["LLM_BRIDGE_STATE_DIR"] = state;
  env["XDG_RUNTIME_DIR"] = mkdtempSync(join(tmpdir(), "bridge-e2e-rt-"));
  return env;
}

interface Setup {
  idle: string;
  busy: string;
  bridge: (args: string[], input?: string) => string;
  setStatus: (missionId: string, status: string) => void;
  stopWorker: () => Promise<void>;
  cleanup: () => void;
}

async function start(coord: Coordinator): Promise<Setup> {
  const state = mkdtempSync(join(tmpdir(), "bridge-e2e-delete-"));
  const env = cleanEnv(state);
  const bridge = (args: string[], input?: string) => execFileSync(python, [join(repo, "llm-bridge"), ...args], { env, encoding: "utf8", input, cwd: repo });
  const create = (name: string, folder: string) =>
    (JSON.parse(bridge(["mission", "create", "--prompt", `${name} objective`, "--name", name, "--cwd", folder, "--providers", "codex", "--json"])) as { mission: { id: string } }).mission.id;
  const idle = create("Never started mission", mkdtempSync(join(tmpdir(), "bridge-e2e-idle-")));
  const busy = create("Running mission", mkdtempSync(join(tmpdir(), "bridge-e2e-busy-")));
  const setStatus = (missionId: string, status: string) =>
    execFileSync(python, ["-c", `import sys; sys.path.insert(0, ${JSON.stringify(repo)}); from llm_tools.bridge_store import BridgeStore, store_path
with BridgeStore(store_path()) as s: s.set_mission_status(${JSON.stringify(missionId)}, ${JSON.stringify(status)})`], { env, cwd: repo });
  // A running mission without a controller: the state the worker must refuse to delete and the page must not offer to.
  setStatus(busy, "RUNNING");
  const token = coord.cli("worker", "add", "e2e-worker");
  bridge(["worker", "enroll", "--name", "e2e-worker", "--coordinator", coord.url.replace("https://", "wss://"), "--token-file", "-", "--ca-file", join(coord.dir, "ca.pem"), "--allow-control"], token);
  const child: ChildProcess = spawn(python, [join(repo, "llm-bridge"), "worker", "run"], { env, cwd: repo, stdio: "ignore" });
  return {
    idle,
    busy,
    bridge,
    setStatus,
    stopWorker: async () => {
      const exited = new Promise<void>((r) => child.once("exit", () => r()));
      child.kill("SIGTERM");
      await Promise.race([exited, new Promise((r) => setTimeout(r, 8000))]);
      child.kill("SIGKILL");
    },
    cleanup: () => {
      child.kill("SIGKILL");
      rmSync(state, { recursive: true, force: true });
    },
  };
}

test.describe("delete mission", () => {
  test.skip(!existsSync(python), "repo .venv python is missing");

  test("is offered only for a finished or never-started mission, asks first, and removes the mission everywhere", async ({ page, coord }) => {
    const fake = await start(coord);
    try {
      await setupViaUi(page, coord);
      await navTo(page, "Missions");
      await expect(page.getByRole("link", { name: "Never started mission" })).toBeVisible({ timeout: 30_000 });
      await expect(page.getByRole("link", { name: "Running mission" })).toBeVisible();

      // A running mission has no Delete mission action.
      await page.getByRole("link", { name: "Running mission" }).click();
      await expect(page.getByRole("heading", { name: "Running mission" })).toBeVisible();
      await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
      // The header is drawn again when the task list arrives and an open menu would be closed with it: wait for the list (the tab shows its count).
      await expect(page.getByRole("tab", { name: /^Tasks \d+$/ })).toBeVisible();
      await page.getByRole("button", { name: "More mission actions" }).click();
      await expect(page.getByRole("menuitem", { name: "Cancel mission" })).toBeVisible();
      await expect(page.locator('[data-action="delete-mission"]')).toHaveCount(0);
      await page.keyboard.press("Escape");
      await navTo(page, "Missions");

      // A never-started mission has it; the dialog names the mission, and going back changes nothing.
      await page.getByRole("link", { name: "Never started mission" }).click();
      await expect(page.getByRole("heading", { name: "Never started mission" })).toBeVisible();
      await expect(page.getByRole("tab", { name: /^Tasks \d+$/ })).toBeVisible();
      // The action is in the mission's overflow menu (the redesign moved it there from a button beside the other actions).
      const remove = page.locator('[data-action="delete-mission"]');
      const openDelete = async () => {
        await page.getByRole("button", { name: "More mission actions" }).click();
        await remove.click();
      };
      await page.getByRole("button", { name: "More mission actions" }).click();
      await expect(remove).toBeVisible();
      await expect(remove).toHaveText("Delete mission");
      await expect(remove).toHaveAttribute("role", "menuitem");
      await remove.click();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toContainText('"Never started mission"');
      await expect(dialog).toContainText("cannot be undone");
      await expectAccessible(page, "delete mission dialog");
      await dialog.getByRole("button", { name: "Go back" }).click();
      await expect(dialog).toBeHidden();
      await expect(page.getByRole("heading", { name: "Never started mission" })).toBeVisible();
      expect(fake.bridge(["mission", "list", "--json"])).toContain(fake.idle);

      // Confirm: back on the missions list with a toast; the mission is gone from the worker and from the coordinator.
      await openDelete();
      await page.locator("#dialog-confirm").click();
      await expect(page).toHaveURL(/\/missions$/);
      await expect(page.locator(".toast", { hasText: /Deleted mission "Never started mission"/ })).toBeVisible({ timeout: 20_000 });
      await expect(page.getByRole("link", { name: "Never started mission" })).toHaveCount(0);
      await expect(page.getByRole("link", { name: "Running mission" })).toBeVisible();
      const local = fake.bridge(["mission", "list", "--json"]);
      expect(local).not.toContain(fake.idle);
      expect(local).toContain(fake.busy);
      await page.reload();
      await expect(page.getByRole("link", { name: "Running mission" })).toBeVisible({ timeout: 20_000 });
      await expect(page.getByRole("link", { name: "Never started mission" })).toHaveCount(0);
      const listed = await page.evaluate(async () => (await (await fetch("/api/v1/missions", { credentials: "same-origin" })).json()) as { missions: Array<{ mission: { id: string } }> });
      expect(listed.missions.map((m) => m.mission.id)).toEqual([fake.busy]);
    } finally {
      fake.cleanup();
    }
  });

  test("a worker that refuses leaves the mission and the page in place", async ({ page, coord }) => {
    const fake = await start(coord);
    try {
      await setupViaUi(page, coord);
      await navTo(page, "Missions");
      await page.getByRole("link", { name: "Never started mission" }).click();
      await expect(page.getByRole("heading", { name: "Never started mission" })).toBeVisible({ timeout: 30_000 });
      await expect(page.getByRole("tab", { name: /^Tasks \d+$/ })).toBeVisible();
      // The mission starts running on the worker after the page was drawn: the worker refuses and says why.
      fake.setStatus(fake.idle, "RUNNING");
      await page.getByRole("button", { name: "More mission actions" }).click();
      await page.locator('[data-action="delete-mission"]').click();
      await page.locator("#dialog-confirm").click();
      await expect(page.locator(".toast", { hasText: /is running, so it cannot be deleted/ })).toBeVisible({ timeout: 20_000 });
      await expect(page).toHaveURL(new RegExp(`/missions/e2e-worker/${fake.idle}$`));
      expect(fake.bridge(["mission", "list", "--json"])).toContain(fake.idle);
    } finally {
      fake.cleanup();
    }
  });
});
