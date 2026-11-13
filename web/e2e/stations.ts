// A real tmux station with an echo loop as the "provider", plus a real Python worker enrolled for it.
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { chmodSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { Coordinator } from "./fixtures";

const repo = resolve(import.meta.dirname, "../..");
const python = join(repo, ".venv", "bin", "python");

const ECHO_LOOP = "import sys\nprint('ready', flush=True)\nprint('ok', flush=True)\nfor line in sys.stdin:\n    print('echo:' + line.strip(), flush=True)\n";
const START_STATION = `
import sys
from llm_tools.terminal import TmuxTerminalBackend
from llm_tools.bridge_runtime import mission_socket_path
mid, cwd, provider_code = sys.argv[1:4]
sock = mission_socket_path(mid)
handle = TmuxTerminalBackend().start(
    provider_argv=[sys.executable, '-u', '-c', provider_code],
    bridge_argv=[sys.executable, '-c', 'import time; time.sleep(600)'],
    cwd=cwd, socket_path=sock, session_name='mission-' + mid)
print(sock)
`;

export interface Station {
  missionId: string;
  socket: string;
  killStation: () => void;
  stopWorker: () => Promise<void>;
  cleanup: () => void;
}

/** Remote access the worker is enrolled with: "control" (view and write), "view" (view only) or "none". */
export type Access = "control" | "view" | "none";

export async function startStation(coord: Coordinator, access: Access): Promise<Station> {
  const state = mkdtempSync(join(tmpdir(), "bridge-e2e-term-"));
  const runtime = mkdtempSync("/tmp/lbe-");
  chmodSync(runtime, 0o700);
  const env: NodeJS.ProcessEnv = {};
  for (const [k, v] of Object.entries(process.env)) if (!k.startsWith("LLM_") && k !== "TMUX" && k !== "TMUX_PANE") env[k] = v;
  env["LLM_BRIDGE_STATE_DIR"] = state;
  env["XDG_RUNTIME_DIR"] = runtime;
  const bridge = (args: string[], input?: string) => execFileSync(python, [join(repo, "llm-bridge"), ...args], { env, encoding: "utf8", input, cwd: repo });
  const created = JSON.parse(bridge(["mission", "create", "--prompt", "Terminal E2E mission", "--cwd", tmpdir(), "--providers", "codex", "--json"])) as { mission: { id: string } };
  const missionId = created.mission.id;
  // The station below stands for a started mission: the mission page only offers a live session for a mission that has been started.
  execFileSync(python, ["-c", "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute(\"UPDATE missions SET status = 'RUNNING' WHERE id = ?\", (sys.argv[2],)); c.commit()", join(state, "bridge.sqlite3"), missionId], { env, cwd: repo });
  const socket = execFileSync(python, ["-c", START_STATION, missionId, tmpdir(), ECHO_LOOP], { env, encoding: "utf8", cwd: repo }).trim();
  const token = coord.cli("worker", "add", "e2e-worker");
  bridge(["worker", "enroll", "--name", "e2e-worker", "--coordinator", coord.url.replace("https://", "wss://"), "--token-file", "-", "--ca-file", join(coord.dir, "ca.pem"), ...(access === "control" ? ["--allow-control"] : access === "view" ? ["--allow-terminal-view"] : [])], token);
  const child: ChildProcess = spawn(python, [join(repo, "llm-bridge"), "worker", "run"], { env, cwd: repo, stdio: process.env["E2E_WORKER_LOG"] ? "inherit" : "ignore" });
  const killStation = () => {
    try {
      execFileSync("tmux", ["-S", socket, "kill-server"], { stdio: "ignore" });
    } catch {
      // already gone
    }
  };
  return {
    missionId,
    socket,
    killStation,
    stopWorker: async () => {
      const exited = new Promise<void>((r) => child.once("exit", () => r()));
      child.kill("SIGTERM");
      await Promise.race([exited, new Promise((r) => setTimeout(r, 8000))]);
    },
    cleanup: () => {
      child.kill("SIGKILL");
      killStation();
      rmSync(state, { recursive: true, force: true });
      rmSync(runtime, { recursive: true, force: true });
    },
  };
}


