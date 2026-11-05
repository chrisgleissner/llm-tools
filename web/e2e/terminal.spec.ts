// Remote terminal end-to-end: browser xterm -> coordinator relay -> real Python worker -> real tmux station.
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { chmodSync, existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import type { Page } from "@playwright/test";
import { expect, expectAccessible, expectNoHorizontalScroll, setupViaUi, test, type Coordinator } from "./fixtures";

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

interface Station {
  missionId: string;
  socket: string;
  killStation: () => void;
  stopWorker: () => Promise<void>;
  cleanup: () => void;
}

/** Remote access the worker is enrolled with: "control" (view and write), "view" (view only) or "none". */
type Access = "control" | "view" | "none";

async function startStation(coord: Coordinator, access: Access): Promise<Station> {
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


/** Click a terminal button and wait for the session; the page may re-render while it loads, so retry a click that was lost. */
async function openTerminal(page: Page, button: string): Promise<void> {
  const status = page.locator("#terminal-status");
  await expect(async () => {
    if ((await status.getAttribute("data-state")) === "idle") await page.locator(button).click({ timeout: 2000 });
    await expect(status).toHaveAttribute("data-state", "live", { timeout: 4000 });
  }).toPass({ timeout: 30_000 });
}

/** The rendered terminal rows. xterm.js pauses painting while the terminal is outside the
 * viewport (content above it can grow while the test runs), so scroll it into view first. */
async function expectScreen(page: Page, text: string, present = true): Promise<void> {
  const rows = page.locator("#terminal-host .xterm-rows");
  await expect(async () => {
    await page.locator("#terminal-host").scrollIntoViewIfNeeded({ timeout: 2000 });
    if (present) await expect(rows).toContainText(text, { timeout: 2000 });
    else await expect(rows).not.toContainText(text, { timeout: 2000 });
  }).toPass({ timeout: 20_000 });
}

test.describe("remote terminal with a real worker and tmux station", () => {
  test.skip(!existsSync(python), "repo .venv python is missing");
  test.skip(() => {
    try {
      execFileSync("tmux", ["-V"], { stdio: "ignore" });
      return false;
    } catch {
      return true;
    }
  }, "tmux is not installed");

  test("view, take control, single writer, copy and input paths", async ({ page, context, coord }) => {
    const st = await startStation(coord, "control");
    try {
      await setupViaUi(page, coord);
      const url = `/missions/e2e-worker/${st.missionId}`;
      await expect(async () => {
        await page.goto(url);
        await expect(page.locator("#terminal-open")).toBeEnabled({ timeout: 2000 });
      }).toPass({ timeout: 30_000 });

      // 1. Take control: granted write, typed input reaches the provider pane and its output comes back.
      const status = page.locator("#terminal-status");
      await openTerminal(page, "#terminal-control");
      await expect(status).toHaveAttribute("data-mode", "write");
      await expect(status).toContainText("you have control");
      await expectScreen(page, "ok");
      await page.locator("#terminal-host .xterm-helper-textarea").focus();
      await page.keyboard.type("hello");
      await page.keyboard.press("Enter");
      await expectScreen(page, "echo:hello");
      await expectNoHorizontalScroll(page);
      await expectAccessible(page, "mission detail with terminal");

      // 2. A second tab asking for write is granted view only; its input never reaches the pane.
      const second = await context.newPage();
      await second.goto(url);
      await expect(second.locator("#terminal-control")).toBeEnabled({ timeout: 30_000 });
      const status2 = second.locator("#terminal-status");
      await openTerminal(second, "#terminal-control");
      await expect(status2).toHaveAttribute("data-mode", "view");
      await expect(status2).toContainText("view only");
      await expect(status2).toContainText("writer role");
      await expectScreen(second, "echo:hello"); // fresh snapshot of the shared pane
      await second.locator("#terminal-host .xterm-helper-textarea").focus();
      await second.keyboard.type("blocked");
      await second.keyboard.press("Enter");
      // The writer tab keeps working and the pane never echoes what the view-only tab typed.
      await page.bringToFront();
      await page.locator("#terminal-host .xterm-helper-textarea").focus();
      await page.keyboard.type("again");
      await page.keyboard.press("Enter");
      await expectScreen(page, "echo:again");
      await second.bringToFront();
      await expectScreen(second, "echo:again"); // live output reaches the viewer
      await expectScreen(page, "echo:blocked", false);
      await expectScreen(second, "echo:blocked", false);

      // 3. Closing the writer frees the role: a new "Take control" in the second tab is granted write.
      await page.bringToFront();
      await page.locator("#terminal-close").click();
      await expect(page.locator("#terminal-open")).toBeEnabled();
      await second.bringToFront();
      await second.locator("#terminal-close").click();
      await openTerminal(second, "#terminal-control");
      await expect(status2).toHaveAttribute("data-mode", "write", { timeout: 20_000 });
      await second.locator("#terminal-host .xterm-helper-textarea").focus();
      await second.keyboard.type("second");
      await second.keyboard.press("Enter");
      await expectScreen(second, "echo:second");
      await second.close();
      await page.bringToFront();

      // 4. Plain view mode: no control requested, input is not sent.
      await openTerminal(page, "#terminal-open");
      await expect(status).toHaveAttribute("data-mode", "view", { timeout: 20_000 });
      await expectScreen(page, "echo:second");
    } finally {
      st.cleanup();
    }
  });

  test("a read-only worker grants view only, and the session ends when the station stops", async ({ page, coord }) => {
    const st = await startStation(coord, "view");
    try {
      await setupViaUi(page, coord);
      const url = `/missions/e2e-worker/${st.missionId}`;
      await expect(async () => {
        await page.goto(url);
        await expect(page.locator("#terminal-control")).toBeEnabled({ timeout: 2000 });
      }).toPass({ timeout: 30_000 });
      const status = page.locator("#terminal-status");
      await openTerminal(page, "#terminal-control");
      await expect(status).toHaveAttribute("data-mode", "view");
      await expect(status).toContainText("read-only");
      await expectScreen(page, "ok");
      st.killStation();
      await expect(status).toHaveAttribute("data-state", "ended", { timeout: 20_000 });
      await expect(status).toContainText("station ended");
      await expect(page.locator("#terminal-open")).toBeEnabled();
    } finally {
      st.cleanup();
    }
  });

  test("a read-only worker without the terminal opt-in refuses terminals", async ({ page, coord }) => {
    const st = await startStation(coord, "none");
    try {
      await setupViaUi(page, coord);
      const url = `/missions/e2e-worker/${st.missionId}`;
      await expect(async () => {
        await page.goto(url);
        await expect(page.locator("#terminal-open")).toBeEnabled({ timeout: 2000 });
      }).toPass({ timeout: 30_000 });
      await page.locator("#terminal-open").click();
      const status = page.locator("#terminal-status");
      await expect(status).toContainText("--allow-terminal-view", { timeout: 20_000 });
      await expect(status).not.toHaveAttribute("data-state", "live");
      // The worker refused before sending any output, so nothing was ever written to the terminal.
      await expect(page.locator("#terminal-host .xterm-rows")).not.toContainText("ok");
    } finally {
      st.cleanup();
    }
  });

  test("an unknown mission is refused with the worker's reason", async ({ page, coord }) => {
    const st = await startStation(coord, "control");
    try {
      await setupViaUi(page, coord);
      await expect(async () => {
        await page.goto(`/missions/e2e-worker/${st.missionId}`);
        await expect(page.locator("#terminal-open")).toBeEnabled({ timeout: 2000 });
      }).toPass({ timeout: 30_000 });
      await st.stopWorker();
      await expect(page.locator("#terminal-open")).toBeDisabled({ timeout: 20_000 });
    } finally {
      st.cleanup();
    }
  });
});
