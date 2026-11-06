// A real Python worker with a seeded Bridge database: many missions, and one mission with tasks in several states.
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { Coordinator } from "./fixtures";

const repo = resolve(import.meta.dirname, "../..");
export const python = join(repo, ".venv", "bin", "python");
export const havePython = existsSync(python);

const SEED = `
import os, sys
from pathlib import Path
from llm_tools.bridge_store import BridgeStore
count, cwd = int(sys.argv[1]), sys.argv[2]
with BridgeStore(Path(os.environ["LLM_BRIDGE_STATE_DIR"]) / "bridge.sqlite3") as store:
    ids = []
    for i in range(count):
        name = ("Parser refactor " if i % 3 == 0 else "Docs sweep " if i % 3 == 1 else "Release prep ") + f"{i:02d}"
        m = store.create_mission(name, "Seeded objective", cwd=cwd)
        ids.append(m["id"])
    first = ids[0]
    t = [store.add_task(first, title + " in detail", title=title) for title in ["Plan the change", "Implement it", "Write tests", "Review the diff", "Ship it"]]
    # VERIFYING and FAILED are reached by the controller; write them directly (the counting triggers still run).
    store.db.execute("UPDATE tasks SET status='VERIFYING' WHERE id=?", (t[1],))
    store.db.execute("UPDATE tasks SET status='FAILED' WHERE id=?", (t[2],))
    store.set_task_status(t[3], "BLOCKED")
    store.set_mission_status(first, "RUNNING")
    if len(ids) > 1:
        store.db.execute("UPDATE missions SET status='FAILED' WHERE id=?", (ids[1],))
    if len(ids) > 2:
        store.db.execute("UPDATE missions SET status='COMPLETED' WHERE id=?", (ids[2],))
    print(first)
`;

export interface Seeded {
  stopWorker: () => Promise<void>;
  cleanup: () => void;
  state: string;
  env: NodeJS.ProcessEnv;
  bridge: (args: string[], input?: string) => string;
}

export async function startSeededWorker(coord: Coordinator, opts: { name?: string; missions?: number; control?: boolean } = {}): Promise<Seeded> {
  const name = opts.name ?? "seed-worker";
  const state = mkdtempSync(join(tmpdir(), "bridge-e2e-seed-"));
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) if (!k.startsWith("LLM_") && k !== "TMUX" && k !== "TMUX_PANE") env[k] = v;
  env["LLM_BRIDGE_STATE_DIR"] = state;
  const bridge = (args: string[], input?: string) => execFileSync(python, [join(repo, "llm-bridge"), ...args], { env, encoding: "utf8", input, cwd: repo });
  const n = opts.missions ?? 1;
  if (n > 0) execFileSync(python, ["-c", SEED, String(n), tmpdir()], { env, cwd: repo });
  const token = coord.cli("worker", "add", name);
  bridge(["worker", "enroll", "--name", name, "--coordinator", coord.url.replace("https://", "wss://"), "--token-file", "-", "--ca-file", join(coord.dir, "ca.pem"), ...(opts.control === false ? [] : ["--allow-control"])], token);
  const child: ChildProcess = spawn(python, [join(repo, "llm-bridge"), "worker", "run"], { env, cwd: repo, stdio: process.env["E2E_WORKER_LOG"] ? "inherit" : "ignore" });
  return {
    state,
    env,
    bridge,
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
