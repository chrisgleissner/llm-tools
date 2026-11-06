// End-to-end with the real Python worker (repo .venv) against the real coordinator and a real Bridge mission database.
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { expect, expectAccessible, expectNoHorizontalScroll, navTo, setupViaUi, test, type Coordinator } from "./fixtures";

const repo = resolve(import.meta.dirname, "../..");
const python = join(repo, ".venv", "bin", "python");

function cleanEnv(state: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) if (!k.startsWith("LLM_") && k !== "TMUX" && k !== "TMUX_PANE") env[k] = v;
  env["LLM_BRIDGE_STATE_DIR"] = state;
  return env;
}

interface Fake {
  missionId: string;
  stopWorker: () => Promise<void>;
  cleanup: () => void;
}

async function startFakeWorker(coord: Coordinator): Promise<Fake> {
  const state = mkdtempSync(join(tmpdir(), "bridge-e2e-worker-"));
  const env = cleanEnv(state);
  const bridge = (args: string[], input?: string) => execFileSync(python, [join(repo, "llm-bridge"), ...args], { env, encoding: "utf8", input, cwd: repo });
  const created = JSON.parse(bridge(["mission", "create", "--prompt", "Browser E2E mission", "--cwd", tmpdir(), "--providers", "codex", "--json"])) as { mission: { id: string } };
  const missionId = created.mission.id;
  bridge(["task", "add", "--mission", missionId, "--prompt", "Second task", "--title", "Second task"]);
  const token = coord.cli("worker", "add", "e2e-worker");
  bridge(["worker", "enroll", "--name", "e2e-worker", "--coordinator", coord.url.replace("https://", "wss://"), "--token-file", "-", "--ca-file", join(coord.dir, "ca.pem"), "--allow-control"], token);
  const child: ChildProcess = spawn(python, [join(repo, "llm-bridge"), "worker", "run"], { env, cwd: repo, stdio: "ignore" });
  return {
    missionId,
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

test.describe("with a connected worker", () => {
  test.skip(!existsSync(python), "repo .venv python is missing");

  test("mission appears, a pause/resume/cancel round-trips, and stale marking follows the worker", async ({ page, coord }) => {
    const fake = await startFakeWorker(coord);
    try {
      await setupViaUi(page, coord);
      // Workers: online.
      await navTo(page, "Workers");
      await expect(page.locator('tr[data-worker="e2e-worker"]').getByText("online", { exact: true })).toBeVisible({ timeout: 20_000 });
      // Missions index: shows the mission, not stale.
      await navTo(page, "Missions");
      const link = page.getByRole("link", { name: "Browser E2E mission" });
      await expect(link).toBeVisible({ timeout: 20_000 });
      await expect(link).toHaveAttribute("href", `/missions/e2e-worker/${fake.missionId}`);
      const row = page.locator("#missions-table tbody tr").first();
      await expect(row).toContainText("e2e-worker");
      await expect(row.locator(".badge.stale")).toHaveCount(0);
      await expectNoHorizontalScroll(page);
      await expectAccessible(page, "missions with data");

      // Mission detail: status and tasks come from the worker via commands.
      await link.click();
      await expect(page.getByRole("heading", { name: "Browser E2E mission" })).toBeVisible();
      const tasks = page.locator("#tasks-table tbody tr");
      await expect(tasks).toHaveCount(2, { timeout: 20_000 });
      await expect(page.locator("#mission-status")).toContainText("status");
      await expect(page.locator("#events-list li").first()).toBeVisible({ timeout: 20_000 });
      await expectNoHorizontalScroll(page);
      await expectAccessible(page, "mission detail");

      // Pause task 2 (round trip through coordinator, worker, SQLite and back).
      const second = page.locator("#tasks-table tbody tr", { hasText: "Second task" });
      await second.getByRole("button", { name: "Pause" }).click();
      await expect(second.locator(".badge")).toHaveText("paused", { timeout: 20_000 });
      await expect(page.locator(".toast", { hasText: /Pause task 2/ })).toBeVisible();
      await second.getByRole("button", { name: "Resume" }).click();
      await expect(second.locator(".badge")).not.toHaveText("paused", { timeout: 20_000 });

      // Destructive action: cancel asks for confirmation; declining changes nothing.
      await second.getByRole("button", { name: "Cancel" }).click();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      await expectAccessible(page, "confirmation dialog");
      await dialog.getByRole("button", { name: "Go back" }).click();
      await expect(dialog).toBeHidden();
      await expect(second.locator(".badge")).not.toHaveText("cancelled");
      await second.getByRole("button", { name: "Cancel" }).click();
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await expect(second.locator(".badge")).not.toHaveText("cancelled");
      await second.getByRole("button", { name: "Cancel" }).click();
      await page.locator("#dialog-confirm").click();
      await expect(second.locator(".badge")).toHaveText("cancelled", { timeout: 20_000 });

      // Stop the worker: the mission and worker become stale/offline while the page is open.
      await fake.stopWorker();
      await expect(page.locator("#mission-tasks").getByRole("button", { name: "Pause" }).first()).toBeDisabled({ timeout: 20_000 });
      await expect(page.locator(".page-head .badge.stale")).toBeVisible({ timeout: 20_000 });
      await expect(page.locator(".page-head .notice")).toContainText("not connected");
      await navTo(page, "Workers");
      await expect(page.locator('tr[data-worker="e2e-worker"]').getByText("offline", { exact: true })).toBeVisible();
      await navTo(page, "Missions");
      await expect(page.locator("#missions-table .badge.stale")).toBeVisible();
    } finally {
      fake.cleanup();
    }
  });
});
