// End-to-end with a real fake provider, worker, coordinator and Bridge mission database.
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { expect, navTo, setupViaUi, test, type Coordinator } from "./fixtures";

const repo = resolve(import.meta.dirname, "../..");
const python = join(repo, ".venv", "bin", "python");

function cleanEnv(state: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) if (!k.startsWith("LLM_") && k !== "TMUX" && k !== "TMUX_PANE") env[k] = v;
  env["LLM_BRIDGE_STATE_DIR"] = state;
  env["SOAK_PROVIDER_MIX"] = "1,0,0,0";
  env["SOAK_PROVIDER_SPEED"] = "0.25";
  env["SOAK_PROVIDER_LINES"] = "30";
  return env;
}

interface Fake {
  missionId: string;
  stopWorker: () => Promise<void>;
  cleanup: () => void;
}

async function startFakeWorker(coord: Coordinator): Promise<Fake> {
  const state = mkdtempSync(join(tmpdir(), "bridge-e2e-first-success-"));
  const env = cleanEnv(state);
  const bridge = (args: string[], input?: string) => execFileSync(python, [join(repo, "llm-bridge"), ...args], { env, encoding: "utf8", input, cwd: repo });
  let started: ChildProcess | undefined;
  try {
    const provider = join(repo, "scripts", "bridge_soak_provider.py");
    const created = JSON.parse(bridge([
      "mission", "create", "--prompt", "First success walkthrough", "--cwd", state,
      "--mode", "background", "--new", "--providers", "codex", "--verifier", "true",
      "--command-template", `${python} ${provider}`, "--json",
    ])) as { mission: { id: string } };
    const missionId = created.mission.id;
    const token = coord.cli("worker", "add", "e2e-first-success-worker");
    bridge(["worker", "enroll", "--name", "e2e-first-success-worker", "--coordinator", coord.url.replace("https://", "wss://"), "--token-file", "-", "--ca-file", join(coord.dir, "ca.pem"), "--allow-control"], token);
    const child = spawn(python, [join(repo, "llm-bridge"), "worker", "run"], { env, cwd: repo, stdio: "ignore" });
    started = child;
    bridge(["start", "--mission", missionId]);
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
  } catch (error) {
    started?.kill("SIGKILL");
    rmSync(state, { recursive: true, force: true });
    throw error;
  }
}

test.describe("first success", () => {
  test.skip(!existsSync(python), "repo .venv python is missing");

  test("first success: an enrolled worker runs a mission and the console shows the provider output live", async ({ page, coord }) => {
    const fake = await startFakeWorker(coord);
    try {
      await setupViaUi(page, coord);
      await navTo(page, "Missions");
      const row = page.locator('[data-entity^="mission:"]').first();
      await expect(row).toBeVisible({ timeout: 30_000 });
      await row.locator("a.row-title").click();
      // The mission page streams the provider terminal through the coordinator (background mode gives the mission a tmux station); the fake provider prints "step N/30" lines.
      await expect(page.locator("main")).toContainText(/step \d+\/30/, { timeout: 60_000 });
    } finally {
      await fake.stopWorker();
      fake.cleanup();
    }
  });
});
