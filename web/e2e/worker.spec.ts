// End-to-end with the real Python worker (repo .venv) against the real coordinator and a real Bridge mission database.
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { showTab } from "./redesign/harness";
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

  test("mission appears, a pause/resume/cancel round-trips, and the offline state follows the worker", async ({ page, coord }) => {
    const fake = await startFakeWorker(coord);
    try {
      await setupViaUi(page, coord);
      // Workers: online.
      await navTo(page, "Workers");
      const online = page.locator('[data-entity="worker:e2e-worker"] .pill[data-state="online"]');
      await expect(online).toHaveText("Online", { timeout: 20_000 });
      // A pill in a list never animates (calm design: at most one running animation per document).
      await page.emulateMedia({ reducedMotion: "no-preference" });
      expect(await online.locator("svg").evaluate((el) => getComputedStyle(el).animationName)).toBe("none");
      // Missions index: shows the mission, not offline.
      await navTo(page, "Missions");
      const link = page.locator("a.row-title", { hasText: "Browser E2E mission" });
      await expect(link).toBeVisible({ timeout: 20_000 });
      await expect(link).toHaveAttribute("href", `/missions/e2e-worker/${fake.missionId}`);
      const row = page.locator('[data-entity^="mission:"]').first();
      await expect(row).toContainText("e2e-worker");
      await expect(row.locator('.pill[data-state="offline"]')).toHaveCount(0);
      await expect(row).not.toContainText(/stale/i);
      await expectNoHorizontalScroll(page);
      await expectAccessible(page, "missions with data");

      let fullStatusReads = 0;
      page.on("request", (request) => {
        const url = new URL(request.url());
        if (request.method() === "GET" && url.pathname === `/api/v1/missions/${fake.missionId}`) fullStatusReads += 1;
      });
      // Compact status is already in the replicated mission row; tasks remain an on-demand worker read.
      await link.click();
      await expect(page.getByRole("heading", { name: "Browser E2E mission" })).toBeVisible();
      await showTab(page, "Tasks");
      const tasks = page.locator('[data-entity^="task:"]');
      await expect(tasks).toHaveCount(2, { timeout: 20_000 });
      expect(fullStatusReads).toBe(0);
      await expectNoHorizontalScroll(page);
      await expectAccessible(page, "mission detail");
      await showTab(page, "Activity");
      await expect(page.locator("#events-list li").first()).toBeVisible({ timeout: 20_000 });
      await expect(page.locator('#events-list [data-event-kind="mission-created"]')).toHaveCount(1);
      await showTab(page, "Tasks");

      // Pause task 2 (round trip through coordinator, worker, SQLite and back): Pause is in the row menu, Resume is inline.
      const second = tasks.filter({ hasText: "Second task" });
      await second.getByRole("button", { name: "More actions for task 2" }).click();
      await page.getByRole("menuitem", { name: "Pause task" }).click();
      await expect(second.locator(".pill")).toHaveText("Paused", { timeout: 20_000 });
      await expect(page.locator(".toast", { hasText: /Paused task 2/ })).toBeVisible();
      await second.getByRole("button", { name: "Resume task 2" }).click();
      await expect(second.locator(".pill")).not.toHaveText("Paused", { timeout: 20_000 });

      // On a phone a toast sits over the lower part of the list and, while the pointer rests on it, does not expire: dismiss them as an operator would.
      for (let i = 0; i < 5 && (await page.locator(".toast").count()) > 0; i += 1) await page.getByRole("button", { name: "Dismiss notification" }).first().click();
      await expect(page.locator(".toast")).toHaveCount(0);

      // Destructive action: cancel asks for confirmation; declining changes nothing.
      const cancel = async () => {
        await second.getByRole("button", { name: "More actions for task 2" }).click();
        await page.getByRole("menuitem", { name: "Cancel task" }).click();
      };
      await cancel();
      const dialog = page.getByRole("dialog");
      await expect(dialog).toBeVisible();
      await expectAccessible(page, "confirmation dialog");
      await dialog.getByRole("button", { name: "Go back" }).click();
      await expect(dialog).toBeHidden();
      await expect(second.locator(".pill")).not.toHaveText("Cancelled");
      await cancel();
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await expect(second.locator(".pill")).not.toHaveText("Cancelled");
      await cancel();
      await page.locator("#dialog-confirm").click();
      await expect(second.locator(".pill")).toHaveText("Cancelled", { timeout: 20_000 });

      // Stop the worker: the mission and worker become offline while the page is open, and mutations say why they are disabled.
      await fake.stopWorker();
      await expect(page.locator('.mission-head .pill[data-state="offline"]')).toBeVisible({ timeout: 20_000 });
      await expect(page.getByRole("button", { name: "Add task" })).toBeDisabled();
      await expect(page.locator(".mission-head")).toContainText(/offline/i);
      await navTo(page, "Workers");
      await expect(page.locator('[data-entity="worker:e2e-worker"] .pill[data-state="offline"]')).toHaveText("Offline");
      await navTo(page, "Missions");
      await expect(page.locator('[data-entity^="mission:"] .pill[data-state="offline"]')).toBeVisible();
    } finally {
      fake.cleanup();
    }
  });
});
