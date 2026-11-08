// Sessions and Usage views against the real Python worker (read-only enrolment) and the real coordinator.
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { expect, expectAccessible, expectNoHorizontalScroll, navTo, setupViaUi, test, type Coordinator } from "./fixtures";

const repo = resolve(import.meta.dirname, "../..");
const python = join(repo, ".venv", "bin", "python");
const SESSION_ID = "0b0f6d3e-1c2a-4f5b-8d7e-123456789abc";

function workerEnv(state: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) if (!k.startsWith("LLM_") && k !== "TMUX" && k !== "TMUX_PANE") env[k] = v;
  const now = Math.floor(Date.now() / 1000);
  env["LLM_BRIDGE_STATE_DIR"] = state;
  env["LLM_USAGE_PROVIDERS"] = "codex";
  env["LLM_USAGE_NO_SERVICE"] = "1";
  env["LLM_USAGE_DISABLE_CODEX_APP_SERVER"] = "1";
  env["LLM_USAGE_CODEX_RATE_LIMITS_JSON"] = JSON.stringify({
    rateLimits: {
      primary: { usedPercent: 25, windowDurationMins: 300, resetsAt: now + 3600 },
      secondary: { usedPercent: 10, windowDurationMins: 10080, resetsAt: now + 86400 },
    },
  });
  return env;
}

const RECORD = `
import os, sys
from llm_tools.bridge_store import BridgeStore
from pathlib import Path
mission_id, session_id, cwd = sys.argv[1:4]
with BridgeStore(Path(os.environ["LLM_BRIDGE_STATE_DIR"]) / "bridge.sqlite3") as store:
    store.set_mission_status(mission_id, "RUNNING")
    task = store.list_tasks(mission_id)[0]["id"]
    op = store.begin_operation(task, iteration=1, station="codex")
    store.record_operation_session(op, restoration_state="NATIVE_CREATED", provider="codex", session_id=session_id, cwd=cwd, source="codex:session_meta")
`;

async function startWorker(coord: Coordinator): Promise<{ stop: () => void; cleanup: () => void }> {
  const state = mkdtempSync(join(tmpdir(), "bridge-e2e-su-"));
  const env = workerEnv(state);
  const bridge = (args: string[], input?: string) => execFileSync(python, [join(repo, "llm-bridge"), ...args], { env, encoding: "utf8", input, cwd: repo });
  const created = JSON.parse(bridge(["mission", "create", "--name", "Session mission", "--prompt", "Objective", "--cwd", tmpdir(), "--providers", "codex", "--json"])) as { mission: { id: string } };
  execFileSync(python, ["-c", RECORD, created.mission.id, SESSION_ID, tmpdir()], { env, cwd: repo });
  const token = coord.cli("worker", "add", "su-worker");
  // Read-only enrolment: no --allow-control. Both views must still work.
  bridge(["worker", "enroll", "--name", "su-worker", "--coordinator", coord.url.replace("https://", "wss://"), "--token-file", "-", "--ca-file", join(coord.dir, "ca.pem")], token);
  const child: ChildProcess = spawn(python, [join(repo, "llm-bridge"), "worker", "run"], { env, cwd: repo, stdio: "ignore" });
  return {
    stop: () => void child.kill("SIGKILL"),
    cleanup: () => {
      child.kill("SIGKILL");
      rmSync(state, { recursive: true, force: true });
    },
  };
}

test.describe("sessions and usage views", () => {
  test.skip(!existsSync(python), "repo .venv python is missing");

  test("show provider sessions and injected usage from a read-only worker, then report it offline", async ({ page, coord }) => {
    const fake = await startWorker(coord);
    try {
      await setupViaUi(page, coord);
      await navTo(page, "Workers");
      await expect(page.locator('[data-entity="worker:su-worker"] .pill')).toHaveText("Online", { timeout: 20_000 });

      await navTo(page, "Sessions");
      await expect(page.getByRole("heading", { name: "Sessions" })).toBeVisible();
      const row = page.locator('[data-entity^="session:"]').first();
      await expect(row).toBeVisible({ timeout: 20_000 });
      await expect(row).toContainText(/codex/i);
      await row.getByRole("button", { name: "Details" }).click();
      await expect(row.locator(`code[title="${SESSION_ID}"]`)).toBeVisible();
      await expect(row.getByRole("link", { name: "Session mission" })).toBeVisible();
      await expectNoHorizontalScroll(page);
      await expectAccessible(page, "sessions");

      await navTo(page, "Usage");
      await expect(page.getByRole("heading", { name: "Provider usage" })).toBeVisible();
      const card = page.locator('[data-entity="provider:codex"]');
      await expect(card).toBeVisible({ timeout: 20_000 });
      const fiveHour = card.getByRole("meter", { name: /5 hours/ });
      await expect(fiveHour).toHaveAttribute("aria-valuenow", "75");
      await expect(card).toContainText("75% left");
      await expect(card.getByRole("meter", { name: /Weekly/ })).toHaveAttribute("aria-valuenow", "90");
      await expect(card).toContainText("90% left");
      await expect(card).toContainText("resets");
      await expectNoHorizontalScroll(page);
      await expectAccessible(page, "usage");

      // Worker goes away: the page says so instead of failing silently.
      fake.stop();
      await expect(page.locator("#view-status")).toContainText("offline", { timeout: 30_000 });
      await expect(page.locator('[data-entity^="provider:"]')).toHaveCount(0);
      await expectAccessible(page, "usage offline");
    } finally {
      fake.cleanup();
    }
  });
});
